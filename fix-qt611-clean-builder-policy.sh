#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/home/corbett/xorg-source-stage/rootfs
INSTALL=/home/corbett/hardened-clean-builder
POLICY="$INSTALL/hardened-build-policy.json"
APPROVER="$INSTALL/approve_clean_builder_change.py"
RUNNER="$INSTALL/run_clean_builder.py"

QWAYLAND=/usr/lib/qt6/plugins/platforms/libqwayland.so
LAYER_SOURCE=/home/corbett/kde/usr/lib/plugins/wayland-shell-integration/liblayer-shell.so
LAYER_DEST=/usr/lib/qt6/plugins/wayland-shell-integration/liblayer-shell.so
BUILD_LAYER_LINK=/home/corbett/kde/usr/lib/libLayerShellQtInterface.so.6
STAGE_LAYER_LINK="$ROOT/usr/lib/libLayerShellQtInterface.so.6"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ARCHIVE=/home/corbett/hardened-builder-archives/qt611-policy-repair-$STAMP

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

[[ $EUID -eq 0 ]] || fail "Run with sudo."
[[ -d "$ROOT" ]] || fail "Runtime root missing: $ROOT"
[[ -f "$POLICY" ]] || fail "Installed policy missing: $POLICY"
[[ -x "$APPROVER" ]] || fail "Approval tool missing: $APPROVER"
[[ -x "$RUNNER" ]] || fail "Clean-builder launcher missing: $RUNNER"
[[ -f "$ROOT$QWAYLAND" ]] || fail "Qt 6.11.1 Wayland QPA plugin is missing: $ROOT$QWAYLAND"
[[ -f "$LAYER_SOURCE" ]] || fail "LayerShell plugin source is missing: $LAYER_SOURCE"

BUILD_LAYER=$(readlink -f "$BUILD_LAYER_LINK")
STAGE_LAYER=$(readlink -f "$STAGE_LAYER_LINK")

[[ -f "$BUILD_LAYER" ]] || fail "Build LayerShell runtime missing: $BUILD_LAYER"
[[ -f "$STAGE_LAYER" ]] || fail "Staged LayerShell runtime missing: $STAGE_LAYER"
cmp -s "$BUILD_LAYER" "$STAGE_LAYER" || fail "Build and staged LayerShell runtime libraries are not exact matches."

mkdir -p "$ARCHIVE"

printf '===== VERIFY QT 6.11.1 WAYLAND QPA PLUGIN =====\n'
chroot "$ROOT" /usr/bin/pacman -Qo "$QWAYLAND" | tee "$ARCHIVE/qwayland-package-owner.txt"

QWAYLAND_LDD=$(chroot "$ROOT" /usr/bin/ldd "$QWAYLAND")
printf '%s\n' "$QWAYLAND_LDD" | tee "$ARCHIVE/qwayland-ldd.txt"
if grep -q 'not found' <<<"$QWAYLAND_LDD"; then
    fail "Qt Wayland QPA plugin has unresolved libraries. No changes made."
fi

cp -a "$POLICY" "$ARCHIVE/hardened-build-policy.json.before"

DEST_ON_HOST="$ROOT$LAYER_DEST"
mkdir -p "$(dirname "$DEST_ON_HOST")"

if [[ -e "$DEST_ON_HOST" || -L "$DEST_ON_HOST" ]]; then
    cp -a "$DEST_ON_HOST" "$ARCHIVE/liblayer-shell.so.before"
else
    printf 'ABSENT BEFORE REPAIR\n' > "$ARCHIVE/liblayer-shell.so.before.ABSENT"
fi

printf '\n===== STAGE VERIFIED LAYERSHELL PLUGIN =====\n'
install -m 0755 -o root -g root "$LAYER_SOURCE" "$DEST_ON_HOST"

sha256sum "$LAYER_SOURCE" "$DEST_ON_HOST" | tee "$ARCHIVE/layer-shell-sha256.txt"
cmp -s "$LAYER_SOURCE" "$DEST_ON_HOST" || fail "Staged LayerShell plugin is not an exact copy of the verified source."

LAYER_LDD=$(chroot "$ROOT" /usr/bin/ldd "$LAYER_DEST")
printf '%s\n' "$LAYER_LDD" | tee "$ARCHIVE/layer-shell-ldd.txt"
if grep -q 'not found' <<<"$LAYER_LDD"; then
    fail "Staged LayerShell plugin has unresolved libraries. Audit not run."
fi

printf '\n===== CORRECT OBSOLETE QT PLUGIN POLICY =====\n'
chmod u+w "$POLICY"

python3 - "$POLICY" <<'PY'
from pathlib import Path
import json
import sys

path = Path(sys.argv[1])
policy = json.loads(path.read_text(encoding="utf-8"))

old = [
    "/usr/lib/qt6/plugins/platforms/libqwayland-egl.so",
    "/usr/lib/qt6/plugins/platforms/libqwayland-generic.so",
]
new = [
    "/usr/lib/qt6/plugins/platforms/libqwayland.so",
]

groups = policy["required_paths"]["any_of"]

if old in groups:
    groups[groups.index(old)] = new
elif new in groups:
    print("Policy already uses the Qt 6.11.1 libqwayland.so path.")
else:
    raise SystemExit("FAIL: Expected old or corrected Qt Wayland policy group was not found.")

path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
PY

printf '\nThe approval tool will now show the exact policy diff.\n'
"$APPROVER" --reason "Correct Qt 6.11.1 Arch plugin requirement from obsolete libqwayland-egl/generic names to qt6-base libqwayland.so; stage the already verified matching LayerShellQt plugin in Qt's configured plugin directory."

printf '\n===== RUN CLEAN READ-ONLY AUDIT =====\n'
"$RUNNER" build --audit-only

printf '\nREPAIR AND AUDIT COMPLETE\n'
printf 'Archive: %s\n' "$ARCHIVE"
