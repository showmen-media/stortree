import re
from pathlib import Path

import pytest
import yaml

from filter_plugins.stortree import (
    _assign_plan_slugs,
    _relate_plan_entries,
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
    plan_remote_sections,
    resolve,
    stale_unit_names,
    samba_access_tokens,
    physical_path,
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
    assert r["client_mounts"] == [
        {
            "local_path": "top",
            "remote": None,
            "args": {},
            "access": {},
            "requires": [],
        }
    ]
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
            "access": {},
            "requires": [],
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


# -- nested clients / client-defaults ------------------------------------


def test_a_nested_client_block_governs_only_its_own_branch():
    # `clients`/`client-defaults` written on a subdirectory, not on the
    # top-level subtree: h2 loses just that one Samba descendant and
    # keeps its sibling, which is the whole point of reading the block
    # at every level rather than only at the top.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {
                "share": {
                    "samba": None,
                    "subdirs": {
                        "a": {
                            "host": "h3",
                            "rclone.remote": "r3:/a",
                            "clients": {"h2": {"rclone": False}},
                        },
                        "b": {"host": "h3", "rclone.remote": "r3:/b"},
                    },
                }
            },
        }
    }
    denied = resolve(tree, "h2", ["h1", "h2", "h3"])
    sourced = {p["local_path"] for p in denied["peer_dependencies"]}
    assert "top/share/a" not in sourced
    assert "top/share/b" in sourced

    # every other non-owning host is untouched by h2's own opt-out
    other = resolve(tree, "h1", ["h1", "h2", "h3"])
    assert {"top/share/a", "top/share/b"} <= {
        p["local_path"] for p in other["peer_dependencies"]
    }

    # and the owner agrees about who it serves: h3 never provisions the
    # peer trust for a mount h2 was just told not to make
    served = resolve(tree, "h3", ["h1", "h2", "h3"])["peer_served_by"]
    assert ("h2", "top/share/a") not in {
        (p["serving_host"], p["local_path"]) for p in served
    }
    assert ("h2", "top/share/b") in {
        (p["serving_host"], p["local_path"]) for p in served
    }


def test_a_nested_block_beats_its_ancestors_and_inherits_where_it_says_nothing():
    # Two axes at once (_client_policy): `clients.<host>` beats
    # `client-defaults` within a node, a deeper node beats a shallower
    # one, and `args` accumulate down the whole chain instead of the
    # nearest block replacing them.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "client-defaults": {"rclone.args": {"dir-cache-time": "5m", "x": "top"}},
            "subdirs": {
                "share": {
                    "samba": None,
                    "client-defaults": {"rclone.args": {"x": "share"}},
                    "clients": {"h2": {"rclone.args": {"vfs-cache-mode": "full"}}},
                    "subdirs": {
                        "deep": {
                            "host": "h3",
                            "rclone.remote": "r3:/deep",
                            "clients": {"h2": {"rclone.args": {"x": "deep"}}},
                        }
                    },
                }
            },
        }
    }
    r = resolve(tree, "h2", ["h1", "h2", "h3"])
    deep = next(
        p for p in r["peer_dependencies"] if p["local_path"] == "top/share/deep"
    )
    assert deep["args"] == {
        "dir-cache-time": "5m",  # inherited from the top-level subtree
        "vfs-cache-mode": "full",  # from an intermediate node's clients entry
        "x": "deep",  # deepest, most specific block wins the conflict
    }


def test_a_subdirectory_can_be_opted_back_in_under_an_opted_out_subtree():
    # The allow-list idiom, one level down: the subtree is off every
    # non-owning host, and a single node inside it is handed to one
    # client on its own. It gets a client mount at its *own* path,
    # sourced from its own resolved owner -- there's no ancestor mount
    # left to reach it through.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "client-defaults": {"rclone": False},
            "subdirs": {
                "pub": {
                    "rclone.remote": "r1:/pub",
                    "clients": {"h2": {"rclone": True}},
                },
                "priv": {"rclone.remote": "r1:/priv"},
            },
        }
    }
    allowed = resolve(tree, "h2", ["h1", "h2", "h3"])
    assert [m["local_path"] for m in allowed["client_mounts"]] == ["top/pub"]
    assert allowed["client_mounts"][0]["remote"] == (
        "peer-h1-top-pub:/srv/stortree/top/pub"
    )
    # the mount is real all the way down, not just an entry in resolve()
    assert "top/pub" in {e["local_path"] for e in plan_mounts(allowed)}

    denied = resolve(tree, "h3", ["h1", "h2", "h3"])
    assert denied["client_mounts"] == []

    # the owner serves exactly that one path, to exactly that one host
    served = resolve(tree, "h1", ["h1", "h2", "h3"])["peer_served_by"]
    assert [(p["serving_host"], p["local_path"]) for p in served] == [("h2", "top/pub")]


def test_an_opted_in_subdirectory_is_not_mounted_twice():
    # A node can be reached two ways at once -- a Samba descendant this
    # host peer-sources *and* the shallowest enabled node of its own
    # branch. Both are the same mount of the same path from the same
    # host, and planning it twice is a unit-slug clash, so the client
    # mount defers to the Samba peer dependency (which additionally
    # carries the node's own `access`).
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "client-defaults": {"rclone": False},
            "subdirs": {
                "share": {
                    "samba": None,
                    "subdirs": {
                        "a": {
                            "host": "h2",
                            "rclone.remote": "r2:/a",
                            "access.group": "Ops",
                            "client-defaults": {"rclone": True},
                        }
                    },
                }
            },
        }
    }
    r = resolve(tree, "h3", ["h1", "h2", "h3"])
    assert r["client_mounts"] == []
    assert [p["local_path"] for p in r["peer_dependencies"]] == ["top/share/a"]
    assert [e["local_path"] for e in plan_mounts(r)] == ["top/share/a"]
    assert plan_mounts(r)[0]["access"]["group"] == "Ops"


def test_a_top_level_subtree_that_shares_itself_is_not_mounted_twice():
    # The same double-source, reachable with no nested client block at
    # all: a remote-backed top-level subtree carrying `samba:` with no
    # children is its own Samba descendant (_has_own_content()) as well
    # as its own client-mount target.
    tree = {"top": {"host": "h1", "rclone.remote": "r1:/", "samba": None}}
    r = resolve(tree, "h2", ["h1", "h2"])
    assert r["client_mounts"] == []
    assert [e["local_path"] for e in plan_mounts(r)] == ["top"]
    assert plan_mounts(r)[0]["remote"] == "peer-h1-top:/srv/stortree/top"


def test_a_nested_opt_out_under_a_mounted_ancestor_carves_no_hole():
    # Honest about the one thing this can't do: an enabled ancestor is a
    # single peer-sftp mount of the owning host's copy, and a subdir
    # opted out below it is still inside that mount. The block isn't
    # ignored -- it governs the mounts that node gets in its own right,
    # which here (nothing Samba-shared, nothing separately owned) is
    # none.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {"inner": {"client-defaults": {"rclone": False}}},
        }
    }
    r = resolve(tree, "h2", ["h1", "h2"])
    assert [m["local_path"] for m in r["client_mounts"]] == ["top"]


def test_a_per_user_node_is_never_a_client_mount_target():
    # A `user-subdirs` descendant's path is still %U-templated and fans
    # out into one mount per granted user; a client_mounts entry
    # describes a single mount and has no expansion step, so the descent
    # stops there. Nothing is silently mounted at a literal "%U" path.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "client-defaults": {"rclone": False},
            "subdirs": {
                "home": {
                    "user-subdirs": {
                        "docs": {
                            "host": "h3",
                            "rclone.remote": "r3:/docs",
                            "access.group": "Staff",
                            "client-defaults": {"rclone": True},
                        }
                    }
                }
            },
        }
    }
    r = resolve(tree, "h2", ["h1", "h2", "h3"])
    assert r["client_mounts"] == []
    assert not any(PER_USER_PLACEHOLDER in e["local_path"] for e in plan_mounts(r))


def test_the_descent_stops_at_a_subtree_this_host_serves_itself():
    # A node this host owns is served from its own local tree, never
    # mounted from a peer -- the same rule the top-level loop always
    # applied, now reached one level down.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "client-defaults": {"rclone": False},
            "subdirs": {"mine": {"host": "h2", "rclone.remote": "r2:/mine"}},
        }
    }
    r = resolve(tree, "h2", ["h1", "h2"])
    assert r["client_mounts"] == []
    assert paths(r["server_subtrees"]) == {"top/mine"}


def test_a_nested_client_block_leaves_the_worked_example_alone():
    # The whole feature is additive: a tree that only ever writes
    # `client-defaults`/`clients` on its top-level subtrees resolves
    # exactly as it did when that was the only level read at all.
    for hostname in EXAMPLE_HOSTS:
        r = resolve(EXAMPLE_TREE, hostname, EXAMPLE_HOSTS)
        for mount in r["client_mounts"]:
            assert "/" not in mount["local_path"]


# -- access in a client block --------------------------------------------


def test_client_defaults_access_grants_a_client_mount_its_own_ownership():
    # A top-level client mount carries no `access` at all by default --
    # the owning host is what enforces the node's own grant. A client
    # block is how this host's own copy gets one, which is what turns
    # into rclone's --uid/--gid/--dir-perms for that mount.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "client-defaults": {"access": {"group": "Readers", "permissions": "rx"}},
        }
    }
    r = resolve(tree, "h2", ["h1", "h2"])
    assert r["client_mounts"][0]["access"] == {
        "group": "Readers",
        "permissions": "rx",
        "permissions_explicit": True,
    }
    # it has to reach the flat plan and the getent lookups too, or the
    # unit template has no gid to render
    assert plan_mounts(r)[0]["access"]["group"] == "Readers"
    assert needed_groups(r) == ["Readers"]


