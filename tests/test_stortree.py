import re
from pathlib import Path

import pytest
import yaml

from filter_plugins.stortree import (
    _assign_plan_slugs,
    samba_write_tokens,
    _check_slug_collisions,
    _layer_plan_entries,
    _plan_user_containers,
    ownership_mismatch,
    _relate_plan_entries,
    DEFAULT_ACCESS_PERMISSIONS,
    PER_USER_PLACEHOLDER,
    _normalize_access,
    _remote_dir,
    _slug,
    access_grant_usernames,
    access_group,
    access_mode,
    bindfs_perms,
    access_owner,
    apt_installable,
    filter_rclone_conf,
    group_gids_from_getent,
    group_members_from_getent,
    merged_getent_results,
    metrics_listeners,
    metrics_ports,
    mount_unit_names,
    mounted_transport_slugs,
    needed_groups,
    needed_users,
    per_user_mount_path,
    plan_mounts,
    plan_remote_sections,
    resolve,
    stale_unit_names,
    samba_access_tokens,
    user_uids_from_getent,
)

def relate(entries):
    """Run the three passes plan_mounts() runs, so a unit test of the
    relation pass exercises it the way the real pipeline does."""
    _assign_plan_slugs(entries)
    transports = _layer_plan_entries(entries)
    _relate_plan_entries(entries, transports)
    _check_slug_collisions(transports + entries)
    return transports


def plan_index(plan):
    """The plan keyed by path, transports excluded.

    A transport and the presentation above it deliberately share a
    `local_path` -- they are the same directory in the two roots -- so a
    plain {local_path: entry} dict silently drops one of them. Tests that
    care about layer 1 use transports() instead."""
    return {e["local_path"]: e for e in plan if e["kind"] != "transport"}


def transports(plan):
    """{local_path: entry} for layer 1 only."""
    return {e["local_path"]: e for e in plan if e["kind"] == "transport"}

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
    # peer-defaults.rclone: false keeps it off every host that isn't
    # explicitly listed in its `peers:` -- alpha isn't, so no subtree
    # mount at all
    assert r["subtree_mounts"] == []


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

    # bravo also has a `peers:` entry on `tree` -- both lists at once
    # (spec.md §1). .gcs-cache is disabled by default (peer-defaults.
    # rclone: false) and bravo isn't in its `peers:`, so it gets none.
    assert len(r["subtree_mounts"]) == 1
    mount = r["subtree_mounts"][0]
    assert mount["local_path"] == "tree"
    # the subtree mount is peer-sourced from alpha (tree's own owner),
    # not a direct mount of tree's own rclone.remote -- see the matching
    # peer_dependencies entry below
    assert mount["remote"] == "peer-storage-node-alpha-tree:/srv/stortree/tree"
    # peer-defaults merged with peers.storage-node-bravo overrides
    assert mount["args"]["vfs-cache-mode"] == "full"  # from peer-defaults
    assert mount["args"]["vfs-cache-max-size"] == "5G"  # bravo's own override
    assert mount["args"]["cache-dir"] == "/srv/stortree/.bravo-cache"


def test_gadget_owns_nothing_but_gets_a_subtree_mount_and_full_samba_share():
    r = resolve(EXAMPLE_TREE, "some-storage-gadget", EXAMPLE_HOSTS)
    assert r["server_subtrees"] == []
    assert len(r["subtree_mounts"]) == 1
    assert r["subtree_mounts"][0]["local_path"] == "tree"
    assert r["subtree_mounts"][0]["args"]["vfs-cache-max-size"] == "20G"

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
    # .gcs-cache is alpha's own too, but peer-defaults.rclone: false
    # keeps it out of peer_served_by for hosts that aren't in its
    # `peers:` (neither bravo nor gadget is)
    assert not any(p["local_path"] == ".gcs-cache" for p in r["peer_served_by"])


def test_every_non_owning_host_peer_sources_tree_from_its_owner():
    # A subtree mount of a top-level subtree is never a direct mount of
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
            r["subtree_mounts"][0]["remote"]
            == "peer-storage-node-alpha-tree:/srv/stortree/tree"
        )

    # alpha itself never peer-sources its own subtree mount of tree -- it
    # owns tree outright (test_alpha_owns_everything_not_overridden
    # already asserts subtree_mounts == [] for it)
    alpha = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    assert not any(p["local_path"] == "tree" for p in alpha["peer_dependencies"])

    # ...and alpha's peer_served_by reflects serving tree to every other
    # host, in addition to whatever samba pieces it serves
    tree_served = [p for p in alpha["peer_served_by"] if p["local_path"] == "tree"]
    assert {p["serving_host"] for p in tree_served} == {
        "storage-node-bravo",
        "some-storage-gadget",
    }


def test_a_peer_mounts_a_subtree_whose_owner_has_no_remote_of_its_own():
    # A non-owning host mounts from the owning host whether or not that
    # host's copy is remote-backed. A peer mount is sftp to the owner's
    # *filesystem path*, which exists just as much when the content
    # simply lives on its disk, or arrives there over a mount stortree
    # knows nothing about, as when rclone puts it there.
    #
    # This used to be gated on the node's own `rclone.remote`, which left
    # the peer with an empty local directory where the subtree should
    # be -- and disagreed with _samba_peer_dependencies(), which has
    # always peered a descendant regardless.
    tree = {"top": {"host": "h1", "subdirs": {"plain": {"host": "h2"}}}}
    r = resolve(tree, "h2", ["h1", "h2"])
    assert r["subtree_mounts"] == [
        {
            "local_path": "top",
            "remote": "peer-h1-top:/srv/stortree/top",
            "args": {},
            "access": {},
            "requires": [],
        }
    ]
    assert [p["local_path"] for p in r["peer_dependencies"] if p["local_path"] == "top"] == [
        "top"
    ]


def test_a_peer_still_gets_no_mount_when_the_subtree_is_opted_out():
    # The way to keep a subtree off its non-owning hosts is the explicit
    # opt-out, not the absence of a remote.
    tree = {
        "top": {
            "host": "h1",
            "peer-defaults": {"rclone": False},
            "subdirs": {"plain": {"host": "h2"}},
        }
    }
    r = resolve(tree, "h2", ["h1", "h2"])
    assert not any(m["remote"] for m in r["subtree_mounts"])
    assert not any(p["local_path"] == "top" for p in r["peer_dependencies"])


# -- peer-defaults.rclone / peers.<host>.rclone opt-out --------------


def test_peer_defaults_rclone_false_keeps_a_subtree_local_by_default():
    tree = {
        "private": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peer-defaults": {"rclone": False},
        }
    }
    for hostname in ("h2", "h3"):
        r = resolve(tree, hostname, ["h1", "h2", "h3"])
        assert r["subtree_mounts"] == []
        assert not any(p["local_path"] == "private" for p in r["peer_dependencies"])
    # the owner is unaffected either way -- it self-mounts via
    # server_subtrees, never through the subtree-mount/gating path at all
    owner = resolve(tree, "h1", ["h1", "h2", "h3"])
    assert paths(owner["server_subtrees"]) == {"private"}


def test_peers_override_acts_as_an_allow_list_when_defaults_are_false():
    tree = {
        "private": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peer-defaults": {"rclone": False},
            "peers": {"h2": {"rclone": {"args": {"vfs-cache-max-size": "1G"}}}},
        }
    }
    allowed = resolve(tree, "h2", ["h1", "h2", "h3"])
    assert allowed["subtree_mounts"] == [
        {
            "local_path": "private",
            "remote": "peer-h1-private:/srv/stortree/private",
            "args": {"vfs-cache-max-size": "1G"},
            "access": {},
            "requires": [],
        }
    ]

    denied = resolve(tree, "h3", ["h1", "h2", "h3"])
    assert denied["subtree_mounts"] == []
    assert not any(p["local_path"] == "private" for p in denied["peer_dependencies"])


def test_peers_override_acts_as_a_deny_list_when_defaults_are_enabled():
    tree = {
        "shared": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peers": {"h2": {"rclone": False}},
        }
    }
    denied = resolve(tree, "h2", ["h1", "h2", "h3"])
    assert denied["subtree_mounts"] == []

    allowed = resolve(tree, "h3", ["h1", "h2", "h3"])
    assert len(allowed["subtree_mounts"]) == 1
    assert allowed["subtree_mounts"][0]["local_path"] == "shared"


def test_bravo_cache_and_gcs_cache_reach_no_other_host():
    # the two independent per-host VFS-cache subtrees in the worked
    # example are exactly what peer-defaults.rclone: false exists for
    # -- confirm neither ever shows up for a host that doesn't own it
    for hostname in EXAMPLE_HOSTS:
        r = resolve(EXAMPLE_TREE, hostname, EXAMPLE_HOSTS)
        mounted_paths = {m["local_path"] for m in r["subtree_mounts"]}
        owned_paths = paths(r["server_subtrees"])
        for cache_path, owner in ((".bravo-cache", "storage-node-bravo"), (".gcs-cache", "storage-node-alpha")):
            if hostname == owner:
                assert cache_path in owned_paths
            else:
                assert cache_path not in mounted_paths
                assert not any(
                    p["local_path"] == cache_path for p in r["peer_dependencies"]
                )


# -- nested peers / peer-defaults ------------------------------------


def test_a_nested_peer_block_governs_only_its_own_branch():
    # `peers`/`peer-defaults` written on a subdirectory, not on the
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
                            "peers": {"h2": {"rclone": False}},
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
    # Two axes at once (_peer_policy): `peers.<host>` beats
    # `peer-defaults` within a node, a deeper node beats a shallower
    # one, and `args` accumulate down the whole chain instead of the
    # nearest block replacing them.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peer-defaults": {"rclone.args": {"dir-cache-time": "5m", "x": "top"}},
            "subdirs": {
                "share": {
                    "samba": None,
                    "peer-defaults": {"rclone.args": {"x": "share"}},
                    "peers": {"h2": {"rclone.args": {"vfs-cache-mode": "full"}}},
                    "subdirs": {
                        "deep": {
                            "host": "h3",
                            "rclone.remote": "r3:/deep",
                            "peers": {"h2": {"rclone.args": {"x": "deep"}}},
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
        "vfs-cache-mode": "full",  # from an intermediate node's peers entry
        "x": "deep",  # deepest, most specific block wins the conflict
    }


def test_a_subdirectory_can_be_opted_back_in_under_an_opted_out_subtree():
    # The allow-list idiom, one level down: the subtree is off every
    # non-owning host, and a single node inside it is handed to one
    # peer on its own. It gets a subtree mount at its *own* path,
    # sourced from its own resolved owner -- there's no ancestor mount
    # left to reach it through.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peer-defaults": {"rclone": False},
            "subdirs": {
                "pub": {
                    "rclone.remote": "r1:/pub",
                    "peers": {"h2": {"rclone": True}},
                },
                "priv": {"rclone.remote": "r1:/priv"},
            },
        }
    }
    allowed = resolve(tree, "h2", ["h1", "h2", "h3"])
    assert [m["local_path"] for m in allowed["subtree_mounts"]] == ["top/pub"]
    assert allowed["subtree_mounts"][0]["remote"] == (
        "peer-h1-top-pub:/srv/stortree/top/pub"
    )
    # the mount is real all the way down, not just an entry in resolve()
    assert "top/pub" in {e["local_path"] for e in plan_mounts(allowed)}

    denied = resolve(tree, "h3", ["h1", "h2", "h3"])
    assert denied["subtree_mounts"] == []

    # the owner serves exactly that one path, to exactly that one host
    served = resolve(tree, "h1", ["h1", "h2", "h3"])["peer_served_by"]
    assert [(p["serving_host"], p["local_path"]) for p in served] == [("h2", "top/pub")]


def test_an_opted_in_subdirectory_is_not_mounted_twice():
    # A node can be reached two ways at once -- a Samba descendant this
    # host peer-sources *and* the shallowest enabled node of its own
    # branch. Both are the same mount of the same path from the same
    # host, and planning it twice is a unit-slug clash, so the subtree
    # mount defers to the Samba peer dependency (which additionally
    # carries the node's own `access`).
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peer-defaults": {"rclone": False},
            "subdirs": {
                "share": {
                    "samba": None,
                    "subdirs": {
                        "a": {
                            "host": "h2",
                            "rclone.remote": "r2:/a",
                            "access.group": "Ops",
                            "peer-defaults": {"rclone": True},
                        }
                    },
                }
            },
        }
    }
    r = resolve(tree, "h3", ["h1", "h2", "h3"])
    assert r["subtree_mounts"] == []
    assert [p["local_path"] for p in r["peer_dependencies"]] == ["top/share/a"]
    # One transport and one presentation for the same path -- the two
    # layers of a single mount, not the same mount planned twice.
    assert [e["local_path"] for e in plan_mounts(r) if e["kind"] == "mount"] == [
        "top/share/a"
    ]
    assert plan_index(plan_mounts(r))["top/share/a"]["access"]["group"] == "Ops"


def test_a_top_level_subtree_that_shares_itself_is_not_mounted_twice():
    # The same double-source, reachable with no nested peer block at
    # all: a remote-backed top-level subtree carrying `samba:` with no
    # children is its own Samba descendant (_has_own_content()) as well
    # as its own subtree-mount target.
    tree = {"top": {"host": "h1", "rclone.remote": "r1:/", "samba": None}}
    r = resolve(tree, "h2", ["h1", "h2"])
    assert r["subtree_mounts"] == []
    assert [e["local_path"] for e in plan_mounts(r) if e["kind"] == "mount"] == ["top"]
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
            "subdirs": {"inner": {"peer-defaults": {"rclone": False}}},
        }
    }
    r = resolve(tree, "h2", ["h1", "h2"])
    assert [m["local_path"] for m in r["subtree_mounts"]] == ["top"]


