from pathlib import Path

import pytest
import yaml

from filter_plugins.stortree import (
    DEFAULT_ACCESS_PERMISSIONS,
    PER_USER_PLACEHOLDER,
    _normalize_access,
    _slug,
    access_grant_usernames,
    access_group,
    access_mode,
    access_owner,
    filter_rclone_conf,
    group_gids_from_getent,
    group_members_from_getent,
    merged_getent_results,
    mount_unit_names,
    needed_groups,
    needed_users,
    per_user_mount_path,
    plan_mounts,
    resolve,
    samba_access_tokens,
    user_container_paths,
    user_mount_unit_names,
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
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {
                "leaf": {
                    "access": [{"group": "a", "permissions": "rx"}, {"group": "b"}],
                }
            },
        }
    }
    with pytest.raises(ValueError, match="access must be a single object"):
        resolve(tree, "h1", ["h1"])


# -- config-schema.md worked example, end to end -----------------------


def test_alpha_owns_everything_not_overridden():
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    assert paths(r["server_subtrees"]) == {
        "tree",
        "tree/backups",
        "tree/home",
        "tree/home/%U/sys-configs",
        "tree/home/%U/media-prod",
        ".gcs-cache",
    }
    # alpha owns both top-level subtrees named above -- unlike the old
    # single-implicit-root design, a top-level entry is an ordinary
    # mountable node now, so its owner self-mounts it same as any other
    # node with its own host+remote. .gcs-cache itself sets no rclone at
    # all -- genuinely local, media-prod's own VFS cache lives directly
    # on its resolved host's disk, no separate mount needed to back it.
    tree = by_path(r["server_subtrees"], "tree")
    assert tree["remote"] == "storagebox:/"
    gcs_cache = by_path(r["server_subtrees"], ".gcs-cache")
    assert gcs_cache["remote"] is None
    # backups sets neither its own rclone nor a different host -- rclone
    # never inherits, so it resolves with no remote at all (just a plain
    # directory that has to exist under alpha's own local tree)
    backups = by_path(r["server_subtrees"], "tree/backups")
    assert backups["remote"] is None
    assert backups["args"] == {}
    # alpha doesn't own .bravo-cache, but that subtree's own
    # client-defaults.rclone: false keeps it off every host that isn't
    # explicitly listed in its `clients:` -- alpha isn't, so no client
    # mount at all
    assert r["client_mounts"] == []


def test_bravo_owns_three_subtrees_with_a_different_remote():
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    assert paths(r["server_subtrees"]) == {
        ".bravo-cache",
        "tree/home/%U/whitfield-media",
        "tree/home/%U/mw-fam",
    }
    for path in ("tree/home/%U/whitfield-media", "tree/home/%U/mw-fam"):
        assert by_path(r["server_subtrees"], path)["remote"].startswith("some-remote:")
    assert by_path(r["server_subtrees"], ".bravo-cache")["remote"] == (
        "some-remote:/.stortree-cache"
    )

    # bravo also has a `clients:` entry on `tree` -- both lists at once
    # (spec.md §1). .gcs-cache is disabled by default (client-defaults.
    # rclone: false) and bravo isn't in its `clients:`, so it gets none.
    assert len(r["client_mounts"]) == 1
    mount = r["client_mounts"][0]
    assert mount["local_path"] == "tree"
    # the client mount is peer-sourced from alpha (tree's own owner),
    # not a direct mount of tree's own rclone.remote -- see the matching
    # peer_dependencies entry below
    assert mount["remote"] == "peer-storage-node-alpha-tree:/srv/stortree/tree"
    # client-defaults merged with clients.storage-node-bravo overrides
    assert mount["args"]["vfs-cache-mode"] == "full"  # from client-defaults
    assert mount["args"]["vfs-cache-max-size"] == "5G"  # bravo's own override
    assert mount["args"]["cache-dir"] == "/srv/stortree/.bravo-cache"


def test_gadget_owns_nothing_but_gets_a_client_mount_and_full_samba_share():
    r = resolve(EXAMPLE_TREE, "some-storage-gadget", EXAMPLE_HOSTS)
    assert r["server_subtrees"] == []
    assert len(r["client_mounts"]) == 1
    assert r["client_mounts"][0]["local_path"] == "tree"
    assert r["client_mounts"][0]["args"]["vfs-cache-max-size"] == "20G"

    assert len(r["samba_shares"]) == 1
    share = r["samba_shares"][0]
    assert share["node_path"] == "tree/home"
    assert share["subpath"] == "%U"

    # owns none of it -- every descendant is a peer dependency
    owners = {p["owning_host"] for p in r["peer_dependencies"]}
    assert owners == {"storage-node-alpha", "storage-node-bravo"}
    by_owner = {}
    for p in r["peer_dependencies"]:
        by_owner.setdefault(p["owning_host"], set()).add(p["local_path"])
    assert "tree/home/%U/whitfield-media" in by_owner["storage-node-bravo"]
    assert "tree/home/%U/mw-fam" in by_owner["storage-node-bravo"]
    assert "tree/home/%U/sys-configs" in by_owner["storage-node-alpha"]
    assert "tree/home/%U/media-prod" in by_owner["storage-node-alpha"]


