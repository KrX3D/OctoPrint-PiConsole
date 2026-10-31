#!/bin/bash
# @name: Check system files
# @description: Finds the cause of random crashes or broken updates: damaged installed files, SD card errors, crashes in the kernel log, overclocking, heat and power problems. Takes a few minutes.
# @confirm: false
# @sudo: true

if ! sudo -n true 2>/dev/null; then
    echo "ERROR: user '$(whoami)' has no sudo rights (wrong password or not in the sudo group)."
    exit 1
fi

echo "== System =="
echo "Model:        $(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
echo "Kernel:       $(uname -r) ($(uname -m))"
echo "Userland:     $(dpkg --print-architecture 2>/dev/null)"
command -v vcgencmd >/dev/null 2>&1 && echo "Temperature:  $(vcgencmd measure_temp 2>/dev/null | cut -d= -f2)"
echo

echo "== Root filesystem =="
findmnt -no SOURCE,FSTYPE,OPTIONS / 2>/dev/null
if findmnt -no OPTIONS / 2>/dev/null | grep -qE '(^|,)ro(,|$)'; then
    echo "WARNING: / is mounted READ-ONLY - Linux does that after filesystem errors."
fi
df -h / | tail -n 1
echo

echo "== Power and throttling =="
if command -v vcgencmd >/dev/null 2>&1; then
    throttled=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
    value=$((throttled))
    if [ "$value" -eq 0 ]; then
        echo "OK (no under-voltage or throttling since boot)"
    else
        echo "get_throttled=$throttled"
        [ $((value & 0x1)) -ne 0 ] && echo "  under-voltage NOW - a weak power supply causes crashes and corruption"
        [ $((value & 0x10000)) -ne 0 ] && echo "  under-voltage happened since boot - a weak power supply causes crashes and corruption"
        [ $((value & 0x4)) -ne 0 ] && echo "  throttled NOW (too hot)"
        [ $((value & 0x40000)) -ne 0 ] && echo "  throttling happened since boot (too hot)"
    fi
else
    echo "(vcgencmd not available)"
fi
echo

echo "== Overclocking (config.txt) =="
config=""
for c in /boot/firmware/config.txt /boot/config.txt; do
    [ -f "$c" ] && config="$c" && break
done
if [ -n "$config" ]; then
    oc=$(grep -E '^[[:space:]]*(arm_freq|over_voltage|over_voltage_delta|force_turbo|core_freq|gpu_freq|sdram_freq|arm_boost)[[:space:]]*=' "$config")
    if [ -n "$oc" ]; then
        echo "$config sets:"
        echo "$oc" | sed 's/^/  /'
        echo "An overclock that isn't fully stable causes exactly this: random 'Illegal instruction' and"
        echo "segfaults, corrupt data while unpacking - while the files on the SD card are fine."
        echo "Comment these lines out (# in front) and reboot to test."
    else
        echo "No overclock settings in $config."
    fi
else
    echo "config.txt not found."
fi
echo

echo "== Preloaded libraries =="
if [ -s /etc/ld.so.preload ]; then
    echo "/etc/ld.so.preload (loaded into EVERY program - a broken entry crashes everything):"
    sed 's/^/  /' /etc/ld.so.preload
    while read -r lib; do
        case "$lib" in ''|'#'*) continue ;; esac
        resolved=$(echo "$lib" | sed "s/\${PLATFORM}/$(uname -m)/; s/\$PLATFORM/$(uname -m)/")
        ls "$resolved" >/dev/null 2>&1 || echo "  note: $resolved does not exist (ignored by the loader with a warning)"
    done < /etc/ld.so.preload
else
    echo "None."
fi
echo

echo "== SD card / filesystem errors in the kernel log =="
kernel_log=$(sudo -n dmesg 2>/dev/null)
errors=$(echo "$kernel_log" | grep -iE 'mmc[0-9]+:.*(error|timeout|crc)|mmcblk.*error|EXT4-fs (error|warning)|I/O error|Buffer I/O' | tail -n 30)
[ -n "$errors" ] && echo "$errors" || echo "None since boot."
echo

echo "== Crashes in the kernel log =="
crashes=$(echo "$kernel_log" | grep -iE 'segfault|unhandled (page fault|signal)|undefined instruction|illegal instruction|Internal error|Oops|Unable to handle|BUG:|soft lockup|Kernel panic' | tail -n 30)
[ -n "$crashes" ] && echo "$crashes" || echo "None since boot (32-bit kernels often don't log program crashes)."
echo

echo "== Verifying installed package files =="
echo "Comparing every installed file with its package checksum..."
result=$(mktemp)
trap 'rm -f "$result"' EXIT
sudo -n nice -n 10 dpkg --verify > "$result" 2>/dev/null

# 3rd flag column "5" = checksum mismatch; config files (marked "c") are allowed to differ
bad=$(awk '$2 != "c" && ($1 ~ /^..5/ || $1 == "missing") {print $NF}' "$result")
if [ -z "$bad" ]; then
    echo "All package files match their checksums."
    exit 0
fi

count=$(echo "$bad" | wc -l)
echo "$count file(s) differ from their package:"
echo "$bad" | head -n 100
[ "$count" -gt 100 ] && echo "... and $((count - 100)) more"
echo

packages=""
lookup_errors=""
while read -r file; do
    [ -n "$file" ] || continue
    owner=$(dpkg-query -S "$file" 2>&1)
    rc=$?
    if [ $rc -eq 0 ]; then
        packages="$packages $(echo "$owner" | grep -v '^diversion' | head -n 1 | sed 's/: \/.*//' | tr ',' ' ')"
    else
        lookup_errors="$lookup_errors\n  $file: ${owner:-dpkg-query exit code $rc}"
    fi
done <<< "$bad"
packages=$(echo $packages | tr ' ' '\n' | sed '/^$/d' | sort -u | tr '\n' ' ')

echo "== Affected packages =="
if [ -n "$packages" ]; then
    echo "$packages"
    echo
    echo "Reinstalling them restores the files (Terminal tab):"
    echo "  sudo apt-get install --reinstall $packages"
else
    echo "(none found)"
fi
if [ -n "$lookup_errors" ]; then
    echo
    echo "Package lookup failed for:"
    printf '%b\n' "$lookup_errors"
fi
echo
echo "A few differing files can be harmless. Many damaged files or SD card errors mean the card is failing:"
echo "download an OctoPrint backup (Settings > Backup & Restore), flash OctoPi to a NEW card, restore it."
echo "Random crashes while the files are fine point to overclocking, heat, power or RAM instead."
