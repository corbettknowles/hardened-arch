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

old = '''      log "SOURCE VERIFICATION FAILED for $base. No checksum was changed or bypassed."
      unset 'ACTIVE[$base]'
      write_status "$base" source-verification-failed
      return 1
'''

new = '''      log "SOURCE VERIFICATION FAILED for $base. No checksum was changed or bypassed."

      # Auto-fetched dependencies may reference a temporarily broken or mutable
      # upstream patch URL. Never accept a mismatched checksum. When enabled,
      # fall back only to the official Arch repository's signed binary package.
      if [[ "$BINARY_FALLBACK" == 1 && "$dir" == "$AUTO_RECIPES/"* ]]; then
        local fallback_pkg
        fallback_pkg="$(dep_name "$requested")"
        log "Source verification failed for auto-fetched $base; installing Arch-signed binary fallback $fallback_pkg."
        if sudo pacman -S --noconfirm --needed --asdeps "$fallback_pkg"; then
          printf '%s\t%s\t%s\t%s\n' \
            "$(now)" "$base" "$fallback_pkg" "source-verification-fallback" \
            >> "$STATE/binary-fallbacks.tsv"
          unset 'ACTIVE[$base]'
          write_status "$base" binary-fallback
          return 0
        fi
        log "Signed binary fallback also failed for $base."
      fi

      unset 'ACTIVE[$base]'
      write_status "$base" source-verification-failed
      return 1
'''

if new in text:
    print("Source-verification fallback fix already installed.")
    raise SystemExit(0)

if text.count(old) != 1:
    raise SystemExit(
        f"Could not locate source-verification failure block safely; matches={text.count(old)}"
    )

backup = path.with_name(
    path.name + ".before-source-binary-fallback-" +
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
