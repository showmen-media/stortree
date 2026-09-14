"""Render the roles' real Jinja templates and assert on what comes out.

tests/test_stortree.py covers the pure functions that decide *what* a
host should do; this file covers the layer that turns those decisions
into the files systemd and Samba actually read. That layer carries real
logic of its own -- wrapper-mount preference, PartOf= vs. Requires=
propagation, `--uid`/`--gid` selection, argument flattening, smb.conf
token quoting -- and until now the only thing that exercised any of it
was the `full-tree` Molecule scenario, which has never been run
(docs/plan.md "What's verified"). A bug here renders a plausible-looking
unit file that silently mounts nothing, or an smb.conf that grants the
wrong people access.

Templates are rendered through ansible-core's own filters, tests and
AnsibleUndefined (see tests/conftest.py), against data straight out of
resolve()/plan_mounts() -- not hand-written
stand-ins -- so a change to a resolved entry's shape shows up here as a
failing render rather than at apply time.
"""

import xml.etree.ElementTree as ET

import pytest
import yaml

from conftest import EXAMPLE_HOSTS, REPO_ROOT
from filter_plugins.stortree import metrics_listeners, metrics_ports

REMOTE_UNIT = "stortree-remote@.service.j2"
MOUNT_UNIT = "stortree-mount@.service.j2"
BIND_UNIT = "stortree-bind@.service.j2"
METRICS_TARGETS = "metrics-targets.json.j2"
SMB_CONF = "smb.conf.j2"
SSSD_CONF = "sssd.conf.j2"
WSDD_UNIT = "wsdd.service.j2"
AVAHI_SERVICE = "stortree-smb.avahi.xml.j2"

ALPHA, BRAVO, GADGET = EXAMPLE_HOSTS


def transport_for(plan, local_path):
    """The layer-1 entry for a path. A transport and the presentation
    above it share a local_path -- they are the same directory in the two
    roots -- so every lookup has to say which layer it means."""
    return next(
        e for e in plan if e["local_path"] == local_path and e["kind"] == "transport"
    )


def entry_for(plan, local_path):
    return next(
        e for e in plan if e["local_path"] == local_path and e["kind"] != "transport"
    )


def container_for(containers, local_path):
    return next(c for c in containers if c["local_path"] == local_path)


def directives(rendered, name):
    """Every value of a systemd directive, in file order -- e.g.
    directives(unit, "After") -> ["network-online.target", ...]."""
    prefix = name + "="
    return [
        line[len(prefix) :]
        for line in rendered.splitlines()
        if line.startswith(prefix)
    ]


def mount_vars(containers, host):
    """The variable set roles/stortree_mounts/tasks/main.yml has in
    scope when it renders a unit template: the metrics settings, off, as
    roles/stortree_facts/defaults/main.yml leaves them. (Which mounts
    have something nested inside them used to be a separate fact the
    role derived and passed alongside, and is a field on the entry
    itself since -- plan_mounts()' `has_nested_children`.)

    Off is the honest default here, not a convenience: a template that
    reads a metrics variable outside its own `stortree_metrics_enabled`
    guard has to fail these renders, and it only does if the guarded
    variables are genuinely absent."""
    return {"stortree_metrics_enabled": False}


# One interface, as ansible_facts would report it, for the listener
# resolution the unit template renders off.
METRICS_FACTS = {"wg0": {"ipv4": {"address": "10.10.0.4"}}}


def metrics_vars(plan, bind=("127.0.0.1",), mode="metrics-addr", htpasswd=None):
    """What the role has in scope once metrics are switched on -- the
    two facts it derives ("Allocate a metrics port and resolve the
    listen addresses for every mount") plus the two settings it passes
    straight through."""
    return {
        "stortree_metrics_enabled": True,
        "stortree_metrics_mode": mode,
        "stortree_metrics_htpasswd": htpasswd,
        "stortree_metrics_listeners": metrics_listeners(list(bind), METRICS_FACTS),
        "stortree_metrics_ports": metrics_ports(plan),
    }


# -- every template at least parses ---------------------------------------


def test_every_role_template_parses(jinja_env):
    # A cheap floor under the targeted tests below: a template with no
    # test of its own (or a branch none of them reach) still can't be
    # committed with a Jinja syntax error in it.
    found = sorted(REPO_ROOT.glob("roles/*/templates/*.j2"))
    assert found, "no role templates found -- did the layout change?"
    for path in found:
        jinja_env.parse(path.read_text(), filename=str(path))


# -- stortree-remote@.service.j2 (layer 1, transport) ---------------------


def test_remote_unit_top_level_subtree_is_ordered_after_the_network_only(
    render, mount_plans, containers
):
    unit = render(
        REMOTE_UNIT,
        entry=transport_for(mount_plans[ALPHA], "tree"),
        **mount_vars(containers, ALPHA),
    )
    assert directives(unit, "After") == ["network-online.target"]
    assert "PartOf" not in unit


def test_remote_unit_mounts_under_the_remotes_root_not_the_tree(
    render, mount_plans, containers
):
    # Layer 1 lives outside stortree_root entirely. That is what keeps
    # bookkeeping names off the backend: the raw mount has somewhere of
    # its own to be, so nothing has to be staged next to the node it
    # serves.
    unit = render(
        REMOTE_UNIT,
        entry=transport_for(mount_plans[ALPHA], "tree"),
        **mount_vars(containers, ALPHA),
    )
    assert "/srv/.stortree-remotes/tree" in unit
    assert " /srv/stortree/tree " not in unit


def test_remote_unit_runs_as_the_stortree_service_account(
    render, mount_plans, containers
):
    unit = render(
        REMOTE_UNIT,
        entry=transport_for(mount_plans[ALPHA], "tree"),
        **mount_vars(containers, ALPHA),
    )
    assert "User=stortree" in unit
    assert "Group=stortree" in unit
    assert "Type=notify" in unit