def test_a_client_access_replaces_the_nodes_own_grant_rather_than_merging():
    # Deliberately not a merge (_client_policy): the nearest, most
    # specific block that sets `access` supplies the whole grant for
    # this host's copy. Half a grant assembled from two places would be
    # unreadable off the config.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {
                "share": {
                    "samba": None,
                    "subdirs": {
                        "leaf": {
                            "host": "h3",
                            "rclone.remote": "r3:/leaf",
                            "access": {"group": "Owners", "permissions": "rwx"},
                            "client-defaults": {"access.group": "Readers"},
                            "clients": {"h2": {"access": {"owner": "jd"}}},
                        }
                    },
                }
            },
        }
    }
    leaf_access = lambda host: next(  # noqa: E731
        p["access"]
        for p in resolve(tree, host, ["h1", "h2", "h3"])["peer_dependencies"]
        if p["local_path"] == "top/share/leaf"
    )
    # clients.<host> beats client-defaults, and neither keeps anything
    # of the node's own `group`/`permissions`
    assert leaf_access("h2") == {
        "owner": "jd",
        "permissions": DEFAULT_ACCESS_PERMISSIONS,
        "permissions_explicit": False,
    }
    assert leaf_access("h1") == {
        "group": "Readers",
        "permissions": DEFAULT_ACCESS_PERMISSIONS,
        "permissions_explicit": False,
    }
    # the owning host is untouched: `clients`/`client-defaults` only ever
    # describe a host that doesn't own the node
    owner = resolve(tree, "h3", ["h1", "h2", "h3"])
    assert by_path(owner["server_subtrees"], "top/share/leaf")["access"] == {
        "group": "Owners",
        "permissions": "rwx",
        "permissions_explicit": True,
    }


def test_an_empty_client_access_drops_the_nodes_grant_on_that_client():
    # `access:` written with nothing in it is the way to say "no grant
    # here", distinct from writing no `access` at all (which keeps
    # whatever the entry would otherwise have carried).
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {
                "share": {
                    "samba": None,
                    "subdirs": {
                        "leaf": {
                            "host": "h3",
                            "rclone.remote": "r3:/leaf",
                            "access.group": "Owners",
                            "clients": {"h2": {"access": None}},
                        }
                    },
                }
            },
        }
    }
    kept = resolve(tree, "h1", ["h1", "h2", "h3"])
    dropped = resolve(tree, "h2", ["h1", "h2", "h3"])
    leaf = lambda r: next(  # noqa: E731
        p for p in r["peer_dependencies"] if p["local_path"] == "top/share/leaf"
    )
    assert leaf(kept)["access"]["group"] == "Owners"
    assert leaf(dropped)["access"] == {}
    assert needed_groups(dropped) == []


def test_a_client_access_owner_reaches_needed_users():
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "clients": {"h2": {"access.owner": "jd"}},
        }
    }
    assert needed_users(resolve(tree, "h2", ["h1", "h2"])) == ["jd"]


def test_client_block_rejects_an_unknown_access_key():
    tree = {
        "top": {
            "host": "h1",
            "clients": {"h2": {"access": {"grup": "Ops"}}},
        }
    }
    with pytest.raises(ValueError, match=r"unknown `clients\.h2\.access` key 'grup'"):
        resolve(tree, "h2", ["h1", "h2"])


def test_client_defaults_rejects_an_unknown_access_key():
    tree = {"top": {"host": "h1", "client-defaults": {"access": {"perms": "rx"}}}}
    with pytest.raises(
        ValueError, match=r"unknown `client-defaults\.access` key 'perms'"
    ):
        resolve(tree, "h2", ["h1", "h2"])


def test_access_must_be_an_object_not_a_scalar():
    tree = {"top": {"host": "h1", "access": "Ops"}}
    with pytest.raises(ValueError, match="must be a single object"):
        resolve(tree, "h1", ["h1"])


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
        "server_subtrees": [
            {"path": "own", "remote": "remote-a:/x", "args": {}, "access": {}}
        ],
        "client_mounts": [],
        "samba_shares": [],
        "peer_dependencies": [
            {
                "owning_host": "host-b",
                "local_path": "shared/piece-b",
                "remote_path": "shared/piece-b",
                "samba_node": "shared",
                "per_user": False,
                "args": {},
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
    # per actual user, named for the same resolved path the mount that
    # uses it lands on -- which is now true by construction, since the
    # section list is read straight off that mount plan
    # (plan_remote_sections())
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
                "args": {},
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
                "args": {},
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
                    "samba": {},
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


def _entry(plan, local_path):
    return next(e for e in plan if e["local_path"] == local_path)


def test_requires_orders_a_client_mount_after_a_sibling_cache_subtree():
    # the case the key exists for: a top-level subtree whose *client*
    # points its cache-dir into another top-level subtree's mount. No
    # nesting relationship at all, so requires_slug can't derive it.
    tree = {
        "tree": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "requires": [".cache"],
            "clients": {"h2": {"rclone.args": {"cache-dir": "/srv/stortree/.cache"}}},
        },
        ".cache": {
            "host": "h2",
            "rclone.remote": "r2:/",
            "client-defaults": {"rclone": False},
        },
    }
    plan = plan_mounts(resolve(tree, "h2", ["h1", "h2"]))
    assert _entry(plan, "tree")["requires_mounts"] == [
        {"local_path": ".cache", "slug": ".cache"}
    ]
    # and it really is the mount that isn't nested under it
    assert _entry(plan, "tree")["requires_slug"] is None

    # h1 owns `tree` and never mounts .cache at all (client-defaults
    # rclone: false), so the same declaration resolves to nothing there
    # rather than to a unit that doesn't exist.
    plan_h1 = plan_mounts(resolve(tree, "h1", ["h1", "h2"]))
    assert _entry(plan_h1, "tree")["requires_mounts"] == []


def test_example_tree_requires_resolves_only_where_the_cache_is_mounted():
    # the worked example (docs/config-schema.md, config.yml.example):
    # `tree` requires .bravo-cache, which only storage-node-bravo mounts.
    by_host = {
        h: plan_mounts(resolve(EXAMPLE_TREE, h, EXAMPLE_HOSTS)) for h in EXAMPLE_HOSTS
    }
    assert _entry(by_host["storage-node-bravo"], "tree")["requires_mounts"] == [
        {"local_path": ".bravo-cache", "slug": ".bravo\\x2dcache"}
    ]
    for host in ("storage-node-alpha", "some-storage-gadget"):
        assert _entry(by_host[host], "tree")["requires_mounts"] == []


def test_requires_accepts_a_bare_string_and_applies_to_a_server_subtree():
    tree = {
        "tree": {"host": "h1", "rclone.remote": "r1:/", "requires": ".cache"},
        ".cache": {"host": "h1", "rclone.remote": "r2:/"},
    }
    plan = plan_mounts(resolve(tree, "h1", ["h1"]))
    assert _entry(plan, "tree")["requires_mounts"] == [
        {"local_path": ".cache", "slug": ".cache"}
    ]


def test_requires_on_a_plain_local_target_resolves_to_nothing():
    # a real node, so not a config error -- but no rclone.remote means no
    # unit to depend on; it's an ordinary directory this same apply
    # creates before anything starts.
    tree = {
        "tree": {"host": "h1", "rclone.remote": "r1:/", "requires": [".cache"]},
        ".cache": {"host": "h1"},
    }
    plan = plan_mounts(resolve(tree, "h1", ["h1"]))
    assert _entry(plan, "tree")["requires_mounts"] == []


def test_requires_reaches_a_nested_node_and_leaves_bind_mounts_alone():
    # a per-user node can *declare* requires (it lands on the one real
    # mount); the bind mounts fanning that mount out don't repeat it.
    tree = {
        "tree": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {
                "home": {
                    "samba": {},
                    "user-subdirs": {
                        "fam": {
                            "access.group": "Fam",
                            "requires": [".cache"],
                        }
                    },
                }
            },
        },
        ".cache": {"host": "h1", "rclone.remote": "r2:/"},
    }
    plan = plan_mounts(resolve(tree, "h1", ["h1"]), {"Fam": ["ann", "bo"]})
    real = _entry(plan, "tree/home/.mounts/fam")
    assert real["requires_mounts"] == [{"local_path": ".cache", "slug": ".cache"}]
    for user in ("ann", "bo"):
        assert _entry(plan, f"tree/home/{user}/fam")["requires_mounts"] == []


def test_requires_rejects_an_unknown_path():
    tree = {"tree": {"host": "h1", "rclone.remote": "r1:/", "requires": [".typo"]}}
    with pytest.raises(ValueError, match="not a path anywhere in the tree"):
        resolve(tree, "h1", ["h1"])


def test_requires_rejects_self_and_per_user_targets():
    with pytest.raises(ValueError, match="lists itself"):
        resolve({"tree": {"host": "h1", "requires": ["tree"]}}, "h1", ["h1"])

    tree = {
        "tree": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "requires": ["tree/home/%U/fam"],
            "subdirs": {
                "home": {"user-subdirs": {"fam": {"access.group": "Fam"}}},
            },
        }
    }
    with pytest.raises(ValueError, match="per-user node"):
        resolve(tree, "h1", ["h1"])


def test_requires_rejects_a_cycle():
    tree = {
        "a": {"host": "h1", "rclone.remote": "r1:/", "requires": ["b"]},
        "b": {"host": "h1", "rclone.remote": "r2:/", "requires": ["a"]},
    }
    with pytest.raises(ValueError, match="`requires` cycle"):
        resolve(tree, "h1", ["h1"])


