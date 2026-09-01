from pathlib import Path

import yaml

from filter_plugins.stortree import (
    DEFAULT_ACCESS_PERMISSIONS,
    PER_USER_PLACEHOLDER,
    access_grant_usernames,
    filter_rclone_conf,
    group_members_from_getent,
    mount_unit_names,
    plan_mounts,
    resolve,
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
    # backups takes root's host/remote as-is, verbatim, no override
    backups = by_path(r["server_subtrees"], "backups")
    assert backups["remote"] == "storagebox:/"
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
    assert mount["remote"] == "storagebox:/"
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


def test_sys_configs_access_defaults_permissions_and_is_per_user():
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    sys_configs = by_path(r["server_subtrees"], "home/%U/sys-configs")
    assert sys_configs["per_user"] is True
    assert sys_configs["access"] == [
        {"user": "jd", "permissions": DEFAULT_ACCESS_PERMISSIONS}
    ]


def test_rclone_remote_is_verbatim_not_position_dependent():
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    cache_gcs = by_path(r["server_subtrees"], ".cache/some-gcs-bucket")
    backups = by_path(r["server_subtrees"], "backups")
    # both inherit the exact same root remote, unmodified by tree position
    assert cache_gcs["remote"] == backups["remote"] == "storagebox:/"


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
                "rclone": {"args": {"vfs-cache-mode": "full"}},
                "subdirs": {"child": {}},
            }
        },
    }
    r = resolve(tree, "h1", ["h1"])
    parent = by_path(r["server_subtrees"], "parent")
    child = by_path(r["server_subtrees"], "parent/child")
    assert parent["args"] == {"vfs-cache-mode": "full"}
    assert child["args"] == {}  # not inherited, even though host/remote are
    assert child["host"] == "h1"
    assert child["remote"] == "r1:/"


def test_dotted_and_nested_forms_are_equivalent():
    dotted = {
        "host": "h1",
        "rclone.remote": "r1:/",
        "subdirs": {"a": {"rclone.remote": "r2:/x", "access.user": "jd"}},
    }
    nested = {
        "host": "h1",
        "rclone": {"remote": "r1:/"},
        "subdirs": {
            "a": {"rclone": {"remote": "r2:/x"}, "access": {"user": "jd"}}
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


# -- group_members_from_getent / access_grant_usernames -----------------


def test_group_members_from_getent_parses_csv_member_field():
    getent_group = {
        "Michael Whitfield Family": ["x", "2001", "mike,dana"],
        "empty-group": ["x", "2002", ""],
    }
    members = group_members_from_getent(getent_group)
    assert members == {"Michael Whitfield Family": ["mike", "dana"], "empty-group": []}


def test_access_grant_usernames_combines_users_and_group_members():
    access = [
        {"user": "jd", "permissions": "rwx"},
        {"group": "Michael Whitfield Family", "permissions": "rwx"},
    ]
    group_members = {"Michael Whitfield Family": ["mike", "dana", "jd"]}
    assert access_grant_usernames(access, group_members) == ["dana", "jd", "mike"]


# -- plan_mounts -----------------------------------------------------------


def test_plan_mounts_expands_per_user_nodes_and_orders_nesting():
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    group_members = {"Michael Whitfield Family": ["mike"]}

    plan = plan_mounts(r, group_members)
    by_local_path = {e["local_path"]: e for e in plan}

    # per-user node mw-fam (access.group: Michael Whitfield Family)
    # expands to one mount per resolved member
    assert "home/mike/mw-fam" in by_local_path
    assert "home/%U/mw-fam" not in by_local_path

    # whitfield-media also grants "Michael Whitfield Family" (among
    # others), so mike gets a mount there too via that grant
    assert "home/mike/whitfield-media" in by_local_path
    # but nobody unresolvable (no group_members entry) gets one
    assert not any(
        p.startswith("home/") and p.endswith("/whitfield-media") and p != "home/mike/whitfield-media"
        for p in by_local_path
    )

    # bravo's client mount (root, local_path "") nests everything under it
    root_slug = by_local_path[""]["slug"]
    assert by_local_path[".cache/storage-node-bravo"]["requires_slug"] == root_slug
    assert by_local_path["home/mike/mw-fam"]["requires_slug"] == root_slug


def test_plan_mounts_nested_nonuser_paths_require_their_parent():
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    plan = plan_mounts(r, {})
    by_local_path = {e["local_path"]: e for e in plan}

    cache_slug = by_local_path[".cache"]["slug"]
    assert by_local_path[".cache/some-gcs-bucket"]["requires_slug"] == cache_slug


def test_mount_unit_names():
    plan = [{"slug": "backups"}, {"slug": "root"}]
    assert mount_unit_names(plan) == [
        "stortree-mount@backups.service",
        "stortree-mount@root.service",
    ]