def test_remote_unit_presents_one_uniform_ownership_and_enforces_it(
    render, mount_plans, containers
):
    # A transport carries no `access`-derived ownership at all: that is
    # layer 2's entire job, and doing it in both places is how the two
    # mechanisms used to disagree. 0700 because this is the raw view of
    # the backend, where every node is visible regardless of grant.
    #
    # --default-permissions is what makes the kernel check that mode.
    # Without it FUSE skips permission checking, rclone permits
    # everything, and the mode is decorative -- which is how any local
    # account could read any path in the tree.
    unit = render(
        REMOTE_UNIT,
        entry=transport_for(mount_plans[BRAVO], "tree/home/.mounts/whitfield-media"),
        **mount_vars(containers, BRAVO),
    )
    assert "--dir-perms 0700 \\" in unit
    assert "--file-perms 0600 \\" in unit
    assert "--default-permissions \\" in unit
    assert "--uid" not in unit
    assert "--gid" not in unit
    # Still needed despite 0700: without it root -- Ansible, which
    # creates every directory in this tree -- cannot stat the mountpoint,
    # and rclone ignores --allow-root.
    assert "--allow-other \\" in unit


def test_remote_unit_nests_inside_the_transport_above_it(
    render, mount_plans, containers
):
    # The remotes root mirrors the tree, so a transport sits inside
    # whichever transport is above it and a remount of the outer one
    # detaches it -- PartOf=, exactly as in the visible tree.
    unit = render(
        REMOTE_UNIT,
        entry=transport_for(mount_plans[BRAVO], "tree/home/.mounts/whitfield-media"),
        **mount_vars(containers, BRAVO),
    )
    assert "After=stortree-remote@tree.service" in unit
    assert "PartOf=stortree-remote@tree.service" in unit
    assert (
        "RequiresMountsFor=/srv/.stortree-remotes/tree/home/.mounts/whitfield-media"
        in unit
    )


def test_remote_unit_declared_requires_is_hard_and_never_partof(
    render, mount_plans, containers
):
    # A declared `requires` is a backend dependency -- a cache directory
    # that has to be mounted before the mount writing into it -- so it
    # attaches to layer 1. Requires=, not PartOf=: a cache blip should
    # not tear down a whole subtree.
    entry = transport_for(mount_plans[BRAVO], "tree")
    unit = render(REMOTE_UNIT, entry=entry, **mount_vars(containers, BRAVO))
    assert "Requires=stortree-remote@.bravo\\x2dcache.service" in unit
    assert "PartOf=stortree-remote@.bravo\\x2dcache.service" not in unit


def test_remote_unit_executes_the_configured_rclone_binary(
    render, mount_plans, containers
):
    # stortree_rclone_install: upstream puts a pinned build at
    # /usr/local/bin and leaves the distro's at /usr/bin, so the unit
    # has to name the one it means. A hardcoded /usr/bin/rclone here
    # would run 1.60.1 on a host that was upgraded precisely to stop
    # doing that -- and the metrics flavour, decided from the *other*
    # binary's version, would then render a flag this one rejects,
    # taking the mount down rather than merely mis-versioning it.
    entry = transport_for(mount_plans[ALPHA], "tree")
    common = mount_vars(containers, ALPHA)

    apt = render(REMOTE_UNIT, entry=entry, stortree_rclone_bin="/usr/bin/rclone", **common)
    upstream = render(
        REMOTE_UNIT, entry=entry, stortree_rclone_bin="/usr/local/bin/rclone", **common
    )

    assert "ExecStart=/usr/bin/rclone mount " in apt
    assert "ExecStart=/usr/local/bin/rclone mount " in upstream
    assert "/usr/bin/rclone" not in upstream


def test_remote_unit_flattens_rclone_args_one_per_line(
    render, mount_plans, containers
):
    unit = render(
        REMOTE_UNIT,
        entry=transport_for(mount_plans[BRAVO], "tree"),
        **mount_vars(containers, BRAVO),
    )
    assert "  --vfs-cache-mode full \\" in unit
    assert "  --dir-cache-time 5m \\" in unit


def test_remote_unit_renders_a_boolean_true_arg_as_a_bare_flag(
    render, mount_plans, containers
):
    entry = dict(transport_for(mount_plans[ALPHA], "tree"), args={"read-only": True})
    unit = render(REMOTE_UNIT, entry=entry, **mount_vars(containers, ALPHA))
    assert "  --read-only \\" in unit


def test_remote_unit_stop_is_tolerant_of_an_already_gone_mountpoint(
    render, mount_plans, containers
):
    unit = render(
        REMOTE_UNIT,
        entry=transport_for(mount_plans[ALPHA], "tree"),
        **mount_vars(containers, ALPHA),
    )
    assert "mountpoint -q" in unit
    assert "|| exit 0" in unit


# -- metrics endpoints on the transport unit ------------------------------


def test_remote_unit_renders_nothing_about_metrics_when_they_are_off(
    render, mount_plans, containers
):
    unit = render(
        REMOTE_UNIT,
        entry=transport_for(mount_plans[ALPHA], "tree"),
        **mount_vars(containers, ALPHA),
    )
    assert "--metrics-addr" not in unit
    assert "--rc" not in unit


def test_remote_unit_renders_one_metrics_addr_per_listener(
    render, mount_plans, containers
):
    # Both flags may be repeated, which is what makes a list of bind
    # addresses expressible at all -- and why this can't live in
    # `rclone.args`, a dict, one key one value.
    plan = mount_plans[ALPHA]
    entry = transport_for(plan, "tree")
    unit = render(
        REMOTE_UNIT,
        entry=entry,
        **metrics_vars(plan, bind=("127.0.0.1", "wg0")),
    )
    port = metrics_ports(plan)[entry["slug"]]
    assert f"  --metrics-addr 127.0.0.1:{port} \\" in unit
    assert f"  --metrics-addr 10.10.0.4:{port} \\" in unit
    # Nothing of the rc API on this flavour: counters only, no
    # config/dump.
    assert "--rc" not in unit


