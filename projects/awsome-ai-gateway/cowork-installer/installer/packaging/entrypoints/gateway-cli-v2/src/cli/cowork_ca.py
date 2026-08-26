# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Install the corporate TLS CA into the OS trust store for Cowork.

Cowork is Claude Desktop, an Electron/Chromium app. Chromium validates TLS
against the OS trust store (Windows CryptoAPI ``Root`` store; macOS System
keychain), NOT the ``NODE_EXTRA_CA_CERTS`` / ``REQUESTS_CA_BUNDLE`` /
``SSL_CERT_FILE`` env vars that Claude Code (a Node CLI) honours. Behind a
TLS-intercepting egress proxy, the proxy re-signs Cowork's mandatory bundle
fetch with a corporate CA; unless that CA is in the OS store the handshake
fails. The Python helper processes route TLS through the OS store too (via
``truststore``), so one install covers them without any env vars.

Scope: strictly the *egress-proxy* surface. The inference URL
(``inferenceGatewayBaseUrl``) is publicly trusted (CloudFront) and unaffected.
See docs/installer-key-differences.md §4.

Design:
- PEM source: ``site_defaults.configured_ca_bundle()`` (env
  ``GATEWAY_CLI_CA_BUNDLE`` or the baked ``DEFAULT_CA_BUNDLE``).
- ``install`` records a small backup JSON (thumbprint + store) under
  ``data_dir()`` so ``restore`` removes **only** the cert this tool installed —
  never a pre-existing corporate CA.
- ``install`` is idempotent: a no-op if the CA (matched by fingerprint) is
  already trusted.
- **Fingerprint pin (security):** a build-time SHA-256 of the expected CA is
  baked in (``site_defaults.expected_ca_sha256``); ``install`` refuses any PEM
  whose SHA-256 does not match, so a swapped/attacker PEM cannot become a
  trusted root. ``--force`` overrides for a legitimate CA rotation. We do NOT
  enforce *critical* BasicConstraints — the real corporate CA's basicConstraints
  are non-critical (the very deviation that forces OS-store routing), so
  enforcement would reject the legitimate CA; pinning is the right control.

Everything is best-effort and returns structured results.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from cli import site_defaults
from cli.paths import data_dir

log = structlog.get_logger(component="cowork-ca")

# Backup of what *this tool* installed, so restore removes only that cert.
_BACKUP_NAME = "cowork-ca-backup.json"


def _backup_path() -> Path:
    return data_dir() / _BACKUP_NAME


@dataclass
class CaResult:
    """Outcome of a CA operation.

    ok       — the operation succeeded (or was already in the desired state).
    changed  — the trust store was modified by this call.
    detail   — a short human-readable status line.
    store    — which OS store was touched, when applicable.
    warnings — non-fatal notes to surface (e.g. a forced pin override).
    """

    ok: bool
    changed: bool
    detail: str
    store: str | None = None
    warnings: list[str] = field(default_factory=list)


# --- PEM parsing (no external crypto dependency) -----------------------------

def _read_der(pem_path: Path) -> bytes:
    """Return the DER bytes of the first certificate in a PEM/DER file.

    Accepts either a PEM (``-----BEGIN CERTIFICATE-----`` base64) or raw DER.
    Kept dependency-free (stdlib only) so it works in the frozen PyInstaller build.
    """
    raw = pem_path.read_bytes()
    text = raw.decode("ascii", errors="ignore")
    marker = "-----BEGIN CERTIFICATE-----"
    if marker in text:
        body = text.split(marker, 1)[1].split("-----END CERTIFICATE-----", 1)[0]
        b64 = "".join(body.split())
        return base64.b64decode(b64)
    # Assume raw DER.
    return raw


def ca_fingerprint(pem_path: Path) -> str:
    """SHA-1 fingerprint (uppercase hex, no separators) of the CA cert.

    SHA-1 (a.k.a. "thumbprint") is what the Windows cert store keys on, so using
    it lets us match/remove by the same identifier PowerShell reports. This is an
    *identity* used for store bookkeeping, NOT the security pin — the pin is the
    SHA-256 in :func:`ca_sha256` (SHA-1 is collision-broken and unfit for it).
    """
    return hashlib.sha1(_read_der(pem_path)).hexdigest().upper()


def ca_sha256(pem_path: Path) -> str:
    """SHA-256 fingerprint (uppercase hex, no separators) of the CA cert.

    This is the value the build-time pin (``site_defaults.expected_ca_sha256``)
    is compared against — the security control that stops a swapped/attacker PEM
    from being installed as a trusted root.
    """
    return hashlib.sha256(_read_der(pem_path)).hexdigest().upper()


