#!/usr/bin/env bash
set -euo pipefail

PROFILE=""
ALLOW_PRODUCTION=0
DRY_RUN=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SOURCE_DIR="${HERMES_PROFILE_SOURCE_DIR:-$REPO_DIR/profiles/clinic_receptionist}"
PLUGIN_SOURCE_DIR="${REFERENCE_LIBRARY_PLUGIN_DIR:-$REPO_DIR/hermes_plugins/reference_library}"
LATENCY_PLUGIN_SOURCE_DIR="${LATENCY_TRACE_PLUGIN_DIR:-$REPO_DIR/hermes_plugins/latency_trace}"
PROFILES_DIR="${HERMES_PROFILES_DIR:-$HOME/.hermes/profiles}"
HERMES_PYTHON="${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python}"

usage() {
  cat <<'EOF'
Usage: scripts/m1max/sync_hermes_profile.sh --profile NAME [options]

Publish the tracked receptionist profile modules to one existing Hermes profile.
The script does not copy or clean credentials, memories, sessions, skills, logs,
response databases, or any other runtime state.

Options:
  --profile NAME          Existing Hermes profile to update (required).
  --source-dir DIR        Tracked profile source directory.
  --profiles-dir DIR      Hermes profiles root.
  --allow-production     Permit the reachyclinic production profile target.
  --dry-run              Print the files and config changes without writing them.
  -h, --help              Show this help.

The reachyclinic production target is rejected unless --allow-production is
provided after the staging persona and behavior have been explicitly approved.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="$2"
      shift 2
      ;;
    --source-dir)
      SOURCE_DIR="$2"
      shift 2
      ;;
    --profiles-dir)
      PROFILES_DIR="$2"
      shift 2
      ;;
    --allow-production)
      ALLOW_PRODUCTION=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$PROFILE" ]]; then
  echo "--profile is required" >&2
  usage >&2
  exit 2