def test_remote_unit_gives_every_mount_on_a_host_its_own_port(
    render, mount_plans, containers
):
    # The failure this prevents is not a missing counter: rclone exits
    # when it cannot bind, and a Type=notify unit that exits is a mount
    # that never comes up.
    plan = mount_plans[BRAVO]
    ports = metrics_ports(plan)
    assert len(set(ports.values())) == len(ports) > 1

    rendered = [
        render(REMOTE_UNIT, entry=entry, **metrics_vars(plan))
        for entry in plan
        if entry["kind"] == "transport"
    ]
    listened = [
        line.split()[-2].rsplit(":", 1)[-1]
        for unit in rendered
        for line in unit.splitlines()
        if "--metrics-addr" in line
    ]
    assert len(set(listened)) == len(listened)


def test_remote_unit_orders_itself_after_an_interface_it_binds_to(
    render, mount_plans, containers
):
    # rclone binds at startup and exits if the address isn't there yet.
    # Wants=, never Requires=: a Requires= on an absent .device unit
    # fails the start job outright and systemd does not retry that --
    # Restart=on-failure covers a process that died, not a dependency
    # that was missing -- leaving the mount down until someone restarts
    # it by hand.
    plan = mount_plans[ALPHA]
    unit = render(
        REMOTE_UNIT,
        entry=transport_for(plan, "tree"),
        **metrics_vars(plan, bind=("wg0",)),
    )
    device = "sys-subsystem-net-devices-wg0.device"
    assert device in directives(unit, "After")
    assert device in directives(unit, "Wants")
    assert device not in directives(unit, "Requires")


def test_remote_unit_orders_itself_after_nothing_for_a_literal_address(
    render, mount_plans, containers
):
    # A literal address is nothing systemd can wait on.
    plan = mount_plans[ALPHA]
    unit = render(
        REMOTE_UNIT,
        entry=transport_for(plan, "tree"),
        **metrics_vars(plan, bind=("10.10.0.4",)),
    )
    assert "sys-subsystem-net-devices" not in unit


def test_remote_unit_rc_flavour_always_carries_its_htpasswd(
    render, mount_plans, containers
):
    # The older spelling serves /metrics on the rc port, and the rc port
    # also serves config/dump -- this host's scoped rclone.conf,
    # credentials and all. The role refuses to render this flavour
    # without an htpasswd (see its own assert); the template must
    # actually place the flag when it does.
    plan = mount_plans[ALPHA]
    entry = transport_for(plan, "tree")
    unit = render(
        REMOTE_UNIT,
        entry=entry,
        **metrics_vars(plan, mode="rc", htpasswd="/etc/stortree/metrics.htpasswd"),
    )
    port = metrics_ports(plan)[entry["slug"]]
    assert "  --rc \\" in unit
    assert "  --rc-enable-metrics \\" in unit
    assert "  --rc-htpasswd /etc/stortree/metrics.htpasswd \\" in unit
    assert f"  --rc-addr 127.0.0.1:{port} \\" in unit
    assert "--metrics-addr" not in unit


def test_remote_unit_keeps_the_config_flag_last_whatever_metrics_render(
    render, mount_plans, containers
):
    # Every rendered ExecStart line but the last one ends in a
    # continuation; a metrics block appended after --config would end
    # the command early and silently mount with no config file.
    plan = mount_plans[ALPHA]
    unit = render(
        REMOTE_UNIT,
        entry=transport_for(plan, "tree"),
        **metrics_vars(plan, bind=("127.0.0.1", "wg0")),
    )
    exec_start = unit.split("ExecStart=")[1].split("ExecStop=")[0].rstrip("\n")
    assert exec_start.rstrip().endswith("--config /etc/stortree/rclone.conf")
    assert all(
        line.rstrip().endswith("\\")
        for line in exec_start.splitlines()[:-1]
    )


# -- metrics-targets.json.j2 ----------------------------------------------


def targets(render, plan, host, **overrides):
    import json

    return json.loads(
        render(
            METRICS_TARGETS,
            inventory_hostname=host,
            stortree_mounts_plan=plan,
            **metrics_vars(plan, **overrides),
        )
    )


def test_metrics_fragment_lists_one_object_per_transport(
    render, mount_plans, containers
):
    plan = mount_plans[BRAVO]
    rows = targets(render, plan, BRAVO)
    assert len(rows) == len([e for e in plan if e["kind"] == "transport"])
    assert {row["labels"]["stortree_host"] for row in rows} == {BRAVO}


def test_metrics_fragment_labels_the_unit_an_alert_would_have_to_name(
    render, mount_plans, containers
):
    # The slug is not derivable from the path by eye (systemd escaping),
    # and it is exactly what whoever is woken up has to type after
    # `systemctl status stortree-remote@`.
    plan = mount_plans[ALPHA]
    (row,) = [r for r in targets(render, plan, ALPHA) if r["labels"]["stortree_node"] == "tree"]
    entry = transport_for(plan, "tree")
    assert row["labels"]["stortree_slug"] == entry["slug"]
    assert row["labels"]["stortree_remote"] == entry["remote"]
    assert row["targets"] == [f"127.0.0.1:{metrics_ports(plan)[entry['slug']]}"]


def test_metrics_fragment_prefers_an_address_a_scraper_can_reach(
    render, mount_plans, containers
):
    # With both bound, publishing the loopback target too would just
    # double-scrape the same process through an address no other host
    # can reach.
    plan = mount_plans[ALPHA]
    rows = targets(render, plan, ALPHA, bind=("127.0.0.1", "wg0"))
    assert all(
        target.startswith("10.10.0.4:") for row in rows for target in row["targets"]
    )


def test_metrics_fragment_still_lists_a_loopback_only_host(
    render, mount_plans, containers
):
    # Reachable from an agent on the host or through a tunnel. Emitting
    # nothing would be indistinguishable from metrics being switched
    # off.
    plan = mount_plans[ALPHA]
    rows = targets(render, plan, ALPHA)
    assert rows and all(row["targets"] for row in rows)


# -- stortree-mount@.service.j2 (layer 2, presentation) -------------------


def test_mount_unit_reads_the_remotes_root_and_writes_the_visible_path(
    render, mount_plans, containers
):
    # Source and target are the same directory on the backend reached by
    # two different local paths -- which is what makes this need no data
    # movement and be unable to self-mount.
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[ALPHA], "tree"),
        **mount_vars(containers, ALPHA),
    )
    assert (
        "ExecStart=/usr/bin/bindfs /srv/.stortree-remotes/tree /srv/stortree/tree \\"
        in unit
    )
    assert "Type=forking" in unit
    assert "Type=notify" not in unit


