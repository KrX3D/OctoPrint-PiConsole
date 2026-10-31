#!/bin/bash
# @name: Enable SSH
# @description: Installs the SSH server if missing, regenerates missing host keys, then enables and (re)starts it.
# @confirm: true
# @sudo: true

if ! sudo -n true 2>/dev/null; then
    echo "ERROR: user '$(whoami)' has no sudo rights (wrong password or not in the sudo group)."
    exit 1
fi

if [ ! -x /usr/sbin/sshd ]; then
    echo "== Installing openssh-server =="
    sudo -n apt-get update -q || exit 1
    sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y -q openssh-server || exit 1
    echo
fi

echo "== Host keys =="
# missing or empty host keys are a common reason for sshd refusing to start on a Pi
for key in /etc/ssh/ssh_host_*_key; do
    if [ -e "$key" ] && [ ! -s "$key" ]; then
        echo "Removing empty key $key"
        sudo -n rm -f "$key" "$key.pub"
    fi
done
sudo -n ssh-keygen -A && echo "Host keys OK"
echo

echo "== Enabling and starting SSH =="
sudo -n systemctl enable ssh.service
sudo -n systemctl restart ssh.service
sleep 1
systemctl --no-pager --lines=10 status ssh.service
echo

if systemctl is-active --quiet ssh.service; then
    echo "SSH is running. Connect with: ssh $(whoami)@$(hostname -I | awk '{print $1}')"
else
    echo "SSH failed to start - see the log above."
    exit 1
fi