def test_alpha_peer_depends_only_on_bravos_pieces():
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    owners = {p["owning_host"] for p in r["peer_dependencies"]}
    assert owners == {"storage-node-bravo"}
    local_paths = {p["local_path"] for p in r["peer_dependencies"]}
    assert local_paths == {"tree/home/%U/whitfield-media", "tree/home/%U/mw-fam"}


def test_alpha_peer_served_by_includes_bravo_and_gadget():
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    servers = {p["serving_host"] for p in r["peer_served_by"]}
    assert servers == {"storage-node-bravo", "some-storage-gadget"}
    # .gcs-cache is alpha's own too, but client-defaults.rclone: false
    # keeps it out of peer_served_by for hosts that aren't in its
    # `clients:` (neither bravo nor gadget is)
    assert not any(p["local_path"] == ".gcs-cache" for p in r["peer_served_by"])


def test_every_non_owning_host_peer_sources_tree_from_its_owner():
    # A client mount of a top-level subtree is never a direct mount of
    # that subtree's own rclone.remote -- it's a peer-sftp mount of the
    # owning host's own copy, sourced the same way as any samba peer
    # dependency (docs/spec.md §1).
    for hostname in ("storage-node-bravo", "some-storage-gadget"):
        r = resolve(EXAMPLE_TREE, hostname, EXAMPLE_HOSTS)
        tree_peers = [
            p
            for p in r["peer_dependencies"]
            if p["owning_host"] == "storage-node-alpha" and p["local_path"] == "tree"
        ]
        assert len(tree_peers) == 1
        assert (
            r["client_mounts"][0]["remote"]
            == "peer-storage-node-alpha-tree:/srv/stortree/tree"
        )

    # alpha itself never peer-sources its own client mount of tree -- it
    # owns tree outright (test_alpha_owns_everything_not_overridden
    # already asserts client_mounts == [] for it)
    alpha = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    assert not any(p["local_path"] == "tree" for p in alpha["peer_dependencies"])

    # ...and alpha's peer_served_by reflects serving tree to every other
    # host, in addition to whatever samba pieces it serves
    tree_served = [p for p in alpha["peer_served_by"] if p["local_path"] == "tree"]
    assert {p["serving_host"] for p in tree_served} == {
        "storage-node-bravo",
        "some-storage-gadget",
    }


def test_subtree_with_no_remote_gets_no_peer_dependency():
    # A top-level subtree with no rclone.remote of its own has nothing to
    # peer for -- the client still resolves (local directory gets created
    # by stortree_mounts), just no mount and no peer dependency for it.
    tree = {"top": {"host": "h1", "subdirs": {"plain": {"host": "h2"}}}}
    r = resolve(tree, "h2", ["h1", "h2"])
    assert r["client_mounts"] == [{"local_path": "top", "remote": None, "args": {}}]
    assert not any(p["local_path"] == "top" for p in r["peer_dependencies"])


# -- client-defaults.rclone / clients.<host>.rclone opt-out --------------


def test_client_defaults_rclone_false_keeps_a_subtree_local_by_default():
    tree = {
        "private": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "client-defaults": {"rclone": False},
        }
    }
    for hostname in ("h2", "h3"):
        r = resolve(tree, hostname, ["h1", "h2", "h3"])
        assert r["client_mounts"] == []
        assert not any(p["local_path"] == "private" for p in r["peer_dependencies"])
    # the owner is unaffected either way -- it self-mounts via
    # server_subtrees, never through the client-mount/gating path at all
    owner = resolve(tree, "h1", ["h1", "h2", "h3"])
    assert paths(owner["server_subtrees"]) == {"private"}


def test_clients_override_acts_as_an_allow_list_when_defaults_are_false():
    tree = {
        "private": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "client-defaults": {"rclone": False},
            "clients": {"h2": {"rclone": {"args": {"vfs-cache-max-size": "1G"}}}},
        }
    }
    allowed = resolve(tree, "h2", ["h1", "h2", "h3"])
    assert allowed["client_mounts"] == [
        {
            "local_path": "private",
            "remote": "peer-h1-private:/srv/stortree/private",
            "args": {"vfs-cache-max-size": "1G"},
        }
    ]

    denied = resolve(tree, "h3", ["h1", "h2", "h3"])
    assert denied["client_mounts"] == []
    assert not any(p["local_path"] == "private" for p in denied["peer_dependencies"])


def test_clients_override_acts_as_a_deny_list_when_defaults_are_enabled():
    tree = {
        "shared": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "clients": {"h2": {"rclone": False}},
        }
    }
    denied = resolve(tree, "h2", ["h1", "h2", "h3"])
    assert denied["client_mounts"] == []

    allowed = resolve(tree, "h3", ["h1", "h2", "h3"])
    assert len(allowed["client_mounts"]) == 1
    assert allowed["client_mounts"][0]["local_path"] == "shared"


