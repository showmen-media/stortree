"""stortree config resolution.

Pure functions only -- no file/network/Ansible I/O beyond what's passed
in as arguments, so this stays unit-testable with plain pytest (see
tests/test_stortree.py) and reusable outside a running playbook. See
docs/spec.md §1 for the design this implements, and docs/plan.md
"Open interpretation calls" for the handful of judgment calls made where
the spec leaves something implicit.

Two Jinja filters are exposed to plays via FilterModule at the bottom:

- stortree_resolve(tree, hostname, all_hosts) -- everything a given host
  must do: server subtrees it owns, its client mount of the tree root
  (if any -- peer-sourced from the resolved root host, not the root's
  own third-party remote), every Samba share in the tree (Samba sharing
  is universal), and its peer dependencies/peer_served_by for cross-host
  sourcing.
- stortree_filter_rclone_conf(rclone_conf_text, resolved, hostvars) --
  the master rclone.conf INI filtered down to only the sections a host's
  resolved facts actually reference, plus synthesized sftp sections for
  its peer dependencies.
"""

from __future__ import annotations

import configparser
import io
import re

# Interpretation call #1 (docs/plan.md): the dotted access shorthand
# (`access.group: X`, `access.owner: X`) never specifies `permissions` in
# any example in docs/config-schema.md, and no default is stated. Every
# example use is a user/group being granted their own private subtree, so
# default to full control. One-line change here if that's wrong.
DEFAULT_ACCESS_PERMISSIONS = "rwx"

# Placeholder path segment for the per-user directory a user-subdirs
# descendant's real path can't be known until apply time (the actual
# username set comes from LDAP/SSSD group membership, resolved by the
# role via `getent`, not from this pure function -- see interpretation
# call #2). Matches Samba's own %U connecting-user token so the same
# placeholder shows up consistently in smb.conf and in resolved paths.
PER_USER_PLACEHOLDER = "%U"

# Convention used by stortree_peer_trust/stortree_secrets: where a
# serving host's SSH keypair for peer sftp mounts lives.
PEER_SSH_KEY_PATH = "/etc/stortree/peer_ssh_key"


def _deep_merge(dst, src):
    """Merge src into dst in place (dict values merge recursively, other
    values are overwritten by src), and return dst."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _expand_dotted(obj):
    """Recursively expand dotted keys into nested mappings.

    Splits on the *last* dot only (`rpartition`), not every dot: this is
    what correctly turns a literal, dot-containing key like
    `.cache.subdirs` into `{".cache": {"subdirs": {...}}}` (see
    docs/config-schema.md "A dotted-path map key") rather than shredding
    it on every `.`. All the two-segment shorthands used elsewhere
    (`rclone.remote`, `access.group`, ...) only have one dot, so last-dot
    and first-dot splitting agree there.
    """
    if isinstance(obj, dict):
        result: dict = {}
        for k, v in obj.items():
            v = _expand_dotted(v)
            if isinstance(k, str) and "." in k:
                outer, _, inner = k.rpartition(".")
                piece = {outer: {inner: v}}
            else:
                piece = {k: v}
            _deep_merge(result, piece)
        return result
    if isinstance(obj, list):
        return [_expand_dotted(x) for x in obj]
    return obj


def _normalize_access(raw):
    """Normalize `access` into a single {group?, owner?, permissions}
    dict -- never a list (docs/config-schema.md "Access"). A remote-backed
    node can only ever carry real, kernel-enforced access as plain Unix
    ownership + mode (rclone's FUSE mount has no POSIX ACL support, spec.md
    §6) -- which is exactly what one owner + one group + one shared
    permissions level can express, and no more, so the schema doesn't let
    you write anything that can't actually be enforced. `raw` with
    neither `group` nor `owner` (including None) normalizes to `{}`."""
    if raw is None:
        raw = {}
    if isinstance(raw, list):
        raise ValueError(
            "access must be a single object ({group?, owner?, permissions?}), "
            "not a list -- see docs/config-schema.md \"Access\""
        )
    entry = dict(raw)
    if not (entry.get("group") or entry.get("owner")):
        return {}
    entry.setdefault("permissions", DEFAULT_ACCESS_PERMISSIONS)
    return entry


def _permission_bits(permissions):
    """'rwx'-style string -> the numeric 0-7 mode bits it represents."""
    bits = 0
    if "r" in permissions:
        bits |= 4
    if "w" in permissions:
        bits |= 2
    if "x" in permissions:
        bits |= 1
    return bits


def access_owner(access, default_owner):
    """The Unix owner a node's `access` implies -- the granted `owner` if
    one's set, else `default_owner` (stortree_user), which also keeps
    full control (see access_mode()) so it can always administer the
    path regardless of who else is granted access to it."""
    return (access or {}).get("owner") or default_owner


def access_group(access, default_group):
    """The Unix group a node's `access` implies -- the granted `group` if
    one's set, else `default_group` (stortree_group)."""
    return (access or {}).get("group") or default_group


