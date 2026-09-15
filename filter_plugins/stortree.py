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
  must do: server subtrees it owns, its own subtree mount of each
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
import hashlib
import io
import ipaddress
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

# Where layer-1 (transport) rclone mounts live: a local directory on the
# host, deliberately *outside* stortree_root and never part of the
# visible tree.
#
# It is local, which is the whole point. Nothing here is ever written to
# a backend, so a remote's own directory structure contains only the
# paths config.yml describes -- no staging siblings, no bookkeeping
# names. An earlier revision staged inside the tree instead, next to
# each node it served, and every one of those names showed up on the
# remote.
#
# Dot-prefixed and mode 0700: the raw, ungoverned view of every backend
# this host mounts is reachable here, so it is hidden from listings and
# closed to everyone but the service account and root.
DEFAULT_STORTREE_REMOTES_ROOT = "/srv/.stortree-remotes"

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
# `peer_defaults:` quietly re-enables a subtree its author meant to
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
        "userdir-groups",
        "peer-defaults",
        "peers",
    }
)
_RCLONE_KEYS = frozenset({"remote", "args"})
_ACCESS_KEYS = frozenset({"group", "owner", "permissions"})
# `permissions` is either one `rwx`-style level for the whole grant, or
# one per Unix class -- the three classes a mode has, named the way
# `access` already names the first two (docs/config-schema.md "Access").
_PERMISSION_CLASSES = frozenset({"owner", "group", "other"})
# The alphabet a level is read with: _permission_bits() looks for `r`,
# `w` and `x`, and `-` is the conventional filler for a bit not granted.
_PERMISSION_LEVEL = re.compile(r"^[rwx-]*$")
_SAMBA_KEYS = frozenset({"name", "hidden"})
# A peer-defaults block, or one entry of `peers`, carries `rclone`
# (either `false` or a `{args: ...}` mapping), `access` (the same
# {group?, owner?, permissions?} object a node itself takes, replacing
# the node's own grant for this peer's copy -- docs/config-schema.md
# "Peer-side access") and/or `samba` (the same share settings a node
# itself takes, replacing the node's own export on this one host --
# docs/config-schema.md "Per-host shares", and the only way to export a
# node on some hosts and not others).
#
# `userdir-groups` is the fourth, and the one that composes differently:
# where the other three describe one host's copy of a node and replace
# what the node itself said, this one *adds* to the node's own list
# rather than replacing it (_peer_userdir_groups(),
# docs/config-schema.md "`userdir-groups`").
_PEER_BLOCK_KEYS = frozenset({"rclone", "access", "samba", "userdir-groups"})
_PEER_RCLONE_KEYS = frozenset({"args"})

# "this block didn't set the key at all", as against setting it to
# something falsy -- `rclone: false` disables a mount, `access:` with
# nothing in it deliberately drops a grant, and both are statements the
# absence of the key is not. Defined up here because _normalize_samba()
# below needs it too, for the same distinction on `samba`.
_UNSET = object()


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


def _reject_samba_subpath(samba, node_path, block="samba"):
    """`samba.subpath` used to be written in config.yml and no longer is
    -- it's derived from the node's own shape (_normalize_samba()).

    Worth its own message rather than falling through to the generic
    unknown-key error, because a config that sets it isn't a typo: it
    was valid, it did what it said, and the fix is to delete the line
    rather than to correct it."""
    if isinstance(samba, dict) and "subpath" in samba:
        raise ValueError(
            f"stortree: {node_path!r} sets `{block}.subpath`, which is no longer "
            f"written in config.yml -- it is derived from the node: one with a "
            f"per-user level (a `user-subdirs` or `userdir-groups` key) gets "
            f"the per-user {PER_USER_PLACEHOLDER!r} path, one without serves the "
            f"node itself. Delete the line. See "
            f'docs/config-schema.md "Samba sharing is universal"'
        )


# `clients:`/`client-defaults:` were the original names for what is now
# `peers:`/`peer-defaults:`. The mount those blocks configure was never a
# client of the subtree's own `rclone.remote` -- it is an sftp hop into
# the owning host's already-mounted copy (_peer_remote_ref(), spec.md
# §1), provisioned by `stortree_peer_trust`, so the old name described a
# client/server relationship the design deliberately doesn't have. It
# also collided with the one thing "client" does mean here: an SMB
# client connecting to a share.
_RENAMED_PEER_KEYS = {
    "clients": "peers",
    "client-defaults": "peer-defaults",
}


def _reject_legacy_peer_keys(node, node_path):
    """`clients:`/`client-defaults:` were renamed to `peers:`/
    `peer-defaults:`.

    Worth its own message rather than the generic unknown-key error for
    the same reason `samba.subpath` is: a config that sets them isn't a
    typo, it's a config written against the old schema, and the fix is a
    rename rather than a correction. The generic error wouldn't even
    suggest it -- difflib doesn't score `clients` close enough to
    `peers` to offer it as a did-you-mean."""
    for old in _RENAMED_PEER_KEYS:
        if old in node:
            new = _RENAMED_PEER_KEYS[old]
            raise ValueError(
                f"stortree: {node_path!r} sets `{old}`, which was renamed to "
                f"`{new}` -- a host that mounts a subtree it doesn't own is a "
                f"peer of the owning host (an sftp hop into that host's own "
                f"copy), never a client of the subtree's `rclone.remote`. "
                f"Rename the key; the block's contents are unchanged. See "
                f'docs/config-schema.md "Peers"'
            )


def _require_mapping(value, block, node_path):
    """`subdirs`/`user-subdirs`/`peers` are maps of name -> node. A
    list there (the shape you get from writing them as a YAML sequence)
    otherwise surfaces as a bare AttributeError from inside the walk,
    with nothing naming the node it came from."""
    if value is not None and not isinstance(value, dict):
        raise ValueError(
            f"stortree: {node_path!r}'s `{block}` must be a mapping, got "
            f"{type(value).__name__} -- see docs/config-schema.md"
        )


def _reject_ungranted_permissions(access, block, node_path):
    """Reject a grant that carries a `permissions` level with no `group`
    or `owner` for it to apply to.

    A level on its own grants nothing to anybody, so _normalize_access()
    reads the whole grant as empty -- which is a statement of its own now
    that `access` inherits: it means the plain ungranted default at this
    path (docs/config-schema.md "Access inheritance"). An operator who
    wrote a level out means the opposite, and the two are
    indistinguishable by the time anything downstream sees `{}`.

    Asked of the *resolved* grant, not of the block as written, because
    under merging those differ: `access.permissions: rx` written under a
    group-granted ancestor is the ordinary way to narrow that group's
    level and is exactly right, while the same line with nothing granted
    above it is the mistake this catches. It also catches the other way
    in: nulling an inherited principal (`access.group: null`) while
    leaving its level behind, which reads as deliberate and grants
    nobody anything."""
    if not isinstance(access, dict) or "permissions" not in access:
        return
    if access.get("group") or access.get("owner"):
        return
    raise ValueError(
        f"stortree: {node_path!r}'s `{block}` leaves a `permissions` level "
        f"with no `group` or `owner` to apply it to -- a level on its own "
        f"grants nothing. Name the principal alongside it, or write "
        f"`access:` with nothing in it to drop the grant here entirely. "
        f"See docs/config-schema.md \"Access inheritance\""
    )


def _validate_permissions(access, block, node_path):
    """Check the `permissions` inside one `access` block: an `rwx`-style
    level, or a mapping of one per Unix class.

    Every level is held to the alphabet it is read with, because
    _permission_bits() looks for `r`, `w` and `x` and silently ignores
    everything else: unchecked, `permissions: rwz` is a grant quietly
    missing a bit rather than the typo it is. It also catches the mode
    string this schema deliberately isn't -- `permissions: 750` is an
    integer by the time YAML is done with it (`0750` an entirely
    different one), and would otherwise fail somewhere down in the mode
    arithmetic with nothing pointing back at the line that caused it."""
    if not isinstance(access, dict) or "permissions" not in access:
        return
    permissions = access["permissions"]
    if isinstance(permissions, dict):
        _reject_unknown_keys(
            permissions, _PERMISSION_CLASSES, f"{block}.permissions", node_path
        )
        levels = [
            (f"{block}.permissions.{klass}", level)
            for klass, level in permissions.items()
        ]
    else:
        levels = [(f"{block}.permissions", permissions)]
    for name, level in levels:
        # `null` is how a merge takes a key back (_merge_access()), not a
        # level to check.
        if level is None:
            continue
        if not isinstance(level, str) or not _PERMISSION_LEVEL.match(level):
            raise ValueError(
                f"stortree: {node_path!r}'s `{name}` must be an rwx-style "
                f"string -- any of `r`, `w`, `x`, with `-` for a bit not "
                f"granted (`rwx`, `r-x`, `rx`) -- got {level!r}. It is not a "
                f"numeric mode: which class each level applies to is said by "
                f"writing one per class (`permissions: {{owner: rwx, group: "
                f"r-x}}`). See docs/config-schema.md \"Access\""
            )


def _validate_node(node, node_path):
    """Reject anything in this node the schema doesn't define, before
    any of it is read."""
    if not isinstance(node, dict):
        raise ValueError(
            f"stortree: {node_path!r} must be a mapping of node settings, got "
            f"{type(node).__name__} -- see docs/config-schema.md"
        )
    _reject_legacy_peer_keys(node, node_path)
    _reject_unknown_keys(node, _NODE_KEYS, "", node_path)
    _reject_unknown_keys(node.get("rclone"), _RCLONE_KEYS, "rclone", node_path)
    _reject_unknown_keys(node.get("access"), _ACCESS_KEYS, "access", node_path)
    _validate_permissions(node.get("access"), "access", node_path)
    _reject_samba_subpath(node.get("samba"), node_path)
    _reject_unknown_keys(node.get("samba"), _SAMBA_KEYS, "samba", node_path)
    _normalize_userdir_groups(node.get("userdir-groups"), node_path)
    # Whether the node declares a per-user level *in its own right*,
    # which is what a peer block below is allowed to add groups to.
    per_user_shaped = "user-subdirs" in node or "userdir-groups" in node

    for block in ("subdirs", "user-subdirs", "peers"):
        _require_mapping(node.get(block), block, node_path)

    peer_blocks = [("peer-defaults", node.get("peer-defaults"))]
    peer_blocks += [
        (f"peers.{name}", entry)
        for name, entry in (node.get("peers") or {}).items()
    ]
    for name, entry in peer_blocks:
        _reject_unknown_keys(entry, _PEER_BLOCK_KEYS, name, node_path)
        if isinstance(entry, dict):
            _reject_unknown_keys(
                entry.get("rclone"),
                _PEER_RCLONE_KEYS,
                f"{name}.rclone",
                node_path,
            )
            # A peer block's `access` is the same object a node's own
            # `access` is, held to the same keys -- a typo here drops a
            # peer-side grant exactly as silently as one on the node.
            _reject_unknown_keys(
                entry.get("access"),
                _ACCESS_KEYS,
                f"{name}.access",
                node_path,
            )
            _validate_permissions(entry.get("access"), f"{name}.access", node_path)
            # Likewise `samba`: a peer block's is the same object a
            # node's own is, held to the same keys and the same
            # derived-`subpath` rule, so a typo here drops a per-host
            # share exactly as silently as one on the node.
            _reject_samba_subpath(entry.get("samba"), node_path, f"{name}.samba")
            _reject_unknown_keys(
                entry.get("samba"),
                _SAMBA_KEYS,
                f"{name}.samba",
                node_path,
            )
            _normalize_userdir_groups(
                entry.get("userdir-groups"),
                node_path,
                f"{name}.userdir-groups",
            )
            # A peer block adds groups to a per-user level the node
            # already has; it cannot bring one into being for one host.
            # The share path is derived from the node's shape and has to
            # be the same everywhere (_normalize_samba()): a node that
            # were per-user on one host and not on another would answer
            # to one share name while serving `<node>/%U` on the host
            # that added the groups and `<node>` -- every user's folder,
            # to every user -- on all the rest.
            if "userdir-groups" in entry and not per_user_shaped:
                raise ValueError(
                    f"stortree: {node_path!r} sets `{name}.userdir-groups` on "
                    f"a node that has neither `user-subdirs` nor a "
                    f"`userdir-groups` of its own -- a peer block adds groups "
                    f"to a per-user level the node already declares, it does "
                    f"not create one for a single host (the share path is "
                    f"derived from the node and is the same on every host). "
                    f"Give the node its own `userdir-groups` -- an empty list "
                    f"is enough -- and add to it here. See "
                    'docs/config-schema.md "`userdir-groups`"'
                )


