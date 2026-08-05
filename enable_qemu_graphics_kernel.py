#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HOME = Path('/home/corbett')
KERNEL = HOME / 'linux-7.1.2'
CONFIG_TOOL = KERNEL / 'scripts/config'
ROOTFS = HOME / 'iso-systemd/rootfs.ext2'
ISO_ROOT = HOME / 'iso-systemd'
EFI_IMAGE = ISO_ROOT / 'efiboot.img'
KERNEL_DEST = ISO_ROOT / 'boot/vmlinuz-7.1.2'
ISO_BUILDER = HOME / 'build_hardened_iso.py'

OPTIONS = (
    'DRM','DRM_KMS_HELPER','DRM_FBDEV_EMULATION','DRM_SIMPLEDRM',
    'DRM_BOCHS','DRM_VIRTIO_GPU','VIRTIO','VIRTIO_PCI',
    'VIRTIO_PCI_LEGACY','SYSFB','SYSFB_SIMPLEFB','FB',
    'FRAMEBUFFER_CONSOLE','VT','VT_CONSOLE','FS_POSIX_ACL',
    'EXT4_FS_POSIX_ACL','EXT4_FS_SECURITY','TMPFS_XATTR',
    'TMPFS_POSIX_ACL','BINFMT_MISC',
)


def die(msg: str) -> None:
    print(f'ERROR: {msg}', file=sys.stderr)
    raise SystemExit(1)


def run(args, check=True, capture=False, cwd=None):
    print('+', ' '.join(map(str, args)))
    try:
        return subprocess.run(
            [str(x) for x in args], check=check, text=True,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
        )
    except subprocess.CalledProcessError as exc:
        if capture and exc.stdout:
            print(exc.stdout, file=sys.stderr)
        die(f'command failed ({exc.returncode}): {" ".join(map(str, args))}')


def qemu_closed() -> None:
    if shutil.which('pgrep'):
        r = subprocess.run(['pgrep','-f','qemu-system-x86_64'], text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if r.returncode == 0 and r.stdout.strip():
            die('QEMU is still running. Shut it down first.')


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def loops_for(image: Path) -> list[str]:
    r = run(['losetup','--noheadings','--output','NAME','--associated',str(image)],
            check=False, capture=True)
    return sorted({x.strip() for x in r.stdout.splitlines() if x.strip().startswith('/dev/loop')})


def clear_loops(image: Path) -> None:
    for loop in loops_for(image):
        r = run(['findmnt','-rn','-S',loop,'-o','TARGET'], check=False, capture=True)
        for target in sorted({x.strip() for x in r.stdout.splitlines() if x.strip()}, key=len, reverse=True):
            u = run(['umount',target], check=False, capture=True)
            if u.returncode:
                die(f'could not unmount {target}:\n{u.stdout}')
        run(['losetup','-d',loop], check=False)


def configure_kernel() -> None:
    config = KERNEL / '.config'
    if not config.is_file() or not CONFIG_TOOL.is_file():
        die('kernel .config or scripts/config is missing')
    backup = config.with_name('.config.bak-qemu-graphics-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(config, backup)
    print(f'Backed up kernel config: {backup}')
    CONFIG_TOOL.chmod(CONFIG_TOOL.stat().st_mode | 0o111)
    for option in OPTIONS:
        run([str(CONFIG_TOOL),'--enable',option], cwd=KERNEL)
    run(['make','olddefconfig'], cwd=KERNEL)
    text = config.read_text(encoding='utf-8', errors='replace')
    missing = [o for o in OPTIONS if f'CONFIG_{o}=y' not in text]
    if missing:
        die('options not built-in after olddefconfig: ' + ', '.join(missing))
    print('Verified DRM, VirtIO, framebuffer, binfmt, and ACL options.')


def build_kernel() -> None:
    jobs = max(1, min(os.cpu_count() or 2, 4))
    run(['make',f'-j{jobs}','bzImage','modules'], cwd=KERNEL)
    bz = KERNEL / 'arch/x86/boot/bzImage'
    if not bz.is_file() or bz.stat().st_size < 5 * 1024 * 1024:
        die('rebuilt bzImage is missing or unexpectedly small')
    KERNEL_DEST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bz, KERNEL_DEST)
    KERNEL_DEST.chmod(0o644)
    print(f'Installed kernel to {KERNEL_DEST}')


def install_modules() -> None:
    clear_loops(ROOTFS)
    run(['e2fsck','-f','-y',str(ROOTFS)])
    loop = run(['losetup','--find','--show',str(ROOTFS)], capture=True).stdout.strip()
    mountpoint = Path(tempfile.mkdtemp(prefix='hardened-kernel-modules-', dir='/mnt'))
    mounted = False
    try:
        run(['mount','-t','ext2','-o','rw',loop,str(mountpoint)])
        mounted = True
        run(['make','modules_install',f'INSTALL_MOD_PATH={mountpoint}'], cwd=KERNEL)
        boot = mountpoint / 'boot'
        boot.mkdir(parents=True, exist_ok=True)
        shutil.copy2(KERNEL_DEST, boot / 'vmlinuz-7.1.2')
        if shutil.which('depmod'):
            run(['depmod','-b',str(mountpoint),'7.1.2'])
        run(['sync'])
    finally:
        if mounted:
            run(['umount',str(mountpoint)], check=False)
        run(['losetup','-d',loop], check=False)
        try:
            mountpoint.rmdir()
        except OSError:
            pass
    run(['e2fsck','-f','-y',str(ROOTFS)])
    run(['e2fsck','-fn',str(ROOTFS)])


def sync_efi() -> None:
    backup = EFI_IMAGE.with_name(EFI_IMAGE.name + '.bak-qemu-graphics-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(EFI_IMAGE, backup)
    mountpoint = Path(tempfile.mkdtemp(prefix='efi-qemu-graphics-', dir='/mnt'))
    mounted = False
    try:
        r = run(['mount','-o','loop,rw,sync',str(EFI_IMAGE),str(mountpoint)], check=False, capture=True)
        if r.returncode:
            die(f'could not mount efiboot.img:\n{r.stdout}')
        mounted = True
        dest = mountpoint / 'boot/vmlinuz-7.1.2'
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(KERNEL_DEST, dest)
        os.sync()
        if sha256(KERNEL_DEST) != sha256(dest):
            die('EFI kernel hash verification failed')
    finally:
        if mounted:
            r = run(['umount',str(mountpoint)], check=False, capture=True)
            if r.returncode:
                die(f'could not unmount efiboot.img:\n{r.stdout}')
        try:
            mountpoint.rmdir()
        except OSError:
            pass
    print('EFI kernel synchronized and verified.')


def main() -> None:
    if os.geteuid() != 0:
        die('run with sudo')
    qemu_closed()
    for cmd in ('make','losetup','findmnt','mount','umount','e2fsck','sync'):
        if not shutil.which(cmd):
            die(f'missing host command: {cmd}')
    for path in (KERNEL,ROOTFS,EFI_IMAGE,ISO_BUILDER):
        if not path.exists():
            die(f'missing required path: {path}')
    configure_kernel()
    build_kernel()
    install_modules()
    sync_efi()
    run([sys.executable, str(ISO_BUILDER)])
    print('\n=== SUCCESS ===')
    print('Kernel 7.1.2 now has built-in QEMU DRM graphics and POSIX ACL support.')
    print('Modules, EFI image, and outer ISO were rebuilt.')


if __name__ == '__main__':
    main()
