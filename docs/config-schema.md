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

  client-defaults:            # applies to this node and everything under it — write it
                               # on a subdirectory as readily as on a top-level subtree
    rclone.args: {...}        # base rclone mount args, merged into every non-owning
                               # host's peer mount of this node — including one with
                               # no entry under clients: below
    rclone: false              # optional — set instead of/alongside .args to keep this
                               # node off every non-owning host by default; see
                               # "Per-client mount opt-out" below
    access: {...}              # optional — the {group?, owner?, permissions?} object a
                               # node itself takes, replacing this node's own grant on
                               # every non-owning host; see "Client-side access" below

  clients:
    <hostname>:
      rclone.args: {...}      # this client's overrides, merged over client-defaults —
                               # a host needs no entry here to become a client; see
                               # "Every inventory host participates" below
      rclone: false            # optional per-client override of client-defaults.rclone;
                               # see "Per-client mount opt-out" below
      access: {...}            # optional per-client override of client-defaults.access;
                               # see "Client-side access" below

  requires: [<path>, ...]      # optional — other subtrees whose mounts must be up
                               # before this one starts; tree-relative paths, a bare
                               # string accepted for one. See "Requires" below
  access: {group: <name>, owner: <name>, permissions: <rwx-string>}  # see Access below
                               # — a single object, all three optional; dotted shorthand
                               # (access.group:/access.owner:/access.permissions:) works too
  samba:                       # presence marks this node for export — write it bare
                               # (or `samba: true`/`samba: {}`) to share with the
                               # defaults, `samba: false` to opt back out
                               # — every participating host exposes this node
                               # as a share, not just its resolved owner; see
                               # "Samba sharing is universal" below. The share
                               # path is derived: a node with `user-subdirs`
                               # gets Samba's per-user `%U`, one without serves
                               # the node itself
    name: "<share-name>"      # optional — the share's name in smb.conf, i.e. what
                               # clients mount as //<host>/<name>; defaults to the
                               # node's path with everything outside [A-Za-z0-9_-]
                               # folded to `_` (`tree/home` → `tree_home`)
  subdirs: {...}               # recurse — this and everything under it works exactly the
                               # same as it does at the top level, just nested
  user-subdirs: {...}          # recurse — see note below
```

### Example

A filled-in tree, used as the running example for the rest of this doc —
a default host serving most of the tree directly, a second host serving a
couple of subtrees of its own, and a third host that only ever
client-mounts. It shows both `cache-dir` patterns from spec.md §1:
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
each sets `client-defaults.rclone: false` since nothing but its own
owning host ever needs it mounted (or, for `.gcs-cache`, created at all)
anywhere else:

```yaml
tree:
  host: storage-node-alpha
  rclone.remote: storagebox:/

  requires:
    - .bravo-cache

  client-defaults:
    rclone.args:
      vfs-cache-mode: full
      vfs-cache-max-age: 100h
      dir-cache-time: 5m

  clients:
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
  client-defaults:
    rclone: false

.gcs-cache:
  host: storage-node-alpha
  client-defaults:
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
the `cache-dir` that `clients.storage-node-bravo.rclone.args` points at
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
`tree`'s `clients:` — but because `home` carries a `samba:` block, it
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

### Per-client mount opt-out

By default, every top-level subtree is peer-mounted (or, with no
`rclone.remote` of its own, just created as an empty placeholder
directory) onto every inventory host that doesn't own it (see "Every
inventory host participates" below). A subtree that has no business
being visible anywhere but its own owning host — a per-host VFS-cache
backing store, remote-backed like `.bravo-cache` above or genuinely
local like `.gcs-cache` — can opt out with `client-defaults.rclone:
false` either way: it's not just about skipping a peer mount nobody
needs, a remote-less subtree like `.gcs-cache` would otherwise still get
that pointless empty placeholder directory created on every other host.

```yaml
<name>:
  host: <hostname>
  rclone.remote: <remote-spec>
  client-defaults:
    rclone: false             # no non-owning host gets a mount of this by default
  clients:
    <hostname>:
      rclone: false            # or true / {args: {...}} — always wins over client-defaults
```

