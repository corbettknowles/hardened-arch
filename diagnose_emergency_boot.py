#!/usr/bin/env python3
"""
Diagnose why the Hardened Arch ext2 root enters systemd emergency mode.

Read-only: this script does not modify rootfs.ext2.

Run:
    sudo python3 /home/corbett/diagnose_emergency_boot.py
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def die(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
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
        die(f"Command failed ({exc.returncode}): {' '.join(map(str, args))}")


def require_root() -> None:
    if os.geteuid() != 0:
        die("Run this script with sudo.")


def require_command(name: str) -> str:
    path = shutil.which(name)
    if not path:
        die(f"Required command not found: {name}")
    return path


def describe_path(root: Path, relative: str) -> None:
    path = root / relative.lstrip("/")
    print(f"{relative}:")
    if not path.exists() and not path.is_symlink():
        print("  MISSING")
        return

    if path.is_symlink():
        target = os.readlink(path)
        try:
            resolved = path.resolve(strict=False)
            resolved_text = "/" + str(resolved.relative_to(root))
        except ValueError:
            resolved_text = str(resolved)
        print(f"  symlink -> {target}")
        print(f"  resolves -> {resolved_text}")
    else:
        st = path.stat()
        kind = "directory" if path.is_dir() else "regular file"
        print(f"  {kind}, mode {oct(st.st_mode & 0o7777)}, size {st.st_size}")


def parse_fstab(text: str) -> list[list[str]]:
    entries: list[list[str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) >= 4:
            entries.append(fields)
    return entries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rootfs",
        type=Path,
        default=Path("/home/corbett/iso-systemd/rootfs.ext2"),
    )
    parser.add_argument(
        "--loader-entry",
        type=Path,
        default=Path("/home/corbett/iso-systemd/loader/entries/hardened.conf"),
    )
    args = parser.parse_args()

    require_root()
    mount_cmd = require_command("mount")
    umount_cmd = require_command("umount")
    blkid_cmd = require_command("blkid")

    rootfs = args.rootfs.resolve()
    loader_entry = args.loader_entry.resolve()

    if not rootfs.is_file():
        die(f"Root filesystem image not found: {rootfs}")

    print("=== Hardened Arch emergency-mode diagnosis ===")
    print(f"Root image: {rootfs}")
    print()

    blkid = run([blkid_cmd, str(rootfs)], check=False)
    print("=== Filesystem identity ===")
    print(blkid.stdout.strip() or "[blkid returned no information]")
    print()

    if loader_entry.is_file():
        print("=== systemd-boot loader entry ===")
        print(loader_entry.read_text(encoding="utf-8", errors="replace").rstrip())
        print()
    else:
        print(f"WARNING: loader entry not found: {loader_entry}")
        print()

    mountpoint = Path(tempfile.mkdtemp(prefix="hardened-root-diag-", dir="/mnt"))
    mounted = False

    findings: list[str] = []

    try:
        result = run(
            [mount_cmd, "-o", "loop,ro", str(rootfs), str(mountpoint)],
            check=False,
        )
        if result.returncode != 0:
            print(result.stdout, file=sys.stderr)
            die("Could not mount rootfs.ext2 read-only.")
        mounted = True

        print("=== Critical init paths ===")
        for relative in (
            "/usr/lib/systemd/systemd",
            "/sbin/init",
            "/bin/sh",
            "/usr/bin/bash",
            "/usr/sbin/sulogin",
            "/sbin/sulogin",
        ):
            describe_path(mountpoint, relative)
        print()

        print("=== Default target ===")
        default_candidates = (
            "/etc/systemd/system/default.target",
            "/usr/lib/systemd/system/default.target",
            "/lib/systemd/system/default.target",
        )
        found_default = False
        for relative in default_candidates:
            path = mountpoint / relative.lstrip("/")
            if path.exists() or path.is_symlink():
                found_default = True
                describe_path(mountpoint, relative)
                if path.is_symlink():
                    target = os.readlink(path).lower()
                    if "emergency.target" in target or "rescue.target" in target:
                        findings.append(
                            f"PRIMARY SUSPECT: {relative} points to {target}."
                        )
        if not found_default:
            findings.append("No default.target was found.")
        print()

        fstab_path = mountpoint / "etc/fstab"
        print("=== /etc/fstab ===")
        if not fstab_path.is_file():
            print("[missing]")
            findings.append("/etc/fstab is missing (usually not fatal by itself).")
            fstab_entries: list[list[str]] = []
        else:
            fstab_text = fstab_path.read_text(encoding="utf-8", errors="replace")
            print(fstab_text.rstrip() or "[empty]")
            fstab_entries = parse_fstab(fstab_text)

        risky_entries: list[str] = []
        for fields in fstab_entries:
            source, target, fstype, options = fields[:4]
            if fstype in {"proc", "sysfs", "devtmpfs", "tmpfs", "devpts"}:
                continue
            if target == "/" or source.startswith(("UUID=", "PARTUUID=", "/dev/")):
                risky_entries.append(" ".join(fields))
            elif target == "none" and fstype == "swap":
                risky_entries.append(" ".join(fields))

        if risky_entries:
            findings.append(
                "PRIMARY SUSPECT: fstab contains device/root/swap entries that can "
                "fail in the loop-backed live ISO:\n    "
                + "\n    ".join(risky_entries)
            )
        print()

        print("=== Enabled mount/swap units ===")
        unit_links: list[Path] = []
        etc_systemd = mountpoint / "etc/systemd/system"
        if etc_systemd.is_dir():
            for pattern in ("*.mount", "*.swap"):
                unit_links.extend(etc_systemd.rglob(pattern))

        if not unit_links:
            print("[none found under /etc/systemd/system]")
        else:
            for path in sorted(unit_links):
                rel = "/" + str(path.relative_to(mountpoint))
                if path.is_symlink():
                    print(f"{rel} -> {os.readlink(path)}")
                else:
                    print(rel)
            findings.append(
                "Mount or swap units are enabled under /etc/systemd/system; "
                "a failed one can trigger emergency mode."
            )
        print()

        print("=== Writable-path readiness ===")
        for relative in ("/run", "/tmp", "/var", "/var/log", "/home"):
            path = mountpoint / relative.lstrip("/")
            if path.exists():
                mode = oct(path.stat().st_mode & 0o7777)
                print(f"{relative}: present, mode {mode}")
            else:
                print(f"{relative}: MISSING")
        print()

        if not (mountpoint / "usr/sbin/sulogin").exists() and not (
            mountpoint / "sbin/sulogin"
        ).exists():
            findings.append(
                "SECONDARY ISSUE: sulogin is missing, so emergency mode cannot "
                "open its normal maintenance login."
            )

        if loader_entry.is_file():
            loader_text = loader_entry.read_text(
                encoding="utf-8", errors="replace"
            )
            options_lines = [
                line.strip()
                for line in loader_text.splitlines()
                if line.strip().startswith("options ")
            ]
            if any(" ro " in f" {line} " for line in options_lines):
                findings.append(
                    "DESIGN NOTE: the live ext2 root is mounted read-only. "
                    "A writable overlay or tmpfs-backed writable paths will be "
                    "needed for a normal desktop boot."
                )

        print("=== VERDICT ===")
        if findings:
            for index, finding in enumerate(findings, 1):
                print(f"{index}. {finding}")
        else:
            print(
                "No obvious offline cause was found. Capture the first failed "
                "systemd unit from the serial console."
            )

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
