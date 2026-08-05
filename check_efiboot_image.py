#!/usr/bin/env python3
"""
Read-only inspection of the Hardened Arch EFI boot image.

Default paths:
  EFI image:   /home/corbett/iso-systemd/efiboot.img
  ISO staging: /home/corbett/iso-systemd

The script mounts efiboot.img read-only, lists relevant boot files,
prints loader entries, searches for stale initramfs references, and
compares hashes against the files in the ISO staging tree when possible.

Run:
    sudo python3 /home/corbett/check_efiboot_image.py
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


BOOT_NAME_PATTERNS = (
    "initramfs",
    "initrd",
    "vmlinuz",
    "hardened.conf",
    "loader.conf",
    "bootx64.efi",
)


def die(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def require_root() -> None:
    if os.geteuid() != 0:
        die("Run this script with sudo.")


def require_command(name: str) -> str:
    path = shutil.which(name)
    if not path:
        die(f"Required command not found: {name}")
    return path


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(str(x) for x in args))
    try:
        return subprocess.run(
            [str(x) for x in args],
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, file=sys.stderr)
        die(f"Command failed with exit status {exc.returncode}: {' '.join(args)}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{size} B"


def interesting(path: Path) -> bool:
    name = path.name.lower()
    return any(pattern in name for pattern in BOOT_NAME_PATTERNS)


def list_files(mountpoint: Path) -> list[Path]:
    files: list[Path] = []
    for root, _, names in os.walk(mountpoint):
        root_path = Path(root)
        for name in names:
            files.append(root_path / name)
    return sorted(files)


def print_loader_file(path: Path, mountpoint: Path) -> None:
    rel = path.relative_to(mountpoint)
    print()
    print(f"--- {rel} ---")
    try:
        print(path.read_text(encoding="utf-8", errors="replace").rstrip())
    except OSError as exc:
        print(f"[could not read: {exc}]")


def find_matching_source(efi_file: Path, mountpoint: Path, iso_root: Path) -> Path | None:
    rel = efi_file.relative_to(mountpoint)

    candidates = [
        iso_root / rel,
        iso_root / "boot" / efi_file.name,
        iso_root / "loader" / "entries" / efi_file.name,
        iso_root / "loader" / efi_file.name,
        iso_root / "EFI" / "BOOT" / efi_file.name,
    ]

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except OSError:
            pass
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return candidate
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect efiboot.img for stale loader, kernel, or initramfs files."
    )
    parser.add_argument(
        "--efi-image",
        type=Path,
        default=Path("/home/corbett/iso-systemd/efiboot.img"),
    )
    parser.add_argument(
        "--iso-root",
        type=Path,
        default=Path("/home/corbett/iso-systemd"),
    )
    args = parser.parse_args()

    require_root()
    mount_cmd = require_command("mount")
    umount_cmd = require_command("umount")
    file_cmd = require_command("file")

    efi_image = args.efi_image.resolve()
    iso_root = args.iso_root.resolve()

    if not efi_image.is_file():
        die(f"EFI image does not exist: {efi_image}")
    if not iso_root.is_dir():
        die(f"ISO staging directory does not exist: {iso_root}")

    print("=== EFI boot image inspection ===")
    print(f"EFI image:   {efi_image}")
    print(f"Image size:  {human_size(efi_image.stat().st_size)}")
    print(f"ISO staging: {iso_root}")
    print()

    file_result = run([file_cmd, str(efi_image)])
    print(file_result.stdout.rstrip())
    print()

    mountpoint = Path(tempfile.mkdtemp(prefix="efi-check-", dir="/mnt"))
    mounted = False

    stale_references: list[tuple[Path, str]] = []
    mismatches: list[tuple[Path, Path]] = []
    matches: list[tuple[Path, Path]] = []

    try:
        result = run(
            [mount_cmd, "-o", "loop,ro", str(efi_image), str(mountpoint)],
            check=False,
        )
        if result.returncode != 0:
            print(result.stdout, file=sys.stderr)
            die("Could not mount the EFI image read-only.")
        mounted = True

        files = list_files(mountpoint)

        print("=== Files inside efiboot.img ===")
        if not files:
            print("[no files found]")
        else:
            for path in files:
                rel = path.relative_to(mountpoint)
                print(f"{human_size(path.stat().st_size):>11}  {rel}")

        print()
        print("=== Boot-related files ===")
        boot_files = [path for path in files if interesting(path)]
        if not boot_files:
            print("[no obvious boot-related files found]")
        else:
            for path in boot_files:
                print(path.relative_to(mountpoint))

        loader_files = [
            path
            for path in files
            if path.name == "loader.conf"
            or (path.suffix == ".conf" and "loader" in path.parts)
        ]

        print()
        print("=== Loader configuration ===")
        if not loader_files:
            print("[no loader configuration files found]")
        else:
            for path in loader_files:
                print_loader_file(path, mountpoint)

                text = path.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    stripped = line.strip()
                    lowered = stripped.lower()
                    if (
                        "initramfs-" in lowered
                        or lowered.startswith("initrd ")
                        or "rdinit=" in lowered
                    ):
                        stale_references.append((path, stripped))

        print()
        print("=== Hash comparison with ISO staging tree ===")
        comparable = [
            path
            for path in boot_files
            if path.is_file()
            and (
                path.name.startswith("initramfs-")
                or path.name.startswith("vmlinuz-")
                or path.name in {"hardened.conf", "loader.conf", "BOOTX64.EFI"}
            )
        ]

        if not comparable:
            print("[no comparable files found inside efiboot.img]")
        else:
            for efi_file in comparable:
                source = find_matching_source(efi_file, mountpoint, iso_root)
                rel = efi_file.relative_to(mountpoint)

                if source is None:
                    print(f"NO SOURCE MATCH: {rel}")
                    continue

                efi_hash = sha256(efi_file)
                source_hash = sha256(source)

                if efi_hash == source_hash:
                    matches.append((efi_file, source))
                    print(f"MATCH:    {rel}")
                else:
                    mismatches.append((efi_file, source))
                    print(f"MISMATCH: {rel}")
                    print(f"  EFI image: {efi_hash}")
                    print(f"  Staging:   {source_hash}")
                    print(f"  Source:    {source}")

        print()
        print("=== Reference review ===")
        if not stale_references:
            print("[no initrd/rdinit references found in loader configuration]")
        else:
            for path, line in stale_references:
                rel = path.relative_to(mountpoint)
                marker = ""
                lowered = line.lower()
                if ".img" in lowered:
                    marker = "  <-- STALE .img REFERENCE"
                elif "initramfs-7.1.2.cpio.gz" in lowered:
                    marker = "  <-- clean cpio.gz reference"
                elif "rdinit=/init" in lowered:
                    marker = "  <-- explicit rdinit"
                print(f"{rel}: {line}{marker}")

        print()
        print("=== VERDICT ===")

        if mismatches:
            print("FAIL: efiboot.img contains boot files that do not match the ISO staging tree.")
            for efi_file, source in mismatches:
                print(
                    f"  {efi_file.relative_to(mountpoint)} differs from {source}"
                )
        else:
            print("No hash mismatches were found among comparable files.")

        stale_img_lines = [
            (path, line)
            for path, line in stale_references
            if ".img" in line.lower()
        ]
        if stale_img_lines:
            print("FAIL: loader configuration still references an old .img initramfs.")
        else:
            print("No stale .img reference was found in loader configuration.")

        has_clean_initrd = any(
            "initramfs-7.1.2.cpio.gz" in line
            for _, line in stale_references
        )
        if has_clean_initrd:
            print("PASS: loader configuration references initramfs-7.1.2.cpio.gz.")
        else:
            print(
                "WARNING: no loader entry inside efiboot.img references "
                "initramfs-7.1.2.cpio.gz."
            )

        clean_initramfs_inside = any(
            path.name == "initramfs-7.1.2.cpio.gz" for path in files
        )
        if clean_initramfs_inside:
            print("PASS: the clean cpio.gz initramfs is present inside efiboot.img.")
        else:
            print("WARNING: the clean cpio.gz initramfs is not present inside efiboot.img.")

        if mismatches or stale_img_lines:
            raise SystemExit(2)

    finally:
        if mounted:
            cleanup = run([umount_cmd, str(mountpoint)], check=False)
            if cleanup.returncode != 0:
                print(
                    f"WARNING: could not unmount {mountpoint}:\n{cleanup.stdout}",
                    file=sys.stderr,
                )
        try:
            mountpoint.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    main()
