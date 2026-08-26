#!/usr/bin/env bash
#
# Pre-downloads all build dependencies as wheels for an air-gapped build.
#
# Run this on an INTERNET-CONNECTED Linux x86_64 machine with the same Python
# minor version, distro glibc, and architecture as the build machine. It fills a
# wheel directory with the project's runtime dependencies plus pip/PyInstaller.
#
# Copy the resulting directory (plus this repository) to the offline build
# machine, then run:
#
#   ./build.sh --wheel-dir /path/to/wheels
#
# Usage:
#   ./download_wheels.sh [--out-dir DIR]     (default: ./wheels)

set -euo pipefail

OUT_DIR="wheels"
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--out-dir) OUT_DIR="$2"; shift 2;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
        *) echo "Unknown option: $1" >&2; exit 2;;
    esac
done

PACKAGING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$PACKAGING_DIR")"
# The project is single-sourced from the sibling Windows packaging folder.
PROJECT_DIR="$REPO_ROOT/packaging/entrypoints/gateway-cli-v2"
if [[ ! -f "$PROJECT_DIR/pyproject.toml" ]]; then
    PROJECT_DIR="$PACKAGING_DIR/entrypoints/gateway-cli-v2"
fi
cd "$REPO_ROOT"

# Prefer the newest Python >= 3.11 so wheels match what build.sh will select.
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then PYTHON="$candidate"; break; fi
done
if [[ -z "$PYTHON" ]]; then echo "ERROR: no python found" >&2; exit 1; fi

mkdir -p "$OUT_DIR"

# Wheels must match the build machine: Linux + same arch + same Python minor +
# compatible glibc. Running this on that same platform guarantees it. Many of
# these deps (boto3, PyInstaller bootloader) ship platform-specific wheels, so
# do NOT reuse a wheel cache built on a different distro/arch.
"$PYTHON" -m pip download --dest "$OUT_DIR" \
    "$PROJECT_DIR" \
    "pyinstaller>=6.11" \
    "pip" "setuptools" "wheel"

echo ""
echo "Wheel cache ready: $OUT_DIR"
echo "Copy it to the build machine and pass --wheel-dir to build.sh."
