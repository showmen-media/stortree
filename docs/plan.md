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
   `stortree:stortree` default. Resolved to: `_plan_user_containers()`
   (`filter_plugins/stortree.py`) derives the one real user each
   container belongs to, and `stortree_mounts` gives it real ownership —
   but *how* depends on what's above it, since that's the one thing this
   call couldn't just apply uniformly: a plain `chown`/`chmod` for a
   container nested under a genuinely local top-level subtree (`host`
   set, no `rclone`), since real, native Unix ownership already works
   there; a **presentation mount** (`bindfs`, source = the same path
   under `stortree_remotes_root`, target = the node itself, the
   resolved grant as `-u`/`-g`/`-p`) for one nested inside a
   remote-backed ancestor's own rclone mount instead, discovered the
   hard way against a live deployment: a plain `chown` there is accepted
   by the FUSE layer (Ansible reports `changed`) but never actually
   persists, since a single mount can only ever present one uniform
   owner for everything under it.

   Generalised since: the identical problem applies to *any* node with
   an `access` grant and no `rclone.remote` of its own, not just a
   per-user container, and the same two-layer mechanism now covers
   both -- the raw rclone mount moved out of the visible tree into
   `stortree_remotes_root`, and a bindfs mount composes it back in under
   the resolved grant. That same constraint is also why a sibling
   descendant with its own distinct ownership (a `group`-only grant's
   bind mount, e.g. `mw-fam`) has to wait for the presentation too,
   stacking its own real ownership on top exactly as it already stacked
   on the outer mount before (spec.md §6).

5. **What a key the schema doesn't define should do**, and **what
   counts as a `samba:` block.** Neither is stated anywhere. Both were
   resolved the same way — silently ignoring input is the worst
   available option for this particular program — after finding that
   `resolve()` read the keys it recognized and discarded everything
   else. Resolved to: `_validate_node()` in
   `filter_plugins/stortree.py` rejects any key the schema doesn't
   define, at the node level and inside `rclone:`/`access:`/`samba:`/
   `peer-defaults:`/`peers:`, naming the node and suggesting the
   near match (docs/config-schema.md "Unknown keys are an error"). The
   argument is the failure direction: every typo tested resolved to
   something plausible and wrong, and several of them wrong in the
   direction of *more* access or *more* peer trust than was written —
   a misspelled `peer-defaults` re-enables a subtree on every host
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

7. **Whether "Samba sharing is universal" should be absolute.** The
   spec states universality as a property, not as a default, and
   nothing offered a way out of it. Resolved to: it stays the default
   (no existing tree changes behaviour), but `stortree_samba_hosts`
   (roles/stortree_facts/defaults/main.yml) narrows it. The argument
   for making it adjustable at all is the cost, which is easy to miss
   because the visible artifact — an `smb.conf` stanza — is the free
   part: a host exporting a share it doesn't own peer-mounts that
   content, so it pays an rclone process and a VFS cache per
   peer-sourced path, and a cold read traverses SMB → sftp → the
   owner's rclone → the third-party remote. N exporting hosts hold N
   caches of identical bytes. That is the same duplication call #2
   above went to some length to remove *within* a host, where one
   shared mount plus bind mounts replaced one mount per group member;
   across hosts a bind mount can't help, so the only available answer
   is to let an operator say no. Hence the opt-out suppresses the peer
   dependencies too, not just the stanza — suppressing only the stanza
   would leave the entire cost in place and save nothing. A
   fleet-level list rather than a per-host boolean because `resolve()`
   has to reach the same conclusion about other hosts as they reach
   about themselves (spec.md §1 rules out `hostvars`
   cross-referencing), which is also what keeps `peer_served_by` — and
   so the SSH trust `stortree_peer_trust` provisions — in step with
   the mounts the other end actually makes.

8. **What a run should report when a directory it was told to create
   isn't there.** Every directory-creation task in `stortree_mounts`
   is `ignore_errors: true`, for a reason established in production
   (a stale mount aborting the play before the render/restart that
   would fix it — call it out twice over, since it repeated
   identically on every subsequent run). Nothing was stated about what
   should happen afterwards, and what did happen was nothing: the run
   reported success whether the directories appeared or not, so a full
   disk and the transient it was meant to tolerate were
   indistinguishable. Resolved to: a re-stat of every expected path at
   the end of the role, *after* render/restart — by which point a
   path that failed only because its mount was stale has usually
   appeared — which always reports what is still missing and fails
   only under `stortree_mounts_strict`. Advisory by default because
   "missing on this apply" genuinely isn't an error: a brand-new
   nested entry needs two runs by design, and failing the first would
   break the documented pattern rather than catch a bug. Strict is for
   a converged fleet, where a second apply should be clean — CI, or an
   operator's own re-run. Masked paths are excluded: that is a known
   state with its own runbook entry, not a missing directory.

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

