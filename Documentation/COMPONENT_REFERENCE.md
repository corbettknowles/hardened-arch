# Component Reference

Documentation, man pages, and help files are intentionally stripped from
this build during package extraction to keep the shipped image small.
This document exists to fill that gap — a plain-language reference for
what's actually running on your system, since it won't be available via
`man` locally.

## Core system

**Kernel (Linux 7.1.2, hardened configuration)**
The core of the operating system — manages hardware, memory, processes,
and security enforcement. This build uses a hardened kernel configuration
rather than a stock/default one, meaning additional compile-time security
mitigations are enabled beyond what a typical distribution ships.

**Xorg (X.org / X11)**
The display server — the layer that actually draws windows on screen and
handles input from your keyboard and mouse. XFCE runs on top of this. Not
to be confused with Wayland, a newer, different display protocol this
build does not use.

**Pacman**
Arch Linux's package manager — installs, removes, and updates software.
Commands run through `pacman`, e.g. `pacman -S <package>` to install,
`pacman -Syu` to update the whole system.

**SDDM (Simple Desktop Display Manager)**
The login screen. Handles authenticating your user account and starting
your desktop session after login.

**Limine**
The bootloader — the first piece of software that runs when the computer
powers on, responsible for loading the kernel and handing off control to
it.

## Desktop environment

**XFCE**
The full desktop environment — the panel, file manager, window manager,
settings, and everything you interact with day-to-day. Chosen over KDE
Plasma for this build specifically because Plasma dropped X11 support
entirely as of version 6.7.0.

**xfwm4**
XFCE's window manager — handles window borders, moving/resizing windows,
alt-tab switching, and window decorations.

**xfdesktop**
Manages the desktop background and desktop icons specifically (separate
from the window manager).

**xfce4-panel**
The taskbar/panel itself — the bar containing the start menu, running
applications, system tray, and clock.

**Thunar**
XFCE's default file manager.

## Included applications

**Kate**
A KDE-authored text editor with syntax highlighting, useful for code and
config file editing. Runs standalone on XFCE via the KDE Frameworks 6
runtime libraries — it doesn't require KDE Plasma to be installed.

**Dolphin**
A KDE-authored file manager, included as an alternative to Thunar.

**KMag**
A screen magnifier, also from the KDE project, included since XFCE has
no native magnifier of its own.

**Magnus**
A separate, lightweight, non-KDE screen magnifier (Python-based),
included as an alternative to KMag for users who prefer a smaller,
non-Qt tool.

**Firefox / Chromium**
Both included as official Arch Linux package builds, giving users a
choice between the two major independent browser engines.

**xfce4-terminal**
XFCE's default terminal emulator.

## Package repositories

**core / extra / multilib**
The standard official Arch Linux package repositories.

**BlackArch**
An optional, separately-configured repository providing several thousand
security and penetration-testing tools. Not enabled by default in the
sense that its GPG signing key must be manually trusted before use — see
`BUILD_NOTES.md` for the exact steps. This is a legitimate, well-known
Arch-based security project (blackarch.org), unaffiliated with this
distribution.

## Accessibility

**Sticky Keys**
Configured natively via XFCE's own AccessX mechanism (not a KDE-derived
tool), allowing modifier keys (Ctrl, Alt, Shift) to be pressed one at a
time instead of held simultaneously.

**Orca**
Not bundled by default, but this system is compatible with it — Orca is
the standard Linux screen reader, communicating via the AT-SPI
accessibility bus, which works across desktop environments rather than
being tied to GNOME specifically despite originating there.

## Getting more information once installed

Since local documentation is stripped to save space, the most reliable
way to look up a specific command or tool once you're actually using the
system is:

- `<command> --help` — most command-line tools support this even without
  full man page installation
- The Arch Wiki (wiki.archlinux.org) — covers virtually every package in
  these repositories in detail, and applies directly since this is a
  genuine Arch Linux base
- `pacman -Qi <package>` — shows installed package metadata, including a
  description
