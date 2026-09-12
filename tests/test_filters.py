"""The FilterModule contract, and the one filter nothing else covers.

filter_plugins/stortree.py exposes its pure functions to plays through
FilterModule.filters(). Nothing in tests/test_stortree.py touches that
mapping -- it imports the functions directly -- so until now a filter
renamed in the module but not in the mapping, or referenced by a role
under a name the mapping never had, would leave the whole suite green
and break every playbook on the first run.

`path_masked` is here rather than in test_stortree.py for a related
reason: it's registered, roles/stortree_mounts guards half its tasks
with it, and it had no test at all.
"""

import re
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from filter_plugins.stortree import FilterModule, path_masked

FILTERS = FilterModule().filters()

# Where a `stortree_*` filter can be referenced from. Templates count:
# smb.conf.j2 and the unit templates all call one.
SOURCES = sorted(
    [
        *REPO_ROOT.glob("roles/*/tasks/*.yml"),
        *REPO_ROOT.glob("roles/*/handlers/*.yml"),
        *REPO_ROOT.glob("roles/*/defaults/*.yml"),
        *REPO_ROOT.glob("roles/*/templates/*.j2"),
        *REPO_ROOT.glob("playbooks/*.yml"),
        *REPO_ROOT.glob("molecule/*/*.yml"),
    ]
)

# A Jinja filter is always invoked through a pipe, so anchoring on the
# pipe is both sufficient and the only way to tell a filter call from
# the many `stortree_*` *variables* (stortree_root, stortree_mounts_plan,
# ...) that share the prefix. `\s` spans newlines, which matters: the
# roles routinely break a long expression across lines after the pipe.
# Two spellings, because both are real uses: `| stortree_x(...)`, and
# `map('stortree_x', ...)` / `select(...)` / `reject(...)`, where the
# filter is named as a quoted string instead. Missing the second one
# would report a filter the roles genuinely call as dead.
_FILTER_CALL = re.compile(
    r"\|\s*(stortree_[a-z_]+)"
    r"|(?:map|select|reject|selectattr|rejectattr)\(\s*['\"](stortree_[a-z_]+)['\"]"
)

def referenced_filters():
    """Every `stortree_*` name used as a filter anywhere in the roles,
    playbooks or Molecule scenarios, as {name: [files]}."""
    found = {}
    for path in SOURCES:
        for piped, quoted in _FILTER_CALL.findall(path.read_text()):
            name = piped or quoted
            found.setdefault(name, []).append(str(path.relative_to(REPO_ROOT)))
    return found


def test_every_registered_filter_is_callable():
    assert FILTERS, "FilterModule registered no filters at all"
    for name, fn in FILTERS.items():
        assert callable(fn), f"{name} is registered but isn't callable"


def test_every_registered_filter_name_is_namespaced():
    # Ansible's filter namespace is flat and global; an un-prefixed name
    # here would shadow a builtin or another collection's filter.
    unprefixed = [n for n in FILTERS if not n.startswith("stortree_")]
    assert unprefixed == []


def test_no_two_names_point_at_the_same_function():
    # A copy-paste slip in the mapping (two keys, one value) is
    # otherwise invisible -- both names work, and the filter the second
    # one was meant to expose is silently unreachable.
    seen = {}
    for name, fn in FILTERS.items():
        seen.setdefault(fn, []).append(name)
    duplicates = {fn.__name__: names for fn, names in seen.items() if len(names) > 1}
    assert duplicates == {}


def test_every_filter_the_roles_use_is_registered():
    referenced = referenced_filters()
    assert referenced, "found no filter calls at all -- did the regex go stale?"
    missing = {
        name: files for name, files in referenced.items() if name not in FILTERS
    }
    assert missing == {}, (
        "these are called as filters but aren't in FilterModule.filters(): "
        f"{missing}"
    )


def test_every_registered_filter_is_actually_used_somewhere():
    # The other direction: a filter kept in the mapping after its last
    # caller went away -- or, as happened with stortree_access_users,
    # registered speculatively and never called at all -- is dead
    # surface that still has to keep working. Not a hard rule for a
    # library; it is for this one, which exists only to serve these
    # roles. A filter meant for an operator's own plays belongs in
    # docs/runbook.md, and this assertion relaxed to match.
    unused = sorted(set(FILTERS) - set(referenced_filters()))
    assert unused == []


@pytest.mark.parametrize("name", sorted(FILTERS))
def test_every_registered_filter_has_a_docstring(name):
    # These are the project's public surface for anyone writing a play
    # against it, and several take non-obvious arguments.
    doc = (FILTERS[name].__doc__ or "").strip()
    assert doc, f"{name} has no docstring"


# -- path_masked -----------------------------------------------------------


def test_path_masked_matches_the_masked_path_itself():
    assert path_masked("tree/home", ["tree/home"])


def test_path_masked_matches_a_descendant_at_any_depth():
    # The case that motivated walking the whole ancestor chain: a
    # peer-sourced samba descendant nested several levels under a
    # top-level subtree that's still masked from a previous run.
    assert path_masked("tree/home/jd/mw-fam", ["tree"])
    assert path_masked("tree/home/jd", ["tree/home"])


def test_path_masked_does_not_match_a_same_prefix_sibling():
    # "tree" masks "tree/home" but must not mask "treehouse" -- a plain
    # startswith() without the separator would.
    assert not path_masked("treehouse", ["tree"])
    assert not path_masked("tree-backups", ["tree"])


def test_path_masked_does_not_match_an_ancestor_of_a_masked_path():
    # Masking is inherited downward only: a mount being unreachable says
    # nothing about the directory it sits in.
    assert not path_masked("tree", ["tree/home"])


def test_path_masked_with_nothing_masked_is_always_false():
    assert not path_masked("tree/home", [])


def test_path_masked_checks_every_masked_path_not_just_the_first():
    assert path_masked("b/c", ["a", "b"])
