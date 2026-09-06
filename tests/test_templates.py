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
resolve()/plan_mounts()/user_container_paths() -- not hand-written
stand-ins -- so a change to a resolved entry's shape shows up here as a
failing render rather than at apply time.
"""

import pytest

from conftest import EXAMPLE_HOSTS, REPO_ROOT

MOUNT_UNIT = "stortree-mount@.service.j2"
BIND_UNIT = "stortree-bind@.service.j2"
USER_MOUNT_UNIT = "stortree-user-mount@.service.j2"
SMB_CONF = "smb.conf.j2"
SSSD_CONF = "sssd.conf.j2"

ALPHA, BRAVO, GADGET = EXAMPLE_HOSTS


def entry_for(plan, local_path):
    return next(e for e in plan if e["local_path"] == local_path)


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


def mount_vars(mount_plans, containers, parent_slugs, host):
    """The variable set roles/stortree_mounts/tasks/main.yml has in
    scope when it renders a unit template."""
    return {
        "stortree_user_containers": containers[host],
        "stortree_mounts_parent_slugs": parent_slugs[host],
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


# -- stortree-mount@.service.j2 -------------------------------------------


def test_mount_unit_top_level_subtree_is_ordered_after_the_network_only(
    render, mount_plans, containers, parent_slugs
):
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[ALPHA], "tree"),
        **mount_vars(mount_plans, containers, parent_slugs, ALPHA),
    )
    # `tree` on its owning host nests inside nothing and declares no
    # `requires` that resolves here, so network-online is its only
    # ordering edge -- and nothing may make it PartOf anything.
    assert directives(unit, "After") == ["network-online.target"]
    assert directives(unit, "PartOf") == []
    assert directives(unit, "Requires") == []
    assert (
        "ExecStart=/usr/bin/rclone mount storagebox:/ /srv/stortree/tree \\" in unit
    )
    assert "--config /etc/stortree/rclone.conf" in unit
    assert unit.startswith("[Unit]\n")
    assert unit.rstrip().endswith("WantedBy=multi-user.target")


def test_mount_unit_runs_as_the_stortree_service_account(
    render, mount_plans, containers, parent_slugs
):
    # Every nested mount depends on this being unconditional -- see the
    # "Note which mounts have another mount nested inside them" task in
    # roles/stortree_mounts: fusermount's same-owner check for a new
    # mount only passes against a parent the mounting user owns.
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[BRAVO], "tree/home/.mounts/whitfield-media"),
        **mount_vars(mount_plans, containers, parent_slugs, BRAVO),
    )
    assert "User=stortree" in unit
    assert "Group=stortree" in unit
    assert "Type=notify" in unit


def test_mount_unit_nested_mount_is_partof_the_mount_it_lives_inside(
    render, mount_plans, containers, parent_slugs
):
    # PartOf, not just After: a parent remount otherwise leaves this
    # rclone process alive over a detached mountpoint, with systemd
    # still reporting the unit active (the template's own comment).
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[BRAVO], "tree/home/.mounts/whitfield-media"),
        **mount_vars(mount_plans, containers, parent_slugs, BRAVO),
    )
    assert "After=stortree-mount@tree.service" in unit
    assert "PartOf=stortree-mount@tree.service" in unit
    assert (
        "RequiresMountsFor=/srv/stortree/tree/home/.mounts/whitfield-media" in unit
    )


def test_mount_unit_prefers_its_containers_wrapper_mount_over_the_outer_mount(
    render, mount_plans, containers, parent_slugs
):
    # tree/home/jd/sys-configs sits directly under the per-user
    # container tree/home/jd, which nests inside a remote-backed mount
    # and therefore got a wrapper mount of its own. This entry now lives
    # inside the *wrapper's* presented tree, so it must order after the
    # wrapper -- not after `tree`, which plan_mounts() computed as its
    # requires_slug.
    entry = entry_for(mount_plans[BRAVO], "tree/home/jd/sys-configs")
    assert entry["requires_slug"] == "tree"

    unit = render(
        MOUNT_UNIT,
        entry=entry,
        **mount_vars(mount_plans, containers, parent_slugs, BRAVO),
    )
    assert directives(unit, "After") == [
        "network-online.target",
        "stortree-user-mount@tree-home-jd.service",
    ]
    assert directives(unit, "PartOf") == ["stortree-user-mount@tree-home-jd.service"]
    assert "RequiresMountsFor=/srv/stortree/tree/home/jd" in unit
    assert "stortree-mount@tree.service" not in unit


def test_mount_unit_declared_requires_is_hard_and_never_partof(
    render, mount_plans, containers, parent_slugs
):
    # bravo's client mount of `tree` points its cache-dir into
    # .bravo-cache (docs/config-schema.md "Requires"). That's a hard
    # dependency -- without Requires= the mount starts with its cache
    # target missing and rclone fills the local disk under a path the
    # real mount later shadows -- but deliberately *not* PartOf: a cache
    # blip must not take the whole shared subtree down with it.
    entry = entry_for(mount_plans[BRAVO], "tree")
    assert [r["local_path"] for r in entry["requires_mounts"]] == [".bravo-cache"]

    unit = render(
        MOUNT_UNIT,
        entry=entry,
        **mount_vars(mount_plans, containers, parent_slugs, BRAVO),
    )
    cache_unit = "stortree-mount@.bravo\\x2dcache.service"
    assert f"After={cache_unit}" in unit
    assert f"Requires={cache_unit}" in unit
    assert directives(unit, "PartOf") == []
    assert "RequiresMountsFor=/srv/stortree/.bravo-cache" in unit


def test_mount_unit_flattens_rclone_args_one_per_line(
    render, mount_plans, containers, parent_slugs
):
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[GADGET], "tree"),
        **mount_vars(mount_plans, containers, parent_slugs, GADGET),
    )
    for expected in (
        "  --vfs-cache-mode full \\",
        "  --vfs-cache-max-age 100h \\",
        "  --dir-cache-time 5m \\",
        "  --vfs-cache-max-size 20G \\",
        "  --cache-dir /mnt/some-volume/.rclone-cache \\",
    ):
        assert expected in unit


def test_mount_unit_renders_a_boolean_true_arg_as_a_bare_flag(
    render, mount_plans, containers, parent_slugs
):
    # `rclone.args: {read-only: true}` is a valueless rclone flag, not
    # "--read-only True" (which rclone rejects). Nothing in the worked
    # example uses one, so this is the only place that pins it.
    entry = dict(
        entry_for(mount_plans[ALPHA], "tree"),
        args={"read-only": True, "dir-cache-time": "5m"},
    )
    unit = render(
        MOUNT_UNIT,
        entry=entry,
        **mount_vars(mount_plans, containers, parent_slugs, ALPHA),
    )
    assert "  --read-only \\" in unit
    assert "  --dir-cache-time 5m \\" in unit
    assert "--read-only True" not in unit


def test_mount_unit_owner_grant_pins_uid_and_perms(
    render, mount_plans, containers, parent_slugs
):
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[BRAVO], "tree/home/jd/sys-configs"),
        **mount_vars(mount_plans, containers, parent_slugs, BRAVO),
    )
    assert "--allow-other \\" in unit
    assert "--uid 10001 \\" in unit  # jd, per stortree_user_uids
    assert "--dir-perms 0701 \\" in unit
    assert "--file-perms 0701 \\" in unit
    # An owner-only grant says nothing about the group, so the mount
    # keeps the mounting account's own gid rather than inventing one.
    assert "--gid" not in unit


def test_mount_unit_group_grant_pins_gid_and_not_uid(
    render, mount_plans, containers, parent_slugs
):
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[BRAVO], "tree/home/.mounts/whitfield-media"),
        **mount_vars(mount_plans, containers, parent_slugs, BRAVO),
    )
    assert "--gid 20001 \\" in unit  # "Whitfield Family & Friends"
    assert "--uid" not in unit
    # `permissions: rx` -- group read/execute, no write.
    assert "--dir-perms 0750 \\" in unit


def test_mount_unit_forces_stortree_ownership_on_a_mount_others_nest_inside(
    render, mount_plans, containers, parent_slugs
):
    # `tree` carries no access grant of its own, but other mounts nest
    # inside it, so it must still present predictable stortree:stortree
    # ownership -- otherwise fusermount refuses every nested mount with
    # "bad mount point ... Permission denied" (the task comment in
    # roles/stortree_mounts).
    assert "tree" in parent_slugs[BRAVO]
    entry = entry_for(mount_plans[BRAVO], "tree")
    assert entry["access"] == {}

    unit = render(
        MOUNT_UNIT,
        entry=entry,
        **mount_vars(mount_plans, containers, parent_slugs, BRAVO),
    )
    assert "--allow-other \\" in unit
    assert "--uid 900 \\" in unit  # stortree_uid
    assert "--gid 900 \\" in unit  # stortree_gid


def test_mount_unit_leaves_an_ungranted_leaf_mount_alone(
    render, mount_plans, containers, parent_slugs
):
    # The complement of the test above: no grant *and* nothing nested
    # inside means no --uid/--gid/--allow-other at all, so the mount
    # just presents whatever its backend reports.
    entry = dict(
        entry_for(mount_plans[ALPHA], "tree"),
        local_path="tree/backups-mirror",
        slug="tree-backups\\x2dmirror",
        access={},
        requires_slug="tree",
    )
    unit = render(
        MOUNT_UNIT,
        entry=entry,
        **mount_vars(mount_plans, containers, parent_slugs, ALPHA),
    )
    assert "--allow-other" not in unit
    assert "--uid" not in unit
    assert "--gid" not in unit
    assert "--dir-perms" not in unit


def test_mount_unit_stop_is_tolerant_of_an_already_gone_mountpoint(
    render, mount_plans, containers, parent_slugs
):
    # With PartOf= above, a stop routinely runs after the tree this
    # mount lived in is already gone; that has to be a clean stop.
    unit = render(
        MOUNT_UNIT,
        entry=entry_for(mount_plans[ALPHA], "tree"),
        **mount_vars(mount_plans, containers, parent_slugs, ALPHA),
    )
    (stop,) = directives(unit, "ExecStop")
    assert 'mountpoint -q "/srv/stortree/tree" || exit 0' in stop
    assert 'fusermount -uz "/srv/stortree/tree" || exit 0' in stop
    assert "Restart=on-failure" in unit


def test_mount_unit_for_the_tree_root_mounts_stortree_root_itself(
    render, mount_plans, containers, parent_slugs
):
    # A root-level client mount has local_path "" -- the mount path is
    # stortree_root with no trailing slash, not "/srv/stortree/".
    entry = dict(
        entry_for(mount_plans[GADGET], "tree"), local_path="", slug="root"
    )
    unit = render(
        MOUNT_UNIT,
        entry=entry,
        **mount_vars(mount_plans, containers, parent_slugs, GADGET),
    )
    assert " /srv/stortree \\" in unit
    assert "/srv/stortree/ " not in unit


# -- stortree-user-mount@.service.j2 --------------------------------------


def test_user_mount_unit_presents_the_staging_dir_as_its_owner(
    render, mount_plans, containers, parent_slugs
):
    unit = render(
        USER_MOUNT_UNIT,
        entry=container_for(containers[BRAVO], "tree/home/jd"),
        **mount_vars(mount_plans, containers, parent_slugs, BRAVO),
    )
    assert (
        "ExecStart=/usr/bin/rclone mount /srv/stortree/tree/home/stortree-user-jd "
        "/srv/stortree/tree/home/jd \\" in unit
    )
    assert "--uid 10001 \\" in unit  # jd
    assert "--gid 900 \\" in unit  # stortree_gid -- the container is group-neutral
    assert "--dir-perms 0750 \\" in unit
    assert "--file-perms 0640 \\" in unit
    # A wrapper only re-presents ownership; caching it would be pure
    # duplication of the outer mount's own VFS cache.
    assert "--vfs-cache-mode off" in unit


def test_user_mount_unit_is_partof_the_mount_its_staging_dir_lives_in(
    render, mount_plans, containers, parent_slugs
):
    # Observed in production: one wrapper survived an outer remount as a
    # live process with no mount behind it, and that user's folder sat
    # at the bare directory underneath while the play reported success.
    unit = render(
        USER_MOUNT_UNIT,
        entry=container_for(containers[ALPHA], "tree/home/mw"),
        **mount_vars(mount_plans, containers, parent_slugs, ALPHA),
    )
    assert "After=stortree-mount@tree.service" in unit
    assert "PartOf=stortree-mount@tree.service" in unit
    assert "RequiresMountsFor=/srv/stortree/tree/home/stortree-user-mw" in unit


def test_user_mount_unit_for_a_plain_local_container_has_no_ordering_edge(
    render, mount_plans, containers, parent_slugs
):
    # roles/stortree_mounts only renders this template for containers
    # with requires_slug set, but the template must not fall apart on
    # one without it (a plain-local container, chowned directly).
    entry = dict(
        container_for(containers[ALPHA], "tree/home/jd"), requires_slug=None
    )
    unit = render(
        USER_MOUNT_UNIT,
        entry=entry,
        **mount_vars(mount_plans, containers, parent_slugs, ALPHA),
    )
    assert directives(unit, "After") == []
    assert directives(unit, "PartOf") == []


# -- stortree-bind@.service.j2 --------------------------------------------


def test_bind_unit_depends_on_both_its_source_and_its_container(
    render, mount_plans, containers, parent_slugs
):
    # A bind mount is Type=oneshot + RemainAfterExit, so it reports
    # active forever once ExecStart succeeded, with nothing running to
    # notice its mount went away. Both edges have to be PartOf: the
    # source is the tree it binds *from*, the container the tree its own
    # mountpoint lives *in*.
    unit = render(
        BIND_UNIT,
        entry=entry_for(mount_plans[BRAVO], "tree/home/mw/mw-fam"),
        **mount_vars(mount_plans, containers, parent_slugs, BRAVO),
    )
    source = "stortree-mount@tree-home-.mounts-mw\\x2dfam.service"
    wrapper = "stortree-user-mount@tree-home-mw.service"
    assert directives(unit, "PartOf") == [source, wrapper]
    assert directives(unit, "Requires") == [source, wrapper]
    assert directives(unit, "After") == [source, wrapper]
    assert "Type=oneshot" in unit
    assert "RemainAfterExit=yes" in unit


def test_bind_unit_binds_the_shared_mount_onto_the_per_user_path(
    render, mount_plans, containers, parent_slugs
):
    # A group-only grant backs every member's folder with one shared
    # mount plus a bind per member -- a symlink can't do this job on a
    # backend that can't represent one (docs/plan.md interpretation #2).
    unit = render(
        BIND_UNIT,
        entry=entry_for(mount_plans[BRAVO], "tree/home/jd/whitfield-media"),
        **mount_vars(mount_plans, containers, parent_slugs, BRAVO),
    )
    assert (
        "ExecStart=/bin/mount --bind "
        "/srv/stortree/tree/home/.mounts/whitfield-media "
        "/srv/stortree/tree/home/jd/whitfield-media" in unit
    )


def test_bind_unit_stop_tolerates_an_already_unmounted_path(
    render, mount_plans, containers, parent_slugs
):
    # PartOf= means this unit is routinely stopped as part of a parent
    # remount, by which point its mountpoint can already be gone --
    # nothing to unmount is a successful stop, not a failure.
    unit = render(
        BIND_UNIT,
        entry=entry_for(mount_plans[BRAVO], "tree/home/jd/whitfield-media"),
        **mount_vars(mount_plans, containers, parent_slugs, BRAVO),
    )
    (stop,) = directives(unit, "ExecStop")
    assert stop.startswith("-/bin/sh -c")  # leading `-`: failure ignored
    assert (
        'mountpoint -q "/srv/stortree/tree/home/jd/whitfield-media" || exit 0' in stop
    )


def test_bind_unit_falls_back_to_requires_slug_without_a_wrapper(
    render, mount_plans, containers, parent_slugs
):
    # A container that's plain-local (chowned directly, no wrapper mount
    # rendered) leaves the bind ordered against whatever plan_mounts()
    # computed instead.
    unit = render(
        BIND_UNIT,
        entry=entry_for(mount_plans[BRAVO], "tree/home/mw/mw-fam"),
        stortree_user_containers=[],
        stortree_mounts_parent_slugs=parent_slugs[BRAVO],
    )
    assert "stortree-user-mount@" not in unit
    assert "After=stortree-mount@tree.service" in unit
    assert "PartOf=stortree-mount@tree.service" in unit


# -- smb.conf.j2 ----------------------------------------------------------


@pytest.fixture(scope="session")
def smb_conf(render, resolved):
    # The gadget owns no subtree at all yet still exports the full share
    # -- Samba sharing is universal (docs/config-schema.md).
    return render(SMB_CONF, stortree=resolved[GADGET])


def test_smb_conf_share_name_is_sanitized_from_the_node_path(smb_conf):
    # "tree/home" isn't a legal share name; the slash has to go.
    assert "[tree_home]" in smb_conf
    assert "[tree/home]" not in smb_conf


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
                    "subpath": None,
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