def test_bravo_cache_and_gcs_cache_reach_no_other_host():
    # the two independent per-host VFS-cache subtrees in the worked
    # example are exactly what client-defaults.rclone: false exists for
    # -- confirm neither ever shows up for a host that doesn't own it
    for hostname in EXAMPLE_HOSTS:
        r = resolve(EXAMPLE_TREE, hostname, EXAMPLE_HOSTS)
        mounted_paths = {m["local_path"] for m in r["client_mounts"]}
        owned_paths = paths(r["server_subtrees"])
        for cache_path, owner in ((".bravo-cache", "storage-node-bravo"), (".gcs-cache", "storage-node-alpha")):
            if hostname == owner:
                assert cache_path in owned_paths
            else:
                assert cache_path not in mounted_paths
                assert not any(
                    p["local_path"] == cache_path for p in r["peer_dependencies"]
                )


def test_sys_configs_access_defaults_permissions_and_is_per_user():
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    sys_configs = by_path(r["server_subtrees"], "tree/home/%U/sys-configs")
    assert sys_configs["per_user"] is True
    assert sys_configs["access"] == {
        "owner": "jd",
        "permissions": DEFAULT_ACCESS_PERMISSIONS,
        "permissions_explicit": False,
    }


def test_rclone_remote_does_not_inherit():
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {"container": {"subdirs": {"child": {}}}},
        }
    }
    r = resolve(tree, "h1", ["h1"])
    # neither sets its own rclone nor a different host -- no inheritance
    # from the top-level subtree or from each other means both resolve
    # with no remote
    container = by_path(r["server_subtrees"], "top/container")
    child = by_path(r["server_subtrees"], "top/container/child")
    assert container["remote"] is None
    assert child["remote"] is None
    # host still inherits though -- both are still h1's own subtrees
    assert container["host"] == child["host"] == "h1"


def test_rclone_remote_is_verbatim_when_set_explicitly():
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    whitfield_media = by_path(r["server_subtrees"], "tree/home/%U/whitfield-media")
    mw_fam = by_path(r["server_subtrees"], "tree/home/%U/mw-fam")
    # each sets its own rclone.remote explicitly, path included, and
    # resolve() never appends the node's tree position to it
    assert whitfield_media["remote"] == "some-remote:/media"
    assert mw_fam["remote"] == "some-remote:/fam"


def test_node_with_no_rclone_and_unchanged_host_has_no_remote():
    # case 1 (docs/config-schema.md "Node inheritance"): no rclone of its
    # own, host unchanged from the inherited ancestor -- just a plain
    # directory that has to exist, not a mount
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {"plain": {}},
        }
    }
    r = resolve(tree, "h1", ["h1"])
    plain = by_path(r["server_subtrees"], "top/plain")
    assert plain["host"] == "h1"
    assert plain["remote"] is None


def test_node_with_changed_host_and_no_rclone_is_local_only():
    # case 2 (docs/config-schema.md "Node inheritance"): host changes but
    # no rclone of its own -- the new host keeps the directory locally,
    # no remote to mount from
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {"local-on-h2": {"host": "h2"}},
        }
    }
    r = resolve(tree, "h2", ["h1", "h2"])
    local_only = by_path(r["server_subtrees"], "top/local-on-h2")
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

    # and alpha now also serves charlie
    alpha = resolve(EXAMPLE_TREE, "storage-node-alpha", hosts)
    assert "storage-node-charlie" in {
        p["serving_host"] for p in alpha["peer_served_by"]
    }


# -- inheritance rules ---------------------------------------------------


def test_rclone_args_do_not_inherit():
    tree = {
        "top": {
            "host": "h1",
            "rclone": {"remote": "r1:/", "args": {}},
            "subdirs": {
                "parent": {
                    "rclone": {"remote": "r1:/parent", "args": {"vfs-cache-mode": "full"}},
                    "subdirs": {"child": {"rclone.remote": "r1:/child"}},
                }
            },
        }
    }
    r = resolve(tree, "h1", ["h1"])
    parent = by_path(r["server_subtrees"], "top/parent")
    child = by_path(r["server_subtrees"], "top/parent/child")
    assert parent["args"] == {"vfs-cache-mode": "full"}
    assert child["args"] == {}  # not inherited, even though host is
    assert child["host"] == "h1"
    assert child["remote"] == "r1:/child"  # child sets its own; not inherited either


def test_rclone_remote_does_not_inherit_from_parent_node():
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {
                "parent": {
                    "rclone.remote": "r1:/parent",
                    "subdirs": {"child": {}},
                }
            },
        }
    }
    r = resolve(tree, "h1", ["h1"])
    child = by_path(r["server_subtrees"], "top/parent/child")
    # child sets no rclone of its own -- gets none, not parent's r1:/parent
    # nor top's r1:/
    assert child["remote"] is None
    assert child["host"] == "h1"


