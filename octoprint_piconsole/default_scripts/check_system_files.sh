#!/bin/bash
# @name: Check system files
# @description: Looks for signs of a corrupt SD card: installed files whose checksum no longer matches, SD card and filesystem errors in the kernel log, power problems. Takes several minutes.
# @confirm: false
# @sudo: true

if ! sudo -n true 2>/dev/null; then
    echo "ERROR: user '$(whoami)' has no sudo rights (wrong password or not in the sudo group)."
    exit 1
fi

echo "== Root filesystem =="
findmnt -no SOURCE,FSTYPE,OPTIONS / 2>/dev/null
if findmnt -no OPTIONS / 2>/dev/null | grep -qE '(^|,)ro(,|$)'; then
    echo "WARNING: / is mounted READ-ONLY - Linux does that after filesystem errors."
fi
df -h / | tail -n 1
echo

echo "== Power =="
if command -v vcgencmd >/dev/null 2>&1; then
    throttled=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
    value=$((throttled))
    if [ "$value" -eq 0 ]; then
        echo "OK (no under-voltage since boot)"
    else
        echo "get_throttled=$throttled"
        [ $((value & 0x1)) -ne 0 ] && echo "  under-voltage NOW - a weak power supply can corrupt the SD card"
        [ $((value & 0x10000)) -ne 0 ] && echo "  under-voltage happened since boot - a weak power supply can corrupt the SD card"
    fi
else
    echo "(vcgencmd not available)"
fi
echo

echo "== SD card / filesystem errors in the kernel log =="
errors=$(sudo -n dmesg 2>/dev/null | grep -iE 'mmc[0-9]+:.*(error|timeout|crc)|mmcblk.*error|EXT4-fs (error|warning)|I/O error|Buffer I/O' | tail -n 40)
if [ -n "$errors" ]; then
    echo "$errors"
else
    echo "None since boot."
fi
echo

echo "== Verifying installed package files =="
echo "This compares every installed file with its package checksum and takes several minutes..."
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

packages=$(echo "$bad" | xargs -r dpkg -S 2>/dev/null | grep -v '^diversion' | sed 's/: \/.*//' | tr ',' '\n' | tr -d ' ' | sort -u)
echo "== Affected packages ($(echo "$packages" | wc -l)) =="
echo "$packages" | tr '\n' ' '
echo
echo
echo "Reinstalling them restores the files (Terminal tab):"
echo "  sudo apt-get install --reinstall $(echo "$packages" | tr '\n' ' ')"
echo
echo "A few differences can be harmless (files changed on purpose). Many corrupt files, kernel log errors"
echo "or 'Illegal instruction' crashes mean the SD card is failing: download an OctoPrint backup"
echo "(Settings > Backup & Restore), flash OctoPi to a NEW card and restore the backup there."
