"""stortree config resolution.

Pure functions only -- no file/network/Ansible I/O beyond what's passed
in as arguments, so this stays unit-testable with plain pytest (see
tests/test_stortree.py) and reusable outside a running playbook. See
docs/spec.md §1 for the design this implements, and docs/plan.md
"Open interpretation calls" for the handful of judgment calls made where
the spec leaves something implicit.

Two Jinja filters are exposed to plays via FilterModule at the bottom:

- stortree_resolve(tree, hostname, all_hosts) -- everything a given host
  must do: server subtrees it owns, its client mount (if any), every
  Samba share in the tree (Samba sharing is universal), and its peer
  dependencies/peer_served_by for cross-host sourcing.
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
# (`access.group: X`, `access.user: X`) never specifies `permissions` in
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
    """Normalize either access form into a list of
    {group|user: name, permissions: str} grants."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    result = []
    for item in raw:
        entry = dict(item)
        entry.setdefault("permissions", DEFAULT_ACCESS_PERMISSIONS)
        result.append(entry)
    return result


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

    client_mounts = []
    if hostname != root_host:
        defaults = ((tree.get("client-defaults") or {}).get("rclone") or {}).get(
            "args"
        ) or {}
        override = (
            ((tree.get("clients") or {}).get(hostname) or {}).get("rclone") or {}
        ).get("args") or {}
        merged_args = _deep_merge(dict(defaults), dict(override))
        client_mounts.append({"remote": root_remote, "path": "", "args": merged_args})

    samba_nodes = _samba_nodes(nodes)
    samba_shares = []
    peer_dependencies = []

    for s in samba_nodes:
        descendants = _descendants_of(s, nodes)

        access_union = []
        for d in descendants:
            for a in d["access"]:
                if a not in access_union:
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
            if d["host"] != hostname:
                peer_dependencies.append(
                    {
                        "owning_host": d["host"],
                        "local_path": d["path"],
                        "remote_path": d["path"],
                        "samba_node": s["path"],
                        "per_user": d["per_user"],
                    }
                )

    peer_dependencies = _dedupe(
        peer_dependencies, lambda p: (p["owning_host"], p["local_path"])
    )

    peer_served_by = []
    for other in all_hosts:
        if other == hostname:
            continue
        for s in samba_nodes:
            for d in _descendants_of(s, nodes):
                if d["host"] == hostname:
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
    slug = local_path.replace("/", "-").replace("%", "pct")
    return f"peer-{owning_host}-{slug}"


def filter_rclone_conf(rclone_conf_text, resolved, hostvars=None):
    """Filter the master rclone.conf INI down to only the sections
    `resolved` (this host's stortree_resolve() output) actually needs,
    plus one synthesized sftp section per peer dependency (spec.md §3).
    `hostvars` (Ansible's own magic var, or any {hostname: {ansible_host:
    ...}} mapping) supplies the address to reach each peer's owning host
    at; falls back to the owning hostname itself if not given.
    """
    hostvars = hostvars or {}

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
        section_name = _peer_section_name(peer["owning_host"], peer["local_path"])
        address = (hostvars.get(peer["owning_host"]) or {}).get(
            "ansible_host", peer["owning_host"]
        )
        out[section_name] = {
            "type": "sftp",
            "host": address,
            "user": "stortree",
            "key_file": PEER_SSH_KEY_PATH,
            "shell_type": "unix",
            "path": f"/srv/stortree/{peer['remote_path']}",
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


def access_grant_usernames(access, group_members=None):
    """Resolve a list of access grants ({user: name} or {group: name,
    ...}) to the deduped, sorted set of usernames it grants -- group
    grants expand via `group_members` (docs/plan.md interpretation call
    #2: group membership is host-local via SSSD, not something this pure
    function can look up itself)."""
    group_members = group_members or {}
    users = set()
    for grant in access or []:
        if "user" in grant:
            users.add(grant["user"])
        elif "group" in grant:
            users.update(group_members.get(grant["group"], []))
    return sorted(users)


def plan_mounts(resolved, group_members=None):
    """Flatten this host's resolved server_subtrees/client_mounts into
    one flat plan of every local path that has to exist (spec.md §2),
    expanding a user-subdirs entry's %U-templated path into one entry per
    user actually granted access to it (interpretation call #2) using
    `group_members` (e.g. `ansible_facts.getent_group |
    stortree_group_members`).

    Not every entry is an rclone mount: a server_subtrees entry with
    `remote: None` (a node with no `rclone.remote` of its own -- it never
    inherits one, see _walk_tree()/docs/config-schema.md "Node
    inheritance") is a plain directory that has to exist, not a mount --
    callers should render an rclone unit only for entries with a
    truthy `remote`, e.g. `stortree_mounts_plan | selectattr('remote')`.
    A client_mounts entry always has a remote (the root `rclone.remote`).

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
            {"local_path": "", "remote": m["remote"], "args": m["args"], "access": []}
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
            "stortree_access_users": access_grant_usernames,
            "stortree_plan_mounts": plan_mounts,
            "stortree_mount_unit_names": mount_unit_names,
        }