def test_dotted_and_nested_forms_are_equivalent():
    dotted = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {"a": {"rclone.remote": "r2:/x", "access.owner": "jd"}},
        }
    }
    nested = {
        "top": {
            "host": "h1",
            "rclone": {"remote": "r1:/"},
            "subdirs": {
                "a": {"rclone": {"remote": "r2:/x"}, "access": {"owner": "jd"}}
            },
        }
    }
    assert resolve(dotted, "h1", ["h1"]) == resolve(nested, "h1", ["h1"])


def test_dotted_cache_subdirs_key_expands_to_literal_dotted_name():
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {
                ".cache.subdirs": {
                    "thing": {"host": "h1"},
                }
            },
        }
    }
    r = resolve(tree, "h1", ["h1"])
    assert paths(r["server_subtrees"]) == {"top", "top/.cache", "top/.cache/thing"}


def test_bare_leading_dot_top_level_key_is_not_shredded():
    # a bare dot-prefixed key (this codebase's hidden-subtree convention,
    # e.g. `.bravo-cache`) has to survive _expand_dotted() untouched when
    # it's used at the top level with no `.subdirs`/etc suffix after it
    # -- unlike `.cache.subdirs` above, there's no second dot to split on
    tree = {".private-cache": {"host": "h1", "rclone.remote": "r1:/"}}
    r = resolve(tree, "h1", ["h1"])
    assert paths(r["server_subtrees"]) == {".private-cache"}


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


def test_filter_rclone_conf_group_only_peer_section_collapses_to_one():
    # a `group`-only per-user peer dependency gets exactly one synthesized
    # sftp section, at the owning host's own shared mount path -- not one
    # per member (plan_mounts()'s matching collapse; the owning host's
    # disk genuinely has nothing at a per-member path for this grant, so a
    # per-member section here would point sftp at a path that doesn't
    # exist)
    resolved = {
        "server_subtrees": [],
        "client_mounts": [],
        "samba_shares": [],
        "peer_dependencies": [
            {
                "owning_host": "host-b",
                "local_path": "home/%U/mw-fam",
                "remote_path": "home/%U/mw-fam",
                "samba_node": "home",
                "per_user": True,
                "access": {"group": "Michael Whitfield Family", "permissions": "rwx"},
            }
        ],
    }
    hostvars = {"host-b": {"ansible_host": "10.0.0.2"}}
    group_members = {"Michael Whitfield Family": ["mike", "dana", "jd"]}

    out = filter_rclone_conf(MASTER_INI, resolved, hostvars, group_members)

    assert "[peer-host-b-home-.mounts-mw-fam]" in out
    assert "path = /srv/stortree/home/.mounts/mw-fam" in out
    for user in ("mike", "dana", "jd"):
        assert f"home-{user}-mw-fam" not in out


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
        "top": {
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


def test_merged_getent_results_accumulates_across_loop_iterations():
    # each loop iteration of a looped ansible.builtin.getent task returns
    # its own single-name ansible_facts.getent_<database> -- confirmed in
    # production that Ansible's default fact-merge behaviour replaces
    # (not merges into) the host's whole fact each time, so only the last
    # iteration's name would survive in ansible_facts.getent_passwd
    # itself. merged_getent_results() rebuilds the full map from each
    # iteration's own raw result instead.
    loop_results = [
        {"ansible_facts": {"getent_passwd": {"jd": ["x", "2101", "2101", "", "/home/jd", "/bin/bash"]}}},
        {"ansible_facts": {"getent_passwd": {"mike": ["x", "2102", "2102", "", "/home/mike", "/bin/bash"]}}},
        {"ansible_facts": {"getent_passwd": {"dana": ["x", "2103", "2103", "", "/home/dana", "/bin/bash"]}}},
    ]
    merged = merged_getent_results(loop_results, "passwd")
    assert merged == {
        "jd": ["x", "2101", "2101", "", "/home/jd", "/bin/bash"],
        "mike": ["x", "2102", "2102", "", "/home/mike", "/bin/bash"],
        "dana": ["x", "2103", "2103", "", "/home/dana", "/bin/bash"],
    }
    assert user_uids_from_getent(merged) == {"jd": 2101, "mike": 2102, "dana": 2103}


def test_merged_getent_results_handles_missing_or_empty_ansible_facts():
    # a failed loop iteration (e.g. fail_key on a missing name) may carry
    # no ansible_facts at all -- shouldn't blow up the merge, just
    # contribute nothing for that iteration.
    loop_results = [
        {"ansible_facts": {"getent_group": {"g1": ["x", "2001", "a,b"]}}},
        {"failed": True},
        {"ansible_facts": {}},
    ]
    assert merged_getent_results(loop_results, "group") == {"g1": ["x", "2001", "a,b"]}


def test_needed_users_covers_server_subtrees_and_peer_dependencies():
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    assert needed_users(r) == ["jd"]


def test_needed_users_with_group_members_also_covers_container_owners():
    # without group_members (the plain owner-grant-only set, resolvable
    # before group membership itself is -- stortree_secrets' own first
    # use of this, ahead of the getent-group lookup that produces
    # group_members in the first place) jd is the only user; with it,
    # every per-user container's owner is covered too, group-derived ones
    # included, since stortree_secrets needs their numeric UIDs too for a
    # wrapper mount's --uid (user_container_paths(), stortree_mounts).
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    group_members = {
        "Whitfield Family & Friends": ["mike", "jd"],
        "Michael Whitfield Family": ["dana"],
        "Media Production": ["alex"],
    }
    assert needed_users(r, group_members) == ["alex", "dana", "jd", "mike"]


def _container_entry(local_path, owner, requires_slug=None):
    parent = local_path.rsplit("/", 1)[0]
    return {
        "local_path": local_path,
        "owner": owner,
        "staging_path": f"{parent}/stortree-user-{owner}",
        "slug": _slug(local_path),
        "requires_slug": requires_slug,
    }


def test_user_container_paths_owner_and_group_grants():
    # no mount_plan given -- every container's staging path can't be
    # checked against any real mount, so requires_slug is None
    # throughout (stortree_mounts' "plain local, chown directly" case).
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    group_members = {
        "Whitfield Family & Friends": ["mike", "jd"],
        "Michael Whitfield Family": ["dana"],
        "Media Production": ["alex"],
    }
    containers = user_container_paths(r, group_members)
    assert containers == [
        _container_entry("tree/home/alex", "alex"),
        _container_entry("tree/home/dana", "dana"),
        _container_entry("tree/home/jd", "jd"),
        _container_entry("tree/home/mike", "mike"),
    ]


def test_user_container_paths_covers_peer_dependencies_too():
    # gadget owns nothing itself -- every per-user container it still
    # needs to create/own comes from peer_dependencies alone, same
    # reasoning as needed_groups()/needed_users() covering both scopes.
    r = resolve(EXAMPLE_TREE, "some-storage-gadget", EXAMPLE_HOSTS)
    group_members = {
        "Whitfield Family & Friends": ["jd"],
        "Michael Whitfield Family": [],
        "Media Production": ["alex"],
    }
    containers = user_container_paths(r, group_members)
    assert containers == [
        _container_entry("tree/home/alex", "alex"),
        _container_entry("tree/home/jd", "jd"),
    ]


def test_user_container_paths_dedupes_across_sibling_descendants():
    # jd shows up via both sys-configs (owner) and fam (group membership)
    # -- one container, not two, and it must still resolve to exactly the
    # one owner both descendants agree on.
    tree = {
        "top": {
            "host": "h1",
            "subdirs": {
                "home": {
                    "user-subdirs": {
                        "sys-configs": {"access.owner": "jd"},
                        "fam": {"access.group": "Fam"},
                    }
                }
            },
        }
    }
    r = resolve(tree, "h1", ["h1"])
    containers = user_container_paths(r, {"Fam": ["jd", "mo"]})
    assert containers == [
        _container_entry("top/home/jd", "jd"),
        _container_entry("top/home/mo", "mo"),
    ]


def test_user_container_paths_ignores_non_per_user_and_ungranted_nodes():
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {
                "shared": {"access.group": "not-per-user"},
                "home": {
                    "user-subdirs": {
                        # no access at all -- an intermediate per-user
                        # container with nothing granted contributes no
                        # container of its own
                        "empty": {},
                    }
                },
            },
        }
    }
    r = resolve(tree, "h1", ["h1"])
    assert user_container_paths(r, {}) == []


