#!/usr/bin/env bash
set -Eeuo pipefail

RELEASE=""
ENABLE_SERVICES=0
CONFIG_DIR="${RECEPTION_RUNTIME_CONFIG_DIR:-$HOME/.config/reachy-reception}"
LIB_DIR="${RECEPTION_RUNTIME_LIB_DIR:-$HOME/.local/lib/reachy-reception}"
BIN_DIR="${RECEPTION_RUNTIME_BIN_DIR:-$HOME/.local/bin}"
STATE_DIR="${RECEPTION_RUNTIME_STATE_DIR:-$HOME/.local/state/reachy-reception}"
AGENT_DIR="$HOME/Library/LaunchAgents"
SERVICE_STOP_TIMEOUT_S="${RECEPTION_SERVICE_STOP_TIMEOUT_S:-30}"

wait_for_service_unloaded() {
  local target="$1"
  local waited=0
  while /bin/launchctl print "$target" >/dev/null 2>&1; do
    if [[ "$waited" -ge "$SERVICE_STOP_TIMEOUT_S" ]]; then
      printf 'Timed out waiting for launchd service to unload: %s\n' "$target" >&2
      return 1
    fi
    /bin/sleep 1
    waited=$((waited + 1))
  done
}

usage() {
  cat <<'EOF'
Usage: scripts/m1max/install_production_runtime.sh --release PATH [--enable-services]

Installs the stable reception-prod launcher, private production config, launchd definitions,
and a 30-day recording reminder. Existing production.env is preserved. Enabling services refuses
to take over ports held by unmanaged processes; stop those processes deliberately first.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release)
      RELEASE="${2:-}"
      shift 2
      ;;
    --enable-services)
      ENABLE_SERVICES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 64
      ;;
  esac
done

[[ -n "$RELEASE" && "$RELEASE" == /* ]] || { printf '%s\n' '--release must be an absolute path' >&2; exit 64; }
[[ -d "$RELEASE" ]] || { printf 'Release does not exist: %s\n' "$RELEASE" >&2; exit 64; }

release_name="$(/usr/bin/basename "$RELEASE")"
[[ "$release_name" =~ ^reachy_mini_receptionist_release_([0-9a-f]{7,40})_frozen$ ]] || {
  printf 'Release is not a versioned frozen directory: %s\n' "$release_name" >&2
  exit 64
}
expected_revision="${BASH_REMATCH[1]}"
actual_revision="$(/usr/bin/git -C "$RELEASE" rev-parse HEAD)"
[[ "$actual_revision" == "$expected_revision"* ]] || {
  printf 'Release name %s does not match Git HEAD %s\n' "$expected_revision" "$actual_revision" >&2
  exit 64
}
[[ -z "$(/usr/bin/git -C "$RELEASE" status --porcelain --untracked-files=no)" ]] || {
  printf 'Frozen release has tracked modifications: %s\n' "$RELEASE" >&2
  exit 64
}
[[ -x "$RELEASE/.release-venv/bin/python" ]] || {
  printf 'Missing release Python: %s\n' "$RELEASE/.release-venv/bin/python" >&2
  exit 64
}

mkdir -p "$CONFIG_DIR" "$LIB_DIR" "$BIN_DIR" "$STATE_DIR/logs" "$AGENT_DIR"
/usr/bin/install -m 755 "$RELEASE/scripts/m1max/production_ops.sh" "$BIN_DIR/reception-prod"
/usr/bin/install -m 755 "$RELEASE/scripts/m1max/managed_s2s_service.sh" "$LIB_DIR/managed_s2s_service.sh"
/usr/bin/install -m 755 "$RELEASE/scripts/m1max/managed_hermes_service.sh" "$LIB_DIR/managed_hermes_service.sh"
/usr/bin/install -m 755 "$RELEASE/scripts/m1max/recording_retention_reminder.sh" "$LIB_DIR/recording_retention_reminder.sh"

if [[ ! -f "$CONFIG_DIR/production.env" ]]; then
  /usr/bin/install -m 600 "$RELEASE/config/production.env.example" "$CONFIG_DIR/production.env"
fi

if [[ "$ENABLE_SERVICES" == "1" ]]; then
  for spec in "com.reachy.reception.s2s:8765" "com.reachy.reception.hermes:8642"; do
    label="${spec%%:*}"
    port="${spec##*:}"
    if /bin/launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
      continue
    fi
    if /usr/bin/nc -z 127.0.0.1 "$port" >/dev/null 2>&1; then
      printf 'Port %s is held by an unmanaged process; stop it before enabling %s.\n' "$port" "$label" >&2
      exit 75
    fi
  done
fi

for label in \
  com.reachy.reception.s2s \
  com.reachy.reception.hermes \
  com.reachy.reception.recording-retention; do
  template="$RELEASE/config/launchd/$label.plist.in"
  rendered="$STATE_DIR/$label.plist"
  /usr/bin/sed "s|@HOME@|$HOME|g" "$template" > "$rendered"
  /usr/bin/plutil -lint "$rendered" >/dev/null
  installed="$AGENT_DIR/$label.plist"
  if [[ -f "$installed" ]] && /usr/bin/cmp -s "$rendered" "$installed"; then
    continue
  fi
  target="gui/$UID/$label"
  if /bin/launchctl print "$target" >/dev/null 2>&1; then
    if [[ "$ENABLE_SERVICES" != "1" ]]; then
      printf 'Loaded service definition changed; rerun with --enable-services: %s\n' "$label" >&2
      exit 75
    fi
    /bin/launchctl bootout "$target"
    wait_for_service_unloaded "$target"
  fi
  /usr/bin/install -m 600 "$rendered" "$installed"
done

active_tmp="$CONFIG_DIR/active-release.tmp.$$"
printf '%s\n' "$RELEASE" > "$active_tmp"
/bin/chmod 600 "$active_tmp"
/bin/mv "$active_tmp" "$CONFIG_DIR/active-release"

if [[ "$ENABLE_SERVICES" == "1" ]]; then
  for label in \
    com.reachy.reception.s2s \
    com.reachy.reception.hermes \
    com.reachy.reception.recording-retention; do
    target="gui/$UID/$label"
    if /bin/launchctl print "$target" >/dev/null 2>&1; then
      /bin/launchctl kickstart -k "$target"
    else
      /bin/launchctl bootstrap "gui/$UID" "$AGENT_DIR/$label.plist"
    fi
  done
fi

printf 'Active release: %s\n' "$RELEASE"
printf 'Production config: %s\n' "$CONFIG_DIR/production.env"
printf 'Stable launcher: %s\n' "$BIN_DIR/reception-prod"
printf 'Services enabled: %s\n' "$ENABLE_SERVICES"
