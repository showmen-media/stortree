from pathlib import Path

import pytest
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
