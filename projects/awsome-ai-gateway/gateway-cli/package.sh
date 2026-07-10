#!/bin/bash
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# Package gateway-cli into a distributable tar.gz
set -euo pipefail

VERSION="0.1.0"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="$SCRIPT_DIR/dist"
PACKAGE_NAME="gateway-cli-${VERSION}"

rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR/$PACKAGE_NAME"

# Copy source
cp -r "$SCRIPT_DIR/src" "$DIST_DIR/$PACKAGE_NAME/src"
cp "$SCRIPT_DIR/pyproject.toml" "$DIST_DIR/$PACKAGE_NAME/"

# Create install script
cat > "$DIST_DIR/$PACKAGE_NAME/install.sh" << 'INSTALL_EOF'
#!/bin/bash
# gateway-cli installer
set -euo pipefail

PREFIX="${GATEWAY_CLI_PREFIX:-/usr/local}"
INSTALL_DIR="$PREFIX/lib/gateway-cli"
BIN_DIR="$PREFIX/bin"

echo "Installing gateway-cli to $INSTALL_DIR ..."

# Create install directory
sudo mkdir -p "$INSTALL_DIR"
sudo cp -r src pyproject.toml "$INSTALL_DIR/"

# Create venv and install
sudo python3 -m venv "$INSTALL_DIR/.venv"
sudo "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
sudo "$INSTALL_DIR/.venv/bin/pip" install -q -e "$INSTALL_DIR"

# Create wrapper scripts in /usr/local/bin
for cmd in gateway-cli api-key-helper statusline; do
    sudo tee "$BIN_DIR/$cmd" > /dev/null << EOF
#!/bin/bash
exec "$INSTALL_DIR/.venv/bin/$cmd" "\$@"
EOF
    sudo chmod +x "$BIN_DIR/$cmd"
done

echo ""
echo "Installed:"
echo "  gateway-cli    → $BIN_DIR/gateway-cli"
echo "  api-key-helper → $BIN_DIR/api-key-helper"
echo "  statusline     → $BIN_DIR/statusline"
echo ""
echo "Usage:"
echo "  gateway-cli setup --gateway-url http://your-gateway:8000"
echo "  gateway-cli status"
echo "  gateway-cli disable"
INSTALL_EOF
chmod +x "$DIST_DIR/$PACKAGE_NAME/install.sh"

# Create uninstall script
cat > "$DIST_DIR/$PACKAGE_NAME/uninstall.sh" << 'UNINSTALL_EOF'
#!/bin/bash
set -euo pipefail
PREFIX="${GATEWAY_CLI_PREFIX:-/usr/local}"
echo "Removing gateway-cli..."
sudo rm -rf "$PREFIX/lib/gateway-cli"
sudo rm -f "$PREFIX/bin/gateway-cli" "$PREFIX/bin/api-key-helper" "$PREFIX/bin/statusline"
echo "Done."
UNINSTALL_EOF
chmod +x "$DIST_DIR/$PACKAGE_NAME/uninstall.sh"

# Package
cd "$DIST_DIR"
tar -czf "${PACKAGE_NAME}-linux-amd64.tar.gz" "$PACKAGE_NAME"
echo "Created: $DIST_DIR/${PACKAGE_NAME}-linux-amd64.tar.gz"

# Create copies for other platforms (same Python source)
for variant in linux-arm64 darwin-amd64 darwin-arm64; do
    cp "${PACKAGE_NAME}-linux-amd64.tar.gz" "${PACKAGE_NAME}-${variant}.tar.gz"
done

# Windows zip
cd "$DIST_DIR"
cp -r "$PACKAGE_NAME" "${PACKAGE_NAME}-win"
# Replace install.sh with install.ps1 for Windows
cat > "${PACKAGE_NAME}-win/install.ps1" << 'PS1_EOF'
# gateway-cli installer for Windows
$ErrorActionPreference = "Stop"

$installDir = "$env:ProgramFiles\gateway-cli"
Write-Host "Installing gateway-cli to $installDir ..."

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item -Recurse -Force "src", "pyproject.toml" -Destination $installDir

python -m venv "$installDir\.venv"
& "$installDir\.venv\Scripts\pip" install -q --upgrade pip
& "$installDir\.venv\Scripts\pip" install -q -e $installDir

# Add to PATH via wrapper scripts
$binDir = "$installDir\bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null

foreach ($cmd in @("gateway-cli", "api-key-helper", "statusline")) {
    $wrapper = "@echo off`r`n`"$installDir\.venv\Scripts\$cmd.exe`" %*"
    Set-Content -Path "$binDir\$cmd.cmd" -Value $wrapper
}

# Add to user PATH
$userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
if ($userPath -notlike "*$binDir*") {
    [Environment]::SetEnvironmentVariable("PATH", "$userPath;$binDir", "User")
    Write-Host "Added $binDir to user PATH (restart terminal to take effect)"
}

Write-Host ""
Write-Host "Installed. Restart terminal, then run:"
Write-Host "  gateway-cli setup --gateway-url http://<gateway>:8000"
PS1_EOF

cat > "${PACKAGE_NAME}-win/uninstall.ps1" << 'PS1_EOF'
$installDir = "$env:ProgramFiles\gateway-cli"
Remove-Item -Recurse -Force $installDir -ErrorAction SilentlyContinue
Write-Host "Removed $installDir"
PS1_EOF

cd "$DIST_DIR"
if command -v zip &>/dev/null; then
    zip -qr "${PACKAGE_NAME}-windows-amd64.zip" "${PACKAGE_NAME}-win"
else
    tar -czf "${PACKAGE_NAME}-windows-amd64.zip" "${PACKAGE_NAME}-win"
fi
rm -rf "${PACKAGE_NAME}-win"
echo "Created Windows package"

ls -lh "$DIST_DIR"/*.tar.gz