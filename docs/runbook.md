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

## A stranded mount after a node gains a presentation

Symptom: a nested mount's unit restart-loops with `Fatal error:
directory already mounted, use --allow-non-empty to mount anyway`, and
the path it serves lists as empty. `findmnt` shows it attached as a
sibling of the mount it should be nested inside.

Cause is one-time and specific to the apply that *introduces* a
presentation mount over a path that already has mounts nested under it.
The nested mount was established against the real directory; the new
presentation then mounted over its parent, so the nested mount is still
attached but is no longer reachable at the path it occupies. Its own
`ExecStop` is a no-op there -- `mountpoint -q` resolves through the
presentation and correctly reports nothing mounted -- so stopping and
starting the unit cannot clear it, and the fresh `rclone mount` then
refuses the still-occupied mountpoint. Restarting the presentation
alone does not help either: `PartOf=` propagates a stop to the nested
unit, but the stranded mount is not what that unit is holding.

Fix, once, on that host -- stop from the outside in, unmount the
stranded mount while it is reachable, then start in the other order:

```sh
systemctl stop 'stortree-mount@<nested-slug>.service'
systemctl stop 'stortree-present@<node-slug>.service'
fusermount -uz /srv/stortree/<nested path>        # now reachable
systemctl start 'stortree-present@<node-slug>.service'
systemctl start 'stortree-mount@<nested-slug>.service'
```

Confirm with `findmnt`: the nested mount should now render as a child
of the presentation, not a sibling. Nothing to do on a host that gets
both mounts in the same apply from a clean state -- the units' own
`After=`/`PartOf=` (via `presented_ancestor()`) order them correctly
from then on, and this cannot recur for that node.

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
