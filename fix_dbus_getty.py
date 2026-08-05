#!/usr/bin/env python3
"""
Repair D-Bus and serial-getty in the Hardened Arch ext2 live root.

The script:
  * creates the messagebus user/group required by dbus.service
  * repairs /usr/sbin/agetty using an existing target/staging binary
  * finds a libdbus-1.so.3 that exports LIBDBUS_PRIVATE_1.16.2
  * tests dbus-daemon and agetty inside the target with chroot
  * prepares transient machine-id handling for live media
  * rebuilds the linker cache, checks rootfs.ext2, and rebuilds the ISO

Run:
    sudo python3 /home/corbett/fix_dbus_getty.py
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
STAGING_ROOT = HOME / "linux-7.1.2/rootfs-stage"
ISO_BUILDER = HOME / "build_hardened_iso.py"

REQUIRED_DBUS_SYMBOL = "LIBDBUS_PRIVATE_1.16.2"


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


def read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines()


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(lines).rstrip() + "\n",
        encoding="utf-8",
        newline="\n",
    )
    path.chmod(0o644)


def named_entry_exists(path: Path, name: str) -> bool:
    prefix = name + ":"
    return any(line.startswith(prefix) for line in read_lines(path))


def used_numeric_field(path: Path, field_index: int) -> set[int]:
    values: set[int] = set()

    for line in read_lines(path):
        if not line or line.startswith("#"):
            continue

        fields = line.split(":")
        if len(fields) <= field_index:
            continue

        try:
            values.add(int(fields[field_index]))
        except ValueError:
            pass

    return values


def choose_system_id(passwd: Path, group: Path) -> int:
    used = used_numeric_field(passwd, 2) | used_numeric_field(group, 2)

    for value in range(100, 1000):
        if value not in used:
            return value

    die("No unused system UID/GID was available between 100 and 999.")
    raise AssertionError


def ensure_messagebus_account(root: Path) -> None:
    passwd = root / "etc/passwd"
    group = root / "etc/group"
    shadow = root / "etc/shadow"
    gshadow = root / "etc/gshadow"

    passwd_lines = read_lines(passwd)
    group_lines = read_lines(group)
    shadow_lines = read_lines(shadow)
    gshadow_lines = read_lines(gshadow)

    has_user = named_entry_exists(passwd, "messagebus")
    has_group = named_entry_exists(group, "messagebus")

    if has_user and has_group:
        print("messagebus user and group already exist.")
        return

    identity = choose_system_id(passwd, group)

    shells = (
        "/usr/sbin/nologin",
        "/sbin/nologin",
        "/bin/false",
    )
    shell = next(
        (
            candidate
            for candidate in shells
            if (root / candidate.lstrip("/")).exists()
        ),
        "/bin/false",
    )

    if not has_group:
        group_lines.append(f"messagebus:x:{identity}:")
        if gshadow.is_file() and not named_entry_exists(gshadow, "messagebus"):
            gshadow_lines.append("messagebus:!::")
        print(f"Created messagebus group with GID {identity}.")

    if not has_user:
        passwd_lines.append(
            f"messagebus:x:{identity}:{identity}:"
            f"D-Bus Message Bus:/nonexistent:{shell}"
        )
        if shadow.is_file() and not named_entry_exists(shadow, "messagebus"):
            shadow_lines.append("messagebus:!*:19700:0:99999:7:::")
        print(f"Created messagebus user with UID {identity}.")

    write_lines(passwd, passwd_lines)
    write_lines(group, group_lines)

    if shadow.is_file():
        write_lines(shadow, shadow_lines)
        shadow.chmod(0o640)

    if gshadow.is_file():
        write_lines(gshadow, gshadow_lines)
        gshadow.chmod(0o640)


def chroot_probe(
    root: Path,
    executable: str,
    arguments: list[str],
) -> tuple[int, str]:
    result = run(
        ["chroot", str(root), executable, *arguments],
        check=False,
        capture=True,
    )
    return result.returncode, result.stdout.strip()


def relative_symlink_target(link: Path, target: Path) -> str:
    return os.path.relpath(target, start=link.parent)


def find_agetty_source(root: Path) -> tuple[Path | None, str | None]:
    in_root_candidates = (
        "/usr/bin/agetty",
        "/sbin/agetty",
        "/bin/agetty",
    )

    for absolute in in_root_candidates:
        candidate = root / absolute.lstrip("/")
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate, absolute

    staging_candidates = (
        "/usr/sbin/agetty",
        "/usr/bin/agetty",
        "/sbin/agetty",
        "/bin/agetty",
    )

    if STAGING_ROOT.is_dir():
        for absolute in staging_candidates:
            candidate = STAGING_ROOT / absolute.lstrip("/")
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate, absolute

    return None, None


def ensure_agetty(root: Path) -> None:
    expected = root / "usr/sbin/agetty"

    if expected.is_file() and os.access(expected, os.X_OK):
        code, output = chroot_probe(root, "/usr/sbin/agetty", ["--version"])
        if code == 0:
            print(f"agetty already works: {output.splitlines()[0] if output else 'OK'}")
            return

    source, source_absolute = find_agetty_source(root)
    if source is None or source_absolute is None:
        die(
            "No usable agetty binary was found in the target or staging root. "
            "Install/stage util-linux before rebuilding the ISO."
        )

    expected.parent.mkdir(parents=True, exist_ok=True)

    if source.is_relative_to(root):
        if expected.exists() or expected.is_symlink():
            expected.unlink()

        target = root / source_absolute.lstrip("/")
        expected.symlink_to(relative_symlink_target(expected, target))
        print(f"Created /usr/sbin/agetty -> {source_absolute}")
    else:
        shutil.copy2(source, expected)
        expected.chmod(0o755)
        print(f"Copied staging agetty from {source}")

    code, output = chroot_probe(root, "/usr/sbin/agetty", ["--version"])
    if code != 0:
        die(
            "agetty still cannot run in the target root:\n"
            + (output or "[no output]")
        )

    print(f"agetty probe passed: {output.splitlines()[0] if output else 'OK'}")


def strings_contains(path: Path, token: str) -> bool:
    result = run(
        ["strings", "-a", str(path)],
        check=False,
        capture=True,
    )
    return result.returncode == 0 and token in result.stdout


def find_matching_libdbus(root: Path) -> list[Path]:
    search_roots = [
        STAGING_ROOT,
        HOME / "kde-build",
        HOME,
        Path("/usr/local"),
        Path("/opt"),
    ]

    seen: set[Path] = set()
    candidates: list[Path] = []

    for search_root in search_roots:
        if not search_root.exists():
            continue

        result = run(
            [
                "find",
                str(search_root),
                "-xdev",
                "-type",
                "f",
                "-name",
                "libdbus-1.so.3*",
                "-size",
                "-32M",
                "-print",
            ],
            check=False,
            capture=True,
        )

        for raw in result.stdout.splitlines():
            candidate = Path(raw)

            try:
                resolved = candidate.resolve()
            except OSError:
                continue

            if resolved in seen or not resolved.is_file():
                continue

            seen.add(resolved)

            # Skip the currently active target library. It already failed.
            try:
                if resolved.is_relative_to(root):
                    active = root / "lib/x86_64-linux-gnu/libdbus-1.so.3"
                    if active.exists() and resolved == active.resolve():
                        continue
            except (OSError, ValueError):
                pass

            if strings_contains(resolved, REQUIRED_DBUS_SYMBOL):
                candidates.append(resolved)

    candidates.sort(
        key=lambda path: (
            0 if STAGING_ROOT in path.parents else 1,
            -path.stat().st_mtime,
        )
    )
    return candidates


def save_link_state(path: Path) -> tuple[str, str | Path | None]:
    if path.is_symlink():
        return "symlink", os.readlink(path)

    if path.is_file():
        backup = path.with_name(
            path.name
            + ".pre-dbus-fix-"
            + datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        shutil.copy2(path, backup)
        return "file", backup

    return "missing", None


def restore_link_state(
    path: Path,
    state: tuple[str, str | Path | None],
) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()

    kind, value = state

    if kind == "symlink":
        assert isinstance(value, str)
        path.symlink_to(value)
    elif kind == "file":
        assert isinstance(value, Path)
        shutil.copy2(value, path)


def ensure_matching_dbus_library(root: Path) -> None:
    daemon = root / "usr/bin/dbus-daemon"
    if not daemon.is_file():
        die("The target is missing /usr/bin/dbus-daemon.")

    code, output = chroot_probe(root, "/usr/bin/dbus-daemon", ["--version"])
    if code == 0:
        print(f"dbus-daemon already runs: {output.splitlines()[0] if output else 'OK'}")
        return

    print("Current dbus-daemon probe failed:")
    print(output or "[no output]")

    if REQUIRED_DBUS_SYMBOL not in output:
        die(
            "dbus-daemon failed for a reason other than the known "
            f"{REQUIRED_DBUS_SYMBOL} mismatch."
        )

    candidates = find_matching_libdbus(root)
    if not candidates:
        die(
            "No libdbus-1.so.3 exporting "
            f"{REQUIRED_DBUS_SYMBOL} was found under the staging/build trees. "
            "Rebuild or install the matching D-Bus 1.16.2 library."
        )

    library_dir = root / "lib/x86_64-linux-gnu"
    library_dir.mkdir(parents=True, exist_ok=True)
    active_link = library_dir / "libdbus-1.so.3"
    original_state = save_link_state(active_link)

    for index, candidate in enumerate(candidates, 1):
        destination = library_dir / (
            f"libdbus-1.so.3.hardened-1.16.2-{index}"
        )

        print(f"Testing matching library candidate: {candidate}")

        if destination.exists():
            destination.unlink()

        shutil.copy2(candidate, destination)
        destination.chmod(0o644)

        if active_link.exists() or active_link.is_symlink():
            active_link.unlink()

        active_link.symlink_to(destination.name)

        code, output = chroot_probe(
            root,
            "/usr/bin/dbus-daemon",
            ["--version"],
        )

        if code == 0:
            print(
                "dbus-daemon probe passed with "
                f"{candidate}: {output.splitlines()[0] if output else 'OK'}"
            )
            return

        print("Candidate failed:")
        print(output or "[no output]")

        active_link.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        restore_link_state(active_link, original_state)

    die(
        "Matching-symbol libdbus candidates were found, but none allowed "
        "dbus-daemon to execute successfully."
    )


def prepare_machine_id(root: Path) -> None:
    machine_id = root / "etc/machine-id"
    machine_id.parent.mkdir(parents=True, exist_ok=True)
    machine_id.write_text("", encoding="ascii")
    machine_id.chmod(0o644)

    dbus_dir = root / "var/lib/dbus"
    dbus_dir.mkdir(parents=True, exist_ok=True)

    dbus_machine_id = dbus_dir / "machine-id"
    if dbus_machine_id.exists() or dbus_machine_id.is_symlink():
        if dbus_machine_id.is_dir() and not dbus_machine_id.is_symlink():
            die(f"Unexpected directory at {dbus_machine_id}")
        dbus_machine_id.unlink()

    dbus_machine_id.symlink_to("/etc/machine-id")

    print("Prepared empty writable /etc/machine-id for per-boot generation.")
    print("Linked /var/lib/dbus/machine-id -> /etc/machine-id.")


def verify_unit_account(root: Path) -> None:
    unit_candidates = (
        root / "etc/systemd/system/dbus.service",
        root / "usr/lib/systemd/system/dbus.service",
        root / "lib/systemd/system/dbus.service",
    )

    unit = next(
        (
            path
            for path in unit_candidates
            if path.exists() or path.is_symlink()
        ),
        None,
    )

    if unit is None:
        die("dbus.service was not found after repair.")

    try:
        resolved = unit.resolve(strict=True)
    except OSError as exc:
        die(f"dbus.service does not resolve correctly: {exc}")

    text = resolved.read_text(encoding="utf-8", errors="replace")

    if "User=messagebus" in text:
        print("Verified dbus.service User=messagebus.")
    if "Group=messagebus" in text:
        print("Verified dbus.service Group=messagebus.")


def main() -> None:
    require_root()

    for command in (
        "mount",
        "umount",
        "chroot",
        "find",
        "strings",
        "e2fsck",
        "ldconfig",
        "sync",
    ):
        require_command(command)

    if not ROOTFS.is_file():
        die(f"Root filesystem image not found: {ROOTFS}")
    if not ISO_BUILDER.is_file():
        die(f"ISO builder not found: {ISO_BUILDER}")

    mountpoint = Path(
        tempfile.mkdtemp(prefix="hardened-dbus-getty-fix-", dir="/mnt")
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

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        for relative in (
            "etc/passwd",
            "etc/group",
            "etc/shadow",
            "etc/gshadow",
            "etc/machine-id",
        ):
            source = mountpoint / relative
            if source.exists() or source.is_symlink():
                backup = source.with_name(
                    source.name + f".bak-dbus-getty-{timestamp}"
                )
                if source.is_symlink():
                    backup.symlink_to(os.readlink(source))
                elif source.is_file():
                    shutil.copy2(source, backup)

        print("=== Repairing messagebus account ===")
        ensure_messagebus_account(mountpoint)
        verify_unit_account(mountpoint)

        print("=== Repairing agetty path ===")
        ensure_agetty(mountpoint)

        print("=== Repairing D-Bus library consistency ===")
        ensure_matching_dbus_library(mountpoint)

        print("=== Preparing live machine ID ===")
        prepare_machine_id(mountpoint)

        print("=== Rebuilding target linker cache ===")
        ldconfig = run(
            ["ldconfig", "-r", str(mountpoint)],
            check=False,
            capture=True,
        )
        if ldconfig.returncode != 0:
            print(
                "WARNING: host ldconfig reported:\n"
                + (ldconfig.stdout or "[no output]")
            )

        code, output = chroot_probe(
            mountpoint,
            "/usr/bin/dbus-daemon",
            ["--version"],
        )
        if code != 0:
            die(
                "Final dbus-daemon probe failed after ldconfig:\n"
                + (output or "[no output]")
            )

        code, output = chroot_probe(
            mountpoint,
            "/usr/sbin/agetty",
            ["--version"],
        )
        if code != 0:
            die(
                "Final agetty probe failed:\n"
                + (output or "[no output]")
            )

        print("Final dbus-daemon probe: PASS")
        print("Final agetty probe: PASS")
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

    print("=== Checking rootfs.ext2 ===")
    run(["e2fsck", "-f", "-y", str(ROOTFS)])
    run(["e2fsck", "-fn", str(ROOTFS)])

    print("=== Rebuilding verified ISO ===")
    run([sys.executable, str(ISO_BUILDER)])

    print()
    print("=== SUCCESS ===")
    print("messagebus user/group now exist.")
    print("/usr/sbin/agetty is valid and executable.")
    print("dbus-daemon now has a matching libdbus library.")
    print("machine-id is prepared for per-boot generation.")
    print("The ext2 root and final ISO were rebuilt and verified.")
    print()
    print("Boot Hardened Arch Live (Debug) again.")


if __name__ == "__main__":
    main()