def test_a_per_user_node_is_never_a_subtree_mount_target():
    # A `user-subdirs` descendant's path is still %U-templated and fans
    # out into one mount per granted user; a subtree_mounts entry
    # describes a single mount and has no expansion step, so the descent
    # stops there. Nothing is silently mounted at a literal "%U" path.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peer-defaults": {"rclone": False},
            "subdirs": {
                "home": {
                    "user-subdirs": {
                        "docs": {
                            "host": "h3",
                            "rclone.remote": "r3:/docs",
                            "access.group": "Staff",
                            "peer-defaults": {"rclone": True},
                        }
                    }
                }
            },
        }
    }
    r = resolve(tree, "h2", ["h1", "h2", "h3"])
    assert r["subtree_mounts"] == []
    assert not any(PER_USER_PLACEHOLDER in e["local_path"] for e in plan_mounts(r))


def test_the_descent_stops_at_a_subtree_this_host_serves_itself():
    # A node this host owns is served from its own local tree, never
    # mounted from a peer -- the same rule the top-level loop always
    # applied, now reached one level down.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peer-defaults": {"rclone": False},
            "subdirs": {"mine": {"host": "h2", "rclone.remote": "r2:/mine"}},
        }
    }
    r = resolve(tree, "h2", ["h1", "h2"])
    assert r["subtree_mounts"] == []
    assert paths(r["server_subtrees"]) == {"top/mine"}


def test_a_nested_peer_block_leaves_the_worked_example_alone():
    # The whole feature is additive: a tree that only ever writes
    # `peer-defaults`/`peers` on its top-level subtrees resolves
    # exactly as it did when that was the only level read at all.
    for hostname in EXAMPLE_HOSTS:
        r = resolve(EXAMPLE_TREE, hostname, EXAMPLE_HOSTS)
        for mount in r["subtree_mounts"]:
            assert "/" not in mount["local_path"]


# -- access in a peer block --------------------------------------------


def test_peer_defaults_access_grants_a_subtree_mount_its_own_ownership():
    # A top-level subtree mount carries no `access` at all by default --
    # the owning host is what enforces the node's own grant. A peer
    # block is how this host's own copy gets one, which is what turns
    # into rclone's --uid/--gid/--dir-perms for that mount.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peer-defaults": {"access": {"group": "Readers", "permissions": "rx"}},
        }
    }
    r = resolve(tree, "h2", ["h1", "h2"])
    assert r["subtree_mounts"][0]["access"] == {
        "group": "Readers",
        "permissions": "rx",
        "permissions_explicit": True,
    }
    # it has to reach the flat plan and the getent lookups too, or the
    # unit template has no gid to render
    assert plan_index(plan_mounts(r))["top"]["access"]["group"] == "Readers"
    assert needed_groups(r) == ["Readers"]


def test_a_peer_access_merges_over_the_nodes_own_grant():
    # One key at a time (_peer_policy, _peer_grant), the same rule
    # an ancestor and its descendant follow: `peers.<host>` wins the
    # keys it sets, `peer-defaults` the ones only it sets, and the
    # node's own grant supplies the rest. Overriding the owner used to
    # drop the group with it, which is how a grant meant to hold
    # fleet-wide stopped at whichever host also named a local principal.
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
                            "peer-defaults": {"access.group": "Readers"},
                            "peers": {"h2": {"access": {"owner": "jd"}}},
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
    # h2 gets its own `owner`, `peer-defaults`' group (the node's
    # `Owners` overridden by the nearer block that set that key), and
    # the node's own explicit level, which nothing in the chain touched.
    assert leaf_access("h2") == {
        "owner": "jd",
        "group": "Readers",
        "permissions": "rwx",
        "permissions_explicit": True,
    }
    # h1 has no `peers` entry of its own, so it stops at
    # peer-defaults over the node.
    assert leaf_access("h1") == {
        "group": "Readers",
        "permissions": "rwx",
        "permissions_explicit": True,
    }
    # the owning host is untouched: `peers`/`peer-defaults` only ever
    # describe a host that doesn't own the node
    owner = resolve(tree, "h3", ["h1", "h2", "h3"])
    assert by_path(owner["server_subtrees"], "top/share/leaf")["access"] == {
        "group": "Owners",
        "permissions": "rwx",
        "permissions_explicit": True,
    }


def test_an_empty_peer_access_drops_the_nodes_grant_on_that_peer():
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
                            "peers": {"h2": {"access": None}},
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
    # Marked as a drop rather than left blank: h2 holds a mounted copy,
    # where "no grant" is not the absence of one but the plain default,
    # and something has to present it (_dropped_access()). Everything
    # that reads ownership out of a grant still reads the default.
    assert leaf(dropped)["access"] == {"reset": True}
    assert access_owner(leaf(dropped)["access"], "stortree") == "stortree"
    assert access_group(leaf(dropped)["access"], "stortree") == "stortree"
    assert access_mode(leaf(dropped)["access"]) == "0751"
    assert needed_groups(dropped) == []


def test_a_peer_access_owner_reaches_needed_users():
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peers": {"h2": {"access.owner": "jd"}},
        }
    }
    assert needed_users(resolve(tree, "h2", ["h1", "h2"])) == ["jd"]


def test_peer_block_rejects_an_unknown_access_key():
    tree = {
        "top": {
            "host": "h1",
            "peers": {"h2": {"access": {"grup": "Ops"}}},
        }
    }
    with pytest.raises(ValueError, match=r"unknown `peers\.h2\.access` key 'grup'"):
        resolve(tree, "h2", ["h1", "h2"])


def test_peer_defaults_rejects_an_unknown_access_key():
    tree = {"top": {"host": "h1", "peer-defaults": {"access": {"perms": "rx"}}}}
    with pytest.raises(
        ValueError, match=r"unknown `peer-defaults\.access` key 'perms'"
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


def test_peer_only_host_still_resolves_peer_dependencies():
    r = resolve(EXAMPLE_TREE, "some-storage-gadget", EXAMPLE_HOSTS)
    assert r["server_subtrees"] == []
    assert len(r["peer_dependencies"]) > 0


def test_host_unnamed_anywhere_in_config_resolves_like_any_other():
    hosts = EXAMPLE_HOSTS + ["storage-node-charlie"]
    charlie = resolve(EXAMPLE_TREE, "storage-node-charlie", hosts)
    gadget = resolve(EXAMPLE_TREE, "some-storage-gadget", hosts)

    assert charlie["server_subtrees"] == []
    assert len(charlie["subtree_mounts"]) == 1
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
        "subtree_mounts": [],
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
        "subtree_mounts": [],
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
        "subtree_mounts": [],
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
    # the only place its per-user access grants show up. Plus `home`'s own
    # `userdir-groups`, which is in no grant anywhere: gadget holds that
    # path through its mount of `tree` and serves the share, so it has to
    # resolve the household's home directories like every other host.
    r = resolve(EXAMPLE_TREE, "some-storage-gadget", EXAMPLE_HOSTS)
    assert needed_groups(r) == [
        "Media Production",
        "Michael Whitfield Family",
        "Whitfield Family & Friends",
        "Whitfield Household",
    ]

    # bravo owns some per-user pieces itself (server_subtrees: mw-fam,
    # whitfield-media) and peer depends on the rest (alpha's sys-configs,
    # a user-only grant with no group; and media-prod, group-granted) --
    # same combined group set as gadget's, just split across both sources,
    # and one more on top: the example gives bravo alone a
    # `peers.storage-node-bravo.userdir-groups`, which adds to `home`'s
    # own list rather than replacing it.
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    assert needed_groups(r) == [
        "Bravo Operators",
        "Media Production",
        "Michael Whitfield Family",
        "Whitfield Family & Friends",
        "Whitfield Household",
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
    # presentation mount's -u (_plan_user_containers(), stortree_mounts).
    r = resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS)
    group_members = {
        "Whitfield Family & Friends": ["mike", "jd"],
        "Michael Whitfield Family": ["dana"],
        "Media Production": ["alex"],
    }
    assert needed_users(r, group_members) == ["alex", "dana", "jd", "mike"]


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


def test_access_mode_per_class_permissions_give_owner_and_group_their_own():
    # The thing one shared level could not say: the owner writes, the
    # group reads. Plain Unix mode bits, no ACL involved -- the
    # single-object restriction on `access` is about naming two *groups*,
    # which is what a mode really can't express.
    access = _normalize_access(
        {"owner": "jd", "group": "g", "permissions": {"owner": "rwx", "group": "r-x"}}
    )
    assert access_mode(access) == "0751"
    assert bindfs_perms(access) == "0640,ugo+X"


def test_access_mode_a_class_left_out_of_the_mapping_keeps_its_default():
    # A mapping settles the classes it names and nothing else, which is
    # what makes it safe to write one about the group alone: `other`
    # keeps the traversal bit a deeper grant needs unless the mapping
    # says otherwise, where a scalar level would have taken it away as a
    # side effect (test above).
    group_only = _normalize_access({"group": "g", "permissions": {"group": "r-x"}})
    assert access_mode(group_only) == "0751"

    spelled_out = _normalize_access(
        {"group": "g", "permissions": {"group": "r-x", "other": "---"}}
    )
    assert access_mode(spelled_out) == "0750"


def test_access_mode_a_mapping_can_grant_others_more_than_traversal():
    # `other` is a class like any other in this form -- the "even the
    # others if need be" case, for a node meant to be readable by
    # anyone who can reach it.
    access = _normalize_access({"group": "g", "permissions": {"other": "r-x"}})
    assert access_mode(access) == "0775"


def test_bindfs_perms_puts_the_execute_bits_on_directories_only():
    # bindfs takes one -p spec for files and directories both, where
    # rclone took --dir-perms and --file-perms separately. The octal is
    # therefore the *file* mode and capital X puts the execute bits back
    # on directories alone. Handing access_mode() to -p directly would
    # mark every regular file in the subtree executable.
    assert bindfs_perms({}) == "0640,ugo+X"  # access_mode 0751
    assert bindfs_perms({"owner": "jd"}) == "0600,uo+X"  # access_mode 0701


def test_bindfs_perms_preserves_the_public_traverse_bit():
    # The one access_mode() adds so a descendant grant owned by someone
    # else stays reachable. It matters more under a presentation mount
    # than it did under rclone: the mount above such a descendant now
    # presents a real owner instead of a uniform stortree:stortree, so
    # this bit is the only thing keeping the descendant reachable.
    assert bindfs_perms(_normalize_access({"owner": "jd"})).endswith("o+X")


def test_bindfs_perms_omits_the_x_clause_when_no_class_has_execute():
    # An explicit `permissions: rw` grant opts out of execute entirely
    # (access_mode 0660). There is then nothing for +X to add, and
    # emitting a bare "+X" would be a chmod syntax error.
    access = {"owner": "jd", "group": "g", "permissions": "rw", "permissions_explicit": True}
    assert bindfs_perms(access) == "0660"


def test_bindfs_perms_round_trips_every_mode_access_mode_can_produce():
    # Each spec below was checked against bindfs 1.14.7 itself, mounting
    # a real tree and stat-ing the result; the comment is the observed
    # directory mode, which is what access_mode() meant in the first
    # place.
    assert bindfs_perms({}) == "0640,ugo+X"  # dir 751
    assert bindfs_perms({"owner": "jd"}) == "0600,uo+X"  # dir 701
    assert bindfs_perms({"group": "g"}) == "0660,ugo+X"  # dir 771
    explicit = {"owner": "jd", "permissions": "rwx", "permissions_explicit": True}
    assert bindfs_perms(explicit) == "0600,u+X"  # dir 700


def test_samba_access_tokens_quotes_names_with_spaces():
    access = [{"group": "Michael Whitfield Family", "permissions": "rwx"}]
    assert samba_access_tokens(access) == ['"@Michael Whitfield Family"']


def test_samba_access_tokens_one_grant_with_both_owner_and_group_yields_two_tokens():
    access = [{"owner": "jd", "group": "IT Admins", "permissions": "rwx"}]
    assert samba_access_tokens(access) == ['"@IT Admins"', '"jd"']


def test_samba_access_tokens_name_each_principal_once():
    # The ordinary shape now that `access` inherits: a share whose
    # descendants all carry the group written above them, inside grants
    # that differ elsewhere and so survive the union's own dedupe.
    access = [
        {"group": "IT Admins", "permissions": "rwx"},
        {"group": "IT Admins", "owner": "svc-one", "permissions": "rwx"},
        {"group": "IT Admins", "owner": "svc-two", "permissions": "rwx"},
    ]
    assert samba_access_tokens(access) == [
        '"@IT Admins"',
        '"svc-one"',
        '"svc-two"',
    ]
    assert samba_write_tokens(access) == ['"@IT Admins"', '"svc-one"', '"svc-two"']


def test_samba_access_tokens_include_self_prepends_percent_u():
    assert samba_access_tokens([], include_self=True) == ['"%U"']
    access = [{"group": "g", "permissions": "rwx"}]
    assert samba_access_tokens(access, include_self=True) == ['"%U"', '"@g"']


def test_samba_write_tokens_follow_each_principals_own_level():
    # The reason `write list` is computed per token rather than by
    # filtering whole grants: these two principals are on one grant and
    # only one of them may write.
    access = [
        _normalize_access(
            {
                "owner": "jd",
                "group": "IT Admins",
                "permissions": {"owner": "rwx", "group": "r-x"},
            }
        )
    ]
    assert samba_access_tokens(access) == ['"@IT Admins"', '"jd"']
    assert samba_write_tokens(access) == ['"jd"']


def test_samba_write_tokens_keep_the_owner_slot_of_a_group_only_grant():
    # A group-only grant leaves `stortree` owning the path with full
    # control (access_mode()), and there is no owner token to emit for
    # it -- but the group's own level still decides its token, and %U
    # still rides along on a per-user share.
    read_only = [_normalize_access({"group": "g", "permissions": "r-x"})]
    assert samba_access_tokens(read_only) == ['"@g"']
    assert samba_write_tokens(read_only) == []
    assert samba_write_tokens(read_only, include_self=True) == ['"%U"']

    writable = [_normalize_access({"group": "g"})]
    assert samba_write_tokens(writable) == ['"@g"']


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
    by_local_path = plan_index(plan)

    # per-user node mw-fam (access.group: Michael Whitfield Family) --
    # `group`-only, so every member's own folder is a symlink back to one
    # shared mount (per_user_mount_path()), not a mount of its own
    assert "tree/home/mike/mw-fam" in by_local_path
    assert by_local_path["tree/home/mike/mw-fam"]["remote"] is None
    assert by_local_path["tree/home/mike/mw-fam"]["symlink_target"] == "tree/home/.mounts/mw-fam"
    assert "tree/home/%U/mw-fam" not in by_local_path
    # The remote belongs to layer 1 now; the presentation reads a
    # local path under the remotes root.
    assert transports(plan)["tree/home/.mounts/mw-fam"]["remote"] == "some-remote:/fam"
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
    assert (
        transports(plan)["tree/home/.mounts/whitfield-media"]["remote"]
        == "some-remote:/media"
    )
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

    # bravo's subtree mount of `tree` nests everything under it that's
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
        e
        for e in plan
        if e["local_path"] == "tree/home/.mounts/whitfield-media"
        and e["kind"] == "mount"
    ]
    assert len(real_mounts) == 1
    real_mount = real_mounts[0]
    assert transports(plan)[real_mount["local_path"]]["remote"] == "some-remote:/media"
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
    by_local_path = plan_index(plan)

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
    by_local_path = plan_index(plan)

    # alpha-owned, per-user, no rclone.remote of its own -- still real
    # content sourced live from alpha's own filesystem at that exact
    # path, since it's a leaf with real per-user content, not a
    # structural container. It needs no transport of its own: gadget's
    # peer mount of `tree`, from the same host, already reaches that
    # path (_transport_covers()), so what it gets is the presentation
    # that applies its grant, reading the covering mount from inside.
    sys_configs = by_local_path["tree/home/jd/sys-configs"]
    assert sys_configs["kind"] == "mount"
    assert "tree/home/jd/sys-configs" not in transports(plan)

    # bravo-owned, per-user, `group`-only -- gadget peer-sources exactly
    # one real mount, at bravo's own shared path (bravo's own plan_mounts()
    # run resolved whitfield-media to that same path first, per
    # per_user_mount_path() -- nothing ever lives at a per-user path on
    # bravo's disk for a group-only grant, so that's the only real path a
    # peer could source it from), not relayed through alpha
    assert "tree/home/mike/mw-fam" not in {
        p for p, e in transports(plan).items() if "alpha" in e["remote"]
    }
    whitfield_mount = transports(plan)["tree/home/.mounts/whitfield-media"]
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
    # own subtree mount of `tree` (also peer-sourced, from alpha) -- not
    # under "tree/home", which was never a mount to nest under in the
    # first place
    tree_slug = by_local_path["tree"]["slug"]
    # Both layers nest the same way, each against its own layer: a
    # transport that survives inside gadget's transport of `tree`, the
    # presentations inside the presentation of `tree`. sys-configs has
    # no layer-1 entry left to nest, so what says where it reads from is
    # the transport its presentation points at.
    assert sys_configs["transport_slug"] == tree_slug
    assert whitfield_mount["parent_transport"] == tree_slug
    # The presentations order against the *deepest* presentation above
    # them, which for a per-user leaf is now its own container rather
    # than the top-level subtree -- the container is a presentation in
    # its own right, and it is what puts this mountpoint on screen.
    assert by_local_path["tree/home/jd/sys-configs"]["requires_slug"] == "tree-home-jd"
    assert (
        by_local_path["tree/home/.mounts/whitfield-media"]["requires_slug"] == tree_slug
    )


