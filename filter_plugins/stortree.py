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

# Segment substituted for PER_USER_PLACEHOLDER when a `group`-only grant
# backs every member's folder with one real, shared mount instead of one
# per member (interpretation call #2, revisited) -- see
# per_user_mount_path() below for why. Dot-prefixed to match this
# tree's existing hidden-subtree convention (`.cache`) and to keep it
# out of the way of any real per-user segment name, which %U itself
# forbids (Samba's own connecting-user names can't contain a literal
# "%" either).
SHARED_MOUNT_SEGMENT = ".mounts"

# Sibling-of-<username> segment name for a per-user container's staging
# directory (user_container_paths()) -- real content, sitting inside
# whatever remote-backed mount the container itself nests under, that a
# per-user "wrapper" rclone mount (the `local` backend) re-presents at
# the container's own path with that one user's real --uid/--gid/
# --dir-perms. Not dot-prefixed like SHARED_MOUNT_SEGMENT: nothing about
# it needs hiding from a %U-templated Samba share (a connecting user's
# own share root is their own container, `home/<them>` -- a *sibling*
# path like `home/stortree-user-<them>` is never reachable through it at
# all, same as any other sibling of their own folder), and unlike
# `.mounts` there's one of these per user, not one shared instance to set
# apart from real per-user segment names.
STORTREE_USER_PREFIX = "stortree-user-"

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

    A key whose *only* dot is its own leading character (e.g. the
    dot-prefixed hidden-subtree convention used bare, `.cache`, with no
    `.subdirs`/etc suffix after it) has an empty `outer` after
    `rpartition` -- that's never a real two-segment shorthand (nothing
    meaningfully nests under an empty key), so it's left as a literal
    key instead of being shredded into `{"": {"cache": {...}}}`.
    """
    if isinstance(obj, dict):
        result: dict = {}
        for k, v in obj.items():
            v = _expand_dotted(v)
            outer, sep, inner = k.rpartition(".") if isinstance(k, str) else ("", "", "")
            if sep and outer:
                piece = {outer: {inner: v}}
            else:
                piece = {k: v}
            _deep_merge(result, piece)
        return result
    if isinstance(obj, list):
        return [_expand_dotted(x) for x in obj]
    return obj


def _normalize_access(raw):
    """Normalize `access` into a single {group?, owner?, permissions,
    permissions_explicit} dict -- never a list (docs/config-schema.md
    "Access"). A remote-backed node can only ever carry real,
    kernel-enforced access as plain Unix ownership + mode (rclone's FUSE
    mount has no POSIX ACL support, spec.md §6) -- which is exactly what
    one owner + one group + one shared permissions level can express, and
    no more, so the schema doesn't let you write anything that can't
    actually be enforced. `raw` with neither `group` nor `owner`
    (including None) normalizes to `{}`.

    `permissions_explicit` records whether the config actually wrote a
    `permissions` value here, before it gets defaulted below --
    access_mode() needs that distinction (an operator who wrote one out
    gets it enforced exactly, other-bits included; a default is free to
    also carry the public-execute safety net that keeps a distinct grant
    nested underneath this node still reachable, see access_mode())."""
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
    entry["permissions_explicit"] = "permissions" in entry
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
    `other` never gets read or write -- nothing here is ever meant to be
    world-readable -- but every node on the path from `stortree_root`
    down to any real grant has to stay *traversable* by everyone, or a
    grant several levels down (e.g. a `user-subdirs` descendant's own
    `access.group`) is unreachable no matter how permissive it is itself:
    `open()`/`chdir()` need execute on every ancestor directory, and the
    connecting user is almost never a member of the local `stortree`
    group that owns an ungranted ancestor. So `other` carries a bare
    execute bit (traversal only, no listing, no reading) everywhere this
    function doesn't know the config author explicitly opted out of it.

    With neither `owner` nor `group` granted, this is the plain default
    (owner: stortree, full control; group: stortree, read+traverse;
    other: execute-only) every path had before `access` existed, now with
    that execute bit added. Granting just one of the two still gives the
    *other* slot its own sensible default rather than leaving it at 0: an
    explicit `owner` grant still lets stortree itself administer the path
    (owner bits stay full even though the path's actual Unix owner is the
    granted user, not stortree); an explicit `group` grant makes the path
    private to that group with no separate stortree-group carve-out,
    since it was deliberately scoped to someone else.

    When `access` carries an *explicit* `permissions` (`permissions_
    explicit`, set by `_normalize_access()` before it defaults the field)
    that choice is honored exactly, other-bits included -- an operator
    who wrote out `permissions:` themselves gets it enforced literally,
    even if that happens to make a deeper, differently-scoped descendant
    grant unreachable through this node. Only the *default* permissions
    level (no `permissions` written in config.yml at all) carries the
    public-execute safety net; a hand-built `access` dict with no
    `permissions_explicit` key at all (e.g. in a test) is treated the
    same as an unset default, which is the safer assumption."""
    access = access or {}
    if not (access.get("owner") or access.get("group")):
        return "0751"
    bits = _permission_bits(access.get("permissions", DEFAULT_ACCESS_PERMISSIONS))
    owner_bits = bits if access.get("owner") else 7
    group_bits = bits if access.get("group") else 0
    other_bit = 0 if access.get("permissions_explicit") else 1
    return f"0{owner_bits}{group_bits}{other_bit}"