def access_mode(access):
    """The Unix mode a node's `access` implies, as a "0NNN" string ready
    for ansible.builtin.file/rclone's --dir-perms/--file-perms alike.
    `other` is always 0 -- nothing here is ever meant to be world-
    readable. With neither `owner` nor `group` granted, this is the
    plain default (owner: stortree, full control; group: stortree,
    read+traverse) every path had before `access` existed. Granting just
    one of the two still gives the *other* slot its own sensible default
    rather than leaving it at 0: an explicit `owner` grant still lets
    stortree itself administer the path (owner bits stay full even
    though the path's actual Unix owner is the granted user, not
    stortree); an explicit `group` grant makes the path private to that
    group with no separate stortree-group carve-out, since it was
    deliberately scoped to someone else."""
    access = access or {}
    if not (access.get("owner") or access.get("group")):
        return "0750"
    bits = _permission_bits(access.get("permissions", DEFAULT_ACCESS_PERMISSIONS))
    owner_bits = bits if access.get("owner") else 7
    group_bits = bits if access.get("group") else 0
    return f"0{owner_bits}{group_bits}0"


def _walk_tree(tree):
    """One host-independent walk of the whole tree.

    Returns (root_host, root_remote, nodes) where `nodes` is a flat list
    of every resolved subdirs/user-subdirs node (root itself is excluded
    -- it's the inheritance anchor, not a mountable node of its own, see
    docs/spec.md "Node inheritance"). `host` inherits down the tree;
    `rclone` -- both `remote` and `args` -- never inherits (spec.md §1):
    a node with no `rclone.remote` of its own resolves to `remote: None`,
    regardless of what any ancestor (root included) sets.

    A node that resolves with `remote: None` isn't a separate mounted
    subtree -- see docs/config-schema.md "Node inheritance" for what that
    means downstream (plan_mounts() below turns it into a plain directory
    to create rather than an rclone mount).
    """
    root_host = tree.get("host")
    root_remote = (tree.get("rclone") or {}).get("remote")
    nodes = []

    def _visit(node, path_parts, host, per_user):
        h = node.get("host", host)
        r = (node.get("rclone") or {}).get("remote")
        args = (node.get("rclone") or {}).get("args") or {}
        access = _normalize_access(node.get("access"))
        samba = node.get("samba")
        path = "/".join(path_parts)
        if path_parts:
            nodes.append(
                {
                    "path": path,
                    "host": h,
                    "remote": r,
                    "args": args,
                    "access": access,
                    "samba": samba,
                    "per_user": per_user,
                }
            )
        for name, child in (node.get("subdirs") or {}).items():
            _visit(child or {}, path_parts + [name], h, per_user)
        for name, child in (node.get("user-subdirs") or {}).items():
            _visit(
                child or {},
                path_parts + [PER_USER_PLACEHOLDER, name],
                h,
                True,
            )

    _visit(tree, [], root_host, False)
    return root_host, root_remote, nodes


def _samba_nodes(nodes):
    return [n for n in nodes if n.get("samba")]


def _descendants_of(samba_node, nodes):
    prefix = samba_node["path"] + "/"
    return [samba_node] + [n for n in nodes if n["path"].startswith(prefix)]


