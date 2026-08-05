#!/usr/bin/env bash
set -euo pipefail

USER_NAME="${SUDO_USER:-${USER:-corbett}}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
[[ -n "$USER_HOME" ]] || USER_HOME="/home/corbett"

XFCE_STAGE="${HARDENED_XFCE_STAGE:-$USER_HOME/xfce-source-stage/rootfs}"
KDE_PREFIX="${HARDENED_KDE_PREFIX:-$USER_HOME/kde/usr}"

if [[ $# -ge 1 ]]; then
    LIVE_ROOT="$1"
else
    LATEST_WORK="$(ls -1dt "$USER_HOME"/hardened-arch-iso-build-xfce-* 2>/dev/null | head -n 1 || true)"
    [[ -n "$LATEST_WORK" ]] || {
        echo "FAIL: No Xfce ISO work directory found under $USER_HOME"
        exit 1
    }
    LIVE_ROOT="$LATEST_WORK/live-root"
fi

if [[ $EUID -ne 0 ]]; then
    exec sudo env \
        HARDENED_XFCE_STAGE="$XFCE_STAGE" \
        HARDENED_KDE_PREFIX="$KDE_PREFIX" \
        "$0" "$LIVE_ROOT"
fi

checkpoint() {
    printf '%s  XFCE DIAGNOSTIC: %s\n' "$(date -Is)" "$*"
}

checkpoint "starting read-only conversion test"
echo "LIVE ROOT:  $LIVE_ROOT"
echo "XFCE STAGE: $XFCE_STAGE"
echo "KDE PREFIX: $KDE_PREFIX"

[[ -d "$LIVE_ROOT" ]] || { echo "FAIL: live root missing: $LIVE_ROOT"; exit 1; }
[[ -d "$XFCE_STAGE" ]] || { echo "FAIL: Xfce stage missing: $XFCE_STAGE"; exit 1; }
[[ -d "$KDE_PREFIX" && ! -L "$KDE_PREFIX" ]] || {
    echo "FAIL: KDE prefix must be a real directory: $KDE_PREFIX"
    exit 1
}

python3 - "$LIVE_ROOT" "$XFCE_STAGE" "$KDE_PREFIX" <<'PY'
from __future__ import annotations

import filecmp
import os
from pathlib import Path
import sys
import time

root = Path(sys.argv[1])
xfce_stage = Path(sys.argv[2]).resolve()
kde_prefix = Path(sys.argv[3]).resolve()

def cp(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  XFCE DIAGNOSTIC: {message}", flush=True)

cp("Python started")

required_xfce = (
    "usr/bin/startxfce4",
    "usr/bin/xfce4-session",
    "usr/bin/xfce4-panel",
    "usr/bin/xfce4-settings-manager",
    "usr/bin/xfdesktop",
    "usr/bin/xfwm4",
    "usr/bin/Thunar",
    "usr/bin/xfce4-power-manager",
    "usr/bin/upower",
    "usr/share/xsessions/xfce.desktop",
)

for relative in required_xfce:
    candidate = xfce_stage / relative
    if not candidate.exists():
        raise SystemExit(f"FAIL: required Xfce file missing: {candidate}")
cp("required Xfce files validated")

cp("starting libtool archive scan")
staged_la = list(xfce_stage.rglob("*.la"))
if staged_la:
    raise SystemExit("FAIL: Xfce stage contains .la files: " + ", ".join(map(str, staged_la[:10])))
cp("libtool archive scan complete: 0 found")

cp("starting KDE source-object enumeration and sort")
source_objects = sorted(
    (
        item
        for item in kde_prefix.rglob("*")
        if item.is_file() or item.is_symlink()
    ),
    key=lambda item: len(item.parts),
    reverse=True,
)
cp(f"KDE source-object enumeration complete: {len(source_objects)} objects")

missing = 0
matching = 0
conflicts = 0
errors = 0

cp("starting read-only KDE payload comparison")
for index, source in enumerate(source_objects, 1):
    relative = source.relative_to(kde_prefix)
    destination = root / "usr" / relative

    try:
        if not os.path.lexists(destination):
            missing += 1
        elif source.is_symlink():
            if destination.is_symlink() and os.readlink(source) == os.readlink(destination):
                matching += 1
            else:
                conflicts += 1
                if conflicts <= 20:
                    print(f"CONFLICT: {destination}", flush=True)
        elif destination.is_file() and not destination.is_symlink():
            if filecmp.cmp(source, destination, shallow=False):
                matching += 1
            else:
                conflicts += 1
                if conflicts <= 20:
                    print(f"CONFLICT: {destination}", flush=True)
        else:
            conflicts += 1
            if conflicts <= 20:
                print(f"CONFLICT: {destination}", flush=True)
    except Exception as exc:
        errors += 1
        if errors <= 20:
            print(f"ERROR: {source} -> {destination}: {exc!r}", flush=True)

    if index % 2000 == 0:
        cp(
            f"comparison progress {index}/{len(source_objects)} "
            f"matching={matching} missing={missing} conflicts={conflicts} errors={errors}"
        )

cp(
    f"comparison complete: matching={matching} missing={missing} "
    f"conflicts={conflicts} errors={errors}"
)

cp("starting KDE directory enumeration and sort")
source_directories = sorted(
    (
        item
        for item in kde_prefix.rglob("*")
        if item.is_dir() and not item.is_symlink()
    ),
    key=lambda item: len(item.parts),
    reverse=True,
)
cp(f"KDE directory enumeration complete: {len(source_directories)} directories")

cp("Python read-only checks complete")
PY

checkpoint "starting rsync dry run of Xfce overlay"
rsync -aHAXn --numeric-ids --stats "$XFCE_STAGE/" "$LIVE_ROOT/"
checkpoint "rsync dry run complete"

checkpoint "READ-ONLY DIAGNOSTIC PASS"
echo "No files were created, removed, overwritten, or activated by this diagnostic."
