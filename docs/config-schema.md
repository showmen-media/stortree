# config.yml / ldap.yml / rclone.conf — schema reference

Describes the shape of the three source-of-truth files. See
[spec.md](spec.md) for how they're used.

## Dotted-key shorthand

Throughout these files, a dotted key is shorthand for a nested mapping.
`rclone.remote: storagebox` means `rclone: {remote: storagebox}`;
`access.owner: jd` means `access: {owner: jd}`. Both forms are
equivalent and may be mixed — use whichever is more readable for a given
node (a single override reads better dotted; several together read better
nested, as `media-prod` does for `rclone`).

## config.yml

The top level of the file *is* the map of top-level subtrees — every key
at this one level names a real subdirectory of `/srv/stortree` directly,
no wrapping `subdirs:` key needed here (nested subdirs still use their
own `subdirs:`/`user-subdirs:` key, same as always). There's no single
implicit tree root: each top-level entry is independent and stands on
its own, shaped exactly like any other node below it:

```yaml
<name>:                       # a real subdirectory of /srv/stortree
  host: <hostname>            # serving host for this subtree and everything under it
  rclone.remote: <remote-spec>  # optional — see "rclone.remote is verbatim" below;
                               # with host+remote both set, <hostname> self-mounts it,
                               # same rule as any other node (see "Node inheritance")

  peer-defaults:              # applies to this node and everything under it — write it
                               # on a subdirectory as readily as on a top-level subtree
    rclone.args: {...}        # base rclone mount args, merged into every non-owning
                               # host's peer mount of this node — including one with
                               # no entry under peers: below
    rclone: false              # optional — set instead of/alongside .args to keep this
                               # node off every non-owning host by default; see
                               # "Per-peer mount opt-out" below
    access: {...}              # optional — the {group?, owner?, permissions?} object a
                               # node itself takes, replacing this node's own grant on
                               # every non-owning host; see "Peer-side access" below
    samba: {...}               # optional — the same share settings a node itself takes,
                               # replacing this node's own export on every non-owning
                               # host (and adding one where the node has none); see
                               # "Per-host shares" below. Does not inherit — it marks
                               # this node only, never its descendants
    userdir-groups: [...]      # optional — extra groups whose members get a per-user
                               # directory here on every non-owning host. The one peer
                               # key that *adds* rather than replaces; see
                               # "`userdir-groups`" below

  peers:
    <hostname>:
      rclone.args: {...}      # this peer's overrides, merged over peer-defaults —
                               # a host needs no entry here to become a peer; see
                               # "Every inventory host participates" below
      rclone: false            # optional per-peer override of peer-defaults.rclone;
                               # see "Per-peer mount opt-out" below
      access: {...}            # optional per-peer override of peer-defaults.access;
                               # see "Peer-side access" below
      samba: {...}             # optional per-peer override of peer-defaults.samba;
                               # `samba: false` withdraws the share on this host alone.
                               # See "Per-host shares" below
      userdir-groups: [...]    # optional — added to peer-defaults.userdir-groups and to
                               # the node's own list for this host, never replacing
                               # either; see "`userdir-groups`" below

  requires: [<path>, ...]      # optional — other subtrees whose mounts must be up
                               # before this one starts; tree-relative paths, a bare
                               # string accepted for one. See "Requires" below
  access: {group: <name>, owner: <name>, permissions: <rwx-string>}  # see Access below
                               # — a single object, all three optional; dotted shorthand
                               # (access.group:/access.owner:/access.permissions:) works too.
                               # `permissions` also takes one level per Unix class:
                               # {owner: rwx, group: r-x, other: "---"}
                               # Inherits: it grants this node and everything under it,
                               # and a descendant's own keys merge over it (see
                               # "Access inheritance"); `<key>: null` takes one key
                               # back, an empty `access:` the whole grant
  samba:                       # presence marks this node for export — write it bare
                               # (or `samba: true`/`samba: {}`) to share with the
                               # defaults, `samba: false` to opt back out
                               # — every participating host exposes this node
                               # as a share, not just its resolved owner; see
                               # "Samba sharing is universal" below. The share
                               # path is derived: a node that declares a per-user
                               # level (`user-subdirs` or `userdir-groups`) gets
                               # Samba's per-user `%U`, one without serves the
                               # node itself
    name: "<share-name>"      # optional — the share's name in smb.conf, i.e. what
                               # SMB clients mount as //<host>/<name>; defaults to the
                               # node's path with everything outside [A-Za-z0-9_-]
                               # folded to `_` (`tree/home` → `tree_home`)
    hidden: true               # optional — keep the share out of the host's browse
                               # list (`browseable = no`). Not access control; the
                               # share stays mountable by its exact name. Nor is it
                               # host discovery (spec.md §4): this hides one share
                               # from a client already talking to the host, and says
                               # nothing about whether the host itself is found
  subdirs: {...}               # recurse — this and everything under it works exactly the
                               # same as it does at the top level, just nested
  user-subdirs: {...}          # recurse — see note below
  userdir-groups: [<group>, ...]  # optional — the groups whose members get a per-user
                               # directory under this node. Declares the per-user level
                               # in its own right, so it needs no `user-subdirs` beside
                               # it; see "`userdir-groups`" below
```

### Example