def test_mount_unit_requires_its_own_transport(render, mount_plans, containers):
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[ALPHA], "tree"),
        **mount_vars(containers, ALPHA),
    )
    assert "After=stortree-remote@tree.service" in unit
    assert "Requires=stortree-remote@tree.service" in unit
    assert "PartOf=stortree-remote@tree.service" in unit


def test_mount_unit_orders_after_the_deepest_presentation_above_it(
    render, mount_plans, containers
):
    # A per-user leaf nests inside its own container's presentation, not
    # merely inside the top-level subtree -- the container is what puts
    # this mountpoint on screen. Found by searching for the deepest
    # presented ancestor rather than looking at the immediate parent.
    entry = entry_for(mount_plans[BRAVO], "tree/home/jd/sys-configs")
    assert entry["requires_slug"] == "tree-home-jd"
    unit = render(MOUNT_UNIT, entry=entry, **mount_vars(containers, BRAVO))
    assert "After=stortree-mount@tree-home-jd.service" in unit
    assert "PartOf=stortree-mount@tree-home-jd.service" in unit
    assert "RequiresMountsFor=/srv/stortree/tree/home/jd" in unit


def test_mount_unit_owner_grant_pins_uid_and_perms(
    render, mount_plans, containers
):
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[BRAVO], "tree/home/jd/sys-configs"),
        **mount_vars(containers, BRAVO),
    )
    assert "-u 10001 \\" in unit  # jd, per stortree_user_uids
    # access_mode 0701 -> files 0600, directories 0701. Handing the mode
    # to -p unchanged would mark every regular file executable.
    assert "-p 0600,uo+X \\" in unit
    # An owner-only grant says nothing about the group, so the mount
    # keeps the mounting account's own gid rather than inventing one.
    assert "-g 900 \\" in unit


def test_mount_unit_group_grant_pins_gid_and_not_uid(
    render, mount_plans, containers
):
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[BRAVO], "tree/home/.mounts/whitfield-media"),
        **mount_vars(containers, BRAVO),
    )
    assert "-g 20001 \\" in unit
    assert "-u 900 \\" in unit


def test_mount_unit_ungranted_mount_still_presents_the_plain_default(
    render, mount_plans, containers
):
    # No grant at all: the mounting account's own uid/gid and the plain
    # 0751 default, which is exactly what an ungranted local directory
    # already gets. `other` keeps a traversal-only execute bit so a
    # deeper grant stays reachable.
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[ALPHA], "tree"),
        **mount_vars(containers, ALPHA),
    )
    assert "-u 900 \\" in unit
    assert "-g 900 \\" in unit
    assert "-p 0640,ugo+X \\" in unit


def test_mount_unit_mirrors_the_service_account_so_nested_mounts_can_start(
    render, mount_plans, containers
):
    # Without --mirror this mount presents its path as the granted owner
    # to everyone, and fusermount then refuses any nested mount beneath
    # it ("user has no write access to mountpoint") because the mounting
    # account no longer appears to own the path it is mounting on.
    # Verified on a real host, both directions.
    #
    # The tree's previous answer was to force any mount with nested
    # children to stortree:stortree and discard its grant -- which is the
    # bug this layer exists to fix.
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[BRAVO], "tree/home/jd/sys-configs"),
        **mount_vars(containers, BRAVO),
    )
    assert "--mirror=stortree" in unit


def test_mount_unit_is_presentation_only_and_tolerates_a_non_empty_mountpoint(
    render, mount_plans, containers
):
    # All three ignore policies, because bindfs treats chown and chgrp
    # separately and --chown-ignore alone still lets a chgrp through to
    # the transport, where it could not have persisted anyway.
    #
    # nonempty is required, not defensive: Debian's bindfs links libfuse2,
    # which refuses a non-empty mountpoint outright, and a presented
    # node's mountpoint routinely is one.
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[ALPHA], "tree"),
        **mount_vars(containers, ALPHA),
    )
    assert "--chown-ignore" in unit
    assert "--chgrp-ignore" in unit
    assert "--chmod-ignore" in unit
    assert "-o allow_other,nonempty \\" in unit


def test_mount_unit_stop_is_tolerant_of_an_already_gone_mountpoint(
    render, mount_plans, containers
):
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[ALPHA], "tree"),
        **mount_vars(containers, ALPHA),
    )
    assert "mountpoint -q" in unit
    assert "|| exit 0" in unit


# -- stortree-bind@.service.j2 --------------------------------------------


def test_bind_unit_depends_on_both_its_source_and_its_container(
    render, mount_plans, containers
):
    # Two edges: the presentation it binds *from*, and the presentation
    # its own mountpoint lives *inside*. A oneshot+RemainAfterExit unit
    # reports active forever once it has run, so either detaching without
    # tearing this down leaves the path silently reverted.
    entry = entry_for(mount_plans[BRAVO], "tree/home/mw/mw-fam")
    unit = render(BIND_UNIT, entry=entry, **mount_vars(containers, BRAVO))
    src = "stortree-mount@tree-home-.mounts-mw\\x2dfam.service"
    container = "stortree-mount@tree-home-mw.service"
    assert f"After={src}" in unit
    assert f"PartOf={src}" in unit
    assert f"After={container}" in unit
    assert f"PartOf={container}" in unit


def test_bind_unit_binds_the_shared_mount_onto_the_per_user_path(
    render, mount_plans, containers
):
    entry = entry_for(mount_plans[BRAVO], "tree/home/mw/mw-fam")
    unit = render(BIND_UNIT, entry=entry, **mount_vars(containers, BRAVO))
    assert (
        "ExecStart=/bin/mount --bind /srv/stortree/tree/home/.mounts/mw-fam "
        "/srv/stortree/tree/home/mw/mw-fam" in unit
    )


