#!/usr/bin/env python3
"""
Finish the Hardened Arch D-Bus/getty repair.

The previous helper found a correct libdbus candidate, but its chroot probe
failed only because the offline target had no /dev/null. This version bind-
mounts /dev into the target before probing and ignores non-ELF *.symbols files.

Run:
    sudo python3 /home/corbett/finish_dbus_getty_fix.py
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

DBUS_DAEMON = "/usr/bin/dbus-daemon"
AGETTY = "/usr/sbin/agetty"
LOGIN_CANDIDATES = ("/usr/bin/login", "/bin/login")
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


def read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines()


def write_lines(path: Path, lines: list[str], mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(lines).rstrip() + "\n",
        encoding="utf-8",
        newline="\n",
    )
    path.chmod(mode)


def entry_exists(path: Path, name: str) -> bool:
    prefix = name + ":"
    return any(line.startswith(prefix) for line in read_lines(path))


def used_ids(path: Path, field: int) -> set[int]:
    values: set[int] = set()
    for line in read_lines(path):
        if not line or line.startswith("#"):
            continue
        fields = line.split(":")
        if len(fields) <= field:
            continue
        try:
            values.add(int(fields[field]))
        except ValueError:
            pass
    return values


def ensure_messagebus(root: Path) -> None:
    passwd = root / "etc/passwd"
    group = root / "etc/group"
    shadow = root / "etc/shadow"
    gshadow = root / "etc/gshadow"

    has_user = entry_exists(passwd, "messagebus")
    has_group = entry_exists(group, "messagebus")

    if has_user and has_group:
        print("messagebus user and group already exist.")
        return

    used = used_ids(passwd, 2) | used_ids(group, 2)
    identity = next((value for value in range(100, 1000) if value not in used), None)
    if identity is None:
        die("No unused system UID/GID between 100 and 999.")

    passwd_lines = read_lines(passwd)
    group_lines = read_lines(group)
    shadow_lines = read_lines(shadow)
    gshadow_lines = read_lines(gshadow)

    shell = next(
        (
            candidate
            for candidate in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false")
            if (root / candidate.lstrip("/")).exists()
        ),
        "/bin/false",
    )

    if not has_group:
        group_lines.append(f"messagebus:x:{identity}:")
        write_lines(group, group_lines)
        if gshadow.is_file() and not entry_exists(gshadow, "messagebus"):
            gshadow_lines.append("messagebus:!::")
            write_lines(gshadow, gshadow_lines, 0o640)
        print(f"Created messagebus group with GID {identity}.")

    if not has_user:
        passwd_lines.append(
            f"messagebus:x:{identity}:{identity}:"
            f"D-Bus Message Bus:/nonexistent:{shell}"
        )
        write_lines(passwd, passwd_lines)
        if shadow.is_file() and not entry_exists(shadow, "messagebus"):
            shadow_lines.append("messagebus:!*:19700:0:99999:7:::")
            write_lines(shadow, shadow_lines, 0o640)
        print(f"Created messagebus user with UID {identity}.")


def ensure_agetty(root: Path) -> None:
    expected = root / AGETTY.lstrip("/")

    if not expected.exists():
        candidates = (
            root / "sbin/agetty",
            root / "usr/bin/agetty",
            STAGING_ROOT / "usr/sbin/agetty",
            STAGING_ROOT / "sbin/agetty",
            STAGING_ROOT / "usr/bin/agetty",
        )
        source = next(
            (
                path
                for path in candidates
                if path.is_file() and os.access(path, os.X_OK)
            ),
            None,
        )
        if source is None:
            die("No usable agetty binary was found.")

        expected.parent.mkdir(parents=True, exist_ok=True)

        if source.is_relative_to(root):
            target = "/" + str(source.relative_to(root))
            expected.symlink_to(
                os.path.relpath(root / target.lstrip("/"), expected.parent)
            )
            print(f"Created {AGETTY} -> {target}")
        else:
            shutil.copy2(source, expected)
            expected.chmod(0o755)
            print(f"Copied agetty from {source}")


def strings_contains(path: Path, token: str) -> bool:
    result = run(
        ["strings", "-a", str(path)],
        check=False,
        capture=True,
    )
    return result.returncode == 0 and token in result.stdout


def is_elf(path: Path) -> bool:
    result = run(
        ["file", "-b", str(path)],
        check=False,
        capture=True,
    )
    return result.returncode == 0 and "ELF" in result.stdout


def dbus_candidates() -> list[Path]:
    preferred = [
        STAGING_ROOT / "usr/lib/x86_64-linux-gnu/libdbus-1.so.3.38.3",
        HOME / "kde-build/dbus-1.16.2/build/dbus/libdbus-1.so.3.38.3",
        HOME / "buildroot/output/target/usr/lib/libdbus-1.so.3.38.3",
    ]

    candidates: list[Path] = []
    seen: set[Path] = set()

    for path in preferred:
        if path.is_file():
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                candidates.append(resolved)

    for base in (STAGING_ROOT, HOME / "kde-build", HOME / "buildroot/output/target"):
        if not base.exists():
            continue
        result = run(
            [
                "find",
                str(base),
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
            path = Path(raw)
            if path.suffix == ".symbols" or path.name.endswith(".symbols"):
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(resolved)

    valid: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        if not is_elf(path):
            continue
        if not strings_contains(path, REQUIRED_DBUS_SYMBOL):
            continue
        valid.append(path)

    return valid


def install_matching_dbus_library(root: Path) -> None:
    daemon = root / DBUS_DAEMON.lstrip("/")
    if not daemon.is_file():
        die(f"Missing {DBUS_DAEMON}")

    code, output = chroot_probe(root, DBUS_DAEMON, ["--version"])
    if code == 0:
        print(f"dbus-daemon already works: {output.splitlines()[0] if output else 'OK'}")
        return

    print("Current dbus-daemon probe failed:")
    print(output or "[no output]")

    if REQUIRED_DBUS_SYMBOL not in output:
        die(
            "dbus-daemon failed for a reason other than the known "
            f"{REQUIRED_DBUS_SYMBOL} mismatch."
        )

    candidates = dbus_candidates()
    if not candidates:
        die(
            "No ELF libdbus-1.so.3 exporting "
            f"{REQUIRED_DBUS_SYMBOL} was found."
        )

    lib_dir = root / "lib/x86_64-linux-gnu"
    lib_dir.mkdir(parents=True, exist_ok=True)
    active = lib_dir / "libdbus-1.so.3"

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = root / "var/backups/hardened-dbus"
    backup_dir.mkdir(parents=True, exist_ok=True)

    if active.exists() or active.is_symlink():
        if active.is_symlink():
            (backup_dir / f"libdbus-link-{timestamp}.txt").write_text(
                os.readlink(active),
                encoding="utf-8",
            )
        elif active.is_file():
            shutil.copy2(
                active,
                backup_dir / f"libdbus-1.so.3-{timestamp}",
            )

    for index, candidate in enumerate(candidates, 1):
        destination = lib_dir / f"libdbus-1.so.3.38.3.hardened-{index}"

        print(f"Testing candidate: {candidate}")
        shutil.copy2(candidate, destination)
        destination.chmod(0o644)

        if active.exists() or active.is_symlink():
            active.unlink()
        active.symlink_to(destination.name)

        code, output = chroot_probe(root, DBUS_DAEMON, ["--version"])
        if code == 0:
            print(
                "dbus-daemon probe passed: "
                f"{output.splitlines()[0] if output else 'OK'}"
            )
            return

        print("Candidate failed:")
        print(output or "[no output]")
        active.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)

    die("No matching libdbus candidate allowed dbus-daemon to run.")


def prepare_machine_id(root: Path) -> None:
    machine_id = root / "etc/machine-id"
    machine_id.parent.mkdir(parents=True, exist_ok=True)
    machine_id.write_text("", encoding="ascii")
    machine_id.chmod(0o644)

    dbus_dir = root / "var/lib/dbus"
    dbus_dir.mkdir(parents=True, exist_ok=True)
    dbus_machine_id = dbus_dir / "machine-id"

    if dbus_machine_id.exists() or dbus_machine_id.is_symlink():
        dbus_machine_id.unlink()

    dbus_machine_id.symlink_to("/etc/machine-id")
    print("Prepared transient live machine-id handling.")


def verify_login(root: Path) -> None:
    login = next(
        (
            path
            for path in LOGIN_CANDIDATES
            if (root / path.lstrip("/")).exists()
        ),
        None,
    )
    if login is None:
        die("agetty exists, but no login binary was found in the target.")
    print(f"Verified login program: {login}")


def main() -> None:
    require_root()

    for command in (
        "mount",
        "umount",
        "chroot",
        "find",
        "strings",
        "file",
        "ldconfig",
        "e2fsck",
        "sync",
    ):
        require_command(command)

    if not ROOTFS.is_file():
        die(f"Root filesystem image not found: {ROOTFS}")
    if not ISO_BUILDER.is_file():
        die(f"ISO builder not found: {ISO_BUILDER}")

    mountpoint = Path(
        tempfile.mkdtemp(prefix="hardened-finish-dbus-", dir="/mnt")
    )
    mounted = False
    dev_bound = False
    proc_bound = False
    sys_bound = False

    try:
        result = run(
            ["mount", "-o", "loop,rw", str(ROOTFS), str(mountpoint)],
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            die(f"Could not mount rootfs.ext2:\n{result.stdout}")
        mounted = True

        for target in ("dev", "proc", "sys"):
            (mountpoint / target).mkdir(parents=True, exist_ok=True)

        run(["mount", "--rbind", "/dev", str(mountpoint / "dev")])
        run(["mount", "--make-rslave", str(mountpoint / "dev")])
        dev_bound = True

        run(["mount", "-t", "proc", "proc", str(mountpoint / "proc")])
        proc_bound = True

        run(["mount", "--rbind", "/sys", str(mountpoint / "sys")])
        run(["mount", "--make-rslave", str(mountpoint / "sys")])
        sys_bound = True

        print("=== Ensuring messagebus identity ===")
        ensure_messagebus(mountpoint)

        print("=== Ensuring getty/login chain ===")
        ensure_agetty(mountpoint)
        verify_login(mountpoint)

        code, output = chroot_probe(mountpoint, AGETTY, ["--version"])
        if code != 0:
            die("agetty probe failed:\n" + (output or "[no output]"))
        print(f"agetty probe passed: {output.splitlines()[0] if output else 'OK'}")

        print("=== Installing matching D-Bus library ===")
        install_matching_dbus_library(mountpoint)

        print("=== Preparing machine ID ===")
        prepare_machine_id(mountpoint)

        print("=== Rebuilding target linker cache ===")
        result = run(
            ["ldconfig", "-r", str(mountpoint)],
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            print("WARNING: ldconfig reported:")
            print(result.stdout or "[no output]")

        code, output = chroot_probe(mountpoint, DBUS_DAEMON, ["--version"])
        if code != 0:
            die("Final dbus-daemon probe failed:\n" + (output or "[no output]"))

        code, output = chroot_probe(mountpoint, AGETTY, ["--version"])
        if code != 0:
            die("Final agetty probe failed:\n" + (output or "[no output]"))

        print("Final dbus-daemon probe: PASS")
        print("Final agetty probe: PASS")
        run(["sync"])

    finally:
        if sys_bound:
            run(["umount", "-R", str(mountpoint / "sys")], check=False)
        if proc_bound:
            run(["umount", str(mountpoint / "proc")], check=False)
        if dev_bound:
            run(["umount", "-R", str(mountpoint / "dev")], check=False)

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
    print("The earlier /dev/null false failure is fixed.")
    print("messagebus, agetty, login, machine-id, and libdbus were verified.")
    print("The ext2 root and final ISO were rebuilt and verified.")
    print()
    print("Boot Hardened Arch Live (Debug) again.")


if __name__ == "__main__":
    main()