def test_user_container_paths_requires_slug_finds_the_nesting_mount():
    # tree/home/jd's staging path (tree/home/stortree-user-jd) nests
    # under "tree"'s own real mount (storagebox:/) -- requires_slug
    # should name that mount's slug, the signal stortree_mounts uses to
    # render a wrapper mount for this container instead of chowning it
    # directly (plain chown can't work: "tree" is one single rclone
    # mount with one uniform --uid/--gid for everything under it).
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    plan = plan_mounts(r, {"Media Production": ["alex"]})
    containers = user_container_paths(r, {}, plan)
    jd = next(c for c in containers if c["local_path"] == "tree/home/jd")
    assert jd["requires_slug"] == _slug("tree")


def test_user_container_paths_no_requires_slug_for_a_plain_local_tree():
    # a container under a purely local (host-set, no rclone.remote)
    # top-level subtree nests under no real mount at all -- requires_slug
    # stays None, so stortree_mounts chowns it directly instead of
    # rendering a wrapper mount that has nothing to nest under.
    tree = {
        "top": {
            "host": "h1",
            "subdirs": {
                "home": {"user-subdirs": {"sys-configs": {"access.owner": "jd"}}}
            },
        }
    }
    r = resolve(tree, "h1", ["h1"])
    plan = plan_mounts(r, {})
    assert user_container_paths(r, {}, plan) == [_container_entry("top/home/jd", "jd")]


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
    # other: execute-only -- what every path had before `access` existed,
    # plus the traversal bit so a real grant nested underneath (e.g. a
    # user-subdirs descendant) stays reachable through this node.
    assert access_mode({}) == "0751"
    assert access_mode(None) == "0751"


def test_access_mode_group_only_leaves_owner_full_and_sets_group_bits():
    # no `permissions_explicit` key (as a hand-built dict here has none)
    # is treated the same as an unset default -- other stays execute-only.
    assert access_mode({"group": "g", "permissions": "rx"}) == "0751"
    assert access_mode({"group": "g", "permissions": "rwx"}) == "0771"