def _walk_tree(tree):
    """One host-independent walk of the whole tree.

    `tree`'s own top level *is* the map of independent, sibling
    top-level subtrees (docs/config-schema.md "Top-level subtrees") --
    every key at the top of config.yml names a real subdirectory of
    `/srv/stortree` directly, no wrapping `subdirs:` key needed at that
    one level (nested subdirs still need their own `subdirs:`/
    `user-subdirs:` key, same as always). Each top-level entry is shaped
    exactly like any other node (its own `host`,
    `rclone`, nested `subdirs`/`user-subdirs`, and optionally its own
    `client-defaults`/`clients` governing how hosts that don't own it
    peer-mount it). There's no single implicit tree root any more --
    each top-level entry stands on its own, so one being nested doesn't
    make sibling entries dependent on it (this is what keeps e.g. a
    host's own local VFS-cache mount from ever needing another
    top-level subtree's mount up first, unlike when everything hung off
    one shared root).

    Returns (roots, nodes). `roots` is one entry per top-level subtree:
    {path, host, remote, node} (`node` is that subtree's own raw dict --
    resolve() reads its client-defaults/clients straight off it).
    `nodes` is a flat list of every node in the whole forest, top-level
    entries included (unlike the old single root, a top-level entry
    *is* an ordinary mountable node now -- see resolve()) -- each also
    carries `root_path`, the top-level entry it's nested under, so
    resolve() can look up the right root's client-defaults/clients for
    an arbitrarily-deep descendant (e.g. a samba share several levels
    under a top-level subtree). `host` inherits down the tree; `rclone`
    -- both `remote` and `args` -- never inherits (spec.md §1): a node
    with no `rclone.remote` of its own resolves to `remote: None`,
    regardless of what any ancestor sets.

    A node that resolves with `remote: None` isn't a separate mounted
    subtree -- see docs/config-schema.md "Node inheritance" for what that
    means downstream (plan_mounts() below turns it into a plain directory
    to create rather than an rclone mount).
    """
    roots = []
    nodes = []

    def _visit(node, path_parts, host, per_user, root_path):
        h = node.get("host", host)
        r = (node.get("rclone") or {}).get("remote")
        args = (node.get("rclone") or {}).get("args") or {}
        access = _normalize_access(node.get("access"))
        samba = node.get("samba")
        path = "/".join(path_parts)
        nodes.append(
            {
                "path": path,
                "host": h,
                "remote": r,
                "args": args,
                "access": access,
                "samba": samba,
                "per_user": per_user,
                "root_path": root_path,
            }
        )
        for name, child in (node.get("subdirs") or {}).items():
            _visit(child or {}, path_parts + [name], h, per_user, root_path)
        for name, child in (node.get("user-subdirs") or {}).items():
            _visit(
                child or {},
                path_parts + [PER_USER_PLACEHOLDER, name],
                h,
                True,
                root_path,
            )

    for name, root_node in tree.items():
        root_node = root_node or {}
        roots.append(
            {
                "path": name,
                "host": root_node.get("host"),
                "remote": (root_node.get("rclone") or {}).get("remote"),
                "node": root_node,
            }
        )
        _visit(root_node, [name], None, False, name)

    return roots, nodes


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


_UNSET = object()


def _rclone_setting(container):
    """The raw `rclone` value inside a client-defaults/clients-style
    block (`container` -- e.g. a top-level subtree's own
    `client-defaults`, or one entry of its `clients` map): `False`
    (mount disabled), a `{args?}` dict (mount enabled, optionally with
    these rclone args), or `_UNSET` if `container` doesn't set `rclone`
    at all. Dot-expansion already turns the `rclone.args: {...}`
    shorthand into `{rclone: {args: {...}}}`, so a disabling
    `rclone: false` and an args-bearing `rclone: {args: {...}}` differ
    only by type, both read the same way here."""
    if not container or "rclone" not in container:
        return _UNSET
    return container["rclone"]


