# Running stortree from Semaphore UI

How to run this project from [Semaphore UI](https://semaphoreui.com/)
instead of `ansible-playbook` by hand. See
[runbook.md](runbook.md) for the manual equivalent of everything below.

## Config repo

`inventory/hosts.yml` and `stortree/config.yml`/`ldap.yml`/`rclone.conf`
are git-ignored in this repo (see `.gitignore`) — real, site-specific
config is never committed here. For Semaphore to run against a real
fleet, keep those files in a second, private repo instead, mirroring the
layout the roles expect:

```
stortree-config/                  (private repo)
├── inventory/
│   └── hosts.yml                 # real fleet, plaintext (no secrets in here)
└── stortree/
    ├── config.yml                # real tree definition, plaintext
    ├── ldap.yml                  # ansible-vault encrypted
    ├── rclone.conf                # ansible-vault encrypted
    └── sshd_config                # optional -- only if using the Match Group/ForceCommand pattern
```

Encrypt `ldap.yml` and `rclone.conf` before committing (same as the
manual setup in [runbook.md](runbook.md#first-time-setup)):

```
ansible-vault encrypt stortree/ldap.yml stortree/rclone.conf
```

Don't commit the vault password anywhere, including this repo — see
[runbook.md "Recovering the control node"](runbook.md#recovering-the-control-node).

## Install Semaphore

```
curl -s https://raw.githubusercontent.com/semaphoreui/semaphore/develop/docker-compose.yml -o docker-compose.yml
docker compose up -d
```

Log in and create a Project (e.g. "stortree").

## Key Store

Add three credentials:

1. **SSH key for managed hosts** — type `SSH Key`. Private key for the
   `ansible_user` your inventory uses (`root` in
   [hosts.yml.example](../inventory/hosts.yml.example)).
2. **Deploy key(s) for the repo(s)** — `SSH Key` or HTTPS login, for
   cloning `stortree` and the private `stortree-config` repo if
   either is private.
3. **Vault password** — type `Login with Password` (username can be
   blank). Password = the `ansible-vault` password for
   `stortree/ldap.yml` and `stortree/rclone.conf`. Attach this to Task
   Templates as the Vault Password key.

## Repositories

Add both repos under **Repositories**:

- `stortree` (branch `master`) — holds `playbooks/`, `roles/`,
  `ansible.cfg`, `requirements.yml`.
- `stortree-config` — holds the real `inventory/` and `stortree/` from
  above.

## Inventory

Add an Inventory of type **File**, selecting the `stortree-config`
repository and the relative path `inventory/hosts.yml`, using the
managed-host SSH key from Key Store step 1. Semaphore resolves this to
a real filesystem path itself right before each run, so you never need
to know or hardcode `stortree-config`'s checkout path.

## Config path override

[roles/stortree_facts/defaults/main.yml](../roles/stortree_facts/defaults/main.yml)
computes every config path from `stortree_repo_root`, which defaults to
`{{ playbook_dir }}/..` — i.e. inside the `stortree` checkout. Since
the real config lives in a separate repo, override it via a Variable
Group (Extra Variables, JSON) on the Task Template:

```json
{ "stortree_repo_root": "{{ inventory_dir }}/.." }
```

This derives the root from wherever Semaphore actually checked out
`stortree-config` for the Inventory above, instead of a hardcoded path
— it stays correct even if Semaphore's internal checkout location
changes between runs or versions. It relies on `inventory/hosts.yml`
sitting directly under `stortree-config`'s root, alongside `stortree/`
(`config.yml`, `ldap.yml`, etc.), which matches the layout above.

This one override redirects all four derived paths at once:
`stortree_config_path`, `stortree_ldap_path`, `stortree_rclone_conf_path`,
`stortree_sshd_config_path`. Because it's a single shared root, keep all
of `stortree/config.yml`, `ldap.yml`, `rclone.conf`, and `sshd_config`
together under that one repo rather than splitting them further.

## Collections requirements

Semaphore's Ansible task type auto-runs
`ansible-galaxy collection install -r requirements.yml` before each task
when that file exists at the repo root — it does
([requirements.yml](../requirements.yml)), so `ansible.posix`,
`community.crypto`, and `community.general` install automatically. No
extra config needed. Make sure the runner's `ansible-core` version
satisfies the repo's pin (`>=2.16,<2.18` in
[requirements.txt](../requirements.txt)); the rest of that file
(`molecule`, `pytest`, `ansible-lint`, `yamllint`) is dev/test tooling
Semaphore doesn't need.

## Task Templates

**"stortree — apply"**
- Playbook: `playbooks/site.yml`
- Repository: `stortree`; Inventory + Variable Group as above
- Vault Password: the key from Key Store step 3

**"stortree — status"** (read-only, see [status.yml](../playbooks/status.yml))
- Playbook: `playbooks/status.yml`
- Same repository/inventory/vault key

**"stortree — dry run"** (optional)
- Same as apply, with CLI args `--check --diff`

For occasional `--limit`/`--tags` runs (tags: `facts`, `common`,
`identity`, `peer_trust`, `secrets`, `mounts`, `samba`,
`pam_smbpass`, `sshd` — see [runbook.md](runbook.md#apply-just-one-concern)),
either add dedicated templates per common case, or enable "Allow CLI args
override" if your Semaphore version supports it.

For the opt-in `mount-recover` tag (a masked root mount, see
[runbook.md](runbook.md#recovering-a-masked-root-mount)), either enable
"Allow CLI args override" so `--tags mount-recover,common,mounts --limit
<host>` can be passed at run time, or add a dedicated one-off Task
Template for it — it's deliberately excluded from every other template's
normal tags (`never`), so it never fires unless asked for by name.

## Gotchas

- **`host_key_checking = True`** in [ansible.cfg](../ansible.cfg) means
  the Semaphore runner needs `known_hosts` entries for every managed
  host, or the first run fails on host-key prompts it can't answer
  non-interactively. Pre-seed `known_hosts` in the runner image/volume,
  or temporarily set `ANSIBLE_HOST_KEY_CHECKING=False` on the Environment
  while bootstrapping.
- **`become: true` with `ansible_user: root`** (per the inventory
  example) makes privilege escalation a no-op — no become-password
  prompt needed.
- Run **"stortree — status"** first against a reachable host. It's
  non-destructive and confirms SSH, the `stortree_repo_root` override,
  and vault decryption all work before running the state-changing
  `site.yml` template.
