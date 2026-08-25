# Linux / WSL Installer Packaging

Build assets that turn `gateway-cli-v2` into a **fully self-contained Linux/WSL
installer** for end users on isolated (air-gapped) networks with **no Python
installed**. PyInstaller embeds the CPython 3.11+ runtime and every dependency
(`click`, `structlog`, `platformdirs`, `boto3`, `requests`, `PyYAML`, plus all
stdlib modules such as `socket`, `subprocess`, `ssl`) into the bundle, so the
target machine needs nothing but a 64-bit Linux kernel (WSL 2 counts as Linux).

This is the Linux/WSL sibling of `../packaging/` (Windows). The two share **one**
CLI source tree — `packaging/entrypoints/gateway-cli-v2` — so the application
code can never drift between platforms; only the build glue and the packaging
format (`.deb` / `.run` vs `setup.exe`) differ.

## Distribution format: `.deb` recommended

The closest match to the Windows experience (no Python pre-install, corporate
defaults baked in, minimal manual steps) is the **native `.deb` package** — it
symlinks the CLIs into `/usr/bin` (already on PATH, no rc editing) and `apt`
tracks/upgrades/removes every file. See [`deb/README.md`](deb/README.md) for the
full recipe. The `.run` self-extractor below is the portable fallback for
non-Debian distros. Both wrap the **same** payload built by `build.sh`.

| Concern | `.run` self-extractor | `.deb` (recommended) |
|---|---|---|
| Python required | No (embedded) | No (embedded) |
| Corporate defaults baked | Yes (`site-config.json`) | Yes (same payload) |
| PATH setup | edits `~/.bashrc` / `profile.d` | `/usr/bin` symlink — already on PATH |
| Uninstall | custom `uninstall.sh` | `sudo apt remove gateway-cli-suite` |
| Upgrade | re-run `.run` | dpkg version handling |
| File tracking | none | dpkg owns every file |

## What gets built

```
dist/
├── gateway-cli-suite/                    # PyInstaller onedir output
│   ├── gateway-cli                       # cli.main:main
│   ├── api-key-helper                    # api_key_helper.main:main
│   ├── statusline                        # statusline.main:main
│   └── _internal/                        # ONE shared Python runtime + deps
└── installer/
    ├── gateway-cli-suite_<version>_<arch>.deb   # native .deb (recommended)
    └── gateway-cli-setup-<version>.run          # single-file offline .run (fallback)
```

The three CLIs share one `_internal` runtime folder (single copy of the Python
shared library, botocore JSON service models, certifi CA bundle), which keeps
the installer several times smaller than three onefile binaries and makes
startup faster — no self-extraction to a temp directory on every run.

## File map

| File | Purpose | Windows analogue |
|---|---|---|
| `entrypoints/*_entry.py` | Thin shims mirroring the `[tool.poetry.scripts]` entries (PyInstaller needs a script file, not a `module:function`) | same |
| `gateway_cli.spec` | PyInstaller spec: 3 console binaries, one shared `COLLECT` | `../packaging/gateway_cli.spec` |
| `build.sh` | One-command build pipeline (venv → pip → PyInstaller → smoke test → `.run`) | `build.ps1` |
| `deb/build-deb.sh` | Thin layer that wraps the payload into a native `.deb` | `installer.iss` + ISCC |
| `make_installer.sh` | Packs the onedir output + `install.sh` into a self-extracting `.run` | `installer.iss` + ISCC |
| `install.sh` | `.run`'s on-target installer: copies runtime, wires PATH, writes uninstaller | `installer.iss` `[Code]` section |
| `download_wheels.sh` | Optional: pre-fetch wheels so the *build machine* can also be offline | `download_wheels.ps1` |
| `site-config.json` | Baked corporate defaults input (OIDC, domains, CA path) | same |
| `site-extra.json.example` | Custom-key injection example | same |

## Build machine requirements

PyInstaller does **not** cross-compile — the build must run on **Linux** with
the **same architecture and a compatible glibc** as the target machines
(a container or CI runner is fine). You need:

