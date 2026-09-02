#!/bin/sh
#
# Invoked by pam_exec.so (expose_authtok) from the auth and password
# PAM chains -- see roles/stortree_pam_smbpass/tasks/main.yml. Syncs
# $PAM_USER's Samba NT-hash to the plaintext credential pam_exec hands
# us on stdin, replacing pam_smbpass.so/libpam-smbpass, which Debian
# and Ubuntu removed from Samba's packaging (upstream dropped it in
# Samba 4.4) and so is no longer installable on any supported host.
# `smbpasswd -s -a` both adds and updates the local entry, so this
# handles a user's first sync and every resync after the same way.
#
# Always exits 0: a sync failure here must never fail the PAM stack
# that invoked it.

user=${PAM_USER:-}
[ -n "$user" ] || exit 0

password=$(head -n1)
[ -n "$password" ] || exit 0

if ! error=$(printf '%s\n%s\n' "$password" "$password" | smbpasswd -s -a "$user" 2>&1); then
    logger -t stortree-pam-smbpass-sync "sync failed for user '$user': $error"
fi

exit 0
