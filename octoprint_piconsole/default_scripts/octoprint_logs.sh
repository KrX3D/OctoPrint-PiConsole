#!/bin/bash
# @name: OctoPrint logs
# @description: Diagnostics overview: all log files, recent errors with their tracebacks, the last lines of every current log and the OctoPrint service journal.
# @confirm: false

# lines shown per log file - edit to taste
TAIL_LINES=100
# recent error blocks shown (an error line plus its traceback)
ERROR_LINES=150

LOG_DIR="${PICONSOLE_LOG_DIR:-$HOME/.octoprint/logs}"
if [ ! -d "$LOG_DIR" ]; then
    echo "Log folder $LOG_DIR not found."
    exit 1
fi
cd "$LOG_DIR" || exit 1
shopt -s nullglob

echo "== Log files in $LOG_DIR =="
ls -lhtr --time-style=+'%Y-%m-%d %H:%M'
echo

echo "== Error / warning counts (current logs) =="
for log in *.log; do
    [ -s "$log" ] || continue
    printf '%-40s errors: %5s  warnings: %5s  tracebacks: %4s\n' "$log" \
        "$(grep -cE ' - (ERROR|CRITICAL) - ' "$log")" \
        "$(grep -c ' - WARNING - ' "$log")" \
        "$(grep -c '^Traceback' "$log")"
done
echo

echo "== Recent errors in octoprint.log (with tracebacks) =="
if [ -s octoprint.log ]; then
    # an error line plus the continuation lines (traceback) that follow it;
    # no {n} intervals, older mawk on Raspberry Pi OS doesn't support them
    awk '
        / - (ERROR|CRITICAL) - / { inblock = 1; print; next }
        inblock && /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] / { inblock = 0 }
        inblock { print }
    ' octoprint.log | tail -n "$ERROR_LINES"
    [ "$(grep -cE ' - (ERROR|CRITICAL) - ' octoprint.log)" = 0 ] && echo "No errors."
else
    echo "octoprint.log is empty or missing."
fi
echo

for log in octoprint.log serial.log plugin_*.log *.log; do
    [ -s "$log" ] || continue
    case " $shown " in *" $log "*) continue ;; esac
    shown="$shown $log"
    echo "== Last $TAIL_LINES lines of $log =="
    tail -n "$TAIL_LINES" "$log"
    echo
done

echo "== OctoPrint service journal (last 50 lines) =="
if journalctl -u octoprint -n 50 --no-pager -q 2>/dev/null | grep -q .; then
    journalctl -u octoprint -n 50 --no-pager -q
elif sudo -n true 2>/dev/null; then
    sudo -n journalctl -u octoprint -n 50 --no-pager -q
else
    echo "(not readable - needs systemd-journal/adm group or passwordless sudo)"
fi