1. **Python 3.11+** (matching `pyproject.toml`)
2. **bash**, **tar**, **gzip** — present on every Linux/WSL distro
3. This repository, with the `packaging/entrypoints/gateway-cli-v2/` project present.

> The CLI project is single-sourced from the sibling `../packaging/` folder.
> If you ship `packaging-linux/` on its own, vendor the project under
> `packaging-linux/entrypoints/gateway-cli-v2/` and the spec/build will use it.

## Building

`build.sh` creates a throwaway `.build-venv`, pip-installs the project (pip
understands the poetry-core backend directly — Poetry itself is not needed),
runs PyInstaller, smoke-tests each binary with `--help`, and packs the payload.

**`.deb` flow (recommended)** — build the shared payload, then wrap it. Bake the
corporate defaults on the `build.sh` step (see the CA-bundle note below):

```bash
./build.sh --skip-installer \
  --gateway-url   https://gateway.example.com \
  --admin-api-url https://api.gateway.example.com \
  --ca-bundle     /etc/ssl/certs/corp-proxy-ca.pem
./deb/build-deb.sh
```

Result: `../dist/installer/gateway-cli-suite_<version>_<arch>.deb` + `.sha256`.

**`.run` flow (fallback)** — one command produces the self-extractor:

```bash
./build.sh   # ../dist/installer/gateway-cli-setup-<version>.run + .sha256
```

> **⚠️ Bake the CA bundle as a Linux path.** In an environment with a corporate
> CA, building without `--ca-bundle` can write an unusable value into the
> settings. Always pass a **Linux** path, e.g.
> `--ca-bundle /etc/ssl/certs/<corp-ca>.pem`. (A platform-aware fallback means
> that when it is left unset, Linux relies on the OS trust store rather than a
> Windows path.)

Skip both installers and ship a tarball instead:

```bash
./build.sh --skip-installer
tar -C ../dist -czf gateway-cli-suite-<version>.tar.gz gateway-cli-suite
```

### Air-gapped build machine

If the build machine has no internet either, prepare a wheel cache on a
connected machine with the **same Linux arch / glibc / Python minor version**:

```bash
./download_wheels.sh --out-dir /path/to/wheels
```

Copy the wheels and the repo across, then:

```bash
./build.sh --wheel-dir /path/to/wheels
```

## Installing on the isolated network

### `.deb` (recommended)

Transfer the single `.deb` (USB, file share, etc.):

```bash
sudo apt install ./gateway-cli-suite_<version>_<arch>.deb
# unified flow, same as Windows:
gateway-cli login
gateway-cli setup --model sonnet
claude
```

- Runtime installs under `/opt/gateway-cli-suite`; `gateway-cli`,
  `api-key-helper`, `statusline` are symlinked into `/usr/bin` — on PATH in
  every shell, **no rc editing, no new-terminal step**.
- Corporate OIDC / gateway / CA defaults are already baked into the binaries.
- **Uninstall:** `sudo apt remove gateway-cli-suite` (the `postrm` drops the
  symlinks). **Upgrade:** `apt install` a newer `.deb` in place.

### `.run` (fallback)

Transfer the single `gateway-cli-setup-<version>.run` (USB, file share, etc.).

- **Per-user (no root):**

  ```bash
  chmod +x gateway-cli-setup-<version>.run
  ./gateway-cli-setup-<version>.run
  ```

  Installs the runtime under `~/.local/share/gateway-cli-suite`, launchers in
  `~/.local/bin`, and adds that dir to PATH via your shell rc / `~/.profile`.

- **System-wide (root):**

  ```bash
  sudo ./gateway-cli-setup-<version>.run
  ```

  Installs under `/opt/gateway-cli-suite`, launchers in `/usr/local/bin`, and a
  PATH snippet in `/etc/profile.d/gateway-cli.sh`.

