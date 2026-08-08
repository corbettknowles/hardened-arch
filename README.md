<p align="center">
  <img src="assets/banner.png" alt="Hardened Arch Linux" width="100%"/>
</p>

<h1 align="center">Hardened Arch Linux — XFCE Edition</h1>





# Hardened Arch Linux — XFCE Edition (Alpha 1.10)

A from-scratch, security-hardened Arch Linux distribution built around
XFCE 4.20, Kernel 7.1.2 (hardened configuration), the Limine bootloader,
and Pacman.

This is an **alpha release**. It is functional and boots reliably, but is
intended for testers and early adopters — not production use. See
`KNOWN_ISSUES.md` for what's still being worked on.

## What's in this build

- **Kernel:** 7.1.2, hardened configuration
- **Desktop:** XFCE 4.20 (X11 session)
- **Display manager:** SDDM
- **Bootloader:** Limine
- **Package manager:** Pacman, with `core`/`extra`/`multilib` and the
  [BlackArch](https://blackarch.org) security-tools repository
  pre-configured (BlackArch's GPG key must be trusted manually on first
  use — see `BUILD_NOTES.md`)
- **Included applications:** Thunar, Kate, KMag, Dolphin, Firefox,
  Chromium, Magnus (screen magnifier), xfce4-terminal
- **Icon/theme set:** Breeze, Breeze-Dark, Papirus (all switchable)
- **Accessibility:** Sticky Keys via native XFCE AccessX, Magnus for
  screen magnification, Orca-compatible via AT-SPI

## Live boot

The live session auto-logs in as the `hardened` user (password:
`hardened`) — this is intentional. Anyone holding the boot media already
has physical access to the live environment, so a password prompt on the
live session adds no real security; it's standard behavior shared by most
major live Linux distributions.

## Installing to disk

Run `hardened-install` from a terminal in the live session. It will:

1. Present a numbered list of detected disks (works correctly in
   VirtualBox and on real hardware)
2. Require you to type `ERASE /dev/sdX` to confirm before anything is
   touched
3. Prompt you to create a real administrator account with a real password

**Once installed to disk, autologin is disabled.** A real account and
password are required to log in, exactly as expected for a persistent
installation. Live-media convenience and installed-system security are
deliberately different by design.

## Reporting issues

This is early alpha software built by one person with community testing
in mind. Please report anything broken, including:

- Panel plugin crashes (a live diagnostic log is written to
  `~/.config/hardened/panel-crash.log` on every boot — please attach it)
- Login/session issues (`~/.config/hardened/first-login.log`)
- Anything visually or functionally inconsistent with what's documented
  here

See `BUILD_NOTES.md` for the full technical build notes and
`KNOWN_ISSUES.md` for what's already been identified and is being worked
on.
