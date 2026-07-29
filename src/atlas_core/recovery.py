from __future__ import annotations

import base64
import json
import os
import struct
from typing import Any

MAGIC = b"ATLASBK1\n"
MAX_HEADER_BYTES = 16 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1


def _passphrase_bytes(passphrase: str) -> bytes:
    if not isinstance(passphrase, str):
        raise ValueError("passphrase must be text.")
    encoded = passphrase.encode("utf-8")
    if not 16 <= len(encoded) <= 1024:
        raise ValueError("passphrase must be between 16 and 1024 UTF-8 bytes.")
    return encoded


def _crypto() -> tuple[type[Any], type[Any], type[Exception]]:
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as error:
        raise RuntimeError(
            "Encrypted recovery requires the 'cryptography' package."
        ) from error
    return AESGCM, Scrypt, InvalidTag


def _key(passphrase: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    _, Scrypt, _ = _crypto()
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(
        _passphrase_bytes(passphrase)
    )


def encrypt_recovery_artifact(
    plaintext: bytes,
    *,
    passphrase: str,
    metadata: dict[str, Any],
) -> bytes:
    if len(plaintext) > MAX_ARTIFACT_BYTES:
        raise ValueError("Backup is too large for an encrypted recovery artifact.")
    AESGCM, _, _ = _crypto()
    salt = os.urandom(16)
    nonce = os.urandom(12)
    header = {
        **metadata,
        "format": "atlas-encrypted-backup",
        "format_version": 1,
        "cipher": "AES-256-GCM",
        "kdf": {
            "name": "scrypt",
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt": base64.b64encode(salt).decode("ascii"),
        },
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }
    header_bytes = json.dumps(
        header,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(header_bytes) > MAX_HEADER_BYTES:
        raise ValueError("Encrypted recovery metadata is too large.")
    prefix = MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes
    ciphertext = AESGCM(
        _key(passphrase, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    ).encrypt(nonce, plaintext, prefix)
    return prefix + ciphertext


def decrypt_recovery_artifact(
    artifact: bytes,
    *,
    passphrase: str,
) -> tuple[dict[str, Any], bytes]:
    if not isinstance(artifact, bytes) or len(artifact) > MAX_ARTIFACT_BYTES:
        raise ValueError("Encrypted recovery artifact is invalid or too large.")
    if len(artifact) < len(MAGIC) + 4 or not artifact.startswith(MAGIC):
        raise ValueError("Encrypted recovery artifact has an invalid format.")
    header_length = struct.unpack(
        ">I",
        artifact[len(MAGIC) : len(MAGIC) + 4],
    )[0]
    if not 1 <= header_length <= MAX_HEADER_BYTES:
        raise ValueError("Encrypted recovery artifact header is invalid.")
    header_start = len(MAGIC) + 4
    ciphertext_start = header_start + header_length
    if ciphertext_start >= len(artifact):
        raise ValueError("Encrypted recovery artifact is truncated.")
    try:
        header = json.loads(artifact[header_start:ciphertext_start])
        kdf = header["kdf"]
        salt = base64.b64decode(kdf["salt"], validate=True)
        nonce = base64.b64decode(header["nonce"], validate=True)
        if (
            header["format"] != "atlas-encrypted-backup"
            or header["format_version"] != 1
            or header["cipher"] != "AES-256-GCM"
            or kdf["name"] != "scrypt"
            or kdf["n"] != SCRYPT_N
            or kdf["r"] != SCRYPT_R
            or kdf["p"] != SCRYPT_P
            or len(salt) != 16
            or len(nonce) != 12
        ):
            raise ValueError
    except (
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise ValueError("Encrypted recovery artifact metadata is invalid.") from error
    AESGCM, _, InvalidTag = _crypto()
    prefix = artifact[:ciphertext_start]
    try:
        plaintext = AESGCM(
            _key(
                passphrase,
                salt,
                n=kdf["n"],
                r=kdf["r"],
                p=kdf["p"],
            )
        ).decrypt(nonce, artifact[ciphertext_start:], prefix)
    except InvalidTag as error:
        raise ValueError(
            "Encrypted recovery artifact authentication failed."
        ) from error
    return header, plaintext