def _has_own_content(node, nodes):
    """Whether `node` is something a peer actually needs its own mount
    for, rather than a pure structural container. A node with its own
    `remote` always does (that's the authoritative source for that exact
    path regardless of what's nested under it -- same as any other
    mounted node). A remote-less node only does if it's a leaf: a
    remote-less node *with children* -- the samba node itself
    (`_descendants_of`'s own first element) is the common case, but any
    plain intermediate container works the same way -- delegates its real
    content entirely to those children, which are their own, more
    specific entries; peer-mounting the container too would be redundant
    at best (whatever's really there is already covered by its children)
    and actively wrong at worst (a peer mount at the container's path
    would become an unrelated ancestor of a same-path child this host
    owns outright, e.g. via server_subtrees -- forcing that child's own
    mount to require a peer connection it never needed)."""
    if node["remote"]:
        return True
    prefix = node["path"] + "/"
    return not any(n["path"].startswith(prefix) for n in nodes)


def _dedupe(items, key):
    seen = set()
    result = []
    for item in items:
        k = key(item)
        if k not in seen:
            seen.add(k)
            result.append(item)
    return result


def resolve(tree, hostname, all_hosts):
    """Resolve everything host `hostname` must do, given the parsed
    contents of config.yml (`tree`) and the full inventory host list
    (`all_hosts`, so a host with no mention in config.yml still resolves
    as a full participant -- config-schema.md "Every inventory host
    participates")."""
    tree = _expand_dotted(tree)
    root_host, root_remote, nodes = _walk_tree(tree)

    server_subtrees = [n for n in nodes if n["host"] == hostname]

    # Every peer mount this host ends up with -- root-level or a samba
    # descendant alike -- uses this host's own client-style args (never the
    # owning host's own rclone.args, which were tuned for its direct
    # connection to the real backend, not this peer-sftp hop): the base
    # `client-defaults`, with this host's own `clients.<hostname>.rclone.args`
    # entry (if any) merged over it. Computed once, up front, so both the
    # samba-descendant loop below and the root client mount can attach it
    # to their own peer_dependencies entries without recomputing it.
    defaults = ((tree.get("client-defaults") or {}).get("rclone") or {}).get(
        "args"
    ) or {}
    override = (
        ((tree.get("clients") or {}).get(hostname) or {}).get("rclone") or {}
    ).get("args") or {}
    peer_mount_args = _deep_merge(dict(defaults), dict(override))

    samba_nodes = _samba_nodes(nodes)
    samba_shares = []
    peer_dependencies = []

    for s in samba_nodes:
        descendants = _descendants_of(s, nodes)

        # Each descendant carries at most one access grant now (never a
        # list, see _normalize_access()) -- the union across descendants
        # is still a list, just of (at most) one grant per descendant
        # rather than several from any single one.
        access_union = []
        for d in descendants:
            a = d.get("access")
            if a and a not in access_union:
                access_union.append(a)

        samba_shares.append(
            {
                "node_path": s["path"],
                "local_path": s["path"],
                "subpath": (s.get("samba") or {}).get("subpath"),
                "access": access_union,
                "descendants": [
                    {
                        "path": d["path"],
                        "owning_host": d["host"],
                        "remote": d["remote"],
                        "args": d["args"],
                        "access": d["access"],
                        "per_user": d["per_user"],
                    }
                    for d in descendants
                ],
            }
        )

        for d in descendants:
            if d["host"] != hostname and _has_own_content(d, nodes):
                peer_dependencies.append(
                    {
                        "owning_host": d["host"],
                        "local_path": d["path"],
                        "remote_path": d["path"],
                        "samba_node": s["path"],
                        "per_user": d["per_user"],
                        "access": d["access"],
                        "args": peer_mount_args,
                    }
                )

    # Client mount of the tree root: a non-root host reaches root's data by
    # peer-sftp'ing the host that actually owns it (root_host) rather than
    # holding direct credentials to root_remote itself -- the same
    # peer-sourcing rule already applied above to every samba descendant a
    # host doesn't own, just for the one root-level piece that isn't a node
    # in `nodes` at all (root is the inheritance anchor, not a mountable
    # node of its own -- see _walk_tree()). A root with no rclone.remote of
    # its own has nothing to peer for -- the client still gets its local
    # root directory created by stortree_mounts, just no mount at all, same
    # as before this peer-sourcing existed.
    client_mounts = []
    if hostname != root_host:
        client_remote = None
        if root_remote:
            peer_dependencies.append(
                {
                    "owning_host": root_host,
                    "local_path": "",
                    "remote_path": "",
                    "samba_node": None,
                    "per_user": False,
                    "access": [],
                    "args": peer_mount_args,
                }
            )
            client_remote = _peer_remote_ref(root_host, "", "")
        client_mounts.append(
            {"remote": client_remote, "path": "", "args": peer_mount_args}
        )

    peer_dependencies = _dedupe(
        peer_dependencies, lambda p: (p["owning_host"], p["local_path"])
    )

    peer_served_by = []
    for other in all_hosts:
        if other == hostname:
            continue
        if hostname == root_host and root_remote:
            peer_served_by.append(
                {
                    "serving_host": other,
                    "local_path": "",
                    "samba_node": None,
                    "per_user": False,
                }
            )
        for s in samba_nodes:
            for d in _descendants_of(s, nodes):
                if d["host"] == hostname and _has_own_content(d, nodes):
                    peer_served_by.append(
                        {
                            "serving_host": other,
                            "local_path": d["path"],
                            "samba_node": s["path"],
                            "per_user": d["per_user"],
                        }
                    )
    peer_served_by = _dedupe(
        peer_served_by, lambda p: (p["serving_host"], p["local_path"])
    )

    return {
        "server_subtrees": server_subtrees,
        "client_mounts": client_mounts,
        "samba_shares": samba_shares,
        "peer_dependencies": peer_dependencies,
        "peer_served_by": peer_served_by,
    }