def test_requires_rejects_a_malformed_value():
    with pytest.raises(ValueError, match="must each be a"):
        resolve({"tree": {"host": "h1", "requires": [7]}}, "h1", ["h1"])
    with pytest.raises(ValueError, match="not a mapping"):
        resolve({"tree": {"host": "h1", "requires": {"path": "x"}}}, "h1", ["h1"])


CONTAINERS_FOR_PHYSICAL_PATH = [
    {
        "local_path": "tree/home/jd",
        "owner": "jd",
        "staging_path": "tree/home/stortree-user-jd",
        "slug": "tree-home-jd",
        "requires_slug": "tree",
    },
    {
        "local_path": "top/home/dana",
        "owner": "dana",
        "staging_path": "top/home/stortree-user-dana",
        "slug": "top-home-dana",
        "requires_slug": None,
    },
]


def test_physical_path_redirects_inside_a_wrapped_container():
    # the container path itself is the wrapper's mountpoint and has to
    # stay physically where it is; anything *under* it is only visible
    # through the wrapper, so it has to be created in the staging
    # directory the wrapper re-presents from.
    assert (
        physical_path("tree/home/jd/mw-fam", CONTAINERS_FOR_PHYSICAL_PATH)
        == "tree/home/stortree-user-jd/mw-fam"
    )
    assert (
        physical_path("tree/home/jd/a/b/c", CONTAINERS_FOR_PHYSICAL_PATH)
        == "tree/home/stortree-user-jd/a/b/c"
    )
    assert physical_path("tree/home/jd", CONTAINERS_FOR_PHYSICAL_PATH) == "tree/home/jd"


def test_physical_path_leaves_everything_else_alone():
    # an unwrapped (plain local, directly chowned) container has no
    # wrapper mount shadowing anything, so nothing under it moves; nor
    # does an unrelated path, nor a same-prefix sibling of a container.
    assert (
        physical_path("top/home/dana/mw-fam", CONTAINERS_FOR_PHYSICAL_PATH)
        == "top/home/dana/mw-fam"
    )
    assert physical_path("tree/home/jdoe/x", CONTAINERS_FOR_PHYSICAL_PATH) == "tree/home/jdoe/x"
    assert physical_path("tree/backups", CONTAINERS_FOR_PHYSICAL_PATH) == "tree/backups"
    assert physical_path("tree/home", []) == "tree/home"


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


# -- paths nothing above reaches -----------------------------------------
#
# Branches that had no test at all -- error paths, opt-out interactions,
# and the shapes the worked example happens never to produce. Grouped
# here rather than scattered above because what they have in common is
# how they were found (a branch-coverage run), not what they're about.


def test_plan_mounts_peer_sources_a_plain_samba_descendant_it_does_not_own():
    # A samba node whose descendant is a plain, non-per-user subtree
    # owned by another host: this host has to peer-mount it to serve a
    # complete share, exactly as it would a per-user one. The worked
    # example only ever produces per-user descendants, so nothing else
    # covers the plain case.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {
                "share": {
                    "samba": {},
                    "subdirs": {"leaf": {"host": "h2", "rclone.remote": "r2:/leaf"}},
                }
            },
        }
    }
    r = resolve(tree, "h1", ["h1", "h2"])
    (peer,) = r["peer_dependencies"]
    assert peer["samba_node"] == "top/share"
    assert peer["per_user"] is False

    leaf = by_path(
        [{"path": e["local_path"], **e} for e in plan_mounts(r, {})],
        "top/share/leaf",
    )
    assert leaf["remote"] == (
        "peer-h2-top-share-leaf:/srv/stortree/top/share/leaf"
    )
    assert leaf["symlink_target"] is None


def test_client_opt_out_beats_universal_samba_sharing():
    # Samba sharing is universal, but `client-defaults.rclone: false`
    # still stops a non-owning host from mounting anything of that
    # subtree -- so it ends up exporting the share with nothing behind
    # it. Worth pinning: the two rules pull in opposite directions and
    # the resolution isn't obvious from either one's own docs.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "client-defaults": {"rclone": False},
            "subdirs": {
                "share": {
                    "samba": {},
                    "subdirs": {"leaf": {"host": "h2", "rclone.remote": "r2:/leaf"}},
                }
            },
        }
    }
    r = resolve(tree, "h3", ["h1", "h2", "h3"])
    assert len(r["samba_shares"]) == 1
    assert r["peer_dependencies"] == []
    assert r["client_mounts"] == []


def test_plan_mounts_rejects_two_entries_that_resolve_to_one_unit_slug():
    # Slugs are systemd unit instance names, so two entries sharing one
    # means a single unit file rendered twice, with whichever entry the
    # loop reaches last silently winning. _escape_slug_segment() makes
    # the encoding injective, so two *different* paths can't collide
    # (test_plan_mounts_slug_distinguishes_hyphen_from_nesting) -- what
    # this guard actually catches is the same path planned twice, which
    # is a resolve() bug rather than a config one. Kept anyway: it's a
    # cheap assertion at exactly the point the damage would be done.
    duplicate = {
        "path": "top/leaf",
        "host": "h1",
        "remote": "r:/",
        "args": {},
        "access": {},
        "per_user": False,
        "requires": [],
    }
    resolved = {
        "server_subtrees": [duplicate, dict(duplicate, remote="other:/")],
        "client_mounts": [],
        "samba_shares": [],
        "peer_dependencies": [],
    }
    with pytest.raises(ValueError, match="resolve to systemd unit slug"):
        plan_mounts(resolved, {})


def test_slug_encoding_is_injective_so_no_two_paths_can_collide():
    # The property the guard above can lean on: "-" inside a segment is
    # escaped, "/" is the only thing that becomes a literal "-", and the
    # escape character itself is escaped too, so decoding is unambiguous.
    assert _slug("a-b") == "a\\x2db"
    assert _slug("a/b") == "a-b"
    assert _slug("a\\x2db") != _slug("a-b")


def test_slug_of_a_single_segment_path_is_that_segment():
    assert _slug("tree") == "tree"


def test_no_resolved_path_is_ever_empty():
    # _slug(), _peer_section_name() and _peer_remote_ref() all used to
    # carry a special case for `local_path == ""` -- the one shared tree
    # root's own client mount, back when a single root existed. Every
    # path now starts at a named top-level subtree (_walk_tree()), so
    # there is no empty path left to special-case. This is the guard on
    # that: reintroduce one and the removed branches become live again,
    # silently, as a systemd instance name and an INI section name that
    # are both just their prefix.
    members = {
        "Whitfield Family & Friends": ["jd", "mw"],
        "Michael Whitfield Family": ["mw"],
        "Media Production": ["jd"],
    }
    for host in EXAMPLE_HOSTS:
        resolved = resolve(EXAMPLE_TREE, host, EXAMPLE_HOSTS)
        for scope in ("server_subtrees", "client_mounts", "peer_dependencies"):
            for entry in resolved[scope]:
                assert entry.get("path") or entry.get("local_path"), (host, scope)
        for entry in plan_mounts(resolved, members):
            assert entry["local_path"], (host, entry)
            assert entry["slug"], (host, entry)


def test_a_node_can_mix_the_dotted_and_nested_rclone_forms():
    # `rclone.remote:` alongside `rclone.args:` on the same node -- two
    # dotted keys expanding to the same parent, which only works if the
    # expansion merges rather than the second overwriting the first.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "rclone.args": {"vfs-cache-mode": "full"},
        }
    }
    (subtree,) = resolve(tree, "h1", ["h1"])["server_subtrees"]
    assert subtree["remote"] == "r1:/"
    assert subtree["args"] == {"vfs-cache-mode": "full"}


def test_a_three_segment_dotted_key_is_rejected_rather_than_dropped():
    # Dot expansion splits on the *last* dot only -- the rule that keeps
    # a literal `.cache.subdirs` intact (docs/config-schema.md "A dotted
    # -path map key") -- so `rclone.args.vfs-cache-mode:` expands to a
    # key literally named "rclone.args", which is not in the schema.
    # That used to be read as "not a key I know" and silently dropped,
    # losing the arg with no error and no mount difference to notice it
    # by. Now it names itself.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "rclone.args.vfs-cache-mode": "full",
        }
    }
    with pytest.raises(ValueError, match="unknown key 'rclone.args'"):
        resolve(tree, "h1", ["h1"])


def test_needed_users_covers_a_peer_dependencys_owner_grant():
    # stortree_secrets calls needed_users() before `getent group` has
    # run, so group_members is None there -- an owner grant on a peer
    # dependency is the one thing it can still resolve, and the host
    # needs that user to exist to own the peer-sourced path.
    resolved = {
        "server_subtrees": [],
        "peer_dependencies": [
            {
                "owning_host": "h2",
                "local_path": "top/home/jd/sys-configs",
                "access": {"owner": "jd", "permissions": "rwx"},
            },
            {"owning_host": "h2", "local_path": "top/other", "access": {}},
            {"owning_host": "h2", "local_path": "top/none"},
        ],
    }
    assert needed_users(resolved) == ["jd"]


def test_filter_rclone_conf_keeps_a_client_mounts_own_direct_remote():
    # A client mount is usually peer-sourced, but a host named under
    # `clients:` for a subtree it doesn't own can still end up with a
    # direct third-party remote -- that section has to survive the
    # filter, or the mount starts with no credentials for it.
    conf = "[direct]\ntype = sftp\n\n[unrelated]\ntype = s3\n"
    resolved = {
        "server_subtrees": [],
        "client_mounts": [{"local_path": "top", "remote": "direct:/", "args": {}}],
        "samba_shares": [],
        "peer_dependencies": [],
    }
    out = filter_rclone_conf(conf, resolved)
    assert "[direct]" in out
    assert "[unrelated]" not in out


