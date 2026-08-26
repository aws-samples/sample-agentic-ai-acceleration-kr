#!/usr/bin/env bash
#
# Builds a native Debian/Ubuntu package (.deb) for the LLM Gateway CLI v2 suite.
#
# This is a THIN packaging layer on top of the shared, distro-agnostic payload
# produced by ../build.sh (dist/gateway-cli-suite/ — the PyInstaller onedir with
# an embedded CPython runtime). It does NOT rebuild the CLI; it only wraps the
# already-built payload in a .deb so dpkg/apt give the native experience:
#
#   sudo apt install ./gateway-cli-suite_<version>_<arch>.deb
#   gateway-cli login && gateway-cli setup --model ... && claude
#   sudo apt remove gateway-cli-suite          # clean uninstall
#
# Why a .deb instead of the .run self-extractor:
#   - Installs to /opt/gateway-cli-suite and symlinks the three CLIs into
#     /usr/bin — already on PATH, so NO shell-rc editing is needed.
#   - dpkg tracks every file, so `apt remove` uninstalls cleanly and upgrades
#     are handled by the package manager (dpkg --compare-versions).
#   - No Python on the target (the runtime is embedded, same as the .run).
#
# Modularity: another distro reuses the SAME dist/gateway-cli-suite/ payload via
# a sibling builder (e.g. ../rpm/build-rpm.sh). Only this packaging format
# differs; the application bundle is identical across all of them.
#
# Usage:
#   ./build-deb.sh [--suite-dir DIR] [--version VER] [--arch ARCH] [--output FILE]
#
# Requires only `dpkg-deb` (present on any Debian/Ubuntu build host, and
# installable elsewhere via the `dpkg` package). No network access.

set -euo pipefail

# ---------------------------------------------------------------------------
# Layout
#   DEB_DIR       = …/packaging-linux/deb           (this script's dir)
#   PACKAGING_DIR = …/packaging-linux
#   REPO_ROOT     = …/installer                     (build/dist output root)
# ---------------------------------------------------------------------------
DEB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGING_DIR="$(dirname "$DEB_DIR")"
REPO_ROOT="$(dirname "$PACKAGING_DIR")"

PACKAGE_NAME="gateway-cli-suite"
SUITE_DIRNAME="gateway-cli-suite"
INSTALL_PREFIX="/opt"                 # runtime lives here (FHS: add-on software)
BIN_DIR="/usr/bin"                    # symlinks here — always on PATH
CLIS=(gateway-cli api-key-helper statusline)
MAINTAINER="LLM Gateway CLI <noreply@example.com>"

SUITE_DIR=""                          # PyInstaller output; default resolved below
VERSION=""
ARCH=""
OUTPUT=""

usage() {
    sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --suite-dir DIR   PyInstaller onedir payload (default: ../dist/gateway-cli-suite).
  --version VER     Package version (default: pyproject.toml version).
  --arch ARCH       Debian arch: amd64 | arm64 (default: derived from uname -m).
  --output FILE     Output .deb path (default: ../dist/installer/<name>_<v>_<arch>.deb).
  -h, --help        Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --suite-dir) SUITE_DIR="$2"; shift 2;;
        --version)   VERSION="$2"; shift 2;;
        --arch)      ARCH="$2"; shift 2;;
        --output)    OUTPUT="$2"; shift 2;;
        -h|--help)   usage; exit 0;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2;;
    esac
done

step() { printf '\n==> %s\n' "$1"; }
warn() { printf 'WARNING: %s\n' "$1" >&2; }

# ---------------------------------------------------------------------------
step "Checking prerequisites"
# ---------------------------------------------------------------------------
if ! command -v dpkg-deb >/dev/null 2>&1; then
    echo "ERROR: dpkg-deb not found. Install it with: sudo apt-get install dpkg-dev" >&2
    exit 1
fi

# Default the payload to the shared build output.
[[ -n "$SUITE_DIR" ]] || SUITE_DIR="$REPO_ROOT/dist/$SUITE_DIRNAME"
if [[ ! -x "$SUITE_DIR/gateway-cli" ]]; then
    echo "ERROR: payload not found at $SUITE_DIR (expected the gateway-cli binary)." >&2
    echo "       Build it first from packaging-linux/:  ./build.sh --skip-installer" >&2
    exit 1
fi