def _remote_section(remote_spec):
    if not remote_spec:
        return None
    return remote_spec.split(":", 1)[0]


def _peer_section_name(owning_host, local_path):
    # Empty local_path is the tree root's own reserved slug (matches
    # _slug()'s "root" convention below) -- the one peer dependency that
    # isn't a nodes() descendant, synthesized for client_mounts instead.
    slug = local_path.replace("/", "-").replace("%", "pct") or "root"
    return f"peer-{owning_host}-{slug}"


def _peer_remote_ref(owning_host, local_path, remote_path):
    """The full `remote:path` rclone reference for a peer-sftp mount.
    rclone's sftp backend has no working way to bake a root path into
    the remote's own .conf section -- a `path` key there is silently
    ignored and the session lands in the login user's home directory
    instead (rclone issue #4307) -- so the absolute path has to be
    appended to the remote reference itself, matching the same `path`
    filter_rclone_conf() writes into that section for documentation."""
    section = _peer_section_name(owning_host, local_path)
    path = f"/srv/stortree/{remote_path}" if remote_path else "/srv/stortree"
    return f"{section}:{path}"


def filter_rclone_conf(rclone_conf_text, resolved, hostvars=None, group_members=None):
    """Filter the master rclone.conf INI down to only the sections
    `resolved` (this host's stortree_resolve() output) actually needs,
    plus one synthesized sftp section per peer dependency (spec.md §3).
    `hostvars` (Ansible's own magic var, or any {hostname: {ansible_host:
    ...}} mapping) supplies the address to reach each peer's owning host
    at; falls back to the owning hostname itself if not given.
    `group_members` (e.g. `ansible_facts.getent_group |
    stortree_group_members`) resolves a per-user peer dependency's %U
    template into one section per actual user, exactly the way
    `plan_mounts()` independently expands the same entry into one mount
    per user -- both have to agree on `_peer_section_name()`'s input (the
    expanded, not templated, path) since that's what ties a mount's
    `remote` back to the section actually holding its credentials.
    """
    hostvars = hostvars or {}
    group_members = group_members or {}

    master = configparser.ConfigParser()
    master.read_string(rclone_conf_text)

    needed = set()
    for entry in resolved.get("server_subtrees", []):
        needed.add(_remote_section(entry.get("remote")))
    for entry in resolved.get("client_mounts", []):
        needed.add(_remote_section(entry.get("remote")))
    for share in resolved.get("samba_shares", []):
        for d in share.get("descendants", []):
            needed.add(_remote_section(d.get("remote")))
    needed.discard(None)

    out = configparser.ConfigParser()
    for section in master.sections():
        if section in needed:
            out[section] = dict(master[section])

    for peer in resolved.get("peer_dependencies", []):
        if peer.get("per_user"):
            users = access_grant_usernames(peer.get("access"), group_members)
            expanded = [
                (
                    peer["local_path"].replace(PER_USER_PLACEHOLDER, u),
                    peer["remote_path"].replace(PER_USER_PLACEHOLDER, u),
                )
                for u in users
            ]
        else:
            expanded = [(peer["local_path"], peer["remote_path"])]

        address = (hostvars.get(peer["owning_host"]) or {}).get(
            "ansible_host", peer["owning_host"]
        )
        for local_path, remote_path in expanded:
            section_name = _peer_section_name(peer["owning_host"], local_path)
            out[section_name] = {
                "type": "sftp",
                "host": address,
                "user": "stortree",
                "key_file": PEER_SSH_KEY_PATH,
                "shell_type": "unix",
                "path": f"/srv/stortree/{remote_path}" if remote_path else "/srv/stortree",
            }

    buf = io.StringIO()
    out.write(buf)
    return buf.getvalue()


