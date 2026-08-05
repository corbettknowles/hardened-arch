#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=/home/corbett/xorg-source-stage/rootfs
BUILDER=/home/corbett/hardened-clean-builder/hardened_clean_iso_builder.py
POLICY=/home/corbett/hardened-clean-builder/hardened-build-policy.json
RUNNER=/home/corbett/hardened-clean-builder/run_clean_builder.py
EXPECTED_URL='https://sourceforge.net/projects/hardened-software-update/files/'

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ARCHIVE="/home/corbett/hardened-builder-archives/recovery-console-$STAMP"

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

[[ $EUID -eq 0 ]] || fail "Run with sudo."
[[ -d "$ROOT" ]] || fail "Runtime root is missing: $ROOT"
[[ -f "$BUILDER" ]] || fail "Clean builder is missing: $BUILDER"
[[ -f "$POLICY" ]] || fail "Policy is missing: $POLICY"
[[ -x "$RUNNER" ]] || fail "Clean-builder launcher is missing: $RUNNER"

echo '===== UPDATE URL LOCK ====='
python3 - "$POLICY" "$BUILDER" "$EXPECTED_URL" <<'PY'
from pathlib import Path
import json
import sys

policy_path = Path(sys.argv[1])
builder_path = Path(sys.argv[2])
expected = sys.argv[3]

policy = json.loads(policy_path.read_text(encoding="utf-8"))
actual = policy.get("repo_url")

if actual != expected:
    raise SystemExit(
        f"FAIL: policy repo_url is {actual!r}; expected {expected!r}"
    )

builder_text = builder_path.read_text(encoding="utf-8")
if expected not in builder_text:
    raise SystemExit(
        "FAIL: exact updater URL is not present in the approved builder."
    )

print(f"PASS: policy repo_url = {actual}")
print("PASS: approved builder contains the exact updater URL")
PY

for unit in \
    "$ROOT/usr/lib/systemd/system/getty@.service" \
    "$ROOT/usr/lib/systemd/system/serial-getty@.service" \
    "$ROOT/usr/lib/systemd/system/timers.target"
do
    [[ -e "$unit" ]] || fail "Required systemd unit is missing: $unit"
done

echo
echo '===== ARCHIVING CURRENT RECOVERY FILES ====='
mkdir -p "$ARCHIVE"

