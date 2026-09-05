# stortree — development plan

## Goal

An Ansible project, run from a single control node (an operator's
workstation or a CI runner — not one of the storage hosts), that turns a
declarative tree (`config.yml` + `ldap.yml` + `rclone.conf`) into:

- rclone mounts (as a host's own client, and/or as the server backing a
  Samba share),
- Samba shares access-controlled by real Unix ownership and mode,
- Unix identity resolved from an existing LDAP directory.

Ansible itself replaces the custom fan-out/push mechanism a bespoke
daemon would otherwise need: the control node already has SSH reach to
every managed host, and `ansible-playbook` already re-applies the same
play to convergence, idempotently, on every run. There is no `stortree`
binary, no daemon, and no `init`/`join`/`apply`/`reconcile` CLI — the
project is a set of roles, a filter plugin, and a playbook.

## Config layout (control node)

```
inventory/
  hosts.yml          # storage hosts + connection vars, ordinary Ansible inventory
                     # — every host here participates (§1), named in config.yml or not
stortree/
  config.yml         # the directory tree: hosts, clients, subdirs, access grants (non-secret)
  ldap.yml           # LDAP server connection + group/POSIX mapping (vaulted)
  rclone.conf        # rclone's own config file — named remotes config.yml references (vaulted)
  sshd_config        # optional: freeform sshd config fragment, pushed + included on every host (see §6)
filter_plugins/
  stortree.py        # resolve() — the pure config-resolution function (§1)
roles/
  stortree_common/         # service account, /srv/stortree, /etc/stortree layout (§7)
  stortree_facts/          # wraps resolve() into per-host Ansible facts (§1)
  stortree_identity/       # SSSD against ldap.yml (§5)
  stortree_peer_trust/     # cross-host keypairs + authorized_keys for peer deps (§1, §7)
  stortree_secrets/        # filtered per-host rclone.conf rendering (§3)
  stortree_mounts/         # rclone mount systemd units + ownership/mode from access (§2, §6)
  stortree_samba/          # smb.conf + testparm + reload (§4)
  stortree_pam_smbpass/    # PAM stacking (§5)
  stortree_sshd/           # optional sshd_config.d fragment (§6)
playbooks/
  site.yml           # the only entrypoint: apply the whole tree to every host
  status.yml         # read-only facts/report play
docs/config-schema.md
```

`sshd_config` is optional and freeform — raw `sshd_config` directives, not
a stortree schema. If present, `stortree_sshd` pushes it to every host as
part of the normal play and installs it as a drop-in include (e.g.
`/etc/ssh/sshd_config.d/stortree.conf`), with `sshd` reloaded. This is
where an operator can hand-add host-wide SSH policy — see §6 for why you
might want a `Match Group`/`ForceCommand` rule here. The role never
generates or auto-populates this file's contents, only pushes and includes
whatever's written in it.

These files live under `stortree/` in this repo (or wherever an operator
checks it out — typically a private git repo, since `ldap.yml` and
`rclone.conf` hold credentials even vaulted). Nothing lives under
`/etc/stortree/` on the control node; that path is reserved for the
per-host rendered state described below.

`ldap.yml` and `rclone.conf` are encrypted at rest with `ansible-vault`
(whole-file encryption — `ansible-vault encrypt stortree/ldap.yml
stortree/rclone.conf`). Ansible transparently decrypts vaulted files it
reads via `vars_files`/`lookup('file', ...)` as long as a vault password
is available (`--ask-vault-pass` or a vault password file), so the
resolver and templates work with plain YAML/INI in memory without a
separate decryption step. The master files never leave the control node
in cleartext — see §3 for what's actually written to each host.

## Architecture

### 1. Config resolution

