#!/usr/bin/env bash
#
# Builds the LLM Gateway CLI v2 Linux/WSL executables and offline installer.
#
# Runs the full packaging pipeline on a Linux x86_64 machine:
#   1. Creates a clean build venv (Python 3.11+).
#   2. Installs the project + PyInstaller into it (online, or from a local
#      wheel cache created by download_wheels.sh for air-gapped builders).
#   3. Runs PyInstaller with gateway_cli.spec.
#   4. Smoke-tests each produced executable (--help).
#   5. Packages a single self-extracting installer .run file (unless -s/--skip-installer).
#
# Output:
#   dist/gateway-cli-suite/                    executables + shared runtime (tar-able)
#   dist/installer/gateway-cli-setup-<v>.run   single-file offline installer
#
# The CLI source is single-sourced from the sibling packaging/ folder, so the
# Linux and Windows builds always ship identical application code.
#
# Usage:
#   ./build.sh [options]
#
# Options mirror build.ps1's parameters (see --help below).

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults / arg parsing
# ---------------------------------------------------------------------------
# Environment-specific corporate values baked into the build. The two OIDC
# values are the ones that must be supplied per environment; the domains and CA
# path have sensible defaults in cli/site_defaults.py and only need
# overriding for a different environment. Any value left blank here (or via the
# matching GATEWAY_CLI_DEFAULT_* env var) falls back to the literal default in
# site_defaults.py.
WHEEL_DIR=""
VERSION=""
OIDC_ISSUER_URL="${GATEWAY_CLI_DEFAULT_OIDC_ISSUER_URL:-}"
OIDC_CLIENT_ID="${GATEWAY_CLI_DEFAULT_OIDC_CLIENT_ID:-}"
GATEWAY_URL="${GATEWAY_CLI_DEFAULT_GATEWAY_URL:-}"
ADMIN_API_URL="${GATEWAY_CLI_DEFAULT_ADMIN_API_URL:-}"
CA_BUNDLE="${GATEWAY_CLI_DEFAULT_CA_BUNDLE:-}"
# Optional detached GPG signature over the .run installer (the Linux analogue of
# Authenticode). Supply a key id/fingerprint; when omitted, signing is skipped
# with a warning and the build still succeeds.
SIGN_GPG_KEY="${GATEWAY_CLI_SIGN_GPG_KEY:-}"
SKIP_INSTALLER=0

usage() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  -w, --wheel-dir DIR        Install from a local wheel cache (offline build).
  -V, --version VER          Version stamped into the installer file name.
                             Defaults to the version in pyproject.toml.
      --oidc-issuer-url URL  Bake the OIDC issuer URL default.
      --oidc-client-id ID    Bake the OIDC client id default.
      --gateway-url URL      Bake the gateway URL default.
      --admin-api-url URL    Bake the admin API URL default.
      --ca-bundle PATH       Bake the CA bundle path default.
      --sign-gpg-key KEYID   Detached-sign the .run installer with this GPG key.
  -s, --skip-installer       Build only the PyInstaller output; skip the .run.
  -h, --help                 Show this help and exit.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -w|--wheel-dir)      WHEEL_DIR="$2"; shift 2;;
        -V|--version)        VERSION="$2"; shift 2;;
        --oidc-issuer-url)   OIDC_ISSUER_URL="$2"; shift 2;;
        --oidc-client-id)    OIDC_CLIENT_ID="$2"; shift 2;;
        --gateway-url)       GATEWAY_URL="$2"; shift 2;;
        --admin-api-url)     ADMIN_API_URL="$2"; shift 2;;
        --ca-bundle)         CA_BUNDLE="$2"; shift 2;;
        --sign-gpg-key)      SIGN_GPG_KEY="$2"; shift 2;;
        -s|--skip-installer) SKIP_INSTALLER=1; shift;;
        -h|--help)           usage; exit 0;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2;;
    esac
done

