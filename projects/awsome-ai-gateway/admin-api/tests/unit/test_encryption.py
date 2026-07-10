# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag

from app.core.encryption import CURRENT_VERSION, AESEncryptionService


TEST_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
OTHER_KEY = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"


class TestAESEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        svc = AESEncryptionService(TEST_KEY)
        plaintext = "vk-a3b9c1d2e4f56789012345678901234567890123456789012345678901234567"

        encrypted = svc.encrypt(plaintext)
        decrypted = svc.decrypt(encrypted)

        assert decrypted == plaintext

    def test_different_encryptions_produce_different_ciphertext(self):
        svc = AESEncryptionService(TEST_KEY)
        plaintext = "test-value"

        ct1 = svc.encrypt(plaintext)
        ct2 = svc.encrypt(plaintext)

        # Random IV ensures different ciphertext
        assert ct1 != ct2

    def test_both_decrypt_to_same_plaintext(self):
        svc = AESEncryptionService(TEST_KEY)
        plaintext = "test-value"

        ct1 = svc.encrypt(plaintext)
        ct2 = svc.encrypt(plaintext)

        assert svc.decrypt(ct1) == plaintext
        assert svc.decrypt(ct2) == plaintext

    def test_encrypted_format_has_version_prefix(self):
        svc = AESEncryptionService(TEST_KEY)
        encrypted = svc.encrypt("hello")

        # Format: b"v1:" + IV(12) + ciphertext + auth_tag(16)
        assert encrypted.startswith(f"{CURRENT_VERSION}:".encode("ascii"))
        # Strip prefix and check length: IV(12) + ciphertext(>=1) + auth_tag(16)
        payload = encrypted[len(CURRENT_VERSION) + 1 :]
        assert len(payload) > 12 + 16

    def test_invalid_key_length_raises(self):
        with pytest.raises(ValueError, match="64-char hex"):
            AESEncryptionService("tooshort")

    def test_empty_key_raises(self):
        with pytest.raises(ValueError, match="64-char hex"):
            AESEncryptionService("")

    def test_invalid_hex_raises(self):
        # 64 chars but not valid hex (contains 'z')
        bad_hex = "z" * 64
        with pytest.raises(ValueError, match="valid hex"):
            AESEncryptionService(bad_hex)

    def test_tampered_ciphertext_raises(self):
        svc = AESEncryptionService(TEST_KEY)
        encrypted = svc.encrypt("secret")

        # Flip a byte in the ciphertext
        tampered = bytearray(encrypted)
        tampered[-1] ^= 0xFF
        tampered = bytes(tampered)

        with pytest.raises(InvalidTag):
            svc.decrypt(tampered)

    def test_wrong_key_decrypt_raises(self):
        # Encrypt with TEST_KEY, try to decrypt with OTHER_KEY.
        enc_svc = AESEncryptionService(TEST_KEY)
        wrong_svc = AESEncryptionService(OTHER_KEY)

        encrypted = enc_svc.encrypt("sensitive")

        with pytest.raises(InvalidTag):
            wrong_svc.decrypt(encrypted)

    def test_non_ascii_plaintext_roundtrip(self):
        svc = AESEncryptionService(TEST_KEY)
        plaintext = "한글 이모지 🔐 Virtual Key — 민감정보"

        encrypted = svc.encrypt(plaintext)
        assert svc.decrypt(encrypted) == plaintext

    def test_long_plaintext_roundtrip(self):
        svc = AESEncryptionService(TEST_KEY)
        plaintext = "x" * 10_000

        encrypted = svc.encrypt(plaintext)
        assert svc.decrypt(encrypted) == plaintext


class TestVersioning:
    """버전 prefix 및 legacy 호환성."""

    def test_current_version_is_v1(self):
        assert CURRENT_VERSION == "v1"

    def test_legacy_ciphertext_without_prefix_decrypts(self):
        """Prefix 없이 저장된 기존 암호문(legacy)이 정상 복호화되어야 함."""
        svc = AESEncryptionService(TEST_KEY)
        plaintext = "legacy-vk"

        # Manually produce legacy-format ciphertext: IV + ciphertext (no prefix).
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        iv = os.urandom(12)
        aesgcm = AESGCM(bytes.fromhex(TEST_KEY))
        legacy = iv + aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)

        assert svc.decrypt(legacy) == plaintext

    def test_unknown_version_raises(self):
        svc = AESEncryptionService(TEST_KEY)

        # Craft a valid-looking payload with unknown version prefix.
        encrypted = svc.encrypt("x")
        payload_without_prefix = encrypted[len(CURRENT_VERSION) + 1 :]
        forged = b"v9:" + payload_without_prefix

        with pytest.raises(ValueError, match="Unknown encryption version 'v9'"):
            svc.decrypt(forged)

    def test_extra_keys_enable_multi_version_decrypt(self):
        """Rotation 시나리오: 이전 버전 키를 extra_keys로 주입하여 과거 암호문 복호화."""
        old_svc = AESEncryptionService(OTHER_KEY)
        old_ciphertext = old_svc.encrypt("old-value")  # uses CURRENT_VERSION=v1 with OTHER_KEY

        # New service: TEST_KEY is current; OTHER_KEY registered as v0 for legacy decrypt.
        # Note: to simulate a true multi-version scenario, we pretend the "old" ciphertext
        # was actually labelled v0. Re-prefix it here.
        payload = old_ciphertext[len(CURRENT_VERSION) + 1 :]
        v0_ciphertext = b"v0:" + payload

        new_svc = AESEncryptionService(TEST_KEY, extra_keys={"v0": OTHER_KEY})
        assert new_svc.decrypt(v0_ciphertext) == "old-value"

    def test_extra_keys_validate_hex_length(self):
        with pytest.raises(ValueError, match="extra key 'v0'"):
            AESEncryptionService(TEST_KEY, extra_keys={"v0": "tooshort"})