A pure, host-agnostic resolution function, `resolve(tree, hostname,
all_hosts)`, implemented once as a Python filter plugin
(`filter_plugins/stortree.py`) and exposed to plays as a Jinja filter.
Given a hostname, the parsed contents of the three config files, and the
full list of inventory hostnames (`all_hosts` — `stortree_facts` passes
Ansible's own `groups['all']`), it computes what that host must do.
`all_hosts` is what lets a host that's in the inventory but named nowhere
in `config.yml` still resolve as a full participant — see "Every
inventory host participates" in
[docs/config-schema.md](config-schema.md) — without `resolve()` reaching
into Ansible state itself. No I/O beyond what's passed in — this stays
unit-testable with plain `pytest` (fixture configs plus a fixture host
list), without touching Ansible, systemd, Samba, or SSSD.

`stortree_facts` is a thin role that runs first in `site.yml` and does
nothing but call the filter and `set_fact` the result onto each host, so
every later role/template works with plain facts rather than calling the
filter directly:

- **Server subtrees** — every node in `config.yml`'s tree where
  `host == this host` (inheriting the nearest ancestor's `host` when a node
  doesn't override it). Each entry resolves to: local path, rclone remote
  (its own, if set — `rclone` never inherits, see below), rclone args
  (also never inherited), Samba export settings (if any), and resolved
  `access` rules. A node with no `rclone.remote` of its own resolves with
  no remote at all — it's a plain directory that has to exist, not a
  separate rclone mount; §2 covers what that means for mount planning.
- **Client mounts** — one per top-level subtree in `config.yml` (every
  key at the file's own top level, config-schema.md "Top-level
  subtrees") that this host doesn't own: a peer-sftp mount sourced from
  the host that actually owns it (that subtree's own `host:`) rather
  than the subtree's own third-party `rclone.remote`. This is the same
  peer-sourcing rule §1 already applies to any samba descendant a host
  doesn't own, just generalized to every top-level subtree instead of
  being funneled through one shared tree root — there's no single
  implicit root any more; each top-level subtree stands on its own,
  independently peer-mounted or self-mounted (config-schema.md "Node
  inheritance"). A client never holds direct credentials to a subtree's
  own remote, only an SFTP hop into its owning host's own already-mounted
  copy of it (provisioned by `stortree_peer_trust`, §7, exactly like any
  other peer dependency). If the host has an entry under that subtree's
  own `clients:` map, `clients.<hostname>.rclone.args` merges over that
  subtree's own `client-defaults`; otherwise it gets `client-defaults`
  verbatim — a `clients:` entry is only ever a per-host override, never a
  prerequisite for being a client (see "Every inventory host
  participates", config-schema.md). A subtree's own
  `client-defaults`/`clients.<hostname>` can also set `rclone: false`
  instead of (or alongside) `.args` to opt a host — or every non-owning
  host by default — out of mounting it at all (config-schema.md
  "Per-client mount opt-out"); this is the mechanism a subtree with no
  business being visible past its own owning host (e.g. a per-host
  VFS-cache backing store) uses to stay local-only. A subtree with no
  `rclone.remote` of its own has nothing to peer for — the client still
  gets its local directory created, just no mount and no peer dependency
  for it. Unlike a samba peer dependency, this one implies no Samba
  behavior of its own — it exists purely so a client's local tree has
  real content — but it does mean a non-owning host now needs
  `stortree_peer_trust` (§7) to reach whichever host owns each top-level
  subtree it client-mounts, which the role already handles the same way
  as any other peer dependency.
- **Samba shares** — every node anywhere in the tree that carries a
  `samba:` block, resolved for *every* host in `all_hosts`, not just the
  node's own resolved `host` or hosts named anywhere in `config.yml`. A
  host that already owns the node's data (as a server subtree above)
  serves it from there; every other participating host — including one
  with no server subtrees of its own and no mention in `config.yml` at
  all — gets the same share provisioned via a peer-sourced mount instead
  (see "Cross-host peer dependencies" below, which this generalizes).

Every resolved local path is realized under the fixed root `/srv/stortree`,
the same on every host — not a configurable field. This is what
`config.yml` values like a `cache-dir` override can reference directly
(e.g. `/srv/stortree/.cache/<name>`) to point back into the host's own
local tree instead of a separate disk. For an actual separate disk, mount
it as an ordinary resolved subtree instead and point `cache-dir` at that
subtree's local path — see the `cache-dir` fields in the example config in
[docs/config-schema.md](config-schema.md), which shows both patterns.

`rclone.remote` itself is always used verbatim when a node sets one —
whatever its own value resolves to is passed to `rclone mount` exactly as
written, never combined with the node's position in the tree, and never
inherited from an ancestor (`rclone` — both `remote` and `args` — is
never inherited; only `host` is). See "Node inheritance" and
"`rclone.remote` is verbatim" in
[docs/config-schema.md](config-schema.md) for the full rule and what a
node with no `rclone.remote` of its own resolves to instead.

A host can appear in both lists (e.g. serve one subtree, direct-mount
another host's remote for itself).

**Cross-host peer dependencies.** Samba sharing is universal (§1 above):
every participating host — every host in `all_hosts`, not only ones
named in `config.yml`, and not only the ones that already own a piece of
it — ends up serving a complete copy of any node that carries a `samba:`
block. Because that node's local directory has to be
one real, complete tree on each of those hosts, and a subtree with a
`samba:` block can span descendants whose `host:` differs from the node's
own — e.g. per-user home folders where some `user-subdirs` entries stay on
one host while others are pinned to a second host — resolution detects,
for *every* participating host H, which descendants of a samba-configured
node resolve to some other host. Each one becomes a **peer dependency**
of H: H needs that descendant's data sourced from its actual owning host,
not mounted a second time from the original third-party remote, and this
holds even when H doesn't already own any other piece of the same node —
a client-only host with no server subtrees of its own still ends up
peer-dependent on every host that owns a piece of a samba-configured node,
exactly like a host that owns part of it and needs the rest. This falls
out of the `host:`/`rclone.remote` fields already in the tree, independent
of `clients:` — no separate field is needed to express it. The
samba-marked node itself is the one descendant this never applies to when
it carries no `rclone.remote` of its own and has children (the common
shape — see §2): a pure structural container like that delegates its real
content entirely to its own, more specific descendants, so peer-sourcing
it too would be redundant at best; a remote-less descendant with no
children of its own (a genuine leaf, just backed by nothing but the
owning host's own local disk) still becomes a peer dependency like any
other, since a live peer connection to that host is the only way to
source it at all. Because
`resolve()` is pure and total over the whole tree, computing H's peer
dependencies just means calling `resolve(tree, B, all_hosts)` for every
owning host B too — the filter plugin does this internally, so peer
resolution never needs Ansible-level `hostvars` cross-referencing between
plays. See §3 for
how it's rendered and §7 for how the necessary trust gets established.

Each descendant's resolved `host` is checked directly against the
requesting host H — never transitively through an intermediate
descendant's host. Combined with `config.yml` being a strict tree (a
node's children are only ever defined under it, never back-referencing an
ancestor), this makes peer-dependency resolution inherently single-hop: a
host can end up peer-dependent on several other hosts at once (and
mutually so, in both directions, for different subtrees), but resolving
one host's peer set never requires first resolving another's, so nothing
can chain or cycle. No cycle-detection logic is needed in the resolver as
a result — the filter plugin's unit tests should still include a
mutual-dependency case, a case where a client-only host with no server
subtrees of its own still resolves peer dependencies for a
samba-configured node, and a case where a host present only in
`all_hosts` — no `host:`, no `clients:` entry, no mention in `config.yml`
at all — resolves the same way, to lock all three invariants in.

`rclone` — `remote` and `args` alike — never follows the ancestor-
inheritance rule Node inheritance (config-schema.md) sets out for `host`;
only the client-mount role layers anything resembling inheritance on top
of a node's own `rclone.args`, and even that is a distinct, one-time merge
rather than tree inheritance:

- **Client-mount role**: `client-defaults.rclone.args` sets the defaults
  applied to every peer that mounts this host's remote, then
  `clients.<host>.rclone.args` merges its own overrides on top for that
  specific peer. This is the one intentional merge chain, exactly two
  levels deep (defaults, then a single per-client override) — later
  entries win on key conflicts.
- **Server role**: a subdir node's `rclone.remote`/`rclone.args` are used
  exactly as set on that node, full stop — never merged with, substituted
  from, or inherited from any ancestor or descendant subdir's `rclone`. A
  node without its own `rclone` resolves to no remote and no args at all,
  even if an ancestor sets some; a node's own `rclone`, once set, isn't
  affected by what its children set either. `host` still inherits down
  the tree as usual (Node inheritance, config-schema.md) — `rclone` is the
  one field, at every level, that's exempt.

### 2. Mount management (rclone)

The `stortree_mounts` role flattens `stortree_facts`' resolved server
subtrees, client mount, and peer dependencies into one plan
(`stortree_plan_mounts`) and, for every entry that actually has a
`remote` (§1 — a server-subtree node with no `rclone.remote` of its own
resolves with none, since `rclone` never inherits), templates one
systemd unit per mount (`templates/rclone-mount@.service.j2`: `rclone
mount <remote> <path> <merged args...>`, `Restart=on-failure`,
`After=network-online.target`, `RequiresMountsFor=` where one mount nests
under another — skipping over any remote-less ancestor in between, since
those have no unit of their own to require), using the `template` module
plus `systemd_service` (`daemon_reload: true`, `enabled`/`state:
started`). Every entry in the plan gets something created on disk: a
remote-less entry is a plain directory that has to exist (nested inside a
mounted ancestor, or as real local storage on its own resolved host if
not), and gets no rclone unit; a per-user bind-mount entry (§6 — the
per-member fan-out for a `group`-only grant) gets an ordinary directory
too, plus its own `templates/stortree-bind@.service.j2` unit (`mount
--bind <real mount's path> <this entry's path>`, `Type=oneshot,
RemainAfterExit=yes`, ordered after and requiring both the real mount it
fans out and whatever mount it's itself nested under) — not an rclone
unit, since it isn't a second rclone mount of the same remote.

Every peer dependency (§1) becomes one of these mount entries too, not
just a top-level subtree's own client mount — a samba descendant this
host doesn't own is real data its own local tree still has to contain,
sourced directly from whichever host actually owns it (mesh, one peer
relationship per distinct owning host, never funneled through one shared
tree root — there is no such thing any more). The one exception is the
samba node marking a subtree for export itself when it carries no
`rclone.remote` of its own and has children: that's a pure structural
container (the common case — `home` in the worked example above), and
peer-mounting it too would be both redundant with its own children's more
specific entries and actively wrong (an unrelated ancestor a same-path
child this host owns outright would then have to require a peer
connection it never needed) — `stortree_plan_mounts` skips it, the same
way it skips any remote-less node with children. A remote-less *leaf*
descendant (no `rclone.remote` of its own, and no children — e.g.
`sys-configs` above) still gets peer-mounted despite having no
third-party backend: from a non-owning host's perspective, its real
content only exists on the owning host's own local disk, so a live
peer-sftp view of that exact path is the only way to source it at all —
unlike a server_subtrees entry with no remote (§1 "Node inheritance"),
where the owning host already has the data locally and needs no mount to
reach it.

A per-user peer dependency's %U template is resolved here the same way a
per-user server_subtrees node's is — see §6 for the full mechanism, using
`stortree_group_members` (§6 for where that fact comes from): an `owner`
grant still gets one real mount at that one user's own path; a
`group`-only grant instead gets exactly one real, shared mount (at a
hidden `.mounts` path standing in for `%U`) plus one bind mount per
member fanning it back out to each member's own folder, rather than one
full duplicate mount per member. `stortree_mounts` computes this for both
server_subtrees and peer_dependencies alike (`stortree_plan_mounts` in
`filter_plugins/stortree.py`), so a peer sources the owning host's real,
shared path directly — that's the only place real content for a
`group`-only grant ever actually lives on the owning host's own disk, the
same way its own per-member paths are bind mounts rather than second
mounts there too.

Ansible's own idempotence covers what a hand-rolled reconciler would
otherwise have to implement: `template` only rewrites a unit file when its
rendered content changes, and `systemd_service` only touches
enabled/running state when it's out of sync — so a stale mount that's no
longer in the resolved tree still needs an explicit cleanup step (the role
also lists both `/etc/systemd/system/stortree-mount@*.service` and
`stortree-bind@*.service`, diffs that against the currently-resolved unit
names, and removes/`daemon-reload`s any that are no longer wanted). This
cleanup runs *before* any path on disk is touched, not after — a path
that's switching from a real per-user mount to a bind-mount destination
(a member's own folder, once a `group`-only grant collapses to a shared
mount) needs its stale unit stopped and unmounted first, or that
still-busy mountpoint ends up bind-mounted on top of yet another live
mount instead of a plain, empty directory. systemd itself handles restart
policy, resource limits, and logging (journald) for both the `rclone
mount` process and the bind-mount unit once each is in place.

### 3. Secrets handling

The control node's `rclone.conf` is rclone's own native config format — no
separate schema to invent or translate, `config.yml`'s `rclone.remote`
fields just reference section names in it directly. It's kept
`ansible-vault`-encrypted on the control node as the master copy, holding
every remote (`storagebox`, `some-remote`, `some-gcs-bucket`, …). The
`stortree_secrets` role reads the decrypted INI in memory (via the same
filter plugin, using Python's `configparser`) and templates a **filtered,
host-specific `rclone.conf`** containing only the remote sections that
host's resolved server+client+Samba role actually needs (Samba sharing
being universal per §1 means even a client-only host can pull in peer
sections here), writing it to
`/etc/stortree/rclone.conf` on that host (mode `0600`, owned by the local
`stortree` service account). This keeps e.g. a storage-gadget's remote
scoped to only the credentials it has a resolved use for — other remotes
stay off it entirely. The master `rclone.conf` itself is never copied to
any managed host.

Peer dependencies (§1) get the same treatment: for each one, the filter
plugin also synthesizes an `sftp` remote section — pointing at the owning
host's `stortree` SSH endpoint (keys provisioned by `stortree_peer_trust`,
§7) and the local path already resolved for that subtree there — into the
filtered `rclone.conf` rendered for the serving host. The serving host's
mount unit (§2) then mounts that synthesized remote at the correct nested
path, same as any other resolved mount. The top-level-subtree peer
dependency behind every client mount (§1) synthesizes the same way, just
with that subtree's own top-level path as the local path — the section's
`path` is the owning host's own copy of that one subtree, and the
client's mount unit references the synthesized section's name the same
way any other peer-sourced mount does, instead of the subtree's own
third-party `rclone.remote` value. A per-user peer dependency's %U gets resolved to a
single real path here too, exactly the way `stortree_mounts` (§2, §6)
independently resolves the same entry to its own one real mount — an
owner grant's own path, or a group-only grant's one shared `.mounts`
path, never one section per member — both have to agree on that resolved
(not templated) path to name the section the same way, so
`stortree_secrets` is the one role that resolves
`stortree_group_members`/`stortree_group_gids`/`stortree_user_uids` (§6)
fresh via `getent`, first in `site.yml`'s order among the roles that need
them; `stortree_mounts` reuses those same facts rather than re-deriving
its own.

### 4. Samba layer

The `stortree_samba` role generates `smb.conf` share stanzas from every
`samba:`-configured node resolved for the current host (§1) — which,
since Samba sharing is universal, means every host gets a stanza for
every such node in the tree, not only the ones whose subtrees it happens
to own: path, subpath templates (`%U` for the `home` per-user pattern),
and `valid users`/`write list` derived from the resolved `access` rules
once those are mapped to real POSIX groups/users (§5, §6). A host
assembles the node's local path the same way whether it's the resolved
owner or sourcing peer data (§1/§3) — Samba itself never needs to know
which. Sets `nt acl support = yes` (still lets a Windows client view the
plain Unix owner/group/mode `stortree_mounts` sets, §6, as a simplified
NT ACL) and `inherit permissions = yes` so a newly created file inherits
its parent directory's ownership/mode rather than the connecting user's
own umask-computed default — `smbd` still runs as the real authenticated
user throughout (nothing here changes that), so this is what makes a new
file land with the *directory's* access, not just whatever that one
session happened to create it with.

The role validates every rendered `smb.conf` with `testparm` (as a
`command`/`validate` argument on the `template` task, so a bad render
fails the play instead of getting written) before triggering a `smbd`
reload handler.

### 5. Identity & authentication (LDAP + SSSD)

`stortree_identity` configures SSSD on every host against the LDAP server
(endpoint/base DN/bind identity from `ldap.yml`) purely for **POSIX
identity** — `uidNumber`/`gidNumber`/group membership — so a group like
"Michael Whitfield Family" resolves to the same real Unix group and GID on
every host. Nothing here is specific to a particular LDAP product; any
server that can expose `posixAccount`/`posixGroup`-style attributes works
the same way.

**Requirement**: the LDAP server must expose real `uidNumber`/`gidNumber`
POSIX attributes — the role assumes this rather than verifying it for you,
since it isn't something a playbook run can check on its own. Confirm it
holds for your directory before relying on it: point SSSD at it and check
that `getent passwd`/`getent group`/`id` all resolve real, consistent ids,
and that `sssd.conf` leaves `ldap_id_mapping` unset (which defaults to
`false` for `id_provider = ldap`, i.e. SSSD reads the directory's own
POSIX attributes rather than algorithmically synthesizing them).

**SMB authentication is the harder part.** The project is designed to work
without relying on NTLM/NT-hash support from the LDAP server, since Samba's
`security = user` model needs an NT-compatible hash, not a plain LDAP bind
— true of most directories that don't specifically maintain the Samba
schema (`sambaSAMAccount`/`sambaNTPassword`). Samba's own answer to this,
historically, was the `pam_smbpass` module (`libpam-smbpass`) — but
upstream Samba dropped it in 4.4, and Debian/Ubuntu removed the package
along with it well before any platform this project targets existed, so
it's not installable anywhere `stortree_pam_smbpass` would run. The
`stortree_pam_smbpass` role instead stacks `pam_exec.so
expose_authtok seteuid` into the host's PAM `auth`/`password` chain
*after* SSSD's module (via `ansible.builtin.pamd` for a declarative,
idempotent edit rather than hand-patching `/etc/pam.d/common-auth`),
pointed at a small script the role deploys
(`roles/stortree_pam_smbpass/files/pam-smbpass-sync.sh`). `pam_exec`
hands that script the plaintext credential on stdin on any successful PAM
authentication (SSH login, `sudo`, etc.) or password change, and the
script drives `smbpasswd -s -a` to update that user's local
`smbpasswd`/`tdbsam` NT-hash entry — so the Samba password stays in sync
with the LDAP password, with the NT hash generated and kept locally on
each host, the same end result `pam_smbpass` used to produce. Both
stackings use `optional` control (a sync side effect must never be able
to gate or short-circuit the real auth/password decision) and `seteuid`
(`passwd` is setuid root, so its real UID is the invoking user — without
`seteuid` the script would run as that user and be unable to write the
local smbpasswd database).

Caveat: the sync only fires on an actual PAM event *on that specific
host*, so a user's first SMB connection to a given host fails until
they've authenticated via some other PAM path there at least once. A plain
password-authenticated `ssh` login is enough to trigger it — it runs the
same PAM `auth` chain `pam_smbpass` is stacked into, so the sync happens as
a side effect of a successful connection, nothing further to run. Mitigation
is therefore just an onboarding fact to document: each LDAP user needs to
log into a given host once (SSH is the simplest path) before their first
SMB connection to that host will work. If you want that login scoped to
something narrower than a full shell, the optional `sshd_config` fragment
(§6) is where a `Match Group`/`ForceCommand` rule can restrict it — the
PAM auth still fires before any forced command runs, so the sync happens
regardless of what that session is allowed to do afterward.

### 6. Access enforcement & SSH access

`access` in `config.yml` is always a single object (never a list) —
`{group?, owner?, permissions?}`, all optional, either written out or via
the dotted shorthand (`access.group: X`, `access.owner: Y`). This isn't
just a syntax restriction: a remote-backed node (directly, or
peer-sourced from whichever host owns it, §1/§2) is always an rclone FUSE
mount, and rclone's FUSE mount never implements `setxattr` — it can't
carry a POSIX ACL at all, on any host, ever. What it *can* carry is plain
Unix ownership and mode: one owner, one group, one shared permissions
level applied to both. That's exactly what one `access` object expresses
and no more, so the schema doesn't let you write anything a remote-backed
node couldn't actually enforce (`_normalize_access()` raises if it's
ever given a list). A plain local node (no `rclone.remote` of its own)
gets the same treatment for a simpler reason: consistency, not
necessity — it could carry a real POSIX ACL, but there's no reason for
its enforcement to work differently from a remote-backed sibling's.

`stortree_mounts` applies `access` directly, for every resolved path —
mount point or plain directory alike — via three pure filters
(`access_owner`/`access_group`/`access_mode`, all in
`filter_plugins/stortree.py`, all unit-tested independent of Ansible):

- **Neither `owner` nor `group` granted**: the plain default every path
  had before `access` existed — owner `stortree` with full control,
  group `stortree` with read+traverse, other execute-only (`0751`).
- **`owner` only**: that user owns the path outright, at the granted
  `permissions` level; group is `stortree` with *no* access — deliberately
  private, since it was scoped to one specific person.
- **`group` only**: `stortree` still owns the path (so it can always be
  administered regardless of who else can reach it) with full control;
  the granted group gets `permissions`.
- **Both**: the path is pinned to that one `owner` (no group-driven
  expansion — see below), who and whose group both get `permissions`.

Every one of these except an *explicit* `permissions` (one the config
actually wrote out, tracked as `permissions_explicit` by
`_normalize_access()`) also carries a bare execute bit on `other`, even
when `owner`/`group` is granted: `open()`/`chdir()` need execute on every
ancestor directory to reach a real grant several levels down (e.g. a
`user-subdirs` descendant's own `access.group`), and the connecting user
is essentially never a member of the local `stortree` group that would
otherwise own an ungranted ancestor — Samba and SSH both run as that real
user post-privilege-drop, subject to the same kernel checks as anyone
else. Nothing here is ever world-*readable*, only world-*traversable*. An
operator who writes `permissions:` out explicitly gets it enforced
exactly as written, other-bits included, even if that happens to block a
differently-scoped descendant grant.

For a plain local directory, that's `ansible.builtin.file`'s
`owner`/`group`/`mode` — real, standard Unix ownership, no `acl` package
needed at all. For a remote-backed node, `stortree_mounts` renders the
same three values into the rclone unit instead: with neither `owner` nor
`group` granted, the unit omits `--allow-other`, so FUSE restricts the
mount to the mounting user (`stortree`) alone — no SSH session or local
process reaches it, root included, and nothing overrides that for Samba
either (`smbd` still runs as the real authenticated user, unchanged from
stock behavior). Root's exclusion here isn't a gap to close: rclone's
own `--allow-root` (libfuse's documented way to widen a private mount to
"the mounting user and root") is silently ignored by the rclone build
this fleet runs ("Ignoring --allow-root. Support has been removed
upstream", logged on every mount attempt) — there is no flag that gets
root into an ungranted mount, full stop, so `stortree_common` and
`stortree_mounts` both treat that as permanent: neither ever tries to
manage a path once a direct probe shows root can't reach it (a `stat`,
not `ansible_facts.mounts` — Ansible's own mount-fact gathering silently
drops any mount whose device string doesn't happen to contain "/",
which some of this fleet's own rclone mounts hit in practice), rather
than assume root access it structurally cannot have.
`--allow-other` (the granted-mount case just below) additionally
requires `user_allow_other` in `/etc/fuse.conf` (`stortree_mounts`
ensures it's set before rendering or restarting any unit) — libfuse
refuses the option outright from a non-root mounting process without it.
With `owner` and/or `group` granted, the
unit instead adds `--allow-other` back and uid/gid-owns the mount
directly (`--uid`/`--gid`, resolved from the same `getent passwd`/`getent
group` lookups `stortree_secrets` already runs for %U-expansion below —
`stortree_user_uids`/`stortree_group_gids` read the numeric id out of the
same `ansible_facts.getent_passwd`/`getent_group` data instead of the
name/member list) and `--dir-perms`/`--file-perms` (both set to the same
mode `access_mode()` computed). Real, kernel-enforced access, checked
against whoever is actually connecting — Samba included, since Samba
still operates as that real user throughout. `mw-fam` in the running
example (`access.group`) is exactly this case.

A %U-templated samba share's own `valid users`/`write list` (§4,
`stortree_samba_access_tokens()`) always include `%U` itself in addition
to the union of its descendants' `access` grants — every user reaches
their own subtree regardless of whether they hold any specific
descendant's grant, matching ordinary Unix home-directory semantics; only
a non-%U share (no "self" concept) stays gated purely by `access`. A
descendant's own grant doesn't strictly need to appear in that union at
all once its own mount enforces it directly (`mw-fam` again) — it's
included anyway since it's harmless there and still load-bearing for a
plain local descendant with no mount of its own to enforce anything. A
single grant with both `owner` and `group` set contributes two tokens to
that list, one for each — both principals get in, since a real Unix
directory works the same way (either the owner or a group member can
open it).

Net effect: every remote-backed node with any `access` at all gets real,
symmetric enforcement over both Samba and SSH; a plain local node gets
real POSIX ownership/mode, also symmetric. Nothing is left in the
`stortree`-only-unreachable state a multi-principal grant used to fall
into before the schema stopped allowing it.

Ownership/mode are naturally idempotent to reapply with the same spec, so
`stortree_mounts` always recomputes and reapplies from the resolved facts
rather than diffing against previous runs.

A per-user grant's %U has to be expanded to real usernames before any of
this — `access_grant_usernames()` resolves an `access` object to the set
of usernames a `user-subdirs` node should get a folder for: an `owner`
grant (with or without `group` alongside it) always pins a single
folder to that one user; only a `group`-only grant expands into one
folder per member, against real group membership (interpretation call
#2), which needs `getent group` run against every group referenced
anywhere in an access grant first — same lookup `stortree_group_gids`
needs for the gid-owned-mount case above, just reading the member list
instead of the numeric id. `stortree_needed_groups()`/`needed_users()`
compute that set once each (covering both `server_subtrees`' own nodes
and `peer_dependencies`' — a peer-sourced descendant's grant expands the
exact same way, §2/§3, and unlike the old per-user-only scoping, both now
cover every node with a grant, not just per-user ones, since a plain
shared node's own `access.group`/`access.owner` still needs its
id resolved to own its mount); the `stortree_secrets` role — first among
the roles that need it in `site.yml`'s order (§8) — runs the `getent`
lookups and sets the `stortree_group_members`/`stortree_group_gids`/
`stortree_user_uids` facts from them, which `stortree_mounts` then
reuses rather than deriving its own (and risking missing a scope, which
is exactly what happened before this was consolidated: `stortree_mounts`
and the old, now-removed `stortree_acl` each ran their own `getent` loop,
scoped to `server_subtrees` only, so a client-only host with no
server_subtrees of its own — like `some-storage-gadget` in the worked
example — never resolved the groups its peer-sourced per-user shares
actually needed).

The per-user folder `access_grant_usernames()` says a `user-subdirs`
node needs — `home/jd`, say — is itself owned by that one real user, not
`stortree`: `user_container_paths()` derives, from the same
`server_subtrees`/`peer_dependencies` facts, every `<prefix>/<username>`
container any resolved grant anywhere under that prefix implies (deduped
by path — several sibling descendants resolving to the same user all
agree on one container, not one each). This is a real,
ordinary-home-directory-style folder — the user can create files
directly in it, not just reach whatever specific descendant grant
(`mw-fam`, say) happens to live inside it — deliberately more than the
bare traversal `access_mode()`'s public-execute bit alone would give an
*unrelated* user passing through an ancestor they don't otherwise own;
here, the container and the grant agree on exactly who it's for.

Getting there takes one of two different mechanisms, depending on what
the container's own ancestors look like, and `user_container_paths()`
tells `stortree_mounts` which one applies via `requires_slug` (`_nearest_
mount_slug()` against the resolved mount plan, checking not the
container's own path but its *staging path*, below): a genuinely local
container — no remote-backed ancestor anywhere above it (a top-level
subtree with `host` set and no `rclone.remote`, config-schema.md
"storing locally") — just gets `ansible.builtin.file`'s real `chown`/
`chmod` applied directly; real, native ownership, nothing more needed.
A container nested inside a remote-backed ancestor's own rclone mount
can't be chowned that way at all — confirmed against a live deployment,
where Ansible reported the chown as `changed`, but the next `stat`
showed the container's owner unmoved, still whatever the ancestor
mount's own single, uniform `--uid`/`--gid` says, for the simple reason
that a plain `chown()`/`chmod()` through that mount's FUSE layer has
nowhere real to persist a *different* value for just this one path.

For that second case, `stortree_mounts` renders a per-user "wrapper
mount" instead: an `rclone mount` using the `local` backend (source =
`STORTREE_USER_PREFIX + username`, a sibling of the container itself,
inside the very same remote-backed ancestor — ordinary content nothing
but the `stortree` account driving both mounts ever touches directly;
target = the container's own path), with that one user's real
`--uid`/`--gid`/`--dir-perms`. This works where a plain `chown` can't
because rclone's VFS-layer `--uid`/`--gid`/`--dir-perms`/`--file-perms`
override applies uniformly to whatever a mount presents, *regardless* of
backend or of what the underlying path natively supports — the same
mechanism that already lets any remote-backed node's mount present
`access`-derived ownership at all, on a backend with no native
permission concept of its own (S3, say). No second network connection
either: the wrapper's source is
already local (it's read through the outer mount, which already has
whatever network connection it needs), so `vfs-cache-mode: off` on the
wrapper — there's no network latency at this layer to hide, only
staleness risk against the real mount underneath it.

`access_grant_usernames()` says *who* gets a folder; `per_user_mount_path()`
says *where the real content actually lives*, and the two only disagree
for a `group`-only grant. An `owner` grant's one user is also where the
one real mount goes — nothing to share, since it was only ever for that
one person. A `group`-only grant's members, though, were always going to
get identical enforcement (the same gid, the same mode — `access_mode()`/
`access_group()` don't vary per member, only per node), so giving each of
them a full, independent rclone mount of the same remote path was pure
duplication: N members meant N separate FUSE processes and N separate VFS
caches of the exact same bytes, with no cache coherency between them
(one member's write wouldn't show up in another's cache until its own
`dir-cache-time`/`vfs-cache-*` expired). `stortree_plan_mounts` instead
resolves a `group`-only node's %U to one shared, hidden path
(`SHARED_MOUNT_SEGMENT` — `.mounts`, matching this tree's existing
dot-prefixed hidden-subtree convention, e.g. `.cache`) and mounts it
exactly once there, gid-owned exactly as before; every actual member's
own folder becomes a kernel bind mount onto that one real mount instead
of a second rclone mount. Not a real symlink: a symlink is a directory
entry the *target* directory's own backend has to be able to represent,
and not every remote backend can — a third-party SMB-backed remote, in
production, flatly refused with an I/O error trying to create one inside
it at all (SMB has no native symlink representation without extensions
this fleet's remote doesn't support), so every per-user symlink under a
top-level subtree backed by that remote failed identically, every run,
regardless of directory-creation ordering. A bind mount is a kernel VFS
relationship instead, entirely local to this host and independent of
what the underlying remote backend can store, so it works everywhere a
symlink sometimes couldn't. Each per-user path gets its own ordinary
directory first (stortree:stortree 0751, no `access` of its own — the
same plain default any unmodeled container path gets), then its own
`stortree-bind@` unit `mount --bind`s the real mount's path onto it —
enforcement still lives entirely at the one real mount, same uid/gid/mode
as always, and every member's bind mount reaches it identically. Samba
follows a bind mount exactly like it would any real directory — no
`wide links` consideration at all (that only applies to actual symlinks),
so no `smb.conf` change is needed for this to work either. A peer-sourced
`group`-only descendant (§2/§3) resolves the exact same way on the
*owning* host first, so a peer never has anything to source but that one
real, shared path — there's nothing at a per-member path on the owning
host's own disk for a `group`-only grant, bind mount included.

A per-user container's own wrapper mount (above) changes what a
descendant's bind-mount unit has to wait for, when that descendant lives
directly under a wrapped container: `mw-fam`'s `home/jd/mw-fam` used to
just nest inside the outer remote-backed mount directly; once `home/jd`
itself is a separate wrapper mount, `home/jd/mw-fam` nests *inside that
wrapper's own presented tree* instead, so `mw-fam`'s bind-mount unit has
to come after the wrapper mount, not just the outer one. `mw-fam` itself
is completely unchanged by this — same bind mount, same gid ownership,
same everything — only *when* its unit is allowed to start moves; the
`stortree-bind@`/`stortree-mount@` unit templates both check, ahead of
whatever `plan_mounts()` itself computed as `requires_slug`, whether the
entry's own immediate parent is a container with a wrapper mount of its
own, and prefer that dependency when one exists. This preserves the
"real ownership stacks on top" pattern (§6) one level deeper: `mw-fam`'s
own distinct group ownership shadows whatever the wrapper mount
would've shown at that exact path, exactly as it already shadowed the
outer mount before the wrapper existed — rclone's uid/gid override is
uniform across a whole mount (no way to carve out one nested path with a
different owner from inside the same mount), so this is the only way
`mw-fam`'s ownership and the container's can coexist at all.

The optional `sshd_config` fragment (`stortree_sshd`, only runs when
`stortree/sshd_config` is present in the repo) is templated to
`/etc/ssh/sshd_config.d/stortree.conf`, included via a drop-in, with
`sshd` reloaded on change. See the config-layout note above and
config-schema.md for the `Match Group`/`ForceCommand` use case this
exists for.

### 7. Peer trust and the `stortree` service account

`stortree_common` runs first (after `stortree_facts`) and creates a local
Unix account, `stortree`, on every host that needs one — a plain local
account, not resolved via LDAP/SSSD, since it exists purely to own mount
processes and any peer-to-peer SSH relationship, independent of any human
identity. It also ensures `/srv/stortree` (the fixed mount root, §1) and
`/etc/stortree` (rendered per-host state: filtered `rclone.conf`, nothing
else) exist with the right ownership/permissions.

Where the old design needed an operator-run `stortree join` handshake to
establish SSH trust between a new host and a "root" host, that step
disappears here: the control node already has SSH reach to every host (via
Ansible's own inventory/connection config), and peer-to-peer trust between
two *storage* hosts (needed only when a peer dependency, §1, actually
requires one host to sftp into another at mount time) is provisioned
declaratively by `stortree_peer_trust`, in the same play that resolves the
tree:

1. For every host that is the *serving* side of at least one peer
   dependency, ensure its `stortree` account has an SSH keypair
   (`community.crypto.openssh_keypair`, idempotent — a no-op if the key
   already exists).
2. Read that host's public key back (`slurp` + `set_fact`).
3. For each peer dependency, `delegate_to` the *owning* host and add the
   serving host's public key to that host's `stortree`
   `authorized_keys` (`ansible.posix.authorized_key`), scoped with a
   `command=` restriction if you want the connection limited to `sftp`
   (recommended — pair with `internal-sftp`/`ForceCommand` via the
   `sshd_config` fragment, §6).

Every host that doesn't own every top-level subtree in `config.yml` is
the *serving* side of at least one peer dependency unconditionally now —
its own top-level-subtree client mounts (§1), one per top-level subtree
it doesn't own and isn't opted out of (config-schema.md "Per-client mount
opt-out") — so this provisions on every `ansible-playbook` run for the
whole fleet, not only where a `samba:` block is in play. Because Samba
sharing is universal
(§1) on top of that, the set of hosts needing this provisioned can extend
further still to ones with no server subtrees of their own, and even ones
with no mention in `config.yml` at all (present only in
`all_hosts`/the inventory) — a client-only or entirely-unnamed host
peer-dependent on every owner of a `samba:`-configured node's descendants
ends up as the *serving* side here too, and a subtree-owning host can
accumulate one `authorized_keys` entry per peer-dependent host rather
than just per Samba-serving ancestor.

This runs on every `ansible-playbook` invocation, so it's kept current
automatically as peer dependencies change — adding, moving, or removing a
peer-dependent subtree in `config.yml` re-provisions trust the next time
the playbook runs, with no separate bootstrap command and nothing
onboarding-specific for the operator to remember.

### 8. The playbook

`playbooks/site.yml` is the only entrypoint that changes state. It runs
the roles above, in order, against every host in `inventory/hosts.yml`:

```
stortree_facts → stortree_common → stortree_identity → stortree_peer_trust
→ stortree_secrets → stortree_mounts → stortree_samba
→ stortree_pam_smbpass → stortree_sshd
```

(`stortree_facts` computes `resolve()` for every host up front so
`stortree_peer_trust` can look up an owning host's account before that
host's own play iteration necessarily runs; `stortree_sshd` only executes
its tasks when `stortree/sshd_config` is present.)

Operators drive the whole system with ordinary `ansible-playbook`
invocations — there is no separate CLI to install or learn:

- **Apply the whole tree**: `ansible-playbook playbooks/site.yml`.
  Idempotent — re-running converges to the same end state rather than
  accumulating drift, the same guarantee the old `apply`/`reconcile` split
  was designed to provide, now just Ansible's normal behavior.
- **Apply to one host** (e.g. after adding it to `inventory/hosts.yml`, the
  direct replacement for the old `join` flow): `ansible-playbook
  playbooks/site.yml --limit storage-node-charlie`. Because peer trust
  (§7) and secrets (§3) are both re-derived from the current tree on every
  run, bringing a new host in is exactly this one command — no separate
  bootstrap step.
- **Apply just one concern** (e.g. after only editing `access` rules):
  `ansible-playbook playbooks/site.yml --tags mounts`. Every role is
  tagged with its own name for this.
- **Check without changing anything**: `ansible-playbook playbooks/site.yml
  --check --diff`.
- **Status**: `ansible-playbook playbooks/status.yml` — a read-only play
  that gathers resolved role, active mount unit states, Samba share
  status (`smbstatus`), and SSSD/PAM sanity checks, and prints a per-host
  summary. No state-changing modules.

If a host is unreachable, that one host's tasks fail and Ansible reports
it while continuing (or halting, depending on `--limit`/strategy) — same
retry-by-rerunning story as the old `apply`, just Ansible's native
failure handling instead of bespoke retry logic.

### 9. Testing (Molecule + Docker)

**Dependencies**, gathered here from the roles above for reference:

- **Control node**: `ansible`, plus the `ansible.posix` collection
  (`ansible.posix.authorized_key` in §7) and the `community.crypto`
  collection (`community.crypto.openssh_keypair` in §7).
- **Every managed host**: `rclone` (§2), `samba` (§4), `sssd` (§5), and
  `samba-common-bin`/`libpam-modules` (`smbpasswd` and `pam_exec.so`,
  §5) — existing, well-known Linux storage/identity tooling the roles
  configure rather than reinvent. Ownership/mode (§6) is plain
  `ansible.builtin.file`/rclone mount flags — no separate `acl` package
  needed anywhere.
- **Test harness only** (not needed in production): Molecule, Docker, and
  the mocked-dependency containers below.

A [Molecule](https://ansible.readthedocs.io/projects/molecule/) scenario
per role for fast, isolated role tests, plus one multi-host `full-tree`
scenario that exercises the whole flow — config resolution, mounts,
ownership/mode, Samba, SSSD/LDAP, and the peer-dependency mechanism (§1,
§7) — without real hardware or the real LDAP/storage backends.

**Topology**: one Docker container per simulated host — at least two
hosts, matching the two-host shape used in the example config in
[docs/config-schema.md](config-schema.md) — each running systemd as PID 1
(`ExecStart=/lib/systemd/systemd`, `/sys/fs/cgroup` mounted in,
`--tmpfs /run --tmpfs /run/lock`), since role verification depends on real
systemd unit management (§2), not just a supervised process. rclone's
FUSE mounts need `--cap-add SYS_ADMIN --device /dev/fuse` (or
`--privileged`) on each host container — Docker's default seccomp/AppArmor
profile blocks FUSE otherwise. Molecule's own Docker driver runs
`ansible-playbook` against these containers exactly as it would against
real hosts, using the same roles and `converge.yml` mapped 1:1 onto
`playbooks/site.yml`.

**Mocked dependencies**:

- **LDAP**: a throwaway OpenLDAP container seeded with a couple of
  `posixAccount`/`posixGroup` entries mirroring the example config in
  [docs/config-schema.md](config-schema.md) — enough to exercise SSSD
  (§5) without standing up the real directory product.
- **Third-party remotes**: `storagebox`/`some-remote` become
  `atmoz/sftp` containers — same `sftp` rclone backend type as
  production, just pointed at a throwaway container instead of the real
  storage box/Synology, so the mount path under test matches production's
  transport, not just its data.
- `some-gcs-bucket` (GCS) is the one remote the harness can't meaningfully fake
  locally — either point it at a real (test) GCS bucket if credentials
  are available, or skip/stub that specific subtree when running fully
  offline.

**What this validates**: config resolution end to end (via Molecule's
`verify.yml` plus the filter plugin's own `pytest` unit tests, which need
no containers at all), rclone mount reconciliation, Samba share behavior
(via `smbclient` from a plain client container), SSSD+LDAP identity
resolution, the `pam_smbpass` sync flow, `access`-derived ownership/mode
enforcement, and peer trust provisioning across the two host containers.

**What it doesn't**: systemd-in-Docker isn't identical to bare-metal
systemd (cgroup quirks, no real reboot/persistence story), there's no
real network latency/partition behavior, and it says nothing about the
real storage box/Synology/GCS's actual auth quirks. A pass here means
"the logic is right," not a substitute for a staging run (`ansible-playbook
site.yml --check --diff` against real hosts, then a real apply) before
bringing real hosts into the tree.

The build sequence used to implement this spec, and current implementation
status, are tracked separately in [plan.md](plan.md) rather than here —
this document describes the target design, not the work of getting there.

## Design decisions / future work

- **Control-node single point of failure** — since `config.yml`/`ldap.yml`/
  `rclone.conf` (and the vault password) only live on the control node
  (or wherever that checkout/repo lives), losing it takes out the ability
  to edit or re-apply config (existing mounts/shares on managed hosts
  keep running under whatever they last converged to; they just can't
  pick up new changes). This is accepted as in-scope for the current
  design — a single source-of-truth checkout keeps config resolution and
  secrets handling simple. Backing up the `stortree/` directory (and the
  vault password, stored separately) like anything else in the tree is
  enough to recover — note that a backup carries the same credentials as
  the live files even vaulted (the vault password itself is the thing
  that must not travel with it), so securing the backup destination and
  the vault password independently is on the operator, outside this
  project's scope; a shared/replicated control node (e.g. the repo in a
  team's existing git remote, playbook runs from CI with the vault
  password in a secrets manager) is a natural extension, not a blocker
  for the current design.