def test_access_mode_owner_only_is_private_to_that_owner():
    # deliberately no stortree-group carve-out -- an owner-only grant was
    # scoped to one specific person, not shared with anyone else by default
    assert access_mode({"owner": "jd", "permissions": "rwx"}) == "0701"
    assert access_mode({"owner": "jd", "permissions": "rx"}) == "0501"


def test_access_mode_owner_and_group_share_the_same_permissions_level():
    assert access_mode({"owner": "jd", "group": "g", "permissions": "rwx"}) == "0771"


def test_access_mode_explicit_permissions_is_honored_with_no_public_execute():
    # permissions_explicit=True (what _normalize_access() sets when
    # config.yml actually wrote out `permissions:` itself) is a deliberate
    # operator choice -- enforced exactly, no safety-net execute bit added,
    # even though that can make a distinct descendant grant nested
    # underneath this node unreachable.
    access = {"group": "g", "permissions": "rx", "permissions_explicit": True}
    assert access_mode(access) == "0750"
    access = {"owner": "jd", "permissions": "rwx", "permissions_explicit": True}
    assert access_mode(access) == "0700"


def test_access_mode_default_permissions_from_normalize_access_gets_public_execute():
    # end-to-end through _normalize_access(), not a hand-built dict: a
    # config.yml grant with no `permissions` at all is exactly the case
    # the public-execute safety net exists for.
    access = _normalize_access({"owner": "jd"})
    assert access_mode(access) == "0701"
    access = _normalize_access({"group": "g", "permissions": "rx"})
    assert access_mode(access) == "0750"


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


def test_per_user_mount_path_owner_grant_resolves_to_that_owner():
    access = {"owner": "jd", "permissions": "rwx"}
    assert per_user_mount_path("home/%U/sys-configs", access) == "home/jd/sys-configs"


def test_per_user_mount_path_owner_and_group_still_resolves_to_the_owner():
    access = {"owner": "jd", "group": "g", "permissions": "rwx"}
    assert per_user_mount_path("home/%U/sys-configs", access) == "home/jd/sys-configs"


def test_per_user_mount_path_group_only_resolves_to_the_shared_segment():
    access = {"group": "Michael Whitfield Family", "permissions": "rwx"}
    assert per_user_mount_path("home/%U/mw-fam", access) == "home/.mounts/mw-fam"


def test_per_user_mount_path_no_grant_also_resolves_to_the_shared_segment():
    # unreachable via plan_mounts() (access_grant_usernames() returns no
    # one to expand for, so no entry is ever built with this path at all)
    # but per_user_mount_path() itself has no reason to special-case it.
    assert per_user_mount_path("home/%U/x", {}) == "home/.mounts/x"


# -- plan_mounts -----------------------------------------------------------


def test_plan_mounts_expands_per_user_nodes_and_orders_nesting():
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    group_members = {
        "Michael Whitfield Family": ["mike"],
        "Whitfield Family & Friends": ["mike", "dana"],
    }

    plan = plan_mounts(r, group_members)
    by_local_path = {e["local_path"]: e for e in plan}

    # per-user node mw-fam (access.group: Michael Whitfield Family) --
    # `group`-only, so every member's own folder is a symlink back to one
    # shared mount (per_user_mount_path()), not a mount of its own
    assert "tree/home/mike/mw-fam" in by_local_path
    assert by_local_path["tree/home/mike/mw-fam"]["remote"] is None
    assert by_local_path["tree/home/mike/mw-fam"]["symlink_target"] == "tree/home/.mounts/mw-fam"
    assert "tree/home/%U/mw-fam" not in by_local_path
    assert by_local_path["tree/home/.mounts/mw-fam"]["remote"] == "some-remote:/fam"
    # the stortree-bind@.service.j2 template computes its own dependency
    # unit's slug straight from `symlink_target` (via the stortree_slug
    # filter, i.e. _slug()) rather than looking the real entry back up in
    # the plan -- has to agree with the real entry's own `slug` field
    assert (
        _slug(by_local_path["tree/home/mike/mw-fam"]["symlink_target"])
        == by_local_path["tree/home/.mounts/mw-fam"]["slug"]
    )

    # whitfield-media (access.group: Whitfield Family & Friends) is its
    # own, separate node -- mike gets a symlink there too, via his own
    # membership in that group, not via mw-fam's
    assert by_local_path["tree/home/mike/whitfield-media"]["symlink_target"] == "tree/home/.mounts/whitfield-media"
    assert by_local_path["tree/home/dana/whitfield-media"]["symlink_target"] == "tree/home/.mounts/whitfield-media"
    # only one real mount backs both of them
    assert by_local_path["tree/home/.mounts/whitfield-media"]["remote"] == "some-remote:/media"
    # but nobody unresolvable (no group_members entry) gets a symlink
    assert not any(
        p.startswith("tree/home/") and p.endswith("/whitfield-media")
        and p not in (
            "tree/home/mike/whitfield-media",
            "tree/home/dana/whitfield-media",
            "tree/home/.mounts/whitfield-media",
        )
        for p in by_local_path
    )

    # bravo's client mount of `tree` nests everything under it that's
    # sourced through that peer connection
    tree_slug = by_local_path["tree"]["slug"]
    assert by_local_path["tree/home/.mounts/mw-fam"]["requires_slug"] == tree_slug

    # bravo's OWN cache mount is a sibling top-level subtree, not nested
    # under `tree` at all -- it requires nothing (this is the whole point
    # of top-level subtrees being independent: a host's own mount never
    # ends up depending on another top-level subtree's mount being up
    # first)
    assert by_local_path[".bravo-cache"]["requires_slug"] is None