def test_filter_rclone_conf_ignores_entries_with_no_remote_at_all():
    # A plain-directory entry has remote None. It has to drop out before
    # any section name is computed from it, rather than blowing up or
    # inventing a section -- and drop out of the *output* too, since a
    # host holding a credential it never mounts with is the thing the
    # scoping rule exists to prevent.
    conf = "[only]\ntype = sftp\n"
    resolved = {
        "server_subtrees": [
            {"path": "top", "remote": None, "args": {}, "access": {}}
        ],
        "client_mounts": [{"local_path": "top/x", "remote": None, "args": {}}],
        "samba_shares": [{"descendants": [{"path": "top/x", "remote": None}]}],
        "peer_dependencies": [],
    }
    out = filter_rclone_conf(conf, resolved)
    assert "[only]" not in out
    assert out.strip() == ""


def test_filter_rclone_conf_never_ships_a_samba_descendants_third_party_remote():
    # Samba sharing is universal, so `samba_shares` names every share in
    # the whole tree on every host -- descendants included, owned by
    # whoever. Reading remotes out of it hands each host the credentials
    # for every remote referenced anywhere under a share: nodes it
    # doesn't own, doesn't peer, and here has been explicitly opted out
    # of. spec.md §3 promises the opposite ("other remotes stay off it
    # entirely"), and h3 below is exactly its worked example -- a host
    # with no subtree of its own at all.
    tree = {
        "tree": {
            "host": "h1",
            "samba": {},
            "subdirs": {
                "private": {
                    "host": "h2",
                    "rclone.remote": "secret-remote:/",
                    "client-defaults": {"rclone": False},
                }
            },
        }
    }
    conf = "[secret-remote]\ntype = sftp\nuser = u\npass = s3cret\n"
    fleet = ["h1", "h2", "h3"]

    # h2 owns it, so it keeps the credentials it actually mounts with.
    assert "[secret-remote]" in filter_rclone_conf(conf, resolve(tree, "h2", fleet))
    # h1 owns the share but not this descendant; h3 owns nothing at all.
    # Both are opted out of mounting it, so neither has any use for it.
    for host in ("h1", "h3"):
        out = filter_rclone_conf(conf, resolve(tree, host, fleet))
        assert "[secret-remote]" not in out, host
        assert "s3cret" not in out, host


def test_filter_rclone_conf_keeps_a_remote_for_a_samba_node_this_host_owns():
    # The other side of the rule above: dropping `samba_shares` from the
    # scan must not cost a host a section it really does mount, which it
    # doesn't -- resolve() puts every node this host owns in
    # `server_subtrees`, whether or not it also carries `samba:`.
    tree = {
        "tree": {
            "host": "h1",
            "samba": {},
            "rclone.remote": "mine:/",
            "subdirs": {
                "sub": {"host": "h1", "rclone.remote": "mine-nested:/", "samba": {}}
            },
        }
    }
    conf = "[mine]\ntype = sftp\n\n[mine-nested]\ntype = sftp\n"
    out = filter_rclone_conf(conf, resolve(tree, "h1", ["h1", "h2"]))
    assert "[mine]" in out
    assert "[mine-nested]" in out


def test_filter_rclone_conf_rejects_two_hosts_claiming_one_peer_section():
    # `peer-<host>-<flattened path>` is readable rather than injectively
    # escaped (an rclone remote name can't hold _slug()'s \\xHH escapes),
    # so two entries can land on one name. Same-host collisions are
    # harmless -- see _check_peer_section_clash() -- but two *different*
    # owning hosts on one name means the second write replaces the
    # first's address, and a mount silently sources its data from the
    # wrong machine. Here "storage" + "node/alpha/tree" and
    # "storage-node-alpha" + "tree" both flatten to
    # peer-storage-node-alpha-tree.
    resolved = {
        "server_subtrees": [],
        "client_mounts": [],
        "samba_shares": [],
        "peer_dependencies": [
            {
                "owning_host": "storage",
                "local_path": "node/alpha/tree",
                "remote_path": "node/alpha/tree",
                "samba_node": "node",
                "per_user": False,
                "args": {},
            },
            {
                "owning_host": "storage-node-alpha",
                "local_path": "tree",
                "remote_path": "tree",
                "samba_node": "tree",
                "per_user": False,
                "args": {},
            },
        ],
    }
    with pytest.raises(ValueError) as excinfo:
        filter_rclone_conf("", resolved)
    message = str(excinfo.value)
    assert "peer-storage-node-alpha-tree" in message
    assert "node/alpha/tree" in message
    assert "rename one of them" in message


def test_filter_rclone_conf_allows_one_host_claiming_a_section_twice():
    # The same collision within a single owning host is not an error: the
    # section body is a function of the owning host alone (its address,
    # user, key file), and each mount carries its own real path in its
    # `remote:path` reference rather than in the section. Rejecting this
    # would fail a config that works.
    resolved = {
        "server_subtrees": [],
        "client_mounts": [],
        "samba_shares": [],
        "peer_dependencies": [
            {
                "owning_host": "h1",
                "local_path": "a/b",
                "remote_path": "a/b",
                "samba_node": "a",
                "per_user": False,
                "args": {},
            },
            {
                "owning_host": "h1",
                "local_path": "a-b",
                "remote_path": "a-b",
                "samba_node": "a-b",
                "per_user": False,
                "args": {},
            },
        ],
    }
    out = filter_rclone_conf("", resolved, {"h1": {"ansible_host": "10.0.0.1"}})
    assert out.count("[peer-h1-a-b]") == 1
    assert "host = 10.0.0.1" in out


def test_an_overridden_stortree_root_reaches_every_generated_path():
    # stortree_root is a role variable an operator can override; it ends
    # up baked into a peer mount's `remote:path` reference and into a
    # synthesized section's own `path`. Hardcoding it in the plugin meant
    # an override produced silently wrong paths in exactly the artifacts
    # nobody watches being generated.
    # No `samba:` here: a samba node is peer-sourced by the share loop
    # instead, which deliberately suppresses the separate client mount.
    tree = {"tree": {"host": "h1", "rclone.remote": "r:/"}}
    resolved = resolve(tree, "h2", ["h1", "h2"], "/data/stortree")
    (client,) = resolved["client_mounts"]
    assert client["remote"] == "peer-h1-tree:/data/stortree/tree"

    (entry,) = [e for e in plan_mounts(resolved, {}, "/data/stortree") if e["remote"]]
    assert entry["remote"] == "peer-h1-tree:/data/stortree/tree"

    out = filter_rclone_conf("", resolved, {}, {}, "/data/stortree", "/opt/st")
    assert "path = /data/stortree/tree" in out
    assert "key_file = /opt/st/peer_ssh_key" in out
    assert "/srv/stortree" not in out


def test_the_path_defaults_still_apply_when_no_root_is_passed():
    # Every existing caller that passes neither keeps the documented
    # /srv/stortree and /etc/stortree, so the new arguments are additive.
    # No `samba:` here: a samba node is peer-sourced by the share loop
    # instead, which deliberately suppresses the separate client mount.
    tree = {"tree": {"host": "h1", "rclone.remote": "r:/"}}
    resolved = resolve(tree, "h2", ["h1", "h2"])
    assert resolved["client_mounts"][0]["remote"] == "peer-h1-tree:/srv/stortree/tree"
    out = filter_rclone_conf("", resolved)
    assert "path = /srv/stortree/tree" in out
    assert "key_file = /etc/stortree/peer_ssh_key" in out


def test_filter_rclone_conf_drops_a_per_user_peer_nobody_is_granted():
    # A group-only per-user peer whose group has no members on this host
    # resolves to no real mount, so it must not get an sftp section
    # either -- an unused section is a peer credential handed to a host
    # with no reason to hold it.
    conf = "[base]\ntype = sftp\n"
    resolved = {
        "server_subtrees": [],
        "client_mounts": [],
        "samba_shares": [],
        "peer_dependencies": [
            {
                "owning_host": "h2",
                "local_path": "top/home/%U/media",
                "remote_path": "top/home/%U/media",
                "samba_node": "top/home",
                "per_user": True,
                "access": {"group": "Nobody Here", "permissions": "rwx"},
                "args": {},
            }
        ],
    }
    assert "peer-h2" not in filter_rclone_conf(conf, resolved, group_members={})
    # ... and does get one as soon as the group has a member.
    granted = filter_rclone_conf(
        conf, resolved, group_members={"Nobody Here": ["someone"]}
    )
    assert "peer-h2" in granted


def test_access_mode_write_only_grant_sets_only_the_write_bit():
    # _permission_bits' r/x branches, neither of which any grant in the
    # worked example leaves out.
    mode = access_mode({"group": "g", "permissions": "w"})
    assert mode[2] == "2"


def test_access_mode_read_write_without_execute_leaves_the_traverse_bit_off():
    mode = access_mode({"group": "g", "permissions": "rw"})
    assert mode[2] == "6"


def test_a_samba_block_marks_a_node_for_export_however_it_is_written():
    # Presence, not truthiness. `samba:` written bare parses as None,
    # and `samba: {}` and `samba: true` are the other two ways to say
    # "share this with the defaults" -- all three used to mean the
    # opposite: the first two resolved to no share at all, silently, and
    # `samba: true` crashed resolve() with an AttributeError from deep
    # inside the share-building loop.
    for block in ({}, None, True, {"name": "top_share"}):
        tree = {"top": {"host": "h1", "rclone.remote": "r:/", "samba": block}}
        (share,) = resolve(tree, "h1", ["h1", "h2"])["samba_shares"]
        assert share["node_path"] == "top"


