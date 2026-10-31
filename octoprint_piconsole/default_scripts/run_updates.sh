#!/bin/bash
# @name: Install updates
# @description: Installs all available system updates (apt full-upgrade) and removes unused packages. Can take a while.
# @confirm: true
# @sudo: true
# @block_while_printing: true

set -o pipefail

if ! sudo -n true 2>/dev/null; then
    echo "ERROR: user '$(whoami)' has no sudo rights (wrong password or not in the sudo group)."
    exit 1
fi

log=$(mktemp)
trap 'rm -f "$log"' EXIT

apt_get() {
    sudo -n env DEBIAN_FRONTEND=noninteractive apt-get -y -q \
        -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "$@"
}

echo "== Refreshing package lists =="
apt_get update || exit 1
echo

echo "== Installing updates =="
apt_get full-upgrade 2>&1 | tee "$log" || exit 1
echo

echo "== Removing unused packages =="
apt_get autoremove || exit 1
echo

if [ -f /var/run/reboot-required ] || \
   grep -qE '^(Unpacking|Setting up) (raspberrypi-kernel|raspberrypi-bootloader|linux-image|raspi-firmware|libraspberrypi0)' "$log"; then
    echo "Kernel or firmware was updated - reboot the Pi (System menu > Reboot) when no print is running."
else
    echo "Done. No reboot needed."
fi
