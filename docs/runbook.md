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

## Recovering a masked root mount

A host whose entire `stortree_root` is itself a whole-tree root client
mount (§1) can end up in a state where even root can't reach anything
under it -- e.g. after upgrading to a `stortree-mount@.service.j2` that
changed access flags (`--allow-root`, `--allow-other`), a host that
already had that mount active from before the upgrade keeps running the
old, more restrictive unit until something restarts it. A plain apply
can't fix this on its own: `stortree_common`/`stortree_mounts`'s own
path-creation tasks fail outright trying to reach paths nested under the
still-masked mount, before the run ever gets to the render/restart step
that would have fixed it. Symptom: a task under `stortree_common` or
`stortree_mounts` fails with something like `There was an issue creating
/srv/stortree/... as requested: [Errno 17] File exists`, on a host you
know already had that path mounted.

Recover it with the opt-in `mount-recover` tag (`tags: [never,
mount-recover]` in `roles/stortree_common/tasks/main.yml` -- deliberately
excluded from every normal run, since it briefly stops a live,
user-facing mount):

```
ansible-playbook playbooks/site.yml --ask-vault-pass --tags mount-recover,common,mounts --limit affected-host-name
```

That stops the stuck mount (its own `ExecStop` runs as `stortree`, the
mount's owner, so this succeeds even though root can't reach its
contents) and lets the rest of the run recreate paths and bring it back
up with the correct flags. One-time per host -- once it comes back up
correctly flagged, normal applies never hit this again on that host.

## Recovering the control node

Only `stortree/` (plus the vault password, stored separately) is the
source of truth; losing it doesn't affect already-converged managed
hosts, just the ability to change them further. Back up `stortree/` like
any other credential-bearing directory, and keep the vault password
somewhere that does *not* travel with that backup (spec.md "Design
decisions / future work").