def test_bind_unit_stop_tolerates_an_already_unmounted_path(
    render, mount_plans, containers
):
    entry = entry_for(mount_plans[BRAVO], "tree/home/mw/mw-fam")
    unit = render(BIND_UNIT, entry=entry, **mount_vars(containers, BRAVO))
    assert "ExecStop=-/bin/sh -c 'mountpoint -q" in unit


def test_bind_unit_names_no_source_unit_when_the_source_is_a_plain_directory(
    render, mount_plans, containers
):
    # A `group`-only node collapses to one shared mount at
    # `.mounts/<name>` plus a bind per member -- but on the host that
    # owns the subtree, with no `rclone` anywhere above it, that shared
    # path is an ordinary local directory: its grant is a real chown, so
    # nothing renders a `stortree-mount@` unit for it.
    #
    # Deriving the source unit's name by slugging `symlink_target` named
    # one anyway, and a `Requires=` on a unit that does not exist is not
    # a dependency that goes unmet later -- systemd refuses to start the
    # bind at all ("Unit stortree-mount@... not found"), so both members
    # lost the folder over an edge that was never meaningful. Production
    # hit this the first time such a node reached a fleet.
    entry = dict(
        entry_for(mount_plans[BRAVO], "tree/home/mw/mw-fam"),
        symlink_target_slug=None,
    )
    unit = render(BIND_UNIT, entry=entry, **mount_vars(containers, BRAVO))

    assert "stortree-mount@tree-home-.mounts-mw\x2dfam.service" not in unit
    # The container edge is a separate question and still applies...
    assert "PartOf=stortree-mount@tree-home-mw.service" in unit
    # ...as does needing the filesystem the source actually lives on.
    assert (
        "RequiresMountsFor=/srv/stortree/tree/home/.mounts/mw-fam" in unit
    )
    # And the bind itself is unchanged -- only the dependency was wrong.
    assert (
        "ExecStart=/bin/mount --bind /srv/stortree/tree/home/.mounts/mw-fam "
        "/srv/stortree/tree/home/mw/mw-fam" in unit
    )


# -- smb.conf.j2 ----------------------------------------------------------


@pytest.fixture(scope="session")
def smb_conf(render, resolved):
    # The gadget owns no subtree at all yet still exports the full share
    # -- Samba sharing is universal (docs/config-schema.md).
    return render(SMB_CONF, stortree=resolved[GADGET])


def test_smb_conf_share_name_is_sanitized_from_the_node_path(smb_conf):
    # "tree/home" isn't a legal share name; the slash has to go. The
    # fold happens in resolve() now, but this is still the name the
    # worked example's one share ends up exported under.
    assert "[tree_home]" in smb_conf
    assert "[tree/home]" not in smb_conf


def test_smb_conf_section_header_is_the_resolved_share_name(render):
    # `samba.name` reaches the stanza header, and nothing else: the path
    # is still the node's real one (docs/config-schema.md "Share names").
    rendered = render(
        SMB_CONF,
        stortree={
            "samba_shares": [
                {
                    "node_path": "tree/home",
                    "name": "home",
                    "subpath": "%U",
                    "hidden": False,
                    "access": [{"group": "ops", "permissions": "rwx"}],
                }
            ]
        },
    )
    assert "[home]" in rendered
    assert "[tree_home]" not in rendered
    assert "path = /srv/stortree/tree/home/%U" in rendered


def test_smb_conf_share_path_appends_the_subpath_template(smb_conf):
    assert "path = /srv/stortree/tree/home/%U" in smb_conf


def test_smb_conf_quotes_principals_so_a_name_with_spaces_stays_one_token(smb_conf):
    # smb.conf(5) lists are whitespace-delimited, and the worked example
    # has two group names with spaces in them.
    (valid_users,) = [
        line for line in smb_conf.splitlines() if "valid users =" in line
    ]
    assert '"@Whitfield Family & Friends"' in valid_users
    assert '"@Michael Whitfield Family"' in valid_users
    assert '"@Media Production"' in valid_users
    assert '"jd"' in valid_users


def test_smb_conf_a_percent_u_share_always_admits_the_connecting_user(smb_conf):
    # The share path already confines each user to their own subtree, so
    # their baseline access there can't depend on some descendant's
    # grant happening to exist (spec.md §6).
    (valid_users,) = [
        line for line in smb_conf.splitlines() if "valid users =" in line
    ]
    assert valid_users.split("=", 1)[1].strip().startswith('"%U"')


def test_smb_conf_write_list_excludes_a_read_only_grant(smb_conf):
    # "Whitfield Family & Friends" has `permissions: rx` -- readable,
    # not writable. Everything else in the example is rwx.
    (write_list,) = [line for line in smb_conf.splitlines() if "write list =" in line]
    assert '"@Whitfield Family & Friends"' not in write_list
    assert '"@Michael Whitfield Family"' in write_list
    assert '"@Media Production"' in write_list
    assert '"jd"' in write_list


def test_smb_conf_an_ordinary_share_emits_no_browseable_directive(smb_conf):
    # The default is Samba's own, left unwritten -- adding `browseable =
    # yes` everywhere would change nothing but the diff.
    assert "browseable" not in smb_conf


def test_smb_conf_a_hidden_share_is_not_browseable(render):
    rendered = render(
        SMB_CONF,
        stortree={
            "samba_shares": [
                {
                    "node_path": "tree/spool",
                    "name": "spool",
                    "subpath": None,
                    "hidden": True,
                    "access": [{"owner": "svc", "permissions": "rwx"}],
                }
            ]
        },
    )
    assert "browseable = no" in rendered
    # Hiding is not access control: the grant is still exported.
    assert 'valid users = "svc"' in rendered


def test_smb_conf_global_section_maps_no_one_to_guest(smb_conf):
    # Access is real Unix ownership; a guest mapping would route around
    # it entirely.
    assert "security = user" in smb_conf
    assert "map to guest = never" in smb_conf


def test_smb_conf_with_no_shares_is_still_a_valid_global_only_config(render):
    rendered = render(SMB_CONF, stortree={"samba_shares": []})
    assert "[global]" in rendered
    assert "valid users" not in rendered


