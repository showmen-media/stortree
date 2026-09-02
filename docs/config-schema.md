# config.yml / ldap.yml / rclone.conf — schema reference

Describes the shape of the three source-of-truth files. See
[spec.md](spec.md) for how they're used.

## Dotted-key shorthand

Throughout these files, a dotted key is shorthand for a nested mapping.
`rclone.remote: storagebox` means `rclone: {remote: storagebox}`;
`access.user: jd` means `access: {user: jd}`. Both forms are
equivalent and may be mixed — use whichever is more readable for a given
node (a single override reads better dotted; several together read better
nested, as `media-prod` does for `rclone`).

## config.yml

```yaml
host: <hostname>              # default serving host for the whole tree
rclone.remote: <remote-spec>  # default rclone source for the tree — see "rclone.remote is verbatim" below

client-defaults:
  rclone.args: {...}          # base rclone mount args, merged into every client mount —
                               # including an inventory host with no entry under clients: below

clients:
  <hostname>:
    rclone.args: {...}        # this client's overrides, merged over client-defaults —
                               # a host needs no entry here to become a client; see
                               # "Every inventory host participates" below

subdirs:
  <name>:
    host: <hostname>          # optional; inherits nearest ancestor's host if omitted
    rclone.remote: <remote-spec>  # optional; inherits nearest ancestor's remote if omitted
    rclone.args: {...}        # optional; used as-is for this node only, does not inherit
    access: [...] | access.group | access.user   # see Access below
    samba:
      subpath: "<template>"   # e.g. "%U" for per-connecting-user substitution
                               # — every participating host exposes this node
                               # as a share, not just its resolved owner; see
                               # "Samba sharing is universal" below
    subdirs: {...}            # recurse
    user-subdirs: {...}       # recurse — see note below
```

### Example

