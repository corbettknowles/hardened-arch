#!/usr/bin/env python3
"""
Comprehensive offline graphics/display-stack diagnostic for a Hardened Arch ISO.

The script DOES NOT modify the ISO. It extracts the SquashFS root filesystem,
checks the known variables that commonly break SDDM/GDM, Xorg, Wayland,
Xwayland, KWin, Plasma, Qt, Mesa/DRM, PAM, systemd, kernel graphics support,
library closure, symlinks, sessions, and boot arguments, then writes a ranked
text report, JSON report, and uploadable tar.gz archive.

Requirements on the Arch WSL host:
    sudo pacman -S --needed xorriso squashfs-tools binutils file

Usage:
    python3 check_iso_graphics.py \
      ~/hardened-arch-1.10-alpha-x86_64.iso \
      --kernel-config ~/linux-7.1.2-build/.config
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass
class Finding:
    severity: str
    category: str
    check: str
    detail: str
    fix: str = ""


class Report:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.raw_dir = out_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.findings: list[Finding] = []

    def add(
        self,
        severity: str,
        category: str,
        check: str,
        detail: str,
        fix: str = "",
    ) -> None:
        severity = severity.upper()
        finding = Finding(severity, category, check, detail.strip(), fix.strip())
        self.findings.append(finding)
        icon = {"FAIL": "[FAIL]", "WARN": "[WARN]", "PASS": "[PASS]", "INFO": "[INFO]"}
        print(f"{icon.get(severity, '[INFO]')} {category}: {check}")
        for line in finding.detail.splitlines():
            print(f"       {line}")
        if finding.fix:
            print(f"       FIX: {finding.fix}")

    def raw(self, name: str, text: str) -> None:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        (self.raw_dir / safe).write_text(text, encoding="utf-8", errors="replace")

    def counts(self) -> dict[str, int]:
        counts = {"FAIL": 0, "WARN": 0, "PASS": 0, "INFO": 0}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts

    def write(self, metadata: dict[str, object]) -> tuple[Path, Path]:
        txt = self.out_dir / "graphics-diagnostic.txt"
        jsn = self.out_dir / "graphics-diagnostic.json"

        payload = {
            "metadata": metadata,
            "summary": self.counts(),
            "findings": [asdict(f) for f in self.findings],
        }
        jsn.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        order = {"FAIL": 0, "WARN": 1, "INFO": 2, "PASS": 3}
        lines = [
            "HARDENED ARCH ISO GRAPHICS DIAGNOSTIC",
            "=" * 78,
        ]
        for key, value in metadata.items():
            lines.append(f"{key}: {value}")
        counts = self.counts()
        lines += [
            "",
            f"SUMMARY: FAIL={counts['FAIL']} WARN={counts['WARN']} "
            f"PASS={counts['PASS']} INFO={counts['INFO']}",
            "",
        ]
        for finding in sorted(
            self.findings,
            key=lambda f: (order.get(f.severity, 9), f.category, f.check),
        ):
            lines.append(f"[{finding.severity}] {finding.category}: {finding.check}")
            for line in finding.detail.splitlines():
                lines.append(f"    {line}")
            if finding.fix:
                lines.append(f"    FIX: {finding.fix}")
            lines.append("")
        txt.write_text("\n".join(lines), encoding="utf-8")
        return txt, jsn


def run(
    args: Sequence[str],
    *,
    timeout: int = 900,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        check=False,
    )


def require(report: Report, commands: Iterable[str]) -> None:
    missing = [cmd for cmd in commands if shutil.which(cmd) is None]
    if missing:
        report.add(
            "FAIL",
            "Host",
            "Required tools",
            "Missing commands: " + ", ".join(missing),
            "sudo pacman -S --needed xorriso squashfs-tools binutils file",
        )
        raise SystemExit(2)
    report.add("PASS", "Host", "Required tools", "All required inspection tools are present.")


def sha256(path: Path) -> str:
    proc = run(["sha256sum", str(path)])
    return proc.stdout.split()[0] if proc.returncode == 0 else "unknown"


def root_path(root: Path, guest: str) -> Path:
    return root / guest.lstrip("/")


def exists(root: Path, guest: str) -> bool:
    path = root_path(root, guest)
    return path.exists() or path.is_symlink()


def executable(root: Path, guest: str) -> bool:
    path = root_path(root, guest)
    try:
        return path.is_file() and bool(path.stat().st_mode & stat.S_IXUSR)
    except OSError:
        return False


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def symlink_target(root: Path, guest: str) -> str | None:
    path = root_path(root, guest)
    if not path.is_symlink():
        return None
    try:
        return os.readlink(path)
    except OSError:
        return None


def symlink_resolves(root: Path, guest: str) -> bool:
    path = root_path(root, guest)
    if not path.is_symlink():
        return path.exists()
    try:
        target = os.readlink(path)
    except OSError:
        return False
    candidate = root / target.lstrip("/") if target.startswith("/") else path.parent / target
    return candidate.exists() or candidate.is_symlink()


def extract_iso_file(iso: Path, iso_path: str, dest: Path) -> tuple[bool, str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = run(
        [
            "xorriso",
            "-osirrox",
            "on",
            "-indev",
            str(iso),
            "-extract",
            iso_path,
            str(dest),
        ],
        timeout=1800,
    )
    return proc.returncode == 0 and dest.exists(), proc.stdout


def check_bootloader(report: Report, iso: Path, work: Path) -> None:
    el = run(["xorriso", "-indev", str(iso), "-report_el_torito", "plain"])
    report.raw("el-torito.txt", el.stdout)
    if el.returncode == 0 and re.search(r"UEFI|EFI", el.stdout, re.I):
        report.add("PASS", "ISO boot", "UEFI El Torito entry", "UEFI boot information is present.")
    else:
        report.add("WARN", "ISO boot", "UEFI El Torito entry", "No UEFI marker was recognized.")

    configs = [
        "/EFI/BOOT/refind.conf",
        "/boot/refind.conf",
        "/loader/entries/hardened-arch.conf",
        "/loader/loader.conf",
    ]
    found = False
    for iso_path in configs:
        dest = work / ("boot_" + iso_path.strip("/").replace("/", "_"))
        ok, output = extract_iso_file(iso, iso_path, dest)
        if not ok:
            continue
        found = True
        text = read_text(dest)
        report.raw(dest.name + ".txt", text)
        dangerous = [
            token
            for token in (
                "nomodeset",
                "modprobe.blacklist=vmwgfx",
                "modprobe.blacklist=vboxvideo",
                "modprobe.blacklist=virtio_gpu",
                "rd.driver.blacklist=vmwgfx",
                "rd.driver.blacklist=vboxvideo",
                "rd.driver.blacklist=virtio_gpu",
                "systemd.unit=multi-user.target",
                "systemd.unit=rescue.target",
                "systemd.unit=emergency.target",
            )
            if token in text
        ]
        if dangerous:
            report.add(
                "FAIL",
                "Boot configuration",
                iso_path,
                "Graphics-breaking options found: " + ", ".join(dangerous),
                "Remove them from the live boot entry.",
            )
        else:
            report.add("PASS", "Boot configuration", iso_path, "No obvious graphics-disabling option found.")
        if "quiet" in text or "splash" in text:
            report.add(
                "INFO",
                "Boot configuration",
                f"{iso_path} verbosity",
                "quiet/splash is enabled and can hide the first graphics failure.",
                "Keep a separate debug entry without quiet or splash.",
            )
    if not found:
        report.add(
            "WARN",
            "Boot configuration",
            "Readable config",
            "No common plain-text bootloader config was found. It may be embedded in efiboot.img.",
        )


def check_usr_merge(report: Report, root: Path) -> None:
    expected = {
        "/bin": "usr/bin",
        "/sbin": "usr/bin",
        "/lib": "usr/lib",
        "/lib64": "usr/lib",
        "/usr/sbin": "bin",
        "/usr/lib64": "lib",
    }
    for guest, wanted in expected.items():
        path = root_path(root, guest)
        if not (path.exists() or path.is_symlink()):
            severity = "WARN" if guest == "/usr/lib64" else "FAIL"
            report.add(severity, "Filesystem", guest, "Missing.", f"Expected symlink to {wanted}.")
        elif path.is_symlink():
            target = os.readlink(path)
            if target in {wanted, "/" + wanted}:
                report.add("PASS", "Filesystem", guest, f"Symlink -> {target}")
            else:
                report.add("WARN", "Filesystem", guest, f"Symlink -> {target}; expected {wanted}.")
        else:
            report.add(
                "WARN",
                "Filesystem",
                guest,
                "Real directory instead of merged-/usr symlink. systemd may report Tainted: unmerged-bin.",
                f"Merge contents carefully and replace it with a symlink to {wanted}.",
            )


def check_systemd(report: Report, root: Path) -> None:
    default = symlink_target(root, "/etc/systemd/system/default.target")
    if default and default.endswith("graphical.target") and symlink_resolves(root, "/etc/systemd/system/default.target"):
        report.add("PASS", "systemd", "Default target", f"default.target -> {default}")
    else:
        report.add(
            "FAIL",
            "systemd",
            "Default target",
            f"default.target is {default!r}.",
            "Point it to /usr/lib/systemd/system/graphical.target.",
        )

    dm_guest = "/etc/systemd/system/display-manager.service"
    dm = symlink_target(root, dm_guest)
    if dm and symlink_resolves(root, dm_guest):
        report.add("PASS", "systemd", "Display manager link", f"{dm_guest} -> {dm}")
    elif dm:
        report.add(
            "FAIL",
            "systemd",
            "Display manager link",
            f"{dm_guest} -> {dm}, but the target is missing.",
            "Point it to the installed sddm.service or gdm.service.",
        )
    else:
        report.add(
            "FAIL",
            "systemd",
            "Display manager link",
            "display-manager.service is missing or not a symlink.",
            "Enable the intended display manager inside runtime-package-root.",
        )

    units = [
        "/usr/lib/systemd/system/graphical.target",
        "/usr/lib/systemd/system/systemd-logind.service",
        "/usr/lib/systemd/system/systemd-udevd.service",
        "/usr/lib/systemd/system/dbus.service",
    ]
    for unit in units:
        report.add(
            "PASS" if exists(root, unit) else "FAIL",
            "systemd",
            unit,
            "Present." if exists(root, unit) else "Missing.",
        )

    polkit_candidates = [
        "/usr/lib/systemd/system/polkit.service",
        "/usr/lib/systemd/system/polkit.service.in",
        "/usr/lib/polkit-1/polkitd",
    ]
    if any(exists(root, path) for path in polkit_candidates):
        report.add("PASS", "systemd", "polkit", "polkit daemon/service files are present.")
    else:
        report.add("WARN", "systemd", "polkit", "No polkit daemon/service file was found.")


def check_binaries(report: Report, root: Path) -> list[str]:
    required = [
        "/usr/bin/Xorg",
        "/usr/bin/Xwayland",
        "/usr/bin/kwin_wayland",
        "/usr/bin/startplasma-wayland",
        "/usr/bin/plasmashell",
        "/usr/bin/dbus-run-session",
    ]
    dynamic_targets: list[str] = []
    for guest in required:
        if executable(root, guest):
            report.add("PASS", "Graphics binaries", guest, "Present and executable.")
            dynamic_targets.append(guest)
        else:
            report.add(
                "FAIL",
                "Graphics binaries",
                guest,
                "Missing or not executable.",
                "Stage the package/build output that provides this file.",
            )

    sddm = [
        "/usr/bin/sddm",
        "/usr/bin/sddm-greeter-qt6",
        "/usr/lib/sddm-helper",
    ]
    gdm = ["/usr/bin/gdm"]
    sddm_count = sum(executable(root, path) for path in sddm)
    gdm_count = sum(executable(root, path) for path in gdm)

    if sddm_count:
        for guest in sddm:
            if executable(root, guest):
                report.add("PASS", "SDDM", guest, "Present and executable.")
                dynamic_targets.append(guest)
            else:
                report.add("FAIL", "SDDM", guest, "Missing or not executable.")
    if gdm_count:
        report.add("PASS", "GDM", "/usr/bin/gdm", "Present and executable.")
        dynamic_targets.append("/usr/bin/gdm")
    if not sddm_count and not gdm_count:
        report.add("FAIL", "Display manager", "Installed manager", "Neither SDDM nor GDM was found.")

    optional = [
        "/usr/bin/startplasma-x11",
        "/usr/bin/kwin_x11",
        "/usr/bin/wayland-info",
        "/usr/bin/eglinfo",
        "/usr/bin/glxinfo",
    ]
    for guest in optional:
        if executable(root, guest):
            report.add("INFO", "Graphics binaries", guest, "Present.")
            dynamic_targets.append(guest)

    return sorted(set(dynamic_targets))


def parse_desktop_exec(path: Path) -> str | None:
    text = read_text(path)
    match = re.search(r"^Exec=(.+)$", text, re.MULTILINE)
    if not match:
        return None
    try:
        return shlex.split(match.group(1))[0]
    except ValueError:
        return match.group(1).split()[0] if match.group(1).split() else None


def check_sessions(report: Report, root: Path) -> None:
    found = 0
    for guest_dir in ["/usr/share/wayland-sessions", "/usr/share/xsessions"]:
        directory = root_path(root, guest_dir)
        if not directory.is_dir():
            continue
        for desktop in sorted(directory.glob("*.desktop")):
            found += 1
            guest = "/" + str(desktop.relative_to(root))
            command = parse_desktop_exec(desktop)
            if not command:
                report.add("WARN", "Desktop sessions", guest, "No usable Exec= entry.")
                continue
            if command.startswith("/"):
                ok = executable(root, command)
            else:
                ok = any(executable(root, f"{prefix}/{command}") for prefix in ("/usr/bin", "/bin"))
            report.add(
                "PASS" if ok else "FAIL",
                "Desktop sessions",
                guest,
                f"Exec={command}; target {'exists' if ok else 'is missing'}.",
                "" if ok else "Install or correct the session launcher.",
            )
    if not found:
        report.add("FAIL", "Desktop sessions", "Session files", "No Wayland or X11 session .desktop files found.")


def check_sddm(report: Report, root: Path) -> None:
    configs: list[Path] = []
    for guest in ["/etc/sddm.conf", "/etc/sddm.conf.d", "/usr/lib/sddm/sddm.conf.d"]:
        path = root_path(root, guest)
        if path.is_file():
            configs.append(path)
        elif path.is_dir():
            configs += sorted(path.glob("*.conf"))

    if not configs:
        report.add("INFO", "SDDM", "Configuration", "No explicit configuration; compiled defaults apply.")
        return

    combined = ""
    for path in configs:
        combined += f"\n# /{path.relative_to(root)}\n{read_text(path)}\n"
    report.raw("sddm-all-configs.txt", combined)

    matches = re.findall(r"^\s*DisplayServer\s*=\s*(\S+)", combined, re.I | re.M)
    mode = matches[-1] if matches else "compiled/default"
    report.add("INFO", "SDDM", "DisplayServer", f"DisplayServer={mode}")

    if mode.lower() == "wayland":
        prerequisites = [
            "/usr/bin/kwin_wayland",
            "/usr/bin/Xwayland",
            "/usr/lib/qt6/plugins/platforms/libqwayland-egl.so",
        ]
        missing = [path for path in prerequisites if not exists(root, path)]
        if missing:
            report.add(
                "FAIL",
                "SDDM",
                "Wayland greeter prerequisites",
                "Missing: " + ", ".join(missing),
                "Install the missing KWin/Xwayland/Qt Wayland pieces or temporarily use an X11 greeter.",
            )
        else:
            report.add("PASS", "SDDM", "Wayland greeter prerequisites", "Core prerequisites are present.")


def check_accounts_pam(report: Report, root: Path) -> None:
    passwd = read_text(root / "etc/passwd")
    groups = read_text(root / "etc/group")
    shadow = read_text(root / "etc/shadow")

    accounts: dict[str, list[str]] = {}
    for line in passwd.splitlines():
        fields = line.split(":")
        if len(fields) >= 7:
            accounts[fields[0]] = fields

    if executable(root, "/usr/bin/sddm"):
        if "sddm" in accounts:
            f = accounts["sddm"]
            report.add("PASS", "Accounts", "sddm", f"uid={f[2]} gid={f[3]} home={f[5]} shell={f[6]}")
        else:
            report.add("FAIL", "Accounts", "sddm", "SDDM binary exists but the sddm account is missing.")

    if executable(root, "/usr/bin/gdm"):
        if "gdm" in accounts:
            f = accounts["gdm"]
            report.add("PASS", "Accounts", "gdm", f"uid={f[2]} gid={f[3]} home={f[5]} shell={f[6]}")
        else:
            report.add("FAIL", "Accounts", "gdm", "GDM binary exists but the gdm account is missing.")

    group_names = {line.split(":")[0] for line in groups.splitlines() if ":" in line}
    for name in ("video", "render", "input"):
        report.add(
            "PASS" if name in group_names else "WARN",
            "Accounts",
            f"{name} group",
            "Present." if name in group_names else "Missing; logind ACLs may still work, but the image is incomplete.",
        )

    root_line = next((line for line in shadow.splitlines() if line.startswith("root:")), "")
    if root_line:
        field = root_line.split(":")[1]
        state = "locked/empty" if field in {"", "!", "*"} or field.startswith("!") else "password set"
        report.add("INFO", "Accounts", "Root password state", state)

    pam_files = [
        "/etc/pam.d/sddm",
        "/etc/pam.d/sddm-autologin",
        "/etc/pam.d/gdm-password",
        "/etc/pam.d/login",
        "/etc/pam.d/system-login",
    ]
    for guest in pam_files:
        path = root_path(root, guest)
        if not path.is_file():
            continue
        text = read_text(path)
        report.raw(guest.strip("/").replace("/", "_") + ".txt", text)
        if "pam_selinux.so" in text:
            selinux_ok = (root / "etc/selinux").is_dir() and (root / "usr/share/selinux").exists()
            report.add(
                "PASS" if selinux_ok else "FAIL",
                "PAM/SELinux",
                guest,
                "pam_selinux.so is referenced; SELinux policy/config "
                + ("appears present." if selinux_ok else "appears incomplete."),
                "" if selinux_ok else "Stage a valid SELinux policy/config or remove pam_selinux for this test build.",
            )


def find_library(root: Path, patterns: Sequence[str]) -> list[str]:
    found: set[str] = set()
    for base_guest in ("/usr/lib", "/lib", "/usr/lib64", "/lib64"):
        base = root_path(root, base_guest)
        if not base.exists():
            continue
        for pattern in patterns:
            for path in base.rglob(pattern):
                found.add("/" + str(path.relative_to(root)))
    return sorted(found)


def check_libraries_plugins(report: Report, root: Path) -> list[str]:
    libs = {
        "Wayland client": ["libwayland-client.so.0*"],
        "Wayland server": ["libwayland-server.so.0*"],
        "Wayland EGL": ["libwayland-egl.so.1*"],
        "XKB common": ["libxkbcommon.so.0*"],
        "XKB X11": ["libxkbcommon-x11.so.0*"],
        "XCB": ["libxcb.so.1*"],
        "X11": ["libX11.so.6*"],
        "DRM": ["libdrm.so.2*"],
        "GBM": ["libgbm.so.1*"],
        "EGL": ["libEGL.so.1*"],
        "GLX": ["libGLX.so.0*"],
        "OpenGL": ["libOpenGL.so.0*"],
        "Vulkan loader": ["libvulkan.so.1*"],
        "libdecor": ["libdecor-0.so.0*"],
        "Qt Wayland client": ["libQt6WaylandClient.so.6*"],
        "Qt Wayland compositor": ["libQt6WaylandCompositor.so.6*"],
    }
    for name, patterns in libs.items():
        found = find_library(root, patterns)
        required = name not in {"Vulkan loader", "Qt Wayland compositor"}
        report.add(
            "PASS" if found else ("FAIL" if required else "WARN"),
            "Graphics libraries",
            name,
            "\n".join(found[:12]) if found else "Not found.",
            "" if found else f"Stage the package providing {name}.",
        )

    groups = {
        "Qt XCB platform": ["/usr/lib/qt6/plugins/platforms/libqxcb.so"],
        "Qt Wayland platform": [
            "/usr/lib/qt6/plugins/platforms/libqwayland-egl.so",
            "/usr/lib/qt6/plugins/platforms/libqwayland-generic.so",
        ],
        "Mesa software rasterizer": [
            "/usr/lib/dri/swrast_dri.so",
            "/usr/lib/dri/kms_swrast_dri.so",
        ],
        "Mesa EGL vendor": ["/usr/share/glvnd/egl_vendor.d/50_mesa.json"],
        "LLVMpipe Vulkan ICD": [
            "/usr/share/vulkan/icd.d/lvp_icd.x86_64.json",
            "/usr/share/vulkan/icd.d/lvp_icd.json",
        ],
    }
    dynamic: list[str] = []
    for name, candidates in groups.items():
        present = [path for path in candidates if exists(root, path)]
        severity = "PASS" if present else ("WARN" if name == "LLVMpipe Vulkan ICD" else "FAIL")
        report.add(
            severity,
            "Graphics plugins",
            name,
            "\n".join(present) if present else "Missing all expected files:\n" + "\n".join(candidates),
        )
        dynamic += [path for path in present if path.endswith(".so")]
    return dynamic


def check_xorg(report: Report, root: Path) -> list[str]:
    drivers = [
        "/usr/lib/xorg/modules/drivers/modesetting_drv.so",
        "/usr/lib/xorg/modules/drivers/vboxvideo_drv.so",
        "/usr/lib/xorg/modules/drivers/vmware_drv.so",
        "/usr/lib/xorg/modules/drivers/vesa_drv.so",
        "/usr/lib/xorg/modules/drivers/fbdev_drv.so",
    ]
    present = [path for path in drivers if exists(root, path)]
    if present:
        report.add("PASS", "Xorg", "Video drivers", "\n".join(present))
    else:
        report.add(
            "FAIL",
            "Xorg",
            "Video drivers",
            "No common modesetting or virtual-machine Xorg driver was found.",
        )

    core = [
        "/usr/lib/xorg/modules/libglamoregl.so",
        "/usr/lib/xorg/modules/extensions/libglx.so",
    ]
    for path in core:
        report.add(
            "PASS" if exists(root, path) else "FAIL",
            "Xorg",
            path,
            "Present." if exists(root, path) else "Missing.",
        )
    if not exists(root, "/usr/lib/Xorg.wrap"):
        report.add(
            "WARN",
            "Xorg",
            "Xorg.wrap",
            "Missing. Rootless logind Xorg can still work, but some display-manager paths expect it.",
        )
    return present + [path for path in core if exists(root, path)]


def parse_kernel_option(text: str, option: str) -> str:
    match = re.search(rf"^{re.escape(option)}=(.+)$", text, re.M)
    if match:
        return match.group(1)
    if re.search(rf"^# {re.escape(option)} is not set$", text, re.M):
        return "n"
    return "missing"


def check_kernel(report: Report, root: Path, config_arg: Path | None) -> None:
    config = None
    if config_arg and config_arg.is_file():
        config = config_arg
    else:
        embedded = sorted((root / "boot").glob("config-*")) if (root / "boot").is_dir() else []
        if embedded:
            config = embedded[-1]

    if not config:
        report.add(
            "WARN",
            "Kernel graphics",
            "Kernel configuration",
            "No build .config supplied or embedded.",
            "Run again with --kernel-config ~/linux-7.1.2-build/.config",
        )
        return

    text = read_text(config)
    report.raw(
        "kernel-graphics-options.txt",
        "\n".join(
            line
            for line in text.splitlines()
            if re.match(r"^(CONFIG_(DRM|FB|FRAMEBUFFER|VT|INPUT|HID|VGA|EFI|PCI).*)", line)
        ),
    )

    critical = [
        "CONFIG_DRM",
        "CONFIG_DRM_KMS_HELPER",
        "CONFIG_VT",
        "CONFIG_VT_CONSOLE",
        "CONFIG_INPUT_EVDEV",
        "CONFIG_HID_GENERIC",
    ]
    for option in critical:
        value = parse_kernel_option(text, option)
        report.add(
            "PASS" if value in {"y", "m"} else "FAIL",
            "Kernel graphics",
            option,
            f"{option}={value}",
            "" if value in {"y", "m"} else f"Enable {option}=y.",
        )

    vm = {
        "CONFIG_DRM_VBOXVIDEO": "vboxvideo*.ko*",
        "CONFIG_DRM_VMWGFX": "vmwgfx*.ko*",
        "CONFIG_DRM_VIRTIO_GPU": "virtio_gpu*.ko*",
    }
    enabled = []
    for option, module_pattern in vm.items():
        value = parse_kernel_option(text, option)
        if value in {"y", "m"}:
            enabled.append(f"{option}={value}")
        if value == "m":
            module_root = root / "usr/lib/modules"
            matches = list(module_root.rglob(module_pattern)) if module_root.is_dir() else []
            report.add(
                "PASS" if matches else "FAIL",
                "Kernel graphics",
                f"{option} module",
                "\n".join("/" + str(p.relative_to(root)) for p in matches[:10])
                if matches
                else f"{option}=m but no {module_pattern} exists in /usr/lib/modules.",
            )
    if enabled:
        report.add("PASS", "Kernel graphics", "VM DRM drivers", "\n".join(enabled))
    else:
        report.add(
            "FAIL",
            "Kernel graphics",
            "VM DRM drivers",
            "None of CONFIG_DRM_VBOXVIDEO, CONFIG_DRM_VMWGFX, or CONFIG_DRM_VIRTIO_GPU is enabled.",
        )

    for option in (
        "CONFIG_DRM_SIMPLEDRM",
        "CONFIG_DRM_FBDEV_EMULATION",
        "CONFIG_FRAMEBUFFER_CONSOLE",
    ):
        value = parse_kernel_option(text, option)
        report.add(
            "PASS" if value in {"y", "m"} else "WARN",
            "Kernel graphics",
            option,
            f"{option}={value}",
        )


def check_modprobe_blacklists(report: Report, root: Path) -> None:
    bad: list[str] = []
    for guest_dir in ("/etc/modprobe.d", "/usr/lib/modprobe.d"):
        directory = root_path(root, guest_dir)
        if not directory.is_dir():
            continue
        for conf in directory.glob("*.conf"):
            text = read_text(conf)
            for module in ("vmwgfx", "vboxvideo", "virtio_gpu", "simpledrm"):
                if re.search(rf"^\s*blacklist\s+{re.escape(module)}\b", text, re.M):
                    bad.append(f"/{conf.relative_to(root)}: blacklist {module}")
    if bad:
        report.add(
            "FAIL",
            "Kernel graphics",
            "Module blacklists",
            "\n".join(bad),
            "Remove graphics-driver blacklists from the live image.",
        )
    else:
        report.add("PASS", "Kernel graphics", "Module blacklists", "No VM graphics-driver blacklist found.")


def check_symlinks(report: Report, root: Path) -> None:
    bases = [
        root / "etc/systemd/system",
        root / "usr/bin",
        root / "usr/lib",
        root / "usr/share/wayland-sessions",
        root / "usr/share/xsessions",
    ]
    broken: list[str] = []
    scanned = 0
    for base in bases:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_symlink():
                continue
            scanned += 1
            guest = "/" + str(path.relative_to(root))
            if not symlink_resolves(root, guest):
                broken.append(f"{guest} -> {os.readlink(path)}")
                if len(broken) >= 100:
                    break
    report.add(
        "FAIL" if broken else "PASS",
        "Symlinks",
        "Broken graphics/system symlinks",
        "\n".join(broken) if broken else f"No broken links among {scanned} inspected symlinks.",
        "Repair dangling links or stage their targets." if broken else "",
    )


def chroot_ldd(root: Path, guest: str) -> tuple[int, str]:
    proc = run(["sudo", "chroot", str(root), "/usr/bin/ldd", guest], timeout=180)
    return proc.returncode, proc.stdout


def check_ldd(report: Report, root: Path, targets: Sequence[str], label: str) -> None:
    clean = 0
    failed = 0
    for guest in sorted(set(targets)):
        path = root_path(root, guest)
        if not path.is_file():
            continue
        rc, output = chroot_ldd(root, guest)
        report.raw("ldd_" + guest.strip("/").replace("/", "_") + ".txt", output)
        missing = sorted(
            set(re.findall(r"^\s*(\S+)\s+=>\s+not found\s*$", output, re.M))
        )
        if missing:
            failed += 1
            report.add(
                "FAIL",
                "Dependency closure",
                guest,
                "Missing:\n" + "\n".join(missing),
                "Stage the packages providing these SONAMEs, run ldconfig in runtime-package-root, and repeat.",
            )
        elif "not a dynamic executable" in output:
            report.add("INFO", "Dependency closure", guest, "Static file or script; ldd not applicable.")
        elif rc != 0:
            report.add("WARN", "Dependency closure", guest, f"ldd returned {rc}:\n{output[-1000:]}")
        else:
            clean += 1
            report.add("PASS", "Dependency closure", guest, "All direct shared libraries resolve.")
    report.add(
        "INFO",
        "Dependency closure",
        label,
        f"Clean objects: {clean}; objects with missing libraries: {failed}.",
    )


def gather_plugins(root: Path) -> list[str]:
    dirs = [
        "/usr/lib/qt6/plugins/platforms",
        "/usr/lib/qt6/plugins/wayland-shell-integration",
        "/usr/lib/qt6/plugins/wayland-graphics-integration-client",
        "/usr/lib/qt6/plugins/wayland-graphics-integration-server",
        "/usr/lib/qt6/qml",
        "/usr/lib/xorg/modules",
        "/usr/lib/libdecor/plugins-1",
        "/usr/lib/dri",
    ]
    found: list[str] = []
    for guest_dir in dirs:
        directory = root_path(root, guest_dir)
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.so"):
            found.append("/" + str(path.relative_to(root)))
    return sorted(set(found))


def check_interpreters(report: Report, root: Path, targets: Sequence[str]) -> None:
    missing: list[str] = []
    for guest in sorted(set(targets)):
        path = root_path(root, guest)
        if not path.is_file():
            continue
        proc = run(["readelf", "-l", str(path)], timeout=60)
        match = re.search(r"Requesting program interpreter:\s*(\S+)\]", proc.stdout)
        if match:
            interp = match.group(1)
            if not exists(root, interp):
                missing.append(f"{guest}: interpreter {interp} is missing")
    report.add(
        "FAIL" if missing else "PASS",
        "ELF",
        "Program interpreters",
        "\n".join(missing) if missing else "All checked ELF interpreters exist in the ISO root.",
    )


def make_archive(out_dir: Path) -> Path:
    return Path(
        shutil.make_archive(
            str(out_dir),
            "gztar",
            root_dir=out_dir.parent,
            base_dir=out_dir.name,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline ISO graphics-stack diagnostic.")
    parser.add_argument("iso", type=Path)
    parser.add_argument("--kernel-config", type=Path, default=None)
    parser.add_argument("--rootfs-path", default="/hardened/rootfs.sfs")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--keep-root", action="store_true")
    args = parser.parse_args()

    iso = args.iso.expanduser().resolve()
    kernel_config = args.kernel_config.expanduser().resolve() if args.kernel_config else None
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = (
        args.output.expanduser().resolve()
        if args.output
        else Path.home() / f"hardened-graphics-diagnostic-{stamp}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    report = Report(out_dir)

    if not iso.is_file():
        report.add("FAIL", "Input", "ISO", f"Not found: {iso}")
        return 2

    require(
        report,
        ["xorriso", "unsquashfs", "sha256sum", "readelf", "file", "sudo", "chroot"],
    )

    print("\nAuthorizing sudo for metadata-preserving extraction and chroot ldd checks...")
    sudo = run(["sudo", "-v"])
    if sudo.returncode != 0:
        report.add("FAIL", "Host", "sudo", sudo.stdout)
        return 2

    metadata = {
        "iso": str(iso),
        "iso_size_bytes": iso.stat().st_size,
        "iso_sha256": sha256(iso),
        "kernel_config": str(kernel_config) if kernel_config else None,
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "offline_limit": (
            "Cannot prove runtime seat0, /dev/dri/card0, selected VirtualBox controller, "
            "loaded kernel modules, firmware success, or actual runtime journal without booting."
        ),
    }

    report.add(
        "PASS",
        "Input",
        "ISO",
        f"{iso}\nSize={iso.stat().st_size} bytes\nSHA-256={metadata['iso_sha256']}",
    )

    work = out_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    rootfs = work / "rootfs.sfs"
    root = work / "root"

    check_bootloader(report, iso, work)

    print("\nExtracting SquashFS from ISO...")
    ok, output = extract_iso_file(iso, args.rootfs_path, rootfs)
    report.raw("xorriso-rootfs-extract.txt", output)
    if not ok:
        report.add(
            "FAIL",
            "ISO structure",
            args.rootfs_path,
            "Could not extract rootfs.sfs.",
            "Confirm the path with: xorriso -indev ISO -ls /hardened",
        )
        report.write(metadata)
        return 2
    report.add(
        "PASS",
        "ISO structure",
        args.rootfs_path,
        f"Extracted {rootfs.stat().st_size} bytes; SHA-256={sha256(rootfs)}",
    )

    print("\nExtracting complete root filesystem. This is the slow part...")
    extracted = run(
        ["sudo", "unsquashfs", "-f", "-d", str(root), str(rootfs)],
        timeout=3600,
    )
    report.raw("unsquashfs.txt", extracted.stdout)
    if extracted.returncode != 0:
        report.add(
            "FAIL",
            "ISO structure",
            "SquashFS extraction",
            f"unsquashfs returned {extracted.returncode}:\n{extracted.stdout[-1500:]}",
        )
        report.write(metadata)
        return 2
    report.add("PASS", "ISO structure", "SquashFS extraction", "Complete root extracted.")

    check_usr_merge(report, root)
    check_systemd(report, root)
    targets = check_binaries(report, root)
    check_sessions(report, root)
    check_sddm(report, root)
    check_accounts_pam(report, root)
    plugin_targets = check_libraries_plugins(report, root)
    xorg_targets = check_xorg(report, root)
    check_kernel(report, root, kernel_config)
    check_modprobe_blacklists(report, root)
    check_symlinks(report, root)

    core_targets = sorted(set(targets + plugin_targets + xorg_targets))
    check_interpreters(report, root, core_targets)
    check_ldd(report, root, core_targets, "Core binaries and modules")

    plugins = gather_plugins(root)
    if len(plugins) > 500:
        report.add(
            "INFO",
            "Dependency closure",
            "Plugin scan size",
            f"Found {len(plugins)} plugin objects; checking the first 500.",
        )
        plugins = plugins[:500]
    else:
        report.add(
            "INFO",
            "Dependency closure",
            "Plugin scan size",
            f"Checking {len(plugins)} plugin objects.",
        )
    check_ldd(report, root, plugins, "Qt/Wayland/Xorg/Mesa plugin scan")

    report.add(
        "INFO",
        "Runtime-only checks",
        "Not provable offline",
        "\n".join(
            [
                "loginctl seat-status seat0",
                "presence and permissions of /dev/dri/card0 and /dev/dri/renderD128",
                "lspci -nnk and the selected VirtualBox graphics controller",
                "which DRM module actually loaded",
                "runtime dmesg DRM/KMS errors",
                "SDDM/GDM/KWin journal output",
                "EGL/GBM initialization against the live virtual GPU",
            ]
        ),
        "After structural failures are zero, boot once and collect these runtime facts.",
    )

    txt, jsn = report.write(metadata)

    if not args.keep_root:
        print("\nRemoving extracted root to reclaim space...")
        run(["sudo", "rm", "-rf", str(root)], timeout=1800)

    archive = make_archive(out_dir)
    counts = report.counts()

    print("\n" + "=" * 78)
    print("DIAGNOSTIC COMPLETE")
    print("=" * 78)
    print(
        f"FAIL={counts['FAIL']} WARN={counts['WARN']} "
        f"PASS={counts['PASS']} INFO={counts['INFO']}"
    )
    print(f"Text report:   {txt}")
    print(f"JSON report:   {jsn}")
    print(f"Upload archive:{archive}")
    print()
    print("Start with every [FAIL] line at the top of graphics-diagnostic.txt.")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