# --- Fingerprint pinning (the security control) ------------------------------
# Pin a build-time SHA-256 of the expected corporate CA and refuse to install any
# PEM whose fingerprint does not match, so a swapped/attacker PEM at the baked
# path — or a hostile GATEWAY_CLI_CA_BUNDLE override — cannot become a trusted OS
# root. Two explicit escape hatches:
#   * a pin match always installs (the path it came from is irrelevant);
#   * `--force` overrides a refusal, for a genuine CA rotation / dev case.
#
# We deliberately do NOT enforce *critical* BasicConstraints: the real corporate
# CA carries non-critical basicConstraints (the deviation that is itself why
# Chromium routes it through the OS store), so strict enforcement would reject the
# legitimate CA. Fingerprint pinning is the correct control.

def _evaluate_pin(pem_path: Path, *, force: bool) -> tuple[str | None, str | None]:
    """Decide whether ``pem_path`` may be installed.

    Returns ``(refusal, warning)``:
    - ``refusal`` is a message when the install must be blocked (None = proceed).
      ``force`` downgrades any refusal to a warning so it never blocks.
    - ``warning`` is a non-fatal note to surface even on a permitted install
      (e.g. a forced override, or an unpinned env-supplied PEM).
    """
    expected = site_defaults.expected_ca_sha256()
    actual = ca_sha256(pem_path)
    is_override = site_defaults.ca_bundle_is_env_override()

    if expected:
        if actual == expected:
            return None, None  # matches the pin — trusted regardless of source
        detail = (
            "refusing to install: CA fingerprint does not match the pinned value.\n"
            f"      expected SHA-256: {expected}\n"
            f"      actual   SHA-256: {actual}\n"
            "      This PEM is not the CA this build was pinned to. If the "
            "corporate CA was legitimately rotated, re-run with --force."
        )
        if force:
            return None, f"OVERRIDE (--force): installing an unpinned CA — {actual}"
        return detail, None

    # No pin baked (dev build). A baked-default PEM is trusted by construction; an
    # env-override PEM is a deliberate runtime deviation, so require --force.
    if is_override and not force:
        return (
            "refusing to install a CA supplied via GATEWAY_CLI_CA_BUNDLE while no "
            "fingerprint is pinned in this build.\n"
            f"      PEM SHA-256: {actual}\n"
            "      Re-run with --force to install this override deliberately.",
            None,
        )
    if is_override:
        return None, f"OVERRIDE (--force): installing env-supplied CA — {actual}"
    return None, None


# --- PEM resolution ----------------------------------------------------------

def resolve_pem() -> Path | None:
    """The corporate CA PEM path if it exists on this machine, else None."""
    configured = site_defaults.configured_ca_bundle()
    if not configured:
        return None
    p = Path(configured)
    return p if p.is_file() else None


# --- PowerShell plumbing (Windows) -------------------------------------------

def _run_powershell(script: str, timeout: int = 30) -> str:
    # Pin both ends to UTF-8. On a non-UTF-8 Windows (e.g. Korean, CP949 console)
    # PowerShell emits localized error text as CP949 bytes; decoding those as UTF-8
    # (main.py sets PYTHONUTF8=1) raises UnicodeDecodeError or yields mojibake,
    # masking the real failure. The prologue forces PowerShell's output stream to
    # UTF-8, and encoding=/errors= make the Python-side decode match + never crash.
    utf8_script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", utf8_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"powershell exited {proc.returncode}")
    return proc.stdout


def _is_admin_windows() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


def _win_thumbprint_trusted(thumbprint: str) -> str | None:
    """Return the store path a cert with this thumbprint lives in, or None.

    Searches both the machine and current-user Root stores, matching the
    reference cowork-test.ps1 corporate-CA trust check behaviour.
    """
    script = (
        f"$tp = '{thumbprint}'; "
        "foreach ($s in 'Cert:\\LocalMachine\\Root','Cert:\\CurrentUser\\Root') { "
        "if (Test-Path (Join-Path $s $tp)) { Write-Output $s; break } }"
    )
    out = _run_powershell(script).strip()
    return out or None


def _win_install(pem_path: Path, thumbprint: str) -> CaResult:
    admin = _is_admin_windows()
    store = "Cert:\\LocalMachine\\Root" if admin else "Cert:\\CurrentUser\\Root"
    script = (
        f"Import-Certificate -FilePath '{pem_path}' "
        f"-CertStoreLocation '{store}' | Out-Null; "
        f"if (Test-Path (Join-Path '{store}' '{thumbprint}')) {{ Write-Output 'ok' }} "
        "else { throw 'import did not land in store' }"
    )
    _run_powershell(script)
    return CaResult(True, True, f"installed CA to {store}", store=store)


