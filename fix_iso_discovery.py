#!/usr/bin/env python3
"""
Repair Hardened Arch optical-media discovery and rebuild the ISO.

This keeps the normal boot hardened:
  * scans optical /dev/sr* devices only
  * mounts media read-only
  * requires both /rootfs.ext2 and the exact Hardened Arch media marker
  * exposes no early root shell
  * disables systemd-boot command-line editing

It also adds safe diagnostic entries that force /dev/sr0 or /dev/sr1
without opening a shell.

Run:
    sudo python3 /home/corbett/fix_iso_discovery.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


ISO_ROOT = Path("/home/corbett/iso-systemd")
INIT_ROOT = Path("/home/corbett/initramfs-root")
TOYBOX = Path("/home/corbett/toybox-x86_64")
KERNEL_VERSION = "7.1.2"
INITRAMFS = ISO_ROOT / "boot" / f"initramfs-{KERNEL_VERSION}.cpio.gz"
EFI_IMAGE = ISO_ROOT / "efiboot.img"
ISO_BUILDER = Path("/home/corbett/build_hardened_iso.py")

INIT_TEXT = r"""#!/bin/sh

PATH=/bin
export PATH

MODE="live"
ROOTFS_PATH="/rootfs.ext2"
FORCED_DEVICE=""
OVERLAY_MODE="tmpfs"

for arg in $(cat /proc/cmdline 2>/dev/null)
do
    case "$arg" in
        hardened.mode=*) MODE="${arg#*=}" ;;
        hardened.rootfs=*) ROOTFS_PATH="${arg#*=}" ;;
        hardened.iso_device=*) FORCED_DEVICE="${arg#*=}" ;;
        hardened.overlay=*) OVERLAY_MODE="${arg#*=}" ;;
    esac
done

mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /run 2>/dev/null || true

exec </dev/console >/dev/console 2>&1

mkdir -p /mnt/iso /run/lower /run/cow /newroot

[ -e /dev/loop-control ] || mknod /dev/loop-control c 10 237 2>/dev/null || true
i=0
while [ "$i" -lt 8 ]
do
    [ -b "/dev/loop$i" ] || mknod "/dev/loop$i" b 7 "$i" 2>/dev/null || true
    i=$((i + 1))
done

secure_failure()
{
    echo
    echo "============================================================"
    echo "HARDENED ARCH EARLY-BOOT FAILURE"
    echo "============================================================"
    echo "$1"
    echo
    echo "No early root shell is exposed by this media."
    echo
    echo "Detected optical devices:"
    ls -l /dev/sr* 2>/dev/null || true
    echo
    while true
    do
        sleep 3600
    done
}

media_is_valid()
{
    [ -f "/mnt/iso$ROOTFS_PATH" ] || {
        echo "  missing $ROOTFS_PATH"
        return 1
    }

    [ -f /mnt/iso/hardened-arch.media ] || {
        echo "  missing /hardened-arch.media"
        return 1
    }

    MEDIA_MARKER="$(cat /mnt/iso/hardened-arch.media 2>/dev/null)"
    case "$MEDIA_MARKER" in
        "Hardened Arch live media"*)
            return 0
            ;;
        *)
            echo "  media marker did not match"
            return 1
            ;;
    esac
}

try_device()
{
    DEVICE="$1"
    [ -b "$DEVICE" ] || return 1

    echo "Trying optical device $DEVICE"
    umount /mnt/iso 2>/dev/null || true

    if mount -t iso9660 -o ro "$DEVICE" /mnt/iso
    then
        if media_is_valid
        then
            ISO_DEVICE="$DEVICE"
            return 0
        fi
        umount /mnt/iso 2>/dev/null || true
    else
        echo "  explicit iso9660 mount failed; retrying filesystem auto-detection"
        if mount -o ro "$DEVICE" /mnt/iso
        then
            if media_is_valid
            then
                ISO_DEVICE="$DEVICE"
                return 0
            fi
            umount /mnt/iso 2>/dev/null || true
        fi
    fi

    return 1
}

find_iso()
{
    if [ -n "$FORCED_DEVICE" ]
    then
        echo "Forced optical device: $FORCED_DEVICE"
        try_device "$FORCED_DEVICE"
        return $?
    fi

    pass=1
    while [ "$pass" -le 15 ]
    do
        for device in /dev/sr*
        do
            try_device "$device" && return 0
        done

        sleep 1
        pass=$((pass + 1))
    done

    return 1
}

