#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${1:-$(pwd)}"
REPO="$(cd "$REPO" && pwd)"
PYTHON="${RECEPTION_RUNTIME_PYTHON:-$REPO/.venv/bin/python}"
LIB_DIR="$HOME/.local/lib/reachy-reception"
STATE_DIR="$HOME/.local/state/reachy-reception"
AGENT_DIR="$HOME/Library/LaunchAgents"
LABEL="com.reachy.reception.recording-retention.local"
PLIST="$AGENT_DIR/$LABEL.plist"

[[ -x "$PYTHON" ]] || { printf 'Missing local runtime Python: %s\n' "$PYTHON" >&2; exit 64; }
mkdir -p "$LIB_DIR" "$STATE_DIR/logs" "$AGENT_DIR"
/usr/bin/install -m 755 \
  "$REPO/scripts/m1max/recording_retention_reminder.sh" \
  "$LIB_DIR/recording_retention_reminder_local.sh"

rendered="$STATE_DIR/$LABEL.plist"
/usr/bin/sed \
  -e "s|@HOME@|$HOME|g" \
  -e "s|@REPO@|$REPO|g" \
  -e "s|@PYTHON@|$PYTHON|g" \
  "$REPO/config/launchd/$LABEL.plist.in" > "$rendered"
/usr/bin/plutil -lint "$rendered" >/dev/null
/usr/bin/install -m 600 "$rendered" "$PLIST"

target="gui/$UID/$LABEL"
if /bin/launchctl print "$target" >/dev/null 2>&1; then
  /bin/launchctl bootout "$target"
fi
/bin/launchctl bootstrap "gui/$UID" "$PLIST"
/bin/launchctl kickstart -k "$target"

printf 'Installed daily local recording reminder: %s\n' "$LABEL"
printf 'Report: %s\n' "$STATE_DIR/recording-retention-local-latest.json"
printf 'No files are deleted automatically.\n'
