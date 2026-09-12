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
as a node's `host:` or under `clients:` -- neither is required, see
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

Reports, per host: resolved server subtrees, whether it has a client
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
  `rclone version`; Debian bookworm ships 1.60.1, eight releases before
  `--metrics-addr`.

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

An interface that appears later than the mount (WireGuard, a bridge
something other than networkd brings up) means rclone had nothing to
bind when it started. The unit orders itself after that interface's
`.device` unit where systemd knows about it, and `Restart=on-failure`
covers a late arrival -- but only within systemd's default start-limit
burst, so an interface that takes minutes needs
`systemctl restart stortree-remote@<slug>` afterwards. Binding loopback
and scraping through a tunnel avoids the whole class.

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

It happens in **either root**, and the remotes root is the more likely
of the two during a migration:

- in the visible tree, when a node gains a presentation
  (`stortree-mount@`) over descendants that already had mounts;
- under `stortree_remotes_root`, when a subtree gains a transport
  (`stortree-remote@`) above transports that were already mounted at
  paths beneath it -- which is what happens the first time a host that
  peer-mounts only some leaves starts peer-mounting the whole subtree.

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

## Taking a host out of Samba service

Narrow `stortree_samba_hosts` (defaults to the whole fleet) and re-run
`site.yml`. The host stops exporting shares, stops peer-mounting the
content that existed only to back them, and has its `smbd` stopped and
disabled; the package and `/etc/samba/smb.conf` are deliberately left in
place for you to remove by hand if the host is done with Samba for good.
Its own client mounts of the tree are unaffected. See
[config-schema.md](config-schema.md) "What universality costs" for why
this is a fleet-level list rather than a per-host flag.

## Recovering the control node

Only `stortree/` (plus the vault password, stored separately) is the
source of truth; losing it doesn't affect already-converged managed
hosts, just the ability to change them further. Back up `stortree/` like
any other credential-bearing directory, and keep the vault password
somewhere that does *not* travel with that backup (spec.md "Design
decisions / future work").