_SLUG_UNSAFE_CHAR = re.compile(r"[^A-Za-z0-9_.]")


def _escape_slug_segment(segment):
    """Escape one path segment so "-" is only ever a literal segment
    separator in the slug it's joined into -- a "-" (or any other
    non-[A-Za-z0-9_.] character, e.g. the "%" in PER_USER_PLACEHOLDER)
    that's part of the segment's own name becomes \\xHH instead, using
    the same convention systemd-escape itself uses for generated unit
    instance names."""
    return _SLUG_UNSAFE_CHAR.sub(lambda m: f"\\x{ord(m.group()):02x}", segment)


def _slug(path):
    """Turn a resolved node's `/`-joined tree path into a systemd
    instance name, unambiguously: each segment is escaped on its own
    (see _escape_slug_segment) before being rejoined with "-", so a
    segment literally named "foo-bar" can no longer collapse onto the
    same slug as nested "foo/bar" the way a naive "/" -> "-" replacement
    would. The empty (root) path maps to the reserved "root" -- see the
    explicit uniqueness check in plan_mounts() for the backstop against
    the one remaining case this doesn't rule out on its own (a real
    top-level segment literally named "root")."""
    if not path:
        return "root"
    return "-".join(_escape_slug_segment(seg) for seg in path.split("/"))


def group_members_from_getent(getent_group):
    """Convert Ansible's `ansible_facts.getent_group` (as populated by
    looping the `ansible.builtin.getent` module over every group name
    referenced in access grants) into a plain {group_name: [usernames]}
    map. `getent_group` entries are [password, gid, "user1,user2,..."].
    """
    result = {}
    for name, fields in (getent_group or {}).items():
        members = fields[2] if len(fields) > 2 and fields[2] else ""
        result[name] = [u for u in members.split(",") if u]
    return result


def group_gids_from_getent(getent_group):
    """Convert the same `ansible_facts.getent_group` data into a plain
    {group_name: gid} map (`getent_group` entries are [password, gid,
    members] -- see group_members_from_getent() above, which reuses the
    same lookup for membership). Used by stortree_mounts to gid-own a
    remote-backed node's rclone mount when its `access` grants a group
    (spec.md §6) -- the kernel can enforce that directly (real
    supplementary-group membership), even though the mount itself can
    never carry POSIX ACLs."""
    result = {}
    for name, fields in (getent_group or {}).items():
        if len(fields) > 1 and fields[1] is not None:
            result[name] = int(fields[1])
    return result


def user_uids_from_getent(getent_passwd):
    """Convert Ansible's `ansible_facts.getent_passwd` (looping
    `ansible.builtin.getent` over every username referenced in an
    `access.owner`) into a plain {username: uid} map. `getent_passwd`
    entries are [password, uid, gid, gecos, home, shell] -- mirrors
    group_gids_from_getent() above, just for the owner side of `access`
    instead of the group side."""
    result = {}
    for name, fields in (getent_passwd or {}).items():
        if len(fields) > 1 and fields[1] is not None:
            result[name] = int(fields[1])
    return result


def access_grant_usernames(access, group_members=None):
    """Resolve one `access` dict ({group?, owner?, permissions?}) to the
    sorted set of usernames a user-subdirs node should expand a per-user
    folder for. An explicit `owner` always pins a single folder to that
    one user -- `group` alongside it is still real (mount/ACL-level
    shared access to that same folder, access_mode()/access_group()),
    just not a second axis of *expansion*: one node still means one
    folder. Only a `group` grant with no `owner` expands into one folder
    per member (interpretation call #2: group membership is host-local
    via SSSD, not something this pure function can look up itself, hence
    `group_members`)."""
    access = access or {}
    if access.get("owner"):
        return [access["owner"]]
    if access.get("group"):
        return sorted((group_members or {}).get(access["group"], []))
    return []


