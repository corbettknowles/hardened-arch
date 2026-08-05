# Build Environment — Source Directory Layout

This documents the actual directory structure the build script depends
on. Anyone reproducing this build from source needs these directories
populated correctly before running the builder.

## Top-level source trees (under `/home/corbett/` in the reference build
environment — adjust paths via the script's command-line arguments if
reproducing elsewhere)

| Directory | Purpose |
|---|---|
| `xorg-source-stage/rootfs/` | Base X11 runtime root. This is `runtime_root` for the XFCE build — a minimal, audited Arch base with X.org staged, no KDE/GNOME content. |
| `xfce-source-stage/rootfs/` | The curated XFCE desktop stage. Overlaid on top of `runtime_root` at build time. Contains XFCE itself plus all standalone applications added during this cycle (Kate, Dolphin, KMag, Firefox, Chromium, Magnus, xfce-polkit, Samba, pacman/libalpm) and their full dependency chains. |
| `system-stage/` | Original full base+KDE system snapshot. Used as `runtime_root` for the separate KDE build variant. **Note:** this snapshot was taken from an active development system and previously contained stray developer home-directory content; the build script now strips this automatically — see `BUILD_NOTES.md`. |
| `kde/usr` | Archived KDE install prefix, historically used as a path-comparison reference for excluding KDE-specific files from the XFCE build. Largely superseded by the explicit allowlist approach used for the KDE variant (see `_copy_shared_kde_components` in the build script). |

## Kernel build trees

| Directory | Purpose |
|---|---|
| `linux-7.1.2/` | Kernel source tree (hardened configuration) |
| `linux-7.1.2-build/` | Kernel build output directory (`O=` target for `make`) |
| `linux-7.1.2-artifacts/` | Final compiled kernel image and related artifacts |
| `linux-7.1.2-stage/` | Staged initramfs contents, including `rootfs/usr/lib/modules/<kver>` used by `prepare_live_root()` |

## Build tooling

| Directory | Purpose |
|---|---|
| `bootloader-build/limine/` | Limine bootloader build output |
| `hardened-qt-tools/install/` | A separate Qt-based tooling payload, overlaid onto the live root late in the build. Contains `etc/systemd/system/getty@tty1.service.d/`, `etc/systemd/system/serial-getty@ttyS0.service.d/`, and `etc/hardened-arch/update.conf`. |

## Build-time working directories (created fresh each run)

| Directory | Purpose |
|---|---|
| `hardened-arch-iso-build/` | Top-level work directory for a build run. Only persists after the build if `--keep-work` is passed. |
| `hardened-arch-iso-build/live-root/` | The actual live filesystem being assembled — this is `paths.live_root`, the real target of every staging/config-writing step in the build script. |
| `hardened-arch-iso-build/iso-root/` | Final ISO filesystem layout, including the packed `rootfs.sfs` squashfs image. |

## Build pipeline order (XFCE build)

1. `runtime_root` (`xorg-source-stage/rootfs`) is copied into `live-root` as
   the base.
2. **Stray developer home directories are stripped and hard-verified**
   (added 2026-08-03) — everything under `/home` except `hardened` is
   removed, and the build fails if anything else survives.
3. Kernel modules are staged in from `linux-7.1.2-stage`.
4. The XFCE stage (`xfce-source-stage/rootfs`) is overlaid via `rsync
   --force` (the `--force` flag is required to correctly replace
   `runtime_root`'s real `usr/sbin` directory with the symlink XFCE's
   stage correctly uses per Arch's merged-usr layout).
5. Account/password setup runs (`usermod` shell fix applied
   unconditionally, `/etc/shells` populated defensively).
6. SDDM/Wayland/DRM-trace/Plymouth/pacman-repo configuration steps run,
   each with their own hard-required file checks.
7. The disk image and squashfs are assembled into `iso-root`.

## Build pipeline order (KDE variant — scaffold only, not yet tested)

1. `runtime_root` (`system-stage`) is copied into `live-root` as the base
   (this tree already contains Plasma/KWin/SDDM's KDE components).
2. Stray developer home directories are stripped (same mechanism as
   XFCE).
3. `_copy_shared_kde_components()` copies an explicit allowlist of
   genuinely shared material out of `xfce-source-stage` — Kate, Dolphin,
   KMag, Magnus, pacman, Firefox, Chromium, the Wayland libraries, Breeze
   assets, the full Qt6/KF6 chain, and Samba. XFCE-only binaries are never
   touched in either direction.
4. SDDM is configured for `Session=plasma` / `XSession=plasma` instead of
   `xfce`.

**This variant has not been run.** See `KNOWN_ISSUES.md`.