def test_plan_mounts_collapses_a_peer_transport_the_mount_above_it_covers():
    # gadget peer-mounts the whole of `tree` from alpha, and `sys-configs`
    # and `media-prod` are alpha's too -- the same account, the same
    # bytes, at a path the outer mount already reaches. One rclone
    # process serves all three, so only the outer one is planned.
    #
    # The presentations stay: they are what applies each node's grant,
    # and they read the covering mount from the inside (transport_slug).
    # Two sftp sessions to one host, two VFS caches and two
    # `--vfs-cache-max-size` budgets over one subtree is what this saves.
    group_members = {
        "Whitfield Family & Friends": ["jd", "mw"],
        "Michael Whitfield Family": ["mw"],
        "Media Production": ["jd"],
    }
    r = resolve(EXAMPLE_TREE, "some-storage-gadget", EXAMPLE_HOSTS)
    plan = plan_mounts(r, group_members)
    by_local_path = plan_index(plan)
    layer1 = transports(plan)

    assert "tree" in layer1
    for collapsed in ("tree/home/jd/sys-configs", "tree/home/.mounts/media-prod"):
        assert collapsed not in layer1
        assert by_local_path[collapsed]["kind"] == "mount"
        assert by_local_path[collapsed]["transport_slug"] == layer1["tree"]["slug"]

    # ...and the credentials go with it. rclone.conf is derived from this
    # same plan (plan_remote_sections()), so a transport that is never
    # planned cannot leave a section behind for a mount nobody makes.
    _master, peers = plan_remote_sections(r, group_members)
    assert "peer-storage-node-alpha-tree" in peers
    assert "peer-storage-node-alpha-tree-home-jd-sys-configs" not in peers


def test_plan_mounts_keeps_a_nested_peer_transport_of_a_different_host():
    # The mesh is the reason the collapse has to check the owning host
    # and not just the path: gadget's mount of `tree` comes from alpha,
    # but whitfield-media and mw-fam inside it are bravo's. Reading them
    # through alpha's copy would relay one host's content through
    # another -- exactly what _subtree_mount_entries() sources directly to
    # avoid -- and alpha may not even mount them.
    group_members = {
        "Whitfield Family & Friends": ["jd", "mw"],
        "Michael Whitfield Family": ["mw"],
        "Media Production": ["jd"],
    }
    plan = plan_mounts(
        resolve(EXAMPLE_TREE, "some-storage-gadget", EXAMPLE_HOSTS), group_members
    )
    layer1 = transports(plan)
    for kept in ("tree/home/.mounts/whitfield-media", "tree/home/.mounts/mw-fam"):
        assert "storage-node-bravo" in layer1[kept]["remote"]
        assert layer1[kept]["parent_transport"] == layer1["tree"]["slug"]


def test_plan_mounts_keeps_a_nested_peer_inside_a_transport_that_is_not_a_peer():
    # alpha's own `tree` is a third-party remote (the Storage Box), and
    # the two nodes bravo owns are nested inside it. The paths nest, but
    # the Storage Box's own `tree/home/.mounts/mw-fam` is whatever alpha
    # last wrote there, not bravo's live content -- there is no
    # relationship between the two to collapse across, only a shared
    # prefix.
    group_members = {
        "Whitfield Family & Friends": ["jd", "mw"],
        "Michael Whitfield Family": ["mw"],
        "Media Production": ["jd"],
    }
    plan = plan_mounts(
        resolve(EXAMPLE_TREE, "storage-node-alpha", EXAMPLE_HOSTS), group_members
    )
    layer1 = transports(plan)
    assert layer1["tree"]["peer"] is None
    for kept in ("tree/home/.mounts/whitfield-media", "tree/home/.mounts/mw-fam"):
        assert "storage-node-bravo" in layer1[kept]["remote"]


def peer_dependency(local_path, **overrides):
    """A resolve()-shaped peer_dependency, for the collapse cases the
    worked example has no natural instance of."""
    return {
        "owning_host": "storage-node-alpha",
        "local_path": local_path,
        "remote_path": local_path,
        "samba_node": "top",
        "per_user": False,
        "access": {},
        "args": {},
        "requires": [],
        **overrides,
    }


def test_plan_mounts_keeps_a_nested_peer_transport_asking_for_different_args():
    # Layer 1 is the only layer that caches, so `args` are what a mount
    # is *for* beyond the bytes it carries. A node whose peer policy
    # asks for a different cache than the subtree above it is asking for
    # a second mount, and collapsing would silently hand it the outer
    # mount's cache instead.
    resolved = {
        "server_subtrees": [],
        "subtree_mounts": [],
        "samba_shares": [],
        "peer_dependencies": [
            peer_dependency("top", args={"vfs-cache-mode": "full"}),
            peer_dependency("top/leaf", args={"vfs-cache-mode": "writes"}),
        ],
    }
    layer1 = transports(plan_mounts(resolved, {}))
    assert set(layer1) == {"top", "top/leaf"}
    assert layer1["top/leaf"]["parent_transport"] == layer1["top"]["slug"]

    # Same policy on both, and the inner one goes.
    resolved["peer_dependencies"][1] = peer_dependency(
        "top/leaf", args={"vfs-cache-mode": "full"}
    )
    assert set(transports(plan_mounts(resolved, {}))) == {"top"}


def test_plan_mounts_keeps_a_nested_peer_transport_reading_another_path():
    # The collapse claims the outer mount already reaches this exact
    # path, so it checks that rather than assuming it: the inner remote
    # has to root where the outer mount lands when you walk down to it.
    # resolve() sets a peer's local_path and remote_path from one node
    # path today, so nothing in the tree can currently break that -- the
    # guard is what keeps the claim true if anything ever sources a path
    # from somewhere else in the owning host's tree.
    resolved = {
        "server_subtrees": [],
        "subtree_mounts": [],
        "samba_shares": [],
        "peer_dependencies": [
            peer_dependency("top"),
            peer_dependency("top/leaf", remote_path="elsewhere/leaf"),
        ],
    }
    assert set(transports(plan_mounts(resolved, {}))) == {"top", "top/leaf"}


GRANT_TREE = {
    "top": {
        "host": "owner",
        "subdirs": {
            "shared": {
                "access.owner": "jd",
                "subdirs": {"inner": {}},
            },
        },
    },
}
GRANT_HOSTS = ["owner", "peer"]


def test_a_nodes_own_grant_is_applied_on_a_host_that_mounts_it_without_owning_it():
    # The same node, the same grant, on both hosts -- spec.md §1's "every
    # host's local tree is the same tree" reaches ownership too. What
    # differs is only the mechanism each host has available: the owner
    # chowns a real directory, and a host holding a mounted copy has to
    # present it, since a chown inside a FUSE mount reports success and
    # persists nothing.
    owner = plan_index(plan_mounts(resolve(GRANT_TREE, "owner", GRANT_HOSTS), {}))
    peer = plan_index(plan_mounts(resolve(GRANT_TREE, "peer", GRANT_HOSTS), {}))

    for plan in (owner, peer):
        assert access_owner(plan["top/shared"]["access"], "stortree") == "jd"
        assert access_mode(plan["top/shared"]["access"]) == "0701"

    assert owner["top/shared"]["kind"] == "dir"
    assert peer["top/shared"]["kind"] == "mount"
    assert peer["top/shared"]["transport_slug"] == "top"

    # The node below inherits that same grant (_walk_tree()) and needs
    # nothing of its own to apply it on either host: the owner chowns a
    # real directory, and on the peer the presentation above already
    # shows every path inside it as `jd` (_grant_presented_above()), so
    # one bindfs covers the whole subtree.
    for plan in (owner, peer):
        assert access_owner(plan["top/shared/inner"]["access"], "stortree") == "jd"
        assert plan["top/shared/inner"]["kind"] == "dir"


def test_a_top_level_subtrees_own_grant_reaches_the_hosts_that_peer_mount_it():
    # The mounted node itself, not just what's inside it: a subtree mount
    # is this host's copy of that node, and §6's rule is about the node.
    # _samba_peer_dependencies() has always fallen back this way for a
    # descendant; the subtree-mount path used to substitute `{}`, which
    # is how a grant could stop at whichever host happened to own it.
    tree = {"top": {"host": "owner", "access.owner": "jd"}}
    for host in GRANT_HOSTS:
        plan = plan_index(plan_mounts(resolve(tree, host, GRANT_HOSTS), {}))
        assert access_owner(plan["top"]["access"], "stortree") == "jd"

    # A peer block on the node still replaces it for the host it names.
    tree["top"]["peers"] = {"peer": {"access.owner": "mw"}}
    plan = plan_index(plan_mounts(resolve(tree, "peer", GRANT_HOSTS), {}))
    assert access_owner(plan["top"]["access"], "stortree") == "mw"


