# stortree — build plan

How [spec.md](spec.md) got built: the judgment calls made where the spec
left something implicit, the phases the work was done in, and what is
and isn't verified. Section references below (`§1`, `§2`, ...) are
spec.md's Architecture sections.

## Status

Every phase below is done, and each says so. The list is kept as the
record of how the project was built, not as a tracker to update — what
is true of the code *today* is whatever `pytest`, `ansible-lint` and
`yamllint --strict` say when you run them, and `git log` is the account
of how it changed. The one thing no check here covers is in "What's
verified" below, and that gap is real rather than pending.

## Repo hygiene

This repo is meant to be publicly shareable, so no real hostnames,
credentials, or topology live in it:

- `stortree/*.yml`, `stortree/rclone.conf`, `stortree/sshd_config`, and
  `inventory/hosts.yml` are gitignored; only `*.example` counterparts
  (the fictional example from config-schema.md) are tracked.
- `ldap.yml`/`rclone.conf` get `ansible-vault`-encrypted on top of that
  once an operator's checkout has real values (spec.md "Config layout").
- An operator's real `stortree/` tree is expected to live in a separate,
  private checkout/repo — this repo is only the roles/filter
  plugin/playbooks (spec.md "Config layout").

## Open interpretation calls

spec.md is thorough but leaves a few things implicit. Decisions made
during implementation, each also marked with a comment at its point of
implementation:

1. **Dotted `access` shorthand with no `permissions:`** (e.g.
   `access.group: Media Production`, `access.owner: jd` — every shorthand
   example in config-schema.md omits it, and no default is stated).
   Resolved to: default `rwx` (full control) when `permissions` is absent
   on a shorthand grant — every example use is a user/group getting their
   own private subtree, where full access is the sensible default.
   Exposed as `DEFAULT_ACCESS_PERMISSIONS` in `filter_plugins/stortree.py`
   so it's a one-line change if wrong.
2. **`user-subdirs` per-user folder existence.** config-schema.md says a
   descendant with an `access` restriction (`sys-configs`, `access.owner:
   jd`) "only shows up inside jd's own per-user folder, not everyone
   else's" — read literally, that's existence-gating, not just ownership
   on an always-created folder. Resolved to: `resolve()` stays pure (no
   LDAP I/O, per §1) and returns each `user-subdirs` descendant tagged
   with its resolved `access` grant; `stortree_mounts`, delegated to the
   resolved host, expands an `access.group` grant into concrete usernames
   via `getent group` at apply time (group membership is host-local via
   SSSD, not visible to a pure function) and only creates each descendant
   under the per-user folders of users actually granted access to it. An
   `access.owner` grant (with or without `group` alongside it) instead
   pins a single folder to that one user directly, no `getent` needed for
   the expansion itself (§6 "Access enforcement" covers both cases, plus
   why `access` is always a single object rather than a list of grants —
   a later revision of this same interpretation call, once it became
   clear a remote-backed node can never carry more than one principal's
   worth of real enforcement anyway). Revisited once more for a
   remote-backed, `group`-only descendant specifically: every member's
   enforcement is identical (one gid, one mode — never varies per
   member), so mounting the same remote path once per member was pure
   duplication (N redundant rclone procs/VFS caches of the same
   content). `stortree_plan_mounts` now resolves that case to one real,
   shared mount plus one bind mount per member instead (spec.md §6,
   `per_user_mount_path()` in `filter_plugins/stortree.py`) — a real
   symlink can't do this job when the member's folder lives on a
   remote-backed node whose backend can't represent one (e.g. an SMB
   share), so the fan-out is a kernel bind mount instead. An `owner`
   grant is unaffected, since it was already exactly one real mount.
3. **Molecule shared `full-tree` scenario location.** Not specified in
   spec.md. Resolved to: `molecule/full-tree/` at repo root (the
   conventional layout for a cross-role scenario, vs. each role's own
   `roles/<role>/molecule/default/`).