def test_smb_conf_share_without_a_subpath_uses_the_node_path_bare(render):
    rendered = render(
        SMB_CONF,
        stortree={
            "samba_shares": [
                {
                    "node_path": "tree/backups",
                    "name": "tree_backups",
                    "subpath": None,
                    "hidden": False,
                    "access": [{"group": "ops", "permissions": "rwx"}],
                }
            ]
        },
    )
    assert "path = /srv/stortree/tree/backups\n" in rendered
    # No %U in the path, so no standing %U entry in valid users either.
    (valid_users,) = [
        line for line in rendered.splitlines() if "valid users =" in line
    ]
    assert "%U" not in valid_users
    assert '"@ops"' in valid_users


# -- sssd.conf.j2 ---------------------------------------------------------


LDAP_MIN = {
    "server": {
        "url": "ldaps://ldap.example.internal:636",
        "base_dn": "dc=example,dc=internal",
        "bind_dn": "cn=stortree,ou=service-accounts,dc=example,dc=internal",
        "bind_password": "CHANGEME",
    },
    "posix": {"uid_attr": "uidNumber", "gid_attr": "gidNumber"},
}


def ini_pairs(rendered, section):
    """The `key = value` pairs of one section of a rendered ini file."""
    out = {}
    in_section = False
    for line in rendered.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_section = line == f"[{section}]"
            continue
        if in_section and " = " in line:
            k, v = line.split(" = ", 1)
            out[k] = v
    return out


def test_sssd_conf_renders_without_an_extra_key_at_all(render):
    # ldap.yml.example ships `extra:` fully commented out, so the
    # common case is a config that simply has no such key.
    rendered = render(SSSD_CONF, stortree_identity_ldap=LDAP_MIN)
    assert ini_pairs(rendered, "sssd") == {
        "services": "nss, pam",
        "domains": "stortree",
    }
    domain = ini_pairs(rendered, "domain/stortree")
    assert domain["id_provider"] == "ldap"
    assert domain["ldap_uri"] == "ldaps://ldap.example.internal:636"
    assert domain["ldap_search_base"] == "dc=example,dc=internal"
    assert domain["ldap_user_uid_number"] == "uidNumber"
    assert domain["ldap_group_gid_number"] == "gidNumber"
    assert domain["cache_credentials"] == "true"
    assert domain["enumerate"] == "false"


def test_sssd_conf_leaves_ldap_id_mapping_unset(render):
    # Deliberate: unset means SSSD reads the directory's real POSIX
    # attributes instead of synthesizing ids (spec.md §5). Setting it
    # would give every host its own made-up, mutually inconsistent uids.
    rendered = render(SSSD_CONF, stortree_identity_ldap=LDAP_MIN)
    # The header comment mentions it by name, so check the directives
    # themselves rather than the raw text.
    assert "ldap_id_mapping" not in ini_pairs(rendered, "domain/stortree")
    assert "ldap_id_mapping" not in ini_pairs(rendered, "sssd")


def test_sssd_conf_extra_overrides_a_default_rather_than_duplicating_it(render):
    rendered = render(
        SSSD_CONF,
        stortree_identity_ldap=dict(
            LDAP_MIN, extra={"sssd": {"services": "nss, pam, ssh"}}
        ),
    )
    assert ini_pairs(rendered, "sssd")["services"] == "nss, pam, ssh"
    assert rendered.count("services = ") == 1


def test_sssd_conf_extra_domain_merges_over_the_built_in_domain_section(render):
    rendered = render(
        SSSD_CONF,
        stortree_identity_ldap=dict(
            LDAP_MIN,
            extra={
                "domain": {
                    "ldap_tls_reqcert": "demand",
                    "cache_credentials": "false",
                }
            },
        ),
    )
    domain = ini_pairs(rendered, "domain/stortree")
    assert domain["ldap_tls_reqcert"] == "demand"
    assert domain["cache_credentials"] == "false"  # overridden, not duplicated
    assert rendered.count("cache_credentials = ") == 1
    assert domain["id_provider"] == "ldap"  # untouched default survives


def test_sssd_conf_any_other_extra_key_becomes_its_own_section(render):
    rendered = render(
        SSSD_CONF,
        stortree_identity_ldap=dict(
            LDAP_MIN,
            extra={
                "sssd": {"services": "nss, pam, ssh"},
                "domain": {"ldap_tls_reqcert": "demand"},
                "ssh": {"ssh_hash_known_hosts": "false"},
            },
        ),
    )
    assert "[ssh]" in rendered
    assert ini_pairs(rendered, "ssh") == {"ssh_hash_known_hosts": "false"}
    # "sssd"/"domain" are special-cased and must not also be emitted as
    # standalone sections named after themselves.
    assert "[domain]\n" not in rendered
    assert rendered.count("[sssd]") == 1


# `stortree_samba_globals` (roles/stortree_samba/defaults/main.yml) is the
# [global] escape hatch -- the same shape ldap.yml's `extra:` gives
# sssd.conf, and the only way to set a per-site directive (`workgroup`
# above all) without forking this template.


def test_smb_conf_globals_render_without_an_override(smb_conf):
    # The built-in defaults, unchanged when the operator sets nothing --
    # the `| default({})` has to hold, since no role variable defines
    # this on a play that never overrode it.
    assert "workgroup = WORKGROUP" in smb_conf
    assert "passdb backend = tdbsam" in smb_conf


def test_smb_conf_global_pins_smb1_off_explicitly(smb_conf):
    # Matches what Samba >= 4.11 already defaults to, so it changes
    # nothing on any platform meta/main.yml lists -- it's here so the
    # posture is visible in the rendered file and a future distro default
    # can't quietly lower it.
    assert "server min protocol = SMB2_02" in smb_conf


def test_smb_conf_globals_override_replaces_a_builtin_rather_than_duplicating_it(
    render, resolved
):
    out = render(
        SMB_CONF,
        stortree=resolved[GADGET],
        stortree_samba_globals={"workgroup": "EXAMPLE"},
    )
    assert "workgroup = EXAMPLE" in out
    # The whole point of `combine` over appending: exactly one workgroup
    # line survives, or smb.conf has two and the last one silently wins.
    assert len([ln for ln in out.splitlines() if ln.startswith("workgroup =")]) == 1
    assert "workgroup = WORKGROUP" not in out


