# stortree — operator runbook

Day-to-day commands for running stortree against a real fleet. See
[spec.md §8](spec.md) for the design behind these, and
[plan.md](plan.md) for build status.

## First-time setup

```
cp inventory/hosts.yml.example inventory/hosts.yml
cp stortree/config.yml.example stortree/config.yml
cp stortree/ldap.yml.example stortree/ldap.yml
cp stortree/rclone.conf.example stortree/rclone.conf
# edit all four for your fleet, then:
ansible-vault encrypt stortree/ldap.yml stortree/rclone.conf

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/ansible-galaxy collection install -r requirements.yml
```

`stortree/sshd_config` is optional -- only create it if you want the
`Match Group`/`ForceCommand` pattern described in
[config-schema.md](config-schema.md#sshd_config-optional).

## Apply the whole tree

```
ansible-playbook playbooks/site.yml --ask-vault-pass
```

Idempotent: re-running converges to the same end state rather than
accumulating drift.

## Bring in one new host

Add it to `inventory/hosts.yml` (and optionally to `stortree/config.yml`
as a node's `host:` or under `peers:` -- neither is required, see
config-schema.md "Every inventory host participates"), then:

```
ansible-playbook playbooks/site.yml --ask-vault-pass --limit new-host-name
```

No separate join/bootstrap step -- peer trust and secrets are both
re-derived from the current tree on every run.

## Apply just one concern

Every role is tagged with its own short name:

```
ansible-playbook playbooks/site.yml --ask-vault-pass --tags mounts
```

Available tags: `facts`, `common`, `identity`, `peer_trust`, `secrets`,
`mounts`, `samba`, `pam_smbpass`, `sshd`.

## Dry run

```
ansible-playbook playbooks/site.yml --ask-vault-pass --check --diff
```

## Check status

Read-only -- no state-changing modules:

```
ansible-playbook playbooks/status.yml --ask-vault-pass
```

Reports, per host: resolved server subtrees, whether it has a subtree
mount, exported Samba shares, peer dependency count, who depends on it,
live mount-unit states, `smbstatus`, and SSSD domain status.

## Publishing rclone metrics

Off until you turn it on, and off is a real state -- an apply against a
fleet that has never enabled it changes nothing.

```yaml
# inventory/group_vars/all.yml
stortree_metrics_enabled: true
stortree_metrics_bind:
  - 127.0.0.1
```

```
ansible-playbook playbooks/site.yml --ask-vault-pass --limit storage-node-alpha
ansible-playbook playbooks/metrics-targets.yml
```

Do the first apply with `--limit`. Every setting here ends up inside an
`ExecStart`, and rclone exits rather than starting without the listener
it was told to bind -- on a `Type=notify` unit that is a mount that does
not come up, and `PartOf=` takes every presentation and bind above it
down as well. One host first makes that a contained surprise.

`site.yml` gives every rclone mount its own endpoint (one process, one
mount, one port -- there is no host-wide endpoint, and no separate
rclone or manager elsewhere can adopt these mounts) and writes the
Prometheus `file_sd` fragment listing them.
`playbooks/metrics-targets.yml` then collects those fragments into one
target file on the control node; see
[prometheus/prometheus.yml.example](../prometheus/prometheus.yml.example)
for the scrape config and for what the numbers are and aren't worth.
Re-run it after any apply that changes which mounts exist or where they
bind.

Per host rather than fleet-wide, or the other way round: ordinary
Ansible precedence, `host_vars/<host>.yml` over `group_vars/`. Unlike
`stortree_samba_hosts` there is no list to keep in agreement, because
nothing on any other host changes because of what this one publishes.

Two settings decide whether this is safe, and the apply refuses rather
than guessing:

- **`stortree_metrics_bind`** is the access control -- there is no
  firewall role in this repo. Loopback is reachable from an agent on the
  host or an SSH tunnel; naming an interface is what publishes the
  endpoint. A name that resolves to no address on a host fails the
  apply there, rather than falling back to a wildcard bind.
- **`stortree_metrics_htpasswd`** is mandatory whenever a host's rclone
  predates 1.68, because the only way to publish metrics on those is
  `--rc --rc-enable-metrics`, and that endpoint also serves
  `config/dump` -- this host's scoped `rclone.conf`, backend credentials
  and all. Loopback is no exemption: every LDAP user with a shell here
  is a local user. `stortree_metrics_flavour` picks per host by reading
  `rclone version`.

  On an apt fleet that is *every* host, permanently: Debian ships 1.60.1
  in bookworm and in trixie, Ubuntu ships it in noble, and 1.69 only
  ever reached sid -- there is no release you can deploy on that has
  `--metrics-addr`. Hosts on `stortree_rclone_install: upstream` get
  `--metrics-addr` instead, which serves counters and no config
  endpoints, and the htpasswd becomes optional there (still worth
  setting if the bind address is a network you would not publish your
  transfer volumes to). Same fleet, same 1.60.1, also means `--rc-addr`
  is not yet repeatable -- it became so in 1.63 -- so a
  `stortree_metrics_bind` naming more than one address quietly listens
  on the last one alone until that host is upgraded.

### "both want metrics port N"

Ports are derived from each node's path so that editing one entry in
`config.yml` doesn't renumber -- and so restart -- every other mount on
the host. Two paths can hash to one port, which fails the apply naming
both. Pin either one:

```yaml
stortree_metrics_port_overrides:
  tree/home/media: 20500
```

### An endpoint that never comes up on an interface

An interface that appears later than the mount (WireGuard, Tailscale, a
bridge something other than networkd brings up) means rclone has nothing
to bind when it starts -- and rclone *exits* when it cannot bind its
metrics server, so this is a failed mount, not a missing counter.

Each transport waits for its own listen addresses before it starts
rclone, polling `ip addr` once a second for up to
`stortree_mounts_bind_wait` (60s by default). A normal boot pays
whatever the interface actually takes and nothing more. Raise it for an
interface that genuinely needs minutes:

```yaml
# inventory/group_vars/all.yml
stortree_mounts_bind_wait: 300
```

The unit also orders itself after the interface's `.device` unit, but
do not rely on that alone: `.device` units are published by udev, and
**udev does not run in a container**, so in an LXC or Docker guest that
unit is loaded and permanently `inactive` however healthy the interface
is. The wait is what holds the guarantee there. Check with:

```sh
systemctl show sys-subsystem-net-devices-<iface>.device -p ActiveState
```

If an address never arrives, the journal names it:

```
stortree: 10.10.0.4 did not appear within 60s -- stortree_metrics_bind
names it, but nothing on this host has configured it
```

Binding loopback and scraping through a tunnel avoids the whole class.

## Upgrading rclone

Debian's rclone is 1.60.1 (November 2022) on every release you can
deploy on -- bookworm, trixie, and Ubuntu noble all ship it, and 1.69
only ever reached sid. `stortree_rclone_install` chooses between that
and a pinned upstream build:

```yaml
# inventory/group_vars/all.yml, or host_vars/<host>.yml to roll it one
# host at a time
stortree_rclone_install: upstream
stortree_rclone_version: "1.75.1"
```

The role downloads `rclone-v<version>-linux-<arch>.zip` into
`/var/cache/stortree`, verifies it against the SHA256 in
`stortree_rclone_checksums`, and installs the binary at
`/usr/local/bin/rclone` -- never `/usr/bin`, which belongs to dpkg. The
apt package is left installed and simply stops being used; the unit
files name `stortree_rclone_bin`, so reverting is
`stortree_rclone_install: apt` plus a re-apply.

**Bumping the version means bumping the checksums.** They are not
fetched at apply time on purpose -- a hash pulled from the same host over
the same TLS session as the download proves little. Get them from
upstream and paste them into `stortree_rclone_checksums`:

```sh
curl -s https://downloads.rclone.org/v1.75.1/SHA256SUMS \
  | grep -E 'linux-(amd64|arm64|arm-v7|386)\.zip'
```

`tests/test_repo_consistency.py` checks their shape and that every
architecture in `stortree_rclone_arch_map` has one, but only upstream
can tell you the values.

**An upgrade restarts every transport mount on the host.** A running
rclone holds the old binary's inode open and goes on behaving like the
old version indefinitely, and replacing the file changes no unit, so the
apply restarts the `stortree-remote@*` units itself when -- and only
when -- the binary on disk actually changed. Every presentation and bind
above them goes down and comes back with them through `PartOf=`. That is
an I/O interruption for anyone reading the tree at the time, so roll it
with `--limit` one host at a time rather than fleet-wide, and check
`playbooks/status.yml` in between.

The restart step itself carries `throttle: 1`, so it is already one host
at a time even when you forget -- see [Restarting mounts on a fleet that
peer-mounts itself](#restarting-mounts-on-a-fleet-that-peer-mounts-itself)
for why that matters more than it sounds. `--limit` is still the right
way to *stage* an upgrade: it lets you look at one host before the rest
gets the new binary at all.

What upgrading gets you, concretely: `--metrics-addr` (1.68) instead of
the `rc` metrics flavour that also serves `config/dump`, so
`stortree_metrics_htpasswd` stops being mandatory; a repeatable
`--rc-addr` (1.63), so a multi-address `stortree_metrics_bind` stops
silently binding only its last entry; and three years of VFS and mount
fixes under processes that are meant to stay up for months.

What it does not get you: `--allow-root`. The "Ignoring --allow-root.
Support has been removed upstream" line the transport units work around
comes from the FUSE library rclone's `mount` is built on, not from the
rclone version, and no upgrade restores it. The `--allow-other` +
`0700` + `--default-permissions` arrangement in
`stortree-remote@.service.j2` stays exactly as it is.

The cost is that nothing else upgrades this binary -- no
unattended-upgrades, no distro security tracker. Watch
<https://github.com/rclone/rclone/releases> and treat a version bump as
a change to be rolled, because it is one.

## A host stuck at `degraded` with failed `sssd-*.socket` units

Symptom: `systemctl is-system-running` says `degraded`, three units are
failed, and identity works perfectly.

```
sssd-nss.socket   loaded failed failed SSSD NSS Service responder socket
sssd-pam.socket   loaded failed failed SSSD PAM Service responder socket
sssd-ssh.socket   loaded failed failed SSSD SSH Service responder socket
```

There are two ways to start an SSSD responder and they are mutually
exclusive. Naming it in `services` in the `[sssd]` section makes
`sssd.service` fork it directly; leaving it out hands the job to socket
activation, which is how Debian ships SSSD. Configure both and each
socket unit refuses to start -- its `ExecStartPre` check exits 17 with
"configured to be socket-activated but it's still mentioned in the
services' line".

Nothing an operator would notice actually breaks, which is the problem:
the responder *is* running, `getent` works, and the host simply sits at
`degraded` forever. The next real failure then arrives somewhere nobody
is looking.

The apply resolves this: it disables and stops the socket for every
responder `services` names, then clears the failed-state bookkeeping
that stopping leaves behind. If you are seeing it, run one.

To go the other way instead -- socket activation, no `services` line,
nothing for stortree to retire -- set it empty in `stortree/ldap.yml`:

```yaml
extra:
  sssd:
    services: ""
```

Only do that on a platform whose responder sockets are enabled. Where
they are not, an empty `services` starts no responders at all and takes
identity down for the whole host.

## Onboarding a new LDAP user for Samba

A user's Samba password only syncs on an actual PAM event on a given
host (spec.md §5) -- have them SSH into a host once (even to a
restricted shell via the `sshd_config` `Match Group`/`ForceCommand`
pattern) before their first SMB connection to that host.

## Editing `access` rules or the tree shape

Edit `stortree/config.yml`, then re-run `site.yml` (optionally
`--tags mounts` if you only changed `access:` blocks, or the full run if
you changed tree shape/hosts/remotes). Nothing needs manual cleanup --
`stortree_mounts` removes stale units and always recomputes every path's
ownership/mode from the current resolved facts.

## A lazy unmount that will not return

Symptom: `umount -l` on a stortree path does not come back at all, and
neither does `stat` or `mountpoint` on it. `findmnt` may show nothing
mounted there while the path still hangs, and the rclone that served it
is a zombie (`Z` in `ps`) that `kill -9` cannot clear.

A lazy unmount detaches a mount from the namespace; it does not cancel
I/O the kernel has already handed to a FUSE server. When that server
died without answering, those requests sit in the connection's queue
forever and every new syscall on the path joins them. The runbook's own
teardown loops then block on the first such path and never reach the
rest.

Abort the connection instead. That fails every pending and future
request on it with `ENOTCONN` immediately, which is what lets the
unmount finish:

```sh
# Connections with requests waiting are the wedged ones.
for c in /sys/fs/fuse/connections/*/; do
  w=$(cat "$c/waiting" 2>/dev/null) || continue
  [ "${w:-0}" != 0 ] && { echo "aborting ${c} (waiting=$w)"; echo 1 > "$c/abort"; }
done
```

Then re-run the unmount sweep. Measured on a production host: one
connection with 19 requests waiting was holding 17 mounts and both
teardown loops; aborting it detached all 17 at once, and the zombie
rclones were reaped immediately. Without it the only remaining option is
a reboot.

Aborting is safe for a mount you are tearing down anyway — it is the
mount equivalent of the process already being dead — and it is not a
substitute for `ExecStop` on a healthy mount, which should be stopped
normally.

**Abort, then unmount, then start — in that order.** Aborting kills the
FUSE server but leaves its mount *attached*, now stale, so anything
trying to mount there next fails with

```
fuse: failed to access mountpoint /srv/stortree/<path>:
      Transport endpoint is not connected
```

and keeps failing, because `Restart=` cannot clear a stale mount. Going
straight from abort to `systemctl start` is therefore worse than the
hang it was meant to fix: it converts one stuck mount into a
mountpoint nothing can use. Doing it in a restart of a whole
presentation family took a host from 29/29 units to 4/29 until the
stranded mount was cleared by hand. Always put the unmount sweep
between them:

```sh
# ... abort loop above, then:
findmnt -t fuse,fuse.rclone,fuse.bindfs -no TARGET | grep '^/srv/' |
  while read -r m; do
    timeout 5 stat "$m" >/dev/null 2>&1 || umount -l "$m"
  done
systemctl reset-failed 'stortree-*'
systemctl start '<the unit>'
```

## A masked mount (e.g. after upgrading `stortree-mount@.service.j2`)

A top-level subtree's own mount can end up active but unreachable to
root -- e.g. after upgrading to a `stortree-mount@.service.j2` that
changed access flags, a host that already had that mount active from
before the upgrade keeps running the old, more restrictive unit until
something restarts it. That upgrade window is the *only* way to get
here now: every mount renders `--allow-other` since the change that
made it unconditional (docs/spec.md §6), so a live mount is reachable
to root whether or not it carries an `access` grant. Before that, a
grantless mount -- a VFS-cache subtree especially, where no grant is
ever meaningful -- masked itself permanently and reported it on every
single apply; if you are staring at a `Permission denied` probe line
that has survived many applies in a row, check that the host's
`/etc/systemd/system/stortree-mount@<slug>.service` actually has
`--allow-other` in its `ExecStart` and that the running process picked
it up (`systemctl show -p ExecStart stortree-mount@<slug>.service`
against `ps`), rather than assuming the mount is transiently masked.
`stortree_mounts` detects masking itself (a direct
`stat` probe against every remote-backed entry's own mountpoint, not
`ansible_facts.mounts`) and skips every task that would otherwise try to
touch that path or anything nested under it, rather than fail outright
-- no separate recovery tag needed. It still renders and restarts that
mount's own unit in the same run (unaffected by the skip, since that
doesn't touch the filesystem tree), which is what actually clears the
masking; anything that was skipped underneath it then gets created
normally on the *next* apply, once the mount is reachable again. Symptom
along the way: `skipping: [host] => (item=...)` lines for entries nested
under the masked subtree, and `...ignoring` after a `stat`
`Permission denied` on the probe task itself -- both expected, not
failures. Two runs back-to-back clear it; nothing to invoke by name.

## A stranded mount after a mount is introduced above an existing one

Symptom: one or more units restart-loop with `Fatal error: directory
already mounted, use --allow-non-empty to mount anyway`, the paths they
serve list as empty, and `findmnt` shows them attached as *siblings* of
the mount they should be nested inside. Everything ordered after them
fails too, with `A dependency job for ... failed`, so a single stranded
mount can present as a dozen failures.

Cause is one-time and specific to the apply that *introduces* a mount
above paths that already have mounts of their own. The nested mount was
established against the real directory; the new mount then covered its
parent, so the nested one is still attached but no longer reachable at
the path it occupies. Its own `ExecStop` is a no-op there --
`mountpoint -q` resolves through the new mount and correctly reports
nothing mounted -- so stopping and starting the unit cannot clear it,
and the fresh mount then refuses the still-occupied mountpoint.
Restarting the covering mount does not help either: `PartOf=`
propagates a stop to the nested unit, but the stranded mount is not
what that unit is holding.

It happens **in the visible tree only**, when a node gains a
presentation (`stortree-mount@`) over descendants that already had
mounts. Layer 1 cannot produce it: transports mount on flat
directories of their own, so none is ever above another
(spec.md §2 "The two layers").

For a single stranded mount, stop from the outside in, unmount it while
it is reachable, then start in the other order:

```sh
systemctl stop 'stortree-mount@<nested-slug>.service'
systemctl stop 'stortree-mount@<covering-slug>.service'
fusermount -uz /srv/stortree/<nested path>        # now reachable
systemctl start 'stortree-mount@<covering-slug>.service'
systemctl start 'stortree-mount@<nested-slug>.service'
```

When several are stranded at once -- the usual case, since one covering
mount strands every mount beneath it -- do not try to unpick them
individually. Tear the host's mounts down completely and let the next
apply rebuild them in dependency order. Stop the families outside in,
then unmount whatever is left **deepest path first**, so no unmount is
attempted through a mount that is about to go away:

```sh
for fam in 'stortree-bind@*' 'stortree-mount@*' 'stortree-remote@*'; do
  for u in $(systemctl list-units "$fam" --no-legend --plain --all | awk '{print $1}'); do
    systemctl stop "$u"
  done
done
systemctl reset-failed

findmnt -t fuse.rclone,fuse -no TARGET \
  | awk '{print length($0)" "$0}' | sort -rn | cut -d' ' -f2- \
  | while read -r t; do fusermount -uz "$t"; done
```

Repeat the `findmnt` step until it prints nothing (a lazy unmount can
take a moment to detach), confirm no `rclone`/`bindfs` processes are
left, then re-apply. Safe because nothing here holds state: every mount
is reconstructed from the plan, and the data lives on the backends.

Confirm afterwards with `findmnt`: each nested mount should render as a
*child* of the mount above it, not a sibling. Nothing to do on a host
that gets the whole tree in one apply from a clean state -- the units'
own `After=`/`PartOf=` order them correctly from then on -- and it
cannot recur once a host is past the apply that introduced the covering
mount.

## "N path(s) ... are still missing" at the end of a mounts run

`stortree_mounts` creates directories with `ignore_errors: true`
throughout, because a stale mount must not abort the play before the
render/restart step that would fix it (see that file's header). The
price is that the creation tasks themselves can't report a real failure,
so the role re-stats every path it expected at the end of the run and
prints whatever is still missing.

One missing path is not automatically a bug:

- **Expected.** You just added a nested entry, and its own parent mount
  didn't exist yet when creation was attempted. This is the documented
  two-run pattern -- run `site.yml` again and the list should be empty.
- **Expected, and pointing at something else.** Every path inside a
  transport is listed, because that transport is down. The role skips
  creating anything inside a transport it cannot see mounted, rather
  than writing it to the local disk under the mountpoint, which rclone
  would then refuse to ever mount over. The missing paths are a
  symptom: fix the transport -- `systemctl status
  stortree-remote@<slug>` -- and they are created on the next apply.
- **Real.** The same paths are still listed after a second, back-to-back
  apply. Scroll back to the `ignore_errors`'d directory tasks in the run
  output: the actual error (a full disk, a backend rejecting the write,
  a permission bug) is on the `...ignoring` line there, which is the
  thing this report exists to stop you from scrolling past.

Paths under a masked mount are excluded from the report entirely -- see
"A masked mount" above, which is its own known state.

To make it fatal instead of advisory, set `stortree_mounts_strict=true`:

```
ansible-playbook playbooks/site.yml -e stortree_mounts_strict=true
```

Worth doing in CI or as a post-deploy gate, where a *converged* fleet is
the expectation and a second apply should be clean. Leave it off for an
ordinary apply that adds tree entries, or the legitimate first of the
two runs will fail.

## Changing Samba's `[global]` settings (e.g. `workgroup`)

stortree renders the whole of `/etc/samba/smb.conf`, so hand-editing it
is overwritten on the next apply. Set `stortree_samba_globals` instead
(inventory, `group_vars`, or `-e`) -- it merges over stortree's own
built-in globals, so a key of the same name replaces that value rather
than adding a second line:

```yaml
# group_vars/all.yml
stortree_samba_globals:
  workgroup: EXAMPLE
  server string: "%h (stortree)"
```

`testparm -s` validates the rendered file before it is written, so a
misspelled directive fails the apply rather than reaching the host. It
will *not* catch an override that is valid but defeats the access model
-- `security` and `passdb backend` are load-bearing (see
`smb.conf.j2`'s own comment).

## Turning host discovery off (or back on)

Every host announces itself on its local network by default, over all
three of WS-Discovery (Windows Explorer's "Network"), mDNS (macOS
Finder, GNOME Files) and NetBIOS. Discovery is per host, so ordinary
Ansible precedence applies:

```yaml
# host_vars/storage-node-bravo.yml -- silence this host entirely
stortree_samba_discovery: false

# ...or group_vars/all.yml -- keep mDNS, drop the other two fleet-wide
stortree_samba_discovery_wsd: false
stortree_samba_discovery_netbios: false
```

Re-run `site.yml`. Turning a flag off is a converging change, not just a
skipped task: the apply stops and disables whatever it previously
started and removes what it published, so a host that was announcing
stops. The WSD daemon's package and `avahi-daemon` stay installed, the
same call this role makes for `samba` itself on a host that stops
serving; both unit names a WSD announcer can go by (`wsdd`, `wsdd2`) are
stopped, disabled and un-rendered, so a host that changed
implementations is not left announcing through the one it moved off.

On a multi-homed host — a storage node that also holds a WireGuard
tunnel or a management network — name the interfaces that should carry
the announcements rather than leaving the announcer on all of them:

```yaml
stortree_samba_wsd_interfaces: [eth0]
```

Exactly one entry on a host running `wsdd2` (Debian trixie and newer),
whose `-i` is not repeatable and takes an interface *name* only; a
longer list fails the apply rather than quietly announcing everywhere.
`wsdd` takes as many as you like, by name or address.

Avahi takes its interface policy from `avahi-daemon.conf`, which stortree
does not manage; set `allow-interfaces` there if the same host should not
publish mDNS everywhere either.

### A host that still doesn't appear

Check the right protocol for the client that can't see it — they do not
substitute for each other:

```bash
systemctl status wsdd wsdd2 nmbd avahi-daemon  # on the host; one WSD unit exists
wsdd --discovery --no-host -v                  # from a Linux box on the LAN
avahi-browse -rt _smb._tcp                     # ...for the macOS path
```

Which WSD unit a host has depends on which daemon its archive packages:
`wsdd` up to Debian bookworm and on the Ubuntus, `wsdd2` from Debian
trixie. The apply reports the one it settled on;
`stortree_samba_wsd_implementation` pins it if you would rather a
platform mismatch fail than be worked around.

Three things account for most of it. Discovery is link-local by design:
WSD and mDNS multicast with a hop limit of 1 and NetBIOS broadcasts, so
a client on another subnet or VLAN will never see the host however
healthy the daemons are — that is the protocol, not a fault, and such a
client needs the hostname. `wsdd` is in `universe` on Ubuntu, so a host
with universe disabled has neither daemon installable, and the apply
says so by name rather than failing on apt's bare "no package". And on
a host running systemd-resolved with MulticastDNS enabled, resolved and
avahi-daemon
both want UDP 5353 and whichever started first keeps it; stortree manages
neither daemon's own configuration.

Nothing here affects who can read what. A host nobody can find is still
mountable by anyone who types `\\host\share`, and `valid users` is still
the thing that says no.

## A shutdown or reboot that takes minutes

Symptom: `systemctl reboot` sits for several minutes; the journal from
the previous boot is full of `Stopping timed out. Terminating.`,
`Killing process N (bindfs) with signal SIGKILL`, and finally
`Processes still around after final SIGKILL. Entering failed mode.`

`bindfs` reads through the rclone mount below it. At shutdown that lower
layer is torn down in the same pass, so bindfs can end up blocked in the
kernel on a FUSE mount whose daemon is already gone. A task in
uninterruptible sleep cannot be killed by anything, SIGKILL included --
which is what that last message means. systemd then applies
`TimeoutStopSec` once per rung of stop -> stop-sigterm -> final-sigterm
-> give up, so the 90s default is really six minutes per wedged unit,
and they stop in sequence rather than together.

Two settings close it, and both are already in the rendered units:
`umount -l` in the bind units (a lazy unmount detaches at once and never
blocks, which is what the rclone layers have always done with
`fusermount -uz`), and `stortree_mounts_stop_timeout` (30s) capping the
ladder for the bind and presentation layers. Re-apply to pick them up on
a host still running older units.

The transports keep systemd's own 90s deliberately -- an rclone mount in
`--vfs-cache-mode full` flushes pending uploads when it stops, and
cutting that short only moves the upload to the next start. Lower
`stortree_mounts_stop_timeout` further if a host still drags; raising it
is almost never the answer, since a lazy unmount that has not returned
in 30s is not going to.

A reboot is also worth checking *after* the fact, for a different
reason: the transports come back, but a presentation whose transport
failed its first start attempt does not (see below).

## Shares are empty after a reboot

Symptom: every `stortree-remote@` unit is `active running`, every
`stortree-mount@` and `stortree-bind@` is `inactive dead` -- not
`failed` -- and the paths they present list zero entries. Nothing
reports an error.

This is fixed, in two places. Read on if you see it anyway.

**What went wrong.** A transport fails its first start, recovers a few
seconds later through `Restart=on-failure`, and takes its whole subtree
with it on the way down. The journal shows the pair plainly:

```
20:25:59  rclone: Failed to start metrics server: listen tcp
          10.10.0.4:27031: bind: cannot assign requested address
20:25:59  Dependency failed for stortree-mount@...whitfield-media
20:26:11  Started stortree-remote@...                     <- recovers, alone
```

Two independent defects, both now addressed:

1. *Why the transport failed at all.* rclone binds its metrics server at
   startup and exits if the address is not there yet -- and on this
   fleet `stortree_metrics_bind` names `tailscale0`, which is not up
   when the unit is first reached. Each transport now waits for its own
   addresses first; see [An endpoint that never comes up on an
   interface](#an-endpoint-that-never-comes-up-on-an-interface).
2. *Why nothing recovered.* `PartOf=` propagates a stop downwards but
   never brings anything back, and systemd propagates a **requested**
   restart to `PartOf=` dependents while an automatic `Restart=` is not
   one. Presentations and binds now carry `UpheldBy=` on each parent
   they are `PartOf=`, so as long as the parent is active systemd keeps
   them up, retried continuously.

**If you still see it.** Re-applying fixes it -- `stortree_mounts`'
"Enable and start every rendered unit" starts each presentation and
bind, and by then the transports are up. There is no cleanup to do
first. Then find out which half did not hold:

```sh
# Did a transport fail at boot, and why?
journalctl -b -u 'stortree-remote@*' --no-pager | grep -iE 'fail|error'

# Is the UpheldBy edge actually on the unit?
systemctl show stortree-mount@<slug>.service -p UpheldBy
```

An empty `UpheldBy` means the host is running units rendered before this
change; one apply rewrites them.

## An apply that takes hours

Known, not fixed. What follows is what is understood about it.

The cost is concentrated in `stortree_mounts`' "Enable and start every
rendered unit" and "Restart any unit whose file actually changed", and
it is paid per unit, serially:

* `bindfs` **blocks** when the source it is mounting cannot be read,
  rather than failing. A presentation whose transport is not up, or
  whose transport is up but reading through it stalls, therefore holds
  its start job until `TimeoutStartSec` -- systemd's default 90s --
  expires. systemd then kills it, `Restart=on-failure` waits
  `RestartSec=5`, and the whole 95s repeats. `StartLimitBurst` never
  intervenes, because five starts at 95s apart do not fall inside the
  10s `StartLimitIntervalSec` window.
* Ansible's `systemd_service` waits for each of those jobs. Ten mounts
  in that state is over fifteen minutes in one task, with nothing
  printed in between.

What puts a mount in that state on this fleet is the peer chain. A host
serves its peers over sftp out of its own visible tree, so a read from
one host traverses SMB or sftp, the peer's `bindfs`, the peer's
`rclone`, and finally the third-party backend. Anything slow or flapping
at any hop is a start that hangs rather than a start that fails.
Observed directly on this fleet, during an apply:

```
rclone[...]: ERROR : home/fp/: Dir.Stat error: error listing "home/fp": connection lost
```

-- with the peer's `sshd` accepting a fresh connection in the same
second, so this is the sftp session being torn down and remade
underneath a listing, not a host refusing connections. `MaxSessions`
and `MaxStartups` on the serving host were checked and are not being
hit.

Two things genuinely help today, both of them already in the repo:

* `throttle: 1` on the restart task, so the fleet no longer does this to
  itself from both ends at once (see [Restarting mounts on a fleet that
  peer-mounts itself](#restarting-mounts-on-a-fleet-that-peer-mounts-itself)).
* Not letting transports fail at boot in the first place, which is what
  the metrics-address wait is for -- a transport that never failed is a
  presentation that never hangs.

What would fix it properly is bounding how long a presentation may
block, and that needs a number nobody has measured yet: too short and a
cold cache on a slow backend becomes a mount that never comes up, which
is worse than a slow one. `TimeoutStartSec` on
`stortree-mount@.service.j2` is where it would go.

If an apply is grinding, this is how to tell it is this and not
something else:

```sh
# Units stuck starting, and for how long.
systemctl list-jobs --no-pager | grep running

# The tell: a start that timed out rather than failed.
journalctl -u 'stortree-mount@*' | grep -E 'start operation timed out|Killing process'
```

## Two hosts that peer-mount each other can stall

Not the old mount-time deadlock -- that one needed a host's own
mountpoints to live inside a mount it peers from the other host, and
layer 1 being flat ended it (spec.md §2). Mounts now always start. What
survives is the same cycle at *read* time, and it is quieter.

Two hosts that peer each other read through one another: one serves its
visible tree out of a mount sourced from the other, and that other
serves the subtrees it peers out of the first one's visible tree. So a
stall anywhere on the cycle travels all the way around it.

**Symptom.** Reads hang rather than fail, on more hosts than the one
with the problem, while `systemctl` insists everything is fine --
`Upholds=` keeps the units `active (running)` because the processes are
alive; it is the *requests* that are stuck.

**Find the one that is actually broken**, rather than the ones waiting
on it. Layer 1 and layer 2 fail separately, so test them separately on
each host: a transport reads at `<remotes_root>/<its flat dir>`, its
presentation at the visible path on top.

```sh
findmnt -no SOURCE /srv/.stortree-remotes/<flat dir>     # what it is
timeout 10 ls /srv/.stortree-remotes/<flat dir>          # layer 1
timeout 10 ls /srv/stortree/<path>                       # layer 2
```

A host where layer 1 answers quickly and layer 2 hangs is the culprit,
and it is local to that host -- nothing upstream is involved. That is
worth knowing before touching the peers, because the hosts that merely
*wait* on it look identically broken from the outside. Seen in
production: one host's root transport answered in 26ms while its
presentation hung past 10s, and on the strength of the hang alone two
other hosts had already been blamed.

The usual culprit is a wedged presentation. Each `bindfs` is
single-threaded (`Tasks: 1` in `systemctl status`), so one request that
never returns blocks that entire mount and everything nested inside it.
`ps -o stat,wchan` shows it parked in `request_wait_answer`.

**Fix the broken one, in this order.** The order matters -- see
"A lazy unmount that will not return" for why aborting first and
starting second does not work.

```sh
# 1. Try the ordinary restart first; it often just works.
systemctl restart 'stortree-mount@<slug>.service'

# 2. If that hangs, the mount is wedged. Abort its FUSE connection,
#    THEN clear the mount it leaves stranded, THEN start.
for c in /sys/fs/fuse/connections/*/; do
  w=$(cat "$c/waiting" 2>/dev/null) || continue
  [ "${w:-0}" != 0 ] && echo 1 > "$c/abort"
done
findmnt -t fuse,fuse.rclone,fuse.bindfs -no TARGET | grep '^/srv/' |
  while read -r m; do
    timeout 5 stat "$m" >/dev/null 2>&1 || umount -l "$m"
  done
systemctl reset-failed 'stortree-*'
systemctl start 'stortree-mount@<slug>.service'
```

**Then check the peers, which do not always recover on their own.** A
peer that read an empty or failing directory while the owner was stuck
has that answer cached for `--dir-cache-time` (5m), so it can come back
*mounted and empty* -- which looks healthy and is not. Compare entry
counts across hosts rather than trusting the mount state:

```sh
ls -A /srv/stortree/<path> | wc -l      # on the owner, and on each peer
```

Restart the peer's transport for any that disagree; that refreshes the
cache. Seen in production immediately after a recovery: the owner and
one peer listed five entries, a second peer listed zero in 18ms, and a
transport restart fixed it.

## Restarting mounts on a fleet that peer-mounts itself

Any edit to a unit template re-renders every unit on every host, and the
apply then restarts each one whose file changed. On a fleet where hosts
peer-mount each other that is sharper than it looks, because a host
serves its peers over sftp **out of its own visible tree**: a transport
restarting here is a source disappearing there.

A peer that is starting its own transport in that window does not
retry and recover. It fails outright:

```
ERROR: Failed to create file system for "peer-<host>-...:/srv/stortree/...":
       stat failed: sftp: "Failure" (SSH_FX_FAILURE)
```

Survivable once. What turns it into an outage is both hosts doing it to
each other at the same time: restart storms on both sides,
`StartLimitBurst` exhausted ("Start request repeated too quickly"), and
transports left behind as **stale FUSE mounts** -- whose mountpoints
then cannot be `stat`'d at all, so the next apply cannot even create the
directory the mount needs. That state does not self-heal; it needs a
lazy unmount by hand.

The restart task runs `throttle: 1`, one host at a time, which removes
the mutual half. If you land in the broken state anyway, recover it in
this order:

```sh
# 1. On each host: stop the transport, which takes its subtree with it.
systemctl stop 'stortree-remote@<slug>.service'

# 2. Find and detach anything left stale. A stale mount fails stat(2)
#    with ENOTCONN ("Transport endpoint is not connected"). Select by
#    filesystem *type*, not by path: /srv/stortree itself may be a
#    mount you put there (a CIFS or NFS share backing the tree root),
#    and a path-glob sweep will try to unmount it too.
findmnt -t fuse.rclone,fuse -no TARGET | grep '^/srv/' |
  while read -r m; do
    timeout 5 stat "$m" >/dev/null 2>&1 || umount -l "$m"
  done

# 3. Clear the start-limit and failed-state bookkeeping.
systemctl reset-failed 'stortree-remote@*.service' \
                       'stortree-mount@*.service' 'stortree-bind@*.service'
```

Then re-apply. Do the host that **owns** the subtree first: a peer
cannot mount a path its owner is not currently serving.

If step 2 hangs rather than returning, the mount is wedged rather than
merely stale -- see "A lazy unmount that will not return" above.

## Stopping a mount by hand

`systemctl stop stortree-mount@<slug>` will not keep it stopped. Every
presentation and bind is `UpheldBy=` the mount below it (see [Shares are
empty after a reboot](#shares-are-empty-after-a-reboot)), so while that
parent is active systemd restarts this one within seconds. That is the
point -- it is what makes the tree converge after a transport blips --
but it does surprise people.

To actually take a path down, stop the mount *below* it. `PartOf=`
carries the stop upwards through every layer, and a parent that is not
running upholds nothing:

```sh
# The whole subtree, transport included.
systemctl stop 'stortree-remote@<transport-slug>.service'
```

Two things that look like they should work and do not:

* `systemctl mask` **fails on these units**: stortree renders one real
  file per instance into `/etc/systemd/system`, and masking works by
  putting a symlink to `/dev/null` in exactly that place --
  `Failed to mask unit: File '/etc/systemd/system/stortree-mount@....service'
  already exists`. Masking into `/run/systemd/system` does not help
  either; `/etc` has the higher precedence of the two.
* `systemctl disable` changes nothing. `Upholds=` is a runtime
  dependency of the parent, not an `[Install]` edge, so a disabled unit
  is upheld exactly as before.

## Taking a host out of Samba service

Narrow `stortree_samba_hosts` (defaults to the whole fleet) and re-run
`site.yml`. The host stops exporting shares, stops peer-mounting the
content that existed only to back them, and has its `smbd` stopped and
disabled; the package and `/etc/samba/smb.conf` are deliberately left in
place for you to remove by hand if the host is done with Samba for good.
It also stops announcing itself, on all three protocols above: a host
that exports nothing has nothing to be found for. Its own subtree mounts
of the tree are unaffected. See
[config-schema.md](config-schema.md) "What universality costs" for why
this is a fleet-level list rather than a per-host flag.

## Recovering the control node

Only `stortree/` (plus the vault password, stored separately) is the
source of truth; losing it doesn't affect already-converged managed
hosts, just the ability to change them further. Back up `stortree/` like
any other credential-bearing directory, and keep the vault password
somewhere that does *not* travel with that backup (spec.md "Design
decisions / future work").