echo "Hardened Arch early userspace started."
echo "Mode: $MODE"
echo "Expected root payload: $ROOTFS_PATH"

find_iso || secure_failure "Could not locate valid Hardened Arch optical media."

echo "Found valid Hardened Arch ISO on $ISO_DEVICE"

LOOP_DEVICE="$(losetup -f 2>/dev/null)"
[ -n "$LOOP_DEVICE" ] || secure_failure "No free loop device is available."

echo "Attaching $ROOTFS_PATH to $LOOP_DEVICE"
losetup -r "$LOOP_DEVICE" "/mnt/iso$ROOTFS_PATH" ||
    secure_failure "Could not attach the ext2 payload."

mount -t ext2 -o ro "$LOOP_DEVICE" /run/lower ||
    secure_failure "Could not mount the ext2 lower root."

if [ "$OVERLAY_MODE" = "tmpfs" ]
then
    mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /run/cow ||
        secure_failure "Could not mount writable tmpfs overlay storage."

    mkdir -p /run/cow/upper /run/cow/work

    mount -t overlay overlay \
        -o lowerdir=/run/lower,upperdir=/run/cow/upper,workdir=/run/cow/work,redirect_dir=off,index=off,metacopy=off \
        /newroot ||
        secure_failure "Could not mount the writable OverlayFS root."
else
    mount --bind /run/lower /newroot ||
        secure_failure "Could not bind the read-only root."
fi

[ -x /newroot/usr/lib/systemd/systemd ] ||
    secure_failure "systemd is missing or not executable."

mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /newroot/run ||
    secure_failure "Could not create the real-root /run."

mkdir -p \
    /newroot/run/initramfs/iso \
    /newroot/run/initramfs/lower \
    /newroot/run/initramfs/cow

mount -o move /mnt/iso /newroot/run/initramfs/iso ||
    secure_failure "Could not preserve the ISO mount."

mount -o move /run/lower /newroot/run/initramfs/lower ||
    secure_failure "Could not preserve the lower-root mount."

if [ "$OVERLAY_MODE" = "tmpfs" ]
then
    mount -o move /run/cow /newroot/run/initramfs/cow ||
        secure_failure "Could not preserve overlay storage."
fi

mount -o move /dev /newroot/dev ||
    secure_failure "Could not transfer /dev."

mount -o move /proc /newroot/proc ||
    secure_failure "Could not transfer /proc."

mount -o move /sys /newroot/sys ||
    secure_failure "Could not transfer /sys."

echo "Starting systemd from the writable live root."
exec /bin/switch_root /newroot /usr/lib/systemd/systemd

