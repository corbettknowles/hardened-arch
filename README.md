<div align="center">

<img src="assets/banner.png" alt="Project Banner" width="100%">

# Arch XFCE ISO Builder

**A custom Arch Linux–based XFCE ISO build system**

> **Disclaimer:** This project is an independent, community-developed project and is **not affiliated with, sponsored by, endorsed by, or otherwise associated with Arch Linux or the Arch Linux project.**
>
> **Arch Linux and related names, logos, and trademarks are the property of their respective owners.** This project does not claim ownership of, or any rights to, Arch Linux trademarks. The use of the name "Arch Linux" is solely to accurately describe the base technology and compatibility of this project.

</div>

> [!WARNING]
>
> ## ⚠️ Important — `build_arch_xfce_iso.py`
>
> **Please use caution when modifying the main body of `build_arch_xfce_iso.py`.**
>
> This is **not simply a Python script**. It is an **ISO builder** that coordinates multiple stages of the build process, including filesystem preparation, package integration, system configuration, boot configuration, and final ISO generation.
>
> Changes that appear minor in one section can produce **undesirable or unexpected results elsewhere in the build**. A modification may not immediately produce an error while still resulting in an incomplete, improperly configured, or otherwise broken ISO.
>
> ### 🛠️ Open Source & Creativity
>
> **Experimentation is absolutely encouraged.**
>
> This project is open source, and you're welcome to modify the builder, add features, restructure components, fork the project, or completely rethink how something works.
>
> Just keep in mind that the build system contains **interdependent components**. Before changing core build logic:
>
> * Understand what the section you're modifying is responsible for.
> * Check how other build stages depend on it.
> * Test the **complete ISO build** after significant changes.
> * Test the resulting ISO rather than relying solely on the Python script completing successfully.
> * Use Git branches or backups when experimenting with major changes.
>
> **In short:**
>
> > **Be creative. Experiment. Break things. Fix things. Improve things.**
> >
> > Just remember that `build_arch_xfce_iso.py` is an **ISO build system**, not a disposable Python script.
>
> A successful execution of the builder does **not automatically mean that the resulting ISO is correct**.
>
> **Have fun hacking on it — and keep an eye on what the builder is actually doing. 😉**

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
