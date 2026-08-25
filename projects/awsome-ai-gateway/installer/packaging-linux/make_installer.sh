#!/usr/bin/env bash
#
# Assembles a single self-extracting installer (.run) from the PyInstaller
# onedir output. This is the Linux analogue of compiling installer.iss with
# Inno Setup on Windows: the result is one file that, when executed, extracts
# the bundle to a temp dir and runs the embedded install.sh on the target.
#
# The .run is a shell stub followed by a gzip-compressed tar payload. Running it
# reads the stub (plain shell), then pipes the payload past the stub offset into
# `tar`. No Python, makeself, or network access is required on either machine —
# only POSIX sh, tail, and tar, which every Linux/WSL distro ships.
#
# Usage:
#   make_installer.sh --dist-dir DIR --install-script FILE --version VER --output FILE.run

set -euo pipefail

DIST_DIR=""
INSTALL_SCRIPT=""
VERSION=""
OUTPUT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dist-dir)       DIST_DIR="$2"; shift 2;;
        --install-script) INSTALL_SCRIPT="$2"; shift 2;;
        --version)        VERSION="$2"; shift 2;;
        --output)         OUTPUT="$2"; shift 2;;
        *) echo "Unknown option: $1" >&2; exit 2;;
    esac
done

for req in DIST_DIR INSTALL_SCRIPT VERSION OUTPUT; do
    if [[ -z "${!req}" ]]; then echo "ERROR: --${req,,} is required" >&2; exit 2; fi
done
if [[ ! -d "$DIST_DIR" ]]; then echo "ERROR: dist dir not found: $DIST_DIR" >&2; exit 1; fi
if [[ ! -f "$INSTALL_SCRIPT" ]]; then echo "ERROR: install script not found: $INSTALL_SCRIPT" >&2; exit 1; fi

mkdir -p "$(dirname "$OUTPUT")"

# ---------------------------------------------------------------------------
# Stage the payload: the bundle plus install.sh, all under one top-level dir so
# the extracted tree is tidy (gateway-cli-suite/ + install.sh next to it).
# ---------------------------------------------------------------------------
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

PAYLOAD_ROOT="$STAGE/payload"
mkdir -p "$PAYLOAD_ROOT"
cp -a "$DIST_DIR" "$PAYLOAD_ROOT/gateway-cli-suite"
cp -f "$INSTALL_SCRIPT" "$PAYLOAD_ROOT/install.sh"
chmod +x "$PAYLOAD_ROOT/install.sh"

PAYLOAD_TGZ="$STAGE/payload.tar.gz"
# Deterministic-ish tarball; --numeric-owner keeps it root-agnostic on extract.
tar --numeric-owner -C "$PAYLOAD_ROOT" -czf "$PAYLOAD_TGZ" .

# ---------------------------------------------------------------------------
# Write the self-extracting stub, then append the payload. The stub finds the
# line after the __PAYLOAD_BELOW__ marker and streams from there into tar.
# ---------------------------------------------------------------------------
STUB="$STAGE/stub.sh"
cat > "$STUB" <<STUBEOF
#!/usr/bin/env bash
#
# LLM Gateway CLI v2 - self-extracting offline installer
# Version: ${VERSION}
#
# Runs anywhere with bash + tar. Extracts the bundle to a temp dir and hands
# off to the embedded install.sh. Pass through any install.sh flags, e.g.:
#   ./gateway-cli-setup-${VERSION}.run --prefix /opt/gateway-cli --no-path
#   sudo ./gateway-cli-setup-${VERSION}.run              # system-wide
#   ./gateway-cli-setup-${VERSION}.run --extract-only DIR # just unpack, no install
set -euo pipefail

GATEWAY_CLI_INSTALLER_VERSION="${VERSION}"
export GATEWAY_CLI_INSTALLER_VERSION

SELF="\$0"
# Byte offset where the tar payload begins (line right after the marker).
MARKER_LINE=\$(awk '/^__PAYLOAD_BELOW__\$/ {print NR + 1; exit 0}' "\$SELF")

WORK="\$(mktemp -d "\${TMPDIR:-/tmp}/gateway-cli-setup.XXXXXX")"
cleanup() { rm -rf "\$WORK"; }
trap cleanup EXIT

tail -n +"\$MARKER_LINE" "\$SELF" | tar -xz -C "\$WORK"

# --extract-only DIR: unpack the bundle without running the installer.
if [[ "\${1:-}" == "--extract-only" ]]; then
    DEST="\${2:?--extract-only requires a destination directory}"
    mkdir -p "\$DEST"
    cp -a "\$WORK/gateway-cli-suite" "\$DEST/"
    echo "Extracted gateway-cli-suite to: \$DEST/gateway-cli-suite"
    exit 0
fi

exec bash "\$WORK/install.sh" "\$@"
__PAYLOAD_BELOW__
STUBEOF

# Concatenate stub + payload into the final .run and make it executable.
cat "$STUB" "$PAYLOAD_TGZ" > "$OUTPUT"
chmod +x "$OUTPUT"

SIZE="$(du -h "$OUTPUT" | cut -f1)"
echo "  wrote installer: $OUTPUT ($SIZE)"