def test_plan_mounts_group_only_per_user_node_collapses_to_one_mount():
    # the redundancy this whole mechanism exists to avoid: without it, 3
    # resolved members of the same group would mean 3 separate rclone
    # mounts of the exact same remote path (3x the VFS cache, 3
    # processes, no cache coherency between them) even though the access
    # grant backing all 3 is identical -- one real, group-gid-owned mount
    # plus 3 symlinks does the same job.
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    group_members = {
        "Whitfield Family & Friends": ["mike", "dana", "jd"],
        "Michael Whitfield Family": [],
    }
    plan = plan_mounts(r, group_members)

    real_mounts = [
        e for e in plan if e["local_path"] == "tree/home/.mounts/whitfield-media"
    ]
    assert len(real_mounts) == 1
    real_mount = real_mounts[0]
    assert real_mount["remote"] == "some-remote:/media"
    assert real_mount["symlink_target"] is None
    assert real_mount["access"] == {
        "group": "Whitfield Family & Friends",
        "permissions": "rx",
        "permissions_explicit": True,
    }

    symlinks = {e["local_path"]: e for e in plan if e["symlink_target"]}
    for user in ("mike", "dana", "jd"):
        link = symlinks[f"tree/home/{user}/whitfield-media"]
        assert link["remote"] is None
        assert link["symlink_target"] == "tree/home/.mounts/whitfield-media"
        # access is enforced once, at the real mount -- the symlink itself
        # carries none of its own
        assert link["access"] == {}

    # a group with no resolved members gets no mount and no symlinks at all
    assert "tree/home/.mounts/mw-fam" not in {e["local_path"] for e in plan}
    assert not any(e["local_path"].endswith("/mw-fam") for e in plan)


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

    assert by_local_path["tree/home/jd/sys-configs"]["access"] == {
        "owner": "jd",
        "permissions": DEFAULT_ACCESS_PERMISSIONS,
        "permissions_explicit": False,
    }
    # media-prod is `group`-only -- its own access grant lives on the one
    # real, shared mount now (per_user_mount_path()), not on alex's symlink
    assert by_local_path["tree/home/.mounts/media-prod"]["access"] == {
        "group": "Media Production",
        "permissions": DEFAULT_ACCESS_PERMISSIONS,
        "permissions_explicit": False,
    }
    assert by_local_path["tree/home/alex/media-prod"]["access"] == {}
    assert by_local_path["tree/home/alex/media-prod"]["symlink_target"] == "tree/home/.mounts/media-prod"
    assert by_local_path["tree/backups"]["access"] == {}


def test_plan_mounts_peer_sources_samba_descendants_it_does_not_own():
    # gadget owns nothing (docs/config-schema.md worked example) -- every
    # piece of `tree/home` it must still serve via Samba (spec.md "Samba
    # sharing is universal") comes from a real mount now, sourced
    # directly (mesh) from whichever host actually owns that piece, not
    # funneled through tree's own owner.
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
    assert "tree/home/jd/sys-configs" in by_local_path
    sys_configs = by_local_path["tree/home/jd/sys-configs"]
    assert sys_configs["remote"] == (
        "peer-storage-node-alpha-tree-home-jd-sys-configs:"
        "/srv/stortree/tree/home/jd/sys-configs"
    )

    # bravo-owned, per-user, `group`-only -- gadget peer-sources exactly
    # one real mount, at bravo's own shared path (bravo's own plan_mounts()
    # run resolved whitfield-media to that same path first, per
    # per_user_mount_path() -- nothing ever lives at a per-user path on
    # bravo's disk for a group-only grant, so that's the only real path a
    # peer could source it from), not relayed through alpha
    assert "tree/home/mike/mw-fam" not in {
        p for p, e in by_local_path.items() if e["remote"] and "alpha" in e["remote"]
    }
    whitfield_mount = by_local_path["tree/home/.mounts/whitfield-media"]
    assert whitfield_mount["remote"] == (
        "peer-storage-node-bravo-tree-home-.mounts-whitfield-media:"
        "/srv/stortree/tree/home/.mounts/whitfield-media"
    )
    # mike's own folder is a symlink to that one real mount, not a peer
    # mount of its own
    whitfield_link = by_local_path["tree/home/mike/whitfield-media"]
    assert whitfield_link["remote"] is None
    assert whitfield_link["symlink_target"] == "tree/home/.mounts/whitfield-media"

    # the samba node itself ("tree/home") is a pure container -- delegates
    # entirely to its own children above, gets no mount/peer of its own
    assert "tree/home" not in by_local_path

    # every peer-sourced entry under `tree` nests directly under gadget's
    # own client mount of `tree` (also peer-sourced, from alpha) -- not
    # under "tree/home", which was never a mount to nest under in the
    # first place
    tree_slug = by_local_path["tree"]["slug"]
    assert sys_configs["requires_slug"] == tree_slug
    assert whitfield_mount["requires_slug"] == tree_slug


