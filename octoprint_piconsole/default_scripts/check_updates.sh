#!/bin/bash
# @name: Check for updates
# @description: Refreshes the package lists and shows which system packages can be upgraded. Installs nothing.
# @confirm: false
# @sudo: true

if ! sudo -n true 2>/dev/null; then
    echo "ERROR: user '$(whoami)' has no sudo rights (wrong password or not in the sudo group)."
    exit 1
fi

echo "== Refreshing package lists =="
sudo -n apt-get update -q || exit 1
echo

upgradable=$(apt list --upgradable 2>/dev/null | tail -n +2)
if [ -z "$upgradable" ]; then
    echo "Everything is up to date."
else
    echo "== $(echo "$upgradable" | wc -l) package(s) can be upgraded =="
    echo "$upgradable"
    echo
    echo "Run 'Install updates' to install them."
fi

if [ -f /var/run/reboot-required ]; then
    echo
    echo "A reboot is still pending from an earlier update."
fi

echo
echo "Note: OctoPrint itself and its plugins are updated via Settings > Software Update."
