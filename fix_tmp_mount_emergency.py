#!/usr/bin/env python3
"""
Fix the systemd emergency-mode trigger caused by tmp.mount.

The live root already has a tmpfs-backed OverlayFS upper layer, so /tmp writes
are already volatile. A second tmpfs mount from /etc/fstab is unnecessary and
is currently failing before local-fs.target.

This script:
  * backs up and edits /etc/fstab inside rootfs.ext2
  * removes active /tmp tmpfs entries
  * creates /tmp and /var/tmp with mode 1777
  * adds a tmpfiles rule to preserve those modes
  * patches repair_live_boot_overlay.py so it will not re-add the bad entry
  * checks the ext2 image
  * rebuilds and verifies the outer ISO

Run:
    sudo python3 /home/corbett/fix_tmp_mount_emergency.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


ROOTFS = Path("/home/corbett/iso-systemd/rootfs.ext2")
REPAIR_SCRIPT = Path("/home/corbett/repair_live_boot_overlay.py")
ISO_BUILDER = Path("/home/corbett/build_hardened_iso.py")


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


def clean_fstab(text: str) -> tuple[str, list[str]]:
    kept: list[str] = []
    removed: list[str] = []

    for raw in text.splitlines():
        stripped = raw.strip()

        if not stripped or stripped.startswith("#"):
            kept.append(raw)
            continue

        fields = stripped.split()
        if len(fields) >= 3:
            source, target, fstype = fields[:3]
            if target == "/tmp" and fstype == "tmpfs":
                removed.append(raw)
                continue

        kept.append(raw)

    header = [
        "# Hardened Arch live media",
        "#",
        "# /rootfs.ext2 is the read-only lower root.",
        "# The writable live layer is already tmpfs-backed OverlayFS.",
        "# Do not add a physical-disk root entry or a second /tmp tmpfs mount here.",
        "#",
    ]

    body = [
        line
        for line in kept
        if line.strip()
        and not line.lstrip().startswith("# Hardened Arch live media")
    ]

    result = "\n".join(header + body).rstrip() + "\n"
    return result, removed


def patch_future_rebuilds() -> None:
    if not REPAIR_SCRIPT.is_file():
        print(f"WARNING: repair script not found: {REPAIR_SCRIPT}")
        return

    text = REPAIR_SCRIPT.read_text(encoding="utf-8")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = REPAIR_SCRIPT.with_name(
        REPAIR_SCRIPT.name + f".bak-tmpmount-{timestamp}"
    )
    shutil.copy2(REPAIR_SCRIPT, backup)

    variants = (
        '"tmpfs /tmp tmpfs rw,nosuid,nodev,mode=1777 0 0\\n",',
        '"tmpfs /tmp tmpfs rw,nosuid,nodev,mode=1777 0 0\\n"',
        "'tmpfs /tmp tmpfs rw,nosuid,nodev,mode=1777 0 0\\n',",
        "'tmpfs /tmp tmpfs rw,nosuid,nodev,mode=1777 0 0\\n'",
    )

    changed = False
    for variant in variants:
        if variant in text:
            text = text.replace(variant, '""')
            changed = True

    if changed:
        compile(text, str(REPAIR_SCRIPT), "exec")
        REPAIR_SCRIPT.write_text(text, encoding="utf-8", newline="\n")
        REPAIR_SCRIPT.chmod(0o755)
        print(f"Patched future rebuild script: {REPAIR_SCRIPT}")
        print(f"Backup: {backup}")
    else:
        print(
            "The repair script did not contain the known /tmp fstab line; "
            "it may already be patched."
        )


def main() -> None:
    require_root()

    for command in ("mount", "umount", "findmnt", "e2fsck", "sync"):
        require_command(command)

    if not ROOTFS.is_file():
        die(f"Root filesystem image not found: {ROOTFS}")
    if not ISO_BUILDER.is_file():
        die(f"ISO builder not found: {ISO_BUILDER}")

    mountpoint = Path(tempfile.mkdtemp(prefix="hardened-tmp-fix-", dir="/mnt"))
    mounted = False
    loop_source = ""

    try:
        result = run(
            ["mount", "-o", "loop,rw", str(ROOTFS), str(mountpoint)],
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            die(f"Could not mount rootfs.ext2 read-write:\n{result.stdout}")
        mounted = True

        source = run(
            ["findmnt", "-rn", "-T", str(mountpoint), "-o", "SOURCE"],
            capture=True,
        )
        loop_source = source.stdout.strip()

        fstab = mountpoint / "etc/fstab"
        fstab.parent.mkdir(parents=True, exist_ok=True)

        old_text = (
            fstab.read_text(encoding="utf-8", errors="replace")
            if fstab.is_file()
            else ""
        )

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if fstab.is_file():
            backup = fstab.with_name(f"fstab.bak-tmpmount-{timestamp}")
            shutil.copy2(fstab, backup)
            print(f"Backed up fstab inside rootfs: /etc/{backup.name}")

        new_text, removed = clean_fstab(old_text)
        fstab.write_text(new_text, encoding="utf-8", newline="\n")
        fstab.chmod(0o644)

        if removed:
            print("Removed failing fstab entries:")
            for line in removed:
                print(f"  {line}")
        else:
            print("No active /tmp tmpfs entry remained in fstab.")

        for relative in ("tmp", "var/tmp"):
            path = mountpoint / relative
            path.mkdir(parents=True, exist_ok=True)
            os.chown(path, 0, 0)
            path.chmod(0o1777)
            print(f"Prepared /{relative} with mode 1777")

        tmpfiles = mountpoint / "etc/tmpfiles.d/hardened-live-tmp.conf"
        tmpfiles.parent.mkdir(parents=True, exist_ok=True)
        tmpfiles.write_text(
            "# Hardened Arch live temporary directories\n"
            "d /tmp 1777 root root -\n"
            "d /var/tmp 1777 root root -\n",
            encoding="utf-8",
            newline="\n",
        )
        tmpfiles.chmod(0o644)

        sulogin_paths = (
            mountpoint / "usr/sbin/sulogin",
            mountpoint / "sbin/sulogin",
        )
        if any(path.exists() for path in sulogin_paths):
            print("sulogin is present in the root filesystem.")
        else:
            print(
                "NOTE: sulogin is still absent. It did not cause emergency mode; "
                "tmp.mount did. Do not replace it with an unauthenticated shell "
                "in the hardened image."
            )

        run(["sync"])

    finally:
        if mounted:
            result = run(
                ["umount", str(mountpoint)],
                check=False,
                capture=True,
            )
            if result.returncode != 0:
                die(f"Could not unmount rootfs.ext2:\n{result.stdout}")

        try:
            mountpoint.rmdir()
        except OSError:
            pass

        if loop_source.startswith("/dev/loop"):
            run(["losetup", "-d", loop_source], check=False)

    patch_future_rebuilds()

    print("=== Checking rootfs.ext2 ===")
    run(["e2fsck", "-f", "-y", str(ROOTFS)])
    run(["e2fsck", "-fn", str(ROOTFS)])

    print("=== Rebuilding verified outer ISO ===")
    run([sys.executable, str(ISO_BUILDER)])

    print()
    print("=== SUCCESS ===")
    print("The failing tmp.mount fstab entry is gone.")
    print("/tmp and /var/tmp are supplied by the writable tmpfs OverlayFS")
    print("and are created with mode 1777.")
    print("The final ISO was rebuilt and verified.")
    print()
    print("Boot Hardened Arch Live (Debug) again.")


if __name__ == "__main__":
    main()