archive_path() {
    local path=$1
    local relative=${path#"$ROOT/"}

    if [[ -e "$path" || -L "$path" ]]; then
        mkdir -p "$ARCHIVE/$(dirname "$relative")"
        cp -a --no-dereference "$path" "$ARCHIVE/$relative"
        echo "ARCHIVED: /$relative"
    else
        mkdir -p "$ARCHIVE/ABSENT/$(dirname "$relative")"
        : > "$ARCHIVE/ABSENT/$relative"
        echo "ABSENT:   /$relative"
    fi
}

archive_path "$ROOT/usr/local/sbin/hardened-collect-logs"
archive_path "$ROOT/usr/local/bin/collect-boot-logs"
archive_path "$ROOT/usr/lib/systemd/system/hardened-boot-diagnostics.service"
archive_path "$ROOT/usr/lib/systemd/system/hardened-boot-diagnostics.timer"
archive_path "$ROOT/etc/systemd/system/timers.target.wants/hardened-boot-diagnostics.timer"
archive_path "$ROOT/etc/systemd/system/getty.target.wants/getty@tty2.service"
archive_path "$ROOT/etc/systemd/system/getty.target.wants/serial-getty@ttyS0.service"
archive_path "$ROOT/etc/issue.d/90-hardened-recovery.issue"

echo
echo '===== INSTALLING LOG COLLECTOR ====='
install -d -m 0755 -o root -g root \
    "$ROOT/usr/local/sbin" \
    "$ROOT/usr/local/bin" \
    "$ROOT/usr/lib/systemd/system" \
    "$ROOT/etc/issue.d"

cat > "$ROOT/usr/local/sbin/hardened-collect-logs" <<'COLLECTOR'
#!/usr/bin/env bash
set -u

OUTDIR=/var/log/hardened-recovery
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
WORK="$OUTDIR/recovery-$STAMP"
ARCHIVE="$OUTDIR/recovery-$STAMP.tar.gz"

mkdir -p "$WORK"
chmod 0755 "$OUTDIR" "$WORK"

capture() {
    local name=$1
    shift

    {
        printf 'COMMAND:'
        printf ' %q' "$@"
        printf '\n\n'
        "$@"
    } > "$WORK/$name.txt" 2>&1 || true
}

{
    echo "UTC: $(date -u --iso-8601=seconds 2>/dev/null || date -u)"
    echo "LOCAL: $(date --iso-8601=seconds 2>/dev/null || date)"
    echo
    uname -a
    echo
    printf 'Kernel command line: '
    cat /proc/cmdline 2>/dev/null || true
} > "$WORK/00-system.txt" 2>&1

capture 10-systemctl-failed systemctl --failed --no-pager --full
capture 11-display-manager-status systemctl status display-manager.service sddm.service --no-pager --full
capture 12-display-manager-properties systemctl show display-manager.service sddm.service
capture 20-journal-boot journalctl -b --no-pager -o short-precise
capture 21-journal-sddm journalctl -b -u display-manager.service -u sddm.service --no-pager -o short-precise
capture 22-journal-errors journalctl -b -p warning..alert --no-pager -o short-precise
capture 30-dmesg dmesg -T
capture 31-loginctl loginctl list-sessions --no-pager
capture 32-seats loginctl seat-status seat0 --no-pager
capture 40-display-link ls -l /etc/systemd/system/display-manager.service
capture 41-sddm-config grep -RnsE '^[[:space:]]*(DisplayServer|Session|RememberLastSession|User|MinimumUid)[[:space:]]*=' /etc/sddm.conf /etc/sddm.conf.d
capture 42-plasma-sessions find /usr/share/wayland-sessions /usr/share/xsessions -maxdepth 2 -type f -print
capture 43-wayland-runtime find /run/user -maxdepth 3 \( -type s -o -type f \) -print
capture 50-drm ls -l /dev/dri
capture 51-input ls -l /dev/input
capture 52-modules lsmod

tar -C "$OUTDIR" -czf "$ARCHIVE" "$(basename "$WORK")" 2>/dev/null || true
chmod -R a+rX "$WORK" 2>/dev/null || true
chmod 0644 "$ARCHIVE" 2>/dev/null || true
ln -sfn "$(basename "$ARCHIVE")" "$OUTDIR/latest.tar.gz"

echo
echo "Recovery logs:"
echo "  $ARCHIVE"
echo "  $WORK"
echo
echo "Copy latest.tar.gz to a mounted USB drive from this console."
COLLECTOR

chmod 0755 "$ROOT/usr/local/sbin/hardened-collect-logs"
ln -sfn ../sbin/hardened-collect-logs \
    "$ROOT/usr/local/bin/collect-boot-logs"

cat > "$ROOT/usr/lib/systemd/system/hardened-boot-diagnostics.service" <<'SERVICE'
[Unit]
Description=Capture delayed hardened live-boot diagnostics
After=systemd-journald.service systemd-udev-settle.service
Wants=systemd-journald.service
ConditionPathExists=/usr/local/sbin/hardened-collect-logs

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/hardened-collect-logs
Nice=10
IOSchedulingClass=idle
SERVICE

cat > "$ROOT/usr/lib/systemd/system/hardened-boot-diagnostics.timer" <<'TIMER'
[Unit]
Description=Capture hardened live-boot diagnostics after startup

[Timer]
OnBootSec=60s
AccuracySec=5s
Unit=hardened-boot-diagnostics.service
Persistent=false

[Install]
WantedBy=timers.target
TIMER

cat > "$ROOT/etc/issue.d/90-hardened-recovery.issue" <<'ISSUE'

Hardened recovery console
Log in as the hardened live user.
Collect a fresh diagnostic bundle with:
  sudo collect-boot-logs

Automatic delayed diagnostics:
  /var/log/hardened-recovery/latest.tar.gz

ISSUE

chmod 0644 \
    "$ROOT/usr/lib/systemd/system/hardened-boot-diagnostics.service" \
    "$ROOT/usr/lib/systemd/system/hardened-boot-diagnostics.timer" \
    "$ROOT/etc/issue.d/90-hardened-recovery.issue"

echo
echo '===== ENABLING RECOVERY CONSOLES ====='
systemctl --root="$ROOT" enable getty@tty2.service
systemctl --root="$ROOT" enable serial-getty@ttyS0.service
systemctl --root="$ROOT" enable hardened-boot-diagnostics.timer

echo
echo '===== SOURCE-ROOT VERIFICATION ====='
test -x "$ROOT/usr/local/sbin/hardened-collect-logs"
test -L "$ROOT/usr/local/bin/collect-boot-logs"
test -L "$ROOT/etc/systemd/system/getty.target.wants/getty@tty2.service"
test -L "$ROOT/etc/systemd/system/getty.target.wants/serial-getty@ttyS0.service"
test -L "$ROOT/etc/systemd/system/timers.target.wants/hardened-boot-diagnostics.timer"

grep -F "$EXPECTED_URL" "$POLICY"
grep -F "$EXPECTED_URL" "$BUILDER"

echo "PASS: tty2 login console enabled"
echo "PASS: ttyS0 serial login console enabled"
echo "PASS: delayed automatic log bundle enabled"
echo "PASS: manual collect-boot-logs command installed"
echo "Archive: $ARCHIVE"

echo
echo '===== CLEAN-BUILDER AUDIT ====='
"$RUNNER" build --audit-only

LATEST_RUN=$(
    find /home/corbett/hardened-clean-build-runs \
        -maxdepth 1 \
        -type d \
        -name 'run-*' \
        -printf '%T@ %p\n' 2>/dev/null |
    sort -nr |
    awk 'NR==1 {sub(/^[^ ]+ /, ""); print; exit}'
)

[[ -n "$LATEST_RUN" ]] || fail "Could not locate the completed audit run."

LIVE="$LATEST_RUN/work/live-root"

echo
echo '===== FRESH LIVE-ROOT RECOVERY VERIFICATION ====='
for path in \
    /usr/local/sbin/hardened-collect-logs \
    /usr/local/bin/collect-boot-logs \
    /usr/lib/systemd/system/hardened-boot-diagnostics.service \
    /usr/lib/systemd/system/hardened-boot-diagnostics.timer \
    /etc/systemd/system/getty.target.wants/getty@tty2.service \
    /etc/systemd/system/getty.target.wants/serial-getty@ttyS0.service \
    /etc/systemd/system/timers.target.wants/hardened-boot-diagnostics.timer \
    /etc/issue.d/90-hardened-recovery.issue
do
    if [[ -e "$LIVE$path" || -L "$LIVE$path" ]]; then
        echo "PASS: $path"
    else
        fail "Fresh live root is missing $path"
    fi
done

echo
echo 'RECOVERY CONSOLE AND UPDATE URL: VERIFIED'
echo 'Normal GUI failure path: Ctrl+Alt+F2, then log in.'
echo 'Serial/QEMU path: ttyS0 at 115200 baud.'
echo 'Manual bundle command: sudo collect-boot-logs'
echo 'Automatic bundle: /var/log/hardened-recovery/latest.tar.gz'
