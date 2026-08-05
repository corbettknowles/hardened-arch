# Contributing to Hardened Arch Linux (XFCE Edition)

Thanks for your interest in contributing. This project follows standard
open-source conventions — if you've contributed to other projects before,
most of this will be familiar.

## Code of Conduct

All community spaces — issues, pull requests, discussions, and any
associated chat/forum — are expected to remain professional and
respectful. This means:

- No harassment, bullying, or personal attacks
- No hateful, discriminatory, or disrespectful language directed at any
  individual or group
- Disagree with ideas and code, not people
- Assume good faith; ask clarifying questions before assuming bad intent

Reports of Code of Conduct violations will be reviewed by the maintainer.
Repeated or severe violations may result in removal from the project's
community spaces.

## Before you start

- Check open issues first to avoid duplicate work
- For anything non-trivial (new features, architectural changes), open an
  issue to discuss the approach before writing code
- For bug fixes, a pull request with a clear description is usually fine
  to open directly

## Commit standards

- All commits must go through the GitHub CLI (`gh`) verification flow —
  direct unverified pushes are not accepted
- Commit messages should be clear and describe *what* changed and *why*,
  not just *that* something changed
- Reference the relevant issue number where applicable (`Fixes #12`,
  `Related to #7`)
- Keep commits scoped — one logical change per commit rather than
  bundling unrelated fixes together

## Pull requests

- PRs should target a specific, described change — explain what problem
  it solves and how you tested it
- Include before/after behavior where relevant (screenshots for UI
  changes, command output for build/script changes)
- Build script changes should be tested against an actual build, not just
  reviewed for syntax — this project has a documented history of changes
  that looked correct but failed silently at runtime (see
  `BUILD_NOTES.md`); real verification before submitting saves everyone
  time
- Be responsive to review feedback; PRs with no activity for an extended
  period may be closed and can be reopened when you're ready to continue

## Bug reports

Please include:

- What you expected to happen vs. what actually happened
- Steps to reproduce
- Whether you're on real hardware or a VM (and which one)
- Relevant logs where applicable:
  - `~/.config/hardened/first-login.log`
  - `~/.config/hardened/panel-crash.log`
  - `/var/log/hardened-drm-trace.log`
  - The build's own console output, if the issue is build-related

Check `KNOWN_ISSUES.md` first — your issue may already be tracked there
with a diagnostic path already in progress.

## Feature requests / discussion

Open a discussion thread rather than an issue for anything exploratory
("should this distro support X") versus something concrete and actionable
("here's a specific bug" or "here's a specific, scoped feature with a
plan").

## Areas currently welcoming help

- Testing the KDE/Plasma build variant (pre-alpha, unverified — see
  `BUILD_ENVIRONMENT.md`)
- Confirming the wallpaper/panel-layout and systray plugin fixes across a
  range of real hardware, not just the environments already tested
- General alpha testing and bug reports from real-world use

## License

This project is licensed under the **GNU General Public License v3.0
(GPL-3.0)**. By contributing, you agree that your contributions will be
licensed under the same terms.
