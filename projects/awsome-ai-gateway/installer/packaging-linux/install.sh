#!/usr/bin/env bash
#
# On-target installer for the LLM Gateway CLI v2 Linux/WSL bundle. This is the
# Linux analogue of the [Setup]/[Files]/[Code] sections of installer.iss: it
# copies the shared runtime into place, wires the three CLIs onto PATH, and
# drops an uninstaller. It runs entirely offline.
#
# It is normally invoked by the self-extracting .run (which extracts the bundle
# next to this script first), but can also be run directly from an unpacked
# gateway-cli-suite/ tree.
#
# Install modes (mirrors Windows per-user vs per-machine):
#   - Run as a normal user            -> per-user install under ~/.local
#   - Run as root (sudo)              -> system-wide install under /opt + /usr/local/bin
#   - Override either with --prefix / --bindir.

set -euo pipefail

APP_NAME="LLM Gateway CLI"
SUITE_DIRNAME="gateway-cli-suite"
CLIS=(gateway-cli api-key-helper statusline)

# ---------------------------------------------------------------------------
# Defaults / arg parsing
# ---------------------------------------------------------------------------
PREFIX=""          # where the shared runtime folder is installed
BINDIR=""          # where the CLI symlinks/launchers go (must be on PATH)
ADD_TO_PATH=1      # append BINDIR to shell rc / profile.d (on by default)
QUIET=0

usage() {
    cat <<EOF
${APP_NAME} installer (version: ${GATEWAY_CLI_INSTALLER_VERSION:-unknown})

Usage: install.sh [options]

  --prefix DIR     Install the runtime under DIR/${SUITE_DIRNAME}.
                   Default: /opt (root) or ~/.local/share (per-user).
  --bindir DIR     Put the CLI launchers in DIR (must be on PATH).
                   Default: /usr/local/bin (root) or ~/.local/bin (per-user).
  --no-path        Do NOT modify PATH (skip shell rc / profile.d edits).
  --quiet          Less output.
  -h, --help       Show this help.

Silent / mass deployment example (Ansible, MDM, etc.):
  sudo ./gateway-cli-setup-<v>.run --quiet
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)  PREFIX="$2"; shift 2;;
        --bindir)  BINDIR="$2"; shift 2;;
        --no-path) ADD_TO_PATH=0; shift;;
        --quiet)   QUIET=1; shift;;
        -h|--help) usage; exit 0;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2;;
    esac
done

say()  { [[ "$QUIET" -eq 1 ]] || printf '%s\n' "$*"; }
step() { [[ "$QUIET" -eq 1 ]] || printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# Locate the bundle relative to this script (the .run extracts it as a sibling).
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_SUITE=""
for cand in "$SCRIPT_DIR/$SUITE_DIRNAME" "$SCRIPT_DIR/dist/$SUITE_DIRNAME" "$SCRIPT_DIR"; do
    if [[ -x "$cand/gateway-cli" ]]; then SRC_SUITE="$cand"; break; fi
done
if [[ -z "$SRC_SUITE" ]]; then
    echo "ERROR: Could not find the $SUITE_DIRNAME bundle next to install.sh." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Resolve install mode (root -> system-wide, else per-user), then paths.
# ---------------------------------------------------------------------------
if [[ "$(id -u)" -eq 0 ]]; then
    MODE="system"
    [[ -n "$PREFIX" ]] || PREFIX="/opt"
    [[ -n "$BINDIR" ]] || BINDIR="/usr/local/bin"
else
    MODE="user"
    [[ -n "$PREFIX" ]] || PREFIX="$HOME/.local/share"
    [[ -n "$BINDIR" ]] || BINDIR="$HOME/.local/bin"
fi
INSTALL_DIR="$PREFIX/$SUITE_DIRNAME"

step "Installing ${APP_NAME} (${MODE} mode)"
say  "  runtime -> $INSTALL_DIR"
say  "  launchers -> $BINDIR"

# ---------------------------------------------------------------------------
# Copy the runtime into place (replace any previous install = upgrade).
# ---------------------------------------------------------------------------
mkdir -p "$PREFIX" "$BINDIR"
if [[ -e "$INSTALL_DIR" ]]; then
    say "  removing previous install at $INSTALL_DIR"
    rm -rf "$INSTALL_DIR"
fi
cp -a "$SRC_SUITE" "$INSTALL_DIR"
chmod +x "$INSTALL_DIR"/gateway-cli "$INSTALL_DIR"/api-key-helper "$INSTALL_DIR"/statusline 2>/dev/null || true

# ---------------------------------------------------------------------------
# Launchers: symlink each CLI from BINDIR into the install dir. The three exes
# share the _internal runtime folder, so they MUST be launched from within the
# install dir — a symlink preserves that (PyInstaller resolves _internal via the
# real executable location, which the symlink points at).
# ---------------------------------------------------------------------------
for cli in "${CLIS[@]}"; do
    ln -sf "$INSTALL_DIR/$cli" "$BINDIR/$cli"
done

# ---------------------------------------------------------------------------
# PATH handling (analogue of installer.iss AddDirToPath). If BINDIR is a
# standard location already on PATH (e.g. /usr/local/bin), nothing to do. For a
# per-user ~/.local/bin that many distros don't pre-add, append an export line.
# ---------------------------------------------------------------------------
path_has_dir() { case ":${PATH}:" in *":$1:"*) return 0;; *) return 1;; esac; }

