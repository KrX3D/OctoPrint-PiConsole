#!/bin/bash
# @name: Check SSH
# @description: Shows whether the SSH server is installed, enabled and listening, which users can log in and recent SSH log entries.
# @confirm: false

can_sudo=0
sudo -n true 2>/dev/null && can_sudo=1

echo "== Host =="
echo "Hostname:     $(hostname)"
echo "IP addresses: $(hostname -I 2>/dev/null)"
echo "Running as:   $(whoami)"
echo

echo "== SSH server =="
if [ ! -x /usr/sbin/sshd ]; then
    echo "openssh-server is NOT installed. Run the 'Enable SSH' script to install it."
    exit 1
fi
echo "ssh.service enabled: $(systemctl is-enabled ssh.service 2>&1)"
echo "ssh.service active:  $(systemctl is-active ssh.service 2>&1)"
if systemctl list-unit-files ssh.socket 2>/dev/null | grep -q '^ssh\.socket'; then
    echo "ssh.socket enabled:  $(systemctl is-enabled ssh.socket 2>&1)"
    echo "ssh.socket active:   $(systemctl is-active ssh.socket 2>&1)"
fi
echo

echo "== Listening sockets =="
listeners=$(ss -tlnH 2>/dev/null | awk '{print $4}' | grep -E ':(22|2222)$')
if [ -n "$listeners" ]; then
    echo "$listeners"
else
    echo "Nothing is listening on port 22. SSH is not reachable."
fi
echo

echo "== Effective sshd settings =="
if [ "$can_sudo" = 1 ]; then
    sudo -n sshd -T 2>&1 | grep -Ei '^(port|listenaddress|passwordauthentication|pubkeyauthentication|permitrootlogin|allowusers|denyusers|allowgroups) '
    echo
    echo "Host keys:"
    ls -l /etc/ssh/ssh_host_*_key 2>/dev/null || echo "  none found - sshd can't start without them ('Enable SSH' regenerates them)"
else
    grep -hEi '^\s*(port|passwordauthentication|pubkeyauthentication|permitrootlogin|allowusers)\b' \
        /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null || echo "(defaults)"
fi
echo

echo "== Boot partition 'ssh' flag file =="
for dir in /boot/firmware /boot; do
    for name in ssh ssh.txt; do
        if [ -e "$dir/$name" ]; then
            echo "Found $dir/$name - it is normally removed after it was processed at boot."
        fi
    done
done
echo "(If nothing is listed, the flag was processed or never present.)"
echo

echo "== Users that can log in =="
getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 && $7 !~ /(nologin|false)$/ {print $1}' | while read -r user; do
    if [ "$can_sudo" = 1 ]; then
        state=$(sudo -n passwd -S "$user" 2>/dev/null | awk '{print $2}')
        case "$state" in
            P) state="password set" ;;
            L) state="password LOCKED - password login impossible" ;;
            NP) state="NO password - password login impossible" ;;
        esac
        echo "  $user ($state)"
    else
        echo "  $user"
    fi
done
echo "Note: newer Raspberry Pi OS / OctoPi images have no default pi/raspberry login."
echo

echo "== Last SSH log entries =="
if [ "$can_sudo" = 1 ]; then
    sudo -n journalctl -u ssh.service -u ssh.socket -n 25 --no-pager 2>&1
else
    echo "(needs passwordless sudo)"
fi