def needed_groups(resolved):
    """Every group name this host's resolved facts reference in an
    `access` grant -- the set `getent group` needs to be run against
    before `group_members_from_getent()`'s result can feed
    `plan_mounts()`/`filter_rclone_conf()`'s own %U expansion, and before
    `group_gids_from_getent()`'s result can gid-own a remote-backed
    node's mount (spec.md §6) -- covers every node, not just per-user
    ones, since a plain shared (non-%U) node can be gid-owned by its
    `access.group` too, same as a per-user one. Covers both
    `server_subtrees` (this host's own nodes) and `peer_dependencies` (a
    samba descendant sourced from a peer, or the root client mount --
    which never carries `access`, so contributes nothing here) -- the two
    are computed once, together, by `stortree_facts` so every later role
    (`stortree_mounts`, `stortree_secrets`) shares one lookup and one
    consistent group_members/group_gids map, rather than each
    recomputing its own scope of it (and risking one missing a scope the
    others cover)."""
    groups = set()
    for n in resolved.get("server_subtrees", []):
        g = (n.get("access") or {}).get("group")
        if g:
            groups.add(g)
    for p in resolved.get("peer_dependencies", []):
        g = (p.get("access") or {}).get("group")
        if g:
            groups.add(g)
    return sorted(groups)


def needed_users(resolved):
    """Every username this host's resolved facts reference in an
    `access.owner` grant -- mirrors needed_groups() above, for the
    `getent passwd` lookup user_uids_from_getent() needs to uid-own a
    remote-backed node's mount (spec.md §6)."""
    users = set()
    for n in resolved.get("server_subtrees", []):
        u = (n.get("access") or {}).get("owner")
        if u:
            users.add(u)
    for p in resolved.get("peer_dependencies", []):
        u = (p.get("access") or {}).get("owner")
        if u:
            users.add(u)
    return sorted(users)


def plan_mounts(resolved, group_members=None):
    """Flatten this host's resolved server_subtrees/client_mounts/
    peer_dependencies into one flat plan of every local path that has to
    exist (spec.md §2), expanding a user-subdirs entry's %U-templated path
    into one entry per user actually granted access to it (interpretation
    call #2) using `group_members` (e.g. `ansible_facts.getent_group |
    stortree_group_members`).

    Not every entry is an rclone mount: a server_subtrees entry with
    `remote: None` (a node with no `rclone.remote` of its own -- it never
    inherits one, see _walk_tree()/docs/config-schema.md "Node
    inheritance") is a plain directory that has to exist, not a mount --
    callers should render an rclone unit only for entries with a
    truthy `remote`, e.g. `stortree_mounts_plan | selectattr('remote')`.
    A client_mounts entry always has a remote (the root `rclone.remote`).

    Every peer_dependencies entry becomes a mount too -- a samba
    descendant this host doesn't own is data this host's local tree still
    has to contain (spec.md §1 "Samba sharing is universal"), sourced
    directly from its actual owning host exactly like the root client
    mount is (mesh, not funneled through root_host -- each peer_dependency
    already names its own real owning host). The root-level entry
    (`local_path == ""`) is skipped here since it's already the
    client_mounts entry above; every other entry gets its own mount,
    per-user-expanded the same way as a per-user server_subtrees entry,
    using its own `access`/`args` (never the owning host's).

    Each returned entry: {local_path, remote, args, slug, requires_slug}.
    `requires_slug` names the nearest ancestor entry that's an actual
    mount (truthy `remote`) whose local_path is the longest proper-prefix
    ancestor of this one, if any -- for systemd RequiresMountsFor= so a
    nested mount starts after the mount it nests under, skipping over any
    non-mounted (plain-directory) ancestor in between, which has no unit
    of its own to require (spec.md §2).
    """
    group_members = group_members or {}
    entries = []

    for m in resolved.get("client_mounts", []):
        entries.append(
            {"local_path": "", "remote": m["remote"], "args": m["args"], "access": {}}
        )

    for n in resolved.get("server_subtrees", []):
        if not n.get("per_user"):
            entries.append(
                {
                    "local_path": n["path"],
                    "remote": n["remote"],
                    "args": n["args"],
                    "access": n["access"],
                }
            )
            continue
        for user in access_grant_usernames(n["access"], group_members):
            entries.append(
                {
                    "local_path": n["path"].replace(PER_USER_PLACEHOLDER, user),
                    "remote": n["remote"],
                    "args": n["args"],
                    "access": n["access"],
                }
            )

    for p in resolved.get("peer_dependencies", []):
        if p["local_path"] == "":
            continue  # already the client_mounts entry above
        if not p.get("per_user"):
            entries.append(
                {
                    "local_path": p["local_path"],
                    "remote": _peer_remote_ref(
                        p["owning_host"], p["local_path"], p["remote_path"]
                    ),
                    "args": p["args"],
                    "access": p.get("access") or {},
                }
            )
            continue
        for user in access_grant_usernames(p.get("access"), group_members):
            local_path = p["local_path"].replace(PER_USER_PLACEHOLDER, user)
            remote_path = p["remote_path"].replace(PER_USER_PLACEHOLDER, user)
            entries.append(
                {
                    "local_path": local_path,
                    "remote": _peer_remote_ref(p["owning_host"], local_path, remote_path),
                    "args": p["args"],
                    "access": p.get("access") or {},
                }
            )

    for e in entries:
        e["slug"] = _slug(e["local_path"])

    mount_entries = [e for e in entries if e["remote"]]

    seen_slugs = {}
    for e in mount_entries:
        clash = seen_slugs.get(e["slug"])
        if clash is not None:
            raise ValueError(
                f"stortree: {clash!r} and {e['local_path']!r} both resolve to "
                f"systemd unit slug {e['slug']!r} -- rename one of them"
            )
        seen_slugs[e["slug"]] = e["local_path"]

    for e in entries:
        best = None
        for other in mount_entries:
            if other is e:
                continue
            op = other["local_path"]
            is_ancestor = op == "" or e["local_path"].startswith(op + "/")
            if is_ancestor and (best is None or len(op) > len(best["local_path"])):
                best = other
        e["requires_slug"] = best["slug"] if best else None

    return entries


