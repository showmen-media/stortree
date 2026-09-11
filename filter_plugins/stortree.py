"""stortree config resolution.

Pure functions only -- no file/network/Ansible I/O beyond what's passed
in as arguments, so this stays unit-testable with plain pytest (see
tests/test_stortree.py) and reusable outside a running playbook. See
docs/spec.md §1 for the design this implements, and docs/plan.md
"Open interpretation calls" for the handful of judgment calls made where
the spec leaves something implicit.

Everything here is exposed to plays as a Jinja filter via FilterModule at
the bottom (see that mapping for the full list, and
tests/test_filters.py, which checks it against what the roles actually
pipe through). The two entry points the rest build on:

- stortree_resolve(tree, hostname, all_hosts) -- everything a given host
  must do: server subtrees it owns, its own client mount of each
  top-level subtree it doesn't own (peer-sourced from that subtree's
  owning host, not from the subtree's own third-party remote), every
  Samba share in the tree (Samba sharing is universal), and its peer
  dependencies/peer_served_by for cross-host sourcing.
- stortree_filter_rclone_conf(rclone_conf_text, resolved, hostvars) --
  the master rclone.conf INI filtered down to only the sections this
  host's own mount plan references, plus a synthesized sftp section per
  peer mount. Derived from the plan (plan_remote_sections()) rather than
  from `resolved`'s scopes, so "holds credentials for" and "mounts"
  cannot come apart: everything else -- including remotes behind Samba
  shares it exports but doesn't own -- stays off the host entirely
  (spec.md §3).
"""

from __future__ import annotations

import collections
import configparser
import difflib
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

# Fallbacks for the two paths the roles own as overridable variables:
# `stortree_root` (roles/stortree_facts/defaults/main.yml), the mount
# root every resolved path hangs off, and `stortree_etc`
# (roles/stortree_common/defaults/main.yml), the per-host state
# directory. Both reach this module as arguments -- `resolve()`,
# `plan_mounts()` and `filter_rclone_conf()` each take the one(s) they
# need, and the roles pass the live variable -- because both end up
# baked into strings that leave this module for good: an absolute path
# inside a peer mount's own `remote:path` reference, and the `key_file`
# of a synthesized sftp section. Hardcoding them here instead would mean
# an operator who overrode either default got a silently wrong path in
# exactly the artifacts they can't see being generated. These constants
# are only the defaults for a direct call that doesn't pass one (the
# tests, and any use outside a running play); a drift guard in
# tests/test_repo_consistency.py holds them to the role defaults they
# mirror.
DEFAULT_STORTREE_ROOT = "/srv/stortree"
DEFAULT_STORTREE_ETC = "/etc/stortree"

# Convention used by stortree_peer_trust/stortree_secrets: the name of a
# serving host's SSH keypair for peer sftp mounts, inside `stortree_etc`.
PEER_SSH_KEY_NAME = "peer_ssh_key"


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


# Every key the schema defines, by the block it belongs to
# (docs/config-schema.md "Top-level subtrees"). _validate_node() below
# rejects anything else rather than reading the keys it recognizes and
# discarding the rest -- which is what used to happen, and which turns
# an ordinary typo into a silent, wrong-but-plausible resolution: an
# `rclone.remte:` leaves the node a plain directory with no mount, a
# `client_defaults:` quietly re-enables a subtree its author meant to
# keep off every other host (and provisions the SSH trust to go with
# it), a misspelled `subdirs:` drops a whole subtree, and a misspelled
# `access.group` drops a grant and leaves the path world-readable.
# None of those announce themselves at apply time.
_NODE_KEYS = frozenset(
    {
        "host",
        "rclone",
        "access",
        "samba",
        "requires",
        "subdirs",
        "user-subdirs",
        "client-defaults",
        "clients",
    }
)
_RCLONE_KEYS = frozenset({"remote", "args"})
_ACCESS_KEYS = frozenset({"group", "owner", "permissions"})
_SAMBA_KEYS = frozenset({"name"})
# A client-defaults block, or one entry of `clients`, carries `rclone`
# (either `false` or a `{args: ...}` mapping) and/or `access` (the same
# {group?, owner?, permissions?} object a node itself takes, replacing
# the node's own grant for this client's copy -- docs/config-schema.md
# "Client-side access").
_CLIENT_BLOCK_KEYS = frozenset({"rclone", "access"})
_CLIENT_RCLONE_KEYS = frozenset({"args"})


def _reject_unknown_keys(mapping, allowed, block, node_path):
    """Raise on any key of `mapping` the schema doesn't define. A
    non-mapping is left alone: it's either absent, or a shape error that
    the block's own normalizer reports better than this can."""
    if not isinstance(mapping, dict):
        return
    unknown = sorted(str(k) for k in mapping if k not in allowed)
    if not unknown:
        return
    where = f"unknown `{block}` key" if block else "unknown key"
    near = difflib.get_close_matches(unknown[0], sorted(allowed), n=1, cutoff=0.6)
    hint = f" (did you mean {near[0]!r}?)" if near else ""
    raise ValueError(
        f"stortree: {node_path!r} has {where} {unknown[0]!r}{hint} -- expected "
        f"one of: {', '.join(sorted(allowed))}. See docs/config-schema.md"
    )


def _reject_samba_subpath(node, node_path):
    """`samba.subpath` used to be written in config.yml and no longer is
    -- it's derived from the node's own shape (_normalize_samba()).

    Worth its own message rather than falling through to the generic
    unknown-key error, because a config that sets it isn't a typo: it
    was valid, it did what it said, and the fix is to delete the line
    rather than to correct it."""
    samba = node.get("samba")
    if isinstance(samba, dict) and "subpath" in samba:
        raise ValueError(
            f"stortree: {node_path!r} sets `samba.subpath`, which is no longer "
            f"written in config.yml -- it is derived from the node: one with a "
            f"`user-subdirs` key gets the per-user {PER_USER_PLACEHOLDER!r} "
            f"path, one without serves the node itself. Delete the line. See "
            f'docs/config-schema.md "Samba sharing is universal"'
        )


def _require_mapping(value, block, node_path):
    """`subdirs`/`user-subdirs`/`clients` are maps of name -> node. A
    list there (the shape you get from writing them as a YAML sequence)
    otherwise surfaces as a bare AttributeError from inside the walk,
    with nothing naming the node it came from."""
    if value is not None and not isinstance(value, dict):
        raise ValueError(
            f"stortree: {node_path!r}'s `{block}` must be a mapping, got "
            f"{type(value).__name__} -- see docs/config-schema.md"
        )


def _validate_node(node, node_path):
    """Reject anything in this node the schema doesn't define, before
    any of it is read."""
    if not isinstance(node, dict):
        raise ValueError(
            f"stortree: {node_path!r} must be a mapping of node settings, got "
            f"{type(node).__name__} -- see docs/config-schema.md"
        )
    _reject_unknown_keys(node, _NODE_KEYS, "", node_path)
    _reject_unknown_keys(node.get("rclone"), _RCLONE_KEYS, "rclone", node_path)
    _reject_unknown_keys(node.get("access"), _ACCESS_KEYS, "access", node_path)
    _reject_samba_subpath(node, node_path)
    _reject_unknown_keys(node.get("samba"), _SAMBA_KEYS, "samba", node_path)

    for block in ("subdirs", "user-subdirs", "clients"):
        _require_mapping(node.get(block), block, node_path)

    client_blocks = [("client-defaults", node.get("client-defaults"))]
    client_blocks += [
        (f"clients.{name}", entry)
        for name, entry in (node.get("clients") or {}).items()
    ]
    for name, entry in client_blocks:
        _reject_unknown_keys(entry, _CLIENT_BLOCK_KEYS, name, node_path)
        if isinstance(entry, dict):
            _reject_unknown_keys(
                entry.get("rclone"),
                _CLIENT_RCLONE_KEYS,
                f"{name}.rclone",
                node_path,
            )
            # A client block's `access` is the same object a node's own
            # `access` is, held to the same keys -- a typo here drops a
            # client-side grant exactly as silently as one on the node.
            _reject_unknown_keys(
                entry.get("access"),
                _ACCESS_KEYS,
                f"{name}.access",
                node_path,
            )


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
    if not isinstance(raw, dict):
        kind = "a list" if isinstance(raw, list) else f"{raw!r}"
        raise ValueError(
            "access must be a single object ({group?, owner?, permissions?}), "
            f"not {kind} -- see docs/config-schema.md \"Access\""
        )
    entry = dict(raw)
    if not (entry.get("group") or entry.get("owner")):
        return {}
    entry["permissions_explicit"] = "permissions" in entry
    entry.setdefault("permissions", DEFAULT_ACCESS_PERMISSIONS)
    return entry


