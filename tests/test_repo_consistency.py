"""Guards against the copies of a thing drifting apart.

Three separate things in this repo are documented as being copies of, or
1:1 with, something else -- the worked example config (three copies),
the Molecule converge playbook (says it's 1:1 with playbooks/site.yml),
and the Molecule fleet list (must match that scenario's own platforms).
Each is currently kept in sync by hand, and drift in any of them is
silent: the tests keep passing against a stale fixture, or the scenario
resolves a different tree than the playbook it claims to mirror.

Also here: the repo-hygiene rule from docs/plan.md that no real config
ever gets committed, which is worth a test precisely because it's the
kind of mistake you only make once, and the handful of paths the filter
plugin has to spell the same way the roles do.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import EXAMPLE_HOSTS, REPO_ROOT
from filter_plugins.stortree import (
    DEFAULT_STORTREE_ETC,
    DEFAULT_STORTREE_ROOT,
    PEER_SSH_KEY_NAME,
    mount_unit_names,
    plan_mounts,
    resolve,
    present_unit_names,
)

# The three copies of docs/config-schema.md's worked example: what the
# unit tests resolve, what an operator copies to start from, and what
# the Molecule scenario applies.
EXAMPLE_COPIES = [
    Path("tests/fixtures/example_tree.yml"),
    Path("stortree/config.yml.example"),
    Path("molecule/fixtures/stortree/config.yml"),
]

SITE_PLAYBOOK = REPO_ROOT / "playbooks/site.yml"
FULL_TREE = REPO_ROOT / "molecule/full-tree"


def load_yaml(relative_path):
    return yaml.safe_load((REPO_ROOT / relative_path).read_text())


# -- the worked example, in triplicate ------------------------------------


@pytest.mark.parametrize("copy", EXAMPLE_COPIES[1:], ids=lambda p: str(p))
def test_every_copy_of_the_worked_example_parses_the_same(copy):
    # Compared as parsed data, not bytes: a differing comment or a
    # leading `---` is fine, a differing tree is not. tests/fixtures is
    # the reference, since it's what the unit tests actually assert on.
    assert load_yaml(copy) == load_yaml(EXAMPLE_COPIES[0])


def test_the_shipped_example_resolves_for_every_host_in_its_fleet():
    # The file an operator copies to stortree/config.yml has to at least
    # resolve. Cheap end-to-end smoke test of the real artifact rather
    # than of the fixture standing in for it.
    tree = load_yaml("stortree/config.yml.example")
    for host in EXAMPLE_HOSTS:
        resolved = resolve(tree, host, EXAMPLE_HOSTS)
        assert set(resolved) == {
            "server_subtrees",
            "client_mounts",
            "samba_shares",
            "peer_dependencies",
            "peer_served_by",
        }
        # Samba sharing is universal, so every host exports the one
        # samba-configured node whether or not it owns any of it.
        assert len(resolved["samba_shares"]) == 1


def test_the_shipped_example_resolves_for_a_host_it_never_mentions():
    # spec.md §1 calls this out explicitly: a host in the inventory but
    # absent from config.yml still participates.
    tree = load_yaml("stortree/config.yml.example")
    fleet = EXAMPLE_HOSTS + ["a-host-config-never-heard-of"]
    resolved = resolve(tree, "a-host-config-never-heard-of", fleet)
    assert resolved["server_subtrees"] == []
    assert resolved["samba_shares"] != []
    assert resolved["peer_dependencies"] != []


def test_the_shipped_example_plans_mounts_without_a_slug_collision():
    # plan_mounts() raises on two paths that collide into one systemd
    # unit name. Nothing else runs the *shipped* example through it.
    tree = load_yaml("stortree/config.yml.example")
    members = {
        "Whitfield Family & Friends": ["jd", "mw"],
        "Michael Whitfield Family": ["mw"],
        "Media Production": ["jd"],
    }
    for host in EXAMPLE_HOSTS:
        assert plan_mounts(resolve(tree, host, EXAMPLE_HOSTS), members)


# -- Molecule scenario vs. the playbook it mirrors -------------------------


def play_roles(playbook_path):
    (play,) = yaml.safe_load(playbook_path.read_text())
    return play["roles"]


def test_the_molecule_converge_applies_the_same_roles_as_site_yml():
    # molecule/full-tree/converge.yml's own header says it's 1:1 with
    # playbooks/site.yml. A role added to one and not the other means
    # the scenario stops testing the thing it exists to test -- in the
    # same order, since these roles depend on each other's facts.
    assert play_roles(FULL_TREE / "converge.yml") == play_roles(SITE_PLAYBOOK)


def test_the_molecule_fleet_matches_the_scenarios_own_platforms():
    # stortree_all_hosts is what resolve() treats as the fleet. If a
    # platform is added to molecule.yml and not here, every host
    # resolves as though it didn't exist -- no peer dependency on it, no
    # share of its subtree -- and the scenario passes anyway.
    molecule = yaml.safe_load((FULL_TREE / "molecule.yml").read_text())
    (converge,) = yaml.safe_load((FULL_TREE / "converge.yml").read_text())

    real_hosts = [
        p["name"] for p in molecule["platforms"] if "mocks" not in p.get("groups", [])
    ]
    assert converge["vars"]["stortree_all_hosts"] == real_hosts
    assert converge["hosts"].split(",") == real_hosts


def test_the_molecule_verify_targets_the_same_hosts_it_converges():
    (converge,) = yaml.safe_load((FULL_TREE / "converge.yml").read_text())
    (verify,) = yaml.safe_load((FULL_TREE / "verify.yml").read_text())
    assert verify["hosts"] == converge["hosts"]


def test_every_role_site_yml_applies_exists_and_has_tasks():
    for role in play_roles(SITE_PLAYBOOK):
        assert (REPO_ROOT / "roles" / role / "tasks" / "main.yml").is_file(), role
        assert (REPO_ROOT / "roles" / role / "meta" / "main.yml").is_file(), role


def test_no_role_on_disk_is_silently_never_applied():
    # A role directory nothing applies is either dead code or a role
    # someone forgot to wire into site.yml -- both worth noticing.
    on_disk = {p.name for p in (REPO_ROOT / "roles").iterdir() if p.is_dir()}
    assert on_disk == set(play_roles(SITE_PLAYBOOK))


# -- repo hygiene (docs/plan.md) ------------------------------------------


def test_no_real_site_config_is_tracked_in_git():
    # These hold real hostnames, topology and credentials. .gitignore
    # covers them; this checks the ignore rules actually held, which
    # `git add -f` or a rule edit can quietly undo.
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    forbidden = {
        "stortree/config.yml",
        "stortree/ldap.yml",
        "stortree/rclone.conf",
        "stortree/sshd_config",
        "inventory/hosts.yml",
    }
    assert forbidden.isdisjoint(tracked)


def test_every_gitignored_site_config_ships_an_example_to_copy():
    # The setup path in README.md is "copy the .example file" -- which
    # only works if there is one.
    for name in ("config.yml", "ldap.yml", "rclone.conf"):
        assert (REPO_ROOT / "stortree" / f"{name}.example").is_file(), name
    assert (REPO_ROOT / "inventory" / "hosts.yml.example").is_file()


def test_the_example_ldap_config_has_the_keys_sssd_conf_j2_reads():
    # sssd.conf.j2 dereferences these unconditionally; an example
    # missing one renders a broken sssd.conf on an operator's first run.
    ldap = load_yaml("stortree/ldap.yml.example")
    assert set(ldap["server"]) >= {"url", "base_dn", "bind_dn", "bind_password"}
    assert set(ldap["posix"]) >= {"uid_attr", "gid_attr"}


# -- filter-plugin fallbacks vs. the role defaults they mirror -------------


def role_defaults(role):
    return load_yaml(Path("roles") / role / "defaults" / "main.yml")


def test_the_plugins_path_fallbacks_match_the_role_defaults():
    # The roles pass `stortree_root`/`stortree_etc` into stortree_resolve,
    # stortree_plan_mounts and stortree_filter_rclone_conf, so an override
    # reaches every generated path. These module-level constants are only
    # the fallback for a call that passes neither -- every unit test here,
    # and any use of the module outside a play. If they drift from the
    # role defaults, the tests assert one set of paths while a real run
    # produces another, which is exactly the gap that makes a fallback
    # worth having a guard on at all.
    assert role_defaults("stortree_facts")["stortree_root"] == DEFAULT_STORTREE_ROOT
    assert role_defaults("stortree_common")["stortree_etc"] == DEFAULT_STORTREE_ETC


def test_stortree_root_has_exactly_one_definition():
    # It lives in stortree_facts, not stortree_common, because
    # playbooks/status.yml applies stortree_facts with no other role in
    # the play -- see that defaults file's own comment. Defining it in
    # both would work by accident (the values agree) right up until one
    # of them was edited.
    defining = [
        role.name
        for role in sorted((REPO_ROOT / "roles").iterdir())
        if role.is_dir()
        and (role / "defaults" / "main.yml").is_file()
        and "stortree_root" in (role_defaults(role.name) or {})
    ]
    assert defining == ["stortree_facts"]


def test_every_play_that_resolves_the_tree_applies_stortree_facts():
    # The single definition above only reaches everything because
    # stortree_facts is in every play. A play that resolved the tree
    # without it would hit an undefined `stortree_root` at the first
    # stortree_resolve call.
    playbooks = [SITE_PLAYBOOK, REPO_ROOT / "playbooks/status.yml"]
    playbooks += sorted((REPO_ROOT / "roles").glob("*/molecule/*/converge.yml"))
    playbooks += sorted((REPO_ROOT / "molecule").glob("*/converge.yml"))
    for playbook in playbooks:
        roles = play_roles(playbook)
        assert roles[0] == "stortree_facts", playbook


def test_the_peer_ssh_key_name_matches_what_stortree_peer_trust_writes():
    # filter_rclone_conf() writes `key_file = {stortree_etc}/{name}` into
    # every synthesized sftp section; stortree_peer_trust is what actually
    # creates the keypair at that path. A rename on either side alone
    # leaves every peer mount authenticating with a key file that isn't
    # there.
    tasks = (
        REPO_ROOT / "roles" / "stortree_peer_trust" / "tasks" / "main.yml"
    ).read_text()
    referenced = {
        name.removesuffix(".pub")
        for name in re.findall(r"\{\{ stortree_etc \}\}/([\w.]+)", tasks)
    }
    assert referenced == {PEER_SSH_KEY_NAME}


def test_every_unit_family_the_plugin_names_is_swept_by_the_role():
    # Three places sweep stortree's units by wildcard -- the `find` that
    # detects stale unit files, the `systemctl reset-failed` that clears
    # ghost state, and playbooks/status.yml's own `list-units` -- and
    # each names the unit families literally. A fourth place invents the
    # names: mount_unit_names()/present_unit_names(). A family added
    # there but missed in any of the three sweeps is a unit that is
    # rendered and started but never listed, never reset, and never
    # cleaned up when it goes stale, on every apply, silently.
    plan = [
        {"local_path": "a", "remote": "r:/", "slug": "a"},
        {"local_path": "b", "remote": None, "symlink_target": "a", "slug": "b"},
    ]
    containers = [{"local_path": "c", "slug": "c", "requires_slug": "a"}]
    families = {
        name.split("@", 1)[0] + "@"
        for name in mount_unit_names(plan) + present_unit_names(containers)
    }
    assert len(families) == 3, f"unexpected unit families: {families}"

    sweeps = {
        "stortree_mounts find + reset-failed": (
            REPO_ROOT / "roles/stortree_mounts/tasks/main.yml"
        ),
        "status.yml list-units": REPO_ROOT / "playbooks/status.yml",
    }
    for where, path in sweeps.items():
        globbed = set(re.findall(r"(stortree-[a-z-]+@)\*\.service", path.read_text()))
        assert families <= globbed, f"{where} misses {families - globbed}"


def test_the_role_derives_no_unit_name_the_plugin_does_not_render():
    # The other half of the same seam: stortree_mounts restarts and
    # enables units by reading each render task's own `dest` back, so a
    # unit's name is written once (in the task that creates the file)
    # rather than re-spelled in the tasks that act on it. A second
    # spelling is what this guards against coming back -- three families
    # x three steps was nine places one rename had to reach.
    tasks = (REPO_ROOT / "roles/stortree_mounts/tasks/main.yml").read_text()
    interpolated = set(re.findall(r"\"(stortree-[a-z-]+@\{\{[^\"]*)\"", tasks))
    assert interpolated == set(), (
        "unit names are being rebuilt in the role instead of read from the "
        f"render task's own dest: {interpolated}"
    )


def test_stortree_samba_hosts_is_defined_once_and_gates_both_ends():
    # The opt-out is only correct if the *same* list reaches resolve()
    # and the stortree_samba role: resolve() decides which shares and
    # peer mounts exist, the role decides whether smbd serves them. Gate
    # one on a different variable than the other and a host either
    # exports shares whose content it never mounted, or mounts content
    # for shares it never exports.
    defining = [
        role.name
        for role in sorted((REPO_ROOT / "roles").iterdir())
        if role.is_dir()
        and (role / "defaults" / "main.yml").is_file()
        and "stortree_samba_hosts" in (role_defaults(role.name) or {})
    ]
    assert defining == ["stortree_facts"]

    # Passed positionally into stortree_resolve by the facts role...
    facts = (REPO_ROOT / "roles/stortree_facts/tasks/main.yml").read_text()
    assert "stortree_samba_hosts" in facts
    assert "stortree_resolve(" in facts

    # ...and read by every task of the samba role, so none of them can
    # act on a host the resolver already excluded.
    samba = yaml.safe_load(
        (REPO_ROOT / "roles/stortree_samba/tasks/main.yml").read_text()
    )
    for task in samba:
        assert "when" in task, task["name"]
        assert "stortree_samba_hosts" in str(task["when"]), task["name"]


def test_the_mounts_verification_covers_the_paths_the_role_creates():
    # The re-stat at the end of stortree_mounts only closes the
    # ignore_errors gap for paths it actually looks at. Both sources the
    # creation tasks loop over -- the mount plan and the per-user
    # containers' staging paths -- have to appear in its loop, or a whole
    # family of directories goes back to failing silently.
    tasks = yaml.safe_load(
        (REPO_ROOT / "roles/stortree_mounts/tasks/main.yml").read_text()
    )
    verify = [t for t in tasks if t["name"].startswith("Re-stat")]
    assert len(verify) == 1
    loop = verify[0]["loop"]
    assert "stortree_mounts_plan" in loop
    assert "staging_path" in loop
    # ...and it must not re-report a masked path, which has its own
    # runbook entry rather than being a failure.
    assert "stortree_path_masked" in str(verify[0]["when"])
