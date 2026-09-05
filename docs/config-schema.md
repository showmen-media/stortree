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

  client-defaults:
    rclone.args: {...}        # base rclone mount args, merged into every non-owning
                               # host's peer mount of this subtree — including one with
                               # no entry under clients: below
    rclone: false              # optional — set instead of/alongside .args to keep this
                               # subtree off every non-owning host by default; see
                               # "Per-client mount opt-out" below

  clients:
    <hostname>:
      rclone.args: {...}      # this client's overrides, merged over client-defaults —
                               # a host needs no entry here to become a client; see
                               # "Every inventory host participates" below
      rclone: false            # optional per-client override of client-defaults.rclone;
                               # see "Per-client mount opt-out" below

  access: {group: <name>, owner: <name>, permissions: <rwx-string>}  # see Access below
                               # — a single object, all three optional; dotted shorthand
                               # (access.group:/access.owner:/access.permissions:) works too
  samba:
    subpath: "<template>"     # e.g. "%U" for per-connecting-user substitution
                               # — every participating host exposes this node
                               # as a share, not just its resolved owner; see
                               # "Samba sharing is universal" below
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
        subpath: "%U"
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
would otherwise reach inside that subtree.

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
  `home/<username>/whitfield-media`, `home/<username>/sys-configs`. This
  holds independent of `samba.subpath: "%U"` (set on `home` in the
  example above) — that setting only templates the *Samba* path so
  an SMB client lands in its own per-user folder, and is otherwise
  irrelevant here; if `user-subdirs` is used without `samba.subpath`, the
  per-user folders still have to be the immediate children of that node.
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
one, client-mount another, and be opted out of a third, all at once.

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

A `samba:` block marks a node for export as an SMB share. That export is
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
resolved to need (see spec.md §3), never the whole file.

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