A filled-in tree, used as the running example for the rest of this doc —
a default host serving most of the tree directly, a second host serving a
couple of subtrees of its own, and a third host that only ever
client-mounts. It shows both `cache-dir` patterns from spec.md §1: `media-prod`
points its `cache-dir` straight at a plain path under the fixed
`/srv/stortree` root (the host's own local tree, no separate mount needed),
while `storage-node-bravo` instead points its `cache-dir` at
`.cache.subdirs.storage-node-bravo` below — a real `some-remote`-backed
mount (a separate disk on `storage-node-bravo`'s local network), so the vfs
cache lives off-host rather than competing for space on the box itself:

```yaml
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
      cache-dir: /srv/stortree/.cache/storage-node-bravo

subdirs:
  .cache.subdirs:
    storage-node-bravo:
      host: storage-node-bravo
      rclone.remote: some-remote:/.stortree-cache
    some-gcs-bucket: {}
  backups: {}
  home:
    samba:
      subpath: "%U"
    user-subdirs:
      whitfield-media:
        access:
          - group: "Whitfield Family & Friends"
            permissions: rx
          - group: "Michael Whitfield Family"
            permissions: rwx
        host: storage-node-bravo
        rclone.remote: some-remote:/media
      sys-configs:
        access.user: jd
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
            cache-dir: /srv/stortree/.cache/some-gcs-bucket
```

`storage-node-alpha` is the tree's default host: it owns everything under
`subdirs` that doesn't override `host`, and its `rclone.remote:
storagebox` is what those inherit too — `backups` is the one node that
takes that inheritance as-is, so it resolves to `storagebox`'s own root,
verbatim. This is purely a resolution default — it does not make
`storage-node-alpha` special at runtime (there is no "root host" in the
Ansible design; see [spec.md](spec.md)). Any host in `config.yml` is
applied to the same way, from the same control node, over the same
`ansible-playbook` run. `storage-node-bravo` owns three subtrees of its
own (its `.cache.subdirs` cache mount, `whitfield-media`, `mw-fam`), each
pointed at a different remote (`some-remote`) than the one it inherits.

`.cache.subdirs.storage-node-bravo` is itself a resolved subtree, like any
other — server-owned by `storage-node-bravo`, mounted from `some-remote`
(a device on that host's own local network). Its only purpose is to back
the `cache-dir` that `clients.storage-node-bravo.rclone.args` points at
above: `storage-node-bravo` caches the files `storagebox` serves (as the
tree's default remote, via `storage-node-alpha`) onto that local-network
disk rather than its own. `stortree_mounts` (spec.md §2) mounts it first
and wires the client mount's unit to require it (`RequiresMountsFor=`),
since the cache path has to exist before anything can write into it.
`media-prod`'s `cache-dir`, by contrast, needs no such node — it's a plain
path under the fixed `/srv/stortree` root, backed by nothing but the
host's own local disk.

`some-storage-gadget` owns no subtree at all — it only appears under
`clients:` — but because `home` carries a `samba:` block, it still ends up
exporting a `home` Samba share of its own: it peer-sources every piece of
`home` it doesn't own (which, since it owns none of `home`, is all of it)
— `sys-configs` and `media-prod` from `storage-node-alpha`,
`whitfield-media` and `mw-fam` from `storage-node-bravo` — the same way
`storage-node-alpha` peer-sources `storage-node-bravo`'s pieces for its
own copy of the share. See "Samba sharing is universal" below. The same
would hold for a fourth host with no mention in `config.yml` at all,
present only in the Ansible inventory — see "Every inventory host
participates" below.

Every name here — hosts, groups, the `jd` user — is fictional; §§ below
reference this same example throughout, all names/values used
consistently.

### Node inheritance

Every node under `subdirs`/`user-subdirs` inherits `host` and
`rclone.remote` from its nearest ancestor unless it overrides them. An
empty node (`backups: {}`) is valid and just means "use everything
inherited, no overrides."

`rclone.args` is the exception: it does **not** inherit. A node's
`rclone.args` is used exactly as set on that node, and a node with no
`rclone.args` of its own resolves to no args, regardless of what any
ancestor sets — see spec.md §1 for the full rationale and how this
differs from the client-mount role's `client-defaults` merge.

#### `rclone.remote` is verbatim

Whatever a node's `rclone.remote` resolves to — its own value, or the
nearest ancestor's — is passed to `rclone mount` exactly as written,
unchanged. `resolve()` never appends a node's position in the tree to it.
A bare section name (`storagebox`) mounts that remote's own root; a
`<section>:<path>` value (`some-remote:/media`) mounts exactly that path
on that remote. Two nodes that resolve to the same `rclone.remote` value
— by inheriting it, or by setting it explicitly — mount identical remote
content, at whatever two local paths their own tree positions give them;
nothing about being a different node makes the source differ. This is why
every node in the example above that needs distinct source content sets
its own `rclone.remote` explicitly, path included, rather than relying on
inheritance.

### `subdirs` vs `user-subdirs`

Same map-of-`name -> node` shape, but they resolve differently:

- `subdirs` entries are literal, single, shared directories — one
  instance on disk, ACL'd once (`.cache`, `frigate`, `backups`).
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
  ACLs on each descendant still apply per the usual rules, so
  `sys-configs` (`access.user: jd`) only shows up inside `jd`'s own
  per-user folder, not everyone else's.

### Access

Two equivalent forms:

```yaml
# list form — multiple group grants at once
access:
  - group: "Whitfield Family & Friends"
    permissions: rx
  - group: "Michael Whitfield Family"
    permissions: rwx

# dotted shorthand — single grant
access.group: "Michael Whitfield Family"
access.user: jd
```

`group`/`user` names are resolved against POSIX identities provided by
SSSD (backed by the configured LDAP server — see `ldap.yml` below).
`permissions` is a `rwx`-style string applied via `setfacl`.

### Every inventory host participates

`clients:` is only ever for a per-host override, never a prerequisite for
being a client. Every host in the Ansible inventory
(`inventory/hosts.yml`, spec.md "Config layout") that isn't itself some
node's resolved `host` gets a client mount of the root `rclone.remote`,
whether or not it has a `clients:` entry and whether or not it's named
anywhere in `config.yml` at all. With an entry, `clients.<hostname>.rclone.args`
merges over `client-defaults`; without one, it just gets `client-defaults`
verbatim.

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

`.cache.subdirs:` under `subdirs:` is the same dotted shorthand as
elsewhere: subdir named `.cache`, containing a nested `subdirs:` map
(`storage-node-bravo`, `some-gcs-bucket`). Not a subdir literally named `.cache.subdirs`.

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
