#!/usr/bin/env python3
"""Persist the PAM login repair in rootfs.ext2 and rebuild the ISO."""

from __future__ import annotations
import os, shutil, subprocess, sys, tempfile
from datetime import datetime
from pathlib import Path

HOME = Path("/home/corbett")
ROOTFS = HOME / "iso-systemd/rootfs.ext2"
BUILDER = HOME / "build_hardened_iso.py"
BACKUPS = HOME / "rootfs-backups"

PAM_FILES = {
"system-auth": """#%PAM-1.0
auth       required     pam_unix.so try_first_pass
account    required     pam_unix.so
password   required     pam_unix.so try_first_pass sha512 shadow
session    required     pam_unix.so
""",
"system-login": """#%PAM-1.0
auth       include      system-auth
account    include      system-auth
password   include      system-auth
session    include      system-auth
""",
"other": """#%PAM-1.0
auth       required     pam_deny.so
account    required     pam_deny.so
password   required     pam_deny.so
session    required     pam_deny.so
""",
"login": """#%PAM-1.0
auth       required     pam_securetty.so
auth       include      system-auth
account    required     pam_nologin.so
account    include      system-auth
password   include      system-auth
session [success=ok ignore=ignore module_unknown=ignore default=bad] pam_selinux.so close
session    include      system-auth
session    required     pam_loginuid.so
session [success=ok ignore=ignore module_unknown=ignore default=bad] pam_selinux.so open
""",
"su": """#%PAM-1.0
auth       sufficient   pam_rootok.so
auth       include      system-auth
account    include      system-auth
session [success=ok ignore=ignore module_unknown=ignore default=bad] pam_selinux.so close
session    include      system-auth
session [success=ok ignore=ignore module_unknown=ignore default=bad] pam_selinux.so open
""",
}

def die(msg):
    print("ERROR:", msg, file=sys.stderr)
    raise SystemExit(1)

def run(args, check=True, capture=False):
    print("+", " ".join(map(str,args)))
    try:
        return subprocess.run([str(x) for x in args], check=check, text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None)
    except subprocess.CalledProcessError as e:
        if capture and e.stdout: print(e.stdout, file=sys.stderr)
        die(f"command failed ({e.returncode})")

def loops():
    r=run(["losetup","--noheadings","-O","NAME","--associated",str(ROOTFS)],False,True)
    return [x.strip() for x in r.stdout.splitlines() if x.strip()]

def clear_loops():
    for loop in loops():
        r=run(["findmnt","-rn","-S",loop,"-o","TARGET"],False,True)
        for target in sorted([x for x in r.stdout.splitlines() if x],key=len,reverse=True):
            run(["umount",target],False)
        run(["losetup","-d",loop],False)
    if loops(): die("rootfs is still attached to a loop device")

def backup(root, rel, out):
    src=root/rel
    if src.is_file():
        dst=out/rel; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst)

def module_exists(root,name):
    for d in ("usr/lib/security","usr/lib64/security","usr/lib/x86_64-linux-gnu/security",
              "lib/security","lib/x86_64-linux-gnu/security"):
        if (root/d/name).exists(): return True
    return False

def main():
    if os.geteuid()!=0: die("run with sudo")
    if subprocess.run(["pgrep","-f","qemu-system-x86_64"],stdout=subprocess.DEVNULL).returncode==0:
        die("close QEMU first")
    for c in ("losetup","findmnt","mount","umount","e2fsck","sync"):
        if not shutil.which(c): die(f"missing host command: {c}")
    if not ROOTFS.is_file(): die(f"missing {ROOTFS}")
    if not BUILDER.is_file(): die(f"missing {BUILDER}")

    clear_loops()
    run(["e2fsck","-f","-y",str(ROOTFS)])
    loop=run(["losetup","--find","--show",str(ROOTFS)],capture=True).stdout.strip()
    mnt=Path(tempfile.mkdtemp(prefix="pam-fix-",dir="/mnt"))
    out=BACKUPS/("pam-"+datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True,exist_ok=True)
    mounted=False
    try:
        run(["mount","-t","ext2","-o","rw",loop,str(mnt)]); mounted=True
        pam=mnt/"etc/pam.d"; pam.mkdir(parents=True,exist_ok=True)
        for name,text in PAM_FILES.items():
            backup(mnt,Path("etc/pam.d")/name,out)
            (pam/name).write_text(text,encoding="utf-8",newline="\n")
            (pam/name).chmod(0o644)

        for f in pam.iterdir():
            if f.is_file():
                text=f.read_text(encoding="utf-8",errors="replace")
                if "pam_console.so" in text:
                    backup(mnt,f.relative_to(mnt),out)
                    f.write_text("\n".join(x for x in text.splitlines()
                        if "pam_console.so" not in x)+"\n",encoding="utf-8")

        needed=("pam_unix.so","pam_deny.so","pam_securetty.so","pam_nologin.so",
                "pam_loginuid.so","pam_rootok.so","pam_selinux.so")
        missing=[x for x in needed if not module_exists(mnt,x)]
        if missing: die("missing PAM modules: "+", ".join(missing))

        for name in PAM_FILES:
            if not (pam/name).is_file(): die(f"missing /etc/pam.d/{name}")
        login=(pam/"login").read_text()
        if "pam_console.so" in login or "pam_selinux.so open" not in login:
            die("PAM verification failed")

        run(["sync"])
        print("PAM verification: PASS")
    finally:
        if mounted: run(["umount",str(mnt)],False)
        run(["losetup","-d",loop],False)
        try: mnt.rmdir()
        except OSError: pass

    run(["e2fsck","-f","-y",str(ROOTFS)])
    run(["e2fsck","-fn",str(ROOTFS)])
    run([sys.executable,str(BUILDER)])
    print("=== SUCCESS ===")
    print("Persistent PAM login repair installed.")
    print("Backups:",out)

if __name__=="__main__":
    main()
