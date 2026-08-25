# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Delegate TLS certificate verification to the operating system trust store.

Why this exists
---------------
On the corporate isolated network every outbound HTTPS connection — including
the public Cognito OIDC endpoint used by ``gateway-cli login`` — is intercepted
by a corporate TLS proxy. The proxy presents a certificate
chained to a corporate root CA. That corporate CA cert marks its
``basicConstraints`` extension **non-critical**, which violates RFC 5280 §4.2.1.9.

OpenSSL 1.1.1 (and the Python interpreters the previous, source-installed CLI ran
on) tolerated that defect. The frozen PyInstaller build ships a newer interpreter
statically linked against **OpenSSL 3.x**, whose stricter verifier rejects the
chain with::

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    Basic Constraints of CA cert not marked critical

Setting ``REQUESTS_CA_BUNDLE`` / ``SSL_CERT_FILE`` does not help: the CA *is*
found, it is the *validation policy* that changed. certifi/OpenSSL cannot be told
to ignore this specific defect.

The fix
-------
``truststore`` (PSF-maintained) patches ``ssl.SSLContext`` so that certificate
verification is performed by the OS instead of OpenSSL's own verifier:

  - Windows  → CryptoAPI chain engine (SChannel) — lenient about the non-critical
    Basic Constraints defect, so the corporate proxy CA validates as it did on the
    old interpreter, provided the CA is trusted by the machine (Windows cert store
    and/or the ``REQUESTS_CA_BUNDLE`` PEM, both of which truststore consults).
  - macOS    → Security framework (SecTrust).
  - Linux    → OpenSSL, i.e. a no-op fall-back — dev machines keep working.

This composes with :func:`cli.site_defaults.apply_ca_bundle`: that still exports
the baked corporate PEM via ``REQUESTS_CA_BUNDLE`` so the extra CA is offered to
truststore, and so non-Python helpers (Claude Code / Node, boto3 subprocesses)
keep trusting it too.
"""

from __future__ import annotations

import contextlib

import structlog

log = structlog.get_logger(component="tls")

_INJECTED = False


def enable_os_trust_store() -> bool:
    """Route TLS verification through the OS trust store. Idempotent.

    Returns True if truststore was injected (or already was), False if it was
    unavailable or refused to load — in which case the process falls back to the
    default OpenSSL/certifi verifier and behaviour is unchanged. Never raises, so
    a missing/broken truststore can never stop the CLI from starting.
    """
    global _INJECTED
    if _INJECTED:
        return True
    try:
        import truststore

        truststore.inject_into_ssl()
        _INJECTED = True
        log.debug("os_trust_store_enabled")
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort; must not break startup
        # Debug-level only: on Linux/dev this is an expected, harmless fall-back.
        log.debug("os_trust_store_unavailable", error=str(exc))
        return False


@contextlib.contextmanager
def without_os_trust_store():
    """Temporarily un-inject truststore for the duration of the ``with`` block.

    ``truststore.inject_into_ssl()`` monkeypatches ``ssl.SSLContext`` process-wide.
    botocore builds its own ``SSLContext`` and re-applies ``.options`` in a way
    that recurses infinitely against truststore's patched descriptor, so a boto3
    call made while truststore is injected dies with ``RecursionError`` (observed
    on botocore 1.43 + truststore 0.10 under the frozen OpenSSL 3.x build).

    boto3/botocore verify TLS via certifi/AWS_CA_BUNDLE, not the OS store, so they
    don't need truststore anyway. This context manager extracts truststore, runs
    the block, and re-injects afterwards, leaving requests/urllib (login/verify)
    on the OS trust store as before. Safe as a no-op when truststore was never
    injected or isn't importable.
    """
    if not _INJECTED:
        yield
        return
    try:
        import truststore

        truststore.extract_from_ssl()
    except Exception as exc:  # noqa: BLE001
        log.debug("os_trust_store_extract_failed", error=str(exc))
        yield
        return
    try:
        yield
    finally:
        try:
            truststore.inject_into_ssl()
        except Exception as exc:  # noqa: BLE001
            log.debug("os_trust_store_reinject_failed", error=str(exc))