A filled-in tree, used as the running example for the rest of this doc —
a default host serving most of the tree directly, a second host serving a
couple of subtrees of its own, and a third host that only ever
peer-mounts. It shows both `cache-dir` patterns from spec.md §1:
`media-prod` points its `cache-dir` straight at a plain path under the
fixed `/srv/stortree` root (the host's own local tree, no separate mount
needed), while `storage-node-bravo` instead points its `cache-dir` at
`.bravo-cache` below — a real `some-remote`-backed mount (a separate disk
on `storage-node-bravo`'s local network), so the vfs cache lives off-host
rather than competing for space on the box itself. `.bravo-cache` and
`.gcs-cache` are their own top-level subtrees, siblings of `tree` rather
than nested inside it — nesting a VFS-cache backing store inside another
host's own remote-backed mount is exactly the kind of collision
top-level subtrees exist to avoid (see "Top-level subtrees" below), and
each sets `peer-defaults.rclone: false` since nothing but its own
owning host ever needs it mounted (or, for `.gcs-cache`, created at all)
anywhere else. `home` shows both halves of the `userdir-groups` key
("`userdir-groups`" below): the household whose members get a home
directory on every host regardless of what is granted inside it, and the
one group `storage-node-bravo` alone adds on top of that list:

```yaml
tree:
  host: storage-node-alpha
  rclone.remote: storagebox:/

  requires:
    - .bravo-cache

  peer-defaults:
    rclone.args:
      vfs-cache-mode: full
      vfs-cache-max-age: 100h
      dir-cache-time: 5m

  peers:
    some-storage-gadget:
      rclone.args:
        vfs-cache-max-size: 20G
        cache-dir: /mnt/some-volume/.rclone-cache
    storage-node-bravo:
      rclone.args:
        vfs-cache-max-size: 5G
        cache-dir: /srv/stortree/.bravo-cache

  subdirs:
    backups: {}
    home:
      samba:
      userdir-groups:
        - "Whitfield Household"
      peers:
        storage-node-bravo:
          userdir-groups:
            - "Bravo Operators"
      user-subdirs:
        whitfield-media:
          access:
            group: "Whitfield Family & Friends"
            permissions: rx
          host: storage-node-bravo
          rclone.remote: some-remote:/media
        sys-configs:
          access.owner: jd
        mw-fam:
          access.group: "Michael Whitfield Family"
          host: storage-node-bravo
          rclone.remote: some-remote:/fam
        media-prod:
          access.group: Media Production
          rclone:
            remote: some-gcs-bucket:/
            args:
              vfs-cache-mode: full
              vfs-cache-max-size: 5G
              vfs-cache-max-age: 100h
              dir-cache-time: 5m
              cache-dir: /srv/stortree/.gcs-cache/media-prod

.bravo-cache:
  host: storage-node-bravo
  rclone.remote: some-remote:/.stortree-cache
  peer-defaults:
    rclone: false

.gcs-cache:
  host: storage-node-alpha
  peer-defaults:
    rclone: false
```

`storage-node-alpha` is `tree`'s own host: it owns everything under
`tree`'s `subdirs` that doesn't override `host`, and — since `tree` sets
its own `host`+`rclone.remote` — self-mounts `tree` itself too (see
"Node inheritance" below; every top-level subtree with both is an
ordinary mountable node for its own host, not special-cased). Unlike
`host`, `rclone` never inherits, so a node under `storage-node-alpha`
that sets neither its own `rclone` nor a different `host` — `backups`,
`sys-configs` — resolves with no remote at all: each is just a plain
directory that has to exist under `storage-node-alpha`'s own local tree
(`/srv/stortree/tree/backups`, `/srv/stortree/tree/home/<user>/
sys-configs`), not a separate rclone mount. This is purely a resolution
default — it does not make `storage-node-alpha` special at runtime
(there is no "root host" in the Ansible design; see [spec.md](spec.md)).
Any host in `config.yml` is applied to the same way, from the same
control node, over the same `ansible-playbook` run. `storage-node-bravo`
owns three subtrees of its own (`.bravo-cache`, `whitfield-media`,
`mw-fam`), each setting its own `rclone.remote` explicitly, pointed at a
different remote (`some-remote`) than the one `storage-node-alpha`
mounts.

`.bravo-cache` is itself a resolved top-level subtree, like `tree` —
server-owned by `storage-node-bravo`, mounted from `some-remote` (a
device on that host's own local network). Its only purpose is to back
the `cache-dir` that `peers.storage-node-bravo.rclone.args` points at
above: `storage-node-bravo` caches the files `storagebox` serves (as
`tree`'s own remote, via `storage-node-alpha`) onto that local-network
disk rather than its own — a real, off-host mount, needed because
`storage-node-bravo` is only ever reading `tree` as a peer, never as its
owner. `.gcs-cache` is a top-level subtree too, but a much plainer one:
no `rclone` at all, so it resolves to nothing more than an ordinary
local directory (docs/spec.md "Node inheritance"). `media-prod`'s own
mounting host is `storage-node-alpha` itself — no peer-sourcing
indirection to cache around the way there is for `storage-node-bravo`'s
read of `tree` — so its VFS cache can just live directly on `alpha`'s
own disk, no real mount of any kind needed to back it, `.gcs-cache`
included. It's still its own top-level subtree, a sibling of `tree`
rather than nested inside it, so `stortree_mounts` (spec.md §2) never
has to order one against the other; nesting it inside `tree` instead
would put `media-prod`'s cache files behind `tree`'s own remote-backed
mount — the exact same bucket `media-prod` mounts as its primary
content, since `media-prod` is nested there — replacing a cache with a
second, redundant, self-referential mount of identical remote data.

`some-storage-gadget` owns no subtree at all — it only appears under
`tree`'s `peers:` — but because `home` carries a `samba:` block, it
still ends up exporting a `home` Samba share of its own: it peer-sources
every piece of `home` it doesn't own (which, since it owns none of
`home`, is all of it) — `sys-configs` and `media-prod` from
`storage-node-alpha`, `whitfield-media` and `mw-fam` from
`storage-node-bravo` — the same way `storage-node-alpha` peer-sources
`storage-node-bravo`'s pieces for its own copy of the share. See "Samba
sharing is universal" below. The same would hold for a fourth host with
no mention in `config.yml` at all, present only in the Ansible inventory
— see "Every inventory host participates" below.

Every name here — hosts, groups, the `jd` user — is fictional; §§ below
reference this same example throughout, all names/values used
consistently.

### Top-level subtrees

Every key at the top level of `config.yml` is an independent subtree,
sibling to every other one — none of them nests inside another, even
though the resolved filesystem still puts them all under the one fixed
`/srv/stortree` (spec.md §2). This matters for two things:

- **Mount ordering.** A nested node's mount only ever has to wait for
  its own real ancestor's mount (`requires_slug`, spec.md §2) — a
  top-level subtree has no ancestor at all, so its mount never depends on
  another top-level subtree's mount being up first, and nothing else's
  mount ever depends on it unless something is genuinely nested inside
  it. Two top-level subtrees whose mounts would otherwise race or shadow
  each other (e.g. one host's own VFS-cache mount and the tree it caches
  for) simply can't, structurally, as long as neither is nested inside
  the other.
- **Ownership.** A top-level subtree with its own `host`+`rclone.remote`
  is an ordinary mountable node — its owning host self-mounts it via
  `server_subtrees`, the same as any node anywhere else in the tree (see
  "Node inheritance" below). There's no single implicit "tree root" that
  behaves differently from everything nested inside it.

### Peers

A host that holds a copy of a subtree it doesn't own is a **peer** of the
subtree's owning host, and `peers:`/`peer-defaults:` are where its copy
is configured — mount args, an `access` grant of its own, a `samba:`
block of its own, and extra `userdir-groups` to serve home directories
for (the one key of the four that adds to what the node said rather than
replacing it — see "Adding groups on one host" below).

The name is the mechanism. A non-owning host never mounts the subtree's
`rclone.remote` itself and never holds credentials for it: it makes an
sftp mount of the owning host's own already-mounted copy, over the
host-to-host trust `stortree_peer_trust` provisions (spec.md §1, §7).
That is the same peer relationship a Samba peer dependency uses, and the
synthesized rclone remote is named for it (`peer-<host>-<path>`, see
"Peer section names" below). The word "client" in this repo means an SMB
client connecting to a share — a machine outside the fleet — and never
one of these hosts.

> **Renamed.** These keys were `clients:` and `client-defaults:`. The old
> names still parse as far as an error message: `resolve()` rejects them
> by name and tells you what to rename them to ("Unknown keys are an
> error" below). Nothing about the blocks' contents changed. The
> resolved facts they feed were renamed to match — `client_mounts` is
> now `subtree_mounts` and `client_grants` is now `subtree_grants`,
> named for what the mount is rather than the protocol it uses, since
> `peer_dependencies` already names a different list of peer mounts
> (spec.md §1).

### Per-peer mount opt-out

By default, every top-level subtree is peer-mounted (or, with no
`rclone.remote` of its own, just created as an empty placeholder
directory) onto every inventory host that doesn't own it (see "Every
inventory host participates" below). A subtree that has no business
being visible anywhere but its own owning host — a per-host VFS-cache
backing store, remote-backed like `.bravo-cache` above or genuinely
local like `.gcs-cache` — can opt out with `peer-defaults.rclone:
false` either way: it's not just about skipping a peer mount nobody
needs, a remote-less subtree like `.gcs-cache` would otherwise still get
that pointless empty placeholder directory created on every other host.

```yaml
<name>:
  host: <hostname>
  rclone.remote: <remote-spec>
  peer-defaults:
    rclone: false             # no non-owning host gets a mount of this by default
  peers:
    <hostname>:
      rclone: false            # or true / {args: {...}} — always wins over peer-defaults
```

`peers.<hostname>.rclone` always wins over `peer-defaults.rclone`
when both are set for the same host: with `peer-defaults.rclone:
false`, a `peers.<hostname>.rclone` entry that's truthy (`true`, or a
dict — with or without `.args`) becomes an *allow-list* — only hosts
listed that way get a mount, everyone else gets none. With
`peer-defaults.rclone` left at its default (unset, i.e. enabled), a
`peers.<hostname>.rclone: false` entry becomes a *deny-list* instead —
every non-owning host gets a mount except the ones explicitly disabled.
The owning host itself is never affected either way — it always
self-mounts via `server_subtrees`, never through this peer-mount path
at all. This applies uniformly to a top-level subtree's own peer mount
and to any Samba descendant nested under it (see "Samba sharing is
universal" below) — one setting governs everything a non-owning host
would otherwise reach inside that subtree, unless a node further down
says otherwise.

#### At any depth

`peer-defaults`/`peers` are ordinary node keys, valid on a
subdirectory exactly as on a top-level subtree, and a node that writes
neither simply inherits whatever its nearest ancestor set. So a tree
that only ever writes them at the top level behaves exactly as it always
did — every node under a subtree gets that subtree's policy — while a
node that does write one refines, or reverses, that policy for itself
and everything under it, without touching its siblings.

Two axes of precedence, composing the same way at every level:

- **Within one node**, an explicit `peers.<hostname>` beats that same
  node's `peer-defaults` — the rule above, unchanged.
- **Across nodes**, a nearer (deeper) node beats a more distant
  ancestor.

`rclone.args` are the exception to "nearest wins": they *accumulate*
down the chain, every level contributing, with the nearest and most
specific block winning any individual key conflict — so a subdirectory
adds a couple of args to whatever its subtree already set for that
peer rather than replacing the lot.

The most useful shape this buys is the allow-list idiom one level down —
a subtree kept off every peer, with a single node inside it handed to
one host:

```yaml
research:
  host: storage-node-alpha
  rclone.remote: storagebox:/research
  peer-defaults:
    rclone: false             # nothing under `research` reaches any other host...
  subdirs:
    published:
      rclone.remote: storagebox:/research-published
      peers:
        some-storage-gadget:
          rclone: true         # ...except this one node, on this one host
    embargoed:
      rclone.remote: storagebox:/research-embargoed
```

`some-storage-gadget` ends up with one mount, at `research/published`,
peer-sourced from `storage-node-alpha` the same way a top-level subtree
mount is — there's no ancestor mount left for it to reach the node
through, so the node gets one of its own. Every other host gets nothing
of `research` at all, and `research/embargoed` reaches nobody but
`storage-node-alpha`.

Note that "truthy means enabled" holds at every level too: a
`peers.<hostname>.rclone.args` (or `peer-defaults.rclone.args`)
written on a node *under* an opted-out ancestor re-enables that node,
since writing peer-side mount args for it is taken to mean peers are
meant to have it. Write `rclone: false` alongside the args if you meant
to keep it off.

#### What a nested opt-out can't do

An *enabled* ancestor is a single peer-sftp mount of the owning host's
whole copy of that node, and a subdirectory inside it is inside that one
mount. A `rclone: false` written on that subdirectory carves no hole out
of it — there's no such thing as unmounting part of a mount. What the
block governs is the mounts that node gets **in its own right**:

- its own separate peer mount as a Samba descendant its owning host
  differs on (see "Samba sharing is universal" below) — the common case,
  and the one where a nested opt-out really does remove something;
- its own subtree mount as the shallowest enabled node of its branch,
  which only exists when every ancestor is opted out.

Where a node has neither, a block written on it resolves to nothing.
That's host-dependent rather than a config error — the same node is
typically a Samba descendant on one host and plain content inside an
ancestor's mount on another — so nothing rejects it; it's just worth
knowing which of the two you're writing.

A `user-subdirs` descendant is never a subtree mount of the shallowest-
enabled kind: its path is still `%U`-templated and fans out into one
mount per granted user (see "`subdirs` vs `user-subdirs`" below), which
a single subtree mount can't describe. It reaches a non-owning host
through the Samba path instead, where the fan-out is resolved — and a
peer block on it governs that, exactly as on any other Samba
descendant.

### Peer-side access

`peer-defaults`/`peers.<hostname>` also take an `access` object —
the same `{group?, owner?, permissions?}` a node itself takes ("Access"
below) — describing what a **non-owning** host applies to its own copy
of the node:

```yaml
tree:
  host: storage-node-alpha
  rclone.remote: storagebox:/
  subdirs:
    home:
      samba:
      subdirs:
        media-prod:
          access.group: Media Production      # what the owner enforces
          host: storage-node-bravo
          rclone.remote: some-remote:/media
          peer-defaults:
            access:                            # what every other host enforces
              group: Media Production
              permissions: rx
```

It becomes the `-u`/`-g`/`-p` of that host's presentation of the node
(and, for a node that resolves to a plain directory rather than a mount,
its ownership and mode) — real, kernel-enforced access on that host,
applied exactly like the node's own grant is on its owner, via the same
mechanism (spec.md §6).

Three things to know about it:

- **It merges over the node's own grant, key by key** — the same rule
  an ancestor and its descendant follow down the tree ("Access
  inheritance" below), and it composes with it: the node's grant is
  whatever it inherited and wrote, and this is written over that. So
  `peers.<host>.access.owner: some-service-account` hands that host's
  copy to that account and leaves the node's group in place, which is
  usually what naming a host-local principal was for. Between blocks,
  nearest and most specific wins each key it sets:
  `peers.<hostname>` over `peer-defaults`, deeper node over
  shallower. To take something back rather than add to it, write `null`
  for that key, or `access:` with nothing in it to drop the whole grant
  on that host — both different from writing no `access` at all, which
  keeps whatever the copy would otherwise have carried. On a host that
  mounts the node rather than owning it, dropping the grant means
  *presenting* the default there, which takes a bindfs mount of its own;
  stortree plans one ("Access inheritance" below).
- **The owning host is never affected.** `peers`/`peer-defaults`
  only ever describe a host that doesn't own the node, so the node's own
  `access` is what its owner enforces, always.
- **It does change what the Samba share admits, on this host.** A
  share's `valid users`/`write list` name the principals the filesystem
  underneath will actually let in, so they follow the grant each host
  enforces: the node's own on the host that owns it, the peer-side one
  everywhere it was written. With no peer-side grant anywhere in a
  share's subtree — the ordinary case — every host derives the same
  list, exactly as before. This is what makes host-local identity work:
  a service account that exists only on one host (a local Unix user,
  never in LDAP) is named in that host's `valid users` and in no other,
  where it could not be resolved. The share's *path* and the peer mounts
  behind it are unchanged either way.

Write no `access` in a peer block and the copy carries the node's own
grant, which is the whole point: a grant describes the node, so the same
path is the same user's on every host that has it. It reached only the
owning host until stortree presented these mounts at all — the reasoning
was that a peer mount reports whatever the owner applied, which no mount
in this tree has ever done. Layer 1 flattens every uid and gid to the
mount's own, and layer 2 re-presents the result, so ownership does not
survive the sftp boundary in either direction; what a non-owning host
showed instead was the uniform default of whichever presentation covered
it. A Samba share whose `valid users` named the grant's owner was a
share that user could traverse and not read.

### Requires

`requires` names other paths in the tree whose mounts have to be up
before this node's own mount starts:

```yaml
tree:
  host: storage-node-alpha
  rclone.remote: storagebox:/
  requires:
    - .bravo-cache            # a bare string works too, for a single entry
```

Almost every dependency in a stortree tree is implied by its *shape*: a
mount nested inside another mount obviously needs the outer one first,
and that's derived automatically (spec.md §2, `requires_slug`) without
anything in config.yml saying so. `requires` exists for the dependency
nesting can't express — one between **sibling top-level subtrees**,
which by design have no containment relationship at all.

The case it exists for is a VFS cache. In the example above,
`storage-node-bravo` mounts `tree` with `cache-dir:
/srv/stortree/.bravo-cache`, and `.bravo-cache` is its own top-level
subtree with its own remote-backed mount. Nothing about the tree's shape
connects the two, so without a declaration systemd starts both units in
parallel at boot — and if `tree` wins, rclone starts filling a cache
directory on the local disk that is silently shadowed the moment
`.bravo-cache` actually mounts over it. Declaring `requires` on `tree`
makes that ordering real.

This is deliberately *declared* rather than inferred from `cache-dir`
itself. What depends on what is a fact about your fleet; reverse-engineering
it out of a path that happens to appear in an rclone argument would be
guessing, and would silently change behaviour whenever an argument
changed.

Some details:

- **Paths are tree-relative**, written exactly as they appear at the top
  level of config.yml (`.bravo-cache`, `tree/backups`) — the same strings
  used everywhere else in this doc. Leading/trailing `/` are trimmed.
- **It applies wherever the node is mounted**, including hosts that
  peer-mount it rather than own it. That's what makes the example work:
  the cache dependency belongs to *bravo's* mount of `tree`, and bravo is
  a peer of `tree`, not its owner.
- **A target that isn't a mount on this host drops out silently**, with
  no unit dependency rendered. That covers both a target some other host
  owns and this one doesn't mount (in the example, `.bravo-cache` is
  invisible on alpha and on `some-storage-gadget`, so `tree`'s
  declaration resolves to nothing there) and a target with no
  `rclone.remote` at all (a plain local directory like `.gcs-cache` —
  an ordinary directory the same apply creates before any unit starts, so
  there's nothing to order against).
- **The dependency is hard, not just ordered** — `After=` *and*
  `Requires=`. If the required mount is down, the dependent mount does
  not start. That's the point: a `tree` mount that starts anyway would
  write its cache to the wrong place, which is the failure the
  declaration exists to prevent. The trade-off is real and worth knowing:
  if the cache's own remote is unreachable at boot, the host loses the
  subtree that depends on it too, until it comes back.
- **These are config errors**, failing the apply on every host rather
  than only where they'd bite: naming a path no node in the tree defines
  (a typo, or a path renamed out from under the declaration), naming the
  declaring node itself, naming a per-user node (it resolves to one mount
  per granted user, so there's no single mount to depend on), or forming
  a cycle.

### Node inheritance

Every node — top-level or nested under `subdirs`/`user-subdirs` — inherits
`host` from its nearest ancestor unless it overrides it (a top-level node
has no ancestor to inherit from, so it always sets its own `host`
explicitly). `access` inherits the same way, with its own rules for
replacing and dropping an inherited grant — see "Access inheritance"
below. `rclone` — both `remote` and `args` — is different: it
**never** inherits, from an ancestor node or from a top-level one. A
node's `rclone` config is used exactly as set on that node, full stop; a
node with no `rclone.remote` of its own resolves to no remote at all,
regardless of what any ancestor sets — see spec.md §1 for the full
rationale. A node with both `host` and `rclone.remote` set is an ordinary
mountable node, self-mounted by its owning host — true at any depth,
top-level subtrees included (see "Top-level subtrees" above).

A node that resolves with no `rclone.remote` isn't a separate mounted
subtree — there's no remote for it to mount from. What that means
depends on whether it also changes `host`:

- **Same `host` (inherited, not overridden)**: the node is just a plain
  subdirectory that has to exist inside the tree its inherited `host`
  already serves. `backups: {}` and `sys-configs` (`access.owner: jd`) in
  the example above are both exactly this — neither sets its own `rclone`
  or a different `host`, so each resolves to an ordinary, empty directory
  under `storage-node-alpha`'s own local tree (`/srv/stortree/tree/
  backups`, `/srv/stortree/tree/home/<user>/sys-configs`) — nothing is
  mounted at either path. `resolve()`/`stortree_plan_mounts` still track
  the node (for ownership/mode, Samba export, per-user expansion), just
  without an rclone unit backing it.
- **Different `host`, still no `rclone` of its own**: that `host` has to
  keep the directory's data locally — there's no remote configured for it
  to mount from, so whatever ends up there is real local storage on that
  host, not synced from anywhere, and it's that host's own responsibility
  to back it up/preserve it like any other local disk contents.

An empty node (`backups: {}`) is valid either way — it just means "use
the inherited `host`, no remote, no overrides."

#### `rclone.remote` is verbatim

When a node does set its own `rclone.remote`, it's passed to `rclone
mount` exactly as written, unchanged — `resolve()` never appends a node's
position in the tree to it. A bare section name (`storagebox`) mounts
that remote's own root; a `<section>:<path>` value (`some-remote:/media`)
mounts exactly that path on that remote. Two nodes that each explicitly
set the same `rclone.remote` value mount identical remote content, at
whatever two local paths their own tree positions give them; nothing
about being a different node makes the source differ. Every node in the
example above that needs distinct source content sets its own
`rclone.remote` explicitly, path included — since `rclone` never
inherits (above), that's the only way for a node to have one at all.

### `subdirs` vs `user-subdirs`

Same map-of-`name -> node` shape, but they resolve differently:

- `subdirs` entries are literal, single, shared directories — one
  instance on disk, owned/moded once (`.cache`, `frigate`, `backups`).
- `user-subdirs` entries are per-user: the immediate children of a
  `user-subdirs` node are per-user folders, and the nodes listed
  (`whitfield-media`, `sys-configs`, `mw-fam`, `media-prod`) describe the
  substructure repeated inside each of those per-user folders, e.g.
  `home/<username>/whitfield-media`, `home/<username>/sys-configs`.
  That shape is also what makes a `samba:` block on the same node a
  per-user share: the Samba path picks up `%U` precisely when the node
  has `user-subdirs` (see "Samba sharing is universal"), so an SMB
  client lands in its own per-user folder rather than in the directory
  holding everyone's.
  Which users get a folder is answered from two places: the groups the
  node names in `userdir-groups` (below), and the grants on the
  descendants themselves. `access` on each descendant still applies per
  the usual rules, so `sys-configs` (`access.owner: jd`) only shows up
  inside `jd`'s own per-user folder, not everyone else's — an `owner` grant always pins
  a single folder like this, whether or not the node also carries a
  `group`; only a `group`-only grant (`mw-fam`, `media-prod`) expands
  into one folder per member. See "Access" below for how that grant then
  gets enforced, which differs for a descendant with its own
  `rclone.remote` (`media-prod`) versus one without (`sys-configs`).
  For a remote-backed, `group`-only descendant specifically (`mw-fam`,
  `whitfield-media`, `media-prod`), every member's folder is a bind
  mount onto one real, shared mount rather than a separate mount of its
  own —
  every member gets the exact same enforcement either way (one gid, one
  mode), so mounting the same remote path once per member would just be
  N redundant copies of identical content; see spec.md §6 for the
  mechanism. `sys-configs` (`owner`-only) has no such sharing to do — an
  `owner` grant always was, and still is, one folder for one person.

  The per-user folder itself (`home/jd`, not `home/jd/sys-configs`) is
  owned outright by that one real user, not by `stortree` — an ordinary
  home directory, not just a passthrough to whatever's granted beneath
  it: `jd` can create files directly in `home/jd`, not only reach
  `sys-configs`. Every descendant under the same `user-subdirs` node that
  resolves to the same user (e.g. `jd` being both `sys-configs`'s owner
  and a `mw-fam` group member) shares that one container; nothing about
  which descendant triggered it changes who owns it. How that ownership
  actually gets applied depends on what's above the container: a plain
  `chown` for a genuinely local top-level subtree (`host` set, no
  `rclone`), or a **presentation mount** for one nested inside a
  remote-backed subtree like `tree` here, since a single rclone mount
  can't present two different paths under it with two different owners.
  The same mechanism applies to any node with an `access` grant and no
  `rclone.remote` of its own, not only per-user containers. See spec.md
  §6 ("The two layers") for the mechanism, and its own note there
  on what this means for a sibling like `mw-fam`'s bind mount, which
  has to wait for that presentation too.

### `userdir-groups`

A list of group names, on a node, saying whose per-user directories live
under it:

```yaml
home:
  samba:
  userdir-groups:
    - "Michael Whitfield Family & Friends"
  user-subdirs:
    mw-fam:
      access.group: "Michael Whitfield Family"
      rclone.remote: some-remote:/fam
```

Every member of every group named gets a folder — `home/jd`, owned
outright by `jd` and private to them, exactly the container a
`user-subdirs` descendant's grant already implies for its own users
("`subdirs` vs `user-subdirs`" above).

**It adds a source, it doesn't replace one.** The grants underneath
still resolve to folders exactly as they did: `mw-fam` above puts one in
each family member's home whether or not the group is also listed here,
and a descendant with `access.owner: jd` still pins one to `jd` alone. A
user who is named both ways gets the one folder either way would have
made. What the key changes is that the answer no longer has to come from
underneath.

Without it, membership is emergent — a home directory exists because
something happened to be granted inside it, and stops existing when that
grant is commented out. That is backwards for the thing the docs already
describe a per-user folder as: "an ordinary home directory, not just a
passthrough to whatever's granted beneath it". `%U` earns each connecting
user a standing entry in the share's `valid users` ("The share path"
below) precisely so their own folder doesn't depend on any descendant
carrying a grant; this is the config half of the same statement.

**Groups only.** An individual is named with `access.owner` on something
beneath, which already pins exactly one folder to exactly one person. A
second way to name one person would only be a second place to look when
asking who has a folder here. Group membership is the part that lives in
LDAP rather than in this file (spec.md §5), which is what the key is for
— and why a group with no members yet, or none this host can resolve,
makes no directories and is not an error.

**It declares the per-user level by itself.** `user-subdirs` is optional
beside it, and leaving it out is a real configuration rather than an
incomplete one: per-user folders with no shared substructure inside them.
Home directories and nothing else. The share path is derived from either
key's presence, so such a node is still exported at `<node>/%U` ("The
share path" below).

Presence, not contents, exactly as `user-subdirs` is read: `userdir-groups:
[]` and a bare `userdir-groups:` say nobody is in the list yet, never
that the node stopped being per-user.

#### Adding groups on one host

A `peer-defaults`/`peers.<hostname>` block may carry a `userdir-groups`
of its own, and it is the one peer key that **adds** to what the node
said rather than replacing it:

```yaml
home:
  userdir-groups: ["Michael Whitfield Family"]
  peers:
    storage-node-bravo:
      userdir-groups: ["Michael Whitfield Family & Friends"]   # bravo serves both
```

`rclone`, `access` and `samba` in a peer block each describe one host's
*copy* of a node — how it mounts it, what it enforces on it, whether it
exports it — and a copy is one thing, so the nearest and most specific
block wins outright ("Per-peer mount opt-out", "Peer-side access",
"Per-host shares"). This key describes something else: who the node is
for. A host that serves one more department's home directories does not
thereby stop serving everyone else's, and reading it the way the other
three are read would mean it could only ever do both by restating the
owner's whole list — two lines meant to agree, drifting apart later. So
the node's own list is the floor everywhere, `peer-defaults` adds to it
on every non-owning host, and `peers.<hostname>` adds to that.

Three limits, all of them the same rules the other peer keys follow:

- **The owning host reads no peer block.** Those blocks describe a host
  holding a copy; the owner holds the original.
- **It doesn't inherit.** Like `samba`, it marks the one node it is
  written on and says nothing about that node's descendants — which have
  their own per-user level, or none.
- **It can't create a per-user level for one host.** A peer block's
  `userdir-groups` is an error on a node that has neither `user-subdirs`
  nor a `userdir-groups` of its own. The share path is derived from the
  node's shape and is the same on every host, so a node that were
  per-user on one host and not on another would answer to one share name
  while serving `<node>/%U` on the host that added the groups and
  `<node>` — every user's folder, to every user — on all the rest. Give
  the node its own `userdir-groups` (an empty list is enough) and add to
  it in the peer block.

A host only creates these directories where it actually holds the path:
it mounts the node or an ancestor of it, or it owns a node somewhere
beneath it. A `peers.<h>.userdir-groups` written on a subtree that same
host is opted out of ("Per-peer mount opt-out") would otherwise leave it
a stray local tree of empty home directories backing nothing.

### Access

`access` is always a single object — `group`, `owner`, and `permissions`
all optional, either written out or via dotted shorthand:

```yaml
# written out
access:
  group: "Michael Whitfield Family"
  owner: jd
  permissions: rwx

# dotted shorthand
access.group: "Michael Whitfield Family"
access.owner: jd
```

Both `group` and `owner` can be set at once — the node is then pinned to
that one `owner`'s folder (see "`subdirs` vs `user-subdirs`" above), with
`group` granting *shared* access to that same folder.

`permissions` is an `rwx`-style string: any of `r`, `w` and `x`, with `-`
for a bit not granted, so `rwx`, `r-x` and `rx` are all read the same
way. It is **not** a numeric mode, and writing one is an error rather
than a surprise — `permissions: 750` is an integer by the time YAML is
done with it, and `0750` a different integer again. A typo in the
letters is an error too (`rwz`), since the alternative is a grant
quietly missing a bit.

Written as one string it is the level for the whole grant. Written as a
mapping it is one level per Unix class:

```yaml
access:
  owner: jd
  group: "Michael Whitfield Family"
  permissions:
    owner: rwx        # jd writes
    group: r-x        # the family reads
    other: "---"      # and nobody else gets even the traversal bit
```

The classes are the three a Unix mode has, named the way `access` already
names the first two. Each one you write is enforced exactly; each one you
leave out keeps the default it would have had (below) — including
`other`'s traversal bit, so a mapping about the group alone does not
quietly close the path to a grant nested deeper. That is the difference
between the two forms: a string settles the whole mode, a mapping settles
the classes it mentions.

A node's `access` is what its *owning* host enforces. A host that only
holds a copy of the node — a subtree mount, or a peer-sourced Samba
descendant — enforces the same grant by default, and can be given a
different one with an `access` inside `peer-defaults`/`peers`; see
"Peer-side access" above.

`group`/`owner` names are resolved against POSIX identities provided by
SSSD (backed by the configured LDAP server — see `ldap.yml` below).
Everything above is applied as plain Unix ownership + mode (spec.md §6),
which is exactly why it stops where it does: **one** owner and **one**
group. Per-class levels need no POSIX ACL — one owner and one group at
two different levels is what a mode has always been able to say — but a
second group at a third level is not, and there's no way to write one.
A remote-backed node (directly, or peer-sourced from whichever host owns
it) is always an rclone FUSE mount, and rclone's FUSE mount never
implements `setxattr`: it cannot carry a POSIX ACL on any host, full
stop. The old list-of-grants form (letting a node like `whitfield-media`
grant two different groups two different levels) could describe
configurations that were never enforceable for such a node, so the
schema no longer lets you write one. A plain local node (`sys-configs`
here) gets the same treatment for consistency, not necessity — it could
carry a real POSIX ACL, but there's no reason for its enforcement to
work differently from a remote-backed sibling's.

With neither `group` nor `owner` granted, a node gets the plain default:
owned by the `stortree` service account with full control, group
`stortree` with read+traverse, and a bare execute (traversal-only, no
read/write) bit for everyone else — needed so a real grant nested several
levels down (e.g. a `user-subdirs` descendant's own `access.group`) stays
reachable through this node, since the connecting user is essentially
never a member of the local `stortree` group. Granting an `owner` makes
it private to that one user instead (no group fallback); granting only a
`group` leaves `stortree` itself with full control and gives the group
`permissions`. That same traversal-only bit for everyone else is also
added whenever `permissions` is left at its default rather than written
out explicitly in config.yml — an explicit `permissions:` *string* is
enforced exactly as written instead, other bits included, while the
per-class mapping settles `other` only if it names it. See spec.md §6
for exactly how this becomes real, symmetric enforcement over both Samba
and SSH alike, for every node with any `access` at all.

#### Access inheritance

A node with no `access` key of its own inherits its nearest ancestor's
grant, exactly as it inherits `host`. So `access.group` written on a
subtree's top node grants that group the subtree, not one directory:

```yaml
project-data:
  host: storage-node-alpha
  access.group: Media Production  # and everything below is this group's
  subdirs:
    footage:                      # inherits it
      subdirs:
        raw: {}                   # so does this
    notes:
      access.owner: jd            # jd's, still Media Production's group
    readonly:
      access.permissions: rx      # same group, narrower level
    scratch:
      access:                     # drops it — back to the plain default
```

Four rules, and a peer block's `access` follows all four over the
result ("Peer-side access" above):

- **No `access` key at all: inherit.** The whole grant, from the nearest
  ancestor that has one. A node with no granted ancestor gets the plain
  default, exactly as it always did.
- **A key of its own: merge over it.** Each of `group`, `owner` and
  `permissions` comes from the nearest ancestor that set it. `notes`
  above names an owner and keeps `Media Production` alongside it;
  `readonly` narrows that group's level to `rx` and keeps the group. A
  node never has to restate the keys it isn't changing — restating them
  is how two lines that were meant to agree drift apart later. A
  per-class `permissions` mapping merges one level further down, class
  by class, so a descendant can restate what the group gets and leave
  the owner's level alone.
- **`null` for a key: take that key back.** `access.owner: null` under a
  granted ancestor leaves the group and drops the owner. With every key
  merging, this is the only way to remove one.
- **An empty `access:`: drop the whole grant.** The same thing said
  about every key at once, and the shorthand you'll actually write.

A `permissions` with nobody to apply it to is refused: on its own a
level grants nothing, and resolves to the same empty grant as the
`access:` that drops one — the opposite of what writing a level out
means. So `access.permissions: rx` is right under a granted ancestor and
an error without one, and nulling a principal while leaving its level
behind (`access: {group: null}` under `{group: G, permissions: rx}`) is
the same error written across two nodes.

Inheriting is what makes a grant describe a subtree. Without it,
`access.group` on a subtree's top node let that group traverse the
directory and read nothing inside it (every node below stayed at the
ungranted default, reachable only through the public-execute bit under
"Access" above), which is essentially never what writing the group
meant. It crosses a `user-subdirs` boundary like any other, so a
per-user node under a granted ancestor now resolves against that grant
instead of resolving to nobody.

What it costs depends on which host you ask. On the host that owns the
subtree these are real directories, so each inherited grant is one more
`chown`. On a host that mounts the subtree, ownership is whatever the
bindfs presentation above the path shows — and bindfs shows one owner,
group and mode over its whole subtree — so a node that inherited its
grant unchanged needs nothing of its own: one presentation covers every
node beneath it, however deep. Only the nodes that say something
*different* from the mount above them get a presentation of their own —
`notes`, `readonly` and `scratch` in the example, and `scratch`
precisely because "the plain default" is something to present, not the
absence of something. See spec.md §2 for the two layers this is talking about.

### Every inventory host participates

`peers:` is only ever for a per-host override, never a prerequisite for
being a peer. Every host in the Ansible inventory
(`inventory/hosts.yml`, spec.md "Config layout") that isn't itself a
given top-level subtree's resolved `host` gets a subtree mount of that
subtree, whether or not it has a `peers:` entry there and whether or
not it's named anywhere in `config.yml` at all — unless that subtree's
own `peer-defaults`/`peers.<hostname>` opts it out (see "Per-peer
mount opt-out" above). With a `peers:` entry, `peers.<hostname>.
rclone.args` merges over `peer-defaults`; without one, it just gets
`peer-defaults` verbatim. That mount is **not** a direct mount of the
subtree's own `rclone.remote` — it's a peer-sftp mount of the resolved
owner's (`host:`'s) own copy of that subtree, provisioned by
`stortree_peer_trust` the same way as any other peer dependency (spec.md
§1/§7); a peer never holds credentials for the subtree's remote itself.
This applies independently to every top-level subtree — a host can own
one, peer-mount another, and be opted out of a third, all at once —
and, where a subtree's own nodes carry their own `peer-defaults`/
`peers`, independently within a subtree too ("At any depth" above).

The same goes for "Samba sharing is universal" below and for cross-host
peer dependencies (spec.md §1/§7): both apply to every inventory host
equally, not only ones named in `config.yml`. Naming a host in
`config.yml` — as a node's `host:`, or under `peers:` — only ever
*adds* something on top of what it already gets by being in the
inventory (subtree ownership, or a per-host `rclone.args` override); it's
never required to get the baseline. Adding a host to
`inventory/hosts.yml` and nowhere else is enough for it to start serving
every `samba:`-configured share, with peer trust provisioned for it the
same as any other host (spec.md §7) — see spec.md §8's "apply to one
host" for the operator-facing side of this.

### Samba sharing is universal

A `samba:` block marks a node for export as an SMB share. It's the key's
*presence* that marks it, not what's under it: `samba:` written bare,
`samba: {}` and `samba: true` all mean "share this with the defaults".
Only an explicit `samba: false` opts a node back out. That export is
not limited to the node's own resolved `host` (or that host's usual peer
dependencies, spec.md §1) — **every host in the Ansible inventory**
exposes the share, including a host that owns no subtree of its own and
only ever appears under `peers:` (`some-storage-gadget` above), and even
a host with no mention in `config.yml` whatsoever (see "Every inventory
host participates" above). A host that already owns some or all of the
node's data serves it from there; whatever it doesn't own, it
peer-sources from the actual owning host — the same peer-trust mechanism
spec.md §1/§7 describes for a Samba node's own descendants, just not
restricted to hosts that already serve some other part of the tree.
There's no "designated Samba host": if a node has a `samba:` block, every
inventory host — server, peer-only, or entirely unnamed in
`config.yml` — ends up serving it.

Two things narrow that, both opt-in and neither changing the default:
`stortree_samba_hosts` takes a host out of exporting anything ("What
universality costs" below), and a `samba:` written inside a
`peer-defaults`/`peers.<hostname>` block scopes one node's share to
the hosts it names ("Per-host shares" below). A config that writes
neither behaves exactly as this section describes.

#### What universality costs, and how to opt a host out

Universality is the default and stays it, but it is not free, and the
cost is paid per exporting host. A host that exports a share whose
content it doesn't own peer-mounts that content over sftp from the
owning host — so it runs its own rclone process and its own VFS cache of
the same bytes, and a cold read traverses SMB → sftp → the owner's
rclone → the third-party remote. Across N exporting hosts that is N
independent caches of identical content. (This is the same duplication
interpretation call #2 in [plan.md](plan.md) eliminated *within* a host,
where one shared mount plus bind mounts replaced one full mount per
group member. Across hosts the answer can't be a bind mount, so it has
to be a choice instead.)

`stortree_samba_hosts` (roles/stortree_facts/defaults/main.yml) is that
choice: a fleet-level list, defaulting to every host, of the hosts that
actually export shares. Narrow it in inventory or `group_vars` for a
host that has no business serving SMB — one that is only a peer of the
tree, or one kept in the fleet purely to own a subtree others consume:

```yaml
# group_vars/all.yml
stortree_samba_hosts:
  - storage-node-alpha
  - storage-node-bravo
```

An excluded host resolves no `samba_shares` **and** none of the peer
dependencies that exist only to back them — the mount, not the smb.conf
stanza, is what universality actually costs, so dropping only the stanza
would save nothing. Its own subtree mounts are untouched: opting out of
*exporting* the tree says nothing about wanting it locally. On the
serving side, hosts that own that content stop provisioning SSH trust
for mounts the excluded host will never make, because every host reads
the same list and reaches the same conclusion.

It is a fleet-level list rather than a per-host boolean deliberately.
`resolve()` is a pure per-host function that has to reach the same
conclusion about *other* hosts as they reach about themselves; a
per-host variable would need `hostvars` cross-referencing to do that,
which spec.md §1 rules out. A host removed from the list after having
served shares gets its `smbd` stopped and disabled on the next apply
(the package and `/etc/samba/smb.conf` are left alone) — otherwise it
would keep exporting the last rendered config, whose share paths point
at peer mounts that no longer resolve.

#### Per-host shares

`stortree_samba_hosts` above decides which hosts export *anything*. A
`samba:` written inside a `peer-defaults`/`peers.<hostname>` block
decides which hosts export *this node* — the one way a share exists on
some hosts and not others:

```yaml
tree:
  host: storage-node-alpha
  rclone.remote: storagebox:/
  subdirs:
    spool:
      peers.storage-node-bravo:
        samba:
          name: spool
          hidden: true
        access.owner: nvr        # a local Unix user on bravo, not in LDAP
```

`storage-node-alpha` owns `tree/spool` and exports no share for it.
`storage-node-bravo` exports `spool`, hidden, peer-sourcing the content
from alpha the same way it would for any other Samba descendant. No
other host exports it at all.

The reason this exists is identity, not tidiness. A share is only usable
where the principals it admits can be resolved, and identity is not
always fleet-wide: an appliance's service account — a camera recorder's,
a backup agent's — often exists as a local Unix user on the one host
that appliance talks to, deliberately never in LDAP. Exported
everywhere, that node's share would name a principal most hosts cannot
resolve; exported nowhere, the host that *can* resolve it has no share
to offer. Per-host is the only shape that is true.

Four things to know about it:

- **It doesn't inherit.** Unlike `rclone` and `access` in the same
  blocks, a `samba:` marks the one node it's written on and says nothing
  about that node's descendants — exactly as a node's own `samba:`
  behaves. Cascading would export every descendant under a single name.
- **The owning host never reads it.** `peers`/`peer-defaults`
  describe a host holding a *copy*; the owner holds the original. A node
  is exported on its owner if, and only if, it carries its own `samba:`.
- **It replaces the node's own export on that host**, rather than
  merging with it. On a node with no `samba:` it adds a share there; on
  one that has a `samba:` it renames or hides that host's copy, and
  `samba: false` withdraws it there while leaving every other host's
  intact. Within a node `peers.<hostname>` beats `peer-defaults`,
  the same precedence `rclone` and `access` follow.
- **`valid users` follows the grant that host enforces** — see
  "Peer-side access" above. That is what lets the `nvr` grant reach
  bravo's share and no other host's.

Nothing else changes: the share path is still derived from the node
("The share path" below), and the peer mounts behind it are provisioned
exactly as for a universal share, on the hosts that actually export it.

#### The share path

Where a share points is derived from the node, not written:

- a node that declares a per-user level — a `user-subdirs` key, a
  `userdir-groups` key, or both — is exported at `<node>/%U`; Samba
  expands `%U` to the connecting username, so each user lands in their
  own folder;
- a node with neither is exported at `<node>` itself.

There is no key for this. The node's shape already answers the question:
either key means the node's immediate children *are* per-user folders,
so a share of that node that didn't descend into one would be exposing
every user's folder to every other user — over SMB, with
nothing at apply time saying so. That was previously reachable two ways,
by omitting the old `samba.subpath` key or by misspelling it, and is now
unreachable.

It's the key's *presence* that decides, exactly as with `samba:` itself.
`user-subdirs: {}` and a bare `user-subdirs:` declare no substructure
yet, but they still say the node has a per-user level — reading them as
"not per-user" would mean emptying a node's `user-subdirs` silently
widens its share from one user's own folder to the directory holding
everyone's. An empty or bare `userdir-groups:` is read the same way, and
a node that writes only `userdir-groups` has no shared substructure to
declare at all — its per-user folders are plain home directories
("`userdir-groups`" above).

`%U` also earns each connecting user a standing entry in the share's
`valid users`, so their access to their own folder doesn't depend on any
particular descendant carrying an `access` grant.

A config that still writes `samba.subpath` fails with a message saying
so — it was a real key once, so it's rejected specifically rather than
as a typo. Delete the line; the derived value is the one it was almost
certainly setting.

#### Share names

The share's name — its `smb.conf` section header, and what an SMB client
mounts as `//<host>/<name>` — is derived from the node's path by default,
with every character outside `A-Za-z0-9_-` folded to `_`: `tree/home` is
exported as `tree_home`. `samba.name` overrides that, on the node
carrying the `samba:` block:

```yaml
tree:
  subdirs:
    home:
      samba:
        name: home
```

An explicit name is held to that same alphabet rather than sanitized
silently — a name is what operators type into a mount command, so a
`samba.name` that wouldn't survive the fold is a mistake worth reporting
rather than quietly rewriting — and the three names `smb.conf` gives its
own meaning (`global`, `homes`, `printers`) are rejected outright: a
share named `global` would merge into the generated `[global]` block and
rewrite fleet-wide settings instead of adding a share.

Names have to be unique, however they were arrived at: two nodes landing
on one name — two `samba.name`s written the same, or two paths folding
together (`tree/a b` and `tree/a_b`) — fail the run, because `smb.conf`
would otherwise keep the first stanza and drop the second, leaving part
of the tree silently unreachable over SMB. The name changes nothing
else: the share's path, its `valid users`/`write list`, and the peer
mounts behind it are all still derived from the node's real path.

Uniqueness is checked per host, since a host's own `smb.conf` is the
file that either can or can't hold both stanzas. Two nodes whose own
`samba:` blocks collide are caught tree-wide, with no host named — they
collide everywhere. A collision that only a per-host share creates ("Per-host
shares" below) names the host whose file couldn't have held both. Either
way it fails the run on *every* host, not only the one it would bite, so
an apply limited to one host still reports it.

### Unknown keys are an error

Every key in a node is either one this schema defines or a mistake, and
`resolve()` treats it as the latter: an unrecognized key anywhere in the
tree — at the node level, or inside `rclone:`, `access:`, `samba:`,
`peer-defaults:` or a `peers:` entry (including the `rclone:`/
`access:`/`samba:` objects nested in those) — fails the run, naming the node it's
on and, where there's a near match, the key it's probably meant to be.

This matters more here than the usual argument for strictness, because
the schema has no key whose absence is loud. A misspelled
`rclone.remote` leaves the node a plain directory and the share on top
of it serving an empty path; a misspelled `peer-defaults` re-enables a
subtree that was meant to stay off every other host, and provisions the
SSH trust to go with it; a misspelled `subdirs` drops a whole subtree; a
misspelled `access.group` drops the grant and leaves the path at its
permissive default. All four resolve to something plausible, and none of
them announce themselves at apply time.

A rejected key is sometimes a setting that is real but belongs
elsewhere. This file describes the *tree* — what exists, who owns it,
who may read it — and `resolve()` is a pure per-host function whose
conclusions about a host must match the ones that host reaches about
itself. Operational policy that no other host depends on is not that,
and lives in ordinary Ansible inventory instead: `stortree_samba_hosts`
(above), the Samba `[global]` overrides in
[runbook.md](runbook.md#changing-sambas-global-settings-eg-workgroup),
and whether a host publishes rclone metrics
([runbook.md](runbook.md) "Publishing rclone metrics"). See
`inventory/group_vars/all.yml.example`.

One consequence worth knowing: dotted shorthand splits on the *last* dot
only (see below), so a three-segment key like
`rclone.args.vfs-cache-mode:` expands to a key literally named
`rclone.args`, which is not in the schema and is therefore rejected.
Write it as `rclone.args: {vfs-cache-mode: ...}` or as a nested
`rclone:` block.

### A dotted-path map key

`.cache.subdirs:` under any `subdirs:` map is the same dotted shorthand
as elsewhere: a subdir named `.cache`, containing a nested `subdirs:` map
of its own. Not a subdir literally named `.cache.subdirs`. This splits on
the *last* dot only, so a key with more than one dot still expands
correctly (`.cache.subdirs` → `.cache` + `subdirs`, not shredded on every
`.`).

A bare, dot-prefixed key with no further dots after it — `.bravo-cache`
in the worked example above, used at the top level with no `.subdirs`/
etc suffix — is different: it's a single literal key (this codebase's
hidden-subtree naming convention, matching `.cache`'s own leading dot),
not a two-segment shorthand with an empty first segment. It's left
untouched rather than being expanded into `{"": {"bravo-cache": {...}}}`.

## Names and identity

Three things in a running fleet are named after a node's path, and all
three use a *different* scheme. They look similar enough to be mistaken
for one another when read side by side in a rendered file, so this is
what each one is and why it isn't the others.

| Name | Looks like | Escaping | On collision |
| --- | --- | --- | --- |
| systemd unit slug | `stortree-mount@tree-home-jd.service` | `\xHH` per segment | rejected |
| rclone peer section | `[peer-storage-node-alpha-tree-home]` | none | rejected across hosts |
| Samba share name | `[tree_home]` | fold to `_` | rejected |

**systemd unit slugs** flatten `tree/home/jd` to `tree-home-jd` and are
what `stortree-remote@`, `stortree-mount@` and `stortree-bind@` are
instantiated with. Because a path segment may itself contain `-`, each
segment is escaped on its own before the `-` join, in the same `\xHH`
convention `systemd-escape` uses: a directory literally named
`backups-mirror` becomes `backups\x2dmirror`, so it can't collide with
nested `backups/mirror`. That makes the scheme injective — two paths
cannot produce one unit name — and it is why unit names in
`/etc/systemd/system` sometimes read oddly. Two mounts landing on one
slug is still checked for and still fails the run, as a backstop.

**rclone peer section names** flatten the same way and are deliberately
*not* escaped, because they can't be: an rclone remote name may hold
only letters, digits, `_`, `-`, `.` and space, so `\xHH` isn't
available, and every readable alternative mangles ordinary names. The
name stays legible instead — it's a string you read straight out of a
rendered `rclone.conf` when a peer mount misbehaves — and the residual
ambiguity is detected rather than encoded away. See "Peer section names"
for the collision rule, which is narrower than the other two: only a
collision between *different* owning hosts is an error.

**Samba share names** don't escape either; they fold every character
outside `A-Za-z0-9_-` to `_`, because that's the alphabet an operator
types into a mount command. Unlike the other two, this one is
overridable — `samba.name` on the node — and unlike the other two the
name is a piece of config rather than purely derived. See "Share names".

The common thread: a derived name that can't be made both injective and
readable is made readable, and the collision is reported with both
paths named rather than resolved silently. What differs is only how much
room each target format leaves — systemd allows escapes, so slugs use
them; rclone and Samba don't, so those two detect instead.

## ldap.yml

LDAP server connection + mapping — not tied to any particular LDAP
product. This file is encrypted at rest with `ansible-vault` on the
control node (`ansible-vault encrypt stortree/ldap.yml`); the plaintext
below is what it decrypts to at playbook run time.

```yaml
server:
  url: ldaps://ldap.example.internal:636
  base_dn: "dc=example,dc=internal"
  bind_dn: "cn=stortree,ou=service-accounts,dc=example,dc=internal"
  bind_password: <plaintext>

# how SSSD should map directory users/groups to POSIX identity.
# NEEDS VERIFYING against your server's config (see spec.md open questions):
# whether uidNumber/gidNumber are exposed, or need an id-mapping scheme instead.
posix:
  uid_attr: uidNumber
  gid_attr: gidNumber

# Optional escape hatch for SSSD directives stortree doesn't model
# itself (TLS cert validation, a separate group search base, sudo
# provider, adding a service like ssh, etc). Keyed by the sssd.conf
# section it targets. "sssd" and "domain" are special-cased -- merged
# over stortree's own defaults for [sssd] and [domain/stortree]
# respectively, so a key here overrides the built-in value of the
# same name instead of producing a duplicate line. Any other key
# becomes a brand-new "[section]" block, appended verbatim with no
# stortree defaults to merge over. Omit entirely, or any part of it,
# if not needed.
extra:
  sssd:
    services: "nss, pam, ssh"
  domain:
    ldap_tls_reqcert: demand
  ssh:
    ssh_hash_known_hosts: "false"
```

`extra` is the one part of this file not otherwise validated or
interpreted by stortree. `sssd` and `domain` pairs are merged over that
section's built-in defaults (`services`/`domains` for `sssd`;
`id_provider`, `cache_credentials`, `enumerate`, etc for `domain`) before
rendering, so a key already set by stortree is replaced, and any other
key is added, one line each, in map order. Every other top-level key
under `extra` (`ssh` above) is rendered as its own new section, in the
order given, at the end of the file -- there's no bound on what section
names are accepted, since stortree has no notion of which ones SSSD
recognizes.

Every value under `extra` is rendered as-is, so quote anything that
looks like a boolean (`"false"`, not `false`) -- unquoted, YAML parses it
as a boolean and Jinja renders it capitalized (`True`/`False`), which
SSSD's ini parser rejects.

## rclone.conf

No custom schema here — this is rclone's own native config file (INI
format, `rclone config` manages it interactively if you want). `config.yml`'s
`rclone.remote` fields reference section names in it directly, so there's
nothing to translate between the two. Lives on the control node as the
master copy with every remote's credentials, encrypted at rest with
`ansible-vault` (`ansible-vault encrypt stortree/rclone.conf`) the same
way as `ldap.yml`. A host only ever receives the filtered sections it's
resolved to need (see spec.md §3), never the whole file. "Resolved to
need" means the remotes that host *mounts with itself*: the ones behind
subtrees it owns, plus any a `peers:` block hands it directly. A remote
behind a subtree it doesn't own never reaches it, even though it exports
that subtree over Samba — it peer-sources the owning host instead.

### Peer section names

Alongside the filtered real sections, each host's rendered `rclone.conf`
gets one synthesized `sftp` section per peer dependency, named
`peer-<owning host>-<path with "/" replaced by "-">` — e.g.
`[peer-storage-node-alpha-tree-home-jd-sys-configs]`. Nothing in
`config.yml` sets these names; they're derived, and they're what a mount
unit's `ExecStart` references, so they're worth recognizing when reading
a rendered file.

Because that flattening is lossy, two entries can in principle land on
one name — a hostname containing `-`, or a path segment containing one,
can reconstruct another entry's name. Where both belong to the *same*
owning host that's harmless (the section carries only that host's
address and key, and each mount's real path travels in its own
`remote:path` reference). Where they belong to *different* hosts it
would silently point one mount at the wrong machine, so rendering the
host's `rclone.conf` fails outright and names both paths — rename one of
them, the same remedy as a duplicate `samba.name` (see "Share names").

```ini
[storagebox]
type = sftp
host = <plaintext>
user = <plaintext>
pass = <plaintext>

[some-remote]
type = sftp
host = <plaintext>
user = <plaintext>
pass = <plaintext>

[some-gcs-bucket]
type = google cloud storage
service_account_credentials = <plaintext>
```

## sshd_config (optional)

Freeform SSH daemon config — not a stortree schema, just raw
`sshd_config` directives. If present, the `stortree_sshd` role pushes it
verbatim to every host in the normal playbook run (see spec.md §6), where
it's installed as a drop-in include (e.g.
`/etc/ssh/sshd_config.d/stortree.conf`, included by the system's own
`sshd_config`) and `sshd` is reloaded. Omit the file entirely and nothing
SSH-related changes from a stock install.

This is where an operator can hand-add access restrictions — for example,
scoping what a `pam_smbpass`-triggering SSH login (see spec.md §5) is
allowed to do, down to a single forced command instead of a full shell:

```
Match Group smb-sync
    ForceCommand /bin/true
```

The package never generates or auto-populates rules like this — it only
pushes and includes whatever's written here.
