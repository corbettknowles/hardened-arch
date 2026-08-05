# Known Issues — Alpha 1.10

This is an honest list of what's not yet fully resolved. If you hit any
of these, the diagnostic logs mentioned below will help — please attach
them to any issue report.

## Wallpaper / panel layout may not apply on first login

The custom wallpaper and bottom-panel layout (with the start menu) are
applied by a first-login script rather than baking directly into every
account's default config. This script has been substantially rewritten
to fix a silent-failure bug (all output was previously suppressed, so
failures were invisible), but has not yet been confirmed reliable across
every environment.

**If your desktop shows the default XFCE top panel/wallpaper instead of
the custom layout:** check
`~/.config/hardened/first-login.log` for the actual reason. The script
will retry on next login if it didn't fully succeed (it only marks itself
complete once the wallpaper genuinely verifies as applied).

## Systray panel plugin may show a repeated-crash dialog

XFCE's panel occasionally reports the status tray plugin crashing
repeatedly and offers to remove it. The underlying cause has not been
conclusively identified yet.

**Diagnostic data is now captured automatically** at
`~/.config/hardened/panel-crash.log` on every login, including whether
the plugin's libraries resolve correctly, who currently owns the X11 tray
manager selection, and what tray-related processes are running. Please
attach this log to any report of this issue.

## Optional thumbnail previews are limited

Video, RAW camera photo, and EPUB ebook thumbnail previews in Thunar are
not included by default (only standard image formats like JPG/PNG use
Tumbler's built-in generic thumbnailer). This is a deliberate size
tradeoff, not a bug — the EPUB thumbnailer specifically pulls in a
133MB WebKit dependency for a feature most users won't need. Install
`ffmpegthumbnailer`, `libopenraw`, and/or `libgepub` via pacman if you
want these previews.

## KDE/Plasma variant is separate and pre-alpha

A KDE-based build (`build_hardened_arch_kde_iso.py`) exists as a starting
scaffold but has not been run or tested. It is not part of this release.