4. **A `user-subdirs` container's own ownership.** Not specified in
   spec.md at all — every existing mechanism enforces a *descendant's*
   `access` grant, never the per-user container path itself
   (`home/jd`), which every task in `stortree_mounts` left at the plain
   `stortree:stortree` default. Resolved to: `user_container_paths()`
   (`filter_plugins/stortree.py`) derives the one real user each
   container belongs to, and `stortree_mounts` gives it real ownership —
   but *how* depends on what's above it, since that's the one thing this
   call couldn't just apply uniformly: a plain `chown`/`chmod` for a
   container nested under a genuinely local top-level subtree (`host`
   set, no `rclone`), since real, native Unix ownership already works
   there; a dedicated per-user "wrapper mount" (`rclone mount`'s `local`
   backend, source = a `stortree-user-<name>` sibling of the container,
   target = the container itself, that user's real `--uid`/`--gid`/
   `--dir-perms`) for one nested inside a remote-backed ancestor's own
   rclone mount instead, discovered the hard way against a live
   deployment: a plain `chown` there is accepted by the FUSE layer
   (Ansible reports `changed`) but never actually persists, since a
   single rclone mount can only ever present one uniform owner for
   everything under it. That same constraint is also why a sibling
   descendant with its own distinct ownership (a `group`-only grant's
   bind mount, e.g. `mw-fam`) now has to wait for the wrapper mount too,
   stacking its own real ownership on top exactly as it already stacked
   on the outer mount before the wrapper existed (spec.md §6).

5. **What a key the schema doesn't define should do**, and **what
   counts as a `samba:` block.** Neither is stated anywhere. Both were
   resolved the same way — silently ignoring input is the worst
   available option for this particular program — after finding that
   `resolve()` read the keys it recognized and discarded everything
   else. Resolved to: `_validate_node()` in
   `filter_plugins/stortree.py` rejects any key the schema doesn't
   define, at the node level and inside `rclone:`/`access:`/`samba:`/
   `client-defaults:`/`clients:`, naming the node and suggesting the
   near match (docs/config-schema.md "Unknown keys are an error"). The
   argument is the failure direction: every typo tested resolved to
   something plausible and wrong, and several of them wrong in the
   direction of *more* access or *more* peer trust than was written —
   a misspelled `client-defaults` re-enables a subtree on every host
   in the fleet and provisions the SSH trust for it, a misspelled
   `access.group` drops the grant and leaves the path at its
   permissive default. Failing at `resolve()` costs a run; the
   alternative costs a silently wrong deployment nobody looks at
   again. This subsumes the three-segment dotted key
   (`rclone.args.vfs-cache-mode:`), which expands to a literal
   `rclone.args` key under the documented last-dot rule and used to be
   dropped without trace. `samba:` was then the same question one
   level down: `_normalize_samba()` treats the key's *presence* as the
   marker, so bare `samba:`, `samba: {}` and `samba: true` all mean
   "share with the defaults" and only `samba: false` opts out. All
   three used to mean the opposite — the first two resolved to no
   share at all, and `samba: true` crashed `resolve()` outright with an
   `AttributeError` from inside the share-building loop.

6. **What a Samba share is called.** spec.md §4 describes the stanza's
   contents and never names it, and config-schema.md had no key for it
   — the name was an implementation detail of `smb.conf.j2`, which
   folded the node path into a section header inline. Resolved to: the
   fold stays the default (nothing an existing tree exports changes
   name), an optional `samba.name` overrides it per node, and both now
   resolve in `_share_name()`/`_normalize_samba()`
   (`filter_plugins/stortree.py`) so the name is a resolved fact rather
   than something the template invents — which is also what lets
   `_validate_share_names()` reject two nodes claiming one name, a
   collision `smb.conf` otherwise resolves by keeping the first stanza
   and dropping the second. An operator-set name is validated against
   the same alphabet the fold produces rather than being sanitized
   silently (a name is typed into a mount command, so one that wouldn't
   survive the fold is a mistake, not something to rewrite behind their
   back), and `global`/`homes`/`printers` are rejected as reserved:
   `[global]` in particular would merge into the generated global block
   and rewrite fleet-wide settings instead of adding a share.

## Phased build plan