fi
if [[ ! "$PROFILE" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
  echo "Invalid Hermes profile name: $PROFILE" >&2
  exit 2
fi
if [[ "$PROFILE" == "reachyclinic" && "$ALLOW_PRODUCTION" -ne 1 ]]; then
  echo "Refusing to update production profile reachyclinic without --allow-production." >&2
  exit 2
fi

PROFILE_DIR="$PROFILES_DIR/$PROFILE"
CONTEXT_DIR="$PROFILE_DIR/context/receptionist"
CONFIG_PATH="$PROFILE_DIR/config.yaml"
PLUGIN_DIR="$PROFILE_DIR/plugins/reference-library"
LATENCY_PLUGIN_DIR="$PROFILE_DIR/plugins/latency-trace"

if [[ ! -d "$PROFILE_DIR" ]]; then
  echo "Hermes profile does not exist: $PROFILE_DIR" >&2
  exit 2
fi
for name in personality.md HERMES.md reference_catalog.yaml clinic_facts.md capabilities.md; do
  if [[ ! -f "$SOURCE_DIR/$name" ]]; then
    echo "Missing profile source file: $SOURCE_DIR/$name" >&2
    exit 2
  fi
done
for name in plugin.yaml __init__.py; do
  if [[ ! -f "$PLUGIN_SOURCE_DIR/$name" ]]; then
    echo "Missing reference-library plugin file: $PLUGIN_SOURCE_DIR/$name" >&2
    exit 2
  fi
  if [[ ! -f "$LATENCY_PLUGIN_SOURCE_DIR/$name" ]]; then
    echo "Missing latency-trace plugin file: $LATENCY_PLUGIN_SOURCE_DIR/$name" >&2
    exit 2
  fi
done
if [[ ! -x "$HERMES_PYTHON" ]]; then
  echo "Hermes Python is not executable: $HERMES_PYTHON" >&2
  exit 2
fi

log() {
  printf '[sync-hermes-profile] %s\n' "$*" >&2
}

publish() {
  local source="$1"
  local destination="$2"
  log "$source -> $destination"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    cp "$source" "$destination"
  fi
}

log "target profile: $PROFILE"
if [[ "$DRY_RUN" -eq 0 ]]; then
  mkdir -p "$CONTEXT_DIR" "$PLUGIN_DIR" "$LATENCY_PLUGIN_DIR"
fi

publish "$SOURCE_DIR/personality.md" "$PROFILE_DIR/SOUL.md"
publish "$SOURCE_DIR/reference_catalog.yaml" "$CONTEXT_DIR/reference_catalog.yaml"
publish "$SOURCE_DIR/clinic_facts.md" "$CONTEXT_DIR/clinic_facts.md"
publish "$SOURCE_DIR/capabilities.md" "$CONTEXT_DIR/capabilities.md"
publish "$PLUGIN_SOURCE_DIR/plugin.yaml" "$PLUGIN_DIR/plugin.yaml"
publish "$PLUGIN_SOURCE_DIR/__init__.py" "$PLUGIN_DIR/__init__.py"
publish "$LATENCY_PLUGIN_SOURCE_DIR/plugin.yaml" "$LATENCY_PLUGIN_DIR/plugin.yaml"
publish "$LATENCY_PLUGIN_SOURCE_DIR/__init__.py" "$LATENCY_PLUGIN_DIR/__init__.py"

log "$SOURCE_DIR/HERMES.md + prompt-delivered references -> $CONTEXT_DIR/HERMES.md"
if [[ "$DRY_RUN" -eq 0 ]]; then
  "$HERMES_PYTHON" - "$SOURCE_DIR" "$CONTEXT_DIR/HERMES.md" <<'PY'
import sys
from pathlib import Path

import yaml

source_dir = Path(sys.argv[1]).resolve(strict=True)
destination = Path(sys.argv[2])
base = (source_dir / "HERMES.md").read_text(encoding="utf-8").rstrip()
catalog = yaml.safe_load(
    (source_dir / "reference_catalog.yaml").read_text(encoding="utf-8")
) or {}
if catalog.get("version") != 1 or not isinstance(catalog.get("references"), dict):
    raise SystemExit("reference catalog must be a version 1 mapping")

sections = [base]
for reference_id, entry in catalog["references"].items():
    if not isinstance(entry, dict):
        raise SystemExit(f"reference {reference_id!r} must be a mapping")
    delivery = entry.get("delivery")
    if delivery not in {"prompt", "on_demand"}:
        raise SystemExit(
            f"reference {reference_id!r} delivery must be 'prompt' or 'on_demand'"
        )
    if delivery != "prompt":
        continue
    title = entry.get("title")
    relative_path = entry.get("path")
    if not isinstance(title, str) or not title.strip():
        raise SystemExit(f"reference {reference_id!r} requires a title")
    if not isinstance(relative_path, str) or Path(relative_path).is_absolute():
        raise SystemExit(f"reference {reference_id!r} requires a relative path")
    reference_path = (source_dir / relative_path).resolve(strict=True)
    try:
        reference_path.relative_to(source_dir)
    except ValueError as exc:
        raise SystemExit(
            f"reference {reference_id!r} resolves outside the profile source"
        ) from exc
    content = reference_path.read_text(encoding="utf-8").strip()
    content_lines = content.splitlines()
    if (
        content_lines
        and content_lines[0].startswith("# ")
        and content_lines[0][2:].strip().casefold() == title.strip().casefold()
    ):
        content_lines = content_lines[1:]
    content = "\n".join(
        f"#{line}" if line.startswith("#") else line for line in content_lines
    ).strip()
    sections.append(f"## {title.strip()}\n\n{content}")

rendered = "\n\n".join(sections) + "\n"
if len(rendered) > 20_000:
    raise SystemExit("generated HERMES.md exceeds the 20,000 character limit")
destination.write_text(rendered, encoding="utf-8")
PY
fi

log "config: terminal.cwd=$CONTEXT_DIR"
log "config: platform_toolsets.api_server=[reference_readonly, no_mcp]"
log "config: agent.disabled_toolsets includes file, skills, memory, web, terminal"
log "config: plugins.enabled includes reference-library and latency-trace"
if [[ "$DRY_RUN" -eq 0 ]]; then
  "$HERMES_PYTHON" - "$CONFIG_PATH" "$CONTEXT_DIR" <<'PY'
import sys
from pathlib import Path

import yaml

config_path = Path(sys.argv[1])
context_dir = sys.argv[2]
if config_path.exists():
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
else:
    config = {}

config.setdefault("terminal", {})["cwd"] = context_dir
config.setdefault("reference_library", {})["catalog"] = str(
    Path(context_dir) / "reference_catalog.yaml"
)
config.setdefault("platform_toolsets", {})["api_server"] = [
    "reference_readonly",
    "no_mcp",
]

disabled = config.setdefault("agent", {}).get("disabled_toolsets") or []
for toolset in ("file", "skills", "memory", "web", "terminal"):
    if toolset not in disabled:
        disabled.append(toolset)
config["agent"]["disabled_toolsets"] = disabled

enabled_plugins = config.setdefault("plugins", {}).get("enabled") or []
for plugin in ("reference-library", "latency-trace"):
    if plugin not in enabled_plugins:
        enabled_plugins.append(plugin)
config["plugins"]["enabled"] = enabled_plugins

config_path.write_text(
    yaml.safe_dump(config, sort_keys=False, allow_unicode=False),
    encoding="utf-8",
)
PY
fi

log "profile modules and API tool policy are current"
