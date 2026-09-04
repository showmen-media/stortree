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
deliberately **not** run here. What was run and passed:

- `pytest tests/` — `resolve()`/`filter_rclone_conf()` unit tests,
  including the mutual-peer-dependency, client-only-host, and
  unnamed-inventory-host cases §1 calls out explicitly.
- `ansible-playbook playbooks/site.yml --syntax-check` and the same for
  `status.yml`, against the `*.example` config.
- `ansible-lint` / `yamllint` over `roles/` and `playbooks/`.

Not run: `molecule test` for any role, or the `full-tree` scenario. The
scenario files exist and are believed correct but are unexercised —
before trusting this against real hosts, run at least the `full-tree`
scenario (`cd molecule/full-tree && molecule test`, or per-role via `cd
roles/<role> && molecule test`) somewhere Docker capacity isn't shared
with other workloads, then a staging pass (`ansible-playbook site.yml
--check --diff` against real hosts, then a real apply) per spec.md §9's
own caveat about what Molecule-in-Docker does and doesn't prove.
