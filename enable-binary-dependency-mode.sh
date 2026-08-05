#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$HOME/arch-rebuild"
RUNNER="$ROOT/tools/arch_build.sh"
STAMP="$(date +%Y%m%d-%H%M%S)"

export XDG_RUNTIME_DIR="/tmp/podman-runtime-$(id -u)"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

podman --cgroup-manager=cgroupfs stop -t 20 \
  arch-rebuild-builder 2>/dev/null || true

podman --cgroup-manager=cgroupfs rm -f \
  arch-rebuild-builder 2>/dev/null || true

cp -a "$RUNNER" \
  "$RUNNER.before-binary-dependency-mode-$STAMP"

python3 - "$RUNNER" <<'PY'
from pathlib import Path
import re
import subprocess
import sys

path = Path(sys.argv[1])
original = path.read_text()
text = original

policy_marker = "# BINARY_FIRST_DEPENDENCY_POLICY_V2"
gate_marker = "# BINARY_DEPENDENCY_GATE_V2"

if policy_marker not in text:
    anchor = "  dep_satisfied() {"

    if anchor not in text:
        raise SystemExit(
            "Could not locate dep_satisfied(); nothing was changed."
        )

    helpers = r'''  # BINARY_FIRST_DEPENDENCY_POLICY_V2
  #
  # Source-build only:
  #   * explicit alpha roots
  #   * Linux/kernel packages
  #   * Qt 6, KDE Frameworks, Plasma and KDE Applications
  #
  # Ordinary dependencies are installed from repositories.
  source_build_allowed() {
    local requested dep roots record dir

    requested="$1"
    dep="$(dep_name "$requested")"
    roots="${ACTIVE_ORDER:-${ORDER:-}}"

    if [[ -n "$roots" && -s "$roots" ]] &&
       grep -Fxq -- "$dep" "$roots"; then
      return 0
    fi

    case "$dep" in
      linux|linux-*|\
      qt6-*|kf6-*|plasma-*|kde-*|\
      kwin|dolphin|libplasma|\
      breeze|breeze-*|oxygen|oxygen-*|\
      kirigami|kirigami-*|\
      kdecoration|kwayland|\
      layer-shell-qt|\
      qqc2-desktop-style|phonon-qt6)
        return 0
        ;;
    esac

    record="$(resolve_recipe "$dep" 2>/dev/null || true)"

    if [[ -n "$record" ]]; then
      dir="${record#*$'\t'}"

      if recipe_srcinfo "$dir" 2>/dev/null |
         awk -F ' = ' '
           $1 ~ /^[[:space:]]*groups$/ &&
           $2 ~ /^(kf6|plasma|kde-applications|qt6)$/ {
             found = 1
           }
           END { exit !found }
         '; then
        return 0
      fi
    fi

    return 1
  }

  install_binary_dependency() {
    local requirement="$1"
    local owner="$2"
    local dep candidate cache record recipe_base

    dep="$(dep_name "$requirement")"

    if dep_satisfied "$requirement"; then
      return 0
    fi

    candidate="$dep"
    cache="$WORK/download-cache/pacman"

    sudo install -d -m 0777 "$cache"

    if ! pacman -Si "$candidate" >/dev/null 2>&1; then
      record="$(resolve_recipe "$dep" 2>/dev/null || true)"

      if [[ -n "$record" ]]; then
        recipe_base="${record%%$'\t'*}"

        if pacman -Si "$recipe_base" >/dev/null 2>&1; then
          candidate="$recipe_base"
        fi
      fi
    fi

    log "BINARY DEP $candidate — required by $owner; source rebuild omitted."

    if ! sudo pacman -S \
      --noconfirm \
      --needed \
      --asdeps \
      --cachedir "$cache" \
      "$candidate"; then

      log "BINARY DEPENDENCY INSTALL FAILED: $candidate required by $owner."
      return 1
    fi

    if ! dep_satisfied "$requirement"; then
      log "BINARY DEPENDENCY UNSATISFIED after install: $requirement."
      return 1
    fi

    printf '%s\n' "$candidate" >> "$STATE/binary-deps.txt"
    sort -u "$STATE/binary-deps.txt" \
      -o "$STATE/binary-deps.txt"

    return 0
  }

'''

    text = text.replace(anchor, helpers + anchor, 1)

if gate_marker not in text:
    lines = text.splitlines(keepends=True)

    fallback_index = None

    for index, line in enumerate(lines):
        if "Final dependency fallback:" in line:
            fallback_index = index
            break

    if fallback_index is None:
        raise SystemExit(
            "Could not locate Final dependency fallback; nothing was changed."
        )

    dependency_index = None

    for index in range(fallback_index - 1, max(-1, fallback_index - 180), -1):
        line = lines[index]

        if (
            'dep_name "$miss"' in line
            and re.search(r'\bdep\s*=', line)
        ):
            dependency_index = index
            break

    if dependency_index is None:
        raise SystemExit(
            "Could not locate dep assignment above fallback; "
            "nothing was changed."
        )

    indent = re.match(r"\s*", lines[dependency_index]).group()

    gate = f'''{indent}{gate_marker}
{indent}if ! source_build_allowed "$miss"; then
{indent}  if install_binary_dependency "$miss" "$base"; then
{indent}    continue
{indent}  fi

{indent}  write_status "$base" binary-dependency-failed
{indent}  return 4
{indent}fi
'''

    lines.insert(dependency_index + 1, gate)
    text = "".join(lines)

path.write_text(text)
path.chmod(0o755)

check = subprocess.run(
    ["bash", "-n", str(path)],
    text=True,
    capture_output=True,
)

if check.returncode:
    path.write_text(original)
    raise SystemExit(
        "Syntax check failed; original runner restored:\n"
        + check.stderr
    )

print("Binary dependency mode installed.")
print("Runner syntax is valid.")
PY

touch "$ROOT/build-state/binary-deps.txt"

sed -i '/^rust$/d' \
  "$ROOT/build-state/completed.txt"

printf '%s\n' rust >> \
  "$ROOT/build-state/binary-deps.txt"

sort -u \
  "$ROOT/build-state/binary-deps.txt" \
  -o "$ROOT/build-state/binary-deps.txt"

rm -f "$ROOT/build-state/BUILD_COMPLETE.json"

echo
echo "Installed policy markers:"
grep -nE \
  'BINARY_FIRST_DEPENDENCY_POLICY|BINARY_DEPENDENCY_GATE' \
  "$RUNNER"

echo
echo "Restarting builder..."

"$RUNNER" resume \
  --root "$ROOT" \
  --jobs 1 \
  --cpus 2
