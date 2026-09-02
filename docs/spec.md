# stortree — development plan

## Goal

An Ansible project, run from a single control node (an operator's
workstation or a CI runner — not one of the storage hosts), that turns a
declarative tree (`config.yml` + `ldap.yml` + `rclone.conf`) into:

- rclone mounts (as a host's own client, and/or as the server backing a
  Samba share),
- Samba shares with POSIX-consistent ACLs,
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
  config.yml         # the directory tree: hosts, clients, subdirs, ACLs (non-secret)
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
  stortree_mounts/         # rclone mount systemd units (§2)
  stortree_acl/            # setfacl from resolved access rules (§6)
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
- **Client mounts** — a peer-sftp mount of the tree root, sourced from
  the host that actually owns it (`host:`'s resolved value, "root_host"
  below) rather than the root's own third-party `rclone.remote` — for
  every host in `all_hosts` that isn't itself a server subtree's resolved
  `host`. This is the same peer-sourcing rule §1 already applies to any
  samba descendant a host doesn't own, just for the one root-level piece
  that isn't a node in the tree at all (root is the inheritance anchor,
  not a mountable node of its own — see "Node inheritance",
  config-schema.md): a client never holds direct credentials to the root
  remote itself, only an SFTP hop into root_host's own already-mounted
  `/srv/stortree` (provisioned by `stortree_peer_trust`, §7, exactly like
  any other peer dependency). If the host has an entry under the root
  `clients:` map, `clients.<hostname>.rclone.args` merges over
  `client-defaults`; otherwise it gets `client-defaults` verbatim — a
  `clients:` entry is only ever a per-host override, never a prerequisite
  for being a client (see "Every inventory host participates",
  config-schema.md). A root with no `rclone.remote` of its own has
  nothing to peer for — the client still gets its local root directory
  created, just no mount and no peer dependency for it. Unlike a samba
  peer dependency, this one implies no Samba behavior of its own — it
  exists purely so a client's local tree has real content — but it does
  mean every non-root host now needs `stortree_peer_trust` (§7) to reach
  root_host, which the role already handles the same way as any other
  peer dependency.
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
started`). Every entry in the plan, mount or not, still gets its local
directory created (`ansible.builtin.file`) — a remote-less entry is a
plain directory that has to exist (nested inside a mounted ancestor, or
as real local storage on its own resolved host if not), it just gets no
rclone unit.

Every peer dependency (§1) becomes one of these mount entries too, not
just the root client mount — a samba descendant this host doesn't own is
real data its own local tree still has to contain, sourced directly from
whichever host actually owns it (mesh, one peer relationship per distinct
owning host, never funneled through root_host). The one exception is the
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

A per-user peer dependency's %U template is expanded here the same way a
per-user server_subtrees node's is — one mount per user actually granted
access, using `stortree_group_members` (see §6 for where that fact comes
from).

Ansible's own idempotence covers what a hand-rolled reconciler would
otherwise have to implement: `template` only rewrites a unit file when its
rendered content changes, and `systemd_service` only touches
enabled/running state when it's out of sync — so a stale mount that's no
longer in the resolved tree still needs an explicit cleanup step (the role
also lists `/etc/systemd/system/stortree-mount-*.service`, diffs it
against the currently-resolved unit names, and removes/`daemon-reload`s
any that are no longer wanted). systemd itself handles restart policy,
resource limits, and logging (journald) for the `rclone mount` process
once the unit is in place.

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
path, same as any other resolved mount. The root-level peer dependency
behind every client mount (§1) synthesizes the same way, just with an
empty local path — the section's `path` is root_host's own
`/srv/stortree`, and the client's mount unit references the synthesized
section's bare name (mounting that remote's own root, same convention as
a bare `rclone.remote` section name) instead of the tree's third-party
`rclone.remote` value. A per-user peer dependency's %U gets expanded into
one section per actual user here too, exactly the way `stortree_mounts`
(§2) independently expands the same entry into one mount per user — both
have to agree on the expanded (not templated) path to name the section
the same way, so `stortree_secrets` is the one role that resolves
`stortree_group_members` (§6) fresh via `getent`, first in `site.yml`'s
order among the roles that need it; `stortree_mounts`/`stortree_acl`
reuse that same fact rather than each re-deriving their own.

### 4. Samba layer

The `stortree_samba` role generates `smb.conf` share stanzas from every
`samba:`-configured node resolved for the current host (§1) — which,
since Samba sharing is universal, means every host gets a stanza for
every such node in the tree, not only the ones whose subtrees it happens
to own: path, subpath templates (`%U` for the `home` per-user pattern),
and `valid users`/`write list` derived from the resolved `access` rules
once those are mapped to real POSIX groups (§5). A host assembles the
node's local path the same way whether it's the resolved owner or
sourcing peer data (§1/§3) — Samba itself never needs to know which.
Sets `nt acl support = yes`, `map acl inherit = yes`,
`inherit permissions = yes` so Samba reads and respects the same POSIX
ACLs applied in §6, rather than maintaining a second, parallel ACL system
that can drift from the first.

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

### 6. ACL enforcement & SSH access

`access` blocks in `config.yml` (list form: `- group: ... permissions: ...`,
or the dotted shorthand `access.group` / `access.user`) resolve against
the SSSD-provided groups/users, then `stortree_acl` applies them with
`ansible.posix.acl` (both default and effective ACLs, recursive) directly
on the physical path — on whichever host actually serves that subtree, or
peer-sources it (§1/§2), same as any other resolved mount. Because this
is POSIX-level, it governs access uniformly whether the path is reached
via Samba, SSH, or a local process. ACLs are naturally idempotent to
reapply with the same spec, so the role always recomputes and reapplies
from the resolved facts rather than diffing against previous runs.

A per-user grant's %U has to be expanded to real usernames before any of
this — `access_grant_usernames()` resolves a group grant against real
group membership (interpretation call #2), which needs `getent group`
run against every group referenced anywhere in a per-user access grant
first. `stortree_needed_groups()` computes that set once (covering both
`server_subtrees`' own per-user nodes and `peer_dependencies`' — a
peer-sourced per-user descendant expands the exact same way, §2/§3); the
`stortree_secrets` role — first among the roles that need it in
`site.yml`'s order (§8) — runs the `getent` lookup and sets the
`stortree_group_members` fact from it, which `stortree_mounts` and this
role both then reuse rather than each deriving their own (and risking one
missing a scope the others cover, which is exactly what happened before
this was consolidated: `stortree_mounts` and `stortree_acl` each ran
their own `getent` loop, scoped to `server_subtrees` only, so a
client-only host with no server_subtrees of its own — like
`some-storage-gadget` in the worked example — never resolved the groups
its peer-sourced per-user shares actually needed).

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

Every non-root host is the *serving* side of at least one peer dependency
unconditionally now — its own root client mount (§1) — so this
provisions on every `ansible-playbook` run for the whole fleet, not only
where a `samba:` block is in play. Because Samba sharing is universal
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
→ stortree_secrets → stortree_mounts → stortree_acl → stortree_samba
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
  `ansible-playbook playbooks/site.yml --tags acl`. Every role is tagged
  with its own name for this.
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
  (`ansible.posix.acl` in §6, `ansible.posix.authorized_key` in §7) and the
  `community.crypto` collection (`community.crypto.openssh_keypair` in
  §7).
- **Every managed host**: `rclone` (§2), `samba` (§4), `sssd` (§5), `acl`
  (§6), and `samba-common-bin`/`libpam-modules` (`smbpasswd` and
  `pam_exec.so`, §5) — existing, well-known Linux storage/identity
  tooling the roles configure rather than reinvent.
- **Test harness only** (not needed in production): Molecule, Docker, and
  the mocked-dependency containers below.

A [Molecule](https://ansible.readthedocs.io/projects/molecule/) scenario
per role for fast, isolated role tests, plus one multi-host `full-tree`
scenario that exercises the whole flow — config resolution, mounts,
Samba, ACLs, SSSD/LDAP, and the peer-dependency mechanism (§1, §7) —
without real hardware or the real LDAP/storage backends.

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
resolution, the `pam_smbpass` sync flow, ACL enforcement, and peer trust
provisioning across the two host containers.

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
