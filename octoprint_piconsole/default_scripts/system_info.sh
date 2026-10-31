#!/bin/bash
# @name: System info
# @description: Pi model, OS, uptime, CPU temperature, power/throttling warnings, disk and memory usage.
# @confirm: false

echo "Model:    $(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
echo "OS:       $(. /etc/os-release && echo "$PRETTY_NAME")"
echo "Kernel:   $(uname -r)"
[ -f /etc/octopi_version ] && echo "OctoPi:   $(cat /etc/octopi_version)"
echo "Hostname: $(hostname)"
echo "IPs:      $(hostname -I 2>/dev/null)"
echo "Uptime:   $(uptime -p)"
echo "Load:     $(cut -d' ' -f1-3 /proc/loadavg)"

if command -v vcgencmd >/dev/null 2>&1; then
    echo "CPU temp: $(vcgencmd measure_temp 2>/dev/null | cut -d= -f2)"
    throttled=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
    if [ -n "$throttled" ]; then
        value=$((throttled))
        if [ "$value" -eq 0 ]; then
            echo "Power:    OK (no under-voltage or throttling since boot)"
        else
            echo "Power:    $throttled"
            [ $((value & 0x1)) -ne 0 ] && echo "          - under-voltage NOW (check the power supply)"
            [ $((value & 0x4)) -ne 0 ] && echo "          - throttled NOW"
            [ $((value & 0x10000)) -ne 0 ] && echo "          - under-voltage happened since boot"
            [ $((value & 0x40000)) -ne 0 ] && echo "          - throttling happened since boot"
        fi
    fi
elif [ -r /sys/class/thermal/thermal_zone0/temp ]; then
    echo "CPU temp: $(awk '{printf "%.1f'"'"'C", $1 / 1000}' /sys/class/thermal/thermal_zone0/temp)"
fi

echo
echo "== Disk =="
df -h / /boot/firmware /boot 2>/dev/null | awk '!seen[$0]++'
echo
echo "== Memory =="
free -h