def samba_access_tokens(access_list, include_self=False):
    """Render a list of `access` grants ({group?, owner?, permissions?} --
    resolve()'s per-share access_union, at most one per descendant) into
    smb.conf's `valid users`/`write list` token list: one quoted token per
    granted principal. A single grant with both `owner` and `group` set
    contributes two tokens, one each -- both get in, not just one.
    Quoted because a principal name can contain a space (smb.conf(5)
    "lists" are otherwise whitespace-delimited, e.g. config-schema.md's
    "Michael Whitfield Family") -- see smb.conf.j2. `include_self`
    prepends `%U` itself (spec.md §6): a %U-templated share's own path
    already confines each connecting user to their own subtree, so their
    baseline access there shouldn't depend on any particular descendant's
    `access` grant existing at all."""
    tokens = ['"%U"'] if include_self else []
    for a in access_list:
        if a.get("group"):
            tokens.append(f'"@{a["group"]}"')
        if a.get("owner"):
            tokens.append(f'"{a["owner"]}"')
    return tokens


def mount_unit_names(mount_plan):
    """The full `stortree-mount@<slug>.service` unit filename for every
    actual rclone mount in a stortree_plan_mounts() result (entries with
    no `remote` are plain directories, not mounts -- see plan_mounts())
    -- used by stortree_mounts to work out which currently-installed
    units are stale."""
    return [f"stortree-mount@{e['slug']}.service" for e in mount_plan if e["remote"]]


class FilterModule(object):
    def filters(self):
        return {
            "stortree_resolve": resolve,
            "stortree_filter_rclone_conf": filter_rclone_conf,
            "stortree_group_members": group_members_from_getent,
            "stortree_group_gids": group_gids_from_getent,
            "stortree_user_uids": user_uids_from_getent,
            "stortree_access_users": access_grant_usernames,
            "stortree_access_owner": access_owner,
            "stortree_access_group": access_group,
            "stortree_access_mode": access_mode,
            "stortree_needed_groups": needed_groups,
            "stortree_needed_users": needed_users,
            "stortree_plan_mounts": plan_mounts,
            "stortree_mount_unit_names": mount_unit_names,
            "stortree_samba_access_tokens": samba_access_tokens,
        }