def test_a_peer_block_grant_beats_the_nodes_own_on_the_host_it_names():
    # `peers.<host>.access` describes one host's copy, and it wins
    # there -- including on a node with no `samba:` or `rclone:` in that
    # block to give it an entry any other way.
    tree = {
        "top": {
            "host": "owner",
            "rclone.remote": "r:/",
            "subdirs": {
                "shared": {
                    "access.owner": "jd",
                    "peers": {"peer": {"access.owner": "mw"}},
                },
            },
        },
    }
    peer = plan_index(plan_mounts(resolve(tree, "peer", GRANT_HOSTS), {}))
    assert access_owner(peer["top/shared"]["access"], "stortree") == "mw"
    # ...and nowhere else.
    owner = plan_index(plan_mounts(resolve(tree, "owner", GRANT_HOSTS), {}))
    assert access_owner(owner["top/shared"]["access"], "stortree") == "jd"


def test_a_grant_is_not_applied_where_the_peer_never_mounts_the_subtree():
    # No mount, no presentation, and no directory either: an opted-out
    # subtree's descendants aren't this host's business at all
    # (docs/config-schema.md "Per-peer mount opt-out"). Applying a
    # grant here would mean creating the path first, which is precisely
    # what the opt-out is for.
    tree = {
        "top": {
            "host": "owner",
            "rclone.remote": "r:/",
            "peer-defaults.rclone": False,
            "subdirs": {"shared": {"access.owner": "jd"}},
        },
    }
    resolved = resolve(tree, "peer", GRANT_HOSTS)
    assert resolved["subtree_grants"] == []
    assert "top/shared" not in plan_index(plan_mounts(resolved, {}))


def test_an_opt_out_below_a_mounted_ancestor_does_not_drop_that_nodes_grant():
    # `rclone: false` on a node underneath an enabled ancestor carves no
    # hole out of the ancestor's mount (_subtree_mount_targets()), so the
    # path is presented on this host regardless. Reading that key here
    # too would leave it presented and ungoverned -- the one outcome
    # neither reading of the opt-out is asking for.
    tree = {
        "top": {
            "host": "owner",
            "rclone.remote": "r:/",
            "subdirs": {
                "shared": {
                    "access.owner": "jd",
                    "peer-defaults.rclone": False,
                },
            },
        },
    }
    peer = plan_index(plan_mounts(resolve(tree, "peer", GRANT_HOSTS), {}))
    assert peer["top/shared"]["kind"] == "mount"
    assert access_owner(peer["top/shared"]["access"], "stortree") == "jd"


# A grant written once, at the top of a subtree, and everything a node
# below it can say about that grant: nothing (inherit it), another key
# (merge over it), the same key again (narrow it), or an empty `access:`
# (drop it).
INHERIT_TREE = {
    "top": {
        "host": "owner",
        "access.group": "Storage Data",
        "subdirs": {
            "backups": {"subdirs": {"nightly": {}}},
            "notes": {"access.owner": "jd"},
            "readonly": {"access.permissions": "rx"},
            "ungoverned": {"access": None},
        },
    },
}


def test_a_grant_inherits_down_to_every_node_beneath_it():
    # What a grant on a subtree's top node means: the whole subtree, not
    # that one directory. Left uninherited, `Storage Data` could traverse
    # `top` (access_mode()'s public-execute bit) and read nothing in it,
    # which is essentially never what writing the group meant.
    plan = plan_index(plan_mounts(resolve(INHERIT_TREE, "owner", GRANT_HOSTS), {}))
    for path in ("top", "top/backups", "top/backups/nightly"):
        assert access_group(plan[path]["access"], "stortree") == "Storage Data"
        # Nothing is mounted anywhere in this tree, so every one of them
        # is a real local directory and a plain chown applies the grant
        # (roles/stortree_mounts "Own every genuinely local granted
        # path").
        assert plan[path]["kind"] == "dir"
        assert plan[path]["transport_slug"] is None


def test_an_inherited_grant_costs_one_presentation_for_the_whole_subtree():
    # The same tree seen by a host that mounts it: `top` is a peer mount,
    # so ownership inside it is whatever a presentation shows. One
    # presentation shows it for every path beneath, since bindfs is
    # uniform over its subtree -- so the nodes that inherited the grant
    # unchanged stay plain directories inside that one mount.
    plan = plan_mounts(resolve(INHERIT_TREE, "peer", GRANT_HOSTS), {})
    indexed = plan_index(plan)
    for path in ("top/backups", "top/backups/nightly"):
        assert access_group(indexed[path]["access"], "stortree") == "Storage Data"
        assert indexed[path]["kind"] == "dir"
        assert indexed[path]["transport_slug"] == _slug("top")

    # Only the nodes saying something different from the mount above
    # them: `top` itself, and the three that added to, narrowed or
    # dropped its grant.
    assert sorted(e["local_path"] for e in plan if e["kind"] == "mount") == [
        "top",
        "top/notes",
        "top/readonly",
        "top/ungoverned",
    ]


def test_a_nodes_own_access_merges_over_the_inherited_grant():
    # Key by key, from the nearest ancestor that set each one. `notes`
    # names an owner and keeps the group above it; `readonly` narrows
    # that group's level and keeps the group itself. Neither has to
    # restate what it isn't changing -- restating it is how two lines
    # that were meant to agree drift apart later.
    for host in GRANT_HOSTS:
        plan = plan_index(plan_mounts(resolve(INHERIT_TREE, host, GRANT_HOSTS), {}))

        notes = plan["top/notes"]["access"]
        assert access_owner(notes, "stortree") == "jd"
        assert access_group(notes, "stortree") == "Storage Data"

        readonly = plan["top/readonly"]["access"]
        assert access_group(readonly, "stortree") == "Storage Data"
        assert access_mode(readonly) == "0750"

        # An ancestor's *defaulted* permissions must not arrive looking
        # like one the config wrote out: `top` wrote no level, so it
        # keeps the traversal bit a deeper grant needs, and `readonly`
        # writing one does not reach back up and change that.
        assert access_mode(plan["top"]["access"]) == "0771"


def test_a_null_takes_back_one_inherited_key():
    # The fine-grained half of the escape hatch: with every key merging,
    # `null` is what says "not this one", and an empty `access:` (tested
    # above) is the same thing said about all of them at once.
    tree = {
        "top": {
            "host": "owner",
            "access": {"group": "Storage Data", "owner": "jd"},
            "subdirs": {"shared": {"access.owner": None}},
        },
    }
    for host in GRANT_HOSTS:
        plan = plan_index(plan_mounts(resolve(tree, host, GRANT_HOSTS), {}))
        shared = plan["top/shared"]["access"]
        assert access_group(shared, "stortree") == "Storage Data"
        assert access_owner(shared, "stortree") == "stortree"


def test_an_empty_access_drops_an_inherited_grant_on_every_host():
    # The escape hatch, and the reason a drop is marked rather than
    # blank (_dropped_access()): on the owning host it is a chown back to
    # the default, but on a host that mounts the subtree the default has
    # to be *presented*, or the grant above would keep applying to a path
    # that just said it doesn't.
    owner = plan_index(plan_mounts(resolve(INHERIT_TREE, "owner", GRANT_HOSTS), {}))
    peer = plan_index(plan_mounts(resolve(INHERIT_TREE, "peer", GRANT_HOSTS), {}))
    for plan in (owner, peer):
        assert access_group(plan["top/ungoverned"]["access"], "stortree") == "stortree"
        assert access_mode(plan["top/ungoverned"]["access"]) == "0751"
    assert owner["top/ungoverned"]["kind"] == "dir"
    assert peer["top/ungoverned"]["kind"] == "mount"


def test_a_grant_is_compared_against_the_nearest_presentation_above_it():
    # `leaf` grants what `top` grants, but `mid` in between grants
    # something else -- and `mid` is what the path actually shows. So
    # `leaf` needs its own presentation to get back to `top`'s grant,
    # even though an ancestor already applies it somewhere above.
    tree = {
        "top": {
            "host": "owner",
            "access.group": "Storage Data",
            "subdirs": {
                "mid": {
                    "access.group": "Media Production",
                    "subdirs": {"leaf": {"access.group": "Storage Data"}},
                },
            },
        },
    }
    peer = plan_index(plan_mounts(resolve(tree, "peer", GRANT_HOSTS), {}))
    assert [peer[p]["kind"] for p in ("top", "top/mid", "top/mid/leaf")] == [
        "mount",
        "mount",
        "mount",
    ]


def test_an_inherited_grant_reaches_a_per_user_node():
    # Inheritance crosses a `user-subdirs` boundary like any other, and
    # it has to: a per-user node resolves against its own grant
    # (_expand_per_user()), so one that inherited nothing resolved to
    # nobody and dropped out of the plan entirely -- an empty per-user
    # folder nobody could reach, under a subtree whose whole point was
    # the group written at the top of it.
    tree = {
        "top": {
            "host": "owner",
            "access.group": "Media Production",
            "user-subdirs": {"docs": {}},
        },
    }
    plan = plan_index(
        plan_mounts(
            resolve(tree, "owner", GRANT_HOSTS),
            {"Media Production": ["jd", "mw"]},
        )
    )
    real = per_user_mount_path(f"top/{PER_USER_PLACEHOLDER}/docs", {"group": "x"})
    assert access_group(plan[real]["access"], "stortree") == "Media Production"
    for user in ("jd", "mw"):
        assert plan[f"top/{user}/docs"]["symlink_target"] == real


def test_an_inherited_grant_is_named_once_in_a_shares_valid_users():
    # `valid users` is a union over the share's descendants, and with
    # `access` inheriting, most of those descendants now carry the same
    # grant the share itself does. It is one principal either way -- and
    # a descendant that dropped the grant contributes nothing at all,
    # since a drop names nobody to admit (_dropped_access()).
    tree = {
        "top": {
            "host": "h1",
            "samba": None,
            "access.group": "Storage Data",
            "subdirs": {"a": {}, "b": {}, "c": {"access": None}},
        }
    }
    share = resolve(tree, "h1", ["h1"])["samba_shares"][0]
    assert [g["group"] for g in share["access"]] == ["Storage Data"]


def test_a_subtree_grants_principals_are_looked_up_on_that_host():
    # The lookup list is what `getent` is run for, and the presentation
    # unit reads the resulting uid/gid maps by name -- so a principal
    # that reaches a host only through a subtree grant, and is named
    # nowhere else in its facts, has to be in it. Missing, the unit
    # template resolves `stortree_group_gids[<name>]` against a map that
    # never had the name and the apply fails on that host alone.
    tree = {
        "top": {
            "host": "owner",
            "rclone.remote": "r:/",
            "subdirs": {"shared": {"access.group": "Only Here"}},
        },
    }
    resolved = resolve(tree, "peer", GRANT_HOSTS)
    assert [g["local_path"] for g in resolved["subtree_grants"]] == ["top/shared"]
    assert needed_groups(resolved) == ["Only Here"]

    tree["top"]["subdirs"]["shared"] = {"access.owner": "only-here"}
    assert needed_users(resolve(tree, "peer", GRANT_HOSTS)) == ["only-here"]


def test_permissions_merge_one_class_at_a_time():
    # The mapping form merges a level further down than the keys around
    # it: `readonly` restates the group's level and keeps the owner's,
    # rather than starting the mapping over. Same rule, one nesting
    # level deeper -- each class from the nearest ancestor that set it.
    tree = {
        "top": {
            "host": "owner",
            "access": {
                "owner": "jd",
                "group": "Storage Data",
                "permissions": {"owner": "rwx", "group": "rwx"},
            },
            "subdirs": {"readonly": {"access": {"permissions": {"group": "r-x"}}}},
        },
    }
    for host in GRANT_HOSTS:
        plan = plan_index(plan_mounts(resolve(tree, host, GRANT_HOSTS), {}))
        assert access_mode(plan["top"]["access"]) == "0771"
        assert access_mode(plan["top/readonly"]["access"]) == "0751"
        assert access_owner(plan["top/readonly"]["access"], "stortree") == "jd"

    # `null` takes the whole level back the way it takes any other key
    # back, leaving the principals with the plain default.
    tree["top"]["subdirs"]["reset"] = {"access": {"permissions": None}}
    plan = plan_index(plan_mounts(resolve(tree, "owner", GRANT_HOSTS), {}))
    assert access_mode(plan["top/reset"]["access"]) == "0771"


def test_a_peers_permissions_narrow_that_hosts_copy_alone():
    # The two merges meeting: a peer block writes one class, keeps the
    # rest of the node's grant, and says nothing about any other host.
    tree = {
        "top": {
            "host": "owner",
            "rclone.remote": "r:/",
            "access.group": "Storage Data",
            "peers": {"peer": {"access": {"permissions": {"group": "r-x"}}}},
        },
    }
    peer = plan_index(plan_mounts(resolve(tree, "peer", GRANT_HOSTS), {}))
    assert access_group(peer["top"]["access"], "stortree") == "Storage Data"
    assert access_mode(peer["top"]["access"]) == "0751"

    owner = plan_index(plan_mounts(resolve(tree, "owner", GRANT_HOSTS), {}))
    assert access_mode(owner["top"]["access"]) == "0771"