- **Silent / mass deployment (Ansible, MDM, etc.):** add `--quiet`. Override
  locations with `--prefix DIR` / `--bindir DIR`, or skip PATH edits with
  `--no-path`. Unpack without installing with `--extract-only DIR`.

- **PATH** takes effect in any **new** terminal (or `source ~/.bashrc`). The
  three CLIs — `gateway-cli`, `api-key-helper`, `statusline` — are then on PATH.
- **Uninstall:** run the generated `uninstall.sh` (printed at the end of the
  install) with the same privileges used to install. It removes the runtime,
  the launchers, and the PATH edits.
- **Upgrades:** run a newer `.run` over the old install — it replaces the
  install dir in place and refreshes the launchers.

## WSL notes

WSL 2 presents as native Linux (`sys.platform == "linux"`), so this bundle runs
there unchanged. The CLI already handles WSL-specific behaviour at runtime (see
`cli/platform.py`): it opens the browser on the Windows side via `wslview` /
`powershell.exe`, and finds onboarding cards in the Windows Downloads folder via
`wslpath`. No packaging changes are needed for WSL versus native Linux — build
once on Linux x86_64 and the same `.run` installs in both.

## Isolated-network gotchas worth planning for

- **TLS to internal endpoints:** the bundle ships certifi's public CA list.
  If the gateway/OIDC endpoints use an internal corporate CA, users must set
  `REQUESTS_CA_BUNDLE` (requests) and `AWS_CA_BUNDLE` (boto3) to the internal
  PEM, or bake the CA path into `site-config.json` (`caBundle`). `truststore`
  (bundled) also routes verification through the system CA store
  (`/etc/ssl/certs`) when the app enables it.
- **boto3 region/endpoint:** with no route to AWS, make sure the CLI points
  boto3 at internal endpoints (`endpoint_url`); nothing in the packaging fixes
  an app-level assumption of public AWS.
- **Executable bit / noexec mounts:** the `.run` must be executable
  (`chmod +x`), and `/tmp` must not be mounted `noexec` (the stub extracts
  there). If `/tmp` is `noexec`, point it elsewhere with `TMPDIR=/var/tmp`.
- **Integrity:** `build.sh` always emits a `.sha256`. Pass `--sign-gpg-key
  <keyid>` to also produce a detached `.asc` GPG signature (the Linux analogue
  of Authenticode) so recipients can `gpg --verify` before running.

## Baked corporate defaults (`site-config.json`)

Edit `site-config.json` (camelCase keys: `oidcIssuerUrl`, `oidcClientId`,
`gatewayUrl`, `adminApiUrl`, `caBundle`). `build.sh` reads it and bakes the
values into the binaries via a generated `cli/_site_config.py`. Precedence:

```
build.sh --flag  >  GATEWAY_CLI_DEFAULT_*  >  site-config.json  >  site_defaults.py literal
```

The file holds environment-specific identifiers, so it is `.gitignore`d — never
commit it.

## Custom-key injection (`site-extra.json`)

Copy `site-extra.json.example` to `site-extra.json` and edit it. `build.sh`
bundles it beside the `cli` package; `gateway-cli setup` deep-merges each
section into the settings files (see the example's `__comment` for the merge
rules). Absent → injection is a no-op. Also `.gitignore`d.

## Maintenance notes

- New dependency in `pyproject.toml`? Picked up automatically, but if it loads
  data files or plugins dynamically, add a `collect_data_files(...)` /
  `collect_submodules(...)` line in `gateway_cli.spec` (keep it in sync with
  the Windows spec).
- New console script in `[tool.poetry.scripts]`? Add a shim in `entrypoints/`,
  an `Analysis`/`PYZ`/`EXE` trio in the spec, add it to the `COLLECT`, extend
  the smoke-test list in `build.sh`, the `CLIS` array in **both** `install.sh`
  and `deb/build-deb.sh`, and the mirror changes in `../packaging/`.
- Bump the product version by editing `pyproject.toml`; `build.sh` reads it
  from there (or override with `--version`).
```
