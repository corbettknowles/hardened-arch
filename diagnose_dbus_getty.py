#!/usr/bin/env python3
"""
Read-only diagnosis of D-Bus and serial-getty failures in rootfs.ext2.

Run:
    sudo python3 /home/corbett/diagnose_dbus_getty.py
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOTFS = Path("/home/corbett/iso-systemd/rootfs.ext2")


def die(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def run(
    args: list[str],
    *,
    check: bool = False,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(item) for item in args],
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def require_root() -> None:
    if os.geteuid() != 0:
        die("Run this script with sudo.")


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        die(f"Required command not found: {name}")


def root_path(root: Path, absolute: str) -> Path:
    return root / absolute.lstrip("/")


def describe(root: Path, absolute: str) -> bool:
    path = root_path(root, absolute)
    if not path.exists() and not path.is_symlink():
        print(f"MISSING  {absolute}")
        return False

    if path.is_symlink():
        print(f"SYMLINK  {absolute} -> {os.readlink(path)}")
    elif path.is_dir():
        print(f"DIR      {absolute}")
    else:
        mode = oct(path.stat().st_mode & 0o7777)
        executable = " executable" if os.access(path, os.X_OK) else ""
        print(
            f"FILE     {absolute}  "
            f"{path.stat().st_size} bytes  mode={mode}{executable}"
        )
    return True


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def find_unit(root: Path, unit_name: str) -> Path | None:
    candidates = (
        root / "etc/systemd/system" / unit_name,
        root / "run/systemd/system" / unit_name,
        root / "usr/local/lib/systemd/system" / unit_name,
        root / "usr/lib/systemd/system" / unit_name,
        root / "lib/systemd/system" / unit_name,
    )

    for candidate in candidates:
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return None


def resolve_inside_root(root: Path, path: Path) -> Path:
    seen: set[Path] = set()
    current = path

    for _ in range(20):
        if current in seen:
            return current
        seen.add(current)

        if not current.is_symlink():
            return current

        target = os.readlink(current)
        if target.startswith("/"):
            current = root / target.lstrip("/")
        else:
            current = current.parent / target

    return current


def print_unit(root: Path, unit_name: str) -> tuple[Path | None, str]:
    unit = find_unit(root, unit_name)
    print(f"=== {unit_name} ===")

    if unit is None:
        print("[unit not found]")
        print()
        return None, ""

    print(f"Path: /{unit.relative_to(root)}")
    resolved = resolve_inside_root(root, unit)
    if resolved != unit:
        print(f"Resolves to: /{resolved.relative_to(root)}")

    if not resolved.is_file():
        print("[resolved unit is not a regular file]")
        print()
        return resolved, ""

    text = read_text(resolved)
    print(text.rstrip())
    print()
    return resolved, text


def parse_exec_paths(unit_text: str) -> list[str]:
    paths: list[str] = []

    for raw in unit_text.splitlines():
        stripped = raw.strip()
        if not stripped.startswith("ExecStart="):
            continue

        value = stripped.split("=", 1)[1].strip()
        value = value.lstrip("-+!:@")

        try:
            parts = shlex.split(value)
        except ValueError:
            parts = value.split()

        if not parts:
            continue

        executable = parts[0]
        if executable.startswith("/"):
            paths.append(executable)

    return paths


def parse_user_group(unit_text: str) -> tuple[list[str], list[str]]:
    users: list[str] = []
    groups: list[str] = []

    for raw in unit_text.splitlines():
        stripped = raw.strip()

        if stripped.startswith("User="):
            users.append(stripped.split("=", 1)[1].strip())
        elif stripped.startswith("Group="):
            groups.append(stripped.split("=", 1)[1].strip())

    return users, groups


def account_exists(root: Path, database: str, name: str) -> bool:
    path = root / "etc" / database
    if not path.is_file():
        return False

    prefix = name + ":"
    return any(
        line.startswith(prefix)
        for line in read_text(path).splitlines()
    )


def elf_interpreter(path: Path) -> str | None:
    if shutil.which("readelf") is None or not path.is_file():
        return None

    result = run(["readelf", "-l", str(path)])
    match = re.search(r"Requesting program interpreter:\s*([^\]]+)", result.stdout)
    return match.group(1).strip() if match else None


def chroot_probe(root: Path, executable: str, arguments: list[str]) -> tuple[int, str]:
    command = ["chroot", str(root), executable, *arguments]
    result = run(command)
    return result.returncode, result.stdout.strip()


def print_machine_id(root: Path) -> list[str]:
    findings: list[str] = []

    print("=== Machine ID ===")
    for absolute in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        path = root_path(root, absolute)

        if not path.is_file():
            print(f"MISSING  {absolute}")
            continue

        value = read_text(path).strip()
        print(f"{absolute}: {value!r}")

        if not value:
            findings.append(f"{absolute} is empty.")
        elif len(value) != 32:
            findings.append(
                f"{absolute} has an unexpected length of {len(value)}."
            )
    print()
    return findings


def main() -> None:
    require_root()
    require_command("mount")
    require_command("umount")
    require_command("chroot")

    if not ROOTFS.is_file():
        die(f"Root filesystem image not found: {ROOTFS}")

    mountpoint = Path(tempfile.mkdtemp(prefix="dbus-getty-diag-", dir="/mnt"))
    mounted = False
    findings: list[str] = []

    try:
        result = run(
            ["mount", "-o", "loop,ro", str(ROOTFS), str(mountpoint)]
        )
        if result.returncode != 0:
            die(f"Could not mount rootfs.ext2 read-only:\n{result.stdout}")
        mounted = True

        dbus_unit_path, dbus_unit_text = print_unit(
            mountpoint, "dbus.service"
        )
        getty_unit_path, getty_unit_text = print_unit(
            mountpoint, "serial-getty@.service"
        )

        print("=== D-Bus files and executables ===")
        common_dbus_paths = (
            "/usr/bin/dbus-daemon",
            "/usr/bin/dbus-broker",
            "/usr/bin/dbus-broker-launch",
            "/usr/share/dbus-1/system.conf",
            "/etc/dbus-1/system.conf",
            "/usr/share/dbus-1/system.d",
            "/etc/dbus-1/system.d",
            "/usr/share/dbus-1/system-services",
            "/run/dbus",
        )
        for absolute in common_dbus_paths:
            describe(mountpoint, absolute)
        print()

        print("=== Getty/login files and executables ===")
        common_getty_paths = (
            "/usr/bin/agetty",
            "/sbin/agetty",
            "/usr/bin/login",
            "/bin/login",
            "/usr/sbin/sulogin",
            "/sbin/sulogin",
            "/etc/securetty",
        )
        for absolute in common_getty_paths:
            describe(mountpoint, absolute)
        print()

        print("=== Unit executable checks ===")
        all_exec_paths: list[tuple[str, str]] = []

        for label, text in (
            ("dbus.service", dbus_unit_text),
            ("serial-getty@.service", getty_unit_text),
        ):
            for executable in parse_exec_paths(text):
                all_exec_paths.append((label, executable))
                target = root_path(mountpoint, executable)

                if not target.exists():
                    print(f"MISSING  {label} ExecStart binary: {executable}")
                    findings.append(
                        f"PRIMARY SUSPECT: {label} references missing {executable}."
                    )
                    continue

                print(f"PRESENT  {label} ExecStart binary: {executable}")
                interpreter = elf_interpreter(target)

                if interpreter:
                    loader = root_path(mountpoint, interpreter)
                    state = "present" if loader.exists() else "MISSING"
                    print(f"         ELF loader {interpreter}: {state}")

                    if not loader.exists():
                        findings.append(
                            f"PRIMARY SUSPECT: {executable} requires missing "
                            f"ELF loader {interpreter}."
                        )
        print()

        print("=== Service account checks ===")
        users, groups = parse_user_group(dbus_unit_text)

        for user in users:
            exists = account_exists(mountpoint, "passwd", user)
            print(f"User={user}: {'present' if exists else 'MISSING'}")
            if not exists:
                findings.append(
                    f"PRIMARY SUSPECT: dbus.service requires missing user {user}."
                )

        for group in groups:
            exists = account_exists(mountpoint, "group", group)
            print(f"Group={group}: {'present' if exists else 'MISSING'}")
            if not exists:
                findings.append(
                    f"PRIMARY SUSPECT: dbus.service requires missing group {group}."
                )

        for account in ("dbus", "messagebus"):
            passwd = account_exists(mountpoint, "passwd", account)
            group = account_exists(mountpoint, "group", account)
            print(
                f"{account}: passwd={'yes' if passwd else 'no'}, "
                f"group={'yes' if group else 'no'}"
            )
        print()

        findings.extend(print_machine_id(mountpoint))

        print("=== Chroot executable probes ===")
        probes: list[tuple[str, list[str]]] = []

        if root_path(mountpoint, "/usr/bin/dbus-daemon").exists():
            probes.append(("/usr/bin/dbus-daemon", ["--version"]))

        if root_path(mountpoint, "/usr/bin/dbus-broker-launch").exists():
            probes.append(("/usr/bin/dbus-broker-launch", ["--version"]))

        if root_path(mountpoint, "/usr/bin/agetty").exists():
            probes.append(("/usr/bin/agetty", ["--version"]))
        elif root_path(mountpoint, "/sbin/agetty").exists():
            probes.append(("/sbin/agetty", ["--version"]))

        if not probes:
            print("[no probeable executables found]")
        else:
            for executable, arguments in probes:
                code, output = chroot_probe(
                    mountpoint, executable, arguments
                )
                print(f"{executable} {' '.join(arguments)} -> exit {code}")
                if output:
                    print("  " + output.replace("\n", "\n  "))

                if code != 0:
                    findings.append(
                        f"{executable} cannot run cleanly in the target root "
                        f"(exit {code}): {output[:300]}"
                    )
        print()

        print("=== VERDICT ===")
        if findings:
            for index, finding in enumerate(findings, 1):
                print(f"{index}. {finding}")
        else:
            print(
                "The offline files look complete. The next useful evidence is "
                "`systemctl status dbus.service` or the D-Bus journal message."
            )

    finally:
        if mounted:
            result = run(["umount", str(mountpoint)])
            if result.returncode != 0:
                print(
                    f"WARNING: could not unmount {mountpoint}:\n{result.stdout}",
                    file=sys.stderr,
                )

        try:
            mountpoint.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    main()
