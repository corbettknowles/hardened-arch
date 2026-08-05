#!/usr/bin/env python3
"""
Add a temporary SELinux-disabled diagnostic boot entry without changing the
normal Hardened Arch boot entries, synchronize efiboot.img, and rebuild ISO.

Run:
    sudo python3 /home/corbett/add_selinux_off_test_entry.py

This is for diagnosis only. The normal and release entries remain unchanged.
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


HOME = Path("/home/corbett")
ISO_ROOT = HOME / "iso-systemd"
EFI_IMAGE = ISO_ROOT / "efiboot.img"
ISO_BUILDER = HOME / "build_hardened_iso.py"
KERNEL_VERSION = "7.1.2"

SOURCE_ENTRY_CANDIDATES = (
    ISO_ROOT / "loader/entries/hardened-debug.conf",
    ISO_ROOT / "loader/entries/hardened.conf",
)

NEW_ENTRY_NAME = "hardened-selinux-off.conf"
NEW_ENTRY_TITLE = "Hardened Arch Live (Debug, SELinux temporarily disabled)"


def die(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def run(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    print("+", " ".join(str(item) for item in args))
    try:
        return subprocess.run(
            [str(item) for item in args],
            check=check,
            text=True,
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


def build_entry(source: Path, destination: Path) -> None:
    text = source.read_text(encoding="utf-8", errors="strict")
    output: list[str] = []
    saw_title = False
    saw_options = False

    for raw in text.splitlines():
        stripped = raw.strip()

        if stripped.startswith("title "):
            output.append(f"title {NEW_ENTRY_TITLE}")
            saw_title = True
            continue

        if stripped.startswith("options "):
            options = stripped[len("options "):].split()
            options = [
                token
                for token in options
                if token not in {"selinux=0", "enforcing=0", "enforcing=1"}
            ]
            options.extend(
                [
                    "selinux=0",
                    "hardened.mode=debug",
                    "loglevel=7",
                    "systemd.show_status=yes",
                ]
            )

            # Deduplicate while keeping the final desired values.
            deduplicated: list[str] = []
            for token in options:
                if token not in deduplicated:
                    deduplicated.append(token)

            output.append("options " + " ".join(deduplicated))
            saw_options = True
            continue

        output.append(raw)

    if not saw_title:
        output.insert(0, f"title {NEW_ENTRY_TITLE}")

    if not saw_options:
        output.append(
            "options rw console=tty0 console=ttyS0,115200 "
            "hardened.rootfs=/rootfs.ext2 hardened.overlay=tmpfs "
            "hardened.mode=debug selinux=0 loglevel=7 "
            "systemd.show_status=yes"
        )

    rendered = "\n".join(output).rstrip() + "\n"

    if f"linux /boot/vmlinuz-{KERNEL_VERSION}" not in rendered:
        die("The source loader entry does not reference the expected kernel.")

    if f"initrd /boot/initramfs-{KERNEL_VERSION}.cpio.gz" not in rendered:
        die("The source loader entry does not reference the expected initramfs.")

    if "selinux=0" not in rendered:
        die("The diagnostic entry was not rendered with selinux=0.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8", newline="\n")
    destination.chmod(0o644)

    print(f"Created test-only loader entry: {destination}")
    print(rendered.rstrip())


def sync_efi_entry(source_entry: Path) -> None:
    if not EFI_IMAGE.is_file():
        die(f"EFI image not found: {EFI_IMAGE}")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = EFI_IMAGE.with_name(EFI_IMAGE.name + f".bak-selinux-test-{timestamp}")
    shutil.copy2(EFI_IMAGE, backup)
    print(f"Backed up EFI image: {backup}")

    mountpoint = Path(
        tempfile.mkdtemp(prefix="efi-selinux-test-", dir="/mnt")
    )
    mounted = False

    try:
        result = run(
            ["mount", "-o", "loop,rw,sync", str(EFI_IMAGE), str(mountpoint)],
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            die(f"Could not mount efiboot.img read-write:\n{result.stdout}")
        mounted = True

        destination = mountpoint / "loader/entries" / NEW_ENTRY_NAME
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_entry, destination)
        os.sync()

        if sha256(source_entry) != sha256(destination):
            die("EFI loader-entry hash verification failed.")

        print(f"Verified EFI entry: /loader/entries/{NEW_ENTRY_NAME}")

    finally:
        if mounted:
            result = run(
                ["umount", str(mountpoint)],
                check=False,
                capture=True,
            )
            if result.returncode != 0:
                die(f"Could not unmount efiboot.img:\n{result.stdout}")

        try:
            mountpoint.rmdir()
        except OSError:
            pass


def main() -> None:
    require_root()

    for command in ("mount", "umount", "sync"):
        require_command(command)

    if not ISO_ROOT.is_dir():
        die(f"ISO staging tree not found: {ISO_ROOT}")

    if not ISO_BUILDER.is_file():
        die(f"ISO builder not found: {ISO_BUILDER}")

    source = next(
        (path for path in SOURCE_ENTRY_CANDIDATES if path.is_file()),
        None,
    )
    if source is None:
        die("No normal or debug loader entry was found.")

    destination = ISO_ROOT / "loader/entries" / NEW_ENTRY_NAME
    build_entry(source, destination)
    sync_efi_entry(destination)

    print("=== Rebuilding verified outer ISO ===")
    run([sys.executable, str(ISO_BUILDER)])

    print()
    print("=== SUCCESS ===")
    print("Added a test-only boot entry with SELinux disabled.")
    print("The normal Hardened Arch entries were not modified.")
    print()
    print(f"Select: {NEW_ENTRY_TITLE}")


if __name__ == "__main__":
    main()
