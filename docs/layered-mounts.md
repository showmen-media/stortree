# Layered mounts — a transport layer and a presentation layer

Status: **proposal**. Nothing here is implemented. It replaces how
`stortree_mounts` builds the visible tree (spec.md §2) and how access is
enforced on it (spec.md §6); everything else — config.yml's shape,
Samba, identity, secrets scoping, peer trust — is untouched.

It exists for **one** reason: a node's `access` grant is silently
unenforced when the node has no remote of its own and sits inside a
remote-backed ancestor. Everything else below is either a consequence of
fixing that or a cost of doing so. Weigh it on that single benefit — the
tidier code and the better-contained raw view are real, but nobody would
rewrite §2 and §6 for them.

## The problem

A node's `access` grant is enforced one of two ways today, and which one
it gets depends on whether it happens to have an `rclone.remote`:

- **Remote-backed node** — the grant becomes `--uid`/`--gid`/
  `--dir-perms`/`--file-perms` on that node's own rclone mount. Real,
  kernel-visible, correct.
- **Remote-less node** — the grant becomes a `chown`/`chmod` via
  `ansible.builtin.file` (`roles/stortree_mounts/tasks/main.yml`, "Ensure
  every resolved mount point or plain directory exists").

The second is correct only when the path is genuinely local. When a
remote-less node sits *inside* a remote-backed ancestor's mount, that
`chown` is aimed through rclone's FUSE layer, which has nowhere to
persist a different owner for one path underneath a mount that presents
a single uniform `--uid`/`--gid` for everything. The call succeeds,
Ansible reports `changed`, and the next `stat` returns the mount's own
owner.

This is already known for one case. `user_container_paths()` documents
it, and the "Own every plain-local per-user container" task is scoped to
`rejectattr('requires_slug')` precisely to avoid it — "confirmed against
a live deployment, where Ansible reported this exact task as `changed`
while the container's real, on-disk owner never actually moved off
`stortree:stortree`". Per-user containers get a wrapper mount instead.

**Every other node with a grant still hits the bug**, and silently: the
post-apply verification re-stats for `stat.exists` only, never
ownership, so nothing reports it. A worked example from the current
production tree — `fips-storage/system-data` carries `access.owner: fp`,
has no remote, and sits inside `fips-storage`'s mount:

```
fips-storage             MOUNT      --uid 900 --gid 900 --dir-perms 0751
fips-storage/system-data plain dir  chown fp mode 0701   <- never sticks
```

The workaround is to give the node its own `rclone.remote`. That works,
but it makes the operator hand-write a path that duplicates the
parent's, and it buys a second remote-backed mount with a second VFS
cache for what is logically the same storage.

### Caching is not part of this

An earlier draft claimed a second problem here: that because
`client-defaults.rclone.args` applies only to non-owning hosts, the host
owning a remote could not be given a cache. That was wrong, and it is
recorded rather than quietly deleted because it was load-bearing for a
benefit this proposal no longer claims.

`rclone.args` on the node configures the owning host's own mount;
`client-defaults.rclone.args` configures every peer's. Both work, and
they are separate because they describe **different mounts over
different networks** — alpha reaching a Hetzner storage box over WAN,
bravo reaching alpha over LAN sftp. One cache configuration spanning
both would be wrong, not convenient. The split is correct design.

So caching is neither a motivation for this proposal nor something it
improves. The layered model preserves the same two knobs unchanged:
layer 1 on the owner takes the node's `rclone.args`, layer 1 on a peer
takes the resolved client chain, exactly as their single mounts do
today.

## The model

Split each mount into two, with a hidden root holding the first half.

**Layer 1 — transport.** One rclone mount, under
`/srv/.stortree-remotes/<remote-slug>` (`stortree_remotes_root`), for
each remote that actually needs layering. This is the only layer that
talks to a backend, and the only layer that caches. All transport
arguments live here: `vfs-cache-*`, `dir-cache-time`, `bwlimit`,
`cache-dir`. It is mounted **without** `--allow-other`.

**Not every remote is layered.** A remote goes layered only when some
node beneath it needs ownership the node that declared the remote cannot
give it — that is, when the gap in "The problem" actually bites. A remote
whose grant is uniform all the way down keeps today's topology exactly:
one direct rclone mount at the visible path, carrying its own
`--uid`/`--gid`/`--dir-perms`, no staging root involvement, no second
layer.

This is not a concession, it is the point. Layering buys per-node
ownership and costs a passthrough; paying that where there is no
ownership split to express would be pure overhead. In the current
production tree exactly one remote qualifies — `storage-box-239178`,
because `system-data` and the per-user home containers each need an
owner `fips-storage` cannot give them. `t21-synology:media`,
`t21-synology:fam`, the cache remote and `psychias-alpha` all keep their
present single direct mount.

**Layer 2 — presentation.** One **bindfs** mount per visible node that
needs its own ownership, sourced from a local path inside layer 1 and
mounted onto the node's path in `/srv/stortree`. bindfs mirrors a local
directory with the ownership and mode rewritten — `-u`/`-g`/`-p` — which
is this layer's entire job and nothing more. It caches nothing, so every
byte and every listing still comes from layer 1.

```
/srv/.stortree-remotes/storage-box-239178          <- rclone, the cache lives here
/srv/stortree/fips-storage                         <- presentation, --uid stortree 0751
/srv/stortree/fips-storage/system-data             <- presentation, --uid fp 0701
/srv/stortree/fips-storage/system-data/storage     <- plain dir inside the above
```

The shape is not new — `stortree-user-mount@.service.j2` already runs a
uid-remapping passthrough in production for per-user containers. What
changes is the tool and the source: bindfs instead of a second rclone
VFS, reading from layer 1 instead of a sibling directory inside the
tree. See "Why bindfs for layer 2" and "Rejected alternatives".

### Why bindfs for layer 2

An rclone mount of a local path works — it is what the per-user wrapper
does today — but it is a cloud-storage abstraction pressed into service
as a passthrough, and this design multiplies passthroughs:

- **Footprint.** rclone is a Go binary; a mount sits in the tens of MB
  RSS even with `--vfs-cache-mode off`. bindfs is a small C program in
  single-digit MB. Irrelevant at two mounts per host, material at one
  per access-distinct node.
- **Per-operation cost.** SMB is metadata-chatty, and every `getattr`
  crosses this layer. bindfs rewrites ownership in the reply and passes
  the syscall to the underlying filesystem; rclone routes it through an
  object-listing model and its own metadata layer.
- **No second directory cache.** `--vfs-cache-mode off` disables *file
  data* caching, not directory caching — the wrapper unit today sets no
  `--dir-cache-time` and so inherits rclone's default, stacking a second
  staleness window on top of layer 1's. bindfs has no directory cache of
  its own; only the kernel dentry cache applies.
- **A cleaner argument split.** Because presentation is no longer rclone
  at all, there is no question of which rclone flag belongs to which
  layer. See "Arguments" below.

**bindfs runs single-threaded, and that is deliberate.** It has run
single-threaded by default since 1.11 because `--multithreaded` carries
a documented race: another process can briefly observe a file with the
wrong owner, group or permissions. The man page names the exact use case
that makes this dangerous — "if you rely on bindfs to reduce permissions
on new files" — which is what this layer exists to do. So presentation
mounts run single-threaded, and the trade is accepted: concurrent
readers of one node serialise through one thread.

That ceiling is per node, not per host or per remote — each presentation
mount is its own process, so separate shares do not contend with each
other. A single node under heavy concurrent read is the case that would
feel it. If one ever does, the answer is to give it its own layer-1
mount and present it directly, not to enable `--multithreaded`.

### Why moving the source out of the tree is the whole trick

The per-user wrapper sources from a *sibling* directory inside the same
ancestor mount (`home/stortree-user-jd` presented at `home/jd`). That
works, but it means the content has to physically live at the staging
name, so adopting the same mechanism for an existing node would require
moving its data on the remote.

Sourcing from layer 1 instead removes that entirely. `system-data`'s
content stays exactly where it is at `u253014-sub1/system-data`; layer 1
mounts `u253014-sub1`, and the presentation mount takes its subpath.
Source and target are different paths in the filesystem namespace while
being the same directory on the backend, so there is no self-mount and
nothing to migrate.

## What this fixes

1. **Per-node ownership works everywhere**, with no `rclone.remote`
   declaration and no data movement. One enforcement mechanism instead
   of two that disagree.
2. **One cache per remote per host**, where two nodes sharing a remote
   would otherwise mount it twice and cache it twice. Narrow — no two
   nodes in the current tree share a remote — and not a motivation on
   its own. See "Caching is not part of this".
3. **The raw view stops being reachable.** See "Security posture".
4. **Three special-case mechanisms collapse into one.** See "Removing
   the `stortree-user-` convention".

## Resolution changes

`resolve()` is unchanged. This is entirely below it, in `plan_mounts()`
and the role.

`plan_mounts()` keeps returning one flat list, and gains an explicit
`kind` on every entry — `transport`, `mount`, `bind`, `dir` — replacing
the current implicit switch on truthy `remote`/`symlink_target`. Callers
(`mount_unit_names()`, the role's `selectattr` loops) move to `kind`.

**Transport entries** are derived, not configured. One per distinct
remote spec across everything the host mounts — server subtrees, client
mounts, peer dependencies alike:

```python
{
  "kind": "transport",
  "slug": "storage-box-239178",              # from the remote spec
  "local_path": ".stortree-remotes/storage-box-239178",
  "remote": "storage-box-239178:u253014-sub1",
  "args": {...},                              # transport args only
}
```

Keyed by remote spec alone, so the path stays readable and stable.
Transport args are merged across every node using that remote; two nodes
setting a *different value for the same key* is a config error naming
both, rather than a silent last-one-wins. (Alternative considered:
key by `(remote, args)` with a hash suffix, which tolerates divergent
args at the cost of unreadable paths and remount churn whenever an arg
changes. Rejected — divergent transport args for one remote is much more
likely a mistake than an intent.)

**Presentation entries** are today's mount entries with the remote
reference swapped for a source path:

```python
{
  "kind": "mount",
  "slug": "fips\\x2dstorage-system\\x2ddata",   # unchanged, from the node path
  "local_path": "fips-storage/system-data",
  "transport_slug": "storage-box-239178",
  "source_path": ".stortree-remotes/storage-box-239178/system-data",
  "access": {...},                              # -> bindfs -u/-g/-p
  "requires_slug": "fips\\x2dstorage",          # parent presentation mount
}
```

`source_path` is the transport mount's path plus the node's path
relative to the remote's own mountpoint. That relative part is already
knowable: it is the node's tree path minus the path of the nearest
ancestor that declared the remote.

**Which nodes get a presentation mount:**

| node | today | proposed |
| --- | --- | --- |
| own `rclone.remote`, remote not layered | rclone mount | unchanged |
| own `rclone.remote`, remote layered | rclone mount | presentation mount |
| no remote, `access` grant, inside a remote-backed ancestor | plain dir + broken chown | **presentation mount** |
| no remote, no grant | plain dir | plain dir |
| genuinely local (no remote-backed ancestor) | plain dir + real chown | unchanged |

Only the third row is new behaviour, and it is the row that is broken
today. The second is the same mount with a different source; the first
and last do not move at all.

### What stays out of the presentation layer's path

Two things short-circuit past it, which is what keeps single-threaded
presentation from mattering on the hot paths:

- **A nested mount.** Once a path crosses into another mount, the
  filesystem underneath is shadowed and plays no further part. A direct
  rclone mount nested inside a presented node is reached *through* the
  presented directory but read from rclone.
- **A bind mount.** Same thing: `home/fp/psychias-media` is a bind of
  `home/.mounts/psychias-media`, so once resolution crosses it, I/O goes
  to that mount, not through the bindfs at `home/fp`.

So a member streaming media touches the presentation layer for a couple
of path lookups and then leaves it entirely. What genuinely sits behind a
single-threaded presentation mount is container metadata and
appliance directories — the low-concurrency cases.

### Arguments

There is nothing to split. **Every value an operator writes in
`rclone.args` goes to transport**, because transport is the only rclone
mount left. Presentation takes no operator-supplied arguments at all —
it is derived entirely from the node's resolved `access` grant:

| grant | bindfs |
| --- | --- |
| `access_owner()` | `-u <user>` |
| `access_group()` | `-g <group>` |
| `access_mode()` | `-p <mode>` |

Plus three fixed options on every presentation mount: `-o allow_other`
so real users can reach it, and `--chown-ignore --chmod-ignore` so a
client's `chmod`/`chown` through the mount succeeds without attempting
to write through to layer 1. That last pair matters — bindfs defaults to
`chown-normal`/`chmod-normal`, which would try to chown the underlying
file on an rclone mount that cannot persist it, reintroducing in a new
place exactly the silent no-op this proposal exists to remove. Samba
with `inherit permissions = yes` will attempt these.

This is a real simplification over the rclone-passthrough version, where
`--read-only` and `--umask` had no obvious home. As rclone args they are
now unambiguously transport.

## Units and ordering

- `stortree-remote@<slug>.service` — new. Layer 1, `rclone mount`,
  `Type=notify` as today. Depends only on `network-online.target`.
- `stortree-mount@<slug>.service` — existing name, now a bindfs mount.
  Layer 2.
- `stortree-bind@<slug>.service` — unchanged.
- `stortree-user-mount@<slug>.service` — **deleted**.

`Type=notify` does not survive on layer 2: rclone speaks sd_notify,
bindfs does not. bindfs forks to the background once the mount is
established, so `Type=forking` gives accurate readiness — the unit is
active exactly when the mountpoint is live, which is what the ordering
edges below depend on. Do not pass `-f`; that keeps it in the foreground
and breaks the readiness signal.

A presentation `ExecStart` reads:

```
ExecStart=/usr/bin/bindfs -u <owner> -g <group> -p <mode> \
  -o allow_other --chown-ignore --chmod-ignore \
  /srv/.stortree-remotes/<transport-slug>/<relpath> /srv/stortree/<node path>
```

A presentation unit carries two dependency edges:

```
After=/Requires=/PartOf=  stortree-remote@<transport_slug>.service
RequiresMountsFor=        /srv/.stortree-remotes/<transport_slug>
After=/PartOf=            stortree-mount@<requires_slug>.service   # if nested
RequiresMountsFor=        /srv/stortree/<parent path>              # mountpoint must exist
```

The first is new; the second is exactly today's `requires_slug` edge.
`PartOf=` on both for the reason the existing templates already
document: a remount of either underlying mount detaches this one while
leaving the process alive and the unit reporting active, which has
happened in production on both the wrapper and the bind units.

The node's declared `requires` (config-schema.md "Requires") attaches to
the **transport** unit, not the presentation one. A VFS cache is a
transport concern, and `.psychias-bravo-cache` exists to back
`cache-dir`, which now lives on layer 1.

## Directory creation

**All directory creation moves to layer 1**, at
`/srv/.stortree-remotes/<transport-slug>/<relative path>`. The visible
tree is composed of presentation mounts and nothing else creates
anything there.

This kills `physical_path()` outright. That function exists because a
wrapper's staging directory is a *different* directory from the
container path, so anything created at the container path before the
wrapper mounts is shadowed the moment it does — the failure that took
out all eight bind mounts on the first apply after wrappers existed.
Under this model the source and the visible path are the same directory
on the backend, seen through two mounts, so there is nothing to shadow
and nothing to redirect.

It also simplifies ordering: creating a directory needs only its
transport mount up, never the presentation mount above it.

## Removing the `stortree-user-` convention

The convention exists for one reason: a per-user container needs a
different owner from the mount it lives in, and the wrapper needs a
source that is not the mountpoint itself. Layer 1 supplies that source,
so the sibling staging directory has no remaining job.

A per-user container becomes an ordinary presentation mount:

```
source: /srv/.stortree-remotes/storage-box-239178/home/fp
target: /srv/stortree/fips-storage/home/fp        --uid fp --dir-perms 0750
```

Deleted as a result:

- `STORTREE_USER_PREFIX` and every `staging_path` in
  `user_container_paths()`
- `physical_path()` and its filter registration
- `stortree-user-mount@.service.j2` and `user_mount_unit_names()`
- the `stortree-user-mount@` branch in `stortree-bind@.service.j2`
- the "plain-local vs wrapped" split (`rejectattr('requires_slug')`) in
  the ownership tasks — every container is now presented the same way

`user_container_paths()` survives, reduced to what it was always for:
which container paths exist and who owns each. `_resolved_user_containers()`
is unchanged.

**This one does need a data migration.** Content currently lives at
`home/stortree-user-<user>` on the remote and must move to `home/<user>`.
Per user, per tree, scriptable with `rclone move`, but it must happen
with the mounts down. It is the only migration in this proposal and it
is optional — the convention could be kept and layered like anything
else, at the cost of keeping all of the above.

The group-only fan-out (`per_user_mount_path()` → `.mounts/<node>` plus
one bind per member) is **kept as is**. bindfs presentation mounts are
cheap enough that N of them would now be quite defensible, but a bind
mount is a kernel VFS relationship with no process behind it at all, and
no userspace layer beats that. It also keeps every member on one thread's
worth of contention rather than N single-threaded mounts of one node.

## Security posture of the staging root

Layer 1 presents raw backend content with no per-node grant applied. If
it is reachable, the entire access model is bypassable by anyone who can
`cd` into it. This is the one way the proposal can end up worse than
today, so it is a requirement, not a detail:

- `/srv/.stortree-remotes` is `0700`, owned `stortree:stortree`.
- Layer-1 mounts are **not** given `--allow-other`. FUSE then restricts
  each mount to the mounting user — `stortree`, which is also what runs
  the presentation mounts. Not even root traverses it without going out
  of its way.
- No Samba share ever points inside it. Share paths are derived from
  node paths under `stortree_root` and cannot address it.
- It sits outside `stortree_root` deliberately, so nothing that walks
  the tree can wander into it.

Net effect: the ungranted view exists on exactly one path, visible to
exactly one service account, and the granted views are the only way in.
That is stronger than today, where the ungranted view *is* the tree and
grants are layered on by mount options.

## What does not change

config.yml's schema, resolve()'s output, Samba shares and `valid users`,
LDAP/SSSD identity, `pam_smbpass`, peer trust, rclone.conf scoping
(`filter_rclone_conf` — the same host needs the same remotes), share
names, `requires` semantics, and the access model itself. Operators
write the same config and get the same tree; only how it is assembled
changes.

## Migration

### Phase 0 — swap the existing wrapper to bindfs, on its own

Independent of everything else here, and worth doing first. Replace
`stortree-user-mount@.service.j2`'s rclone invocation with the bindfs
one above, keeping its current source (the `stortree-user-` sibling) and
its current target. Add `bindfs` to the role's apt task.

It is a contained change — one template, one package, no resolution
changes, no config changes, no data movement — and it pays for itself
immediately: smaller processes, and it closes the directory-cache
staleness window those wrappers carry today. It also proves bindfs in
this fleet before anything larger depends on it, which is the real
reason to do it separately.

**Gate A runs here**, and nothing below starts until it passes. Because
this phase changes the tool and holds everything else fixed, it is the
only clean before/after available; once the source moves to layer 1
there is no longer a like-for-like comparison to make.

### Phases 1+ — the layered model

Gated on A. Do one remote first — `storage-box-239178`, the only one in
the current tree that qualifies — and run **Gates B and C** against it
before layering a second. Per host, in one maintenance window:

1. Stop and disable every `stortree-mount@`, `stortree-user-mount@` and
   `stortree-bind@` unit. The tree goes away.
2. *(Optional, only with the `stortree-user-` removal)* `rclone move`
   each `<prefix>/stortree-user-<user>` to `<prefix>/<user>` on the
   remote, driven from the control node against the backend directly,
   not through a mount.
3. Apply. Transport mounts come up first, directories are created in
   layer 1, presentation mounts come up on top.
4. Verify ownership, not just existence — see below.

5. Run Gates B and C.

Rollback is checking out the previous commit and re-applying; the
backend layout is unchanged unless step 2 ran, and step 2 is reversible
by the same move in the other direction.

**The post-apply verification should be extended to compare owner, group
and mode against the resolved grant, not just `stat.exists`.** The
silence is what let this bug live; a topology change is the wrong moment
to still not be checking.

## Acceptance gates

Each phase has to earn the next one. The gates exist because two of this
proposal's costs — a second userspace layer on every operation, and a
presentation layer that handles one request at a time — are asserted
here from reasoning about the implementations, not from measurement on
this fleet. Reasoning is enough to choose a design; it is not enough to
put one under a production tree.

Measure on bravo. It is the host that peer-mounts everything, so it has
the longest path to the backend and shows any added layer most clearly.

### Gate A — bindfs is not worse than the wrapper it replaces

Runs against **Phase 0**, which changes the tool and nothing else: same
source, same target, same uid remap. That makes it the only clean
before/after this proposal ever gets.

| measure | how | passes if |
| --- | --- | --- |
| metadata throughput | time a recursive `ls -lR` of one per-user container, cold dentry cache | no worse than the rclone wrapper |
| resident memory | `systemctl show -p MemoryCurrent` per wrapper unit | materially below the rclone wrapper; the footprint claim is a headline reason for bindfs |
| staleness | write into the staging directory via the ancestor mount, `stat` at the presented path | visible immediately, where rclone's default directory cache could hide it for minutes |
| stability | leave it up across a full apply and an ancestor remount | `PartOf=` still tears it down and back up cleanly |

Fail on the first two and bindfs is the wrong tool; revert Phase 0 and
reopen "Why bindfs for layer 2". Phase 0 is one template and an apt
task, so that reversal is cheap by construction — which is the reason it
is a separate phase.

### Gate B — single-threaded presentation survives real concurrency

Runs against the **first layered remote**, before a second one follows.
This is the gate for the trade accepted in "Why bindfs for layer 2".

Drive K concurrent SMB clients against a presented node, K at least the
number of real users plus the appliances, and compare against the same
node reached through a direct mount:

| measure | passes if |
| --- | --- |
| p95 directory-listing latency under load | within a small multiple of the direct-mount baseline, and stable rather than growing with K |
| appliance write throughput (frigate, hassio) | unchanged — these are single-writer and should not notice at all |
| media read throughput through a bind | unchanged, confirming binds short-circuit past the presentation layer as "What stays out of the presentation layer's path" claims |

The third row is as much a correctness check on that claim as a
performance one. If media reads *do* slow down, the short-circuit
reasoning is wrong and the scoping argument that makes single-threading
acceptable collapses with it.

### Gate C — the double-FUSE read path

Also against the first layered remote. Cold and warm reads through
presentation → transport, against the same content read directly from
the transport mount:

| measure | passes if |
| --- | --- |
| warm-cache sequential read | within a small margin of the transport mount alone |
| cold read | dominated by backend fetch, as it is today — the added layer should be invisible next to network latency |

### If a gate fails

The fallback for B or C is an **idmapped bind mount**
(`mount -o X-mount.idmap`), which remaps ownership in the kernel with no
userspace layer at all — strictly faster than either bindfs or rclone.
Two costs, which is why it is the fallback and not the plan: it remaps
uid/gid only, so per-node *mode* control would be lost and
`access_mode()` could no longer be enforced at this layer; and idmapped
mounts over FUSE need kernel support recent enough to have it, which
wants confirming on the actual hosts rather than assumed.

Failing Gate B with no acceptable fallback means the scoping rule has to
tighten further: present only the nodes that genuinely cannot be
expressed any other way, and let the rest keep a uniform owner.

## Costs and risks

- **Two FUSE layers on every I/O.** Presentation → transport → backend.
  bindfs makes the top layer about as thin as a userspace layer gets —
  a syscall passed through with the ownership rewritten in the reply —
  but it is still a context switch per operation that does not happen
  today. Phase 0 puts it in production on a small blast radius first,
  and Gates A and C measure it.
- **Presentation mounts are single-threaded**, per the bindfs decision
  above. Concurrent readers of one node serialise. Per node, not per
  host, and not something `--multithreaded` may be used to fix — Gate B
  is what decides whether the trade holds.
- **Process count** rises to one per remote plus one per access-distinct
  node, per host — though the presentation processes are now single-digit
  MB each rather than tens.
- **It is a rewrite of spec.md §2 and §6**, on a fleet whose clean-slate
  path has never been exercised — `molecule test` has still never run.
  The unit tests cover resolution and templates well and would cover
  this too, but nothing covers a first apply against a real host.
- **The staging root is a new failure surface**: a layer-1 mount that
  dies takes every presentation mount above it with it (by design, via
  `PartOf=`), turning one backend hiccup into a visibly empty subtree
  rather than a stale one. Arguably better, definitely different.

## Existing tests that constrain the implementation

Four checks in `tests/test_repo_consistency.py` encode invariants this
change has to keep, and each needs updating in the same commit rather
than discovering later:

- **`test_every_unit_family_the_plugin_names_is_swept_by_the_role`**
  asserts the unit families the plugin invents are all swept by the
  role's stale-file `find`, its `reset-failed`, and `status.yml`'s
  `list-units`. It hardcodes three families. Deleting
  `stortree-user-mount@` and adding `stortree-remote@` keeps it at three,
  but the test's own fixture builds families from
  `user_mount_unit_names()`, which goes away — so the fixture changes
  even though the count does not. All three wildcard sweeps need
  `stortree-remote@*.service` added and `stortree-user-mount@*.service`
  removed.
- **`test_the_role_derives_no_unit_name_the_plugin_does_not_render`**
  guards the other half of that seam: unit names are spelled once, in
  the render task's `dest`. The new transport family must follow the
  same pattern.
- **`test_the_mounts_verification_covers_the_paths_the_role_creates`**
  asserts the re-stat loop mentions `staging_path`. That field is
  removed here, so the assertion becomes the transport-relative creation
  paths instead. This is also the natural place to add the
  owner/group/mode comparison the Migration section calls for.
- **`test_stortree_root_has_exactly_one_definition`** requires
  `stortree_root` to be defined only in `stortree_facts`, because
  `status.yml` applies that role alone. `stortree_remotes_root` must
  follow the same rule for the same reason.

The resolution-level tests are the safety net for the rest:
`plan_mounts()` is covered thoroughly enough that the transport split
and the `kind` field should not be able to land silently wrong.

## Open questions

1. **All-at-once or per-remote opt-in?** A mixed model would let one
   remote adopt this while others keep today's topology, at the cost of
   two code paths in `plan_mounts()` indefinitely. Recommendation:
   all-at-once, one code path.
2. **`stortree-user-` removal in the same change or a follow-up?** It is
   separable, and it is the only part needing a data migration.
3. **Anything outside stortree** holding paths under `/srv/stortree` and
   assuming a particular mount topology — backup jobs, monitoring, the
   frigate and hassio appliances themselves.

Settled while writing this, recorded so they are not reopened: the
argument split (there isn't one — see "Arguments"), and bindfs's
threading *mode* — single, because `--multithreaded` carries a race that
can expose the wrong owner, and no measurement changes that.

Deliberately *not* settled, and gated instead: whether bindfs is the
right presentation layer at all (Gate A), and whether single-threaded is
fast enough for the nodes that end up behind it (Gate B). Those are
performance claims made here from reasoning alone, and this document is
not the right authority on them.

## Rejected alternatives

- **Declare `rclone.remote` on each node needing a grant.** Works today
  with no code change, but duplicates the parent's remote path by hand,
  buys a second cache per node, and leaves the underlying inconsistency
  in place for every node that forgets.
- **Derive the remote from the ancestor** (`rclone.remote: inherit`).
  No data migration and much cheaper to build, but still a second
  backend-facing mount with its own cache per node, and it bends
  spec.md §1's "rclone never inherits" rule.
- **Sibling staging inside the tree** (generalising the per-user wrapper
  as-is). Same runtime shape as this proposal but requires migrating
  every affected node's data on the backend, which this avoids entirely.
- **rclone as the presentation layer** (an rclone mount of a local path,
  what the per-user wrapper does today). Rejected in favour of bindfs —
  see "Why bindfs for layer 2". The deciding factors were footprint
  multiplied across one mount per access-distinct node, and the second
  directory cache an rclone VFS brings whether or not file caching is
  off. Keeping it would also have left the transport/presentation
  argument split as a genuine open question. Rejected on reasoning, not
  measurement, which is exactly what Gate A exists to confirm or
  overturn — if it fails, this is what the design reverts to.
