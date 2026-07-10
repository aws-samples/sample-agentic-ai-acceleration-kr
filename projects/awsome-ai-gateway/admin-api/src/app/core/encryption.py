# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 암호문 포맷: b"v<N>:" + IV(12) + ciphertext + auth_tag(16)
# 버전 prefix가 없는 기존 암호문(legacy)도 decrypt 가능하도록 하위 호환 유지.
# 상세: requirements-document/deployment-secrets.md §1.2, §3.3
CURRENT_VERSION = "v1"
_VERSION_PREFIX_RE = re.compile(rb"^v(\d+):")


class AESEncryptionService:
    """AES-256-GCM encryption for Virtual Key storage.

    Format (current): b"v1:" + IV(12 bytes) + ciphertext + auth_tag(16 bytes, appended by GCM)
    Legacy format   : IV(12 bytes) + ciphertext + auth_tag  (prefix 없음, 하위 호환)
    """

    IV_LENGTH = 12

    def __init__(
        self,
        key_hex: str,
        *,
        extra_keys: dict[str, str] | None = None,
    ) -> None:
        """
        Args:
            key_hex: Current encryption key (64-char hex, 32 bytes). Used for both
                encryption and decryption of v1 ciphertext.
            extra_keys: Optional map of {version: key_hex} for decrypting legacy
                versions (e.g., {"v0": "...", "v2": "..."}). Current version key
                is populated from `key_hex` automatically.
        """
        self._validate_key_hex(key_hex, label="VIRTUAL_KEY_ENCRYPTION_KEY")
        self._current_key = bytes.fromhex(key_hex)
        self._current_aesgcm = AESGCM(self._current_key)

        # Multi-version decrypt map. 현재 버전 + extra_keys.
        self._decrypt_keys: dict[str, AESGCM] = {CURRENT_VERSION: self._current_aesgcm}
        if extra_keys:
            for ver, hex_val in extra_keys.items():
                self._validate_key_hex(hex_val, label=f"extra key '{ver}'")
                self._decrypt_keys[ver] = AESGCM(bytes.fromhex(hex_val))

    @staticmethod
    def _validate_key_hex(key_hex: str, *, label: str) -> None:
        if not key_hex or len(key_hex) != 64:
            raise ValueError(f"{label} must be a 64-char hex string (32 bytes)")
        try:
            bytes.fromhex(key_hex)
        except ValueError as exc:
            raise ValueError(f"{label} must be valid hex") from exc

    def encrypt(self, plaintext: str) -> bytes:
        iv = os.urandom(self.IV_LENGTH)
        ciphertext = self._current_aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
        return f"{CURRENT_VERSION}:".encode("ascii") + iv + ciphertext

    def decrypt(self, data: bytes) -> str:
        version, payload = self._split_version(data)

        if version is None:
            # Legacy: prefix 없음. 현재 키로 시도.
            aesgcm = self._current_aesgcm
        else:
            aesgcm = self._decrypt_keys.get(version)
            if aesgcm is None:
                raise ValueError(
                    f"Unknown encryption version '{version}'. "
                    "Configure the matching key via extra_keys."
                )

        iv = payload[: self.IV_LENGTH]
        ciphertext = payload[self.IV_LENGTH :]
        plaintext = aesgcm.decrypt(iv, ciphertext, None)
        return plaintext.decode("utf-8")

    @staticmethod
    def _split_version(data: bytes) -> tuple[str | None, bytes]:
        match = _VERSION_PREFIX_RE.match(data)
        if match is None:
            return None, data
        version = match.group(0).rstrip(b":").decode("ascii")
        return version, data[match.end() :]