def _rclone_args(setting):
    return dict(setting.get("args") or {}) if isinstance(setting, dict) else {}


def _root_client_policy(root_node, hostname):
    """(enabled, args) describing how `hostname` -- when it doesn't own
    this top-level subtree -- peer-mounts it, from the subtree's own
    `client-defaults`/`clients.<hostname>` (docs/config-schema.md
    "Per-client mount opt-out"). An explicit `clients.<hostname>.rclone`
    always wins over `client-defaults.rclone`: an allow-list when
    defaults are disabled (only explicitly-truthy clients mount it), a
    deny-list otherwise (every non-owning host mounts it except the ones
    explicitly disabled). With neither set anywhere, every non-owning
    host mounts it -- unchanged from before this existed. `args` merges
    client-defaults under the per-client override, same precedence as
    always."""
    defaults_setting = _rclone_setting(root_node.get("client-defaults"))
    client_setting = _rclone_setting((root_node.get("clients") or {}).get(hostname))
    effective = client_setting if client_setting is not _UNSET else defaults_setting
    enabled = effective is not False
    args = _deep_merge(_rclone_args(defaults_setting), _rclone_args(client_setting))
    return enabled, args


def resolve(tree, hostname, all_hosts):
    """Resolve everything host `hostname` must do, given the parsed
    contents of config.yml (`tree`) and the full inventory host list
    (`all_hosts`, so a host with no mention in config.yml still resolves
    as a full participant -- config-schema.md "Every inventory host
    participates")."""
    tree = _expand_dotted(tree)
    roots, nodes = _walk_tree(tree)
    roots_by_path = {r["path"]: r for r in roots}

    server_subtrees = [n for n in nodes if n["host"] == hostname]

    samba_nodes = _samba_nodes(nodes)
    samba_shares = []
    peer_dependencies = []

    for s in samba_nodes:
        descendants = _descendants_of(s, nodes)
        root_node = (roots_by_path.get(s["root_path"]) or {}).get("node") or {}

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
                enabled, args = _root_client_policy(root_node, hostname)
                if not enabled:
                    continue
                peer_dependencies.append(
                    {
                        "owning_host": d["host"],
                        "local_path": d["path"],
                        "remote_path": d["path"],
                        "samba_node": s["path"],
                        "per_user": d["per_user"],
                        "access": d["access"],
                        "args": args,
                    }
                )

    # Client mount of each top-level subtree this host doesn't own: a
    # non-owning host reaches it by peer-sftp'ing the host that actually
    # owns it, rather than holding direct credentials to its own
    # `rclone.remote` -- the same peer-sourcing rule applied above to
    # every samba descendant a host doesn't own, just generalized to
    # every top-level subtree (mesh, not funneled through one shared
    # root -- see _walk_tree()). A subtree with no rclone.remote of its
    # own has nothing to peer for -- the client still gets its local
    # directory created by stortree_mounts, just no mount at all, same
    # as before per-subtree peer-sourcing existed. `client-defaults`/
    # `clients.<hostname>.rclone` (docs/config-schema.md "Per-client
    # mount opt-out") can suppress this entirely for a subtree that has
    # no business being visible outside its own owning host.
    client_mounts = []
    for root in roots:
        if hostname == root["host"]:
            continue
        enabled, args = _root_client_policy(root["node"], hostname)
        if not enabled:
            continue
        client_remote = None
        if root["remote"]:
            peer_dependencies.append(
                {
                    "owning_host": root["host"],
                    "local_path": root["path"],
                    "remote_path": root["path"],
                    "samba_node": None,
                    "per_user": False,
                    "access": [],
                    "args": args,
                }
            )
            client_remote = _peer_remote_ref(root["host"], root["path"], root["path"])
        client_mounts.append(
            {"local_path": root["path"], "remote": client_remote, "args": args}
        )

    peer_dependencies = _dedupe(
        peer_dependencies, lambda p: (p["owning_host"], p["local_path"])
    )

    peer_served_by = []
    for other in all_hosts:
        if other == hostname:
            continue
        for root in roots:
            if hostname == root["host"] and root["remote"]:
                enabled, _args = _root_client_policy(root["node"], other)
                if enabled:
                    peer_served_by.append(
                        {
                            "serving_host": other,
                            "local_path": root["path"],
                            "samba_node": None,
                            "per_user": False,
                        }
                    )
        for s in samba_nodes:
            root_node = (roots_by_path.get(s["root_path"]) or {}).get("node") or {}
            for d in _descendants_of(s, nodes):
                if d["host"] == hostname and _has_own_content(d, nodes):
                    enabled, _args = _root_client_policy(root_node, other)
                    if enabled:
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
            # Exactly one section, at the same real path plan_mounts()
            # resolves this node to -- an owner grant's own path, or a
            # group-only grant's one shared mount (per_user_mount_path()),
            # never one per member; see plan_mounts()'s matching collapse
            # and per_user_mount_path()'s own docstring for why one real
            # mount now backs every member instead of one full duplicate
            # each. No section at all if nobody's actually granted access.
            access = peer.get("access") or {}
            if access_grant_usernames(access, group_members):
                real_path = per_user_mount_path(peer["local_path"], access)
                expanded = [(real_path, real_path)]
            else:
                expanded = []
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
    top-level segment literally named "root"). Also exposed directly as
    the `stortree_slug` filter: a per-user bind-mount unit's own template
    needs to compute its `symlink_target`'s unit slug from that raw path
    string alone (there's no separate plan_mounts() entry lookup handy at
    render time), the same way plan_mounts() computes every entry's own
    `slug` field here."""
    if not path:
        return "root"
    return "-".join(_escape_slug_segment(seg) for seg in path.split("/"))


def merged_getent_results(loop_results, database):
    """Reconstruct the `{name: fields}` shape `group_members_from_getent()`/
    `group_gids_from_getent()`/`user_uids_from_getent()` all expect, from a
    *looped* `ansible.builtin.getent` task's own `register`-d `.results`
    list, rather than from `ansible_facts.getent_<database>` directly.

    Looping the module (one invocation per needed name, `key: "{{ item
    }}"`) rather than passing every name in one call means each
    iteration's own returned `ansible_facts.getent_<database>` only ever
    contains *that one* name -- and Ansible's default fact-merge
    behaviour for a module's `ansible_facts` is a plain replace, not a
    recursive merge (`hash_behaviour` defaults to `replace`, and nothing
    in this project's `ansible.cfg` overrides it), so each iteration's
    result *replaces* the host's whole `getent_<database>` fact rather
    than adding to it. With more than one name ever needed at once, only
    the *last* loop iteration's single entry survives by the time a
    later task reads `ansible_facts.getent_<database>` -- confirmed
    against a live deployment (4 real per-user container owners
    resolved, only the alphabetically-last one's UID actually ended up
    in `stortree_user_uids`, the rest raising `stortree_user_uids[owner]`
    as a missing key). Rebuilding the merged dict here, from each loop
    iteration's own raw result instead of the clobbered shared fact,
    sidesteps the whole problem without needing `ansible.cfg` changed
    fleet-wide (which would affect every other fact-merge in the
    playbook, not just this one)."""
    merged = {}
    for result in loop_results:
        merged.update((result.get("ansible_facts") or {}).get(f"getent_{database}") or {})
    return merged


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


def per_user_mount_path(path, access):
    """Where a `user-subdirs` descendant's %U-templated `path` is actually
    mounted/created, as distinct from where each authorized user's own
    folder ends up (access_grant_usernames() above) -- the two only
    differ for a `group`-only grant. An `owner` grant (with or without
    `group` alongside it) still pins one real folder to that one user
    directly, `%U` -> the owner's own name, same as ever: one user, one
    folder, one mount, nothing to share.

    A `group`-only grant instead resolves `%U` to `SHARED_MOUNT_SEGMENT`
    -- one real mount, shared by every member, rather than one full
    duplicate per member. rclone's FUSE mount can't tell two
    `--allow-other` mounts of the identical remote path apart from two
    independent processes: N members used to mean N redundant rclone
    procs and N redundant VFS caches of the exact same bytes (no cache
    coherency between them either -- one member's write wouldn't show up
    in another's cache until its own dir-cache-time/vfs-cache-* expired),
    even though the access grant backing all of them was always the same
    single group the whole time (access_mode()/access_group() compute
    identically for every one of those duplicate entries -- nothing about
    *which* member's copy it was ever varied the enforcement). One real
    mount, gid-owned exactly as before, does the same job.
    `plan_mounts()` fans that one real mount back out to each member's
    own folder with a bind mount instead of a second rclone mount -- see
    its per-user expansion for both server_subtrees and
    peer_dependencies, and filter_rclone_conf()'s matching peer-section
    collapse. A real symlink can't do this fan-out job when the member's
    own folder lives on a remote-backed node itself (spec.md §6) -- a
    bind mount is a kernel VFS relationship, not a directory entry the
    remote backend has to
    represent, so it works regardless of whether that backend can store
    symlinks at all."""
    segment = (access or {}).get("owner") or SHARED_MOUNT_SEGMENT
    return path.replace(PER_USER_PLACEHOLDER, segment)


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


def needed_users(resolved, group_members=None):
    """Every username this host's resolved facts reference in an
    `access.owner` grant -- mirrors needed_groups() above, for the
    `getent passwd` lookup user_uids_from_getent() needs to uid-own a
    remote-backed node's mount (spec.md §6). Also covers every per-user
    container's own owner (`_resolved_user_containers()`, group-derived
    ones included) when `group_members` is given -- stortree_secrets
    needs those numeric UIDs too, for a wrapper mount's `--uid`
    (user_container_paths(), stortree_mounts) -- omit `group_members`
    (its default, `None`) to get the plain owner-grant-only set, since
    that's resolvable before group membership itself is (this function's
    own first use in stortree_secrets, ahead of the `getent group`
    lookup that produces `group_members` in the first place)."""
    users = set()
    for n in resolved.get("server_subtrees", []):
        u = (n.get("access") or {}).get("owner")
        if u:
            users.add(u)
    for p in resolved.get("peer_dependencies", []):
        u = (p.get("access") or {}).get("owner")
        if u:
            users.add(u)
    if group_members is not None:
        users.update(_resolved_user_containers(resolved, group_members).values())
    return sorted(users)


def _resolved_user_containers(resolved, group_members):
    """{local_path: owner} for every per-user container a user-subdirs
    node's resolved access grants imply -- the mount-plan-independent
    core needed_users() (resolvable before stortree_mounts_plan exists)
    and user_container_paths() (which adds wrapper-mount details on top,
    once it does) both build on.

    `node_path` still carries `PER_USER_PLACEHOLDER` (`%U`) at this
    point (server_subtrees nodes always do; a peer_dependencies entry's
    `local_path` does too, pre-expansion -- see plan_mounts()'s own note
    on this) -- the container path is everything before it, one call to
    access_grant_usernames() away from knowing exactly which real
    usernames it needs to exist for. Every descendant nested under the
    same `user-subdirs` prefix that resolves to the same user contributes
    the identical container -- deduped here (by `local_path`) rather than
    left to the caller, since e.g. two sibling descendants both resolving
    under `home/%U` would otherwise both try to claim `home/jd`
    independently. Covers both `server_subtrees` (this host's own nodes)
    and `peer_dependencies` (a peer-sourced per-user descendant) -- the
    same two scopes needed_groups()/needed_users() already cover, for the
    same reason: a client-only host with no server_subtrees of its own
    still has to own its peer-sourced per-user containers correctly."""
    containers = {}
    for n in resolved.get("server_subtrees", []) + resolved.get("peer_dependencies", []):
        if not n.get("per_user"):
            continue
        node_path = n.get("path") or n.get("local_path")
        access = n.get("access")
        if not node_path or not access or PER_USER_PLACEHOLDER not in node_path:
            continue
        prefix = node_path.split(PER_USER_PLACEHOLDER)[0].rstrip("/")
        for user in access_grant_usernames(access, group_members):
            local_path = f"{prefix}/{user}" if prefix else user
            containers[local_path] = user
    return containers


def _nearest_mount_slug(local_path, mount_entries):
    """The `slug` of whichever entry in `mount_entries` (each with a
    truthy `remote`) is the nearest real mount `local_path` nests under
    -- its own `local_path` is the longest proper-prefix ancestor of
    `local_path`, if any exist at all. Shared by plan_mounts()'s own
    per-entry `requires_slug` (a mount or per-user bind-mount nested
    under another real mount) and user_container_paths()'s wrapper-mount
    ordering (a staging directory nested the exact same way) -- both are
    "what real mount does this path have to wait for" restated for a
    different kind of path."""
    best = None
    for other in mount_entries:
        op = other["local_path"]
        is_ancestor = op == "" or local_path.startswith(op + "/")
        if is_ancestor and (best is None or len(op) > len(best["local_path"])):
            best = other
    return best["slug"] if best else None


def user_container_paths(resolved, group_members=None, mount_plan=None):
    """Every per-user container directory a `user-subdirs` node implies
    -- the immediate `<prefix>/<username>` folder (docs/config-schema.md
    "subdirs vs user-subdirs": "the immediate children of a user-subdirs
    node are per-user folders") that every one of its descendants'
    resolved users needs to already exist -- paired with the one specific
    user it should be privately owned by, closing the gap
    access_mode()'s public-execute bit only papers over: that bit makes
    the container *traversable* by anyone (needed so an unrelated
    descendant grant nested underneath stays reachable at all), not
    *owned* by the one person it's actually for. A real per-user
    container -- one you can also drop a file straight into, like an
    ordinary home directory -- has to be owned by that person outright.

    A container that's a plain local path (no remote-backed ancestor at
    all) gets that ownership the simple way: stortree_mounts just chowns
    it directly, real native ownership, no more machinery needed. One
    nested inside a remote-backed ancestor's own rclone mount can't be
    chowned that way at all -- that ancestor's mount presents one single,
    uniform --uid/--gid for every path underneath it, and a plain
    chown()/chmod() through the FUSE layer has nowhere real to persist a
    *different* value for just this one path (confirmed against a live
    deployment: Ansible reported the chown as `changed`, but the FUSE
    layer just re-reported the mount's own fixed owner on the next
    `stat`). The three extra fields below are for that case: `staging_
    path` (`STORTREE_USER_PREFIX + owner`, a sibling of the container
    itself, ordinary content inside the *same* remote-backed ancestor,
    touched by nothing but the `stortree` account driving the wrapper
    mount), `slug` (the wrapper mount's own systemd unit name, from the
    *container's* path -- not the staging path's -- so it reads the same
    way every other mount's own slug does), and `requires_slug`
    (`_nearest_mount_slug()` against `mount_plan`'s own real mounts,
    naming whichever one the staging path nests under, `None` if it
    doesn't nest under any real mount at all -- i.e. a plain local
    container, which needs no wrapper mount in the first place; see
    stortree_mounts' own split on this field for which of the two
    ownership mechanisms a given container actually gets)."""
    group_members = group_members or {}
    mount_entries = [e for e in (mount_plan or []) if e.get("remote")]
    containers = _resolved_user_containers(resolved, group_members)
    result = []
    for local_path, owner in sorted(containers.items()):
        parent = local_path.rsplit("/", 1)[0] if "/" in local_path else ""
        prefixed = f"{STORTREE_USER_PREFIX}{owner}"
        staging_path = f"{parent}/{prefixed}" if parent else prefixed
        result.append(
            {
                "local_path": local_path,
                "owner": owner,
                "staging_path": staging_path,
                "slug": _slug(local_path),
                "requires_slug": _nearest_mount_slug(staging_path, mount_entries),
            }
        )
    return result


def plan_mounts(resolved, group_members=None):
    """Flatten this host's resolved server_subtrees/client_mounts/
    peer_dependencies into one flat plan of every local path that has to
    exist (spec.md §2), resolving a user-subdirs entry's %U-templated
    path against its own `access` grant (interpretation call #2) using
    `group_members` (e.g. `ansible_facts.getent_group |
    stortree_group_members`): an `owner` grant (with or without `group`
    alongside it) still gets one real mount at that one user's own path,
    same as ever; a `group`-only grant instead gets exactly one real
    mount, at `per_user_mount_path()`'s shared location, plus one
    bind-mount entry per member fanning that single mount back out to
    each member's own folder -- see per_user_mount_path() for why one
    mount now serves every member instead of one full duplicate each.

    Not every entry is an rclone mount: a server_subtrees entry with
    `remote: None` (a node with no `rclone.remote` of its own -- it never
    inherits one, see _walk_tree()/docs/config-schema.md "Node
    inheritance") is a plain directory that has to exist, not a mount;
    a per-user bind-mount entry (`symlink_target` set) is neither an
    rclone mount nor a plain directory left alone, just a kernel bind
    mount back onto the one real mount its node resolved to -- callers
    should render an rclone unit only for entries with a truthy `remote`,
    e.g. `stortree_mounts_plan | selectattr('remote')`, and a bind-mount
    unit only for entries with a truthy `symlink_target` (the field name
    predates the switch from a real symlink to a bind mount -- see this
    field's own note below for why a symlink doesn't work here -- kept
    as-is rather than renamed everywhere a per-user fan-out is read).
    A client_mounts entry always has a remote (the root `rclone.remote`)
    and is never per-user.

    Every peer_dependencies entry becomes a mount too -- a samba
    descendant this host doesn't own is data this host's local tree still
    has to contain (spec.md §1 "Samba sharing is universal"), sourced
    directly from its actual owning host exactly like the root client
    mount is (mesh, not funneled through root_host -- each peer_dependency
    already names its own real owning host). The root-level entry
    (`local_path == ""`) is skipped here since it's already the
    client_mounts entry above; every other entry gets its own mount,
    per-user-resolved the same way as a per-user server_subtrees entry
    (including the shared-mount-plus-bind-mounts case -- the owning
    host's own plan_mounts() run collapses its `group`-only node to that
    exact same shared path first, so a peer sourcing it has to sftp from
    that real path, not a per-user one nothing lives at), using its own
    `access`/`args` (never the owning host's).

    Each returned entry: {local_path, remote, args, slug, requires_slug,
    symlink_target}. `symlink_target` is the real entry's `local_path`
    for a per-user bind-mount entry, else None -- a real symlink would be
    a directory entry the *target* directory's own backend has to be
    able to represent, which not every remote backend can (an SMB share,
    in production, flatly refused with an I/O error trying to create one
    at all: SMB has no native symlink representation without extensions
    this fleet's Storage Box remote doesn't support); a bind mount is a
    kernel VFS relationship instead, entirely local to this host, so it
    works regardless of what the underlying remote can store. `requires_
    slug` names the nearest ancestor entry that's an actual mount (truthy
    `remote`) whose local_path is the longest proper-prefix ancestor of
    this one, if any -- for systemd RequiresMountsFor= so a nested mount
    (or a per-user bind mount's own mountpoint, which lives at exactly
    this kind of nested path) starts after the mount it nests under,
    skipping over any non-mounted (plain-directory) ancestor in between,
    which has no unit of its own to require (spec.md §2). A per-user
    bind-mount entry's own unit additionally orders after and requires
    `symlink_target`'s mount directly (stortree_mounts renders this),
    since that's the *content* it's fanning out, not just a path it's
    nested under.
    """
    group_members = group_members or {}
    entries = []

    def _expand_per_user(node_path, access, remote_of):
        """Shared by the server_subtrees and peer_dependencies loops
        below: resolves one per-user node's %U-templated `node_path`
        against its own `access` into (real entry, [symlink entries]),
        or (None, []) if nobody's actually granted access to it at all
        (access_grant_usernames() returns no one). `remote_of(path)`
        builds the real entry's `remote` from its resolved real path --
        different for a server_subtrees node (its own literal `remote`,
        unaffected by which path it ends up at) versus a peer_dependency
        (`_peer_remote_ref`, which bakes the resolved path into the
        synthesized sftp reference itself)."""
        users = access_grant_usernames(access, group_members)
        if not users:
            return None, []
        real_path = per_user_mount_path(node_path, access)
        real_entry = {"local_path": real_path, "remote": remote_of(real_path), "access": access}
        if access.get("owner"):
            return real_entry, []
        symlinks = [
            {
                "local_path": node_path.replace(PER_USER_PLACEHOLDER, user),
                "remote": None,
                "args": {},
                "access": {},
                "symlink_target": real_path,
            }
            for user in users
        ]
        return real_entry, symlinks

    for m in resolved.get("client_mounts", []):
        entries.append(
            {
                "local_path": m["local_path"],
                "remote": m["remote"],
                "args": m["args"],
                "access": {},
            }
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
        real_entry, symlinks = _expand_per_user(
            n["path"], n["access"], lambda _p: n["remote"]
        )
        if real_entry is not None:
            real_entry["args"] = n["args"]
            entries.append(real_entry)
        entries.extend(symlinks)

    for p in resolved.get("peer_dependencies", []):
        if p.get("samba_node") is None:
            continue  # a top-level subtree's own peer mount, already the client_mounts entry above
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
        access = p.get("access") or {}
        # local_path and remote_path are always the same string pre-expansion
        # (resolve() sets both from the same node path, see peer_dependencies
        # above) -- per_user_mount_path() only needs to run once.
        real_entry, symlinks = _expand_per_user(
            p["local_path"], access, lambda rp: _peer_remote_ref(p["owning_host"], rp, rp)
        )
        if real_entry is not None:
            real_entry["args"] = p["args"]
            entries.append(real_entry)
        entries.extend(symlinks)

    for e in entries:
        e.setdefault("symlink_target", None)
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
        others = [other for other in mount_entries if other is not e]
        e["requires_slug"] = _nearest_mount_slug(e["local_path"], others)

    # Shallowest paths first (stable sort -- ties keep their original
    # relative order): stortree_mounts creates every path one directory
    # level at a time, in this order, never relying on implicit
    # multi-level recursive creation for a path whose own ancestors don't
    # exist yet -- not every backend's mkdir handles that the way a local
    # filesystem or SFTP does (an SMB share, in production, silently
    # errored trying to create two missing levels -- `home` and the
    # synthetic `.mounts` segment beneath it -- in one implicit step,
    # while creating either one alone, from an already-existing parent,
    # worked fine).
    entries.sort(key=lambda e: e["local_path"].count("/"))

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
    """The full systemd unit filename for every actual mount in a
    stortree_plan_mounts() result -- `stortree-mount@<slug>.service` for
    an rclone mount (truthy `remote`), `stortree-bind@<slug>.service` for
    a per-user bind mount (truthy `symlink_target`, see plan_mounts()'s
    own docstring for why that field's name still says "symlink"). An
    entry with neither is a plain directory, not a mount at all -- see
    plan_mounts(). Used by stortree_mounts to work out which currently-
    installed units (of either kind) are stale."""
    return [f"stortree-mount@{e['slug']}.service" for e in mount_plan if e["remote"]] + [
        f"stortree-bind@{e['slug']}.service" for e in mount_plan if e.get("symlink_target")
    ]


def user_mount_unit_names(containers):
    """The full systemd unit filename for every per-user wrapper mount a
    user_container_paths() result actually needs one for -- only entries
    with `requires_slug` set (a container nested under a real remote
    mount); one with none is a plain local path stortree_mounts chowns
    directly instead, no wrapper unit at all. Mirrors mount_unit_names()
    for the same reason: stortree_mounts needs this to work out which
    currently-installed stortree-user-mount@ units are stale."""
    return [
        f"stortree-user-mount@{c['slug']}.service" for c in containers if c.get("requires_slug")
    ]


def physical_path(local_path, containers):
    """Where a path actually has to be *created on disk*, given that a
    wrapped per-user container (user_container_paths() with `requires_slug`
    set) isn't a real directory at all once its wrapper mount is up -- it's
    a mountpoint, and what's visible underneath it is the wrapper's own
    staging directory, not whatever happens to sit physically at that path.

    Anything created at `<container>/<...>` before the wrapper mounts is
    therefore shadowed the instant it does, and anything mounted onto such
    a path fails outright with the mountpoint simply not existing -- which
    is exactly what happened in production the first apply after wrapper
    mounts existed: every per-user bind mount's own mountpoint directory
    had been created under the container path, the bind unit's new
    `Requires=` pulled the wrapper mount up first, and all eight binds then
    failed their `mount --bind` against a path the wrapper had just hidden.
    Rewriting to the staging path puts that directory where the wrapper
    re-presents it from, so it shows up at the visible container path for
    real and stays mountable.

    Only *strict* descendants are rewritten: the container path itself is
    the wrapper's mountpoint and has to keep existing physically right
    where it is. A path under an unwrapped (plain local, directly chowned)
    container is returned untouched -- there's no wrapper mount shadowing
    anything there."""
    for container in containers or []:
        if not container.get("requires_slug"):
            continue
        prefix = container["local_path"] + "/"
        if local_path.startswith(prefix):
            return container["staging_path"] + "/" + local_path[len(prefix) :]
    return local_path


def path_masked(path, masked_paths):
    """Whether `path` is itself one of `masked_paths` (stortree_mounts'
    own stortree_masked_mount_paths, built from probing each
    remote-backed entry's mountpoint), or nested underneath one of them
    (a real ancestor, not just a same-prefix sibling -- 'a' masks 'a/b'
    but not 'ab'). A path resolved *through* an already-mounted-but-
    unreachable ancestor is exactly as unreachable as that ancestor
    itself, even though only the ancestor's own probe ever actually
    failed -- stortree_mounts uses this to skip every task that would
    otherwise try to touch a path root can't currently see, at any
    depth, not just the one masked entry's own immediate parent (which
    used to be the only case handled, until a masked mount two or more
    levels up from a real entry -- e.g. a peer-sourced samba descendant
    nested under a top-level subtree that's itself still masked from a
    previous run -- showed this needed to walk the whole ancestor chain,
    not just check one level)."""
    return any(path == m or path.startswith(m + "/") for m in masked_paths)


class FilterModule(object):
    def filters(self):
        return {
            "stortree_resolve": resolve,
            "stortree_filter_rclone_conf": filter_rclone_conf,
            "stortree_merge_getent": merged_getent_results,
            "stortree_group_members": group_members_from_getent,
            "stortree_group_gids": group_gids_from_getent,
            "stortree_user_uids": user_uids_from_getent,
            "stortree_access_users": access_grant_usernames,
            "stortree_access_owner": access_owner,
            "stortree_access_group": access_group,
            "stortree_access_mode": access_mode,
            "stortree_needed_groups": needed_groups,
            "stortree_needed_users": needed_users,
            "stortree_user_containers": user_container_paths,
            "stortree_plan_mounts": plan_mounts,
            "stortree_slug": _slug,
            "stortree_mount_unit_names": mount_unit_names,
            "stortree_user_mount_unit_names": user_mount_unit_names,
            "stortree_physical_path": physical_path,
            "stortree_path_masked": path_masked,
            "stortree_samba_access_tokens": samba_access_tokens,
        }
