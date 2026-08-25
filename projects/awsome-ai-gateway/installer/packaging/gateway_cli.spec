# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the LLM Gateway CLI v2 Windows distribution.

Builds three console executables that share one runtime folder (one copy of
python DLLs, botocore data, certifi CA bundle, ...):

    dist/gateway-cli-suite/
        gateway-cli.exe
        api-key-helper.exe
        statusline.exe
        _internal/           <- shared Python runtime + dependencies

Run from the repository root (the project must be pip-installed into the
build venv first so the packages are importable by the analysis hooks):

    pyinstaller --noconfirm --clean packaging/gateway_cli.spec
"""

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

APP_VERSION = os.environ.get("GATEWAY_CLI_VERSION", "0.1.0")

# ---------------------------------------------------------------------------
# Resolve paths relative to THIS spec file, not the current working directory.
# The real project (pyproject.toml + src/) is nested under
# packaging/entrypoints/gateway-cli-v2/, while the entry-point shims live in
# packaging/entrypoints/. SPECPATH is the directory containing this spec
# (…/packaging), injected by PyInstaller.
# ---------------------------------------------------------------------------
ENTRYPOINTS_DIR = os.path.join(SPECPATH, "entrypoints")
PROJECT_SRC = os.path.join(ENTRYPOINTS_DIR, "gateway-cli-v2", "src")

# Ensure the analysis hooks (collect_submodules/collect_data_files) can import
# the local packages even if the project was not pip-installed into the venv.
if PROJECT_SRC not in sys.path:
    sys.path.insert(0, PROJECT_SRC)

# ---------------------------------------------------------------------------
# Shared analysis inputs
# ---------------------------------------------------------------------------

# Our own packages use Click groups / dynamic dispatch, so pull in every
# submodule explicitly rather than trusting static import analysis.
local_submodules = (
    collect_submodules("cli")
    + collect_submodules("api_key_helper")
    + collect_submodules("gateway_cli_oidc")
    + collect_submodules("statusline")
)

# boto3/botocore build clients from JSON service models at runtime, and
# requests resolves certifi lazily. pyinstaller-hooks-contrib handles most of
# this, but we collect explicitly so the build does not silently regress if
# hook behaviour changes.
hiddenimports = local_submodules + [
    "boto3",
    "botocore",
    "configparser",  # used by botocore credential resolution
    "certifi",
    "requests",
    "urllib3",
    # truststore routes TLS verification through the OS trust store (Windows
    # SChannel) so the corporate proxy CA validates under OpenSSL 3.x. It is
    # imported lazily inside enable_os_trust_store(), so static analysis misses it.
    "truststore",
    "yaml",
    "click",
    "structlog",
    "platformdirs",
    # stdlib modules loaded dynamically in places static analysis can miss
    "socket",
    "subprocess",
    "ssl",
]

datas = (
    collect_data_files("botocore")   # JSON service definitions
    + collect_data_files("boto3")
    + collect_data_files("certifi")  # cacert.pem for requests/urllib3
)

# Bundle the optional build-time site-extra.json (guideline 1-4) beside the cli
# package so cli.site_extra can read it from the frozen bundle. build.ps1 copies
# packaging/site-extra.json -> src/cli/site_extra.json before this runs; when it
# is absent the injection is simply a no-op.
_site_extra = os.path.join(PROJECT_SRC, "cli", "site_extra.json")
if os.path.isfile(_site_extra):
    datas += [(_site_extra, "cli")]

excludes = [
    "tkinter",
    "pytest",
    "responses",
    "ruff",
    "pip",
    "setuptools",
    "wheel",
]

def analysis_kwargs():
    # Analysis mutates the sequences it is given, so hand each of the three
    # analyses its own copies.
    return dict(
        pathex=[PROJECT_SRC],
        binaries=[],
        datas=list(datas),
        hiddenimports=list(hiddenimports),
        hookspath=[],
        runtime_hooks=[],
        excludes=list(excludes),
        noarchive=False,
    )


# ---------------------------------------------------------------------------
# One Analysis / PYZ / EXE per console script
# ---------------------------------------------------------------------------

a_cli = Analysis([os.path.join(ENTRYPOINTS_DIR, "gateway_cli_entry.py")], **analysis_kwargs())
a_helper = Analysis([os.path.join(ENTRYPOINTS_DIR, "api_key_helper_entry.py")], **analysis_kwargs())
a_status = Analysis([os.path.join(ENTRYPOINTS_DIR, "statusline_entry.py")], **analysis_kwargs())

pyz_cli = PYZ(a_cli.pure)
pyz_helper = PYZ(a_helper.pure)
pyz_status = PYZ(a_status.pure)


def make_exe(pyz, analysis, name):
    return EXE(
        pyz,
        analysis.scripts,
        [],
        exclude_binaries=True,   # onedir mode: binaries live in COLLECT below
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,               # UPX trips AV scanners on locked-down networks
        console=True,            # all three are CLI tools
        disable_windowed_traceback=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )


exe_cli = make_exe(pyz_cli, a_cli, "gateway-cli")
exe_helper = make_exe(pyz_helper, a_helper, "api-key-helper")
exe_status = make_exe(pyz_status, a_status, "statusline")

# ---------------------------------------------------------------------------
# Single COLLECT -> one dist folder, dependencies deduplicated across the
# three executables.
# ---------------------------------------------------------------------------

coll = COLLECT(
    exe_cli,
    a_cli.binaries,
    a_cli.datas,
    exe_helper,
    a_helper.binaries,
    a_helper.datas,
    exe_status,
    a_status.binaries,
    a_status.datas,
    strip=False,
    upx=False,
    name="gateway-cli-suite",
)