def test_smb_conf_globals_can_add_a_directive_stortree_does_not_model(
    render, resolved
):
    out = render(
        SMB_CONF,
        stortree=resolved[GADGET],
        stortree_samba_globals={"server string": "%h (stortree)"},
    )
    assert "server string = %h (stortree)" in out
    # ...without disturbing the built-ins it says nothing about.
    assert "workgroup = WORKGROUP" in out


def test_smb_conf_globals_stay_inside_the_global_section(render, resolved):
    # An override must never leak past the first share header, or it
    # silently becomes a per-share setting for whichever stanza follows.
    out = render(
        SMB_CONF,
        stortree=resolved[GADGET],
        stortree_samba_globals={"workgroup": "EXAMPLE"},
    )
    assert "[global]" in out
    global_block = out.split("[global]", 1)[1].split("\n[", 1)[0]
    assert "workgroup = EXAMPLE" in global_block


# -- host discovery: wsdd.service.j2 --------------------------------------
#
# What puts the *host* on the network, as opposed to what decides which
# of its shares a client then sees (`browseable`, above) or who may open
# them (`valid users`, above that). These two templates are the only
# part of stortree that is about being found at all.

# What roles/stortree_samba/tasks/main.yml has in scope by the time it
# renders the unit: the implementation it settled on, the binary path it
# just discovered for that implementation, the workgroup resolved from
# the role defaults, and the operator's interface list as
# defaults/main.yml leaves it.
DISCOVERY_VARS = {
    "stortree_samba_wsdd_flavour": "wsdd",
    "stortree_samba_wsdd_bin": "/usr/sbin/wsdd",
    "stortree_samba_workgroup": "WORKGROUP",
    "stortree_samba_wsd_interfaces": [],
}

# The other daemon, which Debian trixie packages in place of `wsdd`
# after dropping it. Same template, an entirely different [Service]
# block -- see the template's header.
WSDD2_VARS = {
    "stortree_samba_wsdd_flavour": "wsdd2",
    "stortree_samba_wsdd_bin": "/usr/sbin/wsdd2",
}


def wsdd(render, **overrides):
    return render(WSDD_UNIT, **{**DISCOVERY_VARS, **overrides})


def wsdd2(render, **overrides):
    return wsdd(render, **{**WSDD2_VARS, **overrides})


@pytest.fixture(scope="session")
def wsdd_unit(render):
    return wsdd(render)


@pytest.fixture(scope="session")
def wsdd2_unit(render):
    return wsdd2(render)


def test_wsdd_unit_runs_the_binary_the_role_actually_found(render):
    # noble installs /usr/bin/wsdd where bookworm and jammy install
    # /usr/sbin/wsdd. The role discovers which; a unit that assumed
    # either would fail every start on the other platform with
    # status=203/EXEC and no host in anyone's Network.
    (exec_start,) = directives(
        wsdd(render, stortree_samba_wsdd_bin="/usr/bin/wsdd"), "ExecStart"
    )
    assert exec_start.startswith("/usr/bin/wsdd ")


def test_wsdd_unit_announces_the_workgroup_smb_conf_serves(render, jinja_env, resolved):
    # The seam the shared `stortree_samba_global_defaults` exists to
    # close: [global]'s workgroup and the announcer's --workgroup are
    # two readers of one dict, and drift between them is silent in the
    # worst way -- a host serving EXAMPLE while announcing itself into
    # WORKGROUP is discoverable by nobody who is looking in the right
    # place. Renders the defaults file's own expression rather than
    # restating it, so this fails if that expression stops following an
    # override.
    defaults = yaml.safe_load(
        (REPO_ROOT / "roles/stortree_samba/defaults/main.yml").read_text()
    )
    overrides = {"workgroup": "EXAMPLE"}
    workgroup = jinja_env.from_string(defaults["stortree_samba_workgroup"]).render(
        stortree_samba_global_defaults=defaults["stortree_samba_global_defaults"],
        stortree_samba_globals=overrides,
    )
    conf = render(SMB_CONF, stortree=resolved[GADGET], stortree_samba_globals=overrides)
    (exec_start,) = directives(wsdd(render, stortree_samba_workgroup=workgroup), "ExecStart")
    assert "workgroup = EXAMPLE" in conf
    assert '--workgroup "EXAMPLE"' in exec_start


def test_wsdd_unit_quotes_the_workgroup_as_a_single_argument(render):
    # A legal NetBIOS workgroup has no space in it, but nothing upstream
    # of `stortree_samba_globals` enforces that, and systemd splits an
    # unquoted argument on whitespace -- which would announce the host
    # into "TWO" and pass "WORDS" as a flag.
    (exec_start,) = directives(
        wsdd(render, stortree_samba_workgroup="TWO WORDS"), "ExecStart"
    )
    assert '--workgroup "TWO WORDS"' in exec_start


def test_wsdd_unit_announces_on_every_interface_by_default(wsdd_unit):
    # wsdd's own default, and right for a host with one network: naming
    # interfaces is what a multi-homed host does, not what everyone does.
    # Asserted against ExecStart rather than the whole file: the header
    # names both daemons' interface flags while explaining the
    # difference between them.
    (exec_start,) = directives(wsdd_unit, "ExecStart")
    assert "--interface" not in exec_start


def test_wsdd_unit_restricts_itself_to_the_interfaces_it_was_given(render):
    (exec_start,) = directives(
        wsdd(render, stortree_samba_wsd_interfaces=["eth0", "wg0"]), "ExecStart"
    )
    assert "--interface=eth0" in exec_start
    assert "--interface=wg0" in exec_start


def test_wsdd_unit_stops_announcing_when_smbd_stops(wsdd_unit):
    # A host that appears in Explorer's Network and then errors when
    # clicked is worse than one that never appeared -- the person stops
    # looking elsewhere. Requires= propagates smbd's stop; After= keeps
    # the ordering.
    assert directives(wsdd_unit, "Requires") == ["smbd.service"]
    assert "smbd.service" in directives(wsdd_unit, "After")[0]