def test_a_permissions_level_is_held_to_the_alphabet_it_is_read_with():
    # _permission_bits() looks for `r`, `w` and `x` and ignores the
    # rest, so an unchecked typo is a grant quietly missing a bit.
    with pytest.raises(ValueError, match="must be an rwx-style string"):
        resolve({"top": {"host": "h1", "access": {"group": "g", "permissions": "rwz"}}},
                "h1", ["h1"])

    # The numeric mode this schema deliberately isn't. YAML has already
    # turned it into an integer by the time it gets here (and `0750`
    # into an entirely different one), so the check is also what stops
    # it failing later with nothing pointing back at the line.
    with pytest.raises(ValueError, match="not a numeric mode"):
        resolve({"top": {"host": "h1", "access": {"owner": "jd", "permissions": 750}}},
                "h1", ["h1"])

    with pytest.raises(ValueError, match=r"unknown `access\.permissions` key 'world'"):
        resolve(
            {"top": {"host": "h1", "access": {"group": "g", "permissions": {"world": "r"}}}},
            "h1",
            ["h1"],
        )

    # A class named in a peer block is held to the same alphabet.
    with pytest.raises(ValueError, match=r"`peers\.h2\.access\.permissions\.group`"):
        resolve(
            {
                "top": {
                    "host": "h1",
                    "access.group": "g",
                    "peers": {"h2": {"access": {"permissions": {"group": "rwq"}}}},
                }
            },
            "h1",
            ["h1", "h2"],
        )


def test_a_permissions_level_granted_to_nobody_is_rejected():
    # A level narrows a grant (`readonly` above). With no grant to
    # narrow it grants nobody anything, and resolves to the same `{}` as
    # the empty `access:` that means the plain default -- the opposite of
    # what writing a level out says. Refused while the line that caused
    # it is still in hand.
    node = {"top": {"host": "h1", "access": {"permissions": "rx"}}}
    with pytest.raises(ValueError, match="a level on its own grants nothing"):
        resolve(node, "h1", ["h1"])

    # Nulling the principal and leaving its level behind is the same
    # mistake written across two nodes, and is caught at the one that
    # ends up ungranted.
    nulled = {
        "top": {
            "host": "h1",
            "access": {"group": "Storage Data", "permissions": "rx"},
            "subdirs": {"shared": {"access.group": None}},
        }
    }
    with pytest.raises(ValueError, match="'top/shared'"):
        resolve(nulled, "h1", ["h1"])

    # A peer block's level narrows that host's copy of the node's
    # grant (test above), so the mistake there is this same one: a level
    # over a node that grants nobody anything. Checked against the
    # merged grant, which makes it a per-host error like a share-name
    # collision (_validate_share_names()) rather than a tree-wide one --
    # it is raised on the host whose copy it describes.
    tree = {
        "top": {
            "host": "h1",
            "peers": {"h2": {"access": {"permissions": "rx"}}},
        }
    }
    with pytest.raises(ValueError, match="`access for h2` leaves a "):
        resolve(tree, "h2", ["h1", "h2"])


def test_a_per_user_grant_still_fans_out_rather_than_planning_one_path():
    # A user-subdirs node's path is still %U-templated, so there is no
    # single path for a grant to land on -- it resolves through
    # _expand_per_user() into one presentation per authorized user (or
    # one shared mount plus binds), exactly as it always did, and the
    # subtree-grant stage must not plan a second entry at the template.
    tree = {
        "top": {
            "host": "owner",
            "rclone.remote": "r:/",
            "subdirs": {
                "home": {
                    "samba": None,
                    "user-subdirs": {"docs": {"access.owner": "jd"}},
                },
            },
        },
    }
    resolved = resolve(tree, "peer", GRANT_HOSTS)
    assert resolved["subtree_grants"] == []
    planned = plan_index(plan_mounts(resolved, {}))
    assert "top/home/docs" not in planned
    assert planned["top/home/jd/docs"]["kind"] == "mount"


def test_plan_mounts_nested_paths_require_their_nearest_real_mount_ancestor():
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    plan = plan_mounts(r, {"Michael Whitfield Family": ["mike"]})
    by_local_path = plan_index(plan)

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
    by_local_path = plan_index(plan)

    top = by_local_path["tree/top"]
    plain = by_local_path["tree/top/plain"]
    nested = by_local_path["tree/top/plain/nested"]

    assert plain["remote"] is None
    assert plain["requires_slug"] == top["slug"]
    assert nested["requires_slug"] == top["slug"]


def test_mount_unit_names():
    plan = [
        {"slug": "backups", "kind": "transport"},
        {"slug": "backups", "kind": "mount"},
        {"slug": "tree", "kind": "mount"},
    ]
    # A transport and the presentation above it share a slug and are
    # different units -- one per layer, both named here.
    assert mount_unit_names(plan) == [
        "stortree-remote@backups.service",
        "stortree-mount@backups.service",
        "stortree-mount@tree.service",
    ]


def test_mount_unit_names_excludes_remote_less_entries():
    # a plain directory (no rclone.remote) gets no systemd unit at all
    plan = [
        {"slug": "backups", "kind": "mount"},
        {"slug": "plain-dir", "kind": "dir"},
    ]
    assert mount_unit_names(plan) == ["stortree-mount@backups.service"]


def test_mount_unit_names_includes_bind_units_for_per_user_fan_out():
    # a per-user fan-out entry (symlink_target set, no remote of its own)
    # gets a stortree-bind@ unit, not a stortree-mount@ one -- it's a
    # kernel bind mount back onto the real entry, not a second rclone
    # mount (see plan_mounts()'s own docstring for why a real symlink
    # can't do this job instead).
    plan = [
        {"slug": "tree-home-.mounts-mw\\x2dfam", "kind": "mount"},
        {"slug": "tree-home-dana-mw\\x2dfam", "kind": "bind"},
    ]
    assert mount_unit_names(plan) == [
        "stortree-mount@tree-home-.mounts-mw\\x2dfam.service",
        "stortree-bind@tree-home-dana-mw\\x2dfam.service",
    ]


def _entry(plan, local_path):
    return next(e for e in plan if e["local_path"] == local_path)


def test_requires_orders_a_subtree_mount_after_a_sibling_cache_subtree():
    # the case the key exists for: a top-level subtree whose *peer*
    # points its cache-dir into another top-level subtree's mount. No
    # nesting relationship at all, so requires_slug can't derive it.
    tree = {
        "tree": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "requires": [".cache"],
            "peers": {"h2": {"rclone.args": {"cache-dir": "/srv/stortree/.cache"}}},
        },
        ".cache": {
            "host": "h2",
            "rclone.remote": "r2:/",
            "peer-defaults": {"rclone": False},
        },
    }
    plan = plan_mounts(resolve(tree, "h2", ["h1", "h2"]))
    assert _entry(plan, "tree")["requires_mounts"] == [
        {"local_path": ".cache", "slug": ".cache", "source_path": ".cache"}
    ]
    # and it really is the mount that isn't nested under it
    assert _entry(plan, "tree")["requires_slug"] is None

    # h1 owns `tree` and never mounts .cache at all (peer-defaults
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
        {"local_path": ".bravo-cache", "slug": ".bravo\\x2dcache",
         "source_path": ".bravo-cache"}
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
        {"local_path": ".cache", "slug": ".cache", "source_path": ".cache"}
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
    assert real["requires_mounts"] == [
        {"local_path": ".cache", "slug": ".cache", "source_path": ".cache"}
    ]
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
    by_local_path = plan_index(plan)

    assert by_local_path["tree/media-prod"]["slug"] != by_local_path["tree/media/prod"]["slug"]


def test_plan_mounts_orders_entries_shallowest_first():
    # stortree_mounts creates every path one directory level at a time,
    # in this order -- a deeper entry (more "/"-separated segments) must
    # never appear before a shallower one, or a backend that can't create
    # two missing levels in one implicit step (an SMB share, in
    # production) fails outright creating the deeper one first.
    r = resolve(EXAMPLE_TREE, "storage-node-bravo", EXAMPLE_HOSTS)
    plan = plan_mounts(r, {"Michael Whitfield Family": ["mike"]})
    # Per layer: transports come first as a block (layer 1 must exist
    # before anything is created or mounted against it), and each layer
    # is shallowest-first within itself.
    for kind_group in (
        [e for e in plan if e["kind"] == "transport"],
        [e for e in plan if e["kind"] != "transport"],
    ):
        depths = [e["local_path"].count("/") for e in kind_group]
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


def test_peer_opt_out_beats_universal_samba_sharing():
    # Samba sharing is universal, but `peer-defaults.rclone: false`
    # still stops a non-owning host from mounting anything of that
    # subtree -- so it ends up exporting the share with nothing behind
    # it. Worth pinning: the two rules pull in opposite directions and
    # the resolution isn't obvious from either one's own docs.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peer-defaults": {"rclone": False},
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
    assert r["subtree_mounts"] == []


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
        "subtree_mounts": [],
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
    # root's own subtree mount, back when a single root existed. Every
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
        for scope in ("server_subtrees", "subtree_mounts", "peer_dependencies"):
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


def test_filter_rclone_conf_keeps_a_subtree_mounts_own_direct_remote():
    # A subtree mount is usually peer-sourced, but a host named under
    # `peers:` for a subtree it doesn't own can still end up with a
    # direct third-party remote -- that section has to survive the
    # filter, or the mount starts with no credentials for it.
    conf = "[direct]\ntype = sftp\n\n[unrelated]\ntype = s3\n"
    resolved = {
        "server_subtrees": [],
        "subtree_mounts": [{"local_path": "top", "remote": "direct:/", "args": {}}],
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
        "subtree_mounts": [{"local_path": "top/x", "remote": None, "args": {}}],
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
                    "peer-defaults": {"rclone": False},
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
        "subtree_mounts": [],
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
        "subtree_mounts": [],
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
    # instead, which deliberately suppresses the separate subtree mount.
    tree = {"tree": {"host": "h1", "rclone.remote": "r:/"}}
    resolved = resolve(tree, "h2", ["h1", "h2"], "/data/stortree")
    (mount,) = resolved["subtree_mounts"]
    assert mount["remote"] == "peer-h1-tree:/data/stortree/tree"

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
    # instead, which deliberately suppresses the separate subtree mount.
    tree = {"tree": {"host": "h1", "rclone.remote": "r:/"}}
    resolved = resolve(tree, "h2", ["h1", "h2"])
    assert resolved["subtree_mounts"][0]["remote"] == "peer-h1-tree:/srv/stortree/tree"
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
        "subtree_mounts": [],
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


# -- per-host shares (a `samba:` inside a peer block) --------------------


def _appliance_tree():
    """`spool` is exported on h2 alone -- the host whose local service
    account writes to it. h1 owns the data and exports nothing."""
    return {
        "tree": {
            "host": "h1",
            "rclone.remote": "r:/",
            "subdirs": {
                "spool": {
                    "peers.h2": {
                        "samba": {"name": "spool", "hidden": True},
                        "access.owner": "svc",
                    }
                }
            },
        }
    }


def test_a_peer_block_samba_exports_the_node_on_that_host_alone():
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


def test_peer_defaults_samba_exports_on_every_host_but_the_owner():
    tree = {"a": {"host": "h1", "peer-defaults": {"samba": {"name": "a"}}}}
    hosts = ["h1", "h2", "h3"]
    assert resolve(tree, "h1", hosts)["samba_shares"] == []
    for h in ("h2", "h3"):
        assert [s["name"] for s in resolve(tree, h, hosts)["samba_shares"]] == ["a"]


def test_the_owning_host_ignores_a_peer_block_written_for_itself():
    # `peers`/`peer-defaults` describe a host holding a *copy*; the
    # owner holds the original. Same rule `rclone` and `access` follow.
    tree = {"a": {"host": "h1", "peers.h1": {"samba": {"name": "nope"}}}}
    assert resolve(tree, "h1", ["h1", "h2"])["samba_shares"] == []


def test_a_peer_block_samba_renames_that_hosts_copy_of_a_universal_share():
    tree = {
        "a": {"host": "h1", "samba": {"name": "shared"}, "peers.h2": {"samba": {"name": "local"}}}
    }
    hosts = ["h1", "h2"]
    assert [s["name"] for s in resolve(tree, "h1", hosts)["samba_shares"]] == ["shared"]
    assert [s["name"] for s in resolve(tree, "h2", hosts)["samba_shares"]] == ["local"]


def test_a_peer_block_samba_false_withdraws_a_universal_share_on_that_host():
    tree = {"a": {"host": "h1", "samba": {"name": "a"}, "peers.h2": {"samba": False}}}
    hosts = ["h1", "h2"]
    assert [s["name"] for s in resolve(tree, "h1", hosts)["samba_shares"]] == ["a"]
    assert resolve(tree, "h2", hosts)["samba_shares"] == []


def test_a_peer_block_samba_does_not_cascade_to_descendants():
    # `samba` marks the one node it is written on, never that node's
    # subtree -- exactly as a node's own `samba:` does. Cascading would
    # export every descendant under a single name.
    tree = {
        "a": {
            "host": "h1",
            "peer-defaults": {"samba": {"name": "outer"}},
            "subdirs": {"b": {}, "c": {}},
        }
    }
    shares = resolve(tree, "h2", ["h1", "h2"])["samba_shares"]
    assert [s["node_path"] for s in shares] == ["a"]


def test_within_one_node_peers_host_beats_peer_defaults_for_samba():
    tree = {
        "a": {
            "host": "h1",
            "peer-defaults": {"samba": {"name": "default"}},
            "peers": {"h2": {"samba": {"name": "specific"}}},
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
            "peers.h2": {"samba": {"name": "a"}},
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
        "b": {"host": "h1", "peers.h2": {"samba": {"name": "media"}}},
    }
    for host in ("h1", "h2"):
        with pytest.raises(ValueError, match="share name 'media' on 'h2'"):
            resolve(tree, host, ["h1", "h2"])


def test_a_per_host_share_name_is_free_on_hosts_that_do_not_export_it():
    # The same two nodes, with the per-host share named distinctly:
    # nothing collides, and only h2 sees the second share at all.
    tree = {
        "a": {"host": "h1", "samba": {"name": "media"}},
        "b": {"host": "h1", "peers.h2": {"samba": {"name": "spool"}}},
    }
    hosts = ["h1", "h2"]
    assert [s["name"] for s in resolve(tree, "h1", hosts)["samba_shares"]] == ["media"]
    assert sorted(s["name"] for s in resolve(tree, "h2", hosts)["samba_shares"]) == [
        "media",
        "spool",
    ]


# -- valid users follows what this host actually enforces ------------------


def test_a_peer_side_grant_replaces_the_nodes_own_in_valid_users():
    # A share's `valid users` names the principals the filesystem
    # underneath it will admit -- which is the peer-side grant on a
    # host holding a copy, and the node's own on the host that owns it.
    tree = {
        "a": {
            "host": "h1",
            "samba": {"name": "a"},
            "subdirs": {
                "d": {"access.owner": "alice", "peers.h2": {"access.owner": "bob"}}
            },
        }
    }
    hosts = ["h1", "h2"]
    (on_h1,) = resolve(tree, "h1", hosts)["samba_shares"]
    (on_h2,) = resolve(tree, "h2", hosts)["samba_shares"]
    assert [g["owner"] for g in on_h1["access"]] == ["alice"]
    assert [g["owner"] for g in on_h2["access"]] == ["bob"]


def test_valid_users_is_identical_everywhere_with_no_peer_side_grant():
    # The default is unchanged: without a peer block saying otherwise,
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


def test_a_misspelled_key_inside_a_peer_block_samba_is_rejected():
    tree = {"a": {"host": "h1", "peers.h2": {"samba": {"nmae": "x"}}}}
    with pytest.raises(ValueError, match=r"unknown `peers.h2.samba` key 'nmae'"):
        resolve(tree, "h1", ["h1", "h2"])


def test_samba_subpath_inside_a_peer_block_is_rejected_by_name():
    tree = {"a": {"host": "h1", "peers.h2": {"samba": {"subpath": "%U"}}}}
    with pytest.raises(ValueError, match=r"sets `peers.h2.samba.subpath`"):
        resolve(tree, "h1", ["h1", "h2"])


def test_a_peer_block_samba_name_is_held_to_the_same_alphabet():
    tree = {"a": {"host": "h1", "peers.h2": {"samba": {"name": ".hidden"}}}}
    with pytest.raises(ValueError, match="may contain only letters"):
        resolve(tree, "h1", ["h1", "h2"])


def test_peer_opt_out_also_withholds_peer_trust_from_the_serving_side():
    # The mirror of test_peer_opt_out_beats_universal_samba_sharing,
    # seen from the host that owns the data: if no peer will ever mount
    # it, this host must not list them in peer_served_by either --
    # that's what stortree_peer_trust turns into authorized_keys, and an
    # entry here is real SSH access granted for a mount that can't
    # happen.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peer-defaults": {"rclone": False},
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
    tree["top"].pop("peer-defaults")
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


def test_a_misspelled_peer_defaults_is_rejected_not_silently_ignored():
    # Cost: `peer-defaults.rclone: false` is how a subtree is kept off
    # every non-owning host. Misspell the block and every host in the
    # fleet peer-mounts it instead -- with the SSH trust to match.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peer_defaults": {"rclone": False},
        }
    }
    with pytest.raises(ValueError, match="unknown key 'peer_defaults'"):
        resolve(tree, "h1", ["h1", "h2"])