def test_plan_mounts_nested_paths_require_their_nearest_real_mount_ancestor():
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    plan = plan_mounts(r, {"Michael Whitfield Family": ["mike"]})
    by_local_path = {e["local_path"]: e for e in plan}

    tree_slug = by_local_path["tree"]["slug"]
    assert by_local_path["tree/home/.mounts/mw-fam"]["requires_slug"] == tree_slug


def test_plan_mounts_skips_remote_less_ancestors_for_requires_slug():
    # a node with no rclone of its own is a plain directory, not a mount
    # (no systemd unit of its own) -- a real mount nested underneath it
    # has to require the nearest *actual* mounted ancestor instead,
    # skipping over the remote-less one in between
    tree = {
        "tree": {
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
    }
    r = resolve(tree, "h1", ["h1"])
    plan = plan_mounts(r, {})
    by_local_path = {e["local_path"]: e for e in plan}

    top = by_local_path["tree/top"]
    plain = by_local_path["tree/top/plain"]
    nested = by_local_path["tree/top/plain/nested"]

    assert plain["remote"] is None
    assert plain["requires_slug"] == top["slug"]
    assert nested["requires_slug"] == top["slug"]


def test_mount_unit_names():
    plan = [
        {"slug": "backups", "remote": "r1:/"},
        {"slug": "tree", "remote": "r1:/"},
    ]
    assert mount_unit_names(plan) == [
        "stortree-mount@backups.service",
        "stortree-mount@tree.service",
    ]


def test_mount_unit_names_excludes_remote_less_entries():
    # a plain directory (no rclone.remote) gets no systemd unit at all
    plan = [
        {"slug": "backups", "remote": "r1:/"},
        {"slug": "plain-dir", "remote": None},
    ]
    assert mount_unit_names(plan) == ["stortree-mount@backups.service"]


def test_mount_unit_names_includes_bind_units_for_per_user_fan_out():
    # a per-user fan-out entry (symlink_target set, no remote of its own)
    # gets a stortree-bind@ unit, not a stortree-mount@ one -- it's a
    # kernel bind mount back onto the real entry, not a second rclone
    # mount (see plan_mounts()'s own docstring for why a real symlink
    # can't do this job instead).
    plan = [
        {"slug": "tree-home-.mounts-mw\\x2dfam", "remote": "r1:/", "symlink_target": None},
        {"slug": "tree-home-dana-mw\\x2dfam", "remote": None, "symlink_target": "tree/home/.mounts/mw-fam"},
    ]
    assert mount_unit_names(plan) == [
        "stortree-mount@tree-home-.mounts-mw\\x2dfam.service",
        "stortree-bind@tree-home-dana-mw\\x2dfam.service",
    ]


def test_user_mount_unit_names_only_covers_containers_with_a_wrapper_mount():
    # a container with requires_slug set gets a wrapper-mount unit; one
    # without (a plain local container, chowned directly instead) gets
    # none at all.
    containers = [
        {"slug": "tree-home-jd", "requires_slug": "tree"},
        {"slug": "top-home-jd", "requires_slug": None},
    ]
    assert user_mount_unit_names(containers) == ["stortree-user-mount@tree-home-jd.service"]


def test_plan_mounts_slug_distinguishes_hyphen_from_nesting():
    # a segment literally named "media-prod" and a nested "media/prod"
    # both naively collapse to "media-prod" under a plain "/" -> "-"
    # substitution -- they must not share a systemd unit slug
    tree = {
        "tree": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {
                "media-prod": {"rclone.remote": "r1:/a"},
                "media": {"subdirs": {"prod": {"rclone.remote": "r1:/b"}}},
            },
        }
    }
    r = resolve(tree, "h1", ["h1"])
    plan = plan_mounts(r, {})
    by_local_path = {e["local_path"]: e for e in plan}

    assert by_local_path["tree/media-prod"]["slug"] != by_local_path["tree/media/prod"]["slug"]


def test_plan_mounts_orders_entries_shallowest_first():
    # stortree_mounts creates every path one directory level at a time,
    # in this order -- a deeper entry (more "/"-separated segments) must
    # never appear before a shallower one, or a backend that can't create
    # two missing levels in one implicit step (an SMB share, in
    # production) fails outright creating the deeper one first.
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    plan = plan_mounts(r, {"Michael Whitfield Family": ["mike"]})
    depths = [e["local_path"].count("/") for e in plan]
    assert depths == sorted(depths)
