"""Shared test setup.

Two jobs: put the repo root on sys.path so `filter_plugins.stortree`
imports as a plain module (it's an Ansible filter plugin, not an
installed package), and provide the Jinja environment tests/test_
templates.py uses to render the roles' real `.j2` files outside a
running playbook.
"""

import sys
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from filter_plugins.stortree import (  # noqa: E402  (needs sys.path above)
    FilterModule,
    plan_mounts,
    resolve,
    staged_node_paths,
)

FIXTURES = Path(__file__).parent / "fixtures"

# The three "real" hosts of the worked example (docs/config-schema.md),
# matching molecule/full-tree/molecule.yml's own platform names.
EXAMPLE_HOSTS = ["storage-node-alpha", "storage-node-bravo", "some-storage-gadget"]

# What `getent group` returns on a host for the worked example's groups
# -- resolve() is pure and never sees this, but everything downstream of
# it (plan_mounts, staged_node_paths, the templates) does.
EXAMPLE_GROUP_MEMBERS = {
    "Whitfield Family & Friends": ["jd", "mw"],
    "Michael Whitfield Family": ["mw"],
    "Media Production": ["jd"],
}

# Defaults from roles/stortree_common/defaults/main.yml, plus the
# uid/gid maps roles/stortree_mounts builds from `getent`. Kept here so
# a template test starts from the same variable set a real play gives
# the template.
COMMON_VARS = {
    "stortree_root": "/srv/stortree",
    "stortree_etc": "/etc/stortree",
    "stortree_user": "stortree",
    "stortree_group": "stortree",
    "stortree_uid": 900,
    "stortree_gid": 900,
    "stortree_user_uids": {"jd": 10001, "mw": 10002},
    "stortree_group_gids": {
        "Whitfield Family & Friends": 20001,
        "Michael Whitfield Family": 20002,
        "Media Production": 20003,
    },
}


def load_fixture(name):
    return yaml.safe_load((FIXTURES / name).read_text())


def _ansible_filters_and_tests():
    """The handful of ansible.builtin filters/tests the roles' templates
    use on top of Jinja's own -- imported from ansible-core itself
    rather than reimplemented, so a template test exercises the same
    `regex_replace`/`combine`/`search` a real play would."""
    from ansible.plugins.filter.core import FilterModule as AnsibleCoreFilters
    from ansible.plugins.test.core import TestModule as AnsibleCoreTests

    return AnsibleCoreFilters().filters(), AnsibleCoreTests().tests()


@pytest.fixture(scope="session")
def jinja_env():
    """A Jinja environment set up the way ansible.builtin.template sets
    one up: trim_blocks on, lstrip_blocks off, trailing newline kept,
    every stortree filter registered under the same name FilterModule
    exposes to a play, and ansible-core's own AnsibleUndefined.

    AnsibleUndefined matters: it's a StrictUndefined (so a variable the
    role forgot to set is an error here, not a silently empty systemd
    directive on a real host) whose attribute access chains, which is
    what lets `stortree_identity_ldap.extra.sssd | default({})` in
    sssd.conf.j2 work against a config with no `extra:` key at all.
    Plain StrictUndefined would fail that, and the failure would be the
    test harness's, not the template's.
    """
    from ansible.template import AnsibleUndefined

    ansible_filters, ansible_tests = _ansible_filters_and_tests()
    env = Environment(
        loader=FileSystemLoader(
            [str(p) for p in sorted(REPO_ROOT.glob("roles/*/templates"))]
        ),
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
        undefined=AnsibleUndefined,
        autoescape=False,
    )
    env.filters.update(ansible_filters)
    env.filters.update(FilterModule().filters())
    env.tests.update(ansible_tests)
    return env


@pytest.fixture(scope="session")
def render(jinja_env):
    """render("stortree-mount@.service.j2", entry=..., ...) -> str."""

    def _render(template_name, **variables):
        merged = dict(COMMON_VARS)
        merged.update(variables)
        return jinja_env.get_template(template_name).render(**merged)

    return _render


@pytest.fixture(scope="session")
def example_tree():
    return load_fixture("example_tree.yml")


@pytest.fixture(scope="session")
def resolved(example_tree):
    """{hostname: resolve(...)} for all three hosts of the worked example."""
    return {h: resolve(example_tree, h, EXAMPLE_HOSTS) for h in EXAMPLE_HOSTS}


@pytest.fixture(scope="session")
def mount_plans(resolved):
    """{hostname: plan_mounts(...)} -- what roles/stortree_mounts loops
    its unit templates over."""
    return {h: plan_mounts(r, EXAMPLE_GROUP_MEMBERS) for h, r in resolved.items()}


@pytest.fixture(scope="session")
def containers(resolved, mount_plans):
    """{hostname: staged_node_paths(...)} -- stortree_staged_nodes."""
    return {
        h: staged_node_paths(r, EXAMPLE_GROUP_MEMBERS, mount_plans[h])
        for h, r in resolved.items()
    }

