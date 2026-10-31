#!/bin/bash
# @name: Enable SSH
# @description: Installs the SSH server if missing, reinstalls it if it crashes (damaged files), regenerates missing host keys, then enables and (re)starts it.
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

# exit codes above 128 mean "killed by a signal": sshd crashed instead of reporting a config error
sshd_crashes() {
    sudo -n /usr/sbin/sshd -t >/dev/null 2>&1
    [ $? -ge 128 ]
}

echo "== Checking the sshd program =="
if sshd_crashes; then
    echo "sshd CRASHES - its program files or libraries are damaged (usually a failing SD card)."
    echo "Reinstalling openssh and every library sshd loads..."
    packages="openssh-server openssh-sftp-server openssh-client"
    for lib in $(ldd /usr/sbin/sshd 2>/dev/null | awk '/=>/ {print $3} /^[[:space:]]*\// {print $1}'); do
        owner=$(dpkg -S "$lib" 2>/dev/null || dpkg -S "$(readlink -f "$lib")" 2>/dev/null)
        packages="$packages $(echo "$owner" | grep -v '^diversion' | sed 's/: \/.*//' | tr ',' ' ')"
    done
    packages=$(echo $packages | tr ' ' '\n' | sort -u | tr '\n' ' ')
    echo "Packages: $packages"
    echo
    sudo -n apt-get update -q
    # apt can still fail on other broken packages (e.g. initramfs-tools), the reinstall happens anyway
    sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --reinstall $packages
    echo
    if sshd_crashes; then
        echo "sshd STILL crashes although its files were just reinstalled."
        echo "Then the files are not the problem but the system running them: an unstable overclock,"
        echo "heat, power, RAM or a broken /etc/ld.so.preload. Run 'Check system files' - it shows all of these."
        exit 1
    fi
    echo "sshd works again."
else
    echo "OK"
fi
echo

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

echo "== Checking the configuration =="
if ! sudo -n /usr/sbin/sshd -t; then
    echo "The sshd configuration has errors (see above) - fix them first, sshd won't start like this."
    exit 1
fi
echo "OK"
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