def test_the_old_clients_key_names_its_replacement():
    # `clients:`/`client-defaults:` were the old names. Same cost as the
    # misspelling above -- a silently ignored block re-enables a subtree
    # its author kept local -- but the fix is a rename, not a
    # correction, so it gets a message that says so rather than the
    # generic unknown-key one (difflib scores `clients` nowhere near
    # `peers`, so that error wouldn't even suggest it).
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "clients": {"h2": {"rclone": False}},
        }
    }
    with pytest.raises(ValueError, match="`clients`, which was renamed to `peers`"):
        resolve(tree, "h1", ["h1", "h2"])


def test_the_old_client_defaults_key_names_its_replacement():
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "client-defaults": {"rclone": False},
        }
    }
    with pytest.raises(
        ValueError, match="`client-defaults`, which was renamed to `peer-defaults`"
    ):
        resolve(tree, "h1", ["h1", "h2"])


def test_a_legacy_key_is_caught_at_any_depth_not_just_the_top_level():
    # `peers`/`peer-defaults` are ordinary node keys (config-schema.md
    # "At any depth"), so a config written against the old schema can
    # carry the old name on a subdirectory too.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {"inner": {"client-defaults": {"rclone": False}}},
        }
    }
    with pytest.raises(ValueError, match="'top/inner' sets `client-defaults`"):
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
    # instead of the one SMB clients were told to mount.
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "samba": {"nmae": "home"},
        }
    }
    with pytest.raises(ValueError, match="unknown `samba` key 'nmae'"):
        resolve(tree, "h1", ["h1"])


def test_an_unknown_key_inside_a_per_peer_override_is_rejected():
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peers": {"h2": {"rclone": {"arguments": {"dir-cache-time": "5m"}}}},
        }
    }
    with pytest.raises(
        ValueError, match=r"unknown `peers\.h2\.rclone` key 'arguments'"
    ):
        resolve(tree, "h1", ["h1", "h2"])


def test_an_unknown_key_inside_peer_defaults_is_rejected():
    tree = {
        "top": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peer-defaults": {"rclone": False, "extra": 1},
        }
    }
    with pytest.raises(ValueError, match="unknown `peer-defaults` key 'extra'"):
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
    with pytest.raises(ValueError, match="expected one of: access, host, peer-defaults"):
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
        {"local_path": "top", "kind": "transport", "slug": "top"},
        {"local_path": "top", "kind": "mount", "slug": "top"},
        {"local_path": "top/u", "kind": "bind", "slug": "top-u"},
    ]
    installed = [
        "/etc/systemd/system/stortree-remote@top.service",
        "/etc/systemd/system/stortree-mount@top.service",
        "/etc/systemd/system/stortree-bind@top-u.service",
        "/etc/systemd/system/stortree-remote@gone.service",
        "/etc/systemd/system/stortree-mount@gone.service",
        "/etc/systemd/system/stortree-bind@gone-u.service",
    ]

    assert stale_unit_names(installed, plan) == [
        "stortree-remote@gone.service",
        "stortree-mount@gone.service",
        "stortree-bind@gone-u.service",
    ]


def test_stale_units_is_empty_when_every_installed_unit_is_still_planned():
    plan = [{"local_path": "top", "kind": "mount", "slug": "top"}]
    installed = ["/etc/systemd/system/stortree-mount@top.service"]
    assert stale_unit_names(installed, plan) == []


def test_stale_units_on_a_host_with_nothing_installed_yet():
    assert stale_unit_names([], []) == []


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


# -- `userdir-groups` ------------------------------------------------------
#
# Who has a per-user directory under a node used to be answerable only
# from underneath it: whoever an `access` grant on some descendant
# happened to name. `userdir-groups` lets the node say so itself, which
# is a second source for the same set rather than a replacement -- the
# grants beneath it still contribute exactly as they did.


def test_userdir_groups_makes_a_container_for_every_member():
    tree = {
        "tree": {
            "host": "h1",
            "subdirs": {"home": {"userdir-groups": ["Fam"], "user-subdirs": {}}},
        }
    }
    plan = plan_index(
        plan_mounts(resolve(tree, "h1", ["h1"]), {"Fam": ["ann", "bo"]})
    )
    assert "tree/home/ann" in plan and "tree/home/bo" in plan
    # An ordinary home directory: the plain `owner` grant every container
    # gets, private to that user plus the traversal bit.
    assert plan["tree/home/ann"]["access"]["owner"] == "ann"
    assert access_mode(plan["tree/home/ann"]["access"]) == "0701"


def test_userdir_groups_adds_to_the_grants_beneath_it():
    # Both sources feed one set. `cat` is a member of neither group but
    # owns something nested, and still gets the one container they always
    # did; `ann` is in the named group and gets one with nothing granted
    # inside it at all, which is what the key exists for.
    tree = {
        "tree": {
            "host": "h1",
            "subdirs": {
                "home": {
                    "userdir-groups": ["Fam"],
                    "user-subdirs": {"notes": {"access.owner": "cat"}},
                }
            },
        }
    }
    plan = plan_index(plan_mounts(resolve(tree, "h1", ["h1"]), {"Fam": ["ann"]}))
    assert "tree/home/ann" in plan
    assert "tree/home/cat" in plan
    assert "tree/home/cat/notes" in plan
    assert "tree/home/ann/notes" not in plan


def test_a_user_in_both_sources_gets_one_container():
    tree = {
        "tree": {
            "host": "h1",
            "subdirs": {
                "home": {
                    "userdir-groups": ["Fam"],
                    "user-subdirs": {"notes": {"access.owner": "ann"}},
                }
            },
        }
    }
    plan = plan_mounts(resolve(tree, "h1", ["h1"]), {"Fam": ["ann"]})
    assert [e["local_path"] for e in plan].count("tree/home/ann") == 1


def test_userdir_groups_needs_no_user_subdirs():
    # The config a `user-subdirs` node could never express: per-user
    # folders with nothing shared inside them. Home directories.
    tree = {"tree": {"host": "h1", "samba": {}, "userdir-groups": ["Fam"]}}
    resolved = resolve(tree, "h1", ["h1"])
    (share,) = resolved["samba_shares"]
    assert share["subpath"] == PER_USER_PLACEHOLDER
    plan = plan_index(plan_mounts(resolved, {"Fam": ["ann"]}))
    assert "tree/ann" in plan


@pytest.mark.parametrize("block", [[], None], ids=["empty", "bare"])
def test_an_empty_userdir_groups_block_is_still_a_per_user_share(block):
    # Presence, not contents -- the same reading `user-subdirs` gets.
    # Emptying the list says nobody is in it yet, not that the node
    # stopped being per-user, and reading it the other way would widen
    # the share from one user's folder to the directory holding
    # everyone's.
    tree = {"tree": {"host": "h1", "samba": {}, "userdir-groups": block}}
    (share,) = resolve(tree, "h1", ["h1"])["samba_shares"]
    assert share["subpath"] == PER_USER_PLACEHOLDER


def test_a_group_with_no_members_makes_no_directories():
    # Membership is host-local identity, not config: an empty group is an
    # ordinary state, resolved at apply time and not an error here.
    tree = {"tree": {"host": "h1", "userdir-groups": ["Fam"]}}
    plan = plan_index(plan_mounts(resolve(tree, "h1", ["h1"]), {"Fam": []}))
    assert [p for p in plan if p.startswith("tree/")] == []


def test_needed_groups_covers_userdir_groups():
    # Without this the `getent group` lookup never asks about the group,
    # membership resolves empty, and the directories are silently unmade.
    tree = {"tree": {"host": "h1", "userdir-groups": ["Fam"]}}
    assert needed_groups(resolve(tree, "h1", ["h1"])) == ["Fam"]


def test_needed_users_covers_userdir_group_members():
    # The containers need their owners' numeric UIDs, same as any other.
    tree = {"tree": {"host": "h1", "userdir-groups": ["Fam"]}}
    resolved = resolve(tree, "h1", ["h1"])
    assert needed_users(resolved, {"Fam": ["ann", "bo"]}) == ["ann", "bo"]


def test_userdir_groups_reaches_the_worked_examples_home_share():
    # End-to-end on the shipped example. `pat` is in the household and is
    # named by no grant anywhere in the tree -- exactly the home
    # directory that could not exist before the key -- while `jd` and
    # `mw` keep the folders their grants always gave them. Bravo alone
    # also serves its own operators', added by its peer block rather than
    # substituted for the list `home` itself wrote.
    group_members = {
        "Whitfield Household": ["jd", "mw", "pat"],
        "Whitfield Family & Friends": ["jd", "mw"],
        "Michael Whitfield Family": ["mw"],
        "Media Production": ["jd"],
        "Bravo Operators": ["ops"],
    }
    homes = {
        host: {
            e["local_path"]
            for e in plan_mounts(
                resolve(EXAMPLE_TREE, host, EXAMPLE_HOSTS), group_members
            )
            if e["local_path"].count("/") == 2
            and e["local_path"].startswith("tree/home/")
        }
        for host in EXAMPLE_HOSTS
    }
    for host in ("storage-node-alpha", "some-storage-gadget"):
        # The synthetic `.mounts` segment a group-only grant's one real
        # shared mount lives under is a level deeper, so this set is
        # home directories and nothing else.
        assert homes[host] == {
            "tree/home/jd",
            "tree/home/mw",
            "tree/home/pat",
        }
    assert homes["storage-node-bravo"] == homes["storage-node-alpha"] | {
        "tree/home/ops"
    }


def test_a_peer_block_adds_userdir_groups_rather_than_replacing_them():
    # The one peer-block key that composes additively: `rclone`,
    # `access` and `samba` each describe one host's copy and replace what
    # the node said, while this one says who the node is *for*, and a
    # host serving one more group still serves the node's own.
    tree = {
        "tree": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {
                "home": {
                    "userdir-groups": ["Fam"],
                    "peers": {"h2": {"userdir-groups": ["Extra"]}},
                    "user-subdirs": {},
                }
            },
        }
    }
    members = {"Fam": ["ann"], "Extra": ["zed"]}
    hosts = ["h1", "h2"]

    (parent,) = resolve(tree, "h2", hosts)["userdir_parents"]
    assert parent == {"local_path": "tree/home", "groups": ["Fam", "Extra"]}
    plan = plan_index(plan_mounts(resolve(tree, "h2", hosts), members))
    assert "tree/home/ann" in plan and "tree/home/zed" in plan

    # And the owner reads no peer block at all -- those describe a host
    # holding a copy, and the owner holds the original.
    (parent,) = resolve(tree, "h1", hosts)["userdir_parents"]
    assert parent == {"local_path": "tree/home", "groups": ["Fam"]}
    plan = plan_index(plan_mounts(resolve(tree, "h1", hosts), members))
    assert "tree/home/ann" in plan and "tree/home/zed" not in plan


