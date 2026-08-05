#!/usr/bin/env python3
"""
Repair the SELinux ABI collision in rootfs.ext2.

The Arch SELinux 3.10 libraries are already installed under /usr/lib, but the
target's old Debian-layout copies under /lib/x86_64-linux-gnu are being selected
first. That causes:

    LIBSELINUX_3.10 not found

This script:
  * backs up conflicting old SELinux/audit library files
  * removes only the conflicting SELinux-stack copies from Debian-style paths
  * makes those SONAMEs resolve to the Arch copies in /usr/lib
  * puts /usr/lib first in the target linker configuration
  * rebuilds ld.so.cache
  * verifies semodule, setfiles, restorecon, getenforce, and pam_selinux.so
  * checks that no key SELinux executable has unresolved libraries

Close QEMU first, then run:
    sudo python3 /home/corbett/fix_selinux_abi_collision.py
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
BACKUP_ROOT = HOME / "rootfs-backups"

# Libraries that belong to the Arch SELinux userspace chain.
SONAMES = (
    "libselinux.so.1",
    "libsepol.so.2",
    "libsemanage.so.2",
    "libaudit.so.1",
    "libcap-ng.so.0",
)

# Debian/Ubuntu-style directories that must not override /usr/lib for these
# particular Arch libraries.
CONFLICT_DIRECTORIES = (
    "lib/x86_64-linux-gnu",
    "usr/lib/x86_64-linux-gnu",
    "lib64",
)

CHECK_EXECUTABLES = (
    "/usr/bin/semodule",
    "/usr/bin/setfiles",
    "/usr/bin/restorecon",
    "/usr/sbin/restorecon",
    "/usr/sbin/getenforce",
    "/usr/bin/getenforce",
    "/usr/sbin/setenforce",
    "/usr/bin/setenforce",
)


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


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
        die(
            f"Command failed ({exc.returncode}): "
            + " ".join(str(item) for item in args)
        )


def require_root() -> None:
    if os.geteuid() != 0:
        die("Run this script with sudo.")


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        die(f"Required host command not found: {name}")


def ensure_qemu_closed() -> None:
    pgrep = shutil.which("pgrep")
    if pgrep is None:
        return

    result = subprocess.run(
        [pgrep, "-f", "qemu-system-x86_64"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        die("QEMU is still running. Shut it down first.")


def associated_loops(image: Path) -> list[str]:
    result = run(
        [
            "losetup",
            "--noheadings",
            "--output",
            "NAME",
            "--associated",
            str(image),
        ],
        check=False,
        capture=True,
    )
    return sorted(
        {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith("/dev/loop")
        }
    )


def clear_stale_loops(image: Path) -> None:
    for loop in associated_loops(image):
        targets = run(
            ["findmnt", "-rn", "-S", loop, "-o", "TARGET"],
            check=False,
            capture=True,
        ).stdout.splitlines()

        for target in sorted(
            {line.strip() for line in targets if line.strip()},
            key=len,
            reverse=True,
        ):
            result = run(["umount", target], check=False, capture=True)
            if result.returncode != 0:
                die(f"Could not unmount stale mount {target}:\n{result.stdout}")

        run(["losetup", "--detach", loop], check=False, capture=True)

    remaining = associated_loops(image)
    if remaining:
        die("rootfs.ext2 is still attached to: " + ", ".join(remaining))


def find_arch_library(root: Path, soname: str) -> Path:
    direct = root / "usr/lib" / soname
    if direct.exists() or direct.is_symlink():
        try:
            resolved = direct.resolve(strict=True)
        except FileNotFoundError:
            die(f"Broken Arch library symlink: /usr/lib/{soname}")
        return resolved

    candidates = sorted(
        path
        for path in (root / "usr/lib").glob(soname + "*")
        if path.is_file() or path.is_symlink()
    )
    for candidate in candidates:
        try:
            return candidate.resolve(strict=True)
        except FileNotFoundError:
            continue

    die(f"Arch library is missing from /usr/lib: {soname}")
    raise AssertionError


def backup_conflict(
    root: Path,
    path: Path,
    backup_dir: Path,
) -> None:
    relative = path.relative_to(root)
    destination = backup_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)

    if path.is_symlink():
        marker = destination.with_suffix(destination.suffix + ".symlink.txt")
        marker.write_text(os.readlink(path), encoding="utf-8")
    elif path.is_file():
        shutil.copy2(path, destination, follow_symlinks=False)


def conflicting_family(directory: Path, soname: str) -> list[Path]:
    # libselinux.so.1 -> capture libselinux.so.1 and libselinux.so.1.*
    return sorted(
        {
            path
            for pattern in (soname, soname + ".*")
            for path in directory.glob(pattern)
            if path.exists() or path.is_symlink()
        },
        key=lambda path: path.name,
    )


def repair_library_family(
    root: Path,
    soname: str,
    backup_dir: Path,
) -> None:
    arch_real = find_arch_library(root, soname)
    arch_soname = root / "usr/lib" / soname

    # Ensure the canonical /usr/lib SONAME exists and resolves to the actual
    # Arch library file.
    if not arch_soname.exists() and not arch_soname.is_symlink():
        arch_soname.symlink_to(arch_real.name)

    print(
        f"{soname}: Arch copy /usr/lib/{soname} -> "
        f"{arch_real.name}"
    )

    for relative_directory in CONFLICT_DIRECTORIES:
        directory = root / relative_directory
        if not directory.is_dir():
            continue

        # Never remove the canonical Arch library itself when /lib or /lib64
        # happens to be a symlink into /usr/lib.
        try:
            if directory.resolve() == (root / "usr/lib").resolve():
                continue
        except FileNotFoundError:
            pass

        family = conflicting_family(directory, soname)

        for path in family:
            try:
                resolved = path.resolve(strict=True)
            except FileNotFoundError:
                resolved = None

            if resolved == arch_real:
                continue

            backup_conflict(root, path, backup_dir)

            if path.is_symlink() or path.is_file():
                path.unlink()
                print(f"Removed conflicting /{path.relative_to(root)}")

        compatibility_link = directory / soname

        if compatibility_link.exists() or compatibility_link.is_symlink():
            try:
                if compatibility_link.resolve(strict=True) == arch_real:
                    continue
            except FileNotFoundError:
                compatibility_link.unlink(missing_ok=True)

        # Absolute target is intentional: it resolves inside the final root.
        compatibility_link.symlink_to(f"/usr/lib/{soname}")
        print(
            f"Created /{compatibility_link.relative_to(root)} "
            f"-> /usr/lib/{soname}"
        )


def configure_linker(root: Path) -> None:
    config = root / "etc/ld.so.conf.d/00-arch-selinux.conf"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "# Keep Arch SELinux libraries ahead of stale multiarch copies.\n"
        "/usr/lib\n",
        encoding="utf-8",
        newline="\n",
    )
    config.chmod(0o644)

    cache = root / "etc/ld.so.cache"
    cache.unlink(missing_ok=True)

    ldconfig = None
    for candidate in (
        "/usr/sbin/ldconfig",
        "/sbin/ldconfig",
        "/usr/bin/ldconfig",
        "/bin/ldconfig",
    ):
        if (root / candidate.lstrip("/")).is_file():
            ldconfig = candidate
            break

    if ldconfig is None:
        die("Target ldconfig is missing.")

    run(["chroot", str(root), ldconfig])

    if not cache.is_file():
        die("ldconfig did not recreate /etc/ld.so.cache.")


def find_ldd(root: Path) -> str:
    for candidate in ("/usr/bin/ldd", "/bin/ldd"):
        if (root / candidate.lstrip("/")).is_file():
            return candidate
    die("Target ldd is missing.")
    raise AssertionError


def verify_symbol_version(root: Path) -> None:
    strings = shutil.which("strings")
    if strings is None:
        die("Host strings command is missing.")

    library = find_arch_library(root, "libselinux.so.1")
    result = run(
        [strings, str(library)],
        capture=True,
    )
    if "LIBSELINUX_3.10" not in result.stdout:
        die(
            f"{library} does not export LIBSELINUX_3.10. "
            "The wrong libselinux package is installed."
        )

    print("Verified LIBSELINUX_3.10 in the Arch library.")


def verify_executable(
    root: Path,
    ldd: str,
    executable: str,
) -> None:
    if not (root / executable.lstrip("/")).is_file():
        return

    result = run(
        ["chroot", str(root), ldd, executable],
        check=False,
        capture=True,
    )
    print(result.stdout.rstrip())

    bad_markers = (
        "not found",
        "version `LIBSELINUX_3.10' not found",
        "version 'LIBSELINUX_3.10' not found",
    )

    if result.returncode != 0 or any(
        marker in result.stdout for marker in bad_markers
    ):
        die(f"{executable} still has a linker or ABI failure.")

    if "libselinux.so.1 => /usr/lib/libselinux.so.1" not in result.stdout:
        # A Debian compatibility symlink may be printed instead, but it must
        # resolve to the Arch file. Check with readlink inside the target.
        print(
            f"NOTE: {executable} displays a compatibility library path; "
            "the resolved library will be checked separately."
        )


def verify_pam_selinux(root: Path, ldd: str) -> None:
    candidates = (
        "/usr/lib/security/pam_selinux.so",
        "/usr/lib64/security/pam_selinux.so",
        "/usr/lib/x86_64-linux-gnu/security/pam_selinux.so",
        "/lib/security/pam_selinux.so",
        "/lib/x86_64-linux-gnu/security/pam_selinux.so",
    )

    found = False
    for candidate in candidates:
        path = root / candidate.lstrip("/")
        if not path.is_file():
            continue

        found = True
        verify_executable(root, ldd, candidate)

    if not found:
        die("pam_selinux.so is missing from the target.")


def verify_resolved_library(root: Path, soname: str) -> None:
    compatibility = root / "lib/x86_64-linux-gnu" / soname
    arch_real = find_arch_library(root, soname)

    if compatibility.exists() or compatibility.is_symlink():
        try:
            resolved = compatibility.resolve(strict=True)
        except FileNotFoundError:
            die(f"Broken compatibility link: /lib/x86_64-linux-gnu/{soname}")

        if resolved != arch_real:
            die(
                f"/lib/x86_64-linux-gnu/{soname} still resolves to "
                f"{resolved}, not {arch_real}"
            )


def main() -> None:
    require_root()
    ensure_qemu_closed()

    for command in (
        "losetup",
        "findmnt",
        "mount",
        "umount",
        "e2fsck",
        "chroot",
        "sync",
        "strings",
    ):
        require_command(command)

    if not ROOTFS.is_file():
        die(f"Root filesystem image not found: {ROOTFS}")

    clear_stale_loops(ROOTFS)
    run(["e2fsck", "-f", "-y", str(ROOTFS)])

    loop = run(
        ["losetup", "--find", "--show", str(ROOTFS)],
        capture=True,
    ).stdout.strip()

    mountpoint = Path(
        tempfile.mkdtemp(prefix="selinux-abi-repair-", dir="/mnt")
    )
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = BACKUP_ROOT / f"selinux-abi-collision-{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    mounted = False

    try:
        run(
            [
                "mount",
                "-t",
                "ext2",
                "-o",
                "rw,acl,user_xattr",
                loop,
                str(mountpoint),
            ]
        )
        mounted = True

        verify_symbol_version(mountpoint)

        for soname in SONAMES:
            repair_library_family(
                mountpoint,
                soname,
                backup_dir,
            )

        configure_linker(mountpoint)

        for soname in SONAMES:
            verify_resolved_library(mountpoint, soname)

        ldd = find_ldd(mountpoint)

        checked = 0
        for executable in CHECK_EXECUTABLES:
            if (mountpoint / executable.lstrip("/")).is_file():
                verify_executable(mountpoint, ldd, executable)
                checked += 1

        if checked == 0:
            die("No SELinux policycoreutils executables were found.")

        verify_pam_selinux(mountpoint, ldd)

        # Execute the two tools that were blocking policy work.
        semodule = "/usr/bin/semodule"
        if not (mountpoint / semodule.lstrip("/")).is_file():
            die("semodule is missing.")

        result = run(
            ["chroot", str(mountpoint), semodule, "--help"],
            check=False,
            capture=True,
        )
        if result.returncode not in (0, 1):
            print(result.stdout, file=sys.stderr)
            die("semodule cannot execute after the ABI repair.")

        setfiles = "/usr/bin/setfiles"
        if not (mountpoint / setfiles.lstrip("/")).is_file():
            die("setfiles is missing.")

        result = run(
            ["chroot", str(mountpoint), setfiles, "-h"],
            check=False,
            capture=True,
        )
        # setfiles commonly returns 1 after printing usage. A loader error is
        # what matters here.
        if (
            "LIBSELINUX_3.10" in result.stdout
            or "error while loading shared libraries" in result.stdout
        ):
            print(result.stdout, file=sys.stderr)
            die("setfiles still has a loader failure.")

        run(["sync"])
        print("SELinux ABI and runtime verification: PASS")

    finally:
        if mounted:
            run(["umount", str(mountpoint)], check=False)
        run(["losetup", "--detach", loop], check=False)

        try:
            mountpoint.rmdir()
        except OSError:
            pass

    run(["e2fsck", "-f", "-y", str(ROOTFS)])
    run(["e2fsck", "-fn", str(ROOTFS)])

    print()
    print("=== SUCCESS ===")
    print("The old Debian-layout SELinux libraries no longer override")
    print("the Arch SELinux 3.10 libraries.")
    print("semodule, setfiles, policycoreutils, and pam_selinux verified.")
    print(f"Backups: {backup_dir}")
    print()
    print("Now rerun:")
    print(
        "sudo python3 "
        "/home/corbett/stage_selinux_userspace_and_policy.py"
    )


if __name__ == "__main__":
    main()