def test_a_node_with_no_samba_key_is_not_exported():
    tree = {"top": {"host": "h1", "rclone.remote": "r:/"}}
    assert resolve(tree, "h1", ["h1", "h2"])["samba_shares"] == []


def test_samba_false_is_the_one_way_to_opt_a_node_back_out():
    # The only falsy value that ever plausibly meant "don't share this",
    # as opposed to "share it with nothing configured".
    tree = {"top": {"host": "h1", "rclone.remote": "r:/", "samba": False}}
    assert resolve(tree, "h1", ["h1", "h2"])["samba_shares"] == []


def test_a_samba_block_that_is_neither_a_mapping_nor_a_flag_is_rejected():
    tree = {"top": {"host": "h1", "rclone.remote": "r:/", "samba": "yes"}}
    with pytest.raises(ValueError, match="`samba` must be a mapping"):
        resolve(tree, "h1", ["h1", "h2"])


def test_a_bare_samba_block_shares_the_whole_node_with_no_subpath():
    # The end-to-end shape of the defaults case: no subpath, so the
    # share serves the node path itself rather than a %U template.
    tree = {"top": {"host": "h1", "rclone.remote": "r:/", "samba": None}}
    (share,) = resolve(tree, "h1", ["h1", "h2"])["samba_shares"]
    assert share["subpath"] is None
    assert share["local_path"] == "top"


def test_a_share_name_defaults_to_the_node_path_folded_into_a_legal_name():
    # "tree/home" is not a legal smb.conf section header; the fold is
    # what the template used to do inline (docs/config-schema.md "Share
    # names").
    tree = {"tree": {"host": "h1", "subdirs": {"home": {"samba": None}}}}
    (share,) = resolve(tree, "h1", ["h1", "h2"])["samba_shares"]
    assert share["name"] == "tree_home"
    # The name is the only thing it changes -- the path is still real.
    assert share["node_path"] == "tree/home"


def test_samba_name_overrides_the_derived_share_name():
    tree = {
        "tree": {
            "host": "h1",
            "subdirs": {"home": {"samba": {"name": "home"}}},
        }
    }
    (share,) = resolve(tree, "h1", ["h1", "h2"])["samba_shares"]
    assert share["name"] == "home"
    assert share["node_path"] == "tree/home"


def test_samba_name_does_not_leak_back_into_the_callers_config():
    # resolve() fills in a default name *and* the derived subpath; the
    # operator's own dict is not its to write either of them into. An
    # empty block catches both -- anything at all appearing in it is a
    # leak.
    samba = {}
    tree = {"top": {"host": "h1", "samba": samba}}
    resolve(tree, "h1", ["h1", "h2"])
    assert samba == {}


def test_a_samba_name_outside_the_legal_alphabet_is_rejected():
    # Sanitizing it silently would mean the name an operator typed here
    # is not the name they have to type into a mount command.
    tree = {"top": {"host": "h1", "samba": {"name": "home/shared"}}}
    with pytest.raises(ValueError, match="may contain only"):
        resolve(tree, "h1", ["h1", "h2"])


def test_an_empty_or_non_string_samba_name_is_rejected():
    for name in ("", 7, ["home"]):
        tree = {"top": {"host": "h1", "samba": {"name": name}}}
        with pytest.raises(ValueError, match="must be a non-empty string"):
            resolve(tree, "h1", ["h1", "h2"])


def test_a_reserved_smb_conf_section_name_is_rejected():
    # [global] is the killer: it would merge into the generated global
    # block and rewrite fleet-wide settings rather than add a share.
    for name in ("global", "Homes", "printers"):
        tree = {"top": {"host": "h1", "samba": {"name": name}}}
        with pytest.raises(ValueError, match="reserved"):
            resolve(tree, "h1", ["h1", "h2"])


def test_two_shares_claiming_one_name_are_rejected():
    tree = {
        "a": {"host": "h1", "samba": {"name": "media"}},
        "b": {"host": "h1", "samba": {"name": "media"}},
    }
    with pytest.raises(ValueError, match="both export the Samba share name"):
        resolve(tree, "h1", ["h1", "h2"])


def test_two_node_paths_folding_onto_one_name_are_rejected_too():
    # Reachable without anyone writing a `samba.name` at all: a space
    # folds to `_`, which the sibling next to it already spells.
    tree = {
        "a b": {"host": "h1", "samba": None},
        "a_b": {"host": "h1", "samba": None},
    }
    with pytest.raises(ValueError, match="both export the Samba share name"):
        resolve(tree, "h1", ["h1", "h2"])


# -- samba.hidden ----------------------------------------------------------


def test_a_share_is_not_hidden_unless_it_says_so():
    tree = {"a": {"host": "h1", "samba": None}}
    (share,) = resolve(tree, "h1", ["h1"])["samba_shares"]
    assert share["hidden"] is False


def test_samba_hidden_reaches_the_resolved_share():
    tree = {"a": {"host": "h1", "samba": {"hidden": True}}}
    (share,) = resolve(tree, "h1", ["h1"])["samba_shares"]
    assert share["hidden"] is True


def test_samba_hidden_does_not_change_who_may_connect():
    # `hidden` is browse-list suppression, not access control: the
    # share's own grant is untouched by it.
    tree = {"a": {"host": "h1", "access.group": "ops", "samba": {"hidden": True}}}
    (share,) = resolve(tree, "h1", ["h1"])["samba_shares"]
    assert [g["group"] for g in share["access"]] == ["ops"]


# -- per-host shares (a `samba:` inside a client block) --------------------


def _appliance_tree():
    """`spool` is exported on h2 alone -- the host whose local service
    account writes to it. h1 owns the data and exports nothing."""
    return {
        "tree": {
            "host": "h1",
            "rclone.remote": "r:/",
            "subdirs": {
                "spool": {
                    "clients.h2": {
                        "samba": {"name": "spool", "hidden": True},
                        "access.owner": "svc",
                    }
                }
            },
        }
    }


def test_a_client_block_samba_exports_the_node_on_that_host_alone():
    tree = _appliance_tree()
    assert resolve(tree, "h1", ["h1", "h2"])["samba_shares"] == []
    (share,) = resolve(tree, "h2", ["h1", "h2"])["samba_shares"]
    assert (share["name"], share["node_path"], share["hidden"]) == (
        "spool",
        "tree/spool",
        True,
    )


def test_a_per_host_shares_grant_comes_from_the_block_that_declared_it():
    # The whole point of the per-host share: `svc` is a local Unix user
    # on h2 and exists nowhere else, so its grant reaches h2's own
    # `valid users` and no other host's.
    tree = _appliance_tree()
    (share,) = resolve(tree, "h2", ["h1", "h2"])["samba_shares"]
    assert [g["owner"] for g in share["access"]] == ["svc"]
    assert needed_users(resolve(tree, "h1", ["h1", "h2"])) == []
    assert needed_users(resolve(tree, "h2", ["h1", "h2"])) == ["svc"]


def test_a_per_host_share_peer_sources_its_content_on_that_host_only():
    # And the serving side agrees: h1 provisions SSH trust for exactly
    # the mount h2 will make to back the share, and for nothing else.
    tree = _appliance_tree()
    h2 = resolve(tree, "h2", ["h1", "h2"])
    assert ("h1", "tree/spool") in [
        (p["owning_host"], p["local_path"]) for p in h2["peer_dependencies"]
    ]
    h1 = resolve(tree, "h1", ["h1", "h2"])
    assert ("h2", "tree/spool") in [
        (p["serving_host"], p["local_path"]) for p in h1["peer_served_by"]
    ]


def test_client_defaults_samba_exports_on_every_host_but_the_owner():
    tree = {"a": {"host": "h1", "client-defaults": {"samba": {"name": "a"}}}}
    hosts = ["h1", "h2", "h3"]
    assert resolve(tree, "h1", hosts)["samba_shares"] == []
    for h in ("h2", "h3"):
        assert [s["name"] for s in resolve(tree, h, hosts)["samba_shares"]] == ["a"]


def test_the_owning_host_ignores_a_client_block_written_for_itself():
    # `clients`/`client-defaults` describe a host holding a *copy*; the
    # owner holds the original. Same rule `rclone` and `access` follow.
    tree = {"a": {"host": "h1", "clients.h1": {"samba": {"name": "nope"}}}}
    assert resolve(tree, "h1", ["h1", "h2"])["samba_shares"] == []


def test_a_client_block_samba_renames_that_hosts_copy_of_a_universal_share():
    tree = {
        "a": {"host": "h1", "samba": {"name": "shared"}, "clients.h2": {"samba": {"name": "local"}}}
    }
    hosts = ["h1", "h2"]
    assert [s["name"] for s in resolve(tree, "h1", hosts)["samba_shares"]] == ["shared"]
    assert [s["name"] for s in resolve(tree, "h2", hosts)["samba_shares"]] == ["local"]


def test_a_client_block_samba_false_withdraws_a_universal_share_on_that_host():
    tree = {"a": {"host": "h1", "samba": {"name": "a"}, "clients.h2": {"samba": False}}}
    hosts = ["h1", "h2"]
    assert [s["name"] for s in resolve(tree, "h1", hosts)["samba_shares"]] == ["a"]
    assert resolve(tree, "h2", hosts)["samba_shares"] == []