def test_a_peer_added_group_reaches_a_host_that_owns_a_node_underneath():
    # No mount to inherit the owner's folders from: h2 reaches this node
    # only by owning something under it, so it has to create the whole
    # set itself.
    tree = {
        "tree": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peer-defaults": {"rclone": False},
            "subdirs": {
                "home": {
                    "userdir-groups": ["Fam"],
                    "peers": {"h2": {"userdir-groups": ["Extra"]}},
                    "user-subdirs": {"media": {"host": "h2", "access.owner": "cat"}},
                }
            },
        }
    }
    plan = plan_index(
        plan_mounts(
            resolve(tree, "h2", ["h1", "h2"]), {"Fam": ["ann"], "Extra": ["zed"]}
        )
    )
    for user in ("ann", "zed", "cat"):
        assert f"tree/home/{user}" in plan


def test_a_host_that_holds_nothing_at_the_node_plans_no_user_directories():
    # `peers.<h>.userdir-groups` on a subtree that same host is opted out
    # of would otherwise leave it a stray local tree of empty home
    # directories backing nothing.
    tree = {
        "tree": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "peer-defaults": {"rclone": False},
            "subdirs": {
                "home": {
                    "userdir-groups": ["Fam"],
                    "peers": {"h2": {"userdir-groups": ["Extra"]}},
                    "user-subdirs": {},
                }
            },
        }
    }
    resolved = resolve(tree, "h2", ["h1", "h2"])
    assert resolved["userdir_parents"] == []
    assert plan_mounts(resolved, {"Fam": ["ann"], "Extra": ["zed"]}) == []


def test_a_group_named_twice_is_named_once():
    # Deduped at every level a name can be repeated: within one list,
    # between a peer block's two halves, and between a peer block and the
    # node's own list. Nothing downstream breaks on a repeat -- the
    # containers dedupe by path too -- but `userdir_parents` is read by
    # `needed_groups()` and by a person auditing who has a folder where,
    # and a name listed twice tells neither of them anything new.
    tree = {
        "tree": {
            "host": "h1",
            "rclone.remote": "r1:/",
            "subdirs": {
                "home": {
                    "userdir-groups": ["Fam", "Fam", "Staff"],
                    "peer-defaults": {"userdir-groups": ["Staff", "Extra"]},
                    "peers": {"h2": {"userdir-groups": ["Extra", "Fam"]}},
                    "user-subdirs": {},
                }
            },
        }
    }
    hosts = ["h1", "h2"]
    (own,) = resolve(tree, "h1", hosts)["userdir_parents"]
    assert own["groups"] == ["Fam", "Staff"]
    (peer,) = resolve(tree, "h2", hosts)["userdir_parents"]
    assert peer["groups"] == ["Fam", "Staff", "Extra"]


def test_a_peer_block_userdir_groups_needs_a_per_user_node():
    # It adds to a per-user level the node already declares; it cannot
    # create one for a single host. The share path is derived from the
    # node's shape and has to be the same everywhere, so a node that were
    # per-user on one host and not on another would answer to one name
    # while serving every user's folder to every user on all the rest.
    tree = {"tree": {"host": "h1", "peers": {"h2": {"userdir-groups": ["Extra"]}}}}
    with pytest.raises(ValueError) as excinfo:
        resolve(tree, "h1", ["h1", "h2"])
    message = str(excinfo.value)
    assert "peers.h2.userdir-groups" in message
    assert "does not create one for a single host" in message


def test_userdir_groups_rejects_a_malformed_value():
    with pytest.raises(ValueError, match="must be a list of group names"):
        resolve({"tree": {"host": "h1", "userdir-groups": "Fam"}}, "h1", ["h1"])
    with pytest.raises(ValueError, match="non-empty group name"):
        resolve({"tree": {"host": "h1", "userdir-groups": [7]}}, "h1", ["h1"])
    with pytest.raises(ValueError, match="non-empty group name"):
        resolve({"tree": {"host": "h1", "userdir-groups": [""]}}, "h1", ["h1"])


def test_a_misspelled_userdir_groups_is_rejected_rather_than_ignored():
    # Silently ignored, it is a node whose home directories simply never
    # appear, with nothing at apply time saying why.
    with pytest.raises(ValueError, match="userdir-groups"):
        resolve({"tree": {"host": "h1", "userdir-group": ["Fam"]}}, "h1", ["h1"])


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


def test_layer_plan_entries_keeps_the_deepest_ancestor_whatever_the_order():
    # All three "nearest ancestor" searches -- transport for a node,
    # presentation for a nested mount, and the transport containing a
    # nested transport's node (`parent_transport`, which now only says
    # where that transport's one backend directory goes, not what it
    # waits for) -- have to keep the deepest match, not the last seen.
    entries = _entries(("top", "r:/"), ("top/mid", "r2:/"), ("top/mid/leaf", "r3:/"))
    transports = relate(entries)
    by_path = {e["local_path"]: e for e in entries}
    assert by_path["top/mid/leaf"]["transport_slug"] == "top-mid-leaf"
    assert by_path["top/mid/leaf"]["requires_slug"] == "top-mid"
    assert {t["local_path"]: t["parent_transport"] for t in transports} == {
        "top": None,
        "top/mid": "top",
        "top/mid/leaf": "top-mid",
    }


def test_plan_user_containers_skips_a_per_user_node_that_implies_no_container():
    # The per_user flag alone is not enough: a container can only be
    # derived from a node that has both an `access` grant to name its
    # owner and a %U in its path to say where the container sits.
    resolved = {
        "server_subtrees": [
            {"per_user": True, "path": "top/home/%U/x"},  # no access
            {"per_user": True, "path": "top/plain", "access": {"owner": "jd"}},  # no %U
            {"per_user": True, "access": {"owner": "jd"}},  # no path at all
        ],
        "peer_dependencies": [],
    }
    assert _plan_user_containers(resolved, {}) == []


def test_layer_plan_entries_keeps_the_deepest_ancestor_seen_out_of_order():
    # The same three searches again, but with the entries arriving
    # deepest-first, so the shallower ancestor is the one seen *last* and
    # must not displace the deeper match already found.
    entries = _entries(("top/mid/leaf", "r3:/"), ("top/mid", "r2:/"), ("top", "r:/"))
    transports = relate(entries)
    by_path = {e["local_path"]: e for e in entries}
    assert by_path["top/mid/leaf"]["requires_slug"] == "top-mid"
    assert {t["local_path"]: t["parent_transport"] for t in transports}[
        "top/mid/leaf"
    ] == "top-mid"


def test_ownership_mismatch_is_quiet_when_the_grant_is_applied():
    result = {
        "item": {"local_path": "top/x", "access": _normalize_access({"owner": "jd"})},
        "stat": {"pw_name": "jd", "gr_name": "stortree", "mode": "0701"},
    }
    assert ownership_mismatch(result, "stortree", "stortree") == ""


def test_ownership_mismatch_names_the_difference_when_a_grant_is_unenforced():
    # Exactly the shape the original bug had: the path exists, Ansible
    # reported the chown as changed, and the owner is still the ancestor
    # mount's own uniform one.
    result = {
        "item": {"local_path": "top/x", "access": _normalize_access({"owner": "jd"})},
        "stat": {"pw_name": "stortree", "gr_name": "stortree", "mode": "0751"},
    }
    assert ownership_mismatch(result, "stortree", "stortree") == (
        "top/x is stortree:stortree 751 (expected jd:stortree 701)"
    )


def test_check_slug_collisions_rejects_two_mounts_claiming_one_unit_name():
    entries = _entries(("top/leaf", "r:/"), ("top/leaf", "other:/"))
    with pytest.raises(ValueError, match="resolve to systemd unit slug"):
        relate(entries)


def test_check_slug_collisions_ignores_a_clash_between_non_mounts():
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
    relate(entries)
    by_path = {e["local_path"]: e for e in entries}
    assert by_path["top/mid/leaf"]["requires_slug"] == "top"
    assert by_path["top"]["requires_slug"] is None


def test_remote_dir_never_emits_a_systemd_escape():
    # The whole reason layer 1 mirrored the tree for as long as it did.
    # A slug escapes "-" as \x2d so unit instance names stay injective,
    # and systemd unescapes exactly that again when it parses an
    # ExecStart path -- so a slug used as a directory name silently
    # points the mount somewhere nobody created. Verified on a real
    # host. _remote_dir() must therefore never emit a backslash, and
    # never a "%" either (a systemd specifier).
    assert _slug("backups-mirror") == "backups\\x2dmirror"
    assert _remote_dir("backups-mirror") == "backups-mirror"
    for path in ("backups-mirror", "a b/c%d", "x\\y/z", "p,q/r+s"):
        rendered = _remote_dir(path)
        assert "\\" not in rendered
        assert "%" not in rendered
        assert "/" not in rendered


def test_remote_dir_is_injective_over_paths_that_slugs_would_confuse():
    # The separator has to be unambiguous in both directions: a literal
    # separator inside a segment escapes, so "a,b" as one segment can
    # never collide with "a"/"b" as two.
    assert _remote_dir("a/b") != _remote_dir("a,b")
    assert _remote_dir("a-b/c") != _remote_dir("a/b-c")
    # And the escape introducer escapes itself, so a segment that
    # literally spells an escape sequence is not read as one: "a,b"
    # encodes to "a+2cb", and a segment literally named "a+2cb" must
    # encode to something else again.
    assert _remote_dir("a,b") == "a+2cb"
    assert _remote_dir("a+2cb") != _remote_dir("a,b")


def test_remote_dir_leaves_hyphens_and_dots_alone():
    # Legibility is the point of not reusing the slug alphabet: the
    # common case is an operator reading a mountpoint in `findmnt`.
    assert (
        _remote_dir("tree/home/.mounts/whitfield-media")
        == "tree,home,.mounts,whitfield-media"
    )


def test_layer_plan_entries_lands_every_transport_on_a_flat_directory():
    # Layer 1 is flat: each transport mounts on one directory of its own
    # under the remotes root, so no mountpoint is ever inside another
    # mount. `parent_transport` still names the transport containing a
    # node, but only to say where that node's one backend directory goes
    # -- its presentation mounts inside the presentation above it -- and
    # `parent_source_path` says where. Neither orders anything any more.
    entries = _entries(("top", "r:/"), ("top/leaf", "r2:/"))
    transports = relate(entries)
    by_path = {t["local_path"]: t for t in transports}

    assert by_path["top"]["source_path"] == "top"
    assert by_path["top/leaf"]["source_path"] == "top,leaf"
    # the inner mountpoint is a sibling of the outer one, not a child
    assert not by_path["top/leaf"]["source_path"].startswith(
        by_path["top"]["source_path"] + "/"
    )

    assert by_path["top/leaf"]["parent_transport"] == "top"
    assert by_path["top/leaf"]["parent_source_path"] == "top/leaf"
    assert by_path["top"]["parent_transport"] is None
    assert by_path["top"]["parent_source_path"] is None


def test_layer_plan_entries_keeps_real_names_below_a_transports_own_directory():
    # Only the mountpoint segment is flattened. Everything past it is
    # the tree's own names, because those are directories the backend
    # actually stores -- flattening them would rename the remote.
    entries = _entries(("top", "r:/"), ("top/a-b/c", None))
    relate(entries)
    by_path = {e["local_path"]: e for e in entries}
    assert by_path["top/a-b/c"]["source_path"] == "top/a-b/c"


def test_relate_plan_entries_resolves_a_declared_requires_to_its_mount():
    entries = _entries(("cache", "r:/"), ("top", "r2:/", ["cache"]))
    transports = relate(entries)
    # A declared `requires` is a backend dependency, so it attaches to
    # layer 1 -- the layer that does the caching it exists to order.
    by_path = {t["local_path"]: t for t in transports}
    assert by_path["top"]["requires_mounts"] == [
        {"local_path": "cache", "slug": "cache", "source_path": "cache"}
    ]
    assert "requires" not in by_path["top"]


def test_relate_plan_entries_drops_a_requires_target_that_is_not_a_mount_here():
    # Either a plain local directory this same apply creates before any
    # unit starts, or a mount another host owns that this one doesn't
    # peer -- neither has a unit to order against.
    entries = _entries(("cache", None), ("top", "r:/", ["cache", "elsewhere"]))
    transports = relate(entries)
    by_path = {t["local_path"]: t for t in transports}
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


def test_an_opted_out_host_keeps_its_own_subtree_mount_of_the_tree():
    # Opting out of *exporting* the tree says nothing about wanting it
    # locally -- h3's own subtree mount of `top` is unaffected.
    tree = _samba_opt_out_tree()
    hosts = ["h1", "h2", "h3"]
    off = resolve(tree, "h3", hosts, samba_hosts=["h1", "h2"])
    assert "top" in {m["local_path"] for m in off["subtree_mounts"]}
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


# -- metrics_ports ---------------------------------------------------------


def _transport(local_path):
    return {"local_path": local_path, "kind": "transport", "slug": _slug(local_path)}