# A share name is an smb.conf section header and the name a client
# mounts (`//host/<name>`), neither of which can carry a path separator
# -- so a name derived from the node path folds everything outside this
# alphabet to `_` ("tree/home" -> "tree_home"), and an operator-set
# `samba.name` is held to the same alphabet rather than sanitized behind
# their back (docs/config-schema.md "Share names", docs/plan.md "Open
# interpretation calls" #6).
_SHARE_NAME_ILLEGAL = re.compile(r"[^A-Za-z0-9_-]")
# smb.conf sections with a meaning of their own: a share named `global`
# would merge into the [global] block the template emits above the
# shares, silently rewriting fleet-wide settings instead of adding a
# share.
_RESERVED_SHARE_NAMES = frozenset({"global", "homes", "printers"})


def _share_name(raw, node_path):
    """The share's name in smb.conf: `samba.name` where the node sets
    one, otherwise the node path folded into a legal name."""
    if raw is None:
        return _SHARE_NAME_ILLEGAL.sub("_", node_path)
    if not isinstance(raw, str) or not raw:
        raise ValueError(
            f"stortree: {node_path!r}'s `samba.name` must be a non-empty "
            f"string, got {raw!r} -- see docs/config-schema.md \"Share names\""
        )
    if _SHARE_NAME_ILLEGAL.search(raw):
        raise ValueError(
            f"stortree: {node_path!r}'s `samba.name` {raw!r} may contain only "
            f"letters, digits, `-` and `_` -- it's an smb.conf section header "
            f"and the name clients mount. See docs/config-schema.md "
            f"\"Share names\""
        )
    if raw.lower() in _RESERVED_SHARE_NAMES:
        raise ValueError(
            f"stortree: {node_path!r}'s `samba.name` {raw!r} is a reserved "
            f"smb.conf section name ({', '.join(sorted(_RESERVED_SHARE_NAMES))}) "
            f"-- see docs/config-schema.md \"Share names\""
        )
    return raw


def _normalize_samba(node, node_path):
    """Normalize a node's `samba` into either None (not shared) or the
    share's own settings dict, with its resolved share `name` filled in
    (docs/config-schema.md "Samba sharing is universal").

    Presence, not truthiness, is what marks a node for export: `samba:`
    written bare (which YAML parses as None), `samba: {}`, and
    `samba: true` all mean "share this with the default settings", and
    all three used to mean the opposite -- the first two by silently
    resolving to no share at all, the third by crashing resolve() with
    an AttributeError further downstream. Only an explicit
    `samba: false` opts a node back out, which is the one falsy value
    that ever plausibly meant it."""
    if "samba" not in node:
        return None
    raw = node["samba"]
    if raw is False:
        return None
    if raw is None or raw is True:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"stortree: {node_path!r}'s `samba` must be a mapping of share "
            f"settings (or bare/true for the defaults, false to opt out), got "
            f"{raw!r} -- see docs/config-schema.md \"Samba sharing is universal\""
        )
    # A copy: the caller's own config dict is not this function's to
    # write a resolved default back into.
    samba = dict(raw)
    samba["name"] = _share_name(samba.get("name"), node_path)
    # Derived, never written (_reject_samba_subpath()). A node with
    # `user-subdirs` keeps its per-user folders as its own immediate
    # children (docs/config-schema.md "`subdirs` vs `user-subdirs`"), so
    # the share has to land each connecting user in theirs -- which is
    # exactly what Samba's own %U expansion does. A node without them
    # has no per-user level to descend into and serves itself.
    #
    # Not a knob, because only one of the four combinations an operator
    # could write is ever right, and the wrong one is silent: a
    # `user-subdirs` node shared without %U exposes every user's folder
    # to every other user, over SMB, with nothing at apply time saying
    # so. That was reachable two ways -- omitting the key, or misspelling
    # it -- and is now unreachable. The node's shape already carries the
    # answer; asking for it again only creates ways to get it wrong.
    #
    # Presence, not truthiness, exactly as `samba` itself is read above:
    # `user-subdirs:` written bare parses as None and `user-subdirs: {}`
    # is empty, and neither is a statement that this node is *not*
    # per-user -- both say the per-user level exists and currently
    # declares no substructure. Reading them as "not per-user" would
    # make emptying a node's `user-subdirs` silently widen its share
    # from one user's folder to the directory holding everyone's.
    samba["subpath"] = PER_USER_PLACEHOLDER if "user-subdirs" in node else None
    return samba


