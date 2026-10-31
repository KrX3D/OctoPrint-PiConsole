#!/bin/bash
# @name: Diagnose crashes
# @description: Runs programs that typically crash on a damaged system (xz, sshd, modinfo, ...) and shows which library files they really load - finds libraries that don't come from a package (e.g. in /usr/local) or are damaged.
# @confirm: false
# @sudo: optional

can_sudo=0
sudo -n true 2>/dev/null && can_sudo=1

# exit codes above 128 mean "killed by a signal" = crashed
describe() {
    local rc=$1
    if [ "$rc" -ge 128 ]; then
        local sig
        sig=$(kill -l $((rc - 128)) 2>/dev/null)
        echo "CRASHED (SIG${sig:-$((rc - 128))})"
    else
        echo "ok (exit code $rc)"
    fi
}

probe() {
    local label=$1
    shift
    bash -c "$*" >/dev/null 2>&1
    printf '  %-34s %s\n' "$label" "$(describe $?)"
}

echo "== Test runs =="
probe "xz compress + decompress" "set -o pipefail; echo test | xz | xz -d"
probe "gzip" "set -o pipefail; echo test | gzip | gzip -d"
module=$(find "/lib/modules/$(uname -r)" -name '*.ko*' 2>/dev/null | head -n 1)
[ -n "$module" ] && probe "modinfo ($(basename "$module"))" "modinfo '$module'"
probe "openssl" "openssl version"
probe "python3" "python3 -c pass"
probe "systemctl" "systemctl --version"
if [ -x /usr/sbin/sshd ]; then
    if [ "$can_sudo" = 1 ]; then
        probe "sshd -t" "sudo -n /usr/sbin/sshd -t"
    else
        probe "sshd -t (without sudo)" "/usr/sbin/sshd -t"
    fi
fi
echo

echo "== Library search path overrides =="
if [ -s /etc/ld.so.preload ]; then
    echo "/etc/ld.so.preload (loaded into EVERY program):"
    sed 's/^/  /' /etc/ld.so.preload
else
    echo "/etc/ld.so.preload: none"
fi
echo "LD_LIBRARY_PATH: ${LD_LIBRARY_PATH:-not set}"
echo "Extra library folders (/etc/ld.so.conf.d):"
cat /etc/ld.so.conf.d/*.conf 2>/dev/null | grep -v '^#' | sed '/^$/d; s/^/  /'
local_libs=$(find /usr/local/lib /usr/local/lib64 -maxdepth 2 -name '*.so*' 2>/dev/null)
if [ -n "$local_libs" ]; then
    echo "Libraries in /usr/local (NOT from packages, loaded BEFORE the system ones):"
    echo "$local_libs" | sed 's/^/  /'
else
    echo "Libraries in /usr/local: none"
fi
echo

# dpkg may know a library under /lib or /usr/lib (merged /usr) and under its symlink or real name
owner_of() {
    local path=$1 real candidate owner
    real=$(readlink -f "$path")
    for candidate in "$path" "$real" "${path#/usr}" "/usr${path#/usr}" "${real#/usr}" "/usr${real#/usr}"; do
        owner=$(dpkg-query -S "$candidate" 2>/dev/null | grep -v '^diversion' | head -n 1 | sed 's/: \/.*//')
        if [ -n "$owner" ]; then
            echo "$owner"
            return
        fi
    done
}

echo "== Libraries loaded by the programs that crashed =="
suspicious=""
declare -A checked
for binary in /usr/bin/xz /usr/sbin/sshd /usr/bin/kmod /usr/bin/openssl /bin/bash; do
    [ -x "$binary" ] || continue
    echo "$binary:"
    libs=$(ldd "$binary" 2>&1)
    if [ $? -ge 128 ]; then
        echo "  ldd itself crashed - the dynamic loader or libc is damaged"
        suspicious="$suspicious libc6"
        continue
    fi
    while read -r lib; do
        [ -n "$lib" ] || continue
        if [ "$lib" = "not-found" ]; then
            continue
        fi
        package=$(owner_of "$lib")
        real=$(readlink -f "$lib")
        if [ -z "$package" ]; then
            printf '  %-50s NOT FROM A PACKAGE  <==\n' "$real"
            suspicious="$suspicious $real"
            continue
        fi
        status="${checked[$package]}"
        if [ -z "$status" ]; then
            # check the whole package once: any non-config file with a checksum mismatch?
            if dpkg --verify "$package" 2>/dev/null | awk '$2 != "c" && ($1 ~ /^..5/ || $1 == "missing")' | grep -q .; then
                status="DAMAGED"
            else
                status="ok"
            fi
            checked[$package]=$status
        fi
        if [ "$status" = "DAMAGED" ]; then
            printf '  %-50s %s  DAMAGED FILES  <==\n' "$real" "$package"
            suspicious="$suspicious $package"
        else
            printf '  %-50s %s\n' "$real" "$package"
        fi
    done <<< "$(echo "$libs" | awk '/not found/ {print "not-found"; next} /=>/ {print $3; next} /^[[:space:]]*\// {print $1}')"
    missing=$(echo "$libs" | grep 'not found')
    [ -n "$missing" ] && echo "$missing" | sed 's/^/  MISSING: /'
done
echo

echo "== Result =="
suspicious=$(echo $suspicious | tr ' ' '\n' | sed '/^$/d' | sort -u)
if [ -z "$suspicious" ]; then
    echo "All loaded libraries come from intact packages."
    echo "If programs still crash, the cause is below the software: unstable overclock, heat, power or RAM"
    echo "('Check system files' shows overclock settings, throttling and kernel errors)."
    exit 0
fi
echo "Suspicious:"
echo "$suspicious" | sed 's/^/  /'
echo
if echo "$suspicious" | grep -q '^/'; then
    echo "Libraries outside of packages override the system ones. Move them away and refresh the cache:"
    echo "  sudo mkdir -p /root/local-lib-backup"
    echo "$suspicious" | grep '^/' | while read -r lib; do echo "  sudo mv '$lib' /root/local-lib-backup/"; done
    echo "  sudo ldconfig"
fi
packages=$(echo "$suspicious" | grep -v '^/' | tr '\n' ' ')
if [ -n "$packages" ]; then
    echo "Damaged packages, reinstall them:"
    echo "  sudo apt-get install --reinstall $packages"
fi
echo "Then test again with this script, and finish a broken update with: sudo dpkg --configure -a"