def test_metrics_ports_covers_transports_and_nothing_else():
    # Presentations are bindfs and binds are `mount --bind`; neither is
    # an rclone process, so neither has anything to serve.
    plan = [
        _transport("tree"),
        {"local_path": "tree", "kind": "mount", "slug": "tree"},
        {"local_path": "tree/home/jd", "kind": "bind", "slug": "tree-home-jd"},
        {"local_path": "tree/docs", "kind": "dir", "slug": "tree-docs"},
    ]
    assert set(metrics_ports(plan)) == {"tree"}


def test_metrics_ports_are_derived_from_the_path_not_the_position():
    # The property the whole design rests on: adding a node must not
    # move any other node's port. An index-based allocation would move
    # every port after the insertion, rewriting those units' ExecStart,
    # restarting those transports -- and PartOf= would take every
    # presentation and bind above them down too. One unrelated edit to
    # config.yml would remount the host's whole tree.
    before = metrics_ports([_transport("tree"), _transport("zzz")])
    after = metrics_ports(
        [_transport("aaa"), _transport("tree"), _transport("zzz")]
    )
    assert after["tree"] == before["tree"]
    assert after["zzz"] == before["zzz"]


def test_metrics_ports_do_not_depend_on_plan_order():
    forwards = metrics_ports([_transport("a"), _transport("b")])
    backwards = metrics_ports([_transport("b"), _transport("a")])
    assert forwards == backwards


def test_metrics_ports_stay_inside_the_configured_range():
    plan = [_transport(f"tree/node{i}") for i in range(12)]
    ports = metrics_ports(plan, base_port=30000, span=1000)
    assert len(ports) == 12
    assert all(30000 <= p < 31000 for p in ports.values())


def test_metrics_ports_take_an_override_by_tree_path():
    # Keyed by path, not slug: a slug is a systemd instance name with
    # \xHH escapes in it, and an operator pinning a port for a firewall
    # rule should not have to spell one.
    ports = metrics_ports(
        [_transport("tree/back-ups")], overrides={"tree/back-ups": 20500}
    )
    assert ports[_slug("tree/back-ups")] == 20500


def test_metrics_ports_raise_when_two_nodes_want_one_port():
    # Reported here rather than discovered as a dead mount: rclone exits
    # when it cannot bind and the transport unit is Type=notify.
    with pytest.raises(ValueError) as excinfo:
        metrics_ports(
            [_transport("tree/a"), _transport("tree/b")],
            overrides={"tree/a": 20500, "tree/b": 20500},
        )
    message = str(excinfo.value)
    assert "tree/a" in message and "tree/b" in message
    assert "stortree_metrics_port_overrides" in message


def test_metrics_ports_reject_a_range_that_is_not_a_valid_port_range():
    with pytest.raises(ValueError):
        metrics_ports([_transport("tree")], base_port=60000, span=10000)


# -- metrics_listeners -----------------------------------------------------


def test_metrics_listeners_pass_a_literal_address_through():
    (listener,) = metrics_listeners(["127.0.0.1"], {})
    assert listener == {
        "address": "127.0.0.1",
        "listen": "127.0.0.1",
        "device": None,
        "loopback": True,
        "needs_wait": False,
    }


def test_metrics_listeners_make_a_literal_address_wait_like_any_other():
    # `needs_wait` is deliberately not the same question as `device`.
    # A literal address has no .device unit to order behind and can
    # still be configured seconds after the transport unit is reached --
    # exactly the case the .device edge never covered.
    (listener,) = metrics_listeners(["10.10.0.9"], {})
    assert listener["device"] is None
    assert listener["needs_wait"] is True


def test_metrics_listeners_never_wait_for_loopback_or_the_wildcard():
    # Loopback is configured before userspace starts, so waiting for it
    # is only noise. The wildcard is the one that would actually break:
    # it never appears in `ip addr` output, so a wait for it runs to the
    # timeout and fails a bind that would have worked.
    for address in ("127.0.0.1", "::1", "0.0.0.0", "::"):
        (listener,) = metrics_listeners([address], {})
        assert listener["needs_wait"] is False, address


def test_metrics_listeners_make_an_interface_derived_address_wait():
    # The case that took this fleet down on every boot: tailscale0 up
    # and addressed, its .device unit permanently inactive because no
    # udev runs in a container, and rclone exiting on "cannot assign
    # requested address" before the interface arrived.
    facts = {"wg0": {"ipv4": {"address": "10.10.0.4"}}}
    (listener,) = metrics_listeners(["wg0"], facts)
    assert listener["needs_wait"] is True


def test_metrics_listeners_bracket_ipv6_for_the_listen_string():
    # `--metrics-addr 2001:db8::1:20123` would not parse.
    (listener,) = metrics_listeners(["2001:db8::1"], {})
    assert listener["listen"] == "[2001:db8::1]"


def test_metrics_listeners_treat_the_wildcard_as_not_loopback():
    # 0.0.0.0 is the most exposed bind there is; the role's safety
    # assert must not mistake it for a private one.
    (listener,) = metrics_listeners(["0.0.0.0"], {})
    assert listener["loopback"] is False


def test_metrics_listeners_resolve_an_interface_name_from_facts():
    facts = {"wg0": {"ipv4": {"address": "10.10.0.4"}}}
    (listener,) = metrics_listeners(["wg0"], facts)
    assert listener["address"] == "10.10.0.4"
    assert listener["device"] == "sys-subsystem-net-devices-wg0.device"


def test_metrics_listeners_escape_a_device_unit_the_way_systemd_does():
    # "-" is a path separator in a systemd unit name, so an interface
    # whose own name contains one has to be escaped -- same rule, and
    # the same function, as the mount unit slugs.
    facts = {"br_lan": {"ipv4": {"address": "192.168.1.2"}}}
    (listener,) = metrics_listeners(["br-lan"], facts)
    assert listener["device"] == "sys-subsystem-net-devices-br\\x2dlan.device"


def test_metrics_listeners_find_an_interface_under_ansibles_mangled_key():
    # Ansible flattens "-", "." and ":" to "_" in fact keys, so the name
    # an operator writes is not always the key the facts arrive under.
    facts = {"vlan_10": {"ipv4": {"address": "192.168.10.2"}}}
    (listener,) = metrics_listeners(["vlan.10"], facts)
    assert listener["address"] == "192.168.10.2"


def test_metrics_listeners_fall_back_to_a_routable_ipv6_address():
    facts = {
        "wg0": {
            "ipv6": [
                {"address": "fe80::1", "scope": "link"},
                {"address": "2001:db8::5", "scope": "global"},
            ]
        }
    }
    (listener,) = metrics_listeners(["wg0"], facts)
    assert listener["listen"] == "[2001:db8::5]"


def test_metrics_listeners_reject_an_interface_that_is_not_there():
    # Never a silent fallback to a wildcard bind: guessing 0.0.0.0 for
    # an interface that doesn't exist would publish every mount's
    # endpoint on every network the host is attached to.
    with pytest.raises(ValueError) as excinfo:
        metrics_listeners(["wg0"], {"eth0": {"ipv4": {"address": "10.0.0.1"}}})
    assert "wg0" in str(excinfo.value)


def test_metrics_listeners_reject_an_interface_with_only_a_link_local_address():
    # Binding fe80:: needs a zone id the facts don't carry in `address`,
    # so rclone would fail to bind -- and that failure is a dead mount,
    # not a missing counter.
    facts = {"wg0": {"ipv6": [{"address": "fe80::1", "scope": "link"}]}}
    with pytest.raises(ValueError):
        metrics_listeners(["wg0"], facts)


def test_metrics_listeners_of_nothing_is_empty():
    assert metrics_listeners([], {}) == []
    assert metrics_listeners(None, {}) == []


# -- mounted_transport_slugs -----------------------------------------------
#
# The guard that keeps stortree_mounts from writing into a transport's
# mountpoint while the transport is down -- which is unrecoverable, not
# merely wrong: rclone refuses to mount over a directory that is not
# empty, so the stray content a single failed mount lets Ansible create
# is what stops that mount ever coming back.

MOUNTINFO = """\
25 30 0:22 / /proc rw,nosuid,relatime shared:5 - proc proc rw
26 30 0:23 / /sys rw,nosuid,relatime shared:6 - sysfs sysfs rw
40 30 0:99 / /srv/.stortree-remotes/top rw,nosuid,relatime shared:9 \
- fuse.rclone peer-a-top:/srv/stortree/top rw,user_id=999,allow_other
41 30 0:98 / /srv/.stortree-remotes/top,home,.mounts,_shared rw,relatime shared:10 \
- fuse.rclone other:/media rw,user_id=999,allow_other
"""

# Layer 1 is flat: a transport's mountpoint is `<remotes_root>/<one flat
# name>` (_remote_dir()), never a path inside another transport's mount
# -- note the nested-looking node above is a *sibling* directory here,
# and that the kernel leaves "," alone in mountinfo (it escapes space,
# tab, newline and backslash, which is why _remote_dir() emits none).
PLAN = [
    {"kind": "transport", "slug": "top", "local_path": "top", "source_path": "top"},
    {
        "kind": "transport",
        "slug": "top-home-.mounts-_shared",
        "local_path": "top/home/.mounts/_shared",
        "source_path": "top,home,.mounts,_shared",
    },
    {"kind": "transport", "slug": "other", "local_path": "other", "source_path": "other"},
    {"kind": "dir", "slug": "top-home", "local_path": "top/home", "source_path": "top/home"},
]


def test_a_transport_with_a_live_mount_on_its_mountpoint_is_reported_mounted():
    assert mounted_transport_slugs(PLAN, MOUNTINFO, "/srv/.stortree-remotes") == [
        "top",
        "top-home-.mounts-_shared",
    ]


def test_a_transport_whose_unit_is_down_is_not_reported_mounted():
    # `other` is planned and its mountpoint may well exist as an empty
    # local directory -- which is exactly the state that makes this
    # question worth asking, since existence is not the same as being
    # mounted and only the latter means writes reach the backend.
    assert "other" not in mounted_transport_slugs(
        PLAN, MOUNTINFO, "/srv/.stortree-remotes"
    )


def test_only_transports_are_considered():
    # A `dir` entry's path can perfectly well be a mountpoint for
    # something else; it is never a transport of stortree's, and the
    # callers key their skip decisions off transport slugs.
    assert "top-home" not in mounted_transport_slugs(
        PLAN, MOUNTINFO, "/srv/.stortree-remotes"
    )


def test_a_remotes_root_that_is_not_this_hosts_matches_nothing():
    assert mounted_transport_slugs(PLAN, MOUNTINFO, "/srv/elsewhere") == []


def test_a_trailing_slash_on_the_remotes_root_does_not_break_the_match():
    assert mounted_transport_slugs(PLAN, MOUNTINFO, "/srv/.stortree-remotes/") == [
        "top",
        "top-home-.mounts-_shared",
    ]


def test_an_empty_mount_table_reports_nothing_mounted():
    # A host early in its first boot, or one where every mount failed.
    # Every nested path is then skipped, which is the safe direction:
    # the next apply creates them once the transports are up.
    assert mounted_transport_slugs(PLAN, "", "/srv/.stortree-remotes") == []


def test_mountpoints_with_escaped_characters_are_matched_unescaped():
    # The kernel writes space as \040 in mountinfo. No stortree slug
    # contains one, but stortree_remotes_root is the operator's to
    # choose, and a raw-vs-unescaped mismatch would read as "not
    # mounted" -- the failure direction that quietly stops creating
    # directories rather than the one that shouts.
    mountinfo = (
        "40 30 0:99 / /srv/two\\040words/top rw,relatime shared:9 "
        "- fuse.rclone a:/b rw\n"
    )
    assert mounted_transport_slugs(PLAN, mountinfo, "/srv/two words") == ["top"]


# -- apt_installable -------------------------------------------------------


APT_POLICY = """\
wsdd:
  Installed: (none)
  Candidate: (none)
  Version table:
wsdd2:
  Installed: (none)
  Candidate: 1.8.7+dfsg-1.2
  Version table:
     1.8.7+dfsg-1.2 500
        500 http://deb.debian.org/debian trixie/main amd64 Packages
"""


def test_a_package_with_no_candidate_is_not_installable():
    # Debian trixie: `wsdd` is still referenced in the archive, so apt
    # prints a block for it, but there is nothing to install.
    assert apt_installable(APT_POLICY, ["wsdd", "wsdd2"]) == ["wsdd2"]


def test_a_package_apt_has_never_heard_of_is_not_installable():
    # No block at all -- apt puts its note on stderr, which this never
    # sees. Same answer as an explicit "(none)", because it means the
    # same thing to the caller.
    assert apt_installable("", ["wsdd", "wsdd2"]) == []


def test_the_requested_order_is_preserved_not_apts():
    # The caller's order is its preference order: stortree_samba takes
    # the first installable name, and prefers `wsdd` where a host can
    # somehow install both.
    policy = APT_POLICY.replace("  Candidate: (none)", "  Candidate: 0.7.1-1", 1)
    assert apt_installable(policy, ["wsdd", "wsdd2"]) == ["wsdd", "wsdd2"]
    assert apt_installable(policy, ["wsdd2", "wsdd"]) == ["wsdd2", "wsdd"]


def test_an_installed_package_is_still_reported_by_its_candidate():
    policy = """\
wsdd:
  Installed: 0.7.0-1
  Candidate: 0.7.0-1
  Version table:
"""
    assert apt_installable(policy, ["wsdd"]) == ["wsdd"]