# Version: reuse the same source of truth as build.sh (pyproject.toml).
if [[ -z "$VERSION" ]]; then
    PROJECT_DIR="$REPO_ROOT/packaging/entrypoints/gateway-cli-v2"
    [[ -f "$PROJECT_DIR/pyproject.toml" ]] || PROJECT_DIR="$PACKAGING_DIR/entrypoints/gateway-cli-v2"
    VERSION="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$PROJECT_DIR/pyproject.toml" | head -n1)"
fi
[[ -n "$VERSION" ]] || { echo "ERROR: could not determine version; pass --version." >&2; exit 1; }

# Debian architecture name (differs from uname -m).
if [[ -z "$ARCH" ]]; then
    case "$(uname -m)" in
        x86_64)          ARCH="amd64";;
        aarch64|arm64)   ARCH="arm64";;
        *) echo "ERROR: unsupported arch $(uname -m); pass --arch." >&2; exit 1;;
    esac
fi

echo "Package: $PACKAGE_NAME  Version: $VERSION  Arch: $ARCH"
echo "Payload: $SUITE_DIR"

# ---------------------------------------------------------------------------
step "Staging package tree"
# ---------------------------------------------------------------------------
# Build a throwaway DESTDIR mirroring the on-target filesystem, then let
# dpkg-deb pack it. Everything under here maps 1:1 to installed paths.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

INSTALL_DIR="$INSTALL_PREFIX/$SUITE_DIRNAME"          # e.g. /opt/gateway-cli-suite
mkdir -p "$STAGE$INSTALL_DIR" "$STAGE$BIN_DIR" "$STAGE/DEBIAN"

# Copy the runtime payload. -a preserves the executable bits and the _internal
# shared-runtime folder the three CLIs load relative to their real location.
cp -a "$SUITE_DIR/." "$STAGE$INSTALL_DIR/"

# Symlinks into /usr/bin. They MUST point at the real binary inside the install
# dir (not a copy) so PyInstaller resolves _internal from the install location.
for cli in "${CLIS[@]}"; do
    ln -sf "$INSTALL_DIR/$cli" "$STAGE$BIN_DIR/$cli"
done

# Installed-Size (KiB) — apt shows it and users expect the field present.
INSTALLED_SIZE="$(du -sk "$STAGE$INSTALL_DIR" | cut -f1)"

# ---------------------------------------------------------------------------
step "Writing control metadata"
# ---------------------------------------------------------------------------
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PACKAGE_NAME
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Maintainer: $MAINTAINER
Installed-Size: $INSTALLED_SIZE
Description: LLM Gateway CLI v2 suite (gateway-cli, api-key-helper, statusline)
 Self-contained onboarding CLI for the LLM Gateway. Bundles its own
 Python runtime, so no system Python is required. Corporate defaults (OIDC,
 gateway URL, CA bundle) are baked into the build.
EOF

# postrm: drop the /usr/bin symlinks on remove/purge. dpkg removes the packaged
# symlinks itself, but this guards against a symlink an admin re-pointed.
cat > "$STAGE/DEBIAN/postrm" <<EOF
#!/bin/sh
set -e
if [ "\$1" = "remove" ] || [ "\$1" = "purge" ]; then
    for cli in ${CLIS[*]}; do
        [ -L "$BIN_DIR/\$cli" ] && rm -f "$BIN_DIR/\$cli" || true
    done
fi
exit 0
EOF
chmod 0755 "$STAGE/DEBIAN/postrm"

# ---------------------------------------------------------------------------
step "Building .deb"
# ---------------------------------------------------------------------------
[[ -n "$OUTPUT" ]] || OUTPUT="$REPO_ROOT/dist/installer/${PACKAGE_NAME}_${VERSION}_${ARCH}.deb"
mkdir -p "$(dirname "$OUTPUT")"
# --root-owner-group keeps installed files root:root without needing fakeroot.
dpkg-deb --root-owner-group --build "$STAGE" "$OUTPUT"

# Checksum for integrity, mirroring build.sh's .run flow.
( cd "$(dirname "$OUTPUT")" && sha256sum "$(basename "$OUTPUT")" > "$(basename "$OUTPUT").sha256" )

step "Build complete"
echo "Package:  $OUTPUT"
echo "Checksum: $OUTPUT.sha256"
echo ""
echo "Install on Debian/Ubuntu:"
echo "  sudo apt install $OUTPUT      # resolves nothing extra; runtime is embedded"
echo "  # or: sudo dpkg -i $OUTPUT"
echo "Then:"
echo "  gateway-cli login && gateway-cli setup --model sonnet && claude"
echo "Uninstall:"
echo "  sudo apt remove $PACKAGE_NAME"
