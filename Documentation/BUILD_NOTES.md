# Build Notes — Alpha 1.10

## Login / authentication (fixed and verified on real hardware)

Previous builds rejected the correct account password everywhere (SDDM,
getty console, VirtualBox, real hardware) despite the stored password
hash being provably correct. Root cause: the `hardened` account's shell
was set to `/usr/bin/zsh` only at account-creation time via `useradd -s`.
Because the account already existed (inherited from the base build tree),
that step was skipped and the account kept an old `/bin/bash` shell that
was never registered in `/etc/shells`. `pam_shells.so` — the first check
in the PAM stack — rejects any account with an unregistered shell before
the password is even checked.

**Fix:** the shell is now forced unconditionally with `usermod`, whether
the account is freshly created or already exists, and `/etc/shells` is
populated defensively regardless. Applied to both the live-boot account
setup and the disk installer's admin-account creation.

## Disk installer — VirtualBox detection

The disk-selection menu returned an empty list inside VirtualBox, making
installation impossible in a VM. Root cause: the disk filter assumed
`lsblk` always emits 5 columns (`NAME SIZE MODEL TRAN TYPE`). VirtualBox
virtual disks report empty `MODEL`/`TRAN` fields, and `lsblk` omits empty
columns rather than padding them — so a VBox disk came back as only 3
fields, and the type check (looking specifically at field 5) never
matched.

**Fix:** filtering now checks the last field regardless of column count.
Verified with an isolated behavioral test against both simulated
VirtualBox and physical-disk `lsblk` output.

## Privacy: stray developer directories

The base system image is built from a snapshot of an active development
system, not a blank template. This meant the real developer's home
directory — including old copies of build-staging trees — was being
inherited into every build without anything stripping it back out before
the final image was assembled.

**Fix:** a verified cleanup step now runs immediately after the base
system copy, removing every directory under `/home` except `hardened`,
and **hard-fails the build** if anything else remains afterward. Verified
with an isolated test proving stray directories are removed while the
real `hardened` account's home directory is left untouched.

## Dependency chain fixes

A large number of runtime dependencies were found missing via live
testing and staged in, most notably:

- The dynamic linker itself (`ld-linux-x86-64.so.2` / the `/lib64`
  symlink) — without this, no dynamically-linked binary could execute at
  all inside a chroot
- The full Samba dependency chain (for SMB share browsing in Dolphin)
- `zlib` (`libz.so.1`)
- `libnotify` (desktop notification popups)
- The Wayland client/cursor/egl libraries used by GTK's layer-shell
  support (present but functionally inert on this X11-only build —
  confirmed via XFCE's own documentation that `xfwm4` does not run under
  Wayland at all)

Four of these are now hard-required build-time checks (`libnotify`, the
dynamic linker, `zlib`, Samba), so a future build will fail loudly and
immediately if any of them go missing again, instead of shipping silently
broken and only surfacing the problem hours later at a runtime crash.

## Repository configuration

`pacman.conf` and the mirrorlist were previously untouched — inherited
as-is from the base system snapshot, unaudited. This build now ships
with:

- `core`, `extra`, and `multilib` correctly configured against a curated,
  geographically-diverse set of official Arch mirrors
- The [BlackArch](https://blackarch.org) security-tools repository,
  configured per BlackArch's own current official setup instructions

**BlackArch's GPG signing key is intentionally not pre-trusted.** Silently
trusting a third-party signing key during an unattended build defeats the
purpose of key verification. Before using BlackArch packages, run:

```
curl -O https://blackarch.org/keyring/blackarch-keyring.pkg.tar.xz
pacman-key --lsign-key <verify the current official key ID at blackarch.org>
```

## Installer UX

The disk installer previously required typing a raw device path
(`/dev/sda`, `/dev/nvme0n1`) by hand — easy to mistype before an
irreversible erase operation. It now presents a numbered menu instead;
the live/boot media's own disk is automatically identified and flagged
as non-selectable. The final `ERASE /dev/sdX` typed confirmation is
unchanged as the last safety check before anything is touched.

## Package additions this cycle

Kate, KMag, Dolphin, Firefox (official Arch build), Chromium (official
Arch build), Magnus (AUR), xfce-polkit (AUR), Breeze/Breeze-Dark/Papirus
icon and GTK themes, the BlackArch repository configuration.

See `DEPENDENCIES.md` for the underlying library list.