def _normalize_requires(raw, node_path):
    """Normalize a node's `requires` into a list of tree-relative paths
    (docs/config-schema.md "Requires"): every mount named here has to be
    up before this node's own mount starts.

    This exists for the one dependency the tree's own shape can't imply:
    `requires_slug` (plan_mounts()) is derived purely from path *nesting*,
    which covers a mount inside another mount and nothing else. A mount
    that depends on a sibling top-level subtree -- the real case being a
    `cache-dir` pointing into another subtree's mount -- has no nesting
    relationship at all, so nothing derives it and systemd starts both in
    parallel at boot. Deliberately declared rather than inferred from
    `cache-dir` itself: an operator saying what depends on what is a fact
    about their fleet, not something to reverse-engineer out of an rclone
    argument that happens to contain a path.

    A bare string is accepted as shorthand for a one-element list. Paths
    are tree-relative, exactly as they're written at the top level of
    config.yml (`.bravo-cache`, `tree/backups`), with any leading or
    trailing "/" trimmed -- the same strings `local_path` uses
    everywhere else."""
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list):
        raise ValueError(
            f"stortree: {node_path!r}'s `requires` must be a path or a list of "
            'paths, not a mapping -- see docs/config-schema.md "Requires"'
        )
    paths = []
    for item in items:
        if not isinstance(item, str) or not item.strip("/"):
            raise ValueError(
                f"stortree: {node_path!r}'s `requires` entries must each be a "
                f"non-empty tree-relative path, got {item!r} -- see "
                'docs/config-schema.md "Requires"'
            )
        path = item.strip("/")
        if path not in paths:
            paths.append(path)
    return paths


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

    Returns (roots, nodes, client_chains, children). `roots` is the
    path of each top-level subtree, in config order (each is also an
    ordinary entry in `nodes`). `nodes` is a flat list of
    every node in the whole forest, top-level entries included (unlike
    the old single root, a top-level entry *is* an ordinary mountable
    node now -- see resolve()) -- each also carries `root_path`, the
    top-level entry it's nested under. `host` inherits down the tree;
    `rclone` -- both `remote` and `args` -- never inherits (spec.md §1):
    a node with no `rclone.remote` of its own resolves to
    `remote: None`, regardless of what any ancestor sets.

    `children` is {path: [child path, ...]} -- the tree's own shape,
    kept explicitly rather than re-derived from path prefixes downstream,
    since a `user-subdirs` child sits two path segments below its parent
    (the `%U` placeholder, then its own name) while a `subdirs` child
    sits one, so "how many `/` deeper" doesn't tell them apart.

    `client_chains` is {path: [client block, ...]} -- for every node,
    the `client-defaults`/`clients` blocks found on it and on each of
    its ancestors, ordered shallowest-first, which is what
    _client_policy() below resolves into one host's effective policy for
    that node. A chain rather than a single block because
    `client-defaults`/`clients` apply at any depth, not only on the
    top-level subtree (docs/config-schema.md "Per-client mount
    opt-out"): a nested node's own block refines whatever its ancestors
    set for the same host, and a node that sets none of its own just
    inherits its nearest ancestor's policy unchanged -- which is exactly
    what every node used to get from its top-level subtree, back when
    that was the only level read at all. Only nodes that actually carry
    a block contribute an entry, so the common chain is short (usually
    one, often none).

    A node that resolves with `remote: None` isn't a separate mounted
    subtree -- see docs/config-schema.md "Node inheritance" for what that
    means downstream (plan_mounts() below turns it into a plain directory
    to create rather than an rclone mount).
    """
    roots = []
    nodes = []
    client_chains = {}
    children = {}

    def _visit(node, path_parts, host, per_user, root_path, client_chain, parent_path):
        path = "/".join(path_parts)
        _validate_node(node, path)
        children[path] = []
        if parent_path is not None:
            children[parent_path].append(path)
        if "client-defaults" in node or "clients" in node:
            client_chain = client_chain + [node]
        client_chains[path] = client_chain
        h = node.get("host", host)
        r = (node.get("rclone") or {}).get("remote")
        args = (node.get("rclone") or {}).get("args") or {}
        access = _normalize_access(node.get("access"))
        samba = _normalize_samba(node, path)
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
                "requires": _normalize_requires(node.get("requires"), path),
            }
        )
        for name, child in (node.get("subdirs") or {}).items():
            _visit(
                child or {},
                path_parts + [name],
                h,
                per_user,
                root_path,
                client_chain,
                path,
            )
        for name, child in (node.get("user-subdirs") or {}).items():
            _visit(
                child or {},
                path_parts + [PER_USER_PLACEHOLDER, name],
                h,
                True,
                root_path,
                client_chain,
                path,
            )

    for name, root_node in tree.items():
        _visit(root_node or {}, [name], None, False, name, [], None)
        roots.append(name)

    return roots, nodes, client_chains, children


def _validate_requires(nodes):
    """Check every node's declared `requires` against the whole tree, once,
    host-independently -- a typo'd path is a config error everywhere, not
    just on whichever host happens to resolve it into a real mount, so it
    has to fail the same way on every host rather than going quiet on the
    ones that don't mount the target.

    Three ways it can be wrong: naming a path no node in the tree defines
    (a typo, or a path that was renamed out from under it), naming the
    node itself, or naming a per-user node -- the last because a per-user
    node has no single mount to depend on, it fans out into one mount per
    granted user (or one shared mount plus bind mounts), and which of
    those a dependent would mean is genuinely ambiguous rather than
    something to guess at.

    Naming a real node that simply isn't a mount (no `rclone.remote`
    anywhere, e.g. a plain local cache directory) is *not* an error: it's
    an ordinary directory this same apply creates before any unit starts,
    so there's nothing to order against and plan_mounts() just drops the
    dependency. Same for a mount that exists in the tree but not on this
    particular host."""
    by_path = {n["path"]: n for n in nodes}
    for n in nodes:
        for target in n["requires"]:
            if target == n["path"]:
                raise ValueError(
                    f"stortree: {n['path']!r} lists itself in `requires`"
                )
            if target not in by_path:
                raise ValueError(
                    f"stortree: {n['path']!r} requires {target!r}, which is not "
                    "a path anywhere in the tree -- `requires` takes "
                    "tree-relative paths as written in config.yml, e.g. "
                    '".bravo-cache"'
                )
            if by_path[target]["per_user"]:
                raise ValueError(
                    f"stortree: {n['path']!r} requires {target!r}, which is a "
                    "per-user node -- it resolves to one mount per granted "
                    "user, not a single mount to depend on"
                )

    # Ordering cycles: systemd resolves one by dropping an arbitrary
    # After= and logging it, which turns a config mistake into a silently
    # unpredictable boot order. Fail the apply instead, naming the loop.
    state = {}

    def _walk(path, stack):
        if state.get(path) == "done":
            return
        if state.get(path) == "open":
            loop = stack[stack.index(path):] + [path]
            raise ValueError(
                "stortree: `requires` cycle: " + " -> ".join(repr(p) for p in loop)
            )
        state[path] = "open"
        for target in by_path[path]["requires"]:
            _walk(target, stack + [path])
        state[path] = "done"

    for n in nodes:
        _walk(n["path"], [])


def _samba_nodes(nodes):
    return [n for n in nodes if n["samba"] is not None]


def _validate_share_names(samba_nodes):
    """Two shares can't answer to one name: smb.conf keeps the first
    stanza and drops the second, so half the tree is quietly unreachable
    over SMB. Reachable both by two `samba.name`s written the same and
    by two node paths folding onto one derived name (`a/b c` and
    `a/b_c`), which is why this checks resolved names rather than what
    the config wrote."""
    by_name = {}
    for n in samba_nodes:
        name = n["samba"]["name"]
        clash = by_name.get(name)
        if clash is not None:
            raise ValueError(
                f"stortree: {clash!r} and {n['path']!r} both export the Samba "
                f"share name {name!r} -- set a distinct `samba.name` on one of "
                f"them. See docs/config-schema.md \"Share names\""
            )
        by_name[name] = n["path"]


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


def _client_policy(chain, hostname):
    """(enabled, args, access) describing how `hostname` -- when it
    doesn't own this node -- gets its own copy of it, resolved from
    `chain`: every `client-defaults`/`clients` block on the node and its
    ancestors, shallowest-first (_walk_tree()'s `client_chains`).

    Two axes of precedence, and they compose the same way at every level
    (docs/config-schema.md "Per-client mount opt-out"):

    - Within one node, an explicit `clients.<hostname>` beats that same
      node's `client-defaults`.
    - Across nodes, a nearer (deeper) node beats a more distant ancestor
      -- which is what lets a subdirectory refine, or reverse, whatever
      its top-level subtree set for the same host. A node with no block
      of its own contributes nothing and simply inherits, so a tree that
      only ever writes `client-defaults`/`clients` on its top-level
      subtrees resolves exactly as it did when that was the only level
      read at all.

    `enabled` follows the nearest explicit `rclone` setting found under
    those two rules -- an allow-list where the inherited default is
    disabled (only explicitly-truthy entries mount it), a deny-list
    where it isn't (everyone mounts it but the explicitly disabled).
    "Explicitly truthy" includes an args-bearing `rclone: {args: {...}}`
    with no boolean in sight, at any level: writing client-side mount
    args for a node is taken to mean clients are meant to have it, the
    same reading that makes the one-node allow-list idiom
    (`client-defaults.rclone: false` plus `clients.<h>.rclone.args`)
    work at all.

    `args` merges the other way round -- every level contributes, and
    the ones that win conflicts are the nearest and the most specific:
    client-defaults first, then `clients.<hostname>`, shallowest node to
    deepest.

    `access` does *not* merge: the nearest, most specific block that
    sets one replaces the node's own grant wholesale for this host's
    copy (docs/config-schema.md "Client-side access"), rather than
    layering a partial override over it -- half a grant is not a grant,
    and an `access` that inherited a `group` from one place and
    `permissions` from another would be hard to read off the config at
    all. `_UNSET` when no block in the chain sets one, which is the
    signal to keep the node's own `access` untouched (as against an
    explicit `access:` with nothing in it, which deliberately drops the
    node's grant on this host)."""
    enabled = True
    args = {}
    access = _UNSET
    for node in chain:
        defaults = node.get("client-defaults")
        entry = (node.get("clients") or {}).get(hostname)
        defaults_setting = _rclone_setting(defaults)
        client_setting = _rclone_setting(entry)
        effective = client_setting if client_setting is not _UNSET else defaults_setting
        if effective is not _UNSET:
            enabled = effective is not False
        _deep_merge(args, _rclone_args(defaults_setting))
        _deep_merge(args, _rclone_args(client_setting))
        for container in (defaults, entry):
            if isinstance(container, dict) and "access" in container:
                access = container["access"]
    return enabled, args, access


_TreeIndex = collections.namedtuple(
    "_TreeIndex",
    "roots nodes nodes_by_path children client_chains samba_nodes "
    "samba_descendants requires_by_path",
)


def _index_tree(tree):
    """One host-independent pass over the parsed config.yml: expand the
    dotted-key shorthand, walk the forest (_walk_tree()), run the checks
    that can only be made tree-wide, and index the result into the shape
    the per-host projections below all read from.

    Deliberately before `hostname` is looked at at all. Everything here
    is identical for every host in the fleet, validation included: a bad
    `requires` target or two shares answering to one name is a config
    error everywhere, not only on whichever host happens to resolve the
    node it was written on into a real mount.

    `samba_descendants` maps each `samba:` node's path to the nodes it
    exports (_descendants_of()), computed once here because all three
    samba-facing projections below ask for the same answer."""
    roots, nodes, client_chains, children = _walk_tree(_expand_dotted(tree))
    _validate_requires(nodes)
    samba_nodes = _samba_nodes(nodes)
    _validate_share_names(samba_nodes)
    return _TreeIndex(
        roots=roots,
        nodes=nodes,
        nodes_by_path={n["path"]: n for n in nodes},
        children=children,
        client_chains=client_chains,
        samba_nodes=samba_nodes,
        samba_descendants={s["path"]: _descendants_of(s, nodes) for s in samba_nodes},
        requires_by_path={n["path"]: n["requires"] for n in nodes},
    )


def _client_mount_targets(index, root_path, host):
    """Every node under top-level subtree `root_path` that `host`
    client-mounts in its own right, as (node, args, access) --
    normally just the subtree itself, exactly as when `roots` were
    the only level a client policy could be written at.

    The subtree's own node comes first and, when its policy is
    enabled, it is the only one: one peer-sftp mount of the owning
    host's copy already presents everything nested inside it, so
    nothing below it needs (or could be given) a separate mount of
    its own -- and by the same token a `rclone: false` written on a
    node *underneath* an enabled ancestor can't carve a hole out of
    that ancestor's mount. It only governs the mounts that node gets
    in its own right, which here means none.

    The descent is what a disabled ancestor makes meaningful: with
    the subtree itself opted out for this host, each of its children
    is asked the same question independently, so a single node deep
    in an otherwise host-local subtree can be handed to a client on
    its own (`client-defaults.rclone: false` at the top,
    `clients.<host>.rclone` on just that node -- the allow-list
    idiom, one level down). Two nodes are never descended into:
    one this host already owns (it serves that subtree itself,
    rather than mounting anyone's copy of it -- same rule the
    top-level loop always applied) and a `user-subdirs` node,
    whose path is still `%U`-templated and fans out into one mount
    per granted user rather than the single mount a client_mounts
    entry describes; a per-user node reaches a non-owning host
    through the Samba peer-dependency path instead, which resolves
    that fan-out (plan_mounts()).
    """
    targets = []
    stack = [root_path]
    while stack:
        path = stack.pop(0)
        node = index.nodes_by_path[path]
        if node["host"] == host or node["per_user"]:
            continue
        enabled, args, access = _client_policy(index.client_chains[path], host)
        if enabled:
            targets.append((node, args, access))
            continue
        stack.extend(index.children[path])
    return targets


def _samba_share_entries(index):
    """Every `samba:` node in the tree as one exported share.

    Host-independent on purpose -- it takes no `hostname` and there is
    nothing here to vary by one. Samba sharing is universal (spec.md
    §1): every host exports every share in the tree, so a client reaches
    the same share whichever host it connects to, and the smb.conf
    stanza has to come out identical everywhere."""
    shares = []
    for s in index.samba_nodes:
        descendants = index.samba_descendants[s["path"]]

        # Each descendant carries at most one access grant now (never a
        # list, see _normalize_access()) -- the union across descendants
        # is still a list, just of (at most) one grant per descendant
        # rather than several from any single one.
        access_union = []
        for d in descendants:
            a = d.get("access")
            if a and a not in access_union:
                access_union.append(a)

        shares.append(
            {
                "node_path": s["path"],
                "local_path": s["path"],
                "name": s["samba"]["name"],
                "subpath": s["samba"]["subpath"],
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
    return shares


def _samba_peer_dependencies(index, hostname):
    """Every path under an exported share that `hostname` has to source
    from another host.

    The counterpart to _samba_share_entries() above: the share list is
    the same everywhere, but the *content* behind it isn't -- a
    descendant this host doesn't own is still data its own local tree
    has to contain (spec.md §1), and it comes straight from the host
    that actually owns it rather than from that node's own third-party
    remote (spec.md §3's scoping: a host never holds credentials for a
    remote it doesn't own)."""
    peers = []
    for s in index.samba_nodes:
        for d in index.samba_descendants[s["path"]]:
            if d["host"] == hostname or not _has_own_content(d, index.nodes):
                continue
            # The descendant's own chain, not just its top-level
            # subtree's: a `client-defaults`/`clients` block written
            # on an intermediate node -- or on this descendant
            # itself -- governs this host's copy of exactly this
            # path, without touching its siblings (_client_policy()).
            enabled, args, access = _client_policy(
                index.client_chains[d["path"]], hostname
            )
            if not enabled:
                continue
            peers.append(
                {
                    "owning_host": d["host"],
                    "local_path": d["path"],
                    "remote_path": d["path"],
                    "samba_node": s["path"],
                    "per_user": d["per_user"],
                    # A client-side `access` replaces the node's own
                    # for this host's copy only -- the enforcement
                    # this host actually applies to it (rclone's
                    # --uid/--gid/--dir-perms, and the directory's
                    # own ownership/mode where it isn't a mount).
                    # The share's own `valid users` is deliberately
                    # left alone: it stays the node's tree-wide
                    # grant, identical on every host.
                    "access": (
                        d["access"] if access is _UNSET else _normalize_access(access)
                    ),
                    "args": args,
                    # The node's own declared dependency, not the
                    # owning host's business: a peer mounts the same
                    # path locally and needs the same thing up first.
                    "requires": d["requires"],
                }
            )
    return peers


def _client_mount_entries(index, hostname, samba_sourced_paths, stortree_root):
    """`hostname`'s own client mount of each top-level subtree it doesn't
    own, as (client_mounts, peer_dependencies).

    A non-owning host reaches a subtree by peer-sftp'ing the host that
    actually owns it, rather than holding direct credentials to that
    subtree's own `rclone.remote` -- the same peer-sourcing rule
    _samba_peer_dependencies() applies to every samba descendant a host
    doesn't own, just generalized to every top-level subtree (mesh, not
    funneled through one shared root -- see _walk_tree()). A subtree
    with no rclone.remote of its own has nothing to peer for: the client
    still gets its local directory created by stortree_mounts, just no
    mount at all. `client-defaults`/`clients.<hostname>.rclone`
    (docs/config-schema.md "Per-client mount opt-out") can suppress this
    entirely for a subtree that has no business being visible outside
    its own owning host.

    `samba_sourced_paths` is what _samba_peer_dependencies() already
    claimed, and those paths are skipped here: it's the same mount, of
    the same path, from the same owning host, resolved through the same
    client policy -- and planning it twice is a hard error downstream
    (plan_mounts() sees two entries claiming one systemd unit slug).
    Reachable without any nested client block at all, by a top-level
    subtree that carries `samba:` itself and has no children to delegate
    its content to (_has_own_content()); the descent in
    _client_mount_targets() just widens the ways in. The Samba entry is
    the one to keep: identical args, and it carries the node's own
    `access` grant rather than only what a client block granted."""
    client_mounts = []
    peers = []
    for root_path in index.roots:
        for node, args, access in _client_mount_targets(index, root_path, hostname):
            path = node["path"]
            if path in samba_sourced_paths:
                continue
            # `{}` rather than the node's own `access` when no client
            # block sets one: a client mount has never carried the
            # node's tree-wide grant (it's the owning host that enforces
            # that, and a peer mount of its copy reports whatever that
            # host already applied), and a client policy that says
            # nothing about access shouldn't start.
            access = {} if access is _UNSET else _normalize_access(access)
            client_remote = None
            if node["remote"]:
                peers.append(
                    {
                        "owning_host": node["host"],
                        "local_path": path,
                        "remote_path": path,
                        "samba_node": None,
                        "per_user": False,
                        "access": access,
                        "args": args,
                        "requires": index.requires_by_path.get(path, []),
                    }
                )
                client_remote = _peer_remote_ref(
                    node["host"], path, path, stortree_root
                )
            # A node's `requires` applies wherever it's mounted, its
            # non-owning clients included -- which is the case that
            # motivated the key at all: the cache-dir a *client* points
            # into another subtree's mount belongs to that client's own
            # mount of this subtree, and only the client resolves both
            # ends of it.
            client_mounts.append(
                {
                    "local_path": path,
                    "remote": client_remote,
                    "args": args,
                    # This host's own copy carries whatever the client
                    # policy grants it, and nothing otherwise
                    # (docs/config-schema.md "Client-side access") --
                    # the enforcement this host applies to its own copy:
                    # rclone's --uid/--gid/--dir-perms/--file-perms here,
                    # since a client mount always has a remote.
                    "access": access,
                    "requires": index.requires_by_path.get(path, []),
                }
            )
    return client_mounts, peers


def _peer_served_by_entries(index, hostname, all_hosts, samba_hosts=None):
    """What every *other* host sources from this one -- the mirror of
    _client_mount_entries() and _samba_peer_dependencies(), asked from
    the other side.

    Whatever `other` would client-mount or peer-source from this host is
    what this host has to serve it, resolved through the same
    _client_mount_targets()/_client_policy() this host used for its own
    copy, so a nested opt-out (or opt-in) is honored identically at both
    ends and the sftp trust provisioned by stortree_peer_trust matches
    the mounts that actually get made.

    `samba_hosts` (see resolve()) applies to the samba half only, and to
    `other` rather than to this host: an `other` that exports no shares
    resolves no samba peer dependencies, so serving it would provision
    trust for a mount it will never make. Its own client mounts are
    unaffected -- opting out of *exporting* the tree says nothing about
    wanting it locally."""
    served = []
    for other in all_hosts:
        if other == hostname:
            continue
        other_serves_samba = samba_hosts is None or other in samba_hosts
        for root_path in index.roots:
            for node, _args, _access in _client_mount_targets(index, root_path, other):
                if node["host"] == hostname and node["remote"]:
                    served.append(
                        {
                            "serving_host": other,
                            "local_path": node["path"],
                            "samba_node": None,
                            "per_user": False,
                        }
                    )
        if not other_serves_samba:
            continue
        for s in index.samba_nodes:
            for d in index.samba_descendants[s["path"]]:
                if d["host"] == hostname and _has_own_content(d, index.nodes):
                    enabled, _args, _access = _client_policy(
                        index.client_chains[d["path"]], other
                    )
                    if enabled:
                        served.append(
                            {
                                "serving_host": other,
                                "local_path": d["path"],
                                "samba_node": s["path"],
                                "per_user": d["per_user"],
                            }
                        )
    return _dedupe(served, lambda p: (p["serving_host"], p["local_path"]))


def resolve(
    tree,
    hostname,
    all_hosts,
    stortree_root=DEFAULT_STORTREE_ROOT,
    samba_hosts=None,
):
    """Resolve everything host `hostname` must do, given the parsed
    contents of config.yml (`tree`) and the full inventory host list
    (`all_hosts`, so a host with no mention in config.yml still resolves
    as a full participant -- config-schema.md "Every inventory host
    participates").

    One host-independent index of the tree (_index_tree(), which also
    runs every tree-wide validation), then one small projection per key
    of the returned mapping -- each of which reads that index and says
    what it means on its own, rather than all five falling out of a
    single pass nothing can be read out of in isolation.

    `stortree_root` is the fleet's mount root (the role variable of the
    same name). It's needed here, rather than only where mounts are
    rendered, because a client mount of a subtree this host doesn't own
    resolves to a peer `remote:path` reference with the owning host's
    absolute path already baked into it (_peer_remote_ref()).

    `samba_hosts` is the subset of the fleet that exports Samba shares
    (`stortree_samba_hosts`, roles/stortree_facts/defaults/main.yml).
    `None` means the universal default -- every host, which is what
    "Samba sharing is universal" (config-schema.md) has always meant and
    stays the behaviour for every caller that doesn't pass it.

    Opting a host out has to be resolved *here*, not just by skipping
    the stortree_samba role, because the share list is only half of what
    universality costs: the other half is `peer_dependencies`, the
    peer-sftp mounts a host makes purely to hold content for shares it
    exports but doesn't own (_samba_peer_dependencies()). Skipping only
    the role would leave those mounts -- an rclone process and a VFS
    cache per peer-sourced path -- running to back shares the host no
    longer exports, which is precisely the cost the opt-out exists to
    avoid. Its own client mounts (_client_mount_entries()) are
    deliberately untouched: those are what the host mounts *for itself*,
    and wanting the tree locally is independent of re-exporting it.

    The same list also filters `peer_served_by`, from the other side: an
    opted-out host makes no samba peer mounts, so the hosts that own
    that content must not provision SSH trust for mounts that will never
    be made. Both ends read one fleet-level list and reach the same
    conclusion without hostvars cross-referencing, which is the
    invariant §1 depends on."""
    index = _index_tree(tree)

    serves_samba = samba_hosts is None or hostname in samba_hosts
    samba_peers = _samba_peer_dependencies(index, hostname) if serves_samba else []
    client_mounts, client_peers = _client_mount_entries(
        index,
        hostname,
        {p["local_path"] for p in samba_peers},
        stortree_root,
    )

    return {
        "server_subtrees": [n for n in index.nodes if n["host"] == hostname],
        "client_mounts": client_mounts,
        "samba_shares": _samba_share_entries(index) if serves_samba else [],
        "peer_dependencies": _dedupe(
            samba_peers + client_peers,
            lambda p: (p["owning_host"], p["local_path"]),
        ),
        "peer_served_by": _peer_served_by_entries(
            index, hostname, all_hosts, samba_hosts
        ),
    }


def _remote_section(remote_spec):
    """The rclone.conf section name a `remote:path` reference names.
    Every caller has already established there is a remote -- an entry
    with none never reaches here (plan_remote_sections())."""
    return remote_spec.split(":", 1)[0]


def _peer_section_name(owning_host, local_path):
    """The rclone.conf section name for one peer sftp mount.

    Deliberately *not* `_slug()`'s injective escaping, even though this
    flattens paths the same lossy way `_slug()` was fixed for (nested
    `a/b` and a literal segment `a-b` both land on `a-b`, and the
    `peer-<host>-<path>` join is ambiguous the same way when a hostname
    contains "-"): an rclone remote name may only hold letters, digits,
    `_`, `-`, `.` and space, so the `\\xHH` escapes `_slug()` uses aren't
    available here, and the readable alternatives all mangle every name
    in the common case. This string is one an operator reads directly out
    of a rendered rclone.conf when a peer mount misbehaves, so it stays
    readable and the residual ambiguity is *detected* instead -- see
    _check_peer_section_clash(), and plan_mounts()' own uniqueness check
    for the same trade made once already on systemd unit slugs."""
    slug = local_path.replace("/", "-").replace("%", "pct")
    return f"peer-{owning_host}-{slug}"


def _check_peer_section_clash(clash, section_name, owning_host, local_path):
    """Raise if `clash` -- whatever already claimed `section_name`, or
    None -- is a peer mount of a *different* owning host.

    Only a cross-host clash is an error. Everything in a synthesized
    section except its `path` -- type, host address, user, key_file,
    shell_type -- is a function of the owning host alone, so two paths on
    the *same* host collapsing onto one section name write identical
    credentials and both mounts still work: each one's real path rides on
    its own `remote:path` reference (_peer_remote_ref()), never on the
    section, whose `path` key rclone's sftp backend ignores outright
    (rclone issue #4307) and which is only there as documentation. Two
    *different* hosts landing on one name is the real failure: the second
    write silently replaces the first's address, and a mount that should
    have gone to one machine quietly sources its data from another. Fail
    the render instead, naming both paths, exactly as plan_mounts() does
    for a systemd slug collision."""
    if clash is None or clash["owning_host"] == owning_host:
        return
    raise ValueError(
        f"stortree: {clash['local_path']!r} on {clash['owning_host']!r} and "
        f"{local_path!r} on {owning_host!r} both resolve to the rclone.conf "
        f"section name {section_name!r}, which would point one of them at the "
        f"wrong host -- rename one of them. See docs/config-schema.md "
        f'"Names and identity"'
    )


def _peer_provenance(peer, remote_path):
    """The two facts a synthesized sftp section needs about the peer
    dependency behind a planned mount: which host it points at, and the
    path on that host to point at. Recorded on the plan entry itself
    (plan_mounts()), so plan_remote_sections() can read what this host
    actually mounts instead of re-deriving it from `resolved` in
    parallel -- see plan_remote_sections() for why that parallel
    derivation was worth removing."""
    return {"owning_host": peer["owning_host"], "remote_path": remote_path}


def _peer_mount_path(remote_path, stortree_root=DEFAULT_STORTREE_ROOT):
    """The absolute path on the owning host that a peer sftp section
    points at. Built in one place because two callers have to agree on it
    exactly: _peer_remote_ref() appends it to the remote reference a
    mount unit actually uses, and filter_rclone_conf() writes the same
    string into that section's own `path` key."""
    return f"{stortree_root}/{remote_path}"


def _peer_remote_ref(
    owning_host, local_path, remote_path, stortree_root=DEFAULT_STORTREE_ROOT
):
    """The full `remote:path` rclone reference for a peer-sftp mount.
    rclone's sftp backend has no working way to bake a root path into
    the remote's own .conf section -- a `path` key there is silently
    ignored and the session lands in the login user's home directory
    instead (rclone issue #4307) -- so the absolute path has to be
    appended to the remote reference itself, matching the same `path`
    filter_rclone_conf() writes into that section for documentation."""
    section = _peer_section_name(owning_host, local_path)
    return f"{section}:{_peer_mount_path(remote_path, stortree_root)}"


def plan_remote_sections(
    resolved, group_members=None, stortree_root=DEFAULT_STORTREE_ROOT
):
    """Every rclone.conf section this host needs, derived from the one
    thing that decides it: the mount plan.

    Returns (master_sections, peer_sections). `master_sections` is the
    set of section names to copy verbatim out of the master rclone.conf;
    `peer_sections` maps each synthesized sftp section's name to the
    {owning_host, local_path, path} filter_rclone_conf() writes it from.

    Derived by walking plan_mounts() rather than by re-reading
    `resolved`'s own scopes, because that is exactly the invariant the
    scoping rule states (spec.md §3): a host holds credentials for the
    remotes it mounts, and nothing else. Reading the plan makes both
    halves of that true by construction -- a remote that gets mounted is
    in the plan and therefore gets its section, and a remote that
    doesn't, doesn't.

    The alternative -- deciding this from `resolved` -- means a second
    derivation of "what does this host mount", in parallel with the one
    plan_mounts() already does, and the two can disagree. They have:
    scanning `samba_shares` for remotes here (those are universal, every
    host exports every share in the tree) shipped every host in the
    fleet the credentials for every remote referenced anywhere under a
    share, including nodes it doesn't own, doesn't peer, and had been
    explicitly opted out of by `client-defaults.rclone: false`. Nothing
    about that was visible from either derivation alone. The %U fan-out
    was duplicated the same way, a second time, with the same failure
    mode available to it -- plan_mounts() and this function had to agree
    on which of a per-user node's paths a section gets named for, or a
    mount would point at a section holding no credentials.

    So: one walk, of the plan, and a scope that grows a `remote` later
    can only reach a host's rclone.conf by first reaching its mounts."""
    master_sections = set()
    peer_sections = {}
    for entry in plan_mounts(resolved, group_members, stortree_root):
        if not entry["remote"]:
            continue
        section = _remote_section(entry["remote"])
        peer = entry["peer"]
        if peer is None:
            master_sections.add(section)
            continue
        _check_peer_section_clash(
            peer_sections.get(section),
            section,
            peer["owning_host"],
            entry["local_path"],
        )
        peer_sections[section] = {
            "owning_host": peer["owning_host"],
            "local_path": entry["local_path"],
            "path": _peer_mount_path(peer["remote_path"], stortree_root),
        }
    return master_sections, peer_sections


def filter_rclone_conf(
    rclone_conf_text,
    resolved,
    hostvars=None,
    group_members=None,
    stortree_root=DEFAULT_STORTREE_ROOT,
    stortree_etc=DEFAULT_STORTREE_ETC,
):
    """Filter the master rclone.conf INI down to only the sections this
    host actually mounts with, plus one synthesized sftp section per peer
    mount (spec.md §3).

    What "actually mounts with" means is decided in exactly one place --
    plan_remote_sections(), which reads this host's own mount plan -- so
    this function only renders: copy the master's own stanzas for the
    sections named, write an sftp stanza for each peer section.

    `resolved` is this host's stortree_resolve() output and
    `group_members` (e.g. `ansible_facts.getent_group |
    stortree_group_members`) resolves per-user grants, both because the
    mount plan behind the section list needs them. `hostvars` (Ansible's
    own magic var, or any {hostname: {ansible_host: ...}} mapping)
    supplies the address to reach each peer's owning host at; falls back
    to the owning hostname itself if not given.
    `stortree_root`/`stortree_etc` are the role variables of the same
    names -- the mount root a synthesized section's `path` is built from,
    and the state directory holding the peer SSH key it authenticates
    with."""
    hostvars = hostvars or {}
    master_sections, peer_sections = plan_remote_sections(
        resolved, group_members, stortree_root
    )

    master = configparser.ConfigParser()
    master.read_string(rclone_conf_text)

    out = configparser.ConfigParser()
    for section in master.sections():
        if section in master_sections:
            out[section] = dict(master[section])

    for name, peer in peer_sections.items():
        address = (hostvars.get(peer["owning_host"]) or {}).get(
            "ansible_host", peer["owning_host"]
        )
        out[name] = {
            "type": "sftp",
            "host": address,
            "user": "stortree",
            "key_file": f"{stortree_etc}/{PEER_SSH_KEY_NAME}",
            "shell_type": "unix",
            "path": peer["path"],
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
    would. Every resolved path has at least one segment -- a top-level
    subtree's own name (_walk_tree()) -- so there is no empty-path case
    to reserve a name for; back when one shared tree root existed, that
    root's own client mount resolved to `local_path == ""` and mapped to
    a reserved "root" slug, which nothing can produce now. Also exposed
    directly as the `stortree_slug` filter: a per-user bind-mount unit's
    own template needs to compute its `symlink_target`'s unit slug from
    that raw path string alone (there's no separate plan_mounts() entry
    lookup handy at render time), the same way plan_mounts() computes
    every entry's own `slug` field here."""
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
    `access.group` too, same as a per-user one. Covers all three scopes
    a resolved `access` can turn up in: `server_subtrees` (this host's
    own nodes), `peer_dependencies` (a samba descendant sourced from a
    peer) and `client_mounts` (this host's own copy of a subtree it
    doesn't own -- which carries a grant only when a `client-defaults`/
    `clients.<hostname>` block gave it one, docs/config-schema.md
    "Client-side access"). All computed once, together, by
    `stortree_facts` so every later role (`stortree_mounts`,
    `stortree_secrets`) shares one lookup and one consistent
    group_members/group_gids map, rather than each recomputing its own
    scope of it (and risking one missing a scope the others cover)."""
    groups = set()
    for entry in (
        resolved.get("server_subtrees", [])
        + resolved.get("peer_dependencies", [])
        + resolved.get("client_mounts", [])
    ):
        g = (entry.get("access") or {}).get("group")
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
    for entry in (
        resolved.get("server_subtrees", [])
        + resolved.get("peer_dependencies", [])
        + resolved.get("client_mounts", [])
    ):
        u = (entry.get("access") or {}).get("owner")
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


def _expand_per_user(node_path, access, remote_of, requires, group_members):
    """Resolve one per-user node's %U-templated `node_path` against its
    own `access` into (real entry, [bind-mount entries]), or (None, [])
    if nobody's actually granted access to it at all
    (access_grant_usernames() returns no one).

    An `owner` grant (with or without `group` alongside it) gets one real
    mount at that one user's own path. A `group`-only grant instead gets
    exactly one real mount, at per_user_mount_path()'s shared location,
    plus one bind-mount entry per member fanning that single mount back
    out to each member's own folder -- see per_user_mount_path() for why
    one mount now serves every member instead of one full duplicate each.

    `remote_of(path)` builds the real entry's `remote` from its resolved
    real path -- different for a server_subtrees node (its own literal
    `remote`, unaffected by which path it ends up at) versus a
    peer_dependency (`_peer_remote_ref`, which bakes the resolved path
    into the synthesized sftp reference itself)."""
    users = access_grant_usernames(access, group_members)
    if not users:
        return None, []
    real_path = per_user_mount_path(node_path, access)
    real_entry = {
        "local_path": real_path,
        "remote": remote_of(real_path),
        "access": access,
        "requires": requires,
    }
    if access.get("owner"):
        return real_entry, []
    symlinks = [
        {
            "local_path": node_path.replace(PER_USER_PLACEHOLDER, user),
            "remote": None,
            "args": {},
            "access": {},
            "symlink_target": real_path,
            # The node's `requires` belongs to the one real mount
            # above, not to each bind mount fanning it back out --
            # a bind already orders after that mount, which in turn
            # orders after whatever it requires.
            "requires": [],
        }
        for user in users
    ]
    return real_entry, symlinks


def _plan_client_mounts(resolved):
    """This host's own copy of each top-level subtree it doesn't own, one
    entry each. Never per-user, so there's no %U fan-out to resolve here
    the way the two stages below have to. Usually a mount -- but a
    subtree whose node carries no `rclone.remote` has nothing to peer
    for, and resolve() still gives it a client_mounts entry so the local
    directory gets created, which arrives here as `remote: None` and
    plans as a plain directory like any other.

    A client mount's `remote` was already synthesized by resolve()
    (_peer_remote_ref()), so the provenance plan_remote_sections() needs
    -- which host that reference points at, and the path on it -- is
    joined back on from the matching peer_dependencies entry rather than
    rebuilt here from a second reading of the same tree. Keyed on
    local_path alone: a node's `host` is a single value, so a path has
    exactly one owning host and resolve()'s own dedupe leaves at most one
    entry per path."""
    peer_by_path = {p["local_path"]: p for p in resolved.get("peer_dependencies", [])}
    entries = []
    for m in resolved.get("client_mounts", []):
        peer = peer_by_path.get(m["local_path"])
        entries.append(
            {
                "local_path": m["local_path"],
                "remote": m["remote"],
                "args": m["args"],
                "peer": (
                    None if peer is None else _peer_provenance(peer, peer["remote_path"])
                ),
                # Whatever this host's own client policy granted for its
                # copy, `{}` (the plain ungranted default) otherwise.
                "access": m.get("access") or {},
                "requires": m.get("requires") or [],
            }
        )
    return entries


def _plan_server_subtrees(resolved, group_members):
    """Every node this host owns. A node with no `rclone.remote` of its
    own -- it never inherits one, see _walk_tree()/docs/config-schema.md
    "Node inheritance" -- is a plain directory that has to exist rather
    than a mount, and plans as `remote: None`. A per-user node fans out
    through _expand_per_user()."""
    entries = []
    for n in resolved.get("server_subtrees", []):
        if not n.get("per_user"):
            entries.append(
                {
                    "local_path": n["path"],
                    "remote": n["remote"],
                    "args": n["args"],
                    "access": n["access"],
                    "requires": n.get("requires") or [],
                }
            )
            continue
        real_entry, symlinks = _expand_per_user(
            n["path"],
            n["access"],
            lambda _p: n["remote"],
            n.get("requires") or [],
            group_members,
        )
        if real_entry is not None:
            real_entry["args"] = n["args"]
            entries.append(real_entry)
        entries.extend(symlinks)
    return entries


def _plan_peer_dependencies(resolved, group_members, stortree_root):
    """Every samba descendant this host doesn't own, as a mount of its
    own.

    Such a descendant is data this host's local tree still has to contain
    (spec.md §1 "Samba sharing is universal"), sourced directly from its
    actual owning host exactly like a client mount of a whole top-level
    subtree is -- a mesh: each peer_dependency already names its own real
    owning host, rather than everything funnelling through one shared
    root. It uses its own `access`/`args`, never the owning host's.

    An entry with no `samba_node` is skipped: it's a top-level subtree's
    own peer mount, already planned by _plan_client_mounts(). A per-user
    one resolves the same way a per-user server_subtrees node does,
    including the shared-mount-plus-bind-mounts case -- the owning host's
    own plan_mounts() run collapses its `group`-only node to that exact
    same shared path first, so a peer sourcing it has to sftp from that
    real path, not a per-user one nothing lives at."""
    entries = []
    for p in resolved.get("peer_dependencies", []):
        if p.get("samba_node") is None:
            continue
        if not p.get("per_user"):
            entries.append(
                {
                    "local_path": p["local_path"],
                    "remote": _peer_remote_ref(
                        p["owning_host"],
                        p["local_path"],
                        p["remote_path"],
                        stortree_root,
                    ),
                    "peer": _peer_provenance(p, p["remote_path"]),
                    "args": p["args"],
                    "access": p.get("access") or {},
                    "requires": p.get("requires") or [],
                }
            )
            continue
        access = p.get("access") or {}
        # local_path and remote_path are always the same string pre-expansion
        # (resolve() sets both from the same node path) -- per_user_mount_path()
        # only needs to run once.
        real_entry, symlinks = _expand_per_user(
            p["local_path"],
            access,
            lambda rp: _peer_remote_ref(p["owning_host"], rp, rp, stortree_root),
            p.get("requires") or [],
            group_members,
        )
        if real_entry is not None:
            real_entry["args"] = p["args"]
            # The %U-resolved real path is both the section name's input
            # and the path on the owning host -- resolve() sets a peer
            # dependency's local_path and remote_path from the same node
            # path, and the owning host's own plan collapses it to this
            # same shared location (per_user_mount_path()).
            real_entry["peer"] = _peer_provenance(p, real_entry["local_path"])
            entries.append(real_entry)
        entries.extend(symlinks)
    return entries


def _assign_plan_slugs(entries):
    """Give every entry its systemd instance name, and fail the run if
    two mounts want the same one.

    A slug collision means one unit file rendered twice, with whichever
    entry the role's loop reaches last silently winning -- so it's raised
    here rather than discovered as a mount serving the wrong thing.
    _slug() is injective over paths, so two *different* paths can't
    collide; what this catches is the same path planned twice, which is a
    resolve()-level mistake reaching the plan. Only mounts are checked:
    a plain directory has no unit to collide over.

    Also fills in the two fields only some entries set, so everything
    downstream can read them off any entry unconditionally."""
    for e in entries:
        e.setdefault("symlink_target", None)
        e.setdefault("peer", None)
        e["slug"] = _slug(e["local_path"])

    seen_slugs = {}
    for e in entries:
        if not e["remote"]:
            continue
        clash = seen_slugs.get(e["slug"])
        if clash is not None:
            raise ValueError(
                f"stortree: {clash!r} and {e['local_path']!r} both resolve to "
                f"systemd unit slug {e['slug']!r} -- rename one of them"
            )
        seen_slugs[e["slug"]] = e["local_path"]


def _relate_plan_entries(entries):
    """Resolve the three fields that describe how entries stand to each
    other, rather than what any one of them is on its own. Needs the
    whole plan, which is why it runs once here over the finished list
    instead of inside any of the stages that built it.

    `requires_slug` names the nearest ancestor entry that's an actual
    mount (truthy `remote`) whose local_path is the longest proper-prefix
    ancestor of this one, if any -- for systemd RequiresMountsFor= so a
    nested mount (or a per-user bind mount's own mountpoint, which lives
    at exactly this kind of nested path) starts after the mount it nests
    under, skipping over any non-mounted (plain-directory) ancestor in
    between, which has no unit of its own to require (spec.md §2).

    `has_nested_children` is the same relation read backwards: something
    else nests inside this mount. Such a mount needs consistent,
    predictable stortree:stortree ownership regardless of its own
    `access` grant -- every nested mount always runs as User=stortree
    (stortree-mount@.service.j2, unconditionally), so fusermount's own
    same-owner check for a *new* mount only ever succeeds against a
    parent path it can see itself owning. An *ungranted* parent has no
    such guarantee: its reported ownership, once actually mounted, is
    whatever its own remote backend happens to report (a third-party
    storage box's own arbitrary account, or -- for a peer-sftp mount --
    the numeric uid the owning host's `stortree` account happens to have
    been allocated, never guaranteed to match this host's own) -- neither
    is reliably `stortree` from this host's point of view, and fusermount
    refuses ("bad mount point ... Permission denied") the moment it
    isn't. The unit template forces --uid/--gid for exactly the entries
    flagged here, on top of (never instead of) whatever their own
    `access` grant already pins.

    `requires_mounts` is the node's own declared `requires`
    (_normalize_requires(), validated tree-wide by _validate_requires())
    resolved against *this host's* own mounts: each target path that
    really is a mount here becomes a {local_path, slug} pair the unit
    template renders an After= + Requires= + RequiresMountsFor= for.
    Unlike the two above, nothing about it is derived from the tree's
    shape -- it's the escape hatch for a dependency path nesting can't
    express, a sibling top-level subtree's mount being the case it exists
    for. A target that isn't a mount on this host drops out silently --
    it's either a plain local directory (nothing to order against, this
    same apply creates it before any unit starts) or a mount some other
    host owns and this one doesn't peer (a per-client opt-out, most often
    the very cache subtree that only its own host mounts). The raw
    declaration doesn't survive into the entry: everything downstream
    wants the resolved units."""
    mount_entries = [e for e in entries if e["remote"]]

    for e in entries:
        others = [other for other in mount_entries if other is not e]
        e["requires_slug"] = _nearest_mount_slug(e["local_path"], others)

    parent_slugs = {e["requires_slug"] for e in entries if e["requires_slug"]}
    for e in entries:
        e["has_nested_children"] = e["slug"] in parent_slugs

    mounts_by_path = {e["local_path"]: e for e in mount_entries}
    for e in entries:
        e["requires_mounts"] = [
            {"local_path": path, "slug": mounts_by_path[path]["slug"]}
            for path in e.pop("requires", [])
            if path in mounts_by_path
        ]


def plan_mounts(resolved, group_members=None, stortree_root=DEFAULT_STORTREE_ROOT):
    """Flatten this host's resolved client_mounts/server_subtrees/
    peer_dependencies into one flat plan of every local path that has to
    exist (spec.md §2).

    One stage per source scope, in the order their entries appear in the
    result, then two passes over the finished list: _assign_plan_slugs()
    names every entry, _relate_plan_entries() works out how they stand to
    each other. `group_members` (e.g. `ansible_facts.getent_group |
    stortree_group_members`) resolves a user-subdirs entry's %U-templated
    path against its own `access` grant (interpretation call #2) --
    see _expand_per_user().

    Not every entry is an rclone mount. One with `remote: None` is a
    plain directory that has to exist, not a mount; one with
    `symlink_target` set is neither, just a kernel bind mount back onto
    the real mount its node resolved to. Callers should render an rclone
    unit only for entries with a truthy `remote`, e.g.
    `stortree_mounts_plan | selectattr('remote')`, and a bind-mount unit
    only for entries with a truthy `symlink_target` (the field name
    predates the switch from a real symlink to a bind mount -- see below
    for why a symlink doesn't work here -- kept as-is rather than renamed
    everywhere a per-user fan-out is read).

    Each returned entry: {local_path, remote, args, access, peer, slug,
    requires_slug, has_nested_children, requires_mounts, symlink_target}.
    `peer` is {owning_host, remote_path} for an entry whose remote is a
    synthesized peer reference and None otherwise (_peer_provenance());
    the last four are settled by the two passes above, which document
    them. `symlink_target` is the real entry's `local_path` for a
    per-user bind-mount entry, else None -- a real symlink would be a
    directory entry the *target* directory's own backend has to be able
    to represent, which not every remote backend can (an SMB share, in
    production, flatly refused with an I/O error trying to create one at
    all: SMB has no native symlink representation without extensions this
    fleet's Storage Box remote doesn't support); a bind mount is a kernel
    VFS relationship instead, entirely local to this host, so it works
    regardless of what the underlying remote can store."""
    group_members = group_members or {}

    entries = (
        _plan_client_mounts(resolved)
        + _plan_server_subtrees(resolved, group_members)
        + _plan_peer_dependencies(resolved, group_members, stortree_root)
    )

    _assign_plan_slugs(entries)
    _relate_plan_entries(entries)

    # Shallowest paths first (stable sort -- ties keep the order the
    # stages above produced them in): stortree_mounts creates every path
    # one directory level at a time, in this order, never relying on
    # implicit multi-level recursive creation for a path whose own
    # ancestors don't exist yet -- not every backend's mkdir handles that
    # the way a local filesystem or SFTP does (an SMB share, in
    # production, silently errored trying to create two missing levels --
    # `home` and the synthetic `.mounts` segment beneath it -- in one
    # implicit step, while creating either one alone, from an
    # already-existing parent, worked fine).
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
    installed units (of either kind) are stale -- through
    stale_unit_names() below, which is what the role actually calls."""
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
    currently-installed stortree-user-mount@ units are stale, through
    stale_unit_names() below."""
    return [
        f"stortree-user-mount@{c['slug']}.service" for c in containers if c.get("requires_slug")
    ]


def stale_unit_names(installed_paths, mount_plan, containers):
    """Which currently-installed stortree unit *files* no longer belong
    to this host's resolved plan -- the ones stortree_mounts stops,
    disables and removes before it touches any path on disk.

    `installed_paths` is whatever `ansible.builtin.find` turned up under
    /etc/systemd/system (full paths; only the basename is compared). The
    resolved set is mount_unit_names() plus user_mount_unit_names() --
    the same two functions that name every unit the role renders, which
    is why this join lives next to them rather than as a Jinja chain in
    the role: a rename on either side that stops this matching means a
    live unit stopped and deleted on every apply and re-rendered
    immediately after, and nothing else in the suite would notice."""
    resolved = set(mount_unit_names(mount_plan)) | set(
        user_mount_unit_names(containers)
    )
    return [
        name
        for name in (path.rsplit("/", 1)[-1] for path in installed_paths)
        if name not in resolved
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
            "stortree_access_owner": access_owner,
            "stortree_access_group": access_group,
            "stortree_access_mode": access_mode,
            "stortree_needed_groups": needed_groups,
            "stortree_needed_users": needed_users,
            "stortree_user_containers": user_container_paths,
            "stortree_plan_mounts": plan_mounts,
            "stortree_slug": _slug,
            "stortree_stale_units": stale_unit_names,
            "stortree_physical_path": physical_path,
            "stortree_path_masked": path_masked,
            "stortree_samba_access_tokens": samba_access_tokens,
        }