def _win_remove(thumbprint: str, store: str) -> CaResult:
    script = (
        f"$path = Join-Path '{store}' '{thumbprint}'; "
        "if (Test-Path $path) { Remove-Item $path -Force; Write-Output 'removed' } "
        "else { Write-Output 'absent' }"
    )
    out = _run_powershell(script).strip()
    changed = out == "removed"
    return CaResult(True, changed,
                    f"{'removed' if changed else 'not present'} in {store}",
                    store=store)


# --- macOS plumbing ----------------------------------------------------------

def _mac_thumbprint_trusted(thumbprint: str) -> bool:
    """True if a cert with this SHA-1 is in the System keychain.

    Compares the security(1)-reported SHA-1 hashes against our fingerprint.
    """
    proc = subprocess.run(
        ["security", "find-certificate", "-a", "-Z",
         "/Library/Keychains/System.keychain"],
        capture_output=True, text=True, timeout=20, check=False,
    )
    return thumbprint in proc.stdout.upper().replace(" ", "")


def _mac_install(pem_path: Path) -> CaResult:
    """Add + trust the CA in the System keychain (requires admin/sudo)."""
    store = "/Library/Keychains/System.keychain"
    proc = subprocess.run(
        ["security", "add-trusted-cert", "-d", "-r", "trustRoot",
         "-k", store, str(pem_path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip()
            or "add-trusted-cert failed (needs admin: run with sudo)"
        )
    return CaResult(True, True, f"installed + trusted CA in {store}", store=store)


def _mac_remove(pem_path: Path) -> CaResult:
    store = "/Library/Keychains/System.keychain"
    proc = subprocess.run(
        ["security", "remove-trusted-cert", "-d", str(pem_path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    changed = proc.returncode == 0
    return CaResult(True, changed,
                    f"{'removed trust for' if changed else 'could not remove'} CA in {store}",
                    store=store)


# --- Public operations -------------------------------------------------------

def install(*, force: bool = False) -> CaResult:
    """Install the corporate CA into the OS trust store (idempotent).

    Refuses to install a PEM whose SHA-256 does not match the build-time pin
    (``site_defaults.expected_ca_sha256``); ``force=True`` overrides that refusal
    for a legitimate CA rotation. See :func:`_evaluate_pin` for the full policy.
    """
    pem = resolve_pem()
    if pem is None:
        configured = site_defaults.configured_ca_bundle()
        where = f" (expected at {configured})" if configured else ""
        return CaResult(
            False, False,
            f"corporate CA PEM not found{where}; set GATEWAY_CLI_CA_BUNDLE "
            "or place the PEM at the baked path.",
        )
    try:
        # Fingerprint pin: never install a CA we cannot vouch for. This runs
        # before the already-trusted short-circuit so a mismatch is refused even
        # if some other process already trusted it.
        refusal, warning = _evaluate_pin(pem, force=force)
        if refusal is not None:
            return CaResult(False, False, refusal)
        warnings = [warning] if warning else []

        thumbprint = ca_fingerprint(pem)
        if sys.platform == "win32":
            existing = _win_thumbprint_trusted(thumbprint)
            if existing:
                return CaResult(True, False, f"CA already trusted in {existing}",
                                store=existing, warnings=warnings)
            result = _win_install(pem, thumbprint)
            result.warnings = warnings
            _write_backup(thumbprint, result.store)
            return result
        if sys.platform == "darwin":
            if _mac_thumbprint_trusted(thumbprint):
                return CaResult(True, False, "CA already trusted in System keychain",
                                store="/Library/Keychains/System.keychain",
                                warnings=warnings)
            result = _mac_install(pem)
            result.warnings = warnings
            _write_backup(thumbprint, result.store, pem=str(pem))
            return result
        return CaResult(False, False,
                        "OS trust-store install is only supported on Windows and macOS.")
    except Exception as exc:  # noqa: BLE001
        log.debug("ca_install_failed", error=str(exc))
        return CaResult(False, False, f"CA install failed: {exc}")


def check() -> CaResult:
    """Report whether the corporate CA is trusted by the OS store (read-only)."""
    pem = resolve_pem()
    if pem is None:
        configured = site_defaults.configured_ca_bundle()
        where = f" (expected at {configured})" if configured else ""
        return CaResult(False, False, f"corporate CA PEM not found{where}")
    try:
        thumbprint = ca_fingerprint(pem)
        if sys.platform == "win32":
            store = _win_thumbprint_trusted(thumbprint)
            if store:
                return CaResult(True, False, f"CA is trusted in {store}", store=store)
            return CaResult(False, False, "CA is NOT in any Root store")
        if sys.platform == "darwin":
            if _mac_thumbprint_trusted(thumbprint):
                return CaResult(True, False, "CA is trusted in System keychain",
                                store="/Library/Keychains/System.keychain")
            return CaResult(False, False, "CA is NOT in System keychain")
        return CaResult(False, False, "unsupported platform")
    except Exception as exc:  # noqa: BLE001
        log.debug("ca_check_failed", error=str(exc))
        return CaResult(False, False, f"CA check failed: {exc}")


def restore() -> CaResult:
    """Remove ONLY the CA this tool installed (per the recorded backup)."""
    backup = _read_backup()
    if backup is None:
        return CaResult(True, False,
                        "no install recorded by this tool — nothing to remove.")
    try:
        if sys.platform == "win32":
            result = _win_remove(backup["thumbprint"], backup["store"])
        elif sys.platform == "darwin":
            pem = backup.get("pem")
            if not pem or not Path(pem).is_file():
                return CaResult(False, False,
                                "recorded PEM path is gone; remove the cert manually "
                                "from Keychain Access.")
            result = _mac_remove(Path(pem))
        else:
            return CaResult(False, False, "unsupported platform")
        if result.changed:
            _clear_backup()
        return result
    except Exception as exc:  # noqa: BLE001
        log.debug("ca_restore_failed", error=str(exc))
        return CaResult(False, False, f"CA restore failed: {exc}")


# --- Invocation-scoped rollback (in-process saga tier) -----------------------

@dataclass
class CaInstallPlan:
    """Invocation-scoped plan/apply/compensate for a single CA install.

    The in-process undo for :func:`install`, layered on the durable CA-record
    tier. :meth:`apply` performs the install; :meth:`compensate` removes the cert
    **only if this run actually added it** (``result.changed`` is True) —
    reverting only *this run's* delta, never a pre-existing corporate CA. If the
    CA was already trusted before this run, compensate is a no-op, so a failed
    retry never removes a CA a previous successful run installed. :meth:`compensate`
    returns a :class:`CaResult` so the rollback loop can check ``.ok``. The durable
    CA record is intentionally left on disk so ``clear`` crash-recovery still works.
    """

    force: bool = False
    _result: CaResult | None = None
    _thumbprint: str | None = None
    _pem: Path | None = None

    def apply(self) -> CaResult:
        """Install the CA (delegates to :func:`install`), stashing what it changed."""
        pem = resolve_pem()
        if pem is not None:
            self._pem = pem
            try:
                self._thumbprint = ca_fingerprint(pem)
            except Exception as exc:  # noqa: BLE001 — non-fatal; compensate degrades
                log.debug("ca_plan_fingerprint_failed", error=str(exc))
                self._thumbprint = None
        self._result = install(force=self.force)
        return self._result

    def compensate(self) -> CaResult:
        """Remove the cert this run installed (no-op if it was already trusted).

        Idempotent and no-op-safe: removing an absent cert is harmless. Does not
        touch the durable CA record.
        """
        if self._result is None or not self._result.changed:
            # Either apply never ran, or the CA was already trusted before this run
            # — this invocation added nothing, so there is nothing to undo.
            return CaResult(True, False, "CA not installed by this run — nothing to undo.")
        if self._thumbprint is None:
            return CaResult(False, False,
                            "cannot identify the cert this run installed — remove it "
                            "manually from the OS trust store.")
        try:
            if sys.platform == "win32":
                return _win_remove(self._thumbprint, self._result.store or "")
            if sys.platform == "darwin":
                if self._pem is None or not self._pem.is_file():
                    return CaResult(False, False,
                                    "recorded PEM path is gone; remove the cert manually.")
                return _mac_remove(self._pem)
            return CaResult(False, False, "unsupported platform")
        except Exception as exc:  # noqa: BLE001 — surface as not-ok, never mask
            log.debug("ca_compensate_failed", error=str(exc))
            return CaResult(False, False, f"CA rollback failed: {exc}")


def plan_install(*, force: bool = False) -> CaInstallPlan:
    """Return an invocation-scoped plan for a CA install (see :class:`CaInstallPlan`).

    Armed BEFORE :meth:`CaInstallPlan.apply` mutates the trust store, so the caller
    can register :meth:`CaInstallPlan.compensate` on its rollback stack ahead of the
    first live change.
    """
    return CaInstallPlan(force=force)


# --- Backup bookkeeping ------------------------------------------------------

def _write_backup(thumbprint: str, store: str | None, pem: str | None = None) -> None:
    record = {"thumbprint": thumbprint, "store": store}
    if pem:
        record["pem"] = pem
    path = _backup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def _read_backup() -> dict | None:
    path = _backup_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _clear_backup() -> None:
    path = _backup_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