# ---------------------------------------------------------------------------
# Layout:
#   PACKAGING_DIR = …/packaging-linux                          (this script's dir)
#   REPO_ROOT     = …/installer                                (build/dist output root)
#   PROJECT_DIR   = …/packaging/entrypoints/gateway-cli-v2     (real pyproject + src)
# The project is single-sourced from the sibling Windows packaging folder so the
# two installers can never drift; fall back to a co-located copy if present.
# ---------------------------------------------------------------------------
PACKAGING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$PACKAGING_DIR")"
PROJECT_DIR="$REPO_ROOT/packaging/entrypoints/gateway-cli-v2"
if [[ ! -f "$PROJECT_DIR/pyproject.toml" ]]; then
    PROJECT_DIR="$PACKAGING_DIR/entrypoints/gateway-cli-v2"
fi
if [[ ! -f "$PROJECT_DIR/pyproject.toml" ]]; then
    echo "ERROR: Project not found (expected pyproject.toml under packaging/ or packaging-linux/)." >&2
    exit 1
fi
# Run from REPO_ROOT so PyInstaller writes build/ and dist/ there; the spec
# resolves the project source relative to its own location, not the cwd.
cd "$REPO_ROOT"

step() { printf '\n==> %s\n' "$1"; }
warn() { printf 'WARNING: %s\n' "$1" >&2; }

# ---------------------------------------------------------------------------
step "Checking prerequisites"
# ---------------------------------------------------------------------------
ARCH="$(uname -m)"
if [[ "$ARCH" != "x86_64" && "$ARCH" != "aarch64" && "$ARCH" != "arm64" ]]; then
    warn "Unusual architecture '$ARCH' — the .run will only run on this same arch (PyInstaller does not cross-compile)."
fi

# PyInstaller does NOT cross-compile: the build must run on Linux, and the
# resulting bundle runs on the same OS/arch family. WSL counts as Linux.
if [[ "$(uname -s)" != "Linux" ]]; then
    echo "ERROR: This build must run on Linux (or WSL). PyInstaller does not cross-compile." >&2
    exit 1
fi

# Find a Python >= 3.11 (matches pyproject.toml). Prefer the newest available.
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        v="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
        if [[ -n "$v" ]] && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,11) else 1)'; then
            PYTHON="$candidate"
            echo "Using Python $v via '$candidate'"
            break
        fi
    fi
done
if [[ -z "$PYTHON" ]]; then
    echo "ERROR: No Python >= 3.11 found. Install it (the build machine needs Python; end users do not)." >&2
    exit 1
fi

if [[ -z "$VERSION" ]]; then
    VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$PROJECT_DIR/pyproject.toml" | head -n1)"
fi
echo "Building version: $VERSION"

# ---------------------------------------------------------------------------
step "Creating build venv"
# ---------------------------------------------------------------------------
VENV_DIR="$REPO_ROOT/.build-venv"
rm -rf "$VENV_DIR"
"$PYTHON" -m venv "$VENV_DIR"
PY="$VENV_DIR/bin/python"

PIP_ARGS=()
if [[ -n "$WHEEL_DIR" ]]; then
    if [[ ! -d "$WHEEL_DIR" ]]; then
        echo "ERROR: WheelDir not found: $WHEEL_DIR" >&2
        exit 1
    fi
    echo "Offline mode: installing from $WHEEL_DIR"
    PIP_ARGS=(--no-index --find-links "$WHEEL_DIR")
fi

# ---------------------------------------------------------------------------
step "Installing project and build tools into the venv"
# ---------------------------------------------------------------------------
"$PY" -m pip install --upgrade pip "${PIP_ARGS[@]}"
# pip builds the poetry-core project directly; no Poetry install needed.
"$PY" -m pip install "${PIP_ARGS[@]}" "$PROJECT_DIR" "pyinstaller>=6.11"

