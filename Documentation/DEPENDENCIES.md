# Dependencies — Alpha 1.10

## Desktop environment
| Component | Version |
|---|---|
| xfce4-session | 4.20.x |
| xfce4-panel | 4.20.x |
| xfce4-settings | 4.20.x |
| xfwm4 | 4.20.x |
| xfdesktop | 4.20.x |
| Thunar | 4.20.x |
| xfce4-power-manager | 4.20.x |
| xfce4-terminal | 1.2.0 |
| upower | 1.91.x |

## Applications
| Component | Source |
|---|---|
| Firefox | Official Arch repo build |
| Chromium | Official Arch repo build |
| Kate | Official Arch repo (KF6) |
| KMag | Official Arch repo (KF6) |
| Dolphin | Official Arch repo (KF6) |
| Magnus (screen magnifier) | AUR |
| xfce-polkit | AUR |

## Icon / theme assets
| Component | Source |
|---|---|
| breeze-icons | Official Arch repo |
| papirus-icon-theme | Official Arch repo |
| breeze-gtk | Official Arch repo |

## Core runtime libraries staged/verified this cycle
- `pacman` / `libalpm` — base package manager
- Full Samba dependency chain (`smbclient`, `ldb`, `talloc`, `tevent`,
  `libwbclient`, and Samba's internal private libraries — resolved via
  binary RUNPATH, not a flat library path)
- `zlib` (`libz.so.1`)
- `libnotify` (desktop notifications)
- `readline`, `libarchive`, `popt`
- Dynamic linker (`ld-linux-x86-64.so.2`)
- `libwayland-client`, `libwayland-cursor`, `libwayland-egl`,
  `libwayland-server` — present for GTK layer-shell compatibility;
  functionally inert on this X11-only build
- `libgtk-layer-shell`, `libwnck`
- `libxklavier`, `libgtop`

## Full Qt6/KF6 runtime chain
Pulled as a full dependency tree via `pactree`, covering everything Kate,
KMag, and Dolphin require: `qt6-base`, `qt6-multimedia`, `kio`,
`kbookmarks`, `kcmutils`, `kcodecs`, `kcolorscheme`, `kcompletion`,
`kconfig`, `kconfigwidgets`, `kcoreaddons`, `kcrash`, `kdbusaddons`,
`kfilemetadata`, `kguiaddons`, `ki18n`, `kiconthemes`, `kirigami`,
`kitemviews`, `kjobwidgets`, `knewstuff`, `knotifications`, `kpackage`,
`kparts`, `kservice`, `ktexteditor`, `kuserfeedback`, `kwallet`,
`kwidgetsaddons`, `kwindowsystem`, `kxmlgui`, `solid`, `sonnet`,
`syndication`, `syntax-highlighting`, `baloo`, `baloo-widgets`, and
supporting libraries (`karchive`, `attica`, `editorconfig-core-c`,
`media-player-info`, `polkit-qt6`).

**Intentionally excluded:** `kdoctools` (pulls a 133MB WebKit dependency
solely for an in-app help viewer feature).

## Package repositories configured
- `core`, `extra`, `multilib` — official Arch, via a curated
  geographically-diverse mirror list
- `blackarch` — security/pentesting tools (see BUILD_NOTES.md for GPG key
  trust setup, intentionally not automated)

## Kernel
Linux 7.1.2, hardened configuration, custom build.
