# stortree — build plan

Tracks how [spec.md](spec.md) gets implemented, and current status. Section
references below (`§1`, `§2`, ...) are spec.md's Architecture sections.

## Status

Everything through phase 9 below is implemented: `resolve()`, all ten
roles, both playbooks, and Molecule scaffolding for a per-role `default`
scenario plus one multi-host `full-tree` scenario. What's **not** done is
running `molecule test`/`molecule converge` against real Docker containers
— see "What's verified" below for exactly what has and hasn't been
exercised.

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

Docker on the machine this was built on is in daily use for unrelated
services, so `molecule test`/`molecule converge` (which needs privileged,
systemd-in-Docker containers plus throwaway LDAP/sftp containers, §9) was
deliberately **not** run here. Everything else now runs on every push
via `.github/workflows/ci.yml`, rather than by hand:

- `pytest` — three layers, all pure and hostless:
  - `resolve()`/`filter_rclone_conf()` and the rest of
    `filter_plugins/stortree.py`, including the mutual-peer-dependency,
    client-only-host, and unnamed-inventory-host cases §1 calls out
    explicitly. Statement *and* branch coverage of that module is at
    100%, enforced by a `fail_under` floor in `pyproject.toml`.
  - the `FilterModule` mapping itself (`tests/test_filters.py`) — that
    every name a role pipes through is registered, and vice versa. A
    typo there breaks every playbook while leaving the resolution tests
    green, which is exactly what it used to do.
  - the roles' Jinja templates (`tests/test_templates.py`) — the three
    systemd unit templates, `smb.conf.j2` and `sssd.conf.j2`, rendered
    through ansible-core's own filters/tests/`AnsibleUndefined` against
    real `plan_mounts()`/`user_container_paths()` output. This is the
    layer where a wrong `PartOf=` or a missing `--uid` becomes a mount
    that silently serves the wrong thing, and it previously had no
    coverage outside the unrun Molecule scenario.
  - drift guards (`tests/test_repo_consistency.py`) — the worked example
    exists in three copies (unit fixture, `stortree/config.yml.example`,
    Molecule fixture) and `molecule/full-tree/converge.yml` claims to be
    1:1 with `playbooks/site.yml`; both are now checked rather than
    maintained by hand, along with the repo-hygiene rule above.
- `ansible-playbook playbooks/site.yml --syntax-check` and the same for
  `status.yml`, run against config copied from the `*.example` files by
  the same commands README.md gives an operator — so a stale example
  fails CI rather than someone's first run.
- `ansible-lint` (clean at the `production` profile) and `yamllint
  --strict` over the whole repo. Both were run by hand at the time this
  section was first written and had since drifted red; the skips that
  remain are listed with their rationale in `.ansible-lint`.
- `shellcheck` over `pam-smbpass-sync.sh`, which runs as root inside the
  PAM stack with a plaintext password on stdin.

Still not run: `molecule test` for any role, or the `full-tree`
scenario. The scenario files exist, are checked for internal consistency
by `tests/test_repo_consistency.py`, and are believed correct, but
remain unexercised — before trusting this against real hosts, run at
least the `full-tree` scenario (`cd molecule/full-tree && molecule
test`, the manually dispatched `.github/workflows/molecule.yml`, or
per-role via `cd roles/<role> && molecule test`) somewhere Docker
capacity isn't shared with other workloads, then a staging pass
(`ansible-playbook site.yml --check --diff` against real hosts, then a
real apply) per spec.md §9's own caveat about what Molecule-in-Docker
does and doesn't prove.