0. Repo skeleton: `.gitignore`, `requirements.txt`/`requirements.yml`,
   `ansible.cfg`, `*.example` config files, empty role skeletons
   (`tasks/main.yml`, `meta/main.yml`) for every role, `filter_plugins/
   stortree.py` stubbed out, Molecule scaffolding for a `default`
   scenario per role. — **done**
1. `resolve()` (§1) — pure functions in `filter_plugins/stortree.py`,
   unit-tested with `pytest` against fixture configs, no Ansible runtime
   invoked. `stortree_facts` wraps it into `set_fact`. — **done**
2. `stortree_common` (service account, `/srv/stortree`, `/etc/stortree`)
   and `stortree_mounts` (§2). — **done**; not yet verified against a real
   remote or in a live Molecule run (see "What's verified").
3. `stortree_samba` (§4) from resolved server subtrees. — **done**
4. `stortree_identity` (§5). POSIX attribute exposure is a requirement of
   the LDAP server, not something this phase can verify on its own — see
   §5's own caveat. — **done**
5. `stortree_mounts` extended (§6) to set ownership/mode from resolved
   `access` blocks + SSSD groups/users — no separate ACL role; a
   dedicated `stortree_acl` role existed for a while but was removed once
   `access` became a single object and ownership/mode could just be set
   at directory-creation time, uniformly for local paths and remote
   mounts alike. — **done**
6. `stortree_pam_smbpass` (§5), plus `stortree_sshd` (§6, only runs when
   the optional file is present). — **done**
7. `stortree_secrets` (§3): filtered per-host `rclone.conf` rendering.
   — **done**
8. `stortree_peer_trust` (§7): keypair provisioning + cross-host
   `authorized_keys`. — **done**; the genuine two-host peer-dependency
   exercise this phase calls for is the `full-tree` Molecule scenario,
   scaffolded but not run (see below).
9. `playbooks/status.yml`, the `full-tree` Molecule scenario, and
   [runbook.md](runbook.md) covering the operator commands in §8.
   — **done**

## What's verified

Everything in this repo is checked without a running fleet, and one
thing isn't checked at all.

Run by hand on a checkout (and by `ci.yml` on the unmerged
`github-workflows` branch, which is why this says "by hand"):

- `pytest` — the pure resolution layer, the `FilterModule` mapping, the
  roles' real Jinja templates rendered through ansible-core's own
  filters, and a set of drift guards over the things this repo keeps in
  more than one place. Each `tests/*.py` opens with what it covers and
  why; that's the description, not this list. Statement *and* branch
  coverage of `filter_plugins/stortree.py` is at 100%, enforced by a
  `fail_under` floor in `pyproject.toml`, so a new branch has to arrive
  with the test that exercises it.
- `ansible-playbook playbooks/site.yml --syntax-check`, and the same for
  `status.yml`, against config copied from the `*.example` files by the
  same commands README.md gives an operator — so a stale example fails
  before someone's first run does.
- `ansible-lint` (clean at the `production` profile) and `yamllint
  --strict` over the whole repo. The skips that remain are listed with
  their rationale in `.ansible-lint`.
- `shellcheck` over `pam-smbpass-sync.sh`, which runs as root inside the
  PAM stack with a plaintext password on stdin.

**Not run: Molecule, in any scenario.** `molecule test`/`molecule
converge` needs privileged systemd-in-Docker containers plus throwaway
LDAP/sftp containers (§9), and Docker on the machine this was built on
is in daily use for unrelated services. The scenario files exist and are
checked for internal consistency by `tests/test_repo_consistency.py`,
but nothing has ever applied a role to a container, mounted a real
remote, or exercised a genuine two-host peer dependency.

That is the gap to close before trusting this against real hosts: run at
least the `full-tree` scenario (`cd molecule/full-tree && molecule
test`, or per-role via `cd roles/<role> && molecule test`) somewhere
Docker capacity isn't shared with other workloads, then a staging pass
(`ansible-playbook site.yml --check --diff` against real hosts, then a
real apply) — see spec.md §9's own caveat about what Molecule-in-Docker
does and doesn't prove.
