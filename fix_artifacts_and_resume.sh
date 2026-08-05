#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${1:-$HOME/arch-rebuild}"
BUILD_SCRIPT="$ROOT/tools/arch_build.sh"
LAUNCHER="$ROOT/tools/start_arch_build.sh"

python3 - "$BUILD_SCRIPT" <<'PY'
from pathlib import Path
from datetime import datetime
import subprocess
import sys

path = Path(sys.argv[1]).expanduser()
text = path.read_text(encoding="utf-8", errors="surrogateescape")

old = '''    local -a artifacts=()
    mapfile -t artifacts < <(cd "$dir" && makepkg --packagelist)
    ((${#artifacts[@]})) || { log "No artifacts listed for $base"; return 1; }
    for dep in "${artifacts[@]}"; do
      [[ -f "$dep" ]] || { log "Missing expected artifact: $dep"; return 1; }
    done
'''

new = '''    # makepkg --packagelist may advertise an automatic *-debug package even
    # when the finished package contains no eligible binaries and therefore
    # no debug archive is emitted. Index only artifacts that actually exist.
    local -a expected_artifacts=() artifacts=()
    mapfile -t expected_artifacts < <(cd "$dir" && makepkg --packagelist)

    for dep in "${expected_artifacts[@]}"; do
      if [[ -f "$dep" ]]; then
        artifacts+=("$dep")
      else
        log "Artifact not emitted by makepkg; ignoring prediction: $dep"
      fi
    done

    if ((${#artifacts[@]} == 0)); then
      log "No package artifacts were produced for $base."
      unset 'ACTIVE[$base]'
      write_status "$base" artifact-missing
      return 1
    fi
'''

if old not in text:
    if "Artifact not emitted by makepkg; ignoring prediction" in text:
        print("Artifact handling fix is already installed.")
        raise SystemExit(0)
    raise SystemExit("Could not locate the old artifact-check block safely.")

backup = path.with_name(
    path.name + ".before-artifact-fix-" +
    datetime.now().strftime("%Y%m%d-%H%M%S")
)
backup.write_text(text, encoding="utf-8", errors="surrogateescape")

patched = text.replace(old, new, 1)
path.write_text(patched, encoding="utf-8")
path.chmod(0o755)

check = subprocess.run(
    ["bash", "-n", str(path)],
    text=True,
    capture_output=True,
)
if check.returncode:
    path.write_text(text, encoding="utf-8", errors="surrogateescape")
    raise SystemExit("Syntax check failed; original restored:\n" + check.stderr)

print("Patched:", path)
print("Backup: ", backup)
print("Bash syntax: valid")
PY

export XDG_RUNTIME_DIR="/tmp/podman-runtime-$(id -u)"
export CONTAINERS_CGROUP_MANAGER="cgroupfs"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

podman --cgroup-manager=cgroupfs rm -f arch-rebuild-builder \
  >/dev/null 2>&1 || true

rm -f "$ROOT/build-state/BUILD_COMPLETE.json"

if [[ ! -x "$LAUNCHER" ]]; then
    echo "ERROR: launcher not found or not executable: $LAUNCHER"
    exit 1
fi

exec "$LAUNCHER" "$ROOT"