def test_a_client_block_samba_does_not_cascade_to_descendants():
    # `samba` marks the one node it is written on, never that node's
    # subtree -- exactly as a node's own `samba:` does. Cascading would
    # export every descendant under a single name.
    tree = {
        "a": {
            "host": "h1",
            "client-defaults": {"samba": {"name": "outer"}},
            "subdirs": {"b": {}, "c": {}},
        }
    }
    shares = resolve(tree, "h2", ["h1", "h2"])["samba_shares"]
    assert [s["node_path"] for s in shares] == ["a"]


def test_within_one_node_clients_host_beats_client_defaults_for_samba():
    tree = {
        "a": {
            "host": "h1",
            "client-defaults": {"samba": {"name": "default"}},
            "clients": {"h2": {"samba": {"name": "specific"}}},
        }
    }
    hosts = ["h1", "h2", "h3"]
    assert [s["name"] for s in resolve(tree, "h2", hosts)["samba_shares"]] == ["specific"]
    assert [s["name"] for s in resolve(tree, "h3", hosts)["samba_shares"]] == ["default"]


def test_a_per_host_share_of_a_user_subdirs_node_keeps_the_per_user_path():
    # The subpath is derived from the *node's* shape, not from where the
    # `samba:` was written -- a per-host share of a per-user node is
    # still per-user, or it would expose every user's folder to everyone.
    tree = {
        "a": {
            "host": "h1",
            "user-subdirs": {"x": {"access.group": "g"}},
            "clients.h2": {"samba": {"name": "a"}},
        }
    }
    (share,) = resolve(tree, "h2", ["h1", "h2"])["samba_shares"]
    assert share["subpath"] == "%U"


def test_a_share_name_collision_confined_to_one_host_fails_every_host():
    # h2 is the only host that would export both, and its rendered
    # smb.conf is the file that could not hold them -- so the message
    # names h2. It is still a config error, though, and a config error
    # fails the apply everywhere rather than only where it would bite
    # (the same rule `requires` follows): resolving h1 walks what its
    # peers export, to know what trust to provision, and finds it there.
    tree = {
        "a": {"host": "h1", "samba": {"name": "media"}},
        "b": {"host": "h1", "clients.h2": {"samba": {"name": "media"}}},
    }
    for host in ("h1", "h2"):
        with pytest.raises(ValueError, match="share name 'media' on 'h2'"):
            resolve(tree, host, ["h1", "h2"])


def test_a_per_host_share_name_is_free_on_hosts_that_do_not_export_it():
    # The same two nodes, with the per-host share named distinctly:
    # nothing collides, and only h2 sees the second share at all.
    tree = {
        "a": {"host": "h1", "samba": {"name": "media"}},
        "b": {"host": "h1", "clients.h2": {"samba": {"name": "spool"}}},
    }
    hosts = ["h1", "h2"]
    assert [s["name"] for s in resolve(tree, "h1", hosts)["samba_shares"]] == ["media"]
    assert sorted(s["name"] for s in resolve(tree, "h2", hosts)["samba_shares"]) == [
        "media",
        "spool",
    ]


# -- valid users follows what this host actually enforces ------------------


def test_a_client_side_grant_replaces_the_nodes_own_in_valid_users():
    # A share's `valid users` names the principals the filesystem
    # underneath it will admit -- which is the client-side grant on a
    # host holding a copy, and the node's own on the host that owns it.
    tree = {
        "a": {
            "host": "h1",
            "samba": {"name": "a"},
            "subdirs": {
                "d": {"access.owner": "alice", "clients.h2": {"access.owner": "bob"}}
            },
        }
    }
    hosts = ["h1", "h2"]
    (on_h1,) = resolve(tree, "h1", hosts)["samba_shares"]
    (on_h2,) = resolve(tree, "h2", hosts)["samba_shares"]
    assert [g["owner"] for g in on_h1["access"]] == ["alice"]
    assert [g["owner"] for g in on_h2["access"]] == ["bob"]


def test_valid_users_is_identical_everywhere_with_no_client_side_grant():
    # The default is unchanged: without a client block saying otherwise,
    # every host resolves the same share with the same grant.
    tree = {"a": {"host": "h1", "access.group": "ops", "samba": {"name": "a"}}}
    hosts = ["h1", "h2", "h3"]
    grants = [
        [g["group"] for g in resolve(tree, h, hosts)["samba_shares"][0]["access"]]
        for h in hosts
    ]
    assert grants == [["ops"]] * 3


# -- schema validation for the new keys ------------------------------------


def test_a_misspelled_samba_hidden_is_rejected():
    tree = {"a": {"host": "h1", "samba": {"hiden": True}}}
    with pytest.raises(ValueError, match="unknown `samba` key 'hiden'"):
        resolve(tree, "h1", ["h1"])


def test_a_misspelled_key_inside_a_client_block_samba_is_rejected():
    tree = {"a": {"host": "h1", "clients.h2": {"samba": {"nmae": "x"}}}}
    with pytest.raises(ValueError, match=r"unknown `clients.h2.samba` key 'nmae'"):
        resolve(tree, "h1", ["h1", "h2"])


def test_samba_subpath_inside_a_client_block_is_rejected_by_name():
    tree = {"a": {"host": "h1", "clients.h2": {"samba": {"subpath": "%U"}}}}
    with pytest.raises(ValueError, match=r"sets `clients.h2.samba.subpath`"):
        resolve(tree, "h1", ["h1", "h2"])


def test_a_client_block_samba_name_is_held_to_the_same_alphabet():
    tree = {"a": {"host": "h1", "clients.h2": {"samba": {"name": ".hidden"}}}}
    with pytest.raises(ValueError, match="may contain only letters"):
        resolve(tree, "h1", ["h1", "h2"])


def test_client_opt_out_also_withholds_peer_trust_from_the_serving_side():
    # The mirror of test_client_opt_out_beats_universal_samba_sharing,
    # seen from the host that owns the data: if no peer will ever mount
    # it, this host must not list them in peer_served_by either --
    # that's what stortree_peer_trust turns into authorized_keys, and an
    # entry here is real SSH access granted for a mount that can't
    # happen.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "client-defaults": {"rclone": False},
            "subdirs": {
                "share": {
                    "samba": {},
                    "subdirs": {"leaf": {"host": "h2", "rclone.remote": "r2:/leaf"}},
                }
            },
        }
    }
    assert resolve(tree, "h2", ["h1", "h2", "h3"])["peer_served_by"] == []

    # Without the opt-out, both other hosts are served.
    tree["top"].pop("client-defaults")
    served = resolve(tree, "h2", ["h1", "h2", "h3"])["peer_served_by"]
    assert {p["serving_host"] for p in served} == {"h1", "h3"}


def test_requires_keeps_every_entry_of_a_multi_element_list():
    # The worked example only ever declares one, so nothing else walks
    # this loop more than once -- or exercises its dedupe.
    tree = {
        "a": {"host": "h1", "rclone.remote": "ra:/"},
        "b": {"host": "h1", "rclone.remote": "rb:/"},
        "top": {
            "host": "h1",
            "rclone.remote": "r:/",
            "requires": ["a", "/b/", "a"],
        },
    }
    (subtree,) = [
        s
        for s in resolve(tree, "h1", ["h1"])["server_subtrees"]
        if s["path"] == "top"
    ]
    assert subtree["requires"] == ["a", "b"]


def test_group_gids_from_getent_skips_an_entry_the_directory_had_no_answer_for():
    # `getent group` over a name SSSD can't resolve comes back as an
    # empty/short field list rather than an error, and a KeyError here
    # would take down the whole play for one missing group.
    assert group_gids_from_getent(
        {"real": ["x", "20001", ""], "missing": [""], "null-gid": ["x", None]}
    ) == {"real": 20001}


def test_user_uids_from_getent_skips_an_entry_the_directory_had_no_answer_for():
    assert user_uids_from_getent(
        {"jd": ["x", "10001", "10001"], "ghost": [""], "null-uid": ["x", None]}
    ) == {"jd": 10001}



# -- schema validation ----------------------------------------------------
#
# Every one of these used to resolve without complaint, into something
# plausible and wrong. They're grouped by what the typo costs, because
# that's the argument for validating at all: none of them announce
# themselves at apply time.


def test_a_misspelled_rclone_remote_is_rejected_not_left_unmounted():
    # Cost: the node resolves with remote None and becomes a plain
    # directory. The mount never happens, and the share on top of it
    # serves an empty local path.
    tree = {"top": {"host": "h1", "rclone": {"remte": "r1:/"}}}
    with pytest.raises(ValueError, match="unknown `rclone` key 'remte'"):
        resolve(tree, "h1", ["h1"])


def test_a_misspelled_client_defaults_is_rejected_not_silently_ignored():
    # Cost: `client-defaults.rclone: false` is how a subtree is kept off
    # every non-owning host. Misspell the block and every host in the
    # fleet peer-mounts it instead -- with the SSH trust to match.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "client_defaults": {"rclone": False},
        }
    }
    with pytest.raises(ValueError, match="unknown key 'client_defaults'"):
        resolve(tree, "h1", ["h1", "h2"])


def test_a_misspelled_access_principal_is_rejected_not_dropped():
    # Cost: _normalize_access() returns {} for a grant naming neither
    # `group` nor `owner`, so the restriction vanishes and the path
    # keeps the permissive default -- the failure direction that matters
    # for something whose whole job is access control.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "access": {"gorup": "Media Production", "permissions": "rx"},
        }
    }
    with pytest.raises(ValueError, match="unknown `access` key 'gorup'"):
        resolve(tree, "h1", ["h1"])


def test_a_misspelled_subdirs_is_rejected_not_dropped():
    # Cost: the entire subtree under it disappears from the resolved
    # tree, on every host.
    tree = {"top": {"host": "h1", "rclone.remote": "r1:/", "subdir": {"leaf": {}}}}
    with pytest.raises(ValueError, match="unknown key 'subdir'"):
        resolve(tree, "h1", ["h1"])