secure_failure "switch_root unexpectedly returned."
"""


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def run(args: list[str], *, capture: bool = False, check: bool = True,
        cwd: Path | None = None) -> subprocess.CompletedProcess:
    print("+", " ".join(str(item) for item in args))
    try:
        return subprocess.run(
            [str(item) for item in args],
            check=check,
            text=True,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
        )
    except subprocess.CalledProcessError as exc:
        if capture and exc.stdout:
            print(exc.stdout, file=sys.stderr)
        die(f"Command failed ({exc.returncode}): {' '.join(map(str, args))}")


def require_root() -> None:
    if os.geteuid() != 0:
        die("Run this script with sudo.")


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        die(f"Required command not found: {name}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_paths() -> None:
    required_files = (
        TOYBOX,
        ISO_ROOT / "boot" / f"vmlinuz-{KERNEL_VERSION}",
        ISO_ROOT / "rootfs.ext2",
        EFI_IMAGE,
        ISO_ROOT / "EFI/BOOT/BOOTX64.EFI",
        ISO_BUILDER,
    )
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        die("Missing required files:\n  " + "\n  ".join(missing))


def write_marker() -> None:
    marker = ISO_ROOT / "hardened-arch.media"
    marker.write_text(
        "Hardened Arch live media\n"
        f"Kernel={KERNEL_VERSION}\n"
        "RootPayload=/rootfs.ext2\n"
        "WritableLayer=tmpfs-overlay\n",
        encoding="utf-8",
        newline="\n",
    )
    marker.chmod(0o644)


def ensure_initramfs_tree() -> None:
    if INIT_ROOT.exists():
        shutil.rmtree(INIT_ROOT)

    for relative in ("bin", "dev", "proc", "sys", "run", "mnt/iso", "newroot"):
        (INIT_ROOT / relative).mkdir(parents=True, exist_ok=True)

    installed = INIT_ROOT / "bin/toybox"
    shutil.copy2(TOYBOX, installed)
    installed.chmod(0o755)

    applets = (
        "sh", "mount", "umount", "losetup", "switch_root", "mkdir",
        "mknod", "sleep", "cat", "echo", "ls",
    )
    available = set(run([str(TOYBOX)], capture=True).stdout.split())
    missing = [name for name in applets if name not in available]
    if missing:
        die("Toybox is missing required applets: " + ", ".join(missing))

    for applet in applets:
        (INIT_ROOT / "bin" / applet).symlink_to("toybox")

    init_file = INIT_ROOT / "init"
    init_file.write_text(INIT_TEXT, encoding="utf-8", newline="\n")
    init_file.chmod(0o755)

    for directory, subdirs, files in os.walk(INIT_ROOT):
        os.chown(directory, 0, 0)
        for name in subdirs:
            os.lchown(Path(directory) / name, 0, 0)
        for name in files:
            os.lchown(Path(directory) / name, 0, 0)


def pack_initramfs() -> None:
    INITRAMFS.parent.mkdir(parents=True, exist_ok=True)
    temporary = INITRAMFS.with_suffix(INITRAMFS.suffix + ".tmp")
    temporary.unlink(missing_ok=True)

    with temporary.open("wb") as destination:
        find_process = subprocess.Popen(
            ["find", ".", "-xdev", "-print0"],
            cwd=INIT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert find_process.stdout is not None

        cpio_process = subprocess.Popen(
            ["cpio", "--null", "--create", "--format=newc"],
            cwd=INIT_ROOT,
            stdin=find_process.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        find_process.stdout.close()
        assert cpio_process.stdout is not None

        gzip_process = subprocess.Popen(
            ["gzip", "-9", "-c"],
            stdin=cpio_process.stdout,
            stdout=destination,
            stderr=subprocess.PIPE,
        )
        cpio_process.stdout.close()

        gzip_error = gzip_process.communicate()[1]
        cpio_error = cpio_process.communicate()[1]
        find_error = find_process.communicate()[1]

    if find_process.returncode:
        die(find_error.decode(errors="replace"))
    if cpio_process.returncode:
        die(cpio_error.decode(errors="replace"))
    if gzip_process.returncode:
        die(gzip_error.decode(errors="replace"))

    os.replace(temporary, INITRAMFS)
    INITRAMFS.chmod(0o644)

    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid and gid:
        os.chown(INITRAMFS, int(uid), int(gid))


def write_loader_entries() -> None:
    loader = ISO_ROOT / "loader"
    entries = loader / "entries"
    entries.mkdir(parents=True, exist_ok=True)

    for old in entries.glob("*.conf"):
        old.unlink()

    (loader / "loader.conf").write_text(
        "default hardened.conf\n"
        "timeout 8\n"
        "console-mode max\n"
        "editor no\n",
        encoding="utf-8",
        newline="\n",
    )

    common = (
        "rw console=tty0 console=ttyS0,115200 "
        "hardened.rootfs=/rootfs.ext2 "
        "hardened.overlay=tmpfs"
    )

    entries_data = {
        "hardened.conf": (
            "title Hardened Arch Live\n"
            f"linux /boot/vmlinuz-{KERNEL_VERSION}\n"
            f"initrd /boot/initramfs-{KERNEL_VERSION}.cpio.gz\n"
            f"options {common} hardened.mode=live quiet splash "
            "plymouth.enable=1 loglevel=3 systemd.show_status=auto\n"
        ),
        "hardened-debug.conf": (
            "title Hardened Arch Live (Debug)\n"
            f"linux /boot/vmlinuz-{KERNEL_VERSION}\n"
            f"initrd /boot/initramfs-{KERNEL_VERSION}.cpio.gz\n"
            f"options {common} hardened.mode=debug loglevel=7 "
            "systemd.show_status=yes\n"
        ),
        "hardened-sr0.conf": (
            "title Hardened Arch Live (Force optical /dev/sr0)\n"
            f"linux /boot/vmlinuz-{KERNEL_VERSION}\n"
            f"initrd /boot/initramfs-{KERNEL_VERSION}.cpio.gz\n"
            f"options {common} hardened.mode=debug "
            "hardened.iso_device=/dev/sr0 loglevel=7 "
            "systemd.show_status=yes\n"
        ),
        "hardened-sr1.conf": (
            "title Hardened Arch Live (Force optical /dev/sr1)\n"
            f"linux /boot/vmlinuz-{KERNEL_VERSION}\n"
            f"initrd /boot/initramfs-{KERNEL_VERSION}.cpio.gz\n"
            f"options {common} hardened.mode=debug "
            "hardened.iso_device=/dev/sr1 loglevel=7 "
            "systemd.show_status=yes\n"
        ),
        "hardened-text.conf": (
            "title Hardened Arch Text / Install Environment\n"
            f"linux /boot/vmlinuz-{KERNEL_VERSION}\n"
            f"initrd /boot/initramfs-{KERNEL_VERSION}.cpio.gz\n"
            f"options {common} hardened.mode=debug "
            "systemd.unit=multi-user.target loglevel=5 "
            "systemd.show_status=yes\n"
        ),
    }

    for name, contents in entries_data.items():
        (entries / name).write_text(contents, encoding="utf-8", newline="\n")


def sync_efi_image() -> None:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = EFI_IMAGE.with_name(EFI_IMAGE.name + f".bak-{timestamp}")
    shutil.copy2(EFI_IMAGE, backup)
    print(f"Backed up EFI image: {backup}")

    mountpoint = Path(tempfile.mkdtemp(prefix="efi-discovery-fix-", dir="/mnt"))
    mounted = False

    try:
        result = run(
            ["mount", "-o", "loop,rw,sync", str(EFI_IMAGE), str(mountpoint)],
            capture=True,
            check=False,
        )
        if result.returncode:
            die(f"Could not mount efiboot.img:\n{result.stdout}")
        mounted = True

        destination_entries = mountpoint / "loader/entries"
        if destination_entries.exists():
            shutil.rmtree(destination_entries)

        (mountpoint / "loader").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(
            ISO_ROOT / "loader/loader.conf",
            mountpoint / "loader/loader.conf",
        )
        shutil.copytree(
            ISO_ROOT / "loader/entries",
            destination_entries,
            dirs_exist_ok=True,
        )

        boot = mountpoint / "boot"
        boot.mkdir(parents=True, exist_ok=True)
        for old in boot.glob("initramfs-*"):
            old.unlink()

        shutil.copyfile(
            ISO_ROOT / "boot" / f"vmlinuz-{KERNEL_VERSION}",
            boot / f"vmlinuz-{KERNEL_VERSION}",
        )
        shutil.copyfile(
            INITRAMFS,
            boot / INITRAMFS.name,
        )

        (mountpoint / "EFI/BOOT").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(
            ISO_ROOT / "EFI/BOOT/BOOTX64.EFI",
            mountpoint / "EFI/BOOT/BOOTX64.EFI",
        )

        os.sync()

        checks = (
            (ISO_ROOT / "loader/loader.conf", mountpoint / "loader/loader.conf"),
            (ISO_ROOT / "loader/entries/hardened.conf",
             mountpoint / "loader/entries/hardened.conf"),
            (INITRAMFS, boot / INITRAMFS.name),
        )

        for source, destination in checks:
            if sha256(source) != sha256(destination):
                die(f"EFI synchronization mismatch: {destination}")

    finally:
        if mounted:
            run(["umount", str(mountpoint)], check=False)
        try:
            mountpoint.rmdir()
        except OSError:
            pass


def main() -> None:
    require_root()

    for command in ("mount", "umount", "find", "cpio", "gzip", "sync"):
        require_command(command)

    validate_paths()
    write_marker()

    print("=== Rebuilding diagnostic-safe initramfs ===")
    ensure_initramfs_tree()
    pack_initramfs()
    print(f"Created: {INITRAMFS}")
    print(f"Size: {INITRAMFS.stat().st_size / (1024 * 1024):.2f} MiB")

    print("=== Writing optical discovery boot entries ===")
    write_loader_entries()

    print("=== Synchronizing efiboot.img ===")
    sync_efi_image()

    print("=== Rebuilding verified outer ISO ===")
    run([sys.executable, str(ISO_BUILDER)])

    print()
    print("=== SUCCESS ===")
    print("Optical-media discovery now reports each attempted device.")
    print("The normal entry remains shell-free and requires the exact media marker.")
    print("Safe forced-device entries for /dev/sr0 and /dev/sr1 were added.")


if __name__ == "__main__":
    main()