# ---------------------------------------------------------------------------
# Load site-config.json (guideline 1-1), if present. This is the single file a
# site admin edits to set the baked corporate defaults (OIDC, domains, CA path)
# without touching build.sh. It only fills values NOT already supplied via a
# --flag or GATEWAY_CLI_DEFAULT_* env var, so the documented precedence holds:
#   --flag  >  env var  >  site-config.json  >  site_defaults.py literal
# Edit packaging-linux/site-config.json (camelCase keys) to set the baked values.
# ---------------------------------------------------------------------------
SITE_CONFIG_INPUT="$PACKAGING_DIR/site-config.json"
if [[ -f "$SITE_CONFIG_INPUT" ]]; then
    step "Loading site-config.json"
    # Use the venv Python to parse JSON robustly (no jq dependency). Emit only
    # keys that are set in the file; the shell applies them where empty.
    read_json_key() {
        "$PY" - "$SITE_CONFIG_INPUT" "$1" <<'PYEOF'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)
val = data.get(sys.argv[2], "")
print(val if isinstance(val, str) else "")
PYEOF
    }
    apply_from_file() { # var_name  json_key
        local cur="${!1}"
        if [[ -z "$cur" ]]; then
            local val; val="$(read_json_key "$2")"
            if [[ -n "$val" ]]; then
                printf -v "$1" '%s' "$val"
                echo "  from site-config.json: $1"
            fi
        fi
    }
    apply_from_file OIDC_ISSUER_URL oidcIssuerUrl
    apply_from_file OIDC_CLIENT_ID  oidcClientId
    apply_from_file GATEWAY_URL     gatewayUrl
    apply_from_file ADMIN_API_URL   adminApiUrl
    apply_from_file CA_BUNDLE       caBundle
else
    echo ""
    echo "(no packaging-linux/site-config.json - using --flag/env/site_defaults.py values)"
fi

# ---------------------------------------------------------------------------
step "Baking corporate site defaults into the build"
# ---------------------------------------------------------------------------
# Generate cli/_site_config.py with the environment-specific values. Only the
# keys given here are emitted; anything omitted falls back to the literal
# default in cli/site_defaults.py. The file is build output, not source - it is
# overwritten every build and should not be committed.
SITE_CONFIG_PATH="$PROJECT_DIR/src/cli/_site_config.py"
"$PY" - "$SITE_CONFIG_PATH" \
    "$OIDC_ISSUER_URL" "$OIDC_CLIENT_ID" "$GATEWAY_URL" "$ADMIN_API_URL" "$CA_BUNDLE" <<'PYEOF'
import json, sys
path, issuer, client, gw, admin, ca = sys.argv[1:7]
lines = [
    "# Auto-generated by build.sh - DO NOT EDIT, DO NOT COMMIT.",
    "# Environment-specific corporate defaults baked into this build.",
    "",
]
for name, value in (
    ("OIDC_ISSUER_URL", issuer),
    ("OIDC_CLIENT_ID", client),
    ("GATEWAY_URL", gw),
    ("ADMIN_API_URL", admin),
    ("CA_BUNDLE", ca),
):
    if value.strip():
        lines.append("%s = %s" % (name, json.dumps(value)))
        print("  baked %s" % name)
with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
PYEOF
if [[ -z "${OIDC_ISSUER_URL// }" || -z "${OIDC_CLIENT_ID// }" ]]; then
    warn "OIDC issuer/client not supplied - the build will have blank OIDC defaults."
    warn "Users will need --oidc-issuer-url/--oidc-client-id or a card. Pass --oidc-issuer-url/--oidc-client-id to bake them."
fi

# Bundle the optional site-extra.json (guideline 1-4): the operator edits ONE JSON
# with custom managed/user keys and it is deep-merged into the settings files at
# setup time. Copied next to cli/site_extra.py so it ships inside the bundle;
# the spec collects cli/*.json. Absent -> injection is simply a no-op.
SITE_EXTRA_SRC="$PACKAGING_DIR/site-extra.json"
SITE_EXTRA_DST="$PROJECT_DIR/src/cli/site_extra.json"
if [[ -f "$SITE_EXTRA_SRC" ]]; then
    cp -f "$SITE_EXTRA_SRC" "$SITE_EXTRA_DST"
    echo "  bundled site-extra.json"
