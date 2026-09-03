from pathlib import Path

import pytest
import yaml

from filter_plugins.stortree import (
    DEFAULT_ACCESS_PERMISSIONS,
    PER_USER_PLACEHOLDER,
    access_grant_usernames,
    access_group,
    access_mode,
    access_owner,
    filter_rclone_conf,
    group_gids_from_getent,
    group_members_from_getent,
    mount_unit_names,
    needed_groups,
    needed_users,
    plan_mounts,
    resolve,
    samba_access_tokens,
    user_uids_from_getent,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return yaml.safe_load((FIXTURES / name).read_text())


EXAMPLE_TREE = load("example_tree.yml")
EXAMPLE_HOSTS = ["storage-node-alpha", "storage-node-bravo", "some-storage-gadget"]


def paths(entries):
    return {e["path"] for e in entries}


def by_path(entries, path):
    return next(e for e in entries if e["path"] == path)


def test_access_as_a_list_is_rejected():
    # the old list-of-grants form can't express anything that's actually
    # enforceable anymore (spec.md §6) -- fail loudly at resolve() rather
    # than silently doing something wrong with it.
    tree = {
        "host": "h1",
        "rclone.remote": "r1:/",
        "subdirs": {
            "leaf": {
                "access": [{"group": "a", "permissions": "rx"}, {"group": "b"}],
            }
        },
    }
    with pytest.raises(ValueError, match="access must be a single object"):
        resolve(tree, "h1", ["h1"])


# -- config-schema.md worked example, end to end -----------------------


def test_alpha_owns_everything_not_overridden():
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    assert paths(r["server_subtrees"]) == {
        ".cache",
        ".cache/some-gcs-bucket",
        "backups",
        "home",
        "home/%U/sys-configs",
        "home/%U/media-prod",
    }
    # backups sets neither its own rclone nor a different host -- rclone
    # never inherits, so it resolves with no remote at all (just a plain
    # directory that has to exist under alpha's own local tree)
    backups = by_path(r["server_subtrees"], "backups")
    assert backups["remote"] is None
    assert backups["args"] == {}
    # root host never gets a generic client mount of its own remote
    assert r["client_mounts"] == []


def test_bravo_owns_three_subtrees_with_a_different_remote():
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    assert paths(r["server_subtrees"]) == {
        ".cache/storage-node-bravo",
        "home/%U/whitfield-media",
        "home/%U/mw-fam",
    }
    for entry in r["server_subtrees"]:
        assert entry["remote"].startswith("some-remote:")

    # bravo also has a `clients:` entry -- both lists at once (spec.md §1)
    assert len(r["client_mounts"]) == 1
    mount = r["client_mounts"][0]
    # the root client mount is peer-sourced from alpha (root_host), not a
    # direct mount of the tree's own rclone.remote -- see the matching
    # peer_dependencies entry below
    assert mount["remote"] == "peer-storage-node-alpha-root:/srv/stortree"
    # client-defaults merged with clients.storage-node-bravo overrides
    assert mount["args"]["vfs-cache-mode"] == "full"  # from client-defaults
    assert mount["args"]["vfs-cache-max-size"] == "5G"  # bravo's own override
    assert mount["args"]["cache-dir"] == "/srv/stortree/.cache/storage-node-bravo"


def test_gadget_owns_nothing_but_gets_a_client_mount_and_full_samba_share():
    r = resolve(EXAMPLE_TREE, "some-storage-gadget", EXAMPLE_HOSTS)
    assert r["server_subtrees"] == []
    assert len(r["client_mounts"]) == 1
    assert r["client_mounts"][0]["args"]["vfs-cache-max-size"] == "20G"

    assert len(r["samba_shares"]) == 1
    share = r["samba_shares"][0]
    assert share["node_path"] == "home"
    assert share["subpath"] == "%U"

    # owns none of it -- every descendant is a peer dependency
    owners = {p["owning_host"] for p in r["peer_dependencies"]}
    assert owners == {"storage-node-alpha", "storage-node-bravo"}
    by_owner = {}
    for p in r["peer_dependencies"]:
        by_owner.setdefault(p["owning_host"], set()).add(p["local_path"])
    assert "home/%U/whitfield-media" in by_owner["storage-node-bravo"]
    assert "home/%U/mw-fam" in by_owner["storage-node-bravo"]
    assert "home/%U/sys-configs" in by_owner["storage-node-alpha"]
    assert "home/%U/media-prod" in by_owner["storage-node-alpha"]


def test_alpha_peer_depends_only_on_bravos_pieces():
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    owners = {p["owning_host"] for p in r["peer_dependencies"]}
    assert owners == {"storage-node-bravo"}
    local_paths = {p["local_path"] for p in r["peer_dependencies"]}
    assert local_paths == {"home/%U/whitfield-media", "home/%U/mw-fam"}


def test_alpha_peer_served_by_includes_bravo_and_gadget():
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    servers = {p["serving_host"] for p in r["peer_served_by"]}
    assert servers == {"storage-node-bravo", "some-storage-gadget"}


def test_every_non_root_host_peer_sources_its_client_mount_from_root_host():
    # A client mount is never a direct mount of the tree's own
    # rclone.remote -- it's a peer-sftp mount of root_host's own
    # /srv/stortree, sourced the same way as any samba peer dependency
    # (docs/spec.md §1).
    for hostname in ("storage-node-bravo", "some-storage-gadget"):
        r = resolve(EXAMPLE_TREE, hostname, EXAMPLE_HOSTS)
        root_peers = [
            p
            for p in r["peer_dependencies"]
            if p["owning_host"] == "storage-node-alpha" and p["local_path"] == ""
        ]
        assert len(root_peers) == 1
        assert r["client_mounts"][0]["remote"] == "peer-storage-node-alpha-root:/srv/stortree"

    # root_host itself never peer-sources its own client mount -- it has
    # none (test_alpha_owns_everything_not_overridden already asserts
    # client_mounts == [] for it)
    alpha = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    assert not any(
        p["local_path"] == "" for p in alpha["peer_dependencies"]
    )

    # ...and alpha's peer_served_by reflects serving that root mount to
    # every other host, in addition to whatever samba pieces it serves
    root_served = [p for p in alpha["peer_served_by"] if p["local_path"] == ""]
    assert {p["serving_host"] for p in root_served} == {
        "storage-node-bravo",
        "some-storage-gadget",
    }


def test_root_with_no_remote_gets_no_root_peer_dependency():
    # A root with no rclone.remote of its own has nothing to peer for --
    # the client still resolves (local root directory gets created by
    # stortree_mounts), just no mount and no peer dependency for it.
    tree = {"host": "h1", "subdirs": {"plain": {"host": "h2"}}}
    r = resolve(tree, "h2", ["h1", "h2"])
    assert r["client_mounts"] == [{"remote": None, "path": "", "args": {}}]
    assert not any(p["local_path"] == "" for p in r["peer_dependencies"])


def test_sys_configs_access_defaults_permissions_and_is_per_user():
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    sys_configs = by_path(r["server_subtrees"], "home/%U/sys-configs")
    assert sys_configs["per_user"] is True
    assert sys_configs["access"] == {"owner": "jd", "permissions": DEFAULT_ACCESS_PERMISSIONS}


def test_rclone_remote_does_not_inherit():
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    # neither sets its own rclone nor a different host -- no inheritance
    # from root or from each other means both resolve with no remote
    cache = by_path(r["server_subtrees"], ".cache")
    cache_gcs = by_path(r["server_subtrees"], ".cache/some-gcs-bucket")
    assert cache["remote"] is None
    assert cache_gcs["remote"] is None
    # host still inherits though -- both are still alpha's own subtrees
    assert cache["host"] == cache_gcs["host"] == "storage-node-alpha"


def test_rclone_remote_is_verbatim_when_set_explicitly():
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    whitfield_media = by_path(r["server_subtrees"], "home/%U/whitfield-media")
    mw_fam = by_path(r["server_subtrees"], "home/%U/mw-fam")
    # each sets its own rclone.remote explicitly, path included, and
    # resolve() never appends the node's tree position to it
    assert whitfield_media["remote"] == "some-remote:/media"
    assert mw_fam["remote"] == "some-remote:/fam"


def test_node_with_no_rclone_and_unchanged_host_has_no_remote():
    # case 1 (docs/config-schema.md "Node inheritance"): no rclone of its
    # own, host unchanged from the inherited ancestor -- just a plain
    # directory that has to exist, not a mount
    tree = {
        "host": "h1",
        "rclone.remote": "r1:/",
        "subdirs": {"plain": {}},
    }
    r = resolve(tree, "h1", ["h1"])
    plain = by_path(r["server_subtrees"], "plain")
    assert plain["host"] == "h1"
    assert plain["remote"] is None


def test_node_with_changed_host_and_no_rclone_is_local_only():
    # case 2 (docs/config-schema.md "Node inheritance"): host changes but
    # no rclone of its own -- the new host keeps the directory locally,
    # no remote to mount from
    tree = {
        "host": "h1",
        "rclone.remote": "r1:/",
        "subdirs": {"local-on-h2": {"host": "h2"}},
    }
    r = resolve(tree, "h2", ["h1", "h2"])
    local_only = by_path(r["server_subtrees"], "local-on-h2")
    assert local_only["host"] == "h2"
    assert local_only["remote"] is None


# -- invariants spec.md §1 explicitly calls out -------------------------


def test_mutual_peer_dependency():
    tree = load("mutual_peers.yml")
    hosts = ["host-a", "host-b"]

    a = resolve(tree, "host-a", hosts)
    b = resolve(tree, "host-b", hosts)

    assert {p["owning_host"] for p in a["peer_dependencies"]} == {"host-b"}
    assert {p["owning_host"] for p in b["peer_dependencies"]} == {"host-a"}
    assert {p["serving_host"] for p in a["peer_served_by"]} == {"host-b"}
    assert {p["serving_host"] for p in b["peer_served_by"]} == {"host-a"}


def test_client_only_host_still_resolves_peer_dependencies():
    r = resolve(EXAMPLE_TREE, "some-storage-gadget", EXAMPLE_HOSTS)
    assert r["server_subtrees"] == []
    assert len(r["peer_dependencies"]) > 0


def test_host_unnamed_anywhere_in_config_resolves_like_any_other():
    hosts = EXAMPLE_HOSTS + ["storage-node-charlie"]
    charlie = resolve(EXAMPLE_TREE, "storage-node-charlie", hosts)
    gadget = resolve(EXAMPLE_TREE, "some-storage-gadget", hosts)

    assert charlie["server_subtrees"] == []
    assert len(charlie["client_mounts"]) == 1
    assert {p["owning_host"] for p in charlie["peer_dependencies"]} == {
        p["owning_host"] for p in gadget["peer_dependencies"]
    }
    assert {p["local_path"] for p in charlie["peer_dependencies"]} == {
        p["local_path"] for p in gadget["peer_dependencies"]
    }

    # and alpha/bravo now also serve charlie
    alpha = resolve(EXAMPLE_TREE, "storage-node-alpha", hosts)
    assert "storage-node-charlie" in {
        p["serving_host"] for p in alpha["peer_served_by"]
    }


# -- inheritance rules ---------------------------------------------------


def test_rclone_args_do_not_inherit():
    tree = {
        "host": "h1",
        "rclone": {"remote": "r1:/", "args": {}},
        "subdirs": {
            "parent": {
                "rclone": {"remote": "r1:/parent", "args": {"vfs-cache-mode": "full"}},
                "subdirs": {"child": {"rclone.remote": "r1:/child"}},
            }
        },
    }
    r = resolve(tree, "h1", ["h1"])
    parent = by_path(r["server_subtrees"], "parent")
    child = by_path(r["server_subtrees"], "parent/child")
    assert parent["args"] == {"vfs-cache-mode": "full"}
    assert child["args"] == {}  # not inherited, even though host is
    assert child["host"] == "h1"
    assert child["remote"] == "r1:/child"  # child sets its own; not inherited either


def test_rclone_remote_does_not_inherit_from_parent_node():
    tree = {
        "host": "h1",
        "rclone.remote": "r1:/",
        "subdirs": {
            "parent": {
                "rclone.remote": "r1:/parent",
                "subdirs": {"child": {}},
            }
        },
    }
    r = resolve(tree, "h1", ["h1"])
    child = by_path(r["server_subtrees"], "parent/child")
    # child sets no rclone of its own -- gets none, not parent's r1:/parent
    # nor root's r1:/
    assert child["remote"] is None
    assert child["host"] == "h1"


def test_dotted_and_nested_forms_are_equivalent():
    dotted = {
        "host": "h1",
        "rclone.remote": "r1:/",
        "subdirs": {"a": {"rclone.remote": "r2:/x", "access.owner": "jd"}},
    }
    nested = {
        "host": "h1",
        "rclone": {"remote": "r1:/"},
        "subdirs": {
            "a": {"rclone": {"remote": "r2:/x"}, "access": {"owner": "jd"}}
        },
    }
    assert resolve(dotted, "h1", ["h1"]) == resolve(nested, "h1", ["h1"])


def test_dotted_cache_subdirs_key_expands_to_literal_dotted_name():
    tree = {
        "host": "h1",
        "rclone.remote": "r1:/",
        "subdirs": {
            ".cache.subdirs": {
                "thing": {"host": "h1"},
            }
        },
    }
    r = resolve(tree, "h1", ["h1"])
    assert paths(r["server_subtrees"]) == {".cache", ".cache/thing"}


# -- filter_rclone_conf ---------------------------------------------------

MASTER_INI = """
[remote-a]
type = sftp
host = a.example
user = u
pass = p

[remote-b]
type = sftp
host = b.example
user = u
pass = p

[unused-remote]
type = sftp
host = unused.example
user = u
pass = p
"""


def test_filter_rclone_conf_scopes_to_needed_sections_plus_peers():
    resolved = {
        "server_subtrees": [{"remote": "remote-a:/x"}],
        "client_mounts": [],
        "samba_shares": [],
        "peer_dependencies": [
            {
                "owning_host": "host-b",
                "local_path": "shared/piece-b",
                "remote_path": "shared/piece-b",
                "samba_node": "shared",
                "per_user": False,
            }
        ],
    }
    hostvars = {"host-b": {"ansible_host": "10.0.0.2"}}

    out = filter_rclone_conf(MASTER_INI, resolved, hostvars)

    assert "[remote-a]" in out
    assert "[unused-remote]" not in out
    assert "[remote-b]" not in out  # not directly referenced, only via peer

    assert "[peer-host-b-shared-piece-b]" in out
    assert "host = 10.0.0.2" in out
    assert "path = /srv/stortree/shared/piece-b" in out


def test_filter_rclone_conf_expands_per_user_peer_sections():
    # a per-user peer dependency's %U has to become one real INI section
    # per actual user -- plan_mounts() independently expands the same
    # entry into one mount per user, and both have to compute the exact
    # same section name for a mount's `remote` to actually resolve
    resolved = {
        "server_subtrees": [],
        "client_mounts": [],
        "samba_shares": [],
        "peer_dependencies": [
            {
                "owning_host": "host-b",
                "local_path": "home/%U/sys-configs",
                "remote_path": "home/%U/sys-configs",
                "samba_node": "home",
                "per_user": True,
                "access": {"owner": "jd", "permissions": "rwx"},
            }
        ],
    }
    hostvars = {"host-b": {"ansible_host": "10.0.0.2"}}

    out = filter_rclone_conf(MASTER_INI, resolved, hostvars, group_members={})

    assert "[peer-host-b-home-jd-sys-configs]" in out
    assert "path = /srv/stortree/home/jd/sys-configs" in out
    # no section synthesized for the un-expanded %U template itself
    assert "peer-host-b-home-pctU-sys-configs" not in out


# -- group_members_from_getent / access_grant_usernames / needed_groups ---


def test_needed_groups_covers_server_subtrees_and_peer_dependencies():
    # gadget owns nothing (no server_subtrees at all) -- every group it
    # needs getent'd for comes from peer_dependencies alone, since that's
    # the only place its per-user access grants show up
    r = resolve(EXAMPLE_TREE, "some-storage-gadget", EXAMPLE_HOSTS)
    assert needed_groups(r) == [
        "Media Production",
        "Michael Whitfield Family",
        "Whitfield Family & Friends",
    ]

    # bravo owns some per-user pieces itself (server_subtrees: mw-fam,
    # whitfield-media) and peer depends on the rest (alpha's sys-configs,
    # a user-only grant with no group; and media-prod, group-granted) --
    # same combined group set as gadget's, just split across both sources
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    assert needed_groups(r) == [
        "Media Production",
        "Michael Whitfield Family",
        "Whitfield Family & Friends",
    ]


def test_needed_groups_covers_non_per_user_grants_too():
    # a non-per-user node's own `access.group` still needs its GID
    # resolved (spec.md §6, gid-owning its mount) even though it has no
    # %U-expansion to do -- needed_groups() covers both for exactly that
    # reason, unlike group_members (%U-expansion) which only matters for
    # a per-user node. An owner-only grant contributes no group either way.
    tree = {
        "host": "h1",
        "rclone.remote": "r1:/",
        "subdirs": {
            "shared": {
                "samba": {"subpath": ""},
                "access.group": "not-per-user-group",
                "subdirs": {"leaf": {"host": "h2", "access.owner": "jd"}},
            }
        },
    }
    r = resolve(tree, "h1", ["h1", "h2"])
    assert needed_groups(r) == ["not-per-user-group"]


def test_group_members_from_getent_parses_csv_member_field():
    getent_group = {
        "Michael Whitfield Family": ["x", "2001", "mike,dana"],
        "empty-group": ["x", "2002", ""],
    }
    members = group_members_from_getent(getent_group)
    assert members == {"Michael Whitfield Family": ["mike", "dana"], "empty-group": []}


def test_group_gids_from_getent_parses_numeric_gid():
    # stortree_mounts needs the actual GID (not just membership) to
    # gid-own a remote-backed node's mount for a single-group `access`
    # grant (spec.md §6) -- same ansible_facts.getent_group data
    # group_members_from_getent() reads, just the other field.
    getent_group = {
        "Michael Whitfield Family": ["x", "2001", "mike,dana"],
    }
    assert group_gids_from_getent(getent_group) == {"Michael Whitfield Family": 2001}


def test_user_uids_from_getent_parses_numeric_uid():
    # mirrors group_gids_from_getent() above, for access.owner instead of
    # access.group -- stortree_mounts needs this to uid-own a remote-backed
    # node's mount (spec.md §6).
    getent_passwd = {"jd": ["x", "2101", "2101", "", "/home/jd", "/bin/bash"]}
    assert user_uids_from_getent(getent_passwd) == {"jd": 2101}


def test_needed_users_covers_server_subtrees_and_peer_dependencies():
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    assert needed_users(r) == ["jd"]


def test_access_owner_defaults_to_stortree_when_unset():
    assert access_owner({}, "stortree") == "stortree"
    assert access_owner(None, "stortree") == "stortree"
    assert access_owner({"group": "g"}, "stortree") == "stortree"


def test_access_owner_uses_the_granted_owner():
    assert access_owner({"owner": "jd"}, "stortree") == "jd"


def test_access_group_defaults_to_stortree_when_unset():
    assert access_group({}, "stortree") == "stortree"
    assert access_group({"owner": "jd"}, "stortree") == "stortree"


def test_access_group_uses_the_granted_group():
    assert access_group({"group": "Media Production"}, "stortree") == "Media Production"


def test_access_mode_with_no_grant_is_the_plain_default():
    # owner: stortree (full control), group: stortree (read+traverse),
    # other: none -- what every path had before `access` existed.
    assert access_mode({}) == "0750"
    assert access_mode(None) == "0750"


def test_access_mode_group_only_leaves_owner_full_and_sets_group_bits():
    assert access_mode({"group": "g", "permissions": "rx"}) == "0750"
    assert access_mode({"group": "g", "permissions": "rwx"}) == "0770"


def test_access_mode_owner_only_is_private_to_that_owner():
    # deliberately no stortree-group carve-out -- an owner-only grant was
    # scoped to one specific person, not shared with anyone else by default
    assert access_mode({"owner": "jd", "permissions": "rwx"}) == "0700"
    assert access_mode({"owner": "jd", "permissions": "rx"}) == "0500"


def test_access_mode_owner_and_group_share_the_same_permissions_level():
    assert access_mode({"owner": "jd", "group": "g", "permissions": "rwx"}) == "0770"


def test_samba_access_tokens_quotes_names_with_spaces():
    access = [{"group": "Michael Whitfield Family", "permissions": "rwx"}]
    assert samba_access_tokens(access) == ['"@Michael Whitfield Family"']


def test_samba_access_tokens_one_grant_with_both_owner_and_group_yields_two_tokens():
    access = [{"owner": "jd", "group": "IT Admins", "permissions": "rwx"}]
    assert samba_access_tokens(access) == ['"@IT Admins"', '"jd"']


def test_samba_access_tokens_include_self_prepends_percent_u():
    assert samba_access_tokens([], include_self=True) == ['"%U"']
    access = [{"group": "g", "permissions": "rwx"}]
    assert samba_access_tokens(access, include_self=True) == ['"%U"', '"@g"']


def test_access_grant_usernames_owner_only_pins_a_single_user():
    access = {"owner": "jd", "permissions": "rwx"}
    assert access_grant_usernames(access, {}) == ["jd"]


def test_access_grant_usernames_group_only_expands_to_every_member():
    access = {"group": "Michael Whitfield Family", "permissions": "rwx"}
    group_members = {"Michael Whitfield Family": ["mike", "dana", "jd"]}
    assert access_grant_usernames(access, group_members) == ["dana", "jd", "mike"]


def test_access_grant_usernames_owner_and_group_still_pins_a_single_user():
    # owner determines the one folder that gets created -- group alongside
    # it is real (shared mount/mode-level access to that same folder,
    # access_mode()/access_group()), just not a second axis of expansion.
    access = {"owner": "jd", "group": "Michael Whitfield Family", "permissions": "rwx"}
    group_members = {"Michael Whitfield Family": ["mike", "dana"]}
    assert access_grant_usernames(access, group_members) == ["jd"]


def test_access_grant_usernames_empty_access_expands_to_nobody():
    assert access_grant_usernames({}, {"g": ["mike"]}) == []
    assert access_grant_usernames(None, {"g": ["mike"]}) == []


# -- plan_mounts -----------------------------------------------------------


def test_plan_mounts_expands_per_user_nodes_and_orders_nesting():
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    group_members = {
        "Michael Whitfield Family": ["mike"],
        "Whitfield Family & Friends": ["mike", "dana"],
    }

    plan = plan_mounts(r, group_members)
    by_local_path = {e["local_path"]: e for e in plan}

    # per-user node mw-fam (access.group: Michael Whitfield Family)
    # expands to one mount per resolved member
    assert "home/mike/mw-fam" in by_local_path
    assert "home/%U/mw-fam" not in by_local_path

    # whitfield-media (access.group: Whitfield Family & Friends) is its
    # own, separate node -- mike gets a mount there too, via his own
    # membership in that group, not via mw-fam's
    assert "home/mike/whitfield-media" in by_local_path
    assert "home/dana/whitfield-media" in by_local_path
    # but nobody unresolvable (no group_members entry) gets one
    assert not any(
        p.startswith("home/") and p.endswith("/whitfield-media")
        and p not in ("home/mike/whitfield-media", "home/dana/whitfield-media")
        for p in by_local_path
    )

    # bravo's client mount (root, local_path "") nests everything under it
    root_slug = by_local_path[""]["slug"]
    assert by_local_path[".cache/storage-node-bravo"]["requires_slug"] == root_slug
    assert by_local_path["home/mike/mw-fam"]["requires_slug"] == root_slug


def test_plan_mounts_entries_carry_access_for_ownership_and_mode():
    # stortree_mounts now sets owner/group/mode from `access` on every
    # resolved entry directly (ansible.builtin.file for a plain
    # directory, --uid/--gid/--dir-perms/--file-perms for a remote-backed
    # one, spec.md §6) -- no separate ACL role, and no filtering by
    # `remote` first; both a plain local leaf (sys-configs) and a
    # remote-backed one (media-prod) carry their own `access` straight
    # through into the plan.
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    group_members = {"Media Production": ["alex"]}
    plan = plan_mounts(r, group_members)
    by_local_path = {e["local_path"]: e for e in plan}

    assert by_local_path["home/jd/sys-configs"]["access"] == {
        "owner": "jd",
        "permissions": DEFAULT_ACCESS_PERMISSIONS,
    }
    assert by_local_path["home/alex/media-prod"]["access"] == {
        "group": "Media Production",
        "permissions": DEFAULT_ACCESS_PERMISSIONS,
    }
    assert by_local_path["backups"]["access"] == {}


def test_plan_mounts_peer_sources_samba_descendants_it_does_not_own():
    # gadget owns nothing (docs/config-schema.md worked example) -- every
    # piece of `home` it must still serve via Samba (spec.md "Samba
    # sharing is universal") comes from a real mount now, sourced
    # directly (mesh) from whichever host actually owns that piece, not
    # funneled through root_host.
    r = resolve(EXAMPLE_TREE, "some-storage-gadget", EXAMPLE_HOSTS)
    group_members = {
        "Michael Whitfield Family": ["mike"],
        "Whitfield Family & Friends": ["mike"],
    }
    plan = plan_mounts(r, group_members)
    by_local_path = {e["local_path"]: e for e in plan}

    # alpha-owned, per-user, no rclone.remote of its own -- still a real
    # peer mount (sourced live from alpha's own filesystem at that exact
    # path), since it's a leaf with real per-user content, not a
    # structural container
    assert "home/jd/sys-configs" in by_local_path
    sys_configs = by_local_path["home/jd/sys-configs"]
    assert sys_configs["remote"] == "peer-storage-node-alpha-home-jd-sys-configs:/srv/stortree/home/jd/sys-configs"

    # bravo-owned, per-user, sourced directly from bravo -- not relayed
    # through alpha (root_host), preserving the mesh
    assert "home/mike/mw-fam" not in {
        p for p, e in by_local_path.items() if e["remote"] and "alpha" in e["remote"]
    }
    whitfield = by_local_path["home/mike/whitfield-media"]
    assert whitfield["remote"] == "peer-storage-node-bravo-home-mike-whitfield-media:/srv/stortree/home/mike/whitfield-media"

    # the samba node itself ("home") is a pure container -- delegates
    # entirely to its own children above, gets no mount/peer of its own
    assert "home" not in by_local_path

    # every peer-sourced entry nests directly under gadget's own root
    # client mount (also peer-sourced, from alpha) -- not under "home",
    # which was never a mount to nest under in the first place
    root_slug = by_local_path[""]["slug"]
    assert sys_configs["requires_slug"] == root_slug
    assert whitfield["requires_slug"] == root_slug


def test_plan_mounts_nested_nonuser_paths_require_their_parent():
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    plan = plan_mounts(r, {})
    by_local_path = {e["local_path"]: e for e in plan}

    root_slug = by_local_path[""]["slug"]
    assert by_local_path[".cache/storage-node-bravo"]["requires_slug"] == root_slug


def test_plan_mounts_skips_remote_less_ancestors_for_requires_slug():
    # a node with no rclone of its own is a plain directory, not a mount
    # (no systemd unit of its own) -- a real mount nested underneath it
    # has to require the nearest *actual* mounted ancestor instead,
    # skipping over the remote-less one in between
    tree = {
        "host": "h1",
        "rclone.remote": "r1:/",
        "subdirs": {
            "top": {
                "rclone.remote": "r1:/top",
                "subdirs": {
                    "plain": {
                        "subdirs": {
                            "nested": {"rclone.remote": "r1:/nested"},
                        }
                    }
                },
            }
        },
    }
    r = resolve(tree, "h1", ["h1"])
    plan = plan_mounts(r, {})
    by_local_path = {e["local_path"]: e for e in plan}

    top = by_local_path["top"]
    plain = by_local_path["top/plain"]
    nested = by_local_path["top/plain/nested"]

    assert plain["remote"] is None
    assert plain["requires_slug"] == top["slug"]
    assert nested["requires_slug"] == top["slug"]


def test_mount_unit_names():
    plan = [
        {"slug": "backups", "remote": "r1:/"},
        {"slug": "root", "remote": "r1:/"},
    ]
    assert mount_unit_names(plan) == [
        "stortree-mount@backups.service",
        "stortree-mount@root.service",
    ]


def test_mount_unit_names_excludes_remote_less_entries():
    # a plain directory (no rclone.remote) gets no systemd unit at all
    plan = [
        {"slug": "backups", "remote": "r1:/"},
        {"slug": "plain-dir", "remote": None},
    ]
    assert mount_unit_names(plan) == ["stortree-mount@backups.service"]


def test_plan_mounts_slug_distinguishes_hyphen_from_nesting():
    # a segment literally named "media-prod" and a nested "media/prod"
    # both naively collapse to "media-prod" under a plain "/" -> "-"
    # substitution -- they must not share a systemd unit slug
    tree = {
        "host": "h1",
        "rclone.remote": "r1:/",
        "subdirs": {
            "media-prod": {"rclone.remote": "r1:/a"},
            "media": {"subdirs": {"prod": {"rclone.remote": "r1:/b"}}},
        },
    }
    r = resolve(tree, "h1", ["h1"])
    plan = plan_mounts(r, {})
    by_local_path = {e["local_path"]: e for e in plan}

    assert by_local_path["media-prod"]["slug"] != by_local_path["media/prod"]["slug"]


def test_plan_mounts_raises_on_slug_collision():
    # a top-level segment literally named "root" collides with the
    # reserved slug for a client's own root mount (local_path "") --
    # plan_mounts must fail loudly rather than let the two systemd units
    # silently clobber each other
    tree = {
        "host": "h1",
        "rclone.remote": "r1:/",
        "subdirs": {"root": {"host": "h2", "rclone.remote": "r2:/"}},
    }
    r = resolve(tree, "h2", ["h1", "h2"])
    with pytest.raises(ValueError, match="root"):
        plan_mounts(r, {})