def test_wsdd_unit_runs_the_announcer_unprivileged(wsdd_unit):
    # It parses unauthenticated multicast from the local segment, and
    # both ports it binds are above 1024, so there is no privilege to
    # trade away for it.
    assert directives(wsdd_unit, "User") == ["nobody"]
    assert directives(wsdd_unit, "CapabilityBoundingSet") == [""]
    assert directives(wsdd_unit, "NoNewPrivileges") == ["yes"]


def test_wsdd_unit_keeps_the_address_family_it_watches_interfaces_with(wsdd_unit):
    # AF_NETLINK is not hardening slack: it is how wsdd learns which
    # interfaces exist and notices one appearing later. Dropping it
    # makes an announcer that works until the network changes.
    (families,) = directives(wsdd_unit, "RestrictAddressFamilies")
    assert "AF_NETLINK" in families
    assert "AF_INET" in families


# -- host discovery: the same unit, rendered for wsdd2 --------------------
#
# Debian dropped the Python `wsdd` after bookworm and packages the
# unrelated C `wsdd2` instead, so on trixie the role installs that and
# renders this template for it. The two daemons share no flag, no
# binary name, no unit name and no privilege model -- which is exactly
# why these need testing separately rather than being assumed to follow
# from the wsdd cases above.


def test_wsdd2_unit_announces_the_workgroup_with_its_own_flag(render):
    # -G is wsdd2's --workgroup. Rendering wsdd's long option here would
    # not be a wrong workgroup, it would be a daemon that refuses to
    # start -- and the same silent absence from Explorer's Network that
    # the whole shared-`workgroup` seam exists to prevent.
    (exec_start,) = directives(
        wsdd2(render, stortree_samba_workgroup="EXAMPLE"), "ExecStart"
    )
    assert exec_start.startswith("/usr/sbin/wsdd2 ")
    assert '-G "EXAMPLE"' in exec_start
    assert "--workgroup" not in exec_start
    assert "--shortlog" not in exec_start


def test_wsdd2_unit_quotes_the_workgroup_as_a_single_argument(render):
    (exec_start,) = directives(
        wsdd2(render, stortree_samba_workgroup="TWO WORDS"), "ExecStart"
    )
    assert '-G "TWO WORDS"' in exec_start


def test_wsdd2_unit_announces_on_every_interface_by_default(wsdd2_unit):
    (exec_start,) = directives(wsdd2_unit, "ExecStart")
    assert " -i " not in exec_start


def test_wsdd2_unit_restricts_itself_to_the_interface_it_was_given(render):
    # One interface, because wsdd2's -i is not repeatable. The role
    # refuses an interface list longer than one rather than silently
    # dropping the rest -- see the assert in tasks/main.yml, which is
    # what keeps this template from ever having to decide which one wins.
    (exec_start,) = directives(
        wsdd2(render, stortree_samba_wsd_interfaces=["eth0"]), "ExecStart"
    )
    assert exec_start.endswith("-i eth0")


def test_wsdd2_unit_keeps_the_capabilities_it_cannot_run_without(wsdd2_unit):
    # Where wsdd runs as `nobody` with an empty bounding set, wsdd2 binds
    # its sockets to a named device (SO_BINDTODEVICE, CAP_NET_RAW) and
    # reads the interface table over netlink (CAP_NET_ADMIN). Confining
    # it the way wsdd is confined is a daemon that starts and then
    # announces nothing.
    (ambient,) = directives(wsdd2_unit, "AmbientCapabilities")
    assert "CAP_NET_RAW" in ambient
    assert "CAP_NET_ADMIN" in ambient
    assert directives(wsdd2_unit, "User") == []
    assert directives(wsdd2_unit, "DynamicUser") == ["yes"]
    assert directives(wsdd2_unit, "NoNewPrivileges") == ["yes"]


def test_wsdd2_unit_stops_announcing_when_smbd_stops(wsdd2_unit):
    # The half of the [Unit] section that is shared between the two
    # daemons, asserted on both so a future edit cannot quietly move it
    # inside one branch.
    assert directives(wsdd2_unit, "Requires") == ["smbd.service"]
    assert "smbd.service" in directives(wsdd2_unit, "After")[0]


def test_wsdd2_unit_documents_the_daemon_it_actually_runs(wsdd2_unit, wsdd_unit):
    assert directives(wsdd2_unit, "Documentation") == ["man:wsdd2(8)"]
    assert directives(wsdd_unit, "Documentation") == ["man:wsdd(8)"]


# -- host discovery: stortree-smb.avahi.xml.j2 ----------------------------


@pytest.fixture(scope="session")
def avahi_service(render):
    return render(AVAHI_SERVICE, stortree_samba_mdns_model="RackMac")


def test_avahi_service_file_is_well_formed_xml(avahi_service):
    # Avahi rejects a malformed service file with a syslog line and
    # nothing else, so a typo here is a host that silently never reaches
    # a Finder sidebar. Nothing else in this repo renders XML.
    ET.fromstring(avahi_service)


def test_avahi_service_advertises_smb_on_the_port_samba_serves(avahi_service):
    root = ET.fromstring(avahi_service)
    published = {s.findtext("type"): s.findtext("port") for s in root.findall("service")}
    assert published["_smb._tcp"] == "445"


def test_avahi_service_publishes_under_the_hosts_own_name(avahi_service):
    # %h is Avahi's substitution, not Jinja's, and it only happens
    # because of replace-wildcards -- without the attribute every host on
    # the network advertises itself as a literal "%h".
    name = ET.fromstring(avahi_service).find("name")
    assert name.text == "%h"
    assert name.get("replace-wildcards") == "yes"


def test_avahi_service_omits_the_device_info_record_when_no_model_is_set(render):
    # The model is cosmetic (it picks the icon macOS draws), so a fleet
    # that wants no opinion about it publishes the SMB record alone
    # rather than an empty device-info one.
    out = render(AVAHI_SERVICE, stortree_samba_mdns_model=None)
    ET.fromstring(out)
    assert "_device-info._tcp" not in out
    assert "_smb._tcp" in out
