#!/usr/bin/env python3
'''Build a clean minimal Toybox initramfs for Hardened Arch.

Run:
    sudo python3 /home/corbett/build_small_initramfs.py
'''

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REQUIRED_APPLETS = (
    "sh", "mount", "umount", "losetup", "switch_root",
    "mkdir", "mknod", "sleep", "cat", "echo", "blkid",
)
OPTIONAL_APPLETS = ("ls", "sync", "dmesg")

INIT_TEXT = r'''#!/bin/sh

PATH=/bin
export PATH

mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /run 2>/dev/null || true

exec </dev/console >/dev/console 2>&1

fail()
{
    echo
    echo "============================================================"
    echo "HARDENED ARCH EARLY-BOOT FAILURE"
    echo "============================================================"
    echo "Dropping to the Toybox rescue shell."
    exec /bin/sh
}

echo "Hardened Arch early userspace started."
echo "Searching for an ISO containing /rootfs.ext2 ..."

mkdir -p /mnt/iso /newroot

[ -e /dev/loop-control ] || mknod /dev/loop-control c 10 237 2>/dev/null || true
i=0
while [ "$i" -lt 8 ]
do
    [ -b "/dev/loop$i" ] || mknod "/dev/loop$i" b 7 "$i" 2>/dev/null || true
    i=$((i + 1))
done

ISO_DEVICE=""
pass=1

while [ "$pass" -le 15 ]
do
    for device in /dev/sr* /dev/sd* /dev/vd* /dev/nvme*n* /dev/mmcblk*
    do
        [ -b "$device" ] || continue
        umount /mnt/iso 2>/dev/null || true

        if mount -t iso9660 -o ro "$device" /mnt/iso 2>/dev/null
        then
            if [ -f /mnt/iso/rootfs.ext2 ]
            then
                ISO_DEVICE="$device"
                break 2
            fi
            umount /mnt/iso 2>/dev/null || true
        fi
    done

    sleep 1
    pass=$((pass + 1))
done

[ -n "$ISO_DEVICE" ] || fail
echo "Found boot ISO on $ISO_DEVICE"

LOOP_DEVICE="$(losetup -f 2>/dev/null)"
[ -n "$LOOP_DEVICE" ] || fail

echo "Attaching /rootfs.ext2 to $LOOP_DEVICE"
losetup -r "$LOOP_DEVICE" /mnt/iso/rootfs.ext2 || fail
mount -t ext2 -o ro "$LOOP_DEVICE" /newroot || fail

[ -x /newroot/usr/lib/systemd/systemd ] || fail
[ -d /newroot/dev ] || fail
[ -d /newroot/proc ] || fail
[ -d /newroot/sys ] || fail
[ -d /newroot/run ] || fail

mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /newroot/run || fail
mkdir -p /newroot/run/initramfs/iso

mount -o move /mnt/iso /newroot/run/initramfs/iso || fail
mount -o move /dev /newroot/dev || fail
mount -o move /proc /newroot/proc || fail
mount -o move /sys /newroot/sys || fail

umount /run 2>/dev/null || true

echo "Switching to the ext2 root and starting systemd."
exec /bin/switch_root /newroot /usr/lib/systemd/systemd

fail
'''


