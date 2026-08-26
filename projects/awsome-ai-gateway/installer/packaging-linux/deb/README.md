# Debian / Ubuntu package (`.deb`)

Native `apt`/`dpkg` packaging for the LLM Gateway CLI v2 suite. This is a **thin
layer** over the shared, distro-agnostic payload built by `../build.sh`; it does
not rebuild the CLI.

## Why a `.deb` (vs the `.run` self-extractor)

The customer ask is to match the Windows experience on Linux: **no Python
pre-install, corporate defaults baked in, minimal manual steps, and the same
`install → login → setup → claude` flow.** A native package delivers the "minimal
manual steps" half far better than the generic `.run`:

| Concern | `.run` self-extractor | `.deb` (this) |
|---|---|---|
| Python required | No (embedded) | No (embedded) |
| Corporate defaults baked | Yes (`site-config.json`) | Yes (same payload) |
| PATH setup | Edits `~/.bashrc` / `profile.d` | Symlinks into `/usr/bin` — already on PATH |
| Uninstall | Custom `uninstall.sh` | `sudo apt remove gateway-cli-suite` |
| Upgrade | Re-run `.run` | dpkg version handling |
| File tracking | None | dpkg owns every file |

## Architecture (modularity)

```
packaging-linux/
├── build.sh              # SHARED: builds dist/gateway-cli-suite/ (embedded Python + deps)
├── install.sh            # generic .run on-target installer
├── make_installer.sh     # generic .run packer
└── deb/
    └── build-deb.sh      # Debian/Ubuntu: wraps dist/gateway-cli-suite/ into a .deb
```

`build.sh` produces the **one** payload (`dist/gateway-cli-suite/`). Each
packaging format is a sibling consumer of that payload — the application bundle
never differs between them. Adding another distro is the same shape:

```
deb/build-deb.sh    -> .deb   (Debian, Ubuntu)      ← this file
rpm/build-rpm.sh    -> .rpm   (Fedora, RHEL)         ← future, same pattern
```

An rpm builder would consume the identical `dist/gateway-cli-suite/`, install to
`/opt/gateway-cli-suite`, symlink into `/usr/bin`, and only swap `dpkg-deb` for
`rpmbuild`. No CLI or payload changes.

## Build

Two steps — build the shared payload once, then package it:

```bash
# 1. Build the distro-agnostic payload (from packaging-linux/).
#    Bake corporate defaults here, exactly as for the .run.
./build.sh --skip-installer \
  --oidc-issuer-url https://cognito-idp.<region>.amazonaws.com/<pool> \
  --oidc-client-id  <client-id> \
  --gateway-url     https://<gateway-host> \
  --admin-api-url   https://<admin-api-host> \
  --ca-bundle       /etc/ssl/certs/corp-ca.pem

# 2. Wrap it in a .deb (from packaging-linux/deb/).
./deb/build-deb.sh
```

Output: `dist/installer/gateway-cli-suite_<version>_<arch>.deb` (+ `.sha256`).

Build-host requirement: `dpkg-deb` (`sudo apt-get install dpkg-dev`). The build
must run on the **same arch** as the target (PyInstaller does not cross-compile);
`--arch amd64|arm64` labels the package accordingly.

## Install / use (end user)

```bash
sudo apt install ./gateway-cli-suite_<version>_<arch>.deb
# same unified flow as Windows:
gateway-cli login
gateway-cli setup --model sonnet
claude
```

- Installs the runtime to `/opt/gateway-cli-suite`.
- Symlinks `gateway-cli`, `api-key-helper`, `statusline` into `/usr/bin` (on
  PATH in every shell — no rc editing, no new-terminal step).
- Corporate OIDC / gateway / CA defaults are already inside the binaries.

## Uninstall / upgrade

```bash
sudo apt remove gateway-cli-suite     # clean removal; symlinks dropped by postrm
sudo apt install ./gateway-cli-suite_<newer>_<arch>.deb   # in-place upgrade
```

## Notes

- **CA bundle:** bake `--ca-bundle` at `build.sh` time so `gateway-cli setup`
  writes it into Claude Code settings. The embedded runtime otherwise ships only
  certifi's public CA list.
- **Not for WSL-with-Windows-Claude:** this package is for native Linux (and WSL
  running a native-Linux Claude). WSL where Claude is the Windows binary is
  served by the Windows installer.