PATH_LINE="export PATH=\"$BINDIR:\$PATH\""
PATH_MARKER="# added by ${APP_NAME} installer"

add_to_path() {
    if path_has_dir "$BINDIR"; then
        say "  $BINDIR is already on PATH"
        return
    fi
    if [[ "$MODE" == "system" ]]; then
        # System-wide: a profile.d snippet is sourced by all login shells.
        local snippet="/etc/profile.d/gateway-cli.sh"
        printf '%s\n%s\n' "$PATH_MARKER" "$PATH_LINE" > "$snippet"
        chmod 0644 "$snippet"
        say "  added $BINDIR to PATH via $snippet (new login shells)"
    else
        # Per-user: append to the rc files of the shells the user is likely to
        # use. Idempotent — skip if our marker is already present.
        local touched=0
        for rc in "$HOME/.bashrc" "$HOME/.profile" "$HOME/.zshrc"; do
            [[ -e "$rc" ]] || continue
            if ! grep -qF "$PATH_MARKER" "$rc" 2>/dev/null; then
                printf '\n%s\n%s\n' "$PATH_MARKER" "$PATH_LINE" >> "$rc"
                say "  added $BINDIR to PATH in $rc"
                touched=1
            fi
        done
        # Ensure at least one rc file carries the line even on a fresh home.
        if [[ "$touched" -eq 0 ]] && ! grep -qsF "$PATH_MARKER" "$HOME/.profile" 2>/dev/null; then
            printf '\n%s\n%s\n' "$PATH_MARKER" "$PATH_LINE" >> "$HOME/.profile"
            say "  added $BINDIR to PATH in $HOME/.profile"
        fi
        say "  open a new terminal (or 'source ~/.bashrc') to pick up PATH"
    fi
}

if [[ "$ADD_TO_PATH" -eq 1 ]]; then
    add_to_path
else
    if ! path_has_dir "$BINDIR"; then
        warn "$BINDIR is not on PATH and --no-path was given; add it yourself or call the CLIs by full path."
    fi
fi

# ---------------------------------------------------------------------------
# Uninstaller (analogue of Windows Apps & Features + PATH cleanup). Removes the
# runtime, the launchers, and the PATH edits this installer made.
# ---------------------------------------------------------------------------
UNINSTALL="$INSTALL_DIR/uninstall.sh"
cat > "$UNINSTALL" <<UNEOF
#!/usr/bin/env bash
# Uninstalls ${APP_NAME}. Run with the same privileges used to install
# (sudo for a system install).
set -euo pipefail
INSTALL_DIR="$INSTALL_DIR"
BINDIR="$BINDIR"
MODE="$MODE"
PATH_MARKER="$PATH_MARKER"
echo "Removing ${APP_NAME} from \$INSTALL_DIR"
for cli in ${CLIS[*]}; do
    [[ -L "\$BINDIR/\$cli" ]] && rm -f "\$BINDIR/\$cli"
done
if [[ "\$MODE" == "system" ]]; then
    rm -f /etc/profile.d/gateway-cli.sh
else
    # Strip the two-line PATH block (marker + the export line after it) from
    # each rc file. awk into a temp file is portable across GNU/BSD sed quirks.
    for rc in "\$HOME/.bashrc" "\$HOME/.profile" "\$HOME/.zshrc"; do
        [[ -e "\$rc" ]] || continue
        tmp="\$(mktemp)"
        awk -v marker="\$PATH_MARKER" '
            \$0 == marker { skip = 2 }
            skip > 0      { skip--; next }
            { print }
        ' "\$rc" > "\$tmp" && cat "\$tmp" > "\$rc"
        rm -f "\$tmp"
    done
fi
rm -rf "\$INSTALL_DIR"
echo "Done. Open a new terminal for the PATH change to take effect."
UNEOF
chmod +x "$UNINSTALL"

step "Installation complete"
say "Installed CLIs: ${CLIS[*]}"
say "Uninstall with: $UNINSTALL"
if ! path_has_dir "$BINDIR"; then
    say "Try it now:     $BINDIR/gateway-cli --help"
else
    say "Try it now:     gateway-cli --help"
fi