`clients.<hostname>.rclone` always wins over `client-defaults.rclone`
when both are set for the same host: with `client-defaults.rclone:
false`, a `clients.<hostname>.rclone` entry that's truthy (`true`, or a
dict — with or without `.args`) becomes an *allow-list* — only hosts
listed that way get a mount, everyone else gets none. With
`client-defaults.rclone` left at its default (unset, i.e. enabled), a
`clients.<hostname>.rclone: false` entry becomes a *deny-list* instead —
every non-owning host gets a mount except the ones explicitly disabled.
The owning host itself is never affected either way — it always
self-mounts via `server_subtrees`, never through this client-mount path
at all. This applies uniformly to a top-level subtree's own peer mount
and to any Samba descendant nested under it (see "Samba sharing is
universal" below) — one setting governs everything a non-owning host
would otherwise reach inside that subtree, unless a node further down
says otherwise.

#### At any depth

`client-defaults`/`clients` are ordinary node keys, valid on a
subdirectory exactly as on a top-level subtree, and a node that writes
neither simply inherits whatever its nearest ancestor set. So a tree
that only ever writes them at the top level behaves exactly as it always
did — every node under a subtree gets that subtree's policy — while a
node that does write one refines, or reverses, that policy for itself
and everything under it, without touching its siblings.

Two axes of precedence, composing the same way at every level:

- **Within one node**, an explicit `clients.<hostname>` beats that same
  node's `client-defaults` — the rule above, unchanged.
- **Across nodes**, a nearer (deeper) node beats a more distant
  ancestor.

`rclone.args` are the exception to "nearest wins": they *accumulate*
down the chain, every level contributing, with the nearest and most
specific block winning any individual key conflict — so a subdirectory
adds a couple of args to whatever its subtree already set for that
client rather than replacing the lot.

The most useful shape this buys is the allow-list idiom one level down —
a subtree kept off every client, with a single node inside it handed to
one host:

```yaml
research:
  host: storage-node-alpha
  rclone.remote: storagebox:/research
  client-defaults:
    rclone: false             # nothing under `research` reaches any other host...
  subdirs:
    published:
      rclone.remote: storagebox:/research-published
      clients:
        some-storage-gadget:
          rclone: true         # ...except this one node, on this one host
    embargoed:
      rclone.remote: storagebox:/research-embargoed
```

`some-storage-gadget` ends up with one mount, at `research/published`,
peer-sourced from `storage-node-alpha` the same way a top-level client
mount is — there's no ancestor mount left for it to reach the node
through, so the node gets one of its own. Every other host gets nothing
of `research` at all, and `research/embargoed` reaches nobody but
`storage-node-alpha`.

Note that "truthy means enabled" holds at every level too: a
`clients.<hostname>.rclone.args` (or `client-defaults.rclone.args`)
written on a node *under* an opted-out ancestor re-enables that node,
since writing client-side mount args for it is taken to mean clients are
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
- its own client mount as the shallowest enabled node of its branch,
  which only exists when every ancestor is opted out.

Where a node has neither, a block written on it resolves to nothing.
That's host-dependent rather than a config error — the same node is
typically a Samba descendant on one host and plain content inside an
ancestor's mount on another — so nothing rejects it; it's just worth
knowing which of the two you're writing.

A `user-subdirs` descendant is never a client mount of the shallowest-
enabled kind: its path is still `%U`-templated and fans out into one
mount per granted user (see "`subdirs` vs `user-subdirs`" below), which
a single client mount can't describe. It reaches a non-owning host
through the Samba path instead, where the fan-out is resolved — and a
client block on it governs that, exactly as on any other Samba
descendant.

### Client-side access

`client-defaults`/`clients.<hostname>` also take an `access` object —
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
          client-defaults:
            access:                            # what every other host enforces
              group: Media Production
              permissions: rx
```

It becomes that host's `--uid`/`--gid`/`--dir-perms`/`--file-perms` for
the mount (and, for a node that resolves to a plain directory rather
than a mount, its ownership and mode) — real, kernel-enforced access on
that host, applied exactly like the node's own grant is on its owner,
via the same mechanism (spec.md §6).

Three things to know about it:

- **It replaces the node's own grant, it doesn't merge with it.** Set
  `access` in a client block and that object is the whole grant for that
  host's copy; the node's own `group`/`owner`/`permissions` contribute
  nothing to it. Write `access:` with nothing in it to drop the node's
  grant on that host entirely, which is different from writing no
  `access` at all (which keeps whatever the copy would otherwise have
  carried). Precedence is "nearest wins", exactly as for `rclone` above,
  and unlike `rclone.args` there's no accumulation: a partial grant
  assembled from two different places in the tree would be unreadable
  off the config.
- **The owning host is never affected.** `clients`/`client-defaults`
  only ever describe a host that doesn't own the node, so the node's own
  `access` is what its owner enforces, always.
- **It doesn't change the Samba share.** A share's `valid users`/`write
  list` stay derived from the nodes' own tree-wide grants and are
  identical on every host that exports the share ("Samba sharing is
  universal" below). Only what's enforced on this host's own filesystem
  changes.

A top-level subtree's client mount carries no grant at all unless a
client block gives it one — the owning host is what enforces the node's
grant, and a peer mount of its copy already reports what that host
applied.

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
  client-mount it rather than own it. That's what makes the example work:
  the cache dependency belongs to *bravo's* mount of `tree`, and bravo is
  a client of `tree`, not its owner.
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
explicitly). `rclone` — both `remote` and `args` — is different: it
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
  `access` on each descendant still applies per the usual rules, so
  `sys-configs` (`access.owner: jd`) only shows up inside `jd`'s own
  per-user folder, not everyone else's — an `owner` grant always pins
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
  `rclone`), or a dedicated per-user mount for one nested inside a
  remote-backed subtree like `tree` here, since a single rclone mount
  can't present two different paths under it with two different owners.
  See spec.md §6 (`user_container_paths()`) for the mechanism, and its
  own note there on what this means for a sibling like `mw-fam`'s bind
  mount, which now has to wait for that per-user mount too.

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
`group` granting *shared* access to that same folder at the same
`permissions` level, not a second, differently-permissioned tier. There's
no way to express two different principals at two different permission
levels on one node — see why below.

A node's `access` is what its *owning* host enforces. A host that only
holds a copy of the node — a client mount, or a peer-sourced Samba
descendant — enforces the same grant by default, and can be given a
different one with an `access` inside `client-defaults`/`clients`; see
"Client-side access" above.

`group`/`owner` names are resolved against POSIX identities provided by
SSSD (backed by the configured LDAP server — see `ldap.yml` below).
`permissions` is a `rwx`-style string, applied as plain Unix ownership +
mode (spec.md §6) — not a POSIX ACL, and there's no way to write one:
`access` was deliberately restricted to a single object, one owner + one
group + one shared permissions level, exactly what a remote-backed node
can ever actually carry. A remote-backed node (directly, or peer-sourced
from whichever host owns it) is always an rclone FUSE mount, and rclone's
FUSE mount never implements `setxattr` — it can't carry a POSIX ACL on
any host, full stop, so the old list-of-grants form (letting a node like
`whitfield-media` grant two different groups two different permission
levels) could describe configurations that were never actually
enforceable for such a node; the schema no longer lets you write one. A
plain local node (`sys-configs` here) gets the same treatment for
consistency, not necessity — it could carry a real POSIX ACL, but there's
no reason for its enforcement to work differently from a remote-backed
sibling's.

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
out explicitly in config.yml — an explicit `permissions:` is enforced
exactly as written instead. See spec.md §6 for exactly how this becomes
real, symmetric enforcement over both Samba and SSH alike, for every node
with any `access` at all.

### Every inventory host participates

`clients:` is only ever for a per-host override, never a prerequisite for
being a client. Every host in the Ansible inventory
(`inventory/hosts.yml`, spec.md "Config layout") that isn't itself a
given top-level subtree's resolved `host` gets a client mount of that
subtree, whether or not it has a `clients:` entry there and whether or
not it's named anywhere in `config.yml` at all — unless that subtree's
own `client-defaults`/`clients.<hostname>` opts it out (see "Per-client
mount opt-out" above). With a `clients:` entry, `clients.<hostname>.
rclone.args` merges over `client-defaults`; without one, it just gets
`client-defaults` verbatim. That mount is **not** a direct mount of the
subtree's own `rclone.remote` — it's a peer-sftp mount of the resolved
owner's (`host:`'s) own copy of that subtree, provisioned by
`stortree_peer_trust` the same way as any other peer dependency (spec.md
§1/§7); a client never holds credentials for the subtree's remote itself.
This applies independently to every top-level subtree — a host can own
one, client-mount another, and be opted out of a third, all at once —
and, where a subtree's own nodes carry their own `client-defaults`/
`clients`, independently within a subtree too ("At any depth" above).

The same goes for "Samba sharing is universal" below and for cross-host
peer dependencies (spec.md §1/§7): both apply to every inventory host
equally, not only ones named in `config.yml`. Naming a host in
`config.yml` — as a node's `host:`, or under `clients:` — only ever
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
only ever appears under `clients:` (`some-storage-gadget` above), and even
a host with no mention in `config.yml` whatsoever (see "Every inventory
host participates" above). A host that already owns some or all of the
node's data serves it from there; whatever it doesn't own, it
peer-sources from the actual owning host — the same peer-trust mechanism
spec.md §1/§7 describes for a Samba node's own descendants, just not
restricted to hosts that already serve some other part of the tree.
There's no "designated Samba host": if a node has a `samba:` block, every
inventory host — server, client-only, or entirely unnamed in
`config.yml` — ends up serving it.

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
host that has no business serving SMB — one that is only a client of the
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
would save nothing. Its own client mounts are untouched: opting out of
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

#### The share path

Where a share points is derived from the node, not written:

- a node with a `user-subdirs` key is exported at `<node>/%U` — Samba
  expands `%U` to the connecting username, so each user lands in their
  own folder;
- a node without one is exported at `<node>` itself.

There is no key for this. The node's shape already answers the question:
`user-subdirs` means the node's immediate children *are* per-user
folders, so a share of that node that didn't descend into one would be
exposing every user's folder to every other user — over SMB, with
nothing at apply time saying so. That was previously reachable two ways,
by omitting the old `samba.subpath` key or by misspelling it, and is now
unreachable.

It's the key's *presence* that decides, exactly as with `samba:` itself.
`user-subdirs: {}` and a bare `user-subdirs:` declare no substructure
yet, but they still say the node has a per-user level — reading them as
"not per-user" would mean emptying a node's `user-subdirs` silently
widens its share from one user's own folder to the directory holding
everyone's.

`%U` also earns each connecting user a standing entry in the share's
`valid users`, so their access to their own folder doesn't depend on any
particular descendant carrying an `access` grant.

A config that still writes `samba.subpath` fails with a message saying
so — it was a real key once, so it's rejected specifically rather than
as a typo. Delete the line; the derived value is the one it was almost
certainly setting.

#### Share names

The share's name — its `smb.conf` section header, and what a client
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

Names have to be unique across the tree, however they were arrived at:
two nodes landing on one name — two `samba.name`s written the same, or
two paths folding together (`tree/a b` and `tree/a_b`) — fail the run,
because `smb.conf` would otherwise keep the first stanza and drop the
second, leaving part of the tree silently unreachable over SMB. The name
changes nothing else: the share's path, its `valid users`/`write list`,
and the peer mounts behind it are all still derived from the node's real
path.

### Unknown keys are an error

Every key in a node is either one this schema defines or a mistake, and
`resolve()` treats it as the latter: an unrecognized key anywhere in the
tree — at the node level, or inside `rclone:`, `access:`, `samba:`,
`client-defaults:` or a `clients:` entry (including the `rclone:`/
`access:` objects nested in those) — fails the run, naming the node it's
on and, where there's a near match, the key it's probably meant to be.

This matters more here than the usual argument for strictness, because
the schema has no key whose absence is loud. A misspelled
`rclone.remote` leaves the node a plain directory and the share on top
of it serving an empty path; a misspelled `client-defaults` re-enables a
subtree that was meant to stay off every other host, and provisions the
SSH trust to go with it; a misspelled `subdirs` drops a whole subtree; a
misspelled `access.group` drops the grant and leaves the path at its
permissive default. All four resolve to something plausible, and none of
them announce themselves at apply time.

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
what `stortree-mount@`, `stortree-bind@` and `stortree-user-mount@` are
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
subtrees it owns, plus any a `clients:` block hands it directly. A remote
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