else
    rm -f "$SITE_EXTRA_DST"
    echo "  no site-extra.json (custom-key injection disabled)"
fi

# Re-install so the generated module is importable by PyInstaller's analysis.
"$PY" -m pip install "${PIP_ARGS[@]}" --no-deps --force-reinstall "$PROJECT_DIR" >/dev/null

# ---------------------------------------------------------------------------
step "Running PyInstaller"
# ---------------------------------------------------------------------------
export GATEWAY_CLI_VERSION="$VERSION"
"$PY" -m PyInstaller --noconfirm --clean "$PACKAGING_DIR/gateway_cli.spec"

# ---------------------------------------------------------------------------
step "Smoke-testing executables"
# ---------------------------------------------------------------------------
DIST_DIR="$REPO_ROOT/dist/gateway-cli-suite"
for exe in gateway-cli api-key-helper statusline; do
    path="$DIST_DIR/$exe"
    if [[ ! -x "$path" ]]; then
        echo "ERROR: Expected executable missing: $path" >&2
        exit 1
    fi
    if ! "$path" --help >/dev/null; then
        echo "ERROR: $exe --help failed - the bundle is likely missing a module. Re-run it manually to see the traceback." >&2
        exit 1
    fi
    echo "OK: $exe"
done

# ---------------------------------------------------------------------------
if [[ "$SKIP_INSTALLER" -eq 1 ]]; then
    step "Done (installer skipped)"
    echo "PyInstaller output: $DIST_DIR"
    echo "Ship it as a tarball:  tar -C '$REPO_ROOT/dist' -czf gateway-cli-suite-$VERSION.tar.gz gateway-cli-suite"
    exit 0
fi

# ---------------------------------------------------------------------------
step "Building self-extracting installer (.run)"
# ---------------------------------------------------------------------------
# The .run is the Linux analogue of the Windows setup.exe: a single file that,
# when executed, extracts the bundle and runs install.sh on the target machine.
# make_installer.sh assembles it from the PyInstaller output + install.sh.
INSTALLER_DIR="$REPO_ROOT/dist/installer"
RUN_PATH="$INSTALLER_DIR/gateway-cli-setup-$VERSION.run"
bash "$PACKAGING_DIR/make_installer.sh" \
    --dist-dir "$DIST_DIR" \
    --install-script "$PACKAGING_DIR/install.sh" \
    --version "$VERSION" \
    --output "$RUN_PATH"

# ---------------------------------------------------------------------------
step "Signing installer (optional)"
# ---------------------------------------------------------------------------
if [[ -n "$SIGN_GPG_KEY" ]]; then
    if ! command -v gpg >/dev/null 2>&1; then
        echo "ERROR: --sign-gpg-key was given but gpg is not installed." >&2
        exit 1
    fi
    gpg --batch --yes --local-user "$SIGN_GPG_KEY" \
        --output "$RUN_PATH.asc" --detach-sign --armor "$RUN_PATH"
    echo "  wrote detached signature: $(basename "$RUN_PATH.asc")"
    echo "  verify with:  gpg --verify '$RUN_PATH.asc' '$RUN_PATH'"
else
    warn "Installer is UNSIGNED (no --sign-gpg-key supplied)."
    warn "For a locked-down fleet, sign with a GPG key so recipients can verify integrity."
fi
# Always publish a SHA-256 checksum for basic integrity verification.
( cd "$INSTALLER_DIR" && sha256sum "$(basename "$RUN_PATH")" > "$(basename "$RUN_PATH").sha256" )
echo "  wrote checksum: $(basename "$RUN_PATH").sha256"

step "Build complete"
echo "Executables: $DIST_DIR"
echo "Installer:   $RUN_PATH"
echo "Deliver the .run to the isolated network and run it there:"
echo "  chmod +x gateway-cli-setup-$VERSION.run && ./gateway-cli-setup-$VERSION.run"
echo "  (per-user, no root) or:  sudo ./gateway-cli-setup-$VERSION.run   (system-wide)"
