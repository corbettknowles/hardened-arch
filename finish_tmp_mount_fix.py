#!/usr/bin/env python3
"""
Finish the Hardened Arch tmp.mount repair after the earlier helper failed.

This script safely:
  1. Restores repair_live_boot_overlay.py from its newest valid backup if the
     current copy has a SyntaxError.
  2. Patches the repair helper without breaking its Python syntax.
  3. Removes every active /tmp entry from rootfs.ext2 /etc/fstab.
  4. Masks tmp.mount because /tmp already lives on the writable OverlayFS.
  5. Creates /tmp and /var/tmp with mode 1777 and a tmpfiles rule.
  6. Runs e2fsck and rebuilds the verified outer ISO.

Run:
    sudo python3 /home/corbett/finish_tmp_mount_fix.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


HOME = Path("/home/corbett")
ROOTFS = HOME / "iso-systemd/rootfs.ext2"
REPAIR_SCRIPT = HOME / "repair_live_boot_overlay.py"
ISO_BUILDER = HOME / "build_hardened_iso.py"


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


def valid_python(path: Path) -> tuple[bool, str]:
    try:
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec")
        return True, ""
    except (OSError, SyntaxError) as exc:
        return False, str(exc)


def restore_repair_script_if_needed() -> None:
    if not REPAIR_SCRIPT.is_file():
        die(f"Repair script not found: {REPAIR_SCRIPT}")

    valid, error = valid_python(REPAIR_SCRIPT)
    if valid:
        print(f"Repair helper syntax is already valid: {REPAIR_SCRIPT}")
        return

    print(f"Current repair helper is invalid: {error}")
    patterns = (
        "repair_live_boot_overlay.py.bak-tmpmount-*",
        "repair_live_boot_overlay.py.bak-*",
    )

    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(HOME.glob(pattern))

    candidates = sorted(
        set(candidates),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    for candidate in candidates:
        candidate_valid, candidate_error = valid_python(candidate)
        if not candidate_valid:
            print(f"Skipping invalid backup: {candidate} ({candidate_error})")
            continue

        broken_copy = REPAIR_SCRIPT.with_name(
            REPAIR_SCRIPT.name
            + ".broken-"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        shutil.copy2(REPAIR_SCRIPT, broken_copy)
        shutil.copy2(candidate, REPAIR_SCRIPT)
        REPAIR_SCRIPT.chmod(0o755)

        print(f"Saved broken helper: {broken_copy}")
        print(f"Restored valid helper from: {candidate}")
        return

    die("No valid repair_live_boot_overlay.py backup could be found.")


def patch_repair_script_safely() -> None:
    text = REPAIR_SCRIPT.read_text(encoding="utf-8")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = REPAIR_SCRIPT.with_name(
        REPAIR_SCRIPT.name + f".bak-safe-tmp-patch-{timestamp}"
    )
    shutil.copy2(REPAIR_SCRIPT, backup)

    lines = text.splitlines(keepends=True)
    changed = False
    patched: list[str] = []

    for line in lines:
        if "tmpfs /tmp tmpfs rw,nosuid,nodev,mode=1777 0 0" in line:
            indent = line[: len(line) - len(line.lstrip())]
            newline = "\n" if line.endswith("\n") else ""
            patched.append(
                indent
                + '"# /tmp is supplied by the writable live OverlayFS; '
                + 'do not generate tmp.mount.\\n",'
                + newline
            )
            changed = True
        else:
            patched.append(line)

    patched_text = "".join(patched)

    # Validate in memory before touching the working file.
    compile(patched_text, str(REPAIR_SCRIPT), "exec")

    if changed:
        REPAIR_SCRIPT.write_text(
            patched_text,
            encoding="utf-8",
            newline="\n",
        )
        REPAIR_SCRIPT.chmod(0o755)
        print(f"Safely patched future rebuild helper: {REPAIR_SCRIPT}")
        print(f"Backup: {backup}")
    else:
        print("Future rebuild helper no longer contains the bad /tmp fstab line.")

    final_valid, final_error = valid_python(REPAIR_SCRIPT)
    if not final_valid:
        die(f"Repair helper failed final syntax verification: {final_error}")


def remove_tmp_entries(text: str) -> tuple[str, list[str]]:
    output: list[str] = []
    removed: list[str] = []

    for raw in text.splitlines():
        stripped = raw.strip()

        if not stripped or stripped.startswith("#"):
            output.append(raw)
            continue

        fields = stripped.split()
        if len(fields) >= 2 and fields[1] == "/tmp":
            removed.append(raw)
            continue

        output.append(raw)

    if not any("Hardened Arch live media" in line for line in output):
        output[0:0] = [
            "# Hardened Arch live media",
            "# /tmp is part of the writable tmpfs OverlayFS upper layer.",
            "# A separate tmp.mount is intentionally disabled.",
            "#",
        ]

    return "\n".join(output).rstrip() + "\n", removed


def repair_rootfs() -> None:
    mountpoint = Path(
        tempfile.mkdtemp(prefix="hardened-finish-tmp-fix-", dir="/mnt")
    )
    mounted = False

    try:
        result = run(
            ["mount", "-o", "loop,rw", str(ROOTFS), str(mountpoint)],
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            die(f"Could not mount rootfs.ext2 read-write:\n{result.stdout}")
        mounted = True

        fstab = mountpoint / "etc/fstab"
        fstab.parent.mkdir(parents=True, exist_ok=True)

        old_text = (
            fstab.read_text(encoding="utf-8", errors="replace")
            if fstab.is_file()
            else ""
        )

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if fstab.is_file():
            backup = fstab.with_name(f"fstab.bak-final-tmpfix-{timestamp}")
            shutil.copy2(fstab, backup)
            print(f"Backed up rootfs fstab: /etc/{backup.name}")

        new_text, removed = remove_tmp_entries(old_text)
        fstab.write_text(new_text, encoding="utf-8", newline="\n")
        fstab.chmod(0o644)

        if removed:
            print("Removed active /tmp mount entries:")
            for item in removed:
                print(f"  {item}")
        else:
            print("No active /tmp fstab entry remains.")

        for relative in ("tmp", "var/tmp"):
            path = mountpoint / relative
            if path.is_symlink():
                die(f"/{relative} is unexpectedly a symbolic link: {os.readlink(path)}")
            path.mkdir(parents=True, exist_ok=True)
            os.chown(path, 0, 0)
            path.chmod(0o1777)
            print(f"Prepared /{relative}: root:root mode 1777")

        tmpfiles = mountpoint / "etc/tmpfiles.d/hardened-live-tmp.conf"
        tmpfiles.parent.mkdir(parents=True, exist_ok=True)
        tmpfiles.write_text(
            "# Temporary directories already reside on the volatile live overlay\n"
            "d /tmp 1777 root root -\n"
            "d /var/tmp 1777 root root -\n",
            encoding="utf-8",
            newline="\n",
        )
        tmpfiles.chmod(0o644)

        # Ensure neither a packaged tmp.mount nor an accidental generator input
        # can perform a second mount over the live OverlayFS /tmp directory.
        mask = mountpoint / "etc/systemd/system/tmp.mount"
        mask.parent.mkdir(parents=True, exist_ok=True)

        if mask.exists() or mask.is_symlink():
            if mask.is_dir() and not mask.is_symlink():
                die(f"Unexpected directory at {mask}")
            mask.unlink()

        mask.symlink_to("/dev/null")
        print("Masked tmp.mount -> /dev/null")

        sulogin = (
            mountpoint / "usr/sbin/sulogin",
            mountpoint / "sbin/sulogin",
        )
        if any(path.exists() for path in sulogin):
            print("Verified: sulogin is present.")
        else:
            print(
                "WARNING: sulogin is absent. It did not trigger tmp.mount failure, "
                "but emergency login would remain unavailable."
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


def main() -> None:
    require_root()

    for command in ("mount", "umount", "e2fsck", "sync"):
        require_command(command)

    if not ROOTFS.is_file():
        die(f"Root filesystem image not found: {ROOTFS}")
    if not ISO_BUILDER.is_file():
        die(f"ISO builder not found: {ISO_BUILDER}")

    print("=== Repairing the future rebuild helper ===")
    restore_repair_script_if_needed()
    patch_repair_script_safely()

    print("=== Finalizing rootfs.ext2 tmp.mount repair ===")
    repair_rootfs()

    print("=== Checking rootfs.ext2 ===")
    run(["e2fsck", "-f", "-y", str(ROOTFS)])
    run(["e2fsck", "-fn", str(ROOTFS)])

    print("=== Rebuilding the verified outer ISO ===")
    run([sys.executable, str(ISO_BUILDER)])

    print()
    print("=== SUCCESS ===")
    print("The broken repair helper was restored and safely patched.")
    print("Every active /tmp fstab entry is gone.")
    print("tmp.mount is masked because /tmp already resides on the live OverlayFS.")
    print("/tmp and /var/tmp are mode 1777, and sulogin was checked.")
    print("The ISO was rebuilt and verified.")
    print()
    print("Boot Hardened Arch Live (Debug) again.")


if __name__ == "__main__":
    main()