def fatal(message: str) -> None:
    print(f"\nERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+", " ".join(args))
    try:
        return subprocess.run(args, check=True, text=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        fatal(f"Command failed with exit status {exc.returncode}: {' '.join(args)}")
    except OSError as exc:
        fatal(f"Could not execute {' '.join(args)}: {exc}")


def require_root() -> None:
    if os.geteuid() != 0:
        fatal("Run this script with sudo.")


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        fatal(f"Required host command is missing: {name}")


def chown_to_invoking_user(path: Path) -> None:
    uid = os.environ.get("SUDO_UID")
    gid = os.environ.get("SUDO_GID")
    if uid and gid:
        os.chown(path, int(uid), int(gid))


def verify_toybox(toybox: Path) -> set[str]:
    if not toybox.is_file():
        fatal(f"Toybox was not found: {toybox}")
    if not os.access(toybox, os.X_OK):
        fatal(f"Toybox is not executable: {toybox}")

    result = subprocess.run(
        [str(toybox)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    applets = set(result.stdout.split())
    missing = [name for name in REQUIRED_APPLETS if name not in applets]
    if missing:
        fatal("Toybox is missing required applets: " + ", ".join(missing))
    return applets


def safe_work_tree(path: Path) -> None:
    resolved = path.resolve()
    forbidden = {
        Path("/"),
        Path("/home"),
        Path("/home/corbett"),
        Path("/home/corbett/iso-systemd"),
        Path("/home/corbett/linux-7.1.2"),
    }
    if resolved in forbidden or len(resolved.parts) < 4:
        fatal(f"Refusing to remove unsafe work-tree path: {resolved}")


def create_tree(init_root: Path, toybox: Path, applets: set[str]) -> None:
    safe_work_tree(init_root)

    if init_root.exists():
        print(f"Removing old work tree: {init_root}")
        shutil.rmtree(init_root)

    for relative in ("bin", "dev", "proc", "sys", "run", "mnt/iso", "newroot"):
        (init_root / relative).mkdir(parents=True, exist_ok=True)

    installed = init_root / "bin/toybox"
    shutil.copy2(toybox, installed)
    installed.chmod(0o755)

    for name in REQUIRED_APPLETS + OPTIONAL_APPLETS:
        if name in applets:
            (init_root / "bin" / name).symlink_to("toybox")

    init_file = init_root / "init"
    init_file.write_text(INIT_TEXT, encoding="utf-8", newline="\n")
    init_file.chmod(0o755)

    for current, directories, files in os.walk(init_root):
        os.chown(current, 0, 0)
        for name in directories:
            os.lchown(Path(current) / name, 0, 0)
        for name in files:
            os.lchown(Path(current) / name, 0, 0)


def verify_tree(init_root: Path) -> None:
    total = 0
    oversized = []
    forbidden = []

    for path in init_root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        total += size

        if size > 50 * 1024 * 1024:
            oversized.append((size, path))
        if path.suffix.lower() in {".img", ".iso", ".ext2", ".squashfs"}:
            forbidden.append(path)

    if oversized:
        details = "\n".join(
            f"  {size / 1048576:.1f} MiB  {path}" for size, path in oversized
        )
        fatal("Oversized files found in the initramfs tree:\n" + details)

    if forbidden:
        fatal(
            "Filesystem/image files found in the initramfs tree:\n  "
            + "\n  ".join(str(path) for path in forbidden)
        )

    print(f"Initramfs work-tree payload: {total / 1048576:.2f} MiB")


def pack_archive(init_root: Path, output: Path) -> None:
    for command in ("bash", "find", "cpio", "gzip"):
        require_command(command)

    output.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(output) + ".tmp")
    temp.unlink(missing_ok=True)

    env = os.environ.copy()
    env["INITRAMFS_OUTPUT"] = str(output)
    env["INITRAMFS_TEMP"] = str(temp)

    command = r'''
set -euo pipefail
rm -f "$INITRAMFS_TEMP"
find . -xdev -print0 \
  | cpio --null --create --format=newc \
  | gzip -9 > "$INITRAMFS_TEMP"
gzip -t "$INITRAMFS_TEMP"
mv "$INITRAMFS_TEMP" "$INITRAMFS_OUTPUT"
'''

    print(f"Packing only this directory: {init_root}")
    run(["bash", "-c", command], cwd=init_root, env=env)
    output.chmod(0o644)
    chown_to_invoking_user(output)


def verify_archive(output: Path) -> list[str]:
    command = f'gzip -dc "{output}" | cpio -it 2>/dev/null'
    result = run(["bash", "-c", command], stdout=subprocess.PIPE)
    entries = [line.strip().removeprefix("./") for line in result.stdout.splitlines()]

    required = {"init", "bin/toybox", "bin/sh", "bin/mount", "bin/switch_root"}
    missing = sorted(required.difference(entries))
    if missing:
        fatal("Generated archive is missing: " + ", ".join(missing))

    bad = [
        entry
        for entry in entries
        if entry.endswith((".img", ".iso", ".ext2", ".squashfs"))
        or entry.startswith(("etc/", "usr/lib/systemd", "var/"))
    ]
    if bad:
        fatal(
            "Full-rootfs or image content leaked into the initramfs:\n  "
            + "\n  ".join(bad[:50])
        )

    return entries


def update_loader(loader: Path, kernel_version: str, title: str) -> None:
    loader.parent.mkdir(parents=True, exist_ok=True)

    if loader.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = loader.with_name(loader.name + f".bak-{stamp}")
        shutil.copy2(loader, backup)
        chown_to_invoking_user(backup)
        print(f"Loader backup: {backup}")

    loader.write_text(
        f"title {title}\n"
        f"linux /boot/vmlinuz-{kernel_version}\n"
        f"initrd /boot/initramfs-{kernel_version}.cpio.gz\n"
        "options ro console=tty0 console=ttyS0,115200 loglevel=7\n",
        encoding="utf-8",
        newline="\n",
    )
    loader.chmod(0o644)
    chown_to_invoking_user(loader)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the minimal Toybox initramfs for Hardened Arch."
    )
    parser.add_argument("--kernel-version", default="7.1.2")
    parser.add_argument(
        "--toybox", type=Path, default=Path("/home/corbett/toybox-x86_64")
    )
    parser.add_argument(
        "--iso-root", type=Path, default=Path("/home/corbett/iso-systemd")
    )
    parser.add_argument(
        "--init-root", type=Path, default=Path("/home/corbett/initramfs-root")
    )
    parser.add_argument("--title", default="Hardened Arch V1.10")
    parser.add_argument("--skip-loader-update", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    require_root()

    toybox = args.toybox.resolve()
    iso_root = args.iso_root.resolve()
    init_root = args.init_root.resolve()
    rootfs = iso_root / "rootfs.ext2"
    kernel = iso_root / "boot" / f"vmlinuz-{args.kernel_version}"
    output = iso_root / "boot" / f"initramfs-{args.kernel_version}.cpio.gz"
    loader = iso_root / "loader/entries/hardened.conf"

    print("=== Hardened Arch clean initramfs builder ===")
    print(f"Toybox:       {toybox}")
    print(f"ISO staging:  {iso_root}")
    print(f"ext2 payload: {rootfs}")
    print(f"Kernel:       {kernel}")
    print(f"Work tree:    {init_root}")
    print(f"Output:       {output}")
    print()

    if not rootfs.is_file():
        fatal(f"Missing ext2 root payload: {rootfs}")
    if not kernel.is_file():
        fatal(f"Missing kernel image: {kernel}")

    applets = verify_toybox(toybox)
    create_tree(init_root, toybox, applets)
    verify_tree(init_root)
    pack_archive(init_root, output)
    entries = verify_archive(output)

    if not args.skip_loader_update:
        update_loader(loader, args.kernel_version, args.title)

    print()
    print("=== SUCCESS ===")
    print(f"Created: {output}")
    print(f"Size:    {output.stat().st_size / 1048576:.2f} MiB")
    print(f"Entries: {len(entries)}")
    if not args.skip_loader_update:
        print(f"Updated: {loader}")
    print("Next step: rebuild the ISO from /home/corbett/iso-systemd.")


if __name__ == "__main__":
    main()