def _access_mapping(raw):
    """An `access` value as a plain mapping: `None` (a bare `access:`) is
    the empty one, and anything that isn't a mapping is a config error --
    a list most of all, since that was the old multi-grant form and the
    reason there is a check here at all (_normalize_access())."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        kind = "a list" if isinstance(raw, list) else f"{raw!r}"
        raise ValueError(
            "access must be a single object ({group?, owner?, permissions?}), "
            f"not {kind} -- see docs/config-schema.md \"Access\""
        )
    return raw


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
    entry = dict(_access_mapping(raw))
    if entry.get("permissions") is None:
        entry.pop("permissions", None)
    if not (entry.get("group") or entry.get("owner")):
        return {}
    # Only a *scalar* level settles the whole mode, other bits included
    # (access_mode()). A per-class mapping settles exactly the classes it
    # names and leaves the rest at their defaults, so it is not
    # "explicit" in this sense however much of it was written out.
    entry["permissions_explicit"] = "permissions" in entry and not isinstance(
        entry["permissions"], dict
    )
    entry.setdefault("permissions", DEFAULT_ACCESS_PERMISSIONS)
    return entry


def _access_raw(access):
    """A resolved grant back as the config keys it was written from --
    where a merge of another block over it has to start.

    Normalizing adds two things: a defaulted `permissions` and the
    `permissions_explicit` flag recording whether the config really
    wrote one (_normalize_access()). Both come off again before a merge,
    or a level nobody wrote arrives in the merged grant looking like one
    somebody did, and access_mode() drops the traversal bit a deeper
    grant needs. A per-class `permissions` mapping is kept as written,
    since writing one is never a default."""
    access = access or {}
    raw = {
        key: value
        for key, value in access.items()
        if key not in ("permissions", "permissions_explicit", "reset")
    }
    permissions = access.get("permissions")
    if access.get("permissions_explicit") or isinstance(permissions, dict):
        raw["permissions"] = permissions
    return raw


def _merge_access(inherited, own):
    """`own`'s keys over `inherited`'s, one key at a time -- both raw
    (_access_raw()).

    A `null` value removes the inherited key rather than merging an
    empty one over it: with every key merging there would otherwise be
    no way to take one back. A per-class `permissions` mapping merges
    the same way one level further down, so a descendant can restate one
    class and keep the others; a scalar level and a mapping replace each
    other whole, since they say different kinds of thing."""
    merged = dict(inherited)
    for key, value in own.items():
        if value is None:
            merged.pop(key, None)
        elif (
            key == "permissions"
            and isinstance(value, dict)
            and isinstance(merged.get("permissions"), dict)
        ):
            merged["permissions"] = {
                klass: level
                for klass, level in {**merged["permissions"], **value}.items()
                if level is not None
            }
        else:
            merged[key] = value
    return merged


def _dropped_access(previous):
    """What an explicitly empty `access:` resolves to, written against
    the `previous` grant it replaces -- an ancestor's, where `access`
    inherits down the tree (_walk_tree()), or the node's own, where a
    peer block replaces it for one host (_peer_grant()).

    With nothing to replace it is the plain ungranted `{}` this has
    always been. Where it really does drop a grant it is `{reset: True}`
    instead: still granted to nobody, and still `0751` stortree:stortree
    everywhere ownership is read from it (access_owner(), access_group(),
    access_mode() all ignore the key), but no longer indistinguishable
    from a node that simply never had a grant.

    The difference is worth a key because dropping a grant is not free
    on a host that holds a *mounted* copy of the path. There, ownership
    is whatever the presentation above it shows over its whole subtree,
    so a node that has genuinely dropped its ancestor's grant needs a
    presentation of its own to show anything else -- and asking for one
    is exactly what this marker does (_layer_plan_entries()). Without
    it, the drop would be honoured on the owning host, where a real
    chown settles it, and quietly ignored on every host that mounts the
    subtree instead."""
    return {"reset": True} if previous else {}


def _peer_grant(node_access, peer_access, node_path, hostname):
    """The grant a host actually applies to its own copy of a node: the
    node's own (which every host enforces by default, spec.md §6) with
    whatever a `peer-defaults`/`peers.<host>` block wrote for this
    host merged over it, key by key -- the same rule an ancestor and its
    descendant follow down the tree (_merge_access()).

    So a peer block naming an `owner` and nothing else hands that
    host's copy to that user and leaves the node's group in place. It
    used to replace the whole grant, on the reasoning that a peer
    block describes another host's copy and so states it outright; what
    that meant in practice was that overriding one key silently dropped
    the others, and a grant meant to hold fleet-wide stopped at the one
    host that also had a host-local principal to name. The blocks that
    really do mean "and nothing else" say so the same way a node does,
    with an empty `access:` (_dropped_access()) or a `null` on the key
    they are taking back.

    `node_path`/`hostname` name the grant in an error, and are the
    reason the merged result is checked here rather than each block
    where it is written: a block writing only `permissions` is the
    ordinary way to narrow this host's copy of an inherited grant, and
    whether that leaves anyone granted is a question about the merge,
    not about the block."""
    if peer_access is _UNSET:
        return node_access
    own = _access_mapping(peer_access)
    if not own:
        return _dropped_access(node_access)
    merged = _merge_access(_access_raw(node_access), own)
    _reject_ungranted_permissions(merged, f"access for {hostname}", node_path)
    return _normalize_access(merged) or _dropped_access(node_access)


# A share name is an smb.conf section header and the name an SMB
# client mounts (`//host/<name>`), neither of which can carry a path separator
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
            f"and the name SMB clients mount. See docs/config-schema.md "
            f"\"Share names\""
        )
    if raw.lower() in _RESERVED_SHARE_NAMES:
        raise ValueError(
            f"stortree: {node_path!r}'s `samba.name` {raw!r} is a reserved "
            f"smb.conf section name ({', '.join(sorted(_RESERVED_SHARE_NAMES))}) "
            f"-- see docs/config-schema.md \"Share names\""
        )
    return raw


def _normalize_samba(raw, node_path, per_user_parent):
    """Normalize one `samba` value into either None (not shared) or the
    share's own settings dict, with its resolved share `name` filled in
    (docs/config-schema.md "Samba sharing is universal").

    `raw` is the value as written -- `_UNSET` where the block didn't set
    the key at all -- and `per_user_parent` is whether the *node* it
    belongs to declares a per-user level (a `user-subdirs` or a
    `userdir-groups` key), which is what decides the derived `subpath`
    below. Both are passed in rather than read off a
    raw config node, because the value can come from either of two
    places now: the node's own `samba:`, or a `samba:` inside one of its
    `peer-defaults`/`peers.<host>` blocks (_peer_samba()). The
    node's shape decides the subpath identically either way -- a
    per-host share of a per-user node is still per-user.

    Presence, not truthiness, is what marks a node for export: `samba:`
    written bare (which YAML parses as None), `samba: {}`, and
    `samba: true` all mean "share this with the default settings", and
    all three used to mean the opposite -- the first two by silently
    resolving to no share at all, the third by crashing resolve() with
    an AttributeError further downstream. Only an explicit
    `samba: false` opts a node back out, which is the one falsy value
    that ever plausibly meant it."""
    if raw is _UNSET:
        return None
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
    # Keeps the share out of a host's browse list (`browseable = no`,
    # roles/stortree_samba/templates/smb.conf.j2). Not access control --
    # `valid users` is that, and it is untouched by this -- just a way
    # to keep a share nobody browses for by hand (an appliance's backup
    # target, a camera recorder's spool) out of the list a person sees.
    # The share stays mountable by its exact name.
    samba["hidden"] = bool(samba.get("hidden", False))
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
    # from one user's folder to the directory holding everyone's. A
    # `userdir-groups` with no `user-subdirs` beside it is read the same
    # way, and is the case where that substructure is not "not declared
    # yet" but genuinely absent: per-user folders with nothing shared
    # inside them, which is what a home directory is.
    samba["subpath"] = PER_USER_PLACEHOLDER if per_user_parent else None
    return samba


def _normalize_userdir_groups(raw, node_path, block="userdir-groups"):
    """Normalize a `userdir-groups` value into a list of group names --
    the groups whose members get a per-user directory under this node
    (docs/config-schema.md "`userdir-groups`").

    This is the node saying who its per-user level is *for*. Without it
    the membership of a `user-subdirs` node is emergent: whoever happens
    to be named by an `access` grant on something nested beneath it
    (access_grant_usernames()), so a home directory exists only as a
    side effect of a share being granted inside it, and stops existing
    when that share is commented out. Those grants still contribute --
    this adds a source, it does not replace one -- but a node can now
    name its users outright, and a person with a home and nothing shared
    in it is finally something the config can say.

    Groups only, deliberately: an individual gets a directory through
    `access.owner` on something beneath, and a second way to name one
    person would be a second place to look when asking who has a folder
    here. A group is the thing this key exists to resolve, because
    membership lives in LDAP rather than in config.yml -- which is also
    why the list is not checked against anything here: a group with no
    members, or none this host can resolve, yields no directories and is
    not an error (needed_groups(), spec.md §5).

    A bare `userdir-groups:` parses as None and normalizes to the empty
    list. It still declares the node per-user, exactly as a bare
    `user-subdirs:` does (_normalize_samba()): the key's presence is the
    shape, and its contents are who is in it today."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"stortree: {node_path!r}'s `{block}` must be a list of group "
            f"names, got {type(raw).__name__} -- see docs/config-schema.md "
            '"`userdir-groups`"'
        )
    groups = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"stortree: {node_path!r}'s `{block}` entries must each be a "
                f"non-empty group name, got {item!r} -- see "
                'docs/config-schema.md "`userdir-groups`"'
            )
        if item not in groups:
            groups.append(item)
    return groups


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

    When `access` carries an *explicit* scalar `permissions`
    (`permissions_explicit`, set by `_normalize_access()` before it
    defaults the field) that choice is honored exactly, other-bits
    included -- an operator who wrote out `permissions:` themselves gets
    it enforced literally, even if that happens to make a deeper,
    differently-scoped descendant grant unreachable through this node.
    Only the *default* permissions level (no `permissions` written in
    config.yml at all) carries the public-execute safety net; a
    hand-built `access` dict with no `permissions_explicit` key at all
    (e.g. in a test) is treated the same as an unset default, which is
    the safer assumption.

    `permissions` written as a mapping instead names its levels one Unix
    class at a time (`{owner: rwx, group: r-x, other: "---"}`,
    docs/config-schema.md "Access"). Every class it names is enforced
    exactly as written and every class it doesn't keeps the default
    above -- including `other`'s traversal bit, which a mapping
    therefore has to remove deliberately rather than as a side effect of
    saying something about the group. That is the whole difference
    between the two forms: a scalar settles the mode, a mapping settles
    the classes it mentions.

    Nothing here needs a POSIX ACL: one owner and one group at two
    different levels is what a Unix mode has always been able to say.
    The single-object restriction on `access` (_normalize_access()) is
    about naming two *groups*, which a mode cannot express and rclone's
    FUSE mount cannot carry."""
    access = access or {}
    if not (access.get("owner") or access.get("group")):
        return "0751"
    permissions = access.get("permissions", DEFAULT_ACCESS_PERMISSIONS)
    classes = permissions if isinstance(permissions, dict) else {}
    bits = _permission_bits(
        DEFAULT_ACCESS_PERMISSIONS if classes else permissions
    )
    digits = {
        "owner": bits if access.get("owner") else 7,
        "group": bits if access.get("group") else 0,
        "other": 0 if access.get("permissions_explicit") else 1,
    }
    for klass, level in classes.items():
        digits[klass] = _permission_bits(level)
    return "0{owner}{group}{other}".format(**digits)


def bindfs_perms(access):
    """access_mode() restated as the chmod-style spec bindfs's `-p`
    takes, for a presentation mount (docs/spec.md §2 "The presentation
    layer").

    rclone took two separate flags -- `--dir-perms` and `--file-perms`
    -- so a mode and its file-only counterpart could just be handed over
    as they were. bindfs takes one spec for both and resolves the
    difference with chmod's capital `X`, which sets the execute bit only
    where it means "enter" (a directory) rather than "run" (a regular
    file). So the octal here is the *file* mode -- access_mode() with
    every execute bit cleared -- followed by `+X` for exactly the
    classes whose execute bit access_mode() did set, which puts those
    bits back on directories alone.

    Passing access_mode() to `-p` directly would be the obvious thing
    and is wrong: `0751` would mark every regular file in the subtree
    executable.

    The `other` execute bit access_mode() adds for traversal (see its
    own docstring) survives this translation intact, which matters more
    here than it did under rclone: a presentation mount for a node whose
    descendants are owned by *someone else* is the only thing standing
    between those descendants and being unreachable, since the mount
    above them now presents a real owner instead of the uniform
    stortree:stortree a transport mount used to show."""
    mode = access_mode(access)
    file_digits = ""
    classes = ""
    for digit, klass in zip(mode[1:], "ugo"):
        bits = int(digit)
        file_digits += str(bits & ~1)
        if bits & 1:
            classes += klass
    spec = f"0{file_digits}"
    return f"{spec},{classes}+X" if classes else spec


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
    `peer-defaults`/`peers` governing how hosts that don't own it
    peer-mount it). There's no single implicit tree root any more --
    each top-level entry stands on its own, so one being nested doesn't
    make sibling entries dependent on it (this is what keeps e.g. a
    host's own local VFS-cache mount from ever needing another
    top-level subtree's mount up first, unlike when everything hung off
    one shared root).

    Returns (roots, nodes, peer_chains, children, own_peer_blocks). `roots` is the
    path of each top-level subtree, in config order (each is also an
    ordinary entry in `nodes`). `nodes` is a flat list of
    every node in the whole forest, top-level entries included (unlike
    the old single root, a top-level entry *is* an ordinary mountable
    node now -- see resolve()) -- each also carries `root_path`, the
    top-level entry it's nested under. `host` and `access` inherit down
    the tree; `rclone` -- both `remote` and `args` -- never inherits
    (spec.md §1): a node with no `rclone.remote` of its own resolves to
    `remote: None`, regardless of what any ancestor sets.

    `access` inherits key by key (docs/config-schema.md "Access
    inheritance"). A node's own `access` merges *over* whatever it
    inherited rather than replacing it, so each of `group`, `owner` and
    `permissions` comes from the nearest ancestor that set it: a
    descendant narrowing an inherited group's level writes only
    `permissions`, and one handing a subdirectory to a single person
    writes only `owner`. Writing `null` for a key removes what was
    inherited for that key alone, and an empty `access:` drops the whole
    inherited grant -- the two escape hatches merging needs, since with
    everything else merging there would otherwise be no way down.

    A `peer-defaults`/`peers` block's `access` then merges over the
    result the same way, for the one host it describes (_peer_grant(),
    _peer_policy()) -- so the two axes compose rather than one
    cancelling the other, and a block naming a host-local service
    account as `owner` keeps the fleet-wide group the node granted
    instead of dropping it.

    Raw values are what carry down, normalized once per node, so an
    ancestor's *defaulted* `permissions` never reaches a descendant
    looking like one the config wrote out -- `permissions_explicit`
    (_normalize_access()) has to stay true only where somebody really
    did write a level, or access_mode() drops the traversal bit a
    deeper grant needs.

    Inheriting is what makes a grant describe a *subtree* rather than
    one directory. The alternative, and what this used to do, is that
    `access.group: G` on a node left every subdirectory under it back at
    the ungranted default: the group could traverse into the subtree
    (access_mode()'s public-execute bit) and read nothing inside it,
    which is essentially never what the grant meant. It crosses a
    `user-subdirs` boundary like any other, so a granted node's per-user
    children resolve against that grant (_expand_per_user()) instead of
    resolving to nobody and dropping out of the plan.

    `children` is {path: [child path, ...]} -- the tree's own shape,
    kept explicitly rather than re-derived from path prefixes downstream,
    since a `user-subdirs` child sits two path segments below its parent
    (the `%U` placeholder, then its own name) while a `subdirs` child
    sits one, so "how many `/` deeper" doesn't tell them apart.

    `peer_chains` is {path: [peer block, ...]} -- for every node,
    the `peer-defaults`/`peers` blocks found on it and on each of
    its ancestors, ordered shallowest-first, which is what
    _peer_policy() below resolves into one host's effective policy for
    that node. A chain rather than a single block because
    `peer-defaults`/`peers` apply at any depth, not only on the
    top-level subtree (docs/config-schema.md "Per-peer mount
    opt-out"): a nested node's own block refines whatever its ancestors
    set for the same host, and a node that sets none of its own just
    inherits its nearest ancestor's policy unchanged -- which is exactly
    what every node used to get from its top-level subtree, back when
    that was the only level read at all. Only nodes that actually carry
    a block contribute an entry, so the common chain is short (usually
    one, often none).

    `own_peer_blocks` is {path: raw node} for the nodes that carry a
    block of their own -- the chain's last element, but identified
    rather than guessed at. `samba` is read from this and not from the
    chain, because unlike `rclone`/`access` it does not inherit
    (_peer_samba()). Kept beside the resolved nodes rather than on
    them: a node dict is returned from resolve() and serialized into
    Ansible facts, and raw config has no business travelling there.

    A node that resolves with `remote: None` isn't a separate mounted
    subtree -- see docs/config-schema.md "Node inheritance" for what that
    means downstream (plan_mounts() below turns it into a plain directory
    to create rather than an rclone mount).
    """
    roots = []
    nodes = []
    peer_chains = {}
    children = {}
    own_peer_blocks = {}

    def _visit(
        node,
        path_parts,
        host,
        per_user,
        root_path,
        peer_chain,
        parent_path,
        inherited_access,
    ):
        # The inherited grant travels as the raw config keys it was
        # written from, not as a resolved one -- see the note on
        # `permissions_explicit` in the docstring above.
        path = "/".join(path_parts)
        _validate_node(node, path)
        children[path] = []
        if parent_path is not None:
            children[parent_path].append(path)
        if "peer-defaults" in node or "peers" in node:
            peer_chain = peer_chain + [node]
            own_peer_blocks[path] = node
        peer_chains[path] = peer_chain
        h = node.get("host", host)
        r = (node.get("rclone") or {}).get("remote")
        args = (node.get("rclone") or {}).get("args") or {}
        own_access = node.get("access", _UNSET)
        if own_access is _UNSET:
            raw_access = inherited_access
        else:
            own = _access_mapping(own_access)
            raw_access = _merge_access(inherited_access, own) if own else {}
        _reject_ungranted_permissions(raw_access, "access", path)
        access = _normalize_access(raw_access) or _dropped_access(inherited_access)
        userdir_groups = _normalize_userdir_groups(
            node.get("userdir-groups"), path
        )
        # Either key declares the per-user level, so either one settles
        # the derived share subpath. `userdir-groups` without
        # `user-subdirs` is a node whose per-user folders have no shared
        # substructure inside them -- ordinary home directories, which
        # is a config this could not express before
        # (docs/config-schema.md "`userdir-groups`").
        per_user_parent = "user-subdirs" in node or "userdir-groups" in node
        samba = _normalize_samba(node.get("samba", _UNSET), path, per_user_parent)
        nodes.append(
            {
                "path": path,
                "host": h,
                "remote": r,
                "args": args,
                "access": access,
                "samba": samba,
                "per_user": per_user,
                # Whether this node's *own* shape is per-user (it has
                # a `user-subdirs` or `userdir-groups` key), as against
                # `per_user` above, which says it *sits under* one. Kept
                # by name so a peer block's `samba:` can derive the same
                # `%U` subpath later, without the raw config node in
                # hand.
                "per_user_parent": per_user_parent,
                # The groups this node itself names as having a per-user
                # directory under it, before any peer block adds to them
                # (_userdir_parent_entries()). Empty for every node that
                # doesn't write the key, which is every node that
                # resolved its per-user membership from the grants
                # beneath it and still does.
                "userdir_groups": userdir_groups,
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
                peer_chain,
                path,
                raw_access,
            )
        for name, child in (node.get("user-subdirs") or {}).items():
            _visit(
                child or {},
                path_parts + [PER_USER_PLACEHOLDER, name],
                h,
                True,
                root_path,
                peer_chain,
                path,
                raw_access,
            )

    for name, root_node in tree.items():
        _visit(root_node or {}, [name], None, False, name, [], None, {})
        roots.append(name)

    return roots, nodes, peer_chains, children, own_peer_blocks


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


def _validate_share_names(named, hostname=None):
    """Two shares can't answer to one name: smb.conf keeps the first
    stanza and drops the second, so half the tree is quietly unreachable
    over SMB. Reachable both by two `samba.name`s written the same and
    by two node paths folding onto one derived name (`a/b c` and
    `a/b_c`), which is why this checks resolved names -- `named`, an
    iterable of (node path, share name) -- rather than what the config
    wrote.

    Checked twice, at two different scopes, because a collision can now
    arise at either. Two nodes whose *own* `samba:` blocks land on one
    name collide on every host, so _index_tree() catches that tree-wide
    with no `hostname` to name. A per-host share (docs/config-schema.md
    "Per-host shares") only exists on the hosts it was written for, so a
    name it collides with may be free everywhere else -- that one is
    caught by _host_samba_nodes(), which passes the `hostname` whose
    rendered smb.conf could not have represented both."""
    by_name = {}
    for path, name in named:
        clash = by_name.get(name)
        if clash is not None:
            where = f" on {hostname!r}" if hostname else ""
            raise ValueError(
                f"stortree: {clash!r} and {path!r} both export the Samba "
                f"share name {name!r}{where} -- set a distinct `samba.name` on "
                f"one of them. See docs/config-schema.md \"Share names\""
            )
        by_name[name] = path


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


def _transport_covers(inner, outer):
    """Whether `outer`'s transport already presents everything `inner`
    would mount, so `inner` needs no transport of its own.

    True only for two peer mounts of the same owning host where `inner`'s
    path nests inside `outer`'s and its remote roots at exactly the place
    `outer`'s mount reaches that path -- the same bytes over the same
    account, arrived at two ways. A peer reference is stortree's own
    construction (_peer_remote_ref(): an owning host plus a path under
    that host's `stortree_root`), which is what makes the comparison
    exact rather than a guess about what some backend means by a path.

    Equal `args` is the last condition and not a formality. Layer 1 is
    the only layer that caches, so a node whose peer policy asks for a
    different `vfs-cache-mode`, `cache-dir` or `bwlimit` than the mount
    above it is asking for a second mount, and gets one. Only where
    nothing distinguishes the two is there nothing to lose by dropping
    the inner one -- along with the `requires` that ordered it, which
    ordered a mount that no longer exists."""
    inner_peer, outer_peer = inner["peer"], outer["peer"]
    if not inner_peer or not outer_peer:
        return False
    if inner_peer["owning_host"] != outer_peer["owning_host"]:
        return False
    if not inner["local_path"].startswith(outer["local_path"] + "/"):
        return False
    tail = inner["local_path"][len(outer["local_path"]) :]
    if inner_peer["remote_path"] != outer_peer["remote_path"] + tail:
        return False
    return inner["args"] == outer["args"]


def _grant_presented_above(entry, presentations):
    """Whether some presentation already above `entry` applies exactly
    the grant `entry` carries, so `entry` needs no presentation of its
    own.

    A bindfs mount presents one uniform owner, group and mode over its
    whole subtree (bindfs_perms()) -- that uniformity is the constraint
    the presentation layer exists to work within, and here it pays: a
    path inside a presentation is *already* shown as that presentation's
    grant, so a node whose own grant is the same one has nothing left to
    apply. Since `access` inherits (_walk_tree()), that is the ordinary
    case rather than a corner: one `access.group` on a subtree's top
    node now grants every node beneath it, and without this each of
    those would mount a second bindfs over the first to present what it
    already presented.

    Only the *nearest* presentation above is compared, because it is the
    only one whose ownership is visible at this path -- an intervening
    presentation with a different grant is what the path actually shows,
    and a node matching its grandparent through that is a node that does
    need its own mount to get back to it.

    Two grants are the same one here when they render the same mount --
    the owner, the group and the mode the unit template passes to
    bindfs's `-u`, `-g` and `-p` (roles/stortree_mounts/templates/
    stortree-mount@.service.j2). Nothing else about a grant reaches the
    presentation, so nothing else can distinguish two of them at this
    path; comparing the dicts instead would keep a mount for a
    difference that renders identically, such as an explicit drop
    (_dropped_access()) underneath an ungranted mount."""
    above = [
        pres
        for pres in presentations
        if entry["local_path"].startswith(pres["local_path"] + "/")
    ]
    if not above:
        return False
    nearest = max(above, key=lambda pres: len(pres["local_path"]))
    return _presented_as(nearest.get("access")) == _presented_as(entry.get("access"))


def _presented_as(access):
    """The (owner, group, mode) triple a presentation mount of `access`
    actually shows -- everything the bindfs command line takes from a
    grant, and nothing else."""
    access = access or {}
    return access.get("owner"), access.get("group"), bindfs_perms(access)


def _dedupe(items, key):
    seen = set()
    result = []
    for item in items:
        k = key(item)
        if k not in seen:
            seen.add(k)
            result.append(item)
    return result


def _rclone_setting(container):
    """The raw `rclone` value inside a peer-defaults/peers-style
    block (`container` -- e.g. a top-level subtree's own
    `peer-defaults`, or one entry of its `peers` map): `False`
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


def _peer_policy(chain, hostname):
    """(enabled, args, access) describing how `hostname` -- when it
    doesn't own this node -- gets its own copy of it, resolved from
    `chain`: every `peer-defaults`/`peers` block on the node and its
    ancestors, shallowest-first (_walk_tree()'s `peer_chains`).

    Two axes of precedence, and they compose the same way at every level
    (docs/config-schema.md "Per-peer mount opt-out"):

    - Within one node, an explicit `peers.<hostname>` beats that same
      node's `peer-defaults`.
    - Across nodes, a nearer (deeper) node beats a more distant ancestor
      -- which is what lets a subdirectory refine, or reverse, whatever
      its top-level subtree set for the same host. A node with no block
      of its own contributes nothing and simply inherits, so a tree that
      only ever writes `peer-defaults`/`peers` on its top-level
      subtrees resolves exactly as it did when that was the only level
      read at all.

    `enabled` follows the nearest explicit `rclone` setting found under
    those two rules -- an allow-list where the inherited default is
    disabled (only explicitly-truthy entries mount it), a deny-list
    where it isn't (everyone mounts it but the explicitly disabled).
    "Explicitly truthy" includes an args-bearing `rclone: {args: {...}}`
    with no boolean in sight, at any level: writing peer-side mount
    args for a node is taken to mean peers are meant to have it, the
    same reading that makes the one-node allow-list idiom
    (`peer-defaults.rclone: false` plus `peers.<h>.rclone.args`)
    work at all.

    `args` merges the other way round -- every level contributes, and
    the ones that win conflicts are the nearest and the most specific:
    peer-defaults first, then `peers.<hostname>`, shallowest node to
    deepest.

    `access` accumulates like `args`, one key at a time (_merge_access())
    rather than one block at a time: every block in the chain
    contributes, nearest and most specific winning each key it sets, and
    the result then merges over the node's own grant (_peer_grant()).
    An empty `access:` anywhere in the chain drops what the blocks above
    it built, the same statement it makes on a node.

    `_UNSET` when no block in the chain sets one at all, which is the
    signal to leave the node's own grant untouched -- distinct from the
    `{}` an explicit empty block resolves to, which drops it."""
    enabled = True
    args = {}
    access = _UNSET
    for node in chain:
        defaults = node.get("peer-defaults")
        entry = (node.get("peers") or {}).get(hostname)
        defaults_setting = _rclone_setting(defaults)
        peer_setting = _rclone_setting(entry)
        effective = peer_setting if peer_setting is not _UNSET else defaults_setting
        if effective is not _UNSET:
            enabled = effective is not False
        _deep_merge(args, _rclone_args(defaults_setting))
        _deep_merge(args, _rclone_args(peer_setting))
        for container in (defaults, entry):
            if isinstance(container, dict) and "access" in container:
                block = _access_mapping(container["access"])
                access = (
                    _merge_access({} if access is _UNSET else access, block)
                    if block
                    else {}
                )
    return enabled, args, access


def _peer_samba(own, hostname):
    """The raw `samba` a peer block written *on this node* sets for
    `hostname`, or `_UNSET` where none does. `own` is the node's own raw
    config where it carries a `peer-defaults`/`peers` block at all
    (_TreeIndex.own_peer_blocks), else None.

    Within the node, `peers.<hostname>` beats `peer-defaults` and
    nothing merges -- the same two rules `access` follows in
    _peer_policy(), and for the same reason: a share is a single
    object an operator reads off one place in the config, and half a
    share assembled from two blocks would be no more readable than half
    a grant.

    Across nodes there is deliberately no rule at all, because `samba`
    does not inherit -- a node's own `samba:` never has. It marks the
    one node it is written on for export and says nothing about that
    node's descendants. Reading it off the inherited chain
    _peer_policy() walks would export *every* descendant of a node
    that carried one, all under that one name: a share-name collision by
    construction, and any per-user descendant silently shared along with
    it."""
    if not own:
        return _UNSET
    found = _UNSET
    for container in (
        own.get("peer-defaults"),
        (own.get("peers") or {}).get(hostname),
    ):
        if isinstance(container, dict) and "samba" in container:
            found = container["samba"]
    return found


def _peer_userdir_groups(own, hostname, node_path):
    """The extra groups a peer block written *on this node* gives
    `hostname` per-user directories for. `own` is the node's own raw
    config where it carries a `peer-defaults`/`peers` block at all
    (_TreeIndex.own_peer_blocks), else None.

    The one peer-block key that adds rather than replaces. `rclone`,
    `access` and `samba` each describe one host's *copy* of a node --
    how it mounts it, what it enforces on it, whether it exports it --
    and a copy is a single thing, so the nearest, most specific block
    wins outright (_peer_policy(), _peer_samba()). `userdir-groups`
    describes something else: who the node is for. A host that serves an
    extra department's home directories does not thereby stop serving
    everyone else's, and reading this key the way the others are read
    would mean it could only ever do both by restating the owner's whole
    list -- two lines meant to agree, drifting apart later. So the
    node's own list is the floor everywhere, `peer-defaults` adds to it
    on every non-owning host, and `peers.<hostname>` adds to that.

    Read off the node's own block rather than the inherited chain, for
    the reason `samba` is (above): the key does not inherit. It marks
    the one node it is written on as per-user and says nothing about
    that node's descendants, which have their own per-user level or
    none.

    Never consulted on the host that owns the node: a peer block
    describes a host holding a copy, and the owner holds the original
    (docs/config-schema.md "Per-peer mount opt-out", "Peer-side
    access")."""
    groups = []
    if not own:
        return groups
    for container in (
        own.get("peer-defaults"),
        (own.get("peers") or {}).get(hostname),
    ):
        if not isinstance(container, dict):
            continue
        for group in _normalize_userdir_groups(
            container.get("userdir-groups"), node_path
        ):
            if group not in groups:
                groups.append(group)
    return groups


_TreeIndex = collections.namedtuple(
    "_TreeIndex",
    "roots nodes nodes_by_path children peer_chains own_peer_blocks "
    "requires_by_path",
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

    The samba projections are the one thing that can *not* be settled
    here any more: which nodes a host exports depends on the host
    (_host_samba_nodes()). What stays tree-wide is the half that is
    genuinely host-independent -- a name collision between two nodes'
    own `samba:` blocks, which no host could render."""
    roots, nodes, peer_chains, children, own_peer_blocks = _walk_tree(
        _expand_dotted(tree)
    )
    _validate_requires(nodes)
    _validate_share_names(
        [(n["path"], n["samba"]["name"]) for n in _samba_nodes(nodes)]
    )
    return _TreeIndex(
        roots=roots,
        nodes=nodes,
        nodes_by_path={n["path"]: n for n in nodes},
        children=children,
        peer_chains=peer_chains,
        own_peer_blocks=own_peer_blocks,
        requires_by_path={n["path"]: n["requires"] for n in nodes},
    )


def _host_samba_nodes(index, hostname):
    """(node, samba, descendants) for every node `hostname` exports as a
    Samba share.

    A node's own `samba:` applies on every host -- that is what "Samba
    sharing is universal" has always meant and still means for every
    config that writes nothing else. A `samba:` inside one of the node's
    `peer-defaults`/`peers.<host>` blocks replaces it for that one
    host: on a node with no `samba:` of its own it *adds* a share there
    and nowhere else, and on one that has a `samba:` it renames, hides,
    or (with `samba: false`) withdraws that host's copy.

    Why a share list can be per-host at all, having been fleet-wide
    since the beginning: a share is only usable where the principals it
    admits can be resolved, and identity is not always fleet-wide. A
    service account that exists only on the host running the appliance
    that writes there -- a local Unix user, never in LDAP, deliberately
    -- cannot be named in a share on any other host. Exporting such a
    node everywhere leaves only bad options: name a principal most hosts
    cannot resolve, or export a share nobody can connect to.

    The owning host is never read out of a peer block, the same rule
    `rclone` and `access` already follow (docs/config-schema.md
    "Per-peer mount opt-out", "Peer-side access"): those blocks
    describe a host holding a *copy* of the node, and the owner holds
    the original."""
    out = []
    for n in index.nodes:
        raw = (
            _UNSET
            if n["host"] == hostname
            else _peer_samba(index.own_peer_blocks.get(n["path"]), hostname)
        )
        samba = (
            n["samba"]
            if raw is _UNSET
            else _normalize_samba(raw, n["path"], n["per_user_parent"])
        )
        if samba is not None:
            out.append((n, samba, _descendants_of(n, index.nodes)))
    _validate_share_names([(n["path"], sb["name"]) for n, sb, _d in out], hostname)
    return out


def _subtree_mount_targets(index, root_path, host):
    """Every node under top-level subtree `root_path` that `host`
    peer-mounts in its own right, as (node, args, access) --
    normally just the subtree itself, exactly as when `roots` were
    the only level a peer policy could be written at.

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
    in an otherwise host-local subtree can be handed to a peer on
    its own (`peer-defaults.rclone: false` at the top,
    `peers.<host>.rclone` on just that node -- the allow-list
    idiom, one level down). Two nodes are never descended into:
    one this host already owns (it serves that subtree itself,
    rather than mounting anyone's copy of it -- same rule the
    top-level loop always applied) and a `user-subdirs` node,
    whose path is still `%U`-templated and fans out into one mount
    per granted user rather than the single mount a subtree_mounts
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
        enabled, args, access = _peer_policy(index.peer_chains[path], host)
        if enabled:
            targets.append((node, args, access))
            continue
        stack.extend(index.children[path])
    return targets


def _samba_share_entries(index, hostname):
    """Every node `hostname` exports as a share.

    Universal by default and per-host only where a config says so
    (_host_samba_nodes()): with nothing but node-level `samba:` blocks
    written, every host resolves the same list, a peer reaches the
    same share whichever host it connects to, and the stanza comes out
    identical everywhere -- which is what this always did. A `samba:` in
    a peer block is what makes the list, and the stanza, differ."""
    shares = []
    for s, samba, descendants in _host_samba_nodes(index, hostname):

        # Each descendant carries at most one access grant now (never a
        # list, see _normalize_access()) -- the union across descendants
        # is still a list, just of (at most) one grant per descendant
        # rather than several from any single one.
        #
        # The grant taken is the one *this host* actually enforces on its
        # own copy of the descendant: the node's own where this host owns
        # it (a peer block never describes the owner), and otherwise
        # whatever `peer-defaults`/`peers.<host>` set for it -- the
        # same resolution _samba_peer_dependencies() below applies to the
        # mount itself, so `valid users` and the filesystem underneath it
        # can't disagree.
        #
        # This is the half of "the stanza is identical everywhere" that
        # host-local identity really does break, and it has to break: a
        # grant naming a principal only one host can resolve belongs in
        # that host's `valid users` and nowhere else. Naming it fleet-wide
        # would put an unresolvable name in every other host's smb.conf;
        # omitting it there too would leave the one host that *does*
        # enforce it exporting a share nobody may enter.
        access_union = []
        for d in descendants:
            if d["host"] == hostname:
                a = d.get("access")
            else:
                _enabled, _args, peer_access = _peer_policy(
                    index.peer_chains[d["path"]], hostname
                )
                a = _peer_grant(
                    d.get("access"), peer_access, d["path"], hostname
                )
            # A grant that names nobody says nothing about `valid users`
            # -- and that includes an explicit drop, whose `reset` key is
            # for the filesystem layer alone (_dropped_access()); left in
            # the union it would put a principal-less entry in front of
            # the template's own `selectattr('permissions', ...)`.
            if not (a or {}).get("owner") and not (a or {}).get("group"):
                continue
            if a not in access_union:
                access_union.append(a)

        shares.append(
            {
                "node_path": s["path"],
                "local_path": s["path"],
                "name": samba["name"],
                "subpath": samba["subpath"],
                "hidden": samba["hidden"],
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
    for s, _samba, descendants in _host_samba_nodes(index, hostname):
        for d in descendants:
            if d["host"] == hostname or not _has_own_content(d, index.nodes):
                continue
            # The descendant's own chain, not just its top-level
            # subtree's: a `peer-defaults`/`peers` block written
            # on an intermediate node -- or on this descendant
            # itself -- governs this host's copy of exactly this
            # path, without touching its siblings (_peer_policy()).
            enabled, args, access = _peer_policy(
                index.peer_chains[d["path"]], hostname
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
                    # A peer-side `access` merges over the node's
                    # own for this host's copy only (_peer_grant())
                    # -- the enforcement this host actually applies to
                    # it: the presentation's `-u`/`-g`/`-p`, and the
                    # directory's own ownership and mode where it
                    # isn't a mount.
                    "access": _peer_grant(
                        d["access"], access, d["path"], hostname
                    ),
                    "args": args,
                    # The node's own declared dependency, not the
                    # owning host's business: a peer mounts the same
                    # path locally and needs the same thing up first.
                    "requires": d["requires"],
                }
            )
    return peers


def _subtree_grant_entries(index, hostname, mounted_paths):
    """Every node whose own `access` this host has to apply to content it
    mounts but doesn't own.

    A grant written on a node describes that node, not one host's copy of
    it: spec.md §1 is that every host's local tree is the same tree, and
    a path that is `fp`'s on the host that owns it and `stortree`'s
    everywhere else is two trees. This is what makes it one.

    It used to be left to the owning host alone, on the reasoning that a
    peer mount of the owner's copy reports whatever the owner already
    applied. That was never true of these mounts and is not true now:
    layer 1 flattens every uid and gid to the mount's own `--uid`/`--gid`
    and layer 2 re-presents the result under `-u`/`-g`, so no ownership
    survives the sftp boundary in either direction. What a non-owning
    host showed instead was the covering presentation's own uniform
    ownership -- which, for a Samba share whose `valid users` named the
    grant's owner, is a share that user could traverse but not read.

    Only nodes *inside* something this host actually mounts
    (`mounted_paths`): a grant is applied by presenting a path, and there
    is nothing to present where a peer policy opted this host out of
    the subtree entirely (docs/config-schema.md "Per-peer mount
    opt-out"). A node this host owns is already its own server_subtrees
    entry, and a `user-subdirs` node is still %U-templated and fans out
    through _expand_per_user() rather than resolving to one path here.

    A `peers.<host>.access` block merges over the node's own grant for
    the host it names (_peer_grant()) -- read here from the node's own
    chain, so it applies on a node that has no other reason to be
    planned. A node that does
    (a peer block with `samba:`, say) reaches the plan through
    _samba_peer_dependencies()/_subtree_mount_entries() carrying the same
    grant already, and plan_mounts() keeps that entry rather than this
    one.

    The chain's `rclone` half is deliberately not read. `rclone: false`
    on a node underneath an enabled ancestor carves no hole out of that
    ancestor's mount (_subtree_mount_targets()) -- the path is presented
    on this host either way, and a presented path with its grant dropped
    is the thing this function exists to prevent. What that opt-out does
    govern is the ancestor, and an ancestor this host doesn't mount puts
    nothing in `mounted_paths` for anything below it to match."""
    grants = []
    for n in index.nodes:
        if n["host"] == hostname or n["per_user"]:
            continue
        if not any(n["path"].startswith(m + "/") for m in mounted_paths):
            continue
        _enabled, _args, access = _peer_policy(
            index.peer_chains[n["path"]], hostname
        )
        grant = _peer_grant(n["access"], access, n["path"], hostname)
        if grant:
            grants.append({"local_path": n["path"], "access": grant})
    return grants


def _materializes(index, hostname, path, mounted_paths):
    """Whether `hostname` has a real directory at `path` to create
    per-user folders in -- it mounts the path or an ancestor of it
    (`mounted_paths`, the same list _subtree_grant_entries() gates on),
    or it owns a node somewhere beneath it and therefore builds the
    path down to it.

    A host with neither has no business holding this node's per-user
    directories: a `peers.<h>.userdir-groups` written on a subtree that
    same host opted out of (docs/config-schema.md "Per-peer mount
    opt-out") would otherwise leave it a stray local tree of empty home
    directories backing nothing at all."""
    if any(path == m or path.startswith(m + "/") for m in mounted_paths):
        return True
    prefix = path + "/"
    return any(
        n["host"] == hostname and n["path"].startswith(prefix)
        for n in index.nodes
    )


def _userdir_parent_entries(index, hostname, mounted_paths):
    """Every node `hostname` creates per-user directories under by the
    node's own say-so, as {local_path, groups} (docs/config-schema.md
    "`userdir-groups`").

    The scope that answers "who has a folder here" from the node
    itself. The other answer -- whoever an `access` grant beneath the
    node names -- is unchanged and still resolved where it always was
    (_resolved_user_containers()'s `%U` half); this is a second source
    for the same set, not a replacement, and the two are simply unioned
    (a user named by both gets the one directory either would have
    made).

    `groups` is the node's own list on the host that owns it, and
    whatever a peer block on that node *adds* on every other host
    (_peer_userdir_groups()) -- gated by _materializes(), so a host only
    plans directories inside a path it actually has.

    A node whose groups resolve to nobody yields no entry and no
    directories, which is also what an empty `user-subdirs` has always
    done. The list is host-local identity: a group that exists in LDAP
    but has no members yet is a perfectly ordinary state, not a config
    error, and the only thing that can tell the difference is the host
    doing the `getent` (needed_groups(), spec.md §5)."""
    entries = []
    for n in index.nodes:
        if not n["per_user_parent"]:
            continue
        path = n["path"]
        groups = list(n["userdir_groups"])
        if n["host"] != hostname:
            if not _materializes(index, hostname, path, mounted_paths):
                continue
            # Added to the node's own list, not substituted for it: a
            # peer that serves one more group's home directories serves
            # the node's own groups too. Planning the whole set on every
            # host that holds the path is also what makes the addition
            # survive how the host got there -- the owner's own folders
            # arrive ready-made inside a peer mount of the subtree, but
            # a host that reaches this node by owning something *under*
            # it has no mount to inherit them from and has to create
            # them itself. Creating one that already exists is a no-op,
            # and already what a container resolved on both the owner
            # and a peer does today (_resolved_user_containers()).
            for group in _peer_userdir_groups(
                index.own_peer_blocks.get(path), hostname, path
            ):
                if group not in groups:
                    groups.append(group)
        if groups:
            entries.append({"local_path": path, "groups": groups})
    return entries


def _subtree_mount_entries(index, hostname, samba_sourced_paths, stortree_root):
    """`hostname`'s own subtree mount of each top-level subtree it doesn't
    own, as (subtree_mounts, peer_dependencies).

    A non-owning host reaches a subtree by peer-sftp'ing the host that
    actually owns it, rather than holding direct credentials to that
    subtree's own `rclone.remote` -- the same peer-sourcing rule
    _samba_peer_dependencies() applies to every samba descendant a host
    doesn't own, just generalized to every top-level subtree (mesh, not
    funneled through one shared root -- see _walk_tree()).

    Whether the owning host's own copy is remote-backed is irrelevant
    here, and used to be checked: a peer mount is sftp to that host's
    *filesystem path*, which exists whether the content arrives there
    over rclone, over a mount something else makes, or by simply living
    on its disk. Gating on the node's own `rclone.remote` left a
    non-owning host with an empty local directory where the subtree
    should be -- and it was inconsistent with
    _samba_peer_dependencies(), which has always peered a descendant
    regardless. `peer-defaults`/`peers.<hostname>.rclone`
    (docs/config-schema.md "Per-peer mount opt-out") can suppress this
    entirely for a subtree that has no business being visible outside
    its own owning host.

    `samba_sourced_paths` is what _samba_peer_dependencies() already
    claimed, and those paths are skipped here: it's the same mount, of
    the same path, from the same owning host, resolved through the same
    peer policy -- and planning it twice is a hard error downstream
    (plan_mounts() sees two entries claiming one systemd unit slug).
    Reachable without any nested peer block at all, by a top-level
    subtree that carries `samba:` itself and has no children to delegate
    its content to (_has_own_content()); the descent in
    _subtree_mount_targets() just widens the ways in. The Samba entry is
    the one to keep: identical args, and it carries the node's own
    `access` grant rather than only what a peer block granted."""
    subtree_mounts = []
    peers = []
    for root_path in index.roots:
        for node, args, access in _subtree_mount_targets(index, root_path, hostname):
            path = node["path"]
            if path in samba_sourced_paths:
                continue
            # The node's own grant unless a peer block hands this host
            # a different one -- spec.md §6's rule, and the same fallback
            # _samba_peer_dependencies() has always applied to a
            # descendant. This used to be `{}`, on the reasoning that a
            # peer mount of the owner's copy reports whatever the owner
            # applied; it doesn't (_subtree_grant_entries()), so what a
            # non-owning host showed was its own uniform default and the
            # grant stopped at the host that happened to own the node.
            access = _peer_grant(node["access"], access, path, hostname)
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
            peer_remote = _peer_remote_ref(node["host"], path, path, stortree_root)
            # A node's `requires` applies wherever it's mounted, its
            # non-owning peers included -- which is the case that
            # motivated the key at all: the cache-dir a *peer* points
            # into another subtree's mount belongs to that peer's own
            # mount of this subtree, and only the peer resolves both
            # ends of it.
            subtree_mounts.append(
                {
                    "local_path": path,
                    "remote": peer_remote,
                    "args": args,
                    # Whatever this host's own copy is to be presented
                    # as (docs/config-schema.md "Peer-side access"):
                    # the peer policy's grant where there is one, the
                    # node's own otherwise. Applied by the presentation
                    # over this mount, not by the mount -- layer 1 is
                    # uniform and ungoverned by design (spec.md §6).
                    "access": access,
                    "requires": index.requires_by_path.get(path, []),
                }
            )
    return subtree_mounts, peers


def _peer_served_by_entries(index, hostname, all_hosts, samba_hosts=None):
    """What every *other* host sources from this one -- the mirror of
    _subtree_mount_entries() and _samba_peer_dependencies(), asked from
    the other side.

    Whatever `other` would peer-mount or peer-source from this host is
    what this host has to serve it, resolved through the same
    _subtree_mount_targets()/_peer_policy() this host used for its own
    copy, so a nested opt-out (or opt-in) is honored identically at both
    ends and the sftp trust provisioned by stortree_peer_trust matches
    the mounts that actually get made.

    `samba_hosts` (see resolve()) applies to the samba half only, and to
    `other` rather than to this host: an `other` that exports no shares
    resolves no samba peer dependencies, so serving it would provision
    trust for a mount it will never make. Its own subtree mounts are
    unaffected -- opting out of *exporting* the tree says nothing about
    wanting it locally."""
    served = []
    for other in all_hosts:
        if other == hostname:
            continue
        other_serves_samba = samba_hosts is None or other in samba_hosts
        for root_path in index.roots:
            for node, _args, _access in _subtree_mount_targets(index, root_path, other):
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
        for s, _samba, descendants in _host_samba_nodes(index, other):
            for d in descendants:
                if d["host"] == hostname and _has_own_content(d, index.nodes):
                    enabled, _args, _access = _peer_policy(
                        index.peer_chains[d["path"]], other
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
    rendered, because a subtree mount of a subtree this host doesn't own
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
    avoid. Its own subtree mounts (_subtree_mount_entries()) are
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
    subtree_mounts, subtree_peers = _subtree_mount_entries(
        index,
        hostname,
        {p["local_path"] for p in samba_peers},
        stortree_root,
    )

    peer_dependencies = _dedupe(
        samba_peers + subtree_peers,
        lambda p: (p["owning_host"], p["local_path"]),
    )

    # Every path this host holds a real mount of -- what a node *inside*
    # one of them has to be judged against, both for the grant it
    # applies there (_subtree_grant_entries()) and for the per-user
    # directories it creates there (_userdir_parent_entries()).
    mounted_paths = [m["local_path"] for m in subtree_mounts if m["remote"]] + [
        p["local_path"] for p in peer_dependencies
    ]

    return {
        "server_subtrees": [n for n in index.nodes if n["host"] == hostname],
        "subtree_mounts": subtree_mounts,
        "samba_shares": _samba_share_entries(index, hostname) if serves_samba else [],
        "peer_dependencies": peer_dependencies,
        "subtree_grants": _subtree_grant_entries(index, hostname, mounted_paths),
        "userdir_parents": _userdir_parent_entries(index, hostname, mounted_paths),
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
    explicitly opted out of by `peer-defaults.rclone: false`. Nothing
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
    root's own subtree mount resolved to `local_path == ""` and mapped to
    a reserved "root" slug, which nothing can produce now.

    Internal only. This used to be exposed as a `stortree_slug` filter so
    the per-user bind-mount template could slug its `symlink_target` from
    the raw path at render time -- but a path is exactly the wrong thing
    to derive that unit name from: it can name a unit that does not
    exist, because whether a given path is a presentation depends on the
    plan and not on the path. `_relate_plan_entries()` resolves it to
    `symlink_target_slug` against the entries that actually became
    presentations instead, and answers "there is no unit" with None."""
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
    `access` grant, plus every group a node names in `userdir-groups`
    -- the set `getent group` needs to be run against
    before `group_members_from_getent()`'s result can feed
    `plan_mounts()`/`filter_rclone_conf()`'s own %U expansion, and before
    `group_gids_from_getent()`'s result can gid-own a remote-backed
    node's mount (spec.md §6) -- covers every node, not just per-user
    ones, since a plain shared (non-%U) node can be gid-owned by its
    `access.group` too, same as a per-user one. Covers all three scopes
    a resolved `access` can turn up in: `server_subtrees` (this host's
    own nodes), `peer_dependencies` (a samba descendant sourced from a
    peer) and `subtree_mounts` (this host's own copy of a subtree it
    doesn't own -- which carries a grant only when a `peer-defaults`/
    `peers.<hostname>` block gave it one, docs/config-schema.md
    "Peer-side access") and `subtree_grants` (a node *inside* something
    this host mounts, whose grant it applies with a presentation of its
    own -- the one most likely to name a group nothing else on the host
    does, since the mount above it carries whatever grant was written
    higher up). A fifth, `userdir_parents`, is read after the loop
    rather than in it: its groups are not inside an `access` grant.
    All computed once, together, by
    `stortree_facts` so every later role (`stortree_mounts`,
    `stortree_secrets`) shares one lookup and one consistent
    group_members/group_gids map, rather than each recomputing its own
    scope of it (and risking one missing a scope the others cover)."""
    groups = set()
    for entry in (
        resolved.get("server_subtrees", [])
        + resolved.get("peer_dependencies", [])
        + resolved.get("subtree_mounts", [])
        + resolved.get("subtree_grants", [])
    ):
        g = (entry.get("access") or {}).get("group")
        if g:
            groups.add(g)
    # `userdir_parents` is the fifth scope, and the only one whose
    # groups are not inside an `access` grant at all: a node's
    # `userdir-groups` names a group purely to resolve its membership
    # into per-user directories (_resolved_user_containers()), never to
    # gid-own anything. Missing it would leave that lookup empty and the
    # directories silently unmade -- exactly the "one scope the others
    # cover" failure this function exists to keep out.
    for parent in resolved.get("userdir_parents", []):
        groups.update(parent["groups"])
    return sorted(groups)


def needed_users(resolved, group_members=None):
    """Every username this host's resolved facts reference in an
    `access.owner` grant -- mirrors needed_groups() above, for the
    `getent passwd` lookup user_uids_from_getent() needs to uid-own a
    remote-backed node's mount (spec.md §6) -- over the same four
    `access`-bearing scopes, `subtree_grants` included. Also covers every per-user
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
        + resolved.get("subtree_mounts", [])
        + resolved.get("subtree_grants", [])
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
    same reason: a peer-only host with no server_subtrees of its own
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
    # The other source: a node that names its own groups rather than
    # leaving its membership to whatever is granted beneath it
    # (`userdir_parents`, _userdir_parent_entries()). Its path has no
    # `%U` in it -- the per-user folders *are* its immediate children --
    # so the prefix is the path itself. Deduped into the same map by
    # local_path, so a user who is both a member here and the resolved
    # owner of something nested below still gets exactly one container,
    # owned by them either way.
    for parent in resolved.get("userdir_parents", []):
        prefix = parent["local_path"].rstrip("/")
        for group in parent["groups"]:
            for user in access_grant_usernames({"group": group}, group_members):
                local_path = f"{prefix}/{user}" if prefix else user
                containers[local_path] = user
    return containers


def _plan_user_containers(resolved, group_members):
    """Every per-user container a `user-subdirs` node implies, as a plan
    entry of its own (docs/config-schema.md "subdirs vs user-subdirs":
    "the immediate children of a user-subdirs node are per-user
    folders").

    These used to be collected separately, because a container is never
    produced by any of the three stages above -- it is only ever implied
    as the *ancestor* of one. It is an ordinary entry now: a path that
    has to exist, carrying an `access` grant naming the one person it
    belongs to, which is exactly what every other granted plain-directory
    node is. Whether that grant is applied by a presentation mount or a
    plain chown is then the same question for a container as for anything
    else, answered the same way, in one place.

    The grant is a plain `owner` grant, so access_mode() gives it 0701 --
    private to that user, plus the traversal bit every ancestor of a
    deeper grant needs. The container mounts these carried before were
    hardcoded 0750 with the `stortree` group readable; the service
    account keeps its access through the presentation's own --mirror
    instead, which is narrower and does not depend on group membership."""
    entries = []
    for local_path, owner in sorted(
        _resolved_user_containers(resolved, group_members).items()
    ):
        entries.append(
            {
                "local_path": local_path,
                "remote": None,
                "args": {},
                "access": _normalize_access({"owner": owner}),
                "requires": [],
            }
        )
    return entries


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


def _plan_subtree_mounts(resolved):
    """This host's own copy of each top-level subtree it doesn't own, one
    entry each. Never per-user, so there's no %U fan-out to resolve here
    the way the two stages below have to. Always a mount unless the peer
    opted out (`peer-defaults`/`peers.<host>.rclone: false`), in
    which case resolve() still gives it a subtree_mounts entry so the
    local directory gets created, and it arrives here as `remote: None`
    and plans as a plain directory.

    A subtree mount's `remote` was already synthesized by resolve()
    (_peer_remote_ref()), so the provenance plan_remote_sections() needs
    -- which host that reference points at, and the path on it -- is
    joined back on from the matching peer_dependencies entry rather than
    rebuilt here from a second reading of the same tree. Keyed on
    local_path alone: a node's `host` is a single value, so a path has
    exactly one owning host and resolve()'s own dedupe leaves at most one
    entry per path."""
    peer_by_path = {p["local_path"]: p for p in resolved.get("peer_dependencies", [])}
    entries = []
    for m in resolved.get("subtree_mounts", []):
        peer = peer_by_path.get(m["local_path"])
        entries.append(
            {
                "local_path": m["local_path"],
                "remote": m["remote"],
                "args": m["args"],
                "peer": (
                    None if peer is None else _peer_provenance(peer, peer["remote_path"])
                ),
                # Whatever this host's own peer policy granted for its
                # copy, `{}` (the plain ungranted default) otherwise.
                "access": m.get("access") or {},
                "requires": m.get("requires") or [],
            }
        )
    return entries


def _plan_subtree_grants(resolved):
    """A node's own grant, applied where this host mounts that node's
    content without owning it (_subtree_grant_entries()).

    Never a mount of its own and never a directory of its own: the path
    is already inside some transport, so what this plans is exactly one
    presentation over it, which is the only thing that can make a grant
    stick inside a FUSE mount (_layer_plan_entries()). No `remote`, no
    `args`, no `requires` -- layer 1 is somebody else's entry."""
    return [
        {
            "local_path": g["local_path"],
            "remote": None,
            "peer": None,
            "args": {},
            "access": g["access"],
            "requires": [],
        }
        for g in resolved.get("subtree_grants", [])
    ]


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
    actual owning host exactly like a subtree mount of a whole top-level
    subtree is -- a mesh: each peer_dependency already names its own real
    owning host, rather than everything funnelling through one shared
    root. It uses its own `access`/`args`, never the owning host's.

    An entry with no `samba_node` is skipped: it's a top-level subtree's
    own peer mount, already planned by _plan_subtree_mounts(). A per-user
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
    """Give every entry its systemd instance name, and fill in the two
    fields only some stages set, so everything downstream can read them
    off any entry unconditionally. Collisions are checked once the
    layers are known -- see _check_slug_collisions()."""
    for e in entries:
        e.setdefault("symlink_target", None)
        e.setdefault("peer", None)
        e["slug"] = _slug(e["local_path"])


def _relate_plan_entries(entries, transports):
    """Resolve the fields that describe how entries stand to each other,
    rather than what any one of them is on its own. Needs the whole
    plan, which is why it runs once here over the finished list instead
    of inside any of the stages that built it. Runs after
    _layer_plan_entries(), because every one of these relations is
    between things whose layer is already decided.

    `requires_slug` names the nearest ancestor that is a presentation
    mount, if any -- for systemd After=/PartOf=/RequiresMountsFor= so a
    mount nested inside a presented node starts after the mount that
    puts its own mountpoint on screen, skipping any plain directory in
    between, which has no unit to wait for.

    A *search* for the deepest such ancestor, not a look at the
    immediate parent. A granted node can sit several levels above the
    thing nested inside it -- a grant at `system/data` presenting a
    descendant mount at `system/data/store/thing` is two -- and an
    immediate-parent lookup silently finds nothing there, leaving that
    descendant with no ordering at all against the mount that owns its
    mountpoint.

    There is no `has_nested_children` any more. It existed because a
    mount with something nested inside it had to present
    stortree:stortree regardless of its own grant, or fusermount would
    refuse the nested mountpoint as one the mounting account cannot see
    itself owning. Layer 1 removes the premise for the rclone mounts:
    transports nest inside each other under the remotes root, but every
    transport presents one uniform stortree:stortree, so the mounting
    account always owns the mountpoint. Presentations nest too, and
    handle it with bindfs's --mirror instead, which keeps the grant real
    for every actual user (stortree-mount@.service.j2) rather than
    discarding it -- which was the whole bug.

    `requires_mounts` is the node's own declared `requires`
    (_normalize_requires(), validated tree-wide by _validate_requires())
    resolved against *this host's* transports. It attaches to layer 1,
    not layer 2: what a declaration like this expresses is a backend
    dependency -- a cache directory that must be mounted before the
    mount that writes into it -- and caching is now entirely a transport
    concern. A target that is not backed by a transport on this host
    drops out silently: it is either a plain local directory (nothing to
    order against, this same apply creates it before any unit starts) or
    a subtree some other host owns and this one does not peer."""
    presented = [e for e in entries if e["kind"] == "mount"]
    presented_by_path = {e["local_path"]: e["slug"] for e in presented}

    for e in entries:
        best = None
        for other in presented:
            if other is e:
                continue
            prefix = other["local_path"] + "/"
            if e["local_path"].startswith(prefix):
                if best is None or len(other["local_path"]) > len(best["local_path"]):
                    best = other
        e["requires_slug"] = best["slug"] if best else None
        # The ancestor's own path, so a unit can name the mountpoint it
        # has to wait for without reversing the slug back into a path
        # (slugs are escaped for systemd and are not paths -- see
        # _layer_plan_entries()).
        e["requires_slug_path"] = best["local_path"] if best else None
        # The presentation a bind mounts *from*, when there is one --
        # and there is not always one, which is the whole reason this is
        # a field rather than the bind template slugging `symlink_target`
        # itself. A `group`-only node collapses to one shared mount at
        # `.mounts/<name>` plus a bind per member; whether that shared
        # path is a *presentation* depends on something the path alone
        # cannot say. With a transport above it (the node carries its own
        # `rclone.remote`, or some ancestor does) the grant can only be
        # applied by a bindfs mount, so there is a `stortree-mount@` unit
        # to depend on. On the host that owns the subtree, with no
        # `rclone` anywhere above, the same node is an ordinary local
        # directory whose grant is a real chown -- no mount, and so no
        # unit. Slugging the path regardless produced a bind that
        # `Requires=` a unit nothing ever rendered, which systemd refuses
        # to start at all ("Unit stortree-mount@... not found"), taking
        # down a member's folder over a dependency that was never
        # meaningful. Looked up by path against the entries that actually
        # became presentations, so it answers the real question instead
        # of assuming it.
        e["symlink_target_slug"] = (
            presented_by_path.get(e["symlink_target"])
            if e["symlink_target"]
            else None
        )

    transport_by_path = {t["local_path"]: t["slug"] for t in transports}

    for e in list(entries) + list(transports):
        e["requires_mounts"] = [
            {"local_path": path, "slug": transport_by_path[path]}
            for path in e.pop("requires", [])
            if path in transport_by_path
        ]


def _check_slug_collisions(entries):
    """Fail the run when two entries in the same unit family want one
    instance name.

    A collision means one unit file rendered twice, with whichever entry
    the role's loop reaches last silently winning -- raised here rather
    than discovered as a mount serving the wrong thing. _slug() is
    injective over paths, so two *different* paths cannot collide; what
    this catches is the same path planned twice.

    Per family, because the families are separate namespaces and one
    path legitimately appears in two of them: the node that declares a
    remote names both its transport and its presentation, and
    `stortree-remote@tree.service` and `stortree-mount@tree.service` are
    different units. A `dir` has no unit and is not checked."""
    for kind in ("transport", "mount", "bind"):
        seen = {}
        for e in entries:
            if e["kind"] != kind:
                continue
            clash = seen.get(e["slug"])
            if clash is not None:
                raise ValueError(
                    f"stortree: {clash!r} and {e['local_path']!r} both resolve to "
                    f"systemd unit slug {e['slug']!r} -- rename one of them"
                )
            seen[e["slug"]] = e["local_path"]


def _layer_plan_entries(entries):
    """Split the flat plan into the two layers the tree actually runs
    (docs/spec.md §2 "The two layers"), returning the transport entries
    to prepend.

    **Layer 1, transport.** One `rclone mount` per node that declares an
    `rclone.remote`, mounted under `stortree_remotes_root` at the node's
    own path -- so the remotes root is a parallel copy of the tree
    holding the raw, ungoverned view of every backend, and
    `<remotes_root>/<path>` and `<stortree_root>/<path>` are the same
    directory seen through two mounts. This is the only layer that talks
    to a backend and the only layer that caches, so every
    operator-supplied `rclone.args` value belongs to it.

    Because layer 1 lives outside `stortree_root`, a backend's own
    directory structure holds only what config.yml describes. The
    revision this replaced staged inside the tree, next to each node it
    served, and every staging name it invented showed up on the remote.

    Mirroring the tree rather than flattening to one directory per
    remote is not just tidiness. A flat layout has to name each
    directory somehow, and the obvious name -- the systemd slug -- is
    exactly the wrong one: `_slug()` escapes `-` as `\x2d` so unit names
    stay injective, and systemd then *unescapes* that same sequence when
    it parses an `ExecStart` path, turning a slug like `backups\x2dmirror`
    back into `backups-mirror` and silently pointing the mount at another
    directory. Verified on a real host. Slugs name units; paths name
    paths.

    **Layer 2, presentation.** One `bindfs` mount per visible node that
    needs its own ownership, reading `<remotes_root>/<path>` and mounted
    at `<stortree_root>/<path>`. Source and target are the same
    directory on the backend reached by two different local paths, which
    is what makes this work with no data movement and nothing to
    self-mount.

    `kind` replaces the implicit switch on truthy `remote`/
    `symlink_target` that callers used to make:

    | kind | what it is |
    | --- | --- |
    | `transport` | layer 1, an rclone mount under the remotes root |
    | `mount` | layer 2, a bindfs mount at the node's visible path |
    | `bind` | a per-user fan-out of a layer-2 mount |
    | `dir` | a plain directory, created and nothing more |

    A node gets a presentation when it has a remote of its own (so its
    content reaches the visible tree at all) or when it carries an
    `access` grant that nothing above it already applies and sits inside
    some transport (so that grant is applied where nothing else can
    apply it). Everything else is a `dir`: it appears through whichever
    presentation is above it, and only has to exist.

    "That nothing above it already applies" is what keeps an inherited
    grant (_walk_tree()) from costing a mount per node it reaches: a
    presentation is uniform over its whole subtree, so every node that
    inherited its grant unchanged is already presented correctly and
    stays a `dir` -- one bindfs at the node the grant was written on,
    however deep the subtree beneath it goes. It still carries the grant
    in the plan, and stortree_mounts still verifies the path against it
    (roles/stortree_mounts/tasks/main.yml); what it does not do is mount
    a second bindfs to re-present what the first one presents.
    _grant_presented_above() has the details.

    One transport per declaring node, not per distinct remote spec. Two
    nodes naming the same `remote:path` would share a mount only if one
    nested inside the other, since a transport serves exactly the
    subtree rooted at its own node; deduping siblings would leave the
    second node's subpath served by nothing.

    Nesting is the exception, and _transport_covers() decides it: a peer
    mount whose content a transport already above it presents gets no
    transport of its own, only its presentation, which reads the
    covering mount from the inside. Without that, a host that peers a
    whole subtree *and* exports a Samba share for one path inside it
    (_samba_peer_dependencies(), the ordinary shape for a hidden share
    over content this host doesn't own) opens two sftp sessions to one
    account and runs two rclone processes over the same bytes -- and,
    because `--vfs-cache-max-size` is per mount, budgets the cache twice.
    The presentation is what carries the grant, so collapsing the
    transport under it costs nothing: the path is presented exactly as
    before, from one mount instead of two.

    Only the transports stortree synthesizes itself collapse. A pair of
    operator-written remotes may well nest too, but proving it means
    parsing `remote:path` strings whose meaning belongs to the backend,
    while a peer reference is built here (_peer_remote_ref()) out of an
    owning host and a path this function can compare exactly."""
    transports = []
    for e in sorted(
        (e for e in entries if e["remote"]),
        key=lambda e: (e["local_path"].count("/"), e["local_path"]),
    ):
        # Shallowest first, and only against the transports already
        # kept: a collapsed entry has no mount, so it can never be what
        # covers something deeper. Three nested peers of one host leave
        # one transport at the top, not a chain.
        if any(_transport_covers(e, t) for t in transports):
            continue
        transports.append(
            {
                "kind": "transport",
                "local_path": e["local_path"],
                "slug": e["slug"],
                "remote": e["remote"],
                "args": e["args"],
                "peer": e["peer"],
                "access": {},
                "symlink_target": None,
                "transport_slug": None,
                "requires_slug": None,
                "requires_slug_path": None,
                "requires": list(e.get("requires") or []),
            }
        )

    roots = {t["local_path"]: t["slug"] for t in transports}
    presentations = []
    # Shallowest first, so a presentation is decided before anything it
    # covers is (_grant_presented_above()). The list itself keeps the
    # order the stages built it in -- this only settles each entry's own
    # fields, in place.
    for e in sorted(
        entries, key=lambda e: (e["local_path"].count("/"), e["local_path"])
    ):
        # The deepest transport rooted at or above this path. Picked with
        # max() rather than by tracking a best-so-far, because a
        # best-so-far comparison is only ever exercised in one direction
        # here -- the roots happen to be ordered shallowest-first -- and
        # a guard that cannot be reached is a guard nobody can trust.
        above = [
            (path, slug)
            for path, slug in roots.items()
            if e["local_path"] == path or e["local_path"].startswith(path + "/")
        ]
        e["transport_slug"] = max(above, key=lambda ps: len(ps[0]))[1] if above else None

        access = e.get("access") or {}
        # A drop is a grant for this purpose: it asks for the plain
        # default to be presented at this path, which inside somebody
        # else's presentation takes a mount exactly like any other
        # ownership does (_dropped_access()).
        granted = bool(
            access.get("owner") or access.get("group") or access.get("reset")
        )
        if e["symlink_target"]:
            e["kind"] = "bind"
        elif e["remote"] or (
            granted
            # Asked of every granted entry, inside a transport or not.
            # Outside one there is never anything above to have applied
            # the grant already -- a presentation only exists within
            # layer 1 -- so the answer is "nothing does", and the
            # `transport_slug` test below is left to decide what it
            # always decided: a real local directory takes a chown, not a
            # mount.
            and not _grant_presented_above(e, presentations)
            and e["transport_slug"]
        ):
            e["kind"] = "mount"
            presentations.append(e)
        else:
            e["kind"] = "dir"
        # The remote belongs to layer 1 now; a presentation reads a local
        # path. Leaving it set would have rclone.conf planning and every
        # `selectattr('remote')` count the same backend twice.
        e["remote"] = None

    # A transport nests inside whichever transport is above it, exactly
    # as its node does in the tree -- the remotes root mirrors the tree,
    # so the shapes are identical.
    for t in transports:
        above = [
            other
            for other in transports
            if other is not t
            and t["local_path"].startswith(other["local_path"] + "/")
        ]
        t["requires_transport"] = (
            max(above, key=lambda o: len(o["local_path"]))["slug"] if above else None
        )

    return transports


def plan_mounts(resolved, group_members=None, stortree_root=DEFAULT_STORTREE_ROOT):
    """Flatten this host's resolved subtree_mounts/server_subtrees/
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
        _plan_subtree_mounts(resolved)
        + _plan_server_subtrees(resolved, group_members)
        + _plan_peer_dependencies(resolved, group_members, stortree_root)
    )
    # Client grants and containers are appended last and only where the
    # three stages above produced nothing for that path already: a
    # user-subdirs node whose own immediate child is the granted one
    # yields an entry at the container's exact path, and a node reached
    # by a peer or samba block already carries the grant that block
    # gave it -- which is the one that wins (_subtree_grant_entries()).
    # Either way the two would otherwise race to mount over each other.
    # Deliberately *not* a dedupe over the whole list -- two of the three
    # stages planning one path is a resolve()-level mistake, and
    # _check_slug_collisions() has to still see it.
    for late in (
        _plan_subtree_grants(resolved),
        _plan_user_containers(resolved, group_members),
    ):
        planned = {e["local_path"] for e in entries}
        entries += [e for e in late if e["local_path"] not in planned]

    _assign_plan_slugs(entries)
    transports = _layer_plan_entries(entries)
    _relate_plan_entries(entries, transports)
    _check_slug_collisions(transports + entries)

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

    # Transports first, and shallowest-first among themselves for the
    # same reason: layer 1 has to exist before anything is created or
    # mounted against it, and a nested transport's own mountpoint lives
    # inside the transport above it.
    transports.sort(key=lambda e: e["local_path"].count("/"))
    return transports + entries


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
    `access` grant existing at all.

    Deduped, in first-seen order: the list names principals, and one
    principal named twice admits nobody new. The union it renders is
    deduped by whole grant (_samba_share_entries()), which is not the
    same thing -- since `access` inherits, a share's descendants
    routinely carry the same group inside grants that differ elsewhere,
    and each would otherwise contribute that group again."""
    tokens = ['"%U"'] if include_self else []
    for a in access_list:
        if a.get("group"):
            tokens.append(f'"@{a["group"]}"')
        if a.get("owner"):
            tokens.append(f'"{a["owner"]}"')
    return list(dict.fromkeys(tokens))


def samba_write_tokens(access_list, include_self=False):
    """samba_access_tokens() narrowed to the principals a grant actually
    lets write -- smb.conf's `write list`, as against the `valid users`
    the other renders.

    Per principal, not per grant, because a grant can give its owner and
    its group different levels now (access_mode(), docs/config-schema.md
    "Access"): with `permissions: {owner: rwx, group: r-x}` the owner
    writes and the group does not, and a `write list` naming both would
    tell Samba the opposite of what the filesystem underneath enforces.
    Read off the resolved mode rather than the `permissions` value, so
    the answer is the same one the kernel will give -- including for the
    owner slot of a group-only grant, which keeps full control whatever
    level the group was given (access_mode() again).

    `%U` rides along with `include_self` exactly as it does in
    `valid users`: a per-user share's own path confines each connecting
    user to their own subtree, which is theirs to write in.

    Deduped like `valid users`, and for the same reason -- with one
    difference worth knowing: a principal writable through any grant in
    the union is in the list, so a group given `rwx` on one descendant
    and `r-x` on another writes. The narrower grant is still enforced by
    the filesystem underneath; `write list` only decides whether Samba
    itself refuses first."""
    tokens = ['"%U"'] if include_self else []
    for a in access_list:
        mode = access_mode(a)
        if a.get("group") and int(mode[2]) & 2:
            tokens.append(f'"@{a["group"]}"')
        if a.get("owner") and int(mode[1]) & 2:
            tokens.append(f'"{a["owner"]}"')
    return list(dict.fromkeys(tokens))


def apt_installable(policy_stdout, names):
    """Which of `names` `apt-cache policy` reported an installation
    candidate for, in the order given.

    `apt-cache policy a b` prints one block per name, and a name apt has
    never heard of either gets a block whose `Candidate:` reads "(none)"
    or -- if nothing in the archive so much as references it -- no block
    at all and a note on stderr. Both mean the same thing here, so both
    resolve to "not installable" rather than to two different errors.

    stortree_samba uses this to pick a WS-Discovery implementation:
    Debian dropped the Python `wsdd` after bookworm and ships the
    unrelated C `wsdd2` instead, so "which package does this platform
    have" is a question about the host's apt cache, not about its
    release codename -- a codename table would have to be extended for
    every future release, and would still be wrong on a host whose
    `universe`/backports configuration differs from the fleet's."""
    blocks = {}
    current = None
    for line in policy_stdout.splitlines():
        if line and not line[0].isspace():
            current = line.rstrip(":").strip()
        elif current and line.strip().startswith("Candidate:"):
            blocks[current] = line.split(":", 1)[1].strip()
    return [n for n in names if blocks.get(n, "(none)") != "(none)"]


# Which unit family renders each kind of plan entry. A `dir` has no
# unit at all. Kept as one mapping because three separate places read it
# -- the render tasks, the stale-unit sweep, and status.yml -- and a
# family that exists in one but not the others is a unit that is started
# but never listed, never reset and never cleaned up.
UNIT_FAMILIES = {
    "transport": "stortree-remote@",
    "mount": "stortree-mount@",
    "bind": "stortree-bind@",
}


def mount_unit_names(mount_plan):
    """The full systemd unit filename for every entry in a
    plan_mounts() result that gets one -- one per `kind` that maps to a
    unit family (UNIT_FAMILIES), skipping plain directories, which have
    no unit to name.

    Used by stortree_mounts to work out which currently-installed units
    are stale, through stale_unit_names() below, which is what the role
    actually calls."""
    return [
        f"{UNIT_FAMILIES[e['kind']]}{e['slug']}.service"
        for e in mount_plan
        if e["kind"] in UNIT_FAMILIES
    ]


def stale_unit_names(installed_paths, mount_plan):
    """Which currently-installed stortree unit *files* no longer belong
    to this host's resolved plan -- the ones stortree_mounts stops,
    disables and removes before it touches any path on disk.

    `installed_paths` is whatever `ansible.builtin.find` turned up under
    /etc/systemd/system (full paths; only the basename is compared). The
    resolved set is mount_unit_names() -- the same function that names
    every unit the role renders, which is why this join lives next to it
    rather than as a Jinja chain in the role: a rename on either side
    that stops this matching means a live unit stopped and deleted on
    every apply and re-rendered immediately after, and nothing else in
    the suite would notice."""
    resolved = set(mount_unit_names(mount_plan))
    return [
        name
        for name in (path.rsplit("/", 1)[-1] for path in installed_paths)
        if name not in resolved
    ]


def ownership_mismatch(result, default_owner, default_group):
    """One `stat` result against the grant its entry resolved to: `""`
    when they agree, a human-readable description of the difference when
    they do not.

    A filter rather than an assertion in the role because the comparison
    needs three fields at once and a failing `assert` would stop at the
    first drifted path instead of listing every one.

    Existence was never the interesting question. The bug the
    presentation layer exists to fix looked exactly like success --
    Ansible reported the chown as `changed`, the path existed, and
    `stat` kept returning the ancestor mount's own uniform owner forever
    after. Nothing compared the two, so nothing noticed."""
    item = result["item"]
    access = item.get("access") or {}
    stat = result["stat"]
    want = (
        access_owner(access, default_owner),
        access_group(access, default_group),
        access_mode(access).lstrip("0"),
    )
    # ansible.builtin.stat reports `mode` as an octal *string* ("0751"),
    # not an int, so this is a suffix compare rather than an oct().
    got = (stat.get("pw_name"), stat.get("gr_name"), str(stat.get("mode", ""))[-3:])
    if got == want:
        return ""
    return (
        f"{item['local_path']} is {got[0]}:{got[1]} {got[2]} "
        f"(expected {want[0]}:{want[1]} {want[2]})"
    )


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


def _mountinfo_unescape(field):
    """One /proc/self/mountinfo field with its octal escapes resolved.

    The kernel escapes space, tab, newline and backslash as \\040, \\011,
    \\012 and \\134 in the paths it prints there. None of them appear in a
    stortree path -- slugs are built from config.yml keys -- but the
    field is a path the *operator* chose (stortree_remotes_root) and
    comparing a raw escape against a real path is a silent miss, not an
    error, so it is worth the three lines."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _mountinfo_targets(mountinfo):
    """Every path currently mounted on, from the text of a host's
    /proc/self/mountinfo.

    Deliberately not `ansible_facts.mounts`: Ansible's own mount-fact
    gathering drops any mount whose device string neither starts with
    "/" nor contains ":/", which silently loses some of this fleet's own
    rclone mounts (see stortree_mounts' probe task for the case that hit
    production). It is also not a `stat` -- reading mountinfo touches no
    filesystem at all, so a hung or half-dead FUSE mount elsewhere in
    the tree cannot block the answer, which matters because the callers
    below run on hosts where exactly that is the thing being diagnosed.

    Field 5 (index 4) is the mount point; the fields before it are fixed
    in number, so this needs no fragile scan for the "-" separator that
    precedes the variable-length tail."""
    return {
        _mountinfo_unescape(fields[4])
        for fields in (line.split() for line in mountinfo.splitlines())
        if len(fields) > 4
    }


def mounted_transport_slugs(plan, mountinfo, remotes_root):
    """The slugs of `plan`'s transports that have something mounted on
    their own mountpoint right now (`mountinfo` being the text of this
    host's /proc/self/mountinfo).

    stortree_mounts uses this to decide whether a path that resolves
    *inside* a transport may be created yet, and getting that wrong is
    not a missed directory -- it is an unrecoverable one. A transport's
    mountpoint under the remotes root is an ordinary local directory
    while the mount is down, so `file: state: directory` on anything
    beneath it writes to the underlying disk instead of to the backend,
    and rclone then refuses to ever mount there again ("is not empty,
    use --allow-non-empty to mount anyway"). The transport being down is
    what caused the stray directories, and the stray directories are
    what keep it down: without this check one failed mount is permanent.
    What buries it deeper is that the strays outlive the config they
    came from -- the production host this was found on had a mountpoint
    held shut by two dozen empty directories under names (`mp-fam`) that
    a later rename (`_mp-fam`) had already retired, so nothing in the
    current plan even referred to them any more.

    The masked-path check next door does not cover this. It asks whether
    root can *reach* a path -- which a plain empty directory under a
    stopped mount answers "yes" to, cheerfully and wrongly. This asks
    the different question of whether what is at that path is the
    backend or the bare disk."""
    mounted = _mountinfo_targets(mountinfo)
    return [
        entry["slug"]
        for entry in plan
        if entry["kind"] == "transport"
        and f"{remotes_root.rstrip('/')}/{entry['local_path']}" in mounted
    ]


# -- metrics endpoints (monitoring, layered over the plan) ----------------
#
# Every rclone metrics (or rc) server is per-*process*. A manager or rcd
# running anywhere else cannot see a mount this host started -- upstream
# is explicit about it ("No, you need to mount them on that rclone"), and
# `mount/listmounts` on a CLI-started mount comes back empty. So there is
# no single endpoint per host to point a scraper at: there is one per
# transport unit, each needing a listen address of its own.
#
# Which makes this a mount-availability concern rather than a monitoring
# nicety. rclone exits when it cannot bind its metrics listener, and the
# transport unit is Type=notify, so a port two mounts both want is not a
# missing counter -- it is a mount that never comes up, and (PartOf=)
# every presentation and bind above it going down with it. That is why
# the allocation lives here as a pure function with a collision check
# that raises, instead of being assembled in Jinja at render time where
# the first anyone would hear of a clash is `systemctl status`.

# 20000-29999 by default: clear of the well-known ports a storage host
# actually runs (9090 Prometheus, 9100 node_exporter -- and losing a
# mount to node_exporter's port would be a genuinely baffling outage),
# and below the 32768 floor of Linux's default ip_local_port_range, so a
# listener can't lose a race to an outbound connection's ephemeral port.
DEFAULT_METRICS_BASE_PORT = 20000
DEFAULT_METRICS_PORT_SPAN = 10000


def metrics_ports(
    mount_plan,
    base_port=DEFAULT_METRICS_BASE_PORT,
    span=DEFAULT_METRICS_PORT_SPAN,
    overrides=None,
):
    """slug -> TCP port for every transport entry in a plan_mounts()
    result: the port that mount's own metrics (or rc) server listens on,
    on every address stortree_metrics_bind resolved to.

    Derived from the node's path, and deliberately not from its position
    in the plan. Assigning base_port + index would be simpler, shorter
    and predictable, and it is wrong: adding one node near the start of
    the tree shifts every later node's port, which rewrites those units'
    ExecStart, which makes "Restart any unit whose file actually changed"
    restart them -- and a transport restart cascades through PartOf= to
    every presentation and bind above it. Editing one unrelated entry in
    config.yml would remount the host's whole tree and drop every open
    Samba handle on it. A path-derived port moves only when that node's
    own path moves, which is already a remount.

    The cost of hashing instead of counting is that two paths can land
    on one port -- ~0.2% for a 20-mount host over the default span, and
    reported here rather than discovered as a dead mount. `overrides` is
    the escape hatch: `{tree path: port}` (paths, not slugs -- a slug is
    a systemd instance name, not something an operator should have to
    spell), which also wins over the derived port for a node that has to
    sit somewhere specific for a firewall rule.

    Transports only. Presentations and binds are bindfs and `mount
    --bind`; neither is rclone and neither has anything to serve."""
    if base_port < 1 or span < 1 or base_port + span > 65536:
        raise ValueError(
            f"stortree: metrics port range {base_port}-{base_port + span - 1} "
            "is not a valid TCP port range"
        )
    overrides = overrides or {}
    ports = {}
    claimed = {}
    # Sorted so the pair named in a collision message is stable between
    # runs on the same config, rather than following plan order.
    transports = sorted(
        (e for e in mount_plan if e["kind"] == "transport"),
        key=lambda e: e["local_path"],
    )
    for entry in transports:
        path = entry["local_path"]
        if path in overrides:
            port = int(overrides[path])
        else:
            digest = hashlib.sha256(path.encode("utf-8")).digest()
            port = base_port + int.from_bytes(digest[:4], "big") % span
        clash = claimed.get(port)
        if clash is not None:
            raise ValueError(
                f"stortree: {clash!r} and {path!r} both want metrics port "
                f"{port} -- set stortree_metrics_port_overrides for one of "
                "them (a mount whose listener cannot bind does not start)"
            )
        claimed[port] = path
        ports[entry["slug"]] = port
    return ports


def _interface_address(name, facts):
    """The address to listen on for one interface name, from gathered
    facts. Raises rather than falling back to a wildcard bind: guessing
    0.0.0.0 for an interface that isn't there would silently publish
    every mount's endpoint on every network the host is attached to."""
    # Ansible flattens the characters a real interface name can contain
    # ("br-lan" -> ansible_facts["br_lan"]), so try both spellings
    # before concluding the interface doesn't exist.
    for key in (name, re.sub(r"[-.:]", "_", name)):
        interface = facts.get(key)
        if interface:
            break
    else:
        raise ValueError(
            f"stortree: no gathered facts for interface {name!r} -- "
            "stortree_metrics_bind names it, but this host has no such "
            "interface (or the play did not gather facts)"
        )
    ipv4 = (interface.get("ipv4") or {}).get("address")
    if ipv4:
        return ipv4
    # Link-local is skipped on purpose: binding fe80:: needs a zone id
    # the facts don't carry in `address`, and rclone would fail to bind.
    ipv6 = next(
        (
            a.get("address")
            for a in (interface.get("ipv6") or [])
            if a.get("address") and a.get("scope") != "link"
        ),
        None,
    )
    if ipv6:
        return ipv6
    raise ValueError(
        f"stortree: interface {name!r} has no routable address to bind "
        "metrics to -- give it one, or name an address directly in "
        "stortree_metrics_bind"
    )


def metrics_listeners(bind, facts):
    """Turn `stortree_metrics_bind` -- addresses, interface names, or
    both -- into what the transport unit and the targets file need:

        [{"address", "listen", "device", "loopback"}, ...]

    rclone binds addresses, never interfaces, so an interface name has
    to be resolved against this host's own facts at render time; that is
    also why `stortree_metrics_bind` is host-local operational policy in
    inventory rather than anything config.yml could describe, since the
    same name resolves differently on every host.

    `listen` is the address as it goes into a listen string, bracketed
    when it's IPv6 so `--metrics-addr [2001:db8::1]:20123` parses.
    `device` is the systemd .device unit for an interface-derived
    address -- escaped the way systemd escapes it, via the same
    _escape_slug_segment() the unit slugs use -- for the unit to order
    itself after, or None for a literal address, which is nothing
    systemd can wait on. `loopback` is what the role's safety assert
    reads: an rc-flavour endpoint serves config/dump, so binding one off
    loopback without authentication publishes this host's scoped
    rclone.conf, credentials and all."""
    listeners = []
    for item in bind or []:
        try:
            ip = ipaddress.ip_address(item)
        except ValueError:
            address = _interface_address(item, facts)
            ip = ipaddress.ip_address(address)
            device = (
                "sys-subsystem-net-devices-"
                + _escape_slug_segment(item)
                + ".device"
            )
        else:
            address, device = item, None
        listeners.append(
            {
                "address": address,
                "listen": f"[{address}]" if ip.version == 6 else address,
                "device": device,
                "loopback": ip.is_loopback,
            }
        )
    return listeners


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
            "stortree_bindfs_perms": bindfs_perms,
            "stortree_needed_groups": needed_groups,
            "stortree_needed_users": needed_users,
            "stortree_plan_mounts": plan_mounts,
            "stortree_metrics_ports": metrics_ports,
            "stortree_metrics_listeners": metrics_listeners,
            "stortree_stale_units": stale_unit_names,
            "stortree_path_masked": path_masked,
            "stortree_mountinfo_targets": _mountinfo_targets,
            "stortree_mounted_transports": mounted_transport_slugs,
            "stortree_apt_installable": apt_installable,
            "stortree_ownership_mismatch": ownership_mismatch,
            "stortree_samba_access_tokens": samba_access_tokens,
            "stortree_samba_write_tokens": samba_write_tokens,
        }