def test_a_misspelled_samba_key_is_rejected():
    # `name` is the only key a `samba` block still takes, and getting it
    # wrong silently exports the share under its derived path-based name
    # instead of the one clients were told to mount.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "samba": {"nmae": "home"},
        }
    }
    with pytest.raises(ValueError, match="unknown `samba` key 'nmae'"):
        resolve(tree, "h1", ["h1"])


def test_an_unknown_key_inside_a_per_client_override_is_rejected():
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "clients": {"h2": {"rclone": {"arguments": {"dir-cache-time": "5m"}}}},
        }
    }
    with pytest.raises(
        ValueError, match=r"unknown `clients\.h2\.rclone` key 'arguments'"
    ):
        resolve(tree, "h1", ["h1", "h2"])


def test_an_unknown_key_inside_client_defaults_is_rejected():
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "client-defaults": {"rclone": False, "extra": 1},
        }
    }
    with pytest.raises(ValueError, match="unknown `client-defaults` key 'extra'"):
        resolve(tree, "h1", ["h1", "h2"])


def test_validation_reaches_arbitrarily_deep_into_the_tree():
    # _visit() validates every node it walks, not just top-level ones.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {
                "a": {"subdirs": {"b": {"user-subdirs": {"c": {"hsot": "h2"}}}}}
            },
        }
    }
    with pytest.raises(ValueError, match=r"'top/a/b/%U/c' has unknown key 'hsot'"):
        resolve(tree, "h1", ["h1"])


def test_an_unknown_key_error_suggests_the_key_it_is_closest_to():
    # The whole point of failing here rather than at apply time is that
    # the message says what to fix.
    tree = {"top": {"host": "h1", "rclone.remote": "r1:/", "sambaa": {}}}
    with pytest.raises(ValueError, match=r"did you mean 'samba'\?"):
        resolve(tree, "h1", ["h1"])


def test_an_unknown_key_with_no_near_match_still_lists_what_is_allowed():
    tree = {"top": {"host": "h1", "rclone.remote": "r1:/", "zzzzzz": {}}}
    with pytest.raises(ValueError, match="expected one of: access, client-defaults"):
        resolve(tree, "h1", ["h1"])


def test_subdirs_written_as_a_list_names_the_node_it_is_on():
    # Without the check this surfaces as a bare AttributeError from
    # inside the walk, with nothing saying which node it came from.
    tree = {"top": {"host": "h1", "rclone.remote": "r1:/", "subdirs": ["a", "b"]}}
    with pytest.raises(ValueError, match="`subdirs` must be a mapping, got list"):
        resolve(tree, "h1", ["h1"])


def test_a_node_that_is_not_a_mapping_at_all_names_itself():
    tree = {"top": {"host": "h1", "rclone.remote": "r1:/", "subdirs": {"leaf": 42}}}
    with pytest.raises(ValueError, match="'top/leaf' must be a mapping"):
        resolve(tree, "h1", ["h1"])


def test_an_empty_or_null_node_is_still_perfectly_valid():
    # `backups: {}` in the worked example, and the `child or {}` path in
    # _walk_tree for a subdir written with nothing under it.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {"empty": {}, "null": None},
        }
    }
    paths = {s["path"] for s in resolve(tree, "h1", ["h1"])["server_subtrees"]}
    assert {"top/empty", "top/null"} <= paths


def test_every_key_the_worked_example_uses_passes_validation():
    # The guard against the whitelist itself being wrong: the schema's
    # own worked example has to resolve on every host.
    for host in EXAMPLE_HOSTS:
        assert resolve(EXAMPLE_TREE, host, EXAMPLE_HOSTS)


# -- the section list is the mount plan ------------------------------------


EXAMPLE_GROUPS = {
    "Whitfield Family & Friends": ["jd", "mw"],
    "Michael Whitfield Family": ["mw"],
    "Media Production": ["jd"],
}


def example_tree_remote_sections():
    """Every rclone remote named anywhere in the worked example, as
    section names -- the master rclone.conf a control node would really
    be filtering, rather than a hand-listed subset that goes stale the
    moment the example grows a remote."""
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "rclone.remote" and isinstance(value, str):
                    found.add(value.split(":", 1)[0])
                elif key == "rclone" and isinstance(value, dict):
                    remote = value.get("remote")
                    if isinstance(remote, str):
                        found.add(remote.split(":", 1)[0])
                walk(value)

    walk(EXAMPLE_TREE)
    return sorted(found)


@pytest.mark.parametrize("host", EXAMPLE_HOSTS)
def test_every_shipped_section_is_one_this_hosts_mounts_actually_use(host):
    # The invariant plan_remote_sections() exists to hold, checked
    # end-to-end on the worked example rather than on a hand-built
    # `resolved`: the sections in a host's rclone.conf are exactly the
    # sections its own mount units reference -- none missing (a mount
    # with no credentials), none spare (a credential with no mount).
    conf = "".join(
        f"[{name}]\ntype = smb\n\n" for name in example_tree_remote_sections()
    ) + "[nobodys-remote]\ntype = s3\n"
    resolved = resolve(EXAMPLE_TREE, host, EXAMPLE_HOSTS)
    out = filter_rclone_conf(conf, resolved, {}, EXAMPLE_GROUPS)

    shipped = set(re.findall(r"^\[(.+)\]$", out, re.MULTILINE))
    referenced = {
        e["remote"].split(":", 1)[0]
        for e in plan_mounts(resolved, EXAMPLE_GROUPS)
        if e["remote"]
    }
    assert shipped == referenced, host
    assert "nobodys-remote" not in shipped, "the filter copied a remote nothing uses"


def test_a_per_user_node_nobody_is_granted_ships_no_credential():
    # Reading the plan rather than `resolved`'s own scopes tightened this
    # case: a per-user node this host owns, whose group has no members
    # here, resolves to no mount at all -- so its remote's credentials
    # have no reason to be on the host, and now aren't.
    conf = "[shared-box]\ntype = smb\n"
    tree = {
        "top": {
            "host": "h1",
            "user-subdirs": {
                "vault": {
                    "access.group": "Nobody Here",
                    "rclone.remote": "shared-box:/vault",
                }
            },
        }
    }
    resolved = resolve(tree, "h1", ["h1"])

    assert filter_rclone_conf(conf, resolved, {}, {}).strip() == ""
    assert "[shared-box]" in filter_rclone_conf(
        conf, resolved, {}, {"Nobody Here": ["someone"]}
    )


def test_plan_remote_sections_separates_master_sections_from_peer_ones():
    # The two halves reach filter_rclone_conf() differently -- one is
    # copied out of the master conf verbatim, the other is synthesized --
    # so they come back apart rather than as one set of names.
    tree = {
        "own": {"host": "h1", "rclone.remote": "direct:/x"},
        "theirs": {"host": "h2", "rclone.remote": "not-mine:/y"},
    }
    master, peers = plan_remote_sections(resolve(tree, "h1", ["h1", "h2"]))

    assert master == {"direct"}
    assert set(peers) == {"peer-h2-theirs"}
    assert peers["peer-h2-theirs"]["owning_host"] == "h2"
    assert peers["peer-h2-theirs"]["path"] == "/srv/stortree/theirs"


# -- stale_unit_names ------------------------------------------------------


def test_stale_units_are_the_installed_ones_the_plan_no_longer_names():
    plan = [
        {"local_path": "top", "remote": "r:/", "slug": "top"},
        {"local_path": "top/u", "remote": None, "symlink_target": "top", "slug": "top-u"},
    ]
    containers = [{"local_path": "top/home", "slug": "top-home", "requires_slug": "top"}]
    installed = [
        "/etc/systemd/system/stortree-mount@top.service",
        "/etc/systemd/system/stortree-bind@top-u.service",
        "/etc/systemd/system/stortree-user-mount@top-home.service",
        "/etc/systemd/system/stortree-mount@gone.service",
        "/etc/systemd/system/stortree-bind@gone-u.service",
        "/etc/systemd/system/stortree-user-mount@gone-home.service",
    ]

    assert stale_unit_names(installed, plan, containers) == [
        "stortree-mount@gone.service",
        "stortree-bind@gone-u.service",
        "stortree-user-mount@gone-home.service",
    ]


def test_stale_units_is_empty_when_every_installed_unit_is_still_planned():
    plan = [{"local_path": "top", "remote": "r:/", "slug": "top"}]
    installed = ["/etc/systemd/system/stortree-mount@top.service"]
    assert stale_unit_names(installed, plan, []) == []


def test_stale_units_on_a_host_with_nothing_installed_yet():
    assert stale_unit_names([], [], []) == []


# -- the samba subpath is derived, not written -----------------------------


def test_a_shared_node_with_user_subdirs_gets_the_per_user_path():
    # The whole point of the derivation: `user-subdirs` means the node's
    # immediate children are per-user folders, so the share has to land
    # each connecting user in their own.
    tree = {
        "tree": {
            "host": "h1",
            "subdirs": {
                "home": {"samba": {}, "user-subdirs": {"fam": {"access.group": "F"}}}
            },
        }
    }
    (share,) = resolve(tree, "h1", ["h1"])["samba_shares"]
    assert share["subpath"] == PER_USER_PLACEHOLDER


def test_a_shared_node_without_user_subdirs_serves_itself():
    tree = {
        "tree": {
            "host": "h1",
            "subdirs": {"backups": {"samba": {}, "subdirs": {"old": {}}}},
        }
    }
    (share,) = resolve(tree, "h1", ["h1"])["samba_shares"]
    assert share["subpath"] is None


