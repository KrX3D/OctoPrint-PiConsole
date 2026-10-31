#!/bin/bash
# @name: Follow OctoPrint log (live)
# @description: Shows the last 50 lines of octoprint.log and then every new line as it is written. Press Stop to end; it stops by itself after 30 minutes.
# @confirm: false

LOG="${PICONSOLE_LOG_DIR:-$HOME/.octoprint/logs}/octoprint.log"
if [ ! -e "$LOG" ]; then
    echo "$LOG not found."
    exit 1
fi

echo "Following $LOG - press Stop to end."
echo
timeout --foreground 30m tail -n 50 -F "$LOG"
if [ $? -eq 124 ]; then
    echo
    echo "Stopped automatically after 30 minutes."
fi
exit 0
