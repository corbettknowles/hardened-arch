#!/usr/bin/env bash
set -Eeuo pipefail

BUILD_SCRIPT="${1:-$HOME/arch-rebuild/tools/arch_build.sh}"

python3 - "$BUILD_SCRIPT" <<'PY'
from pathlib import Path
from datetime import datetime
import subprocess
import sys

path = Path(sys.argv[1]).expanduser()
text = path.read_text(encoding="utf-8")

old = '''    if [[ -n "${ACTIVE[$base]:-}" ]]; then
      log "Dependency cycle detected at $base"
      return 1
    fi
'''

new = '''    if [[ -n "${ACTIVE[$base]:-}" ]]; then
      log "Bootstrap dependency cycle detected at $base."
      if [[ "$BINARY_FALLBACK" == 1 ]]; then
        local bootstrap_pkg
        bootstrap_pkg="$(dep_name "$requested")"
        log "Breaking bootstrap cycle with Arch-signed binary $bootstrap_pkg; it may be rebuilt from source later."
        if sudo pacman -S --noconfirm --needed --asdeps "$bootstrap_pkg"; then
          return 0
        fi
      fi
      log "Unable to break dependency cycle at $base."
      return 1
    fi
'''

if new in text:
    print("Bootstrap-cycle fix already installed.")
    raise SystemExit(0)

if text.count(old) != 1:
    raise SystemExit(
        f"Could not locate cycle block safely; matches={text.count(old)}"
    )

backup = path.with_name(
    path.name + ".before-bootstrap-cycle-fix-" +
    datetime.now().strftime("%Y%m%d-%H%M%S")
)
backup.write_text(text, encoding="utf-8")

patched = text.replace(old, new, 1)
path.write_text(patched, encoding="utf-8")
path.chmod(0o755)

check = subprocess.run(
    ["bash", "-n", str(path)],
    text=True,
    capture_output=True,
)
if check.returncode:
    path.write_text(text, encoding="utf-8")
    raise SystemExit("Syntax check failed; original restored:\n" + check.stderr)

print("Patched:", path)
print("Backup: ", backup)
print("Bash syntax: valid")
PY