@pytest.mark.parametrize("block", [{}, None], ids=["empty", "bare"])
def test_an_empty_user_subdirs_block_is_still_a_per_user_share(block):
    # Presence, not truthiness -- the same reading `samba` itself gets.
    # `user-subdirs: {}` and a bare `user-subdirs:` (which YAML parses as
    # None) declare no substructure yet, but they do say the node has a
    # per-user level. Reading them as "not per-user" would mean emptying
    # a node's `user-subdirs` silently widens its share from one user's
    # own folder to the directory holding everyone's.
    tree = {"tree": {"host": "h1", "samba": {}, "user-subdirs": block}}
    (share,) = resolve(tree, "h1", ["h1"])["samba_shares"]
    assert share["subpath"] == PER_USER_PLACEHOLDER


def test_the_derived_subpath_reaches_the_worked_examples_home_share():
    # End-to-end on the shipped example, which no longer writes it: the
    # `home` share is per-user because `home` has `user-subdirs`.
    shares = {
        s["node_path"]: s
        for s in resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)[
            "samba_shares"
        ]
    }
    assert shares["tree/home"]["subpath"] == PER_USER_PLACEHOLDER


def test_writing_samba_subpath_says_it_is_derived_and_what_to_do():
    # It used to be a real key, so an existing config setting it isn't a
    # typo -- the generic unknown-key error would suggest correcting a
    # spelling that was already right. The answer is to delete the line.
    tree = {"top": {"host": "h1", "samba": {"subpath": "%U"}}}
    with pytest.raises(ValueError) as excinfo:
        resolve(tree, "h1", ["h1"])
    message = str(excinfo.value)
    assert "no longer written in config.yml" in message
    assert "user-subdirs" in message
    assert "Delete the line" in message


def test_writing_samba_subpath_is_rejected_even_where_it_matched_the_derivation():
    # Including the case where it would have derived to the same value:
    # a key that is sometimes accepted is a key that still has to be
    # understood, and half a schema is worse than none.
    tree = {
        "top": {
            "host": "h1",
            "samba": {"subpath": PER_USER_PLACEHOLDER},
            "user-subdirs": {"fam": {}},
        }
    }
    with pytest.raises(ValueError, match="no longer written"):
        resolve(tree, "h1", ["h1"])


# -- the two passes over a finished plan -----------------------------------
#
# plan_mounts() builds its entries in three stages and then makes two
# passes over the whole list. The passes are where everything that
# depends on more than one entry lives -- unit-name collisions, what
# nests inside what, which declared `requires` targets are really mounts
# here -- and they take a plain list, so they can be asked directly
# instead of through a tree that happens to produce the right shape.


def _entries(*specs):
    """Minimal plan entries: (local_path, remote) or (local_path, remote,
    requires)."""
    return [
        {
            "local_path": path,
            "remote": remote,
            "args": {},
            "access": {},
            "requires": list(requires[0]) if requires else [],
        }
        for path, remote, *requires in specs
    ]


def test_assign_plan_slugs_rejects_two_mounts_claiming_one_unit_name():
    entries = _entries(("top/leaf", "r:/"), ("top/leaf", "other:/"))
    with pytest.raises(ValueError, match="resolve to systemd unit slug"):
        _assign_plan_slugs(entries)


def test_assign_plan_slugs_ignores_a_clash_between_non_mounts():
    # A plain directory has no unit to collide over, so the check only
    # looks at entries with a remote.
    entries = _entries(("top/leaf", None), ("top/leaf", None))
    _assign_plan_slugs(entries)
    assert [e["slug"] for e in entries] == ["top-leaf", "top-leaf"]


def test_assign_plan_slugs_fills_in_the_fields_only_some_entries_set():
    (entry,) = _entries(("top", "r:/"))
    _assign_plan_slugs([entry])
    assert entry["symlink_target"] is None
    assert entry["peer"] is None


def test_relate_plan_entries_skips_a_plain_directory_ancestor():
    # `top/mid` is a directory, not a mount, so it has no unit for
    # `top/mid/leaf` to order against -- the dependency has to reach past
    # it to `top`, the nearest ancestor that really is a mount.
    entries = _entries(("top", "r:/"), ("top/mid", None), ("top/mid/leaf", "r2:/"))
    _assign_plan_slugs(entries)
    _relate_plan_entries(entries)
    by_path = {e["local_path"]: e for e in entries}
    assert by_path["top/mid/leaf"]["requires_slug"] == "top"
    assert by_path["top"]["requires_slug"] is None


def test_relate_plan_entries_flags_the_mount_that_is_nested_inside():
    # has_nested_children is requires_slug read backwards: whoever every
    # other entry pointed at.
    entries = _entries(("top", "r:/"), ("top/leaf", "r2:/"))
    _assign_plan_slugs(entries)
    _relate_plan_entries(entries)
    by_path = {e["local_path"]: e for e in entries}
    assert by_path["top"]["has_nested_children"]
    assert not by_path["top/leaf"]["has_nested_children"]


def test_relate_plan_entries_resolves_a_declared_requires_to_its_mount():
    entries = _entries(("cache", "r:/"), ("top", "r2:/", ["cache"]))
    _assign_plan_slugs(entries)
    _relate_plan_entries(entries)
    by_path = {e["local_path"]: e for e in entries}
    assert by_path["top"]["requires_mounts"] == [
        {"local_path": "cache", "slug": "cache"}
    ]
    assert "requires" not in by_path["top"]


def test_relate_plan_entries_drops_a_requires_target_that_is_not_a_mount_here():
    # Either a plain local directory this same apply creates before any
    # unit starts, or a mount another host owns that this one doesn't
    # peer -- neither has a unit to order against.
    entries = _entries(("cache", None), ("top", "r:/", ["cache", "elsewhere"]))
    _assign_plan_slugs(entries)
    _relate_plan_entries(entries)
    by_path = {e["local_path"]: e for e in entries}
    assert by_path["top"]["requires_mounts"] == []


# `stortree_samba_hosts` (roles/stortree_facts/defaults/main.yml) narrows
# which hosts export shares. Default is the whole fleet -- "Samba sharing
# is universal" -- so every other test in this file passes no list at all
# and must keep resolving exactly as before.


def _samba_opt_out_tree():
    # h1 owns the share; h2 owns a descendant of it, so any host that
    # exports the share has to peer-mount that descendant from h2.
    return {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {
                "share": {
                    "samba": {},
                    "subdirs": {"leaf": {"host": "h2", "rclone.remote": "r2:/leaf"}},
                }
            },
        }
    }


def test_samba_hosts_defaults_to_every_host_when_not_passed():
    # The universal default is the behaviour every other test relies on;
    # passing the full fleet explicitly must be indistinguishable from
    # passing nothing.
    tree = _samba_opt_out_tree()
    hosts = ["h1", "h2", "h3"]
    assert resolve(tree, "h3", hosts) == resolve(tree, "h3", hosts, samba_hosts=hosts)


def test_an_opted_out_host_exports_no_shares():
    tree = _samba_opt_out_tree()
    hosts = ["h1", "h2", "h3"]
    assert resolve(tree, "h3", hosts, samba_hosts=["h1", "h2"])["samba_shares"] == []
    # ...while a host still in the list exports the share as always.
    assert [
        s["node_path"]
        for s in resolve(tree, "h3", hosts, samba_hosts=hosts)["samba_shares"]
    ] == ["top/share"]


def test_an_opted_out_host_stops_peer_mounting_content_for_shares_it_no_longer_exports():
    # The half of the opt-out that actually costs something: the share
    # stanza is free, the peer mount behind it is an rclone process and a
    # VFS cache of someone else's bytes. Gating only the stortree_samba
    # role would leave this mount running to back a share that is no
    # longer exported.
    tree = _samba_opt_out_tree()
    hosts = ["h1", "h2", "h3"]
    on = resolve(tree, "h3", hosts, samba_hosts=hosts)
    assert ("h2", "top/share/leaf") in {
        (p["owning_host"], p["local_path"]) for p in on["peer_dependencies"]
    }

    off = resolve(tree, "h3", hosts, samba_hosts=["h1", "h2"])
    assert not any(p["owning_host"] == "h2" for p in off["peer_dependencies"])


def test_an_opted_out_host_keeps_its_own_client_mount_of_the_tree():
    # Opting out of *exporting* the tree says nothing about wanting it
    # locally -- h3's own client mount of `top` is unaffected.
    tree = _samba_opt_out_tree()
    hosts = ["h1", "h2", "h3"]
    off = resolve(tree, "h3", hosts, samba_hosts=["h1", "h2"])
    assert "top" in {m["local_path"] for m in off["client_mounts"]}
    assert ("h1", "top") in {
        (p["owning_host"], p["local_path"]) for p in off["peer_dependencies"]
    }


def test_an_opted_out_host_is_not_served_peer_trust_for_the_shares_it_dropped():
    # The serving side's mirror, and the reason this is one fleet-level
    # list rather than a per-host boolean: h2 owns `leaf` and must reach
    # the same conclusion about h3 that h3 reaches about itself, or
    # stortree_peer_trust grants SSH access for a mount that never
    # happens.
    tree = _samba_opt_out_tree()
    hosts = ["h1", "h2", "h3"]
    served_on = resolve(tree, "h2", hosts, samba_hosts=hosts)["peer_served_by"]
    assert {p["serving_host"] for p in served_on} == {"h1", "h3"}

    served_off = resolve(tree, "h2", hosts, samba_hosts=["h1", "h2"])["peer_served_by"]
    assert {p["serving_host"] for p in served_off} == {"h1"}