Three different things back the claims here, and it's worth keeping them
apart: automated checks that run on a checkout, a real fleet this has
been applied to, and one scenario that has never run at all.

**Applied in production.** These roles run against a real fleet, and a
number of the design decisions above exist *because* the obvious version
failed there — interpretation call #4's presentation mounts (a plain `chown`
inside an rclone mount reports `changed` and silently doesn't persist),
the non-fatal directory creation in `stortree_mounts` (a stale mount
blocking the very render that would fix it, twice in a row), the
`PartOf=` on nested mounts, and the multi-name `getent` loop in
`stortree_secrets` (Ansible's `hash_behaviour: replace` clobbering all
but the last lookup). Each is marked at its point of implementation with
what it broke. That is real evidence, but it is evidence about *these*
hosts in *their* current state — it says nothing about a first apply
onto a clean one, which is the gap below.

**Measured in production**, when the presentation layer moved from
`rclone mount` to `bindfs`. Four presentation mounts over a 1000-file
tree on the fleet's most distant host — the one that peer-mounts
everything, so it has the longest path to the backend:

| | rclone | bindfs |
| --- | --- | --- |
| recursive `ls -lR` | 0.178s median | 0.135s median |
| resident memory, 4 mounts | 47.28 MB | 1.46 MB |
| write via the transport, `stat` via the presented path | not visible after 10s | visible immediately |

The staleness row is the correctness one: a presentation mount's whole
job is to re-present a path something else is writing, and rclone's VFS
directory cache hid a new file for longer than the test would wait.

Under concurrency the single-threaded presentation holds up — per-listing
latency *falls* from 0.133s at K=1 to 0.056s at K=16, and wall time grows
sub-linearly (16× the work in 6.8× the time). Sequential reads pay
nothing measurable for the extra layer: 98.6–99.3 MB/s through the
presentation against 94.6–96.4 MB/s straight off the transport mount.
Metadata-only listings are the one place the layer shows, at roughly 3.7×
on a warm cache — a microbenchmark's worst case for a passthrough
filesystem, and the reason the numbers above are recorded rather than
assumed. A bind-mounted descendant bypasses the presentation entirely
(it is its own mount at that path), so nothing fanned out that way pays
even that.

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
  `status.yml` and `metrics-targets.yml`, against config copied from the
  `*.example` files by the same commands README.md gives an operator —
  so a stale example fails before someone's first run does.
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

**Not applied to a host: the metrics endpoints.** The allocation, the
listener resolution, both unit templates, the target fragment, the
flavour decision and every branch of the role's safety assert are
covered by `pytest`, and the collector playbook round-trips against a
local inventory. What no check here touches is the claim the whole
design rests on — that rclone exits when it cannot bind its listener,
which on a `Type=notify` unit makes a misconfigured endpoint a failed
*mount* rather than a missing counter, taking every presentation and
bind above it with it through `PartOf=`. That is read from upstream
behaviour, not watched on these hosts. It is also why the feature
defaults to off, why the first enable belongs behind `--limit`
([runbook.md](runbook.md) "Publishing rclone metrics"), and why the
role fails the apply on a port collision or an unresolvable interface
rather than letting either reach a unit file.

That is the gap, and it is specifically the **clean-slate** gap. It has
since been walked once, by hand, on a real host: a fleet member that
had no stortree units at all took a first apply of every role —
package installation, the initial SSSD join, peer trust bootstrapped
from nothing, and mounts established where no directory yet existed —
and converged. It also paid for itself immediately, surfacing a bug no
converged host could have hit: an `rclone.args` value containing a
space (rclone's own `bwlimit` timetable syntax) rendered unquoted into
`ExecStart`, so systemd split it and `rclone mount` exited 2 on a third
positional argument. Every other host in the fleet happened to use
whitespace-free values, so the defect had been latent since the
template was written.

One host, one config, and a human watching is not the same as a
repeatable check. Closing the gap properly still means running at least
the `full-tree` scenario (`cd
molecule/full-tree && molecule test`, or per-role via `cd roles/<role>
&& molecule test`) somewhere Docker capacity isn't shared with other
workloads — see spec.md §9's own caveat about what Molecule-in-Docker
does and doesn't prove. Adding a *new* host to the existing fleet walks
the same untested path, so `--check --diff` first is worth it there even
though the fleet itself is long past its first apply.
