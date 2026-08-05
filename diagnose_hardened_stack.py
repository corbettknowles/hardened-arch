#!/usr/bin/env python3
"""
Read-only audit for Corbett's Hardened Arch live image.

Run after QEMU is completely powered off:
    sudo python3 /home/corbett/diagnose_hardened_stack.py

Outputs:
    /home/corbett/hardened-stack-diagnostics.txt
    /home/corbett/hardened-stack-diagnostics.json

The script does not change rootfs.ext2, efiboot.img, the ISO, or boot entries.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HOME = Path('/home/corbett')
ROOTFS = HOME / 'iso-systemd/rootfs.ext2'
ISO_ROOT = HOME / 'iso-systemd'
ISO_IMAGE = HOME / 'hardened-arch-systemdboot-clean.iso'
KERNEL_TREE = HOME / 'linux-7.1.2'
KERNEL_CONFIG = KERNEL_TREE / '.config'
KERNEL_IMAGE = KERNEL_TREE / 'arch/x86/boot/bzImage'
INITRAMFS = ISO_ROOT / 'boot/initramfs-7.1.2.cpio.gz'
TEXT_REPORT = HOME / 'hardened-stack-diagnostics.txt'
JSON_REPORT = HOME / 'hardened-stack-diagnostics.json'

LINES: list[str] = []
FINDINGS: list[dict[str, str]] = []
DATA: dict[str, Any] = {
    'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    'findings': FINDINGS,
}


def out(text: str = '') -> None:
    print(text)
    LINES.append(text)


def section(title: str) -> None:
    out()
    out('=' * 78)
    out(title)
    out('=' * 78)


def finding(level: str, component: str, summary: str, evidence: str = '', action: str = '') -> None:
    item = {
        'severity': level,
        'component': component,
        'summary': summary,
        'evidence': evidence,
        'action': action,
    }
    FINDINGS.append(item)
    out(f'[{level}] {component}: {summary}')
    if evidence:
        out(f'  Evidence: {evidence}')
    if action:
        out(f'  Action: {action}')


def run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(x) for x in args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args, 124, (exc.stdout or '') + '\n[TIMEOUT]')
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, f'command not found: {args[0]}')


def exists_cmd(name: str) -> bool:
    return shutil.which(name) is not None


def read_text(path: Path, max_bytes: int = 2_000_000) -> str:
    try:
        if path.stat().st_size > max_bytes:
            return ''
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def target(root: Path, absolute: str) -> Path:
    return root / absolute.lstrip('/')


def mode_owner(path: Path) -> str:
    try:
        st = path.lstat()
        extra = f' -> {os.readlink(path)}' if path.is_symlink() else ''
        return f'{stat.filemode(st.st_mode)} uid={st.st_uid} gid={st.st_gid}{extra}'
    except OSError as exc:
        return f'ERROR: {exc}'


def require_root_and_idle() -> None:
    if os.geteuid() != 0:
        raise SystemExit('ERROR: Run with sudo.')
    if exists_cmd('pgrep'):
        qemu = run(['pgrep', '-af', 'qemu-system'])
        active = [line for line in qemu.stdout.splitlines() if 'diagnose_hardened_stack.py' not in line]
        if qemu.returncode == 0 and active:
            out('QEMU is still running:')
            for line in active:
                out(f'  {line}')
            raise SystemExit('ERROR: Power off QEMU first.')


def parse_kconfig() -> dict[str, str]:
    cfg: dict[str, str] = {}
    for line in read_text(KERNEL_CONFIG, 20_000_000).splitlines():
        if line.startswith('CONFIG_') and '=' in line:
            key, value = line.split('=', 1)
            cfg[key] = value
        elif line.startswith('# CONFIG_') and line.endswith(' is not set'):
            cfg[line[2:].split(' ', 1)[0]] = 'n'
    return cfg


def parse_passwd(root: Path) -> dict[str, dict[str, Any]]:
    users: dict[str, dict[str, Any]] = {}
    for line in read_text(root / 'etc/passwd').splitlines():
        if not line or line.startswith('#'):
            continue
        f = line.split(':')
        if len(f) < 7:
            continue
        try:
            users[f[0]] = {
                'uid': int(f[2]), 'gid': int(f[3]), 'gecos': f[4],
                'home': f[5], 'shell': f[6],
            }
        except ValueError:
            pass
    return users


def parse_group(root: Path) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for line in read_text(root / 'etc/group').splitlines():
        if not line or line.startswith('#'):
            continue
        f = line.split(':')
        if len(f) < 4:
            continue
        try:
            groups[f[0]] = {'gid': int(f[2]), 'members': [x for x in f[3].split(',') if x]}
        except ValueError:
            pass
    return groups


def ldd_in_chroot(root: Path, absolute: str) -> dict[str, Any]:
    result = run(['chroot', str(root), '/usr/bin/ldd', absolute])
    missing = re.findall(r'^\s*(\S+)\s+=>\s+not found\s*$', result.stdout, re.MULTILINE)
    return {'returncode': result.returncode, 'output': result.stdout, 'missing': missing}


def inspect_binary(root: Path, absolute: str) -> dict[str, Any]:
    path = target(root, absolute)
    info: dict[str, Any] = {'exists': path.exists()}
    out(f'\n{absolute}')
    out(f'  exists: {path.exists()}')
    if not path.exists():
        return info
    out(f'  {mode_owner(path)}')
    if path.is_file():
        info['size'] = path.stat().st_size
        info['sha256'] = sha256(path)
        out(f"  size={info['size']} sha256={info['sha256']}")
    if exists_cmd('file'):
        info['file'] = run(['file', '-L', str(path)]).stdout.strip()
        out(f"  file: {info['file']}")
    if exists_cmd('readelf') and path.is_file():
        hdr = run(['readelf', '-h', str(path)]).stdout
        info['elf_header'] = [line.strip() for line in hdr.splitlines() if any(k in line for k in ('Class:', 'Type:', 'Machine:', 'Entry point'))]
        for line in info['elf_header']:
            out(f'  {line}')
        dyn = run(['readelf', '-d', str(path)]).stdout
        info['dynamic'] = [line.strip() for line in dyn.splitlines() if any(k in line for k in ('RPATH', 'RUNPATH', 'NEEDED'))]
        for line in info['dynamic']:
            out(f'  {line}')
    if exists_cmd('strings') and path.is_file():
        strings = run(['strings', '-a', str(path)], 180).stdout.splitlines()
        interesting = sorted({
            line for line in strings
            if any(k in line for k in ('/home/corbett', 'xorg-build', 'kde-build', 'custom.conf', 'defaults.conf', 'runtime.conf'))
            and len(line) < 500
        })[:50]
        info['interesting_strings'] = interesting
        for line in interesting:
            out(f'  string: {line}')
    if target(root, '/usr/bin/ldd').exists() and path.is_file():
        info['ldd'] = ldd_in_chroot(root, absolute)
        out('  ldd:')
        for line in info['ldd']['output'].splitlines():
            out(f'    {line}')
        if info['ldd']['missing']:
            finding('CRITICAL', absolute, 'Missing runtime libraries.', ', '.join(info['ldd']['missing']), 'Stage or rebuild against the target runtime.')
    return info


def inspect_unit(root: Path, name: str) -> dict[str, Any]:
    candidates = [
        target(root, f'/etc/systemd/system/{name}'),
        target(root, f'/usr/lib/systemd/system/{name}'),
        target(root, f'/lib/systemd/system/{name}'),
    ]
    path = next((p for p in candidates if p.exists()), None)
    info: dict[str, Any] = {'exists': bool(path)}
    out(f'\n{name}: {"PRESENT" if path else "MISSING"}')
    if not path:
        return info
    info['path'] = '/' + str(path.relative_to(root))
    info['text'] = read_text(path)
    out(f"  {info['path']} {mode_owner(path)}")
    for line in info['text'].splitlines():
        if line.startswith(('Type=', 'ExecStart=', 'User=', 'Group=', 'Requires=', 'After=')):
            out(f'  {line}')
    dropins = []
    for base in (target(root, f'/etc/systemd/system/{name}.d'), target(root, f'/usr/lib/systemd/system/{name}.d')):
        if base.is_dir():
            for item in sorted(base.glob('*.conf')):
                text = read_text(item)
                dropins.append({'path': '/' + str(item.relative_to(root)), 'text': text})
                out(f'  drop-in: /{item.relative_to(root)}')
                for line in text.splitlines():
                    out(f'    {line}')
    info['dropins'] = dropins
    return info


def inspect_initramfs() -> dict[str, Any]:
    info: dict[str, Any] = {'exists': INITRAMFS.is_file()}
    if not INITRAMFS.is_file() or not (exists_cmd('gzip') and exists_cmd('cpio')):
        out('Initramfs missing or gzip/cpio unavailable.')
        return info
    temp = Path(tempfile.mkdtemp(prefix='hardened-initramfs-'))
    try:
        cmd = f"cd {temp!s} && gzip -dc {INITRAMFS!s} | cpio -idmu --quiet ./init 2>&1"
        result = run(['bash', '-lc', cmd])
        init = temp / 'init'
        if not init.is_file():
            out(result.stdout.strip())
            return info
        text = read_text(init)
        info['init'] = text
        relevant = []
        for n, line in enumerate(text.splitlines(), 1):
            if any(k in line for k in ('overlay', 'tmpfs', 'rootfs.ext2', 'switch_root', 'upperdir', 'workdir', 'selinux')):
                relevant.append(f'{n}: {line}')
        info['relevant_lines'] = relevant
        for line in relevant:
            out(f'  {line}')
        if 'overlay' in text:
            finding('INFO', 'Live root', 'Normal ISO boot uses OverlayFS.', 'The overlay mount itself can show an unknown root label even when lower-root files are labeled.', 'Verify runtime mount options, tmpfs xattrs, and process contexts before enforcing.')
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return info


def inspect_selinux(root: Path, users: dict[str, dict[str, Any]]) -> dict[str, Any]:
    info: dict[str, Any] = {}
    config = target(root, '/etc/selinux/config')
    out(f'/etc/selinux/config: {mode_owner(config)}')
    out(read_text(config).strip())

    base = target(root, '/etc/selinux/refpolicy-arch')
    policies = sorted((base / 'policy').glob('policy.*')) if (base / 'policy').is_dir() else []
    info['policies'] = ['/' + str(p.relative_to(root)) for p in policies]
    out('Compiled policies:')
    for p in policies:
        out(f'  /{p.relative_to(root)} {mode_owner(p)}')
    if not policies:
        finding('CRITICAL', 'SELinux', 'Compiled policy.N is missing.', '', 'Rebuild the policy store.')

    expected = [
        'contexts/dbus_contexts',
        'contexts/default_contexts',
        'contexts/default_type',
        'contexts/failsafe_context',
        'contexts/removable_context',
        'contexts/securetty_types',
        'contexts/files/file_contexts',
        'seusers',
        'contexts/users/root',
    ]
    info['appconfig'] = {}
    out('Policy appconfig files:')
    for relname in expected:
        path = base / relname
        info['appconfig'][relname] = {'exists': path.is_file(), 'mode': mode_owner(path) if path.exists() else ''}
        out(f'  {"PRESENT" if path.is_file() else "MISSING"} /etc/selinux/refpolicy-arch/{relname}' + (f' {mode_owner(path)}' if path.exists() else ''))

    dbus_ctx = base / 'contexts/dbus_contexts'
    if not dbus_ctx.is_file():
        finding('CRITICAL', 'D-Bus/SELinux', 'dbus_contexts is missing.', str(dbus_ctx), 'Install the complete refpolicy appconfig set and relabel.')
    if not (base / 'contexts/users/root').is_file():
        finding('HIGH', 'PAM/SELinux login', 'Root SELinux user-context mapping is missing.', 'Boot reported: A valid context for root could not be obtained.', 'Restore user mappings before enforcing mode.')

    if dbus_ctx.is_file() and 'messagebus' in users:
        st = dbus_ctx.stat()
        readable = bool(st.st_mode & stat.S_IROTH) or st.st_uid == users['messagebus']['uid'] or st.st_gid == users['messagebus']['gid'] and bool(st.st_mode & stat.S_IRGRP)
        info['messagebus_mode_readable'] = readable
        out(f'messagebus final-file mode read check: {readable}')
        if not readable:
            finding('CRITICAL', 'D-Bus/SELinux', 'messagebus cannot read dbus_contexts by mode.', mode_owner(dbus_ctx), 'Correct ownership and mode, then relabel.')

    if exists_cmd('getfattr'):
        info['xattrs'] = {}
        out('security.selinux xattrs visible from host:')
        for absolute in ('/', '/etc', '/usr', '/usr/bin/dbus-daemon', '/usr/sbin/gdm', '/usr/lib/accounts-daemon', '/usr/lib/polkit-1/polkitd', '/etc/selinux/refpolicy-arch/contexts/dbus_contexts'):
            path = target(root, absolute)
            if not path.exists():
                continue
            result = run(['getfattr', '-n', 'security.selinux', '--only-values', str(path)])
            info['xattrs'][absolute] = {'rc': result.returncode, 'value': result.stdout.strip()}
            out(f'  {absolute}: rc={result.returncode} {result.stdout.strip() or "(not readable from host)"}')
    return info


def inspect_abi(root: Path) -> dict[str, Any]:
    info: dict[str, Any] = {}
    accounts = target(root, '/usr/lib/accounts-daemon')
    glib_candidates = [
        target(root, '/usr/lib/libglib-2.0.so.0'),
        target(root, '/lib/x86_64-linux-gnu/libglib-2.0.so.0'),
        target(root, '/usr/lib/x86_64-linux-gnu/libglib-2.0.so.0'),
    ]
    glib = next((p for p in glib_candidates if p.exists()), None)
    requires = False
    provides = False
    if accounts.exists() and exists_cmd('readelf'):
        requires = 'g_variant_builder_init_static' in run(['readelf', '-Ws', str(accounts)]).stdout
    if glib and exists_cmd('nm'):
        provides = ' g_variant_builder_init_static' in run(['nm', '-D', str(glib)]).stdout
    info['accounts_requires_g_variant_builder_init_static'] = requires
    info['glib_path'] = '/' + str(glib.relative_to(root)) if glib else None
    info['glib_provides_g_variant_builder_init_static'] = provides
    out(f'accounts-daemon requires g_variant_builder_init_static: {requires}')
    out(f'target GLib provides g_variant_builder_init_static: {provides}')
    if requires and not provides:
        finding('CRITICAL', 'AccountsService/GLib ABI', 'accounts-daemon requires a symbol absent from the target GLib.', 'g_variant_builder_init_static', 'Rebuild AccountsService against the target GLib or use a matching older AccountsService build.')
    for pc in (
        target(root, '/usr/lib/pkgconfig/glib-2.0.pc'),
        target(root, '/usr/lib/x86_64-linux-gnu/pkgconfig/glib-2.0.pc'),
        target(root, '/lib/x86_64-linux-gnu/pkgconfig/glib-2.0.pc'),
    ):
        if pc.is_file():
            m = re.search(r'^Version:\s*(.+)$', read_text(pc), re.MULTILINE)
            if m:
                info['glib_version'] = m.group(1).strip()
                out(f"Target GLib version: {info['glib_version']}")
            break
    return info


def scan_host_paths(root: Path) -> list[dict[str, str]]:
    roots = [root / 'etc', root / 'usr/lib/systemd', root / 'usr/lib/udev', root / 'usr/share/dbus-1', root / 'usr/share/xsessions', root / 'usr/share/wayland-sessions']
    markers = ('/home/corbett', 'xorg-build', 'kde-build', 'rootfs-stage')
    hits: list[dict[str, str]] = []
    for base in roots:
        if not base.exists():
            continue
        for path in base.rglob('*'):
            try:
                if not path.is_file() or path.stat().st_size > 1_000_000:
                    continue
                for n, line in enumerate(read_text(path, 1_000_000).splitlines(), 1):
                    if any(m in line for m in markers):
                        hits.append({'path': '/' + str(path.relative_to(root)), 'line': str(n), 'text': line[:500]})
            except OSError:
                pass
    return hits


def main() -> None:
    require_root_and_idle()

    section('HARDENED ARCH STACK DIAGNOSTIC — READ ONLY')
    out(f"Generated: {DATA['generated_at']}")
    out(f'Host kernel: {os.uname().release}')
    for path in (ROOTFS, ISO_ROOT, ISO_IMAGE, KERNEL_CONFIG, KERNEL_IMAGE, INITRAMFS):
        out(f'{path}: {"PRESENT" if path.exists() else "MISSING"}')

    if not ROOTFS.is_file():
        raise SystemExit(f'ERROR: Missing {ROOTFS}')

    section('HOST TOOL AVAILABILITY')
    tools = ('e2fsck', 'tune2fs', 'losetup', 'mount', 'umount', 'findmnt', 'file', 'readelf', 'strings', 'nm', 'getfattr', 'gzip', 'cpio', 'chroot')
    DATA['host_tools'] = {tool: exists_cmd(tool) for tool in tools}
    for tool, present in DATA['host_tools'].items():
        out(f'{tool}: {present}')

    section('FILESYSTEM HEALTH AND FEATURES')
    if exists_cmd('e2fsck'):
        fsck = run(['e2fsck', '-f', '-n', str(ROOTFS)], 300)
        DATA['e2fsck'] = {'returncode': fsck.returncode, 'output': fsck.stdout}
        out(fsck.stdout.rstrip())
        if fsck.returncode not in (0, 1):
            finding('CRITICAL', 'rootfs.ext2', 'Read-only filesystem check reported a serious condition.', f'e2fsck rc={fsck.returncode}', 'Repair the image before further changes.')

    features = ''
    if exists_cmd('tune2fs'):
        tune = run(['tune2fs', '-l', str(ROOTFS)])
        DATA['tune2fs'] = tune.stdout
        for line in tune.stdout.splitlines():
            if line.startswith(('Filesystem volume name:', 'Filesystem features:', 'Filesystem state:', 'Block count:', 'Free blocks:', 'Inode count:', 'Free inodes:')):
                out(line)
            if line.startswith('Filesystem features:'):
                features = line.split(':', 1)[1].strip()
    if 'ext_attr' not in features.split():
        finding('CRITICAL', 'Filesystem xattrs', 'ext_attr is absent.', features, 'Enable extended attributes before SELinux labeling.')

    section('KERNEL CONFIGURATION')
    cfg = parse_kconfig()
    DATA['kernel_config'] = cfg
    required = {
        'CONFIG_SECURITY': 'y', 'CONFIG_SECURITY_SELINUX': 'y',
        'CONFIG_EXT4_FS': 'y', 'CONFIG_EXT4_USE_FOR_EXT2': 'y',
        'CONFIG_EXT4_FS_SECURITY': 'y', 'CONFIG_FS_POSIX_ACL': 'y',
        'CONFIG_EXT4_FS_POSIX_ACL': 'y', 'CONFIG_TMPFS': 'y',
        'CONFIG_TMPFS_XATTR': 'y', 'CONFIG_TMPFS_POSIX_ACL': 'y',
        'CONFIG_OVERLAY_FS': 'y', 'CONFIG_DEVTMPFS': 'y',
        'CONFIG_VIRTIO': 'y', 'CONFIG_VIRTIO_PCI': 'y',
        'CONFIG_VIRTIO_BLK': 'y', 'CONFIG_DRM': 'y',
        'CONFIG_DRM_VIRTIO_GPU': 'y', 'CONFIG_VT': 'y',
        'CONFIG_VT_CONSOLE': 'y',
    }
    for key, expected in required.items():
        actual = cfg.get(key, 'MISSING')
        out(f'{key}={actual} expected={expected}')
        if actual != expected:
            finding('HIGH', 'Kernel config', f'{key} is not built in.', f'actual={actual}', 'Enable it as =y before final validation.')

    section('INITRAMFS AND LIVE OVERLAY FLOW')
    DATA['initramfs'] = inspect_initramfs()

    section('READ-ONLY ROOTFS MOUNT')
    stale = run(['losetup', '--noheadings', '--output', 'NAME', '--associated', str(ROOTFS)])
    stale_loops = [x.strip() for x in stale.stdout.splitlines() if x.strip()]
    DATA['preexisting_loops'] = stale_loops
    if stale_loops:
        finding('HIGH', 'Loop devices', 'rootfs.ext2 already has loop associations.', ', '.join(stale_loops), 'Detach stale loops before any write operation.')

    loop = run(['losetup', '--find', '--show', '--read-only', str(ROOTFS)]).stdout.strip()
    if not loop:
        raise SystemExit('ERROR: Could not create read-only loop device.')
    mountpoint = Path(tempfile.mkdtemp(prefix='hardened-diagnostic-', dir='/mnt'))
    mounted = False
    try:
        m = run(['mount', '-t', 'ext2', '-o', 'ro', loop, str(mountpoint)])
        if m.returncode != 0:
            out(m.stdout)
            raise SystemExit('ERROR: Could not mount rootfs read-only.')
        mounted = True
        out(f'Mounted {loop} at {mountpoint} read-only')

        users = parse_passwd(mountpoint)
        groups = parse_group(mountpoint)
        DATA['users'] = users
        DATA['groups'] = groups

        section('SERVICE ACCOUNTS')
        for name in ('root', 'messagebus', 'gdm', 'polkitd', 'nobody'):
            out(f'{name}: {users.get(name, "MISSING")}')
            if name in ('messagebus', 'gdm', 'polkitd') and name not in users:
                finding('CRITICAL', 'Service accounts', f'{name} is missing.', '', 'Create it through the package sysusers definition.')
        uid_map: dict[int, list[str]] = {}
        for name, info in users.items():
            uid_map.setdefault(info['uid'], []).append(name)
        duplicates = {uid: names for uid, names in uid_map.items() if len(names) > 1 and uid not in (0, 65534)}
        DATA['duplicate_uids'] = duplicates
        for uid, names in duplicates.items():
            finding('HIGH', 'Service accounts', f'UID {uid} is shared.', ', '.join(names), 'Resolve UID collisions.')

        section('SELINUX POLICY, APPCONFIG, AND LABELS')
        DATA['selinux'] = inspect_selinux(mountpoint, users)

        section('PAM STACK')
        DATA['pam'] = {}
        for absolute in ('/etc/pam.d/system-auth', '/etc/pam.d/system-login', '/etc/pam.d/other', '/etc/pam.d/login', '/etc/pam.d/su', '/etc/pam.d/gdm-password', '/etc/pam.d/gdm-launch-environment'):
            path = target(mountpoint, absolute)
            text = read_text(path)
            DATA['pam'][absolute] = {'exists': path.is_file(), 'pam_selinux': 'pam_selinux.so' in text, 'pam_console': 'pam_console.so' in text}
            out(f'{absolute}: exists={path.is_file()} pam_selinux={"pam_selinux.so" in text} pam_console={"pam_console.so" in text}')

        section('SYSTEMD UNITS AND DROP-INS')
        DATA['units'] = {name: inspect_unit(mountpoint, name) for name in ('dbus.service', 'dbus.socket', 'systemd-logind.service', 'accounts-daemon.service', 'polkit.service', 'gdm.service', 'display-manager.service', 'sddm.service')}

        section('ELF INTEGRITY, RPATHS, AND DEPENDENCIES')
        binaries = ('/usr/bin/dbus-daemon', '/usr/sbin/gdm', '/usr/lib/accounts-daemon', '/usr/lib/polkit-1/polkitd', '/usr/lib/systemd/systemd-logind', '/usr/bin/Xorg', '/usr/lib/Xorg', '/usr/bin/startplasma-x11', '/usr/bin/sddm')
        DATA['binaries'] = {binary: inspect_binary(mountpoint, binary) for binary in binaries}
        dbus_desc = DATA['binaries'].get('/usr/bin/dbus-daemon', {}).get('file', '')
        if DATA['binaries'].get('/usr/bin/dbus-daemon', {}).get('exists') and 'ELF 64-bit' not in dbus_desc:
            finding('CRITICAL', 'D-Bus binary', 'dbus-daemon is not a normal 64-bit ELF executable.', dbus_desc, 'Restore a known-good binary.')

        section('ACCOUNTSERVICE / GLIB ABI')
        DATA['abi'] = inspect_abi(mountpoint)

        section('GDM CONFIGURATION AND DISPLAY STACK')
        DATA['gdm'] = {'configs': {}, 'sessions': []}
        for absolute in ('/etc/gdm/custom.conf', '/etc/gdm3/custom.conf', '/usr/etc/gdm/custom.conf', '/usr/local/etc/gdm/custom.conf'):
            path = target(mountpoint, absolute)
            text = read_text(path)
            DATA['gdm']['configs'][absolute] = {'exists': path.is_file(), 'text': text}
            out(f'{absolute}: {"PRESENT" if path.is_file() else "MISSING"}')
            for line in text.splitlines():
                out(f'  {line}')
        gdm_policy = target(mountpoint, '/etc/dbus-1/system.d/org.gnome.DisplayManager.conf')
        out(f'GDM D-Bus policy: {"PRESENT" if gdm_policy.is_file() else "MISSING"}')
        for directory in (target(mountpoint, '/usr/share/xsessions'), target(mountpoint, '/usr/share/wayland-sessions')):
            if directory.is_dir():
                for item in sorted(directory.glob('*.desktop')):
                    session = '/' + str(item.relative_to(mountpoint))
                    DATA['gdm']['sessions'].append(session)
                    out(f'  session: {session}')
        if not any('plasma' in s.lower() and 'wayland' not in s.lower() for s in DATA['gdm']['sessions']):
            finding('HIGH', 'Plasma/Xorg session', 'No obvious Plasma X11 session file was found.', ', '.join(DATA['gdm']['sessions']), 'Install the Plasma X11 session file before using SDDM.')
        xorg_dir = target(mountpoint, '/usr/lib/xorg/modules')
        module_count = sum(1 for p in xorg_dir.rglob('*') if p.is_file()) if xorg_dir.is_dir() else 0
        DATA['gdm']['xorg_module_count'] = module_count
        out(f'Xorg module files: {module_count}')
        out(f'SDDM binary present: {target(mountpoint, "/usr/bin/sddm").exists()}')

        section('LIBINPUT UDEV')
        rule = target(mountpoint, '/usr/lib/udev/rules.d/80-libinput-device-groups.rules')
        helper = target(mountpoint, '/usr/lib/udev/libinput-device-group')
        rule_text = read_text(rule)
        DATA['libinput'] = {
            'rule_exists': rule.is_file(),
            'helper_exists': helper.is_file(),
            'helper_executable': helper.is_file() and os.access(helper, os.X_OK),
            'rule_contains_host_path': '/home/corbett' in rule_text,
        }
        out(json.dumps(DATA['libinput'], indent=2))
        if DATA['libinput']['rule_contains_host_path']:
            finding('HIGH', 'libinput udev', 'The rule still contains a host build path.', '', 'Replace it with /usr/lib/udev/libinput-device-group.')
        if DATA['libinput']['rule_exists'] and not DATA['libinput']['helper_executable']:
            finding('HIGH', 'libinput udev', 'The helper is missing or not executable.', str(helper), 'Install the helper into the target.')

        section('D-BUS ACTIVATION FILES')
        DATA['dbus_activation'] = {}
        for absolute in (
            '/usr/share/dbus-1/system-services/org.freedesktop.Accounts.service',
            '/usr/share/dbus-1/system.d/org.freedesktop.Accounts.conf',
            '/usr/share/dbus-1/system-services/org.freedesktop.PolicyKit1.service',
            '/usr/share/dbus-1/system.d/org.freedesktop.PolicyKit1.conf',
            '/etc/dbus-1/system.d/org.gnome.DisplayManager.conf',
        ):
            present = target(mountpoint, absolute).is_file()
            DATA['dbus_activation'][absolute] = present
            out(f'{absolute}: {"PRESENT" if present else "MISSING"}')

        section('HOST BUILD PATH LEAKS')
        leaks = scan_host_paths(mountpoint)
        DATA['host_path_leaks'] = leaks
        if leaks:
            for hit in leaks[:100]:
                out(f"{hit['path']}:{hit['line']}: {hit['text']}")
            finding('HIGH', 'Staging contamination', f'Found {len(leaks)} target references to host build paths.', 'See report section.', 'Patch or rebuild those files.')
        else:
            out('No host build paths found in the inspected text trees.')

        section('BOOT ENTRIES')
        DATA['boot_entries'] = {}
        entries = ISO_ROOT / 'loader/entries'
        if entries.is_dir():
            for path in sorted(entries.glob('*.conf')):
                text = read_text(path)
                DATA['boot_entries'][path.name] = text
                out(f'[{path.name}]')
                for line in text.splitlines():
                    out(f'  {line}')
                lower = path.name.lower()
                if not any(k in lower for k in ('selinux-off', 'direct-shell', 'debug-shell')):
                    options = ' '.join(line for line in text.splitlines() if line.strip().startswith('options '))
                    if 'selinux=1' not in options or 'enforcing=0' not in options:
                        finding('HIGH', 'Boot entry', f'{path.name} is not explicitly permissive.', options, 'Keep selinux=1 enforcing=0 until the stack is validated.')
        else:
            finding('HIGH', 'Boot entries', 'loader/entries is missing.', str(entries), 'Restore boot entries.')

    finally:
        if mounted:
            run(['umount', str(mountpoint)])
        run(['losetup', '--detach', loop])
        shutil.rmtree(mountpoint, ignore_errors=True)

    section('AUTOMATED DIAGNOSIS AND PLAN')
    rank = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'INFO': 3}
    ordered = sorted(FINDINGS, key=lambda x: rank.get(x['severity'], 99))
    if not ordered:
        out('No automated blockers were detected.')
    for n, item in enumerate(ordered, 1):
        out(f"{n}. [{item['severity']}] {item['component']}: {item['summary']}")
        if item['action']:
            out(f"   Action: {item['action']}")

    out()
    out('Recommended repair order:')
    out('  1. Verify filesystem and ELF integrity; preserve a backup image.')
    out('  2. Restore the complete SELinux appconfig set and root login mappings.')
    out('  3. Make D-Bus start by itself before touching GDM.')
    out('  4. Rebuild AccountsService against the target GLib ABI.')
    out('  5. Verify Polkit independently after D-Bus is healthy.')
    out('  6. Test GDM with explicit config paths and working AccountsService.')
    out('  7. If GDM remains unstable, use SDDM with the Plasma X11 session.')
    out('  8. Relabel under the target kernel, rebuild, boot permissive, and collect runtime logs.')
    out('  9. Switch to enforcing only after PAM, D-Bus, logind, Polkit, display manager, and Plasma pass.')

    DATA['findings'] = FINDINGS
    JSON_REPORT.write_text(json.dumps(DATA, indent=2, sort_keys=True), encoding='utf-8')
    TEXT_REPORT.write_text('\n'.join(LINES) + '\n', encoding='utf-8')

    section('REPORT FILES')
    out(str(TEXT_REPORT))
    out(str(JSON_REPORT))
    critical = sum(1 for x in FINDINGS if x['severity'] == 'CRITICAL')
    high = sum(1 for x in FINDINGS if x['severity'] == 'HIGH')
    out(f'Summary: {critical} critical, {high} high-priority findings.')
    TEXT_REPORT.write_text('\n'.join(LINES) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
