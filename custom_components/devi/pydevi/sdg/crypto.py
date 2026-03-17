from __future__ import annotations

import os
import struct
from functools import lru_cache
import hashlib


PACKET_MAGIC = 0x9F909FF0
CRYPTO_BOX_PUBLICKEYBYTES = 32
CRYPTO_BOX_SECRETKEYBYTES = 32
CRYPTO_BOX_BEFORENMBYTES = 32
CRYPTO_BOX_NONCEBYTES = 24
CRYPTO_BOX_ZEROBYTES = 32
CRYPTO_BOX_BOXZEROBYTES = 16
CRYPTO_SCALARMULT_BYTES = 32
CRYPTO_SCALARMULT_SCALARBYTES = 32
CURVECP_COOKIEBYTES = 96


@lru_cache(maxsize=1)
def _bindings():
    try:
        from nacl import bindings  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "PyNaCl is required for the SecureDeviceGrid transport. Install the project requirements first."
        ) from exc
    return bindings


def swap16(value: int) -> int:
    return struct.unpack(">H", struct.pack("<H", value))[0]


def swap64(value: int) -> int:
    return struct.unpack(">Q", struct.pack("<Q", value))[0]


def generate_private_key() -> bytes:
    return os.urandom(CRYPTO_BOX_SECRETKEYBYTES)


def calculate_public_key(private_key: bytes) -> bytes:
    return _bindings().crypto_scalarmult_base(private_key)


def build_short_term_nonce(prefix: bytes, value: int) -> bytes:
    return prefix[:16].ljust(16, b"\x00") + struct.pack(">Q", value)


def build_long_term_nonce(prefix: bytes, values: tuple[int, int]) -> bytes:
    return prefix[:8].ljust(8, b"\x00") + struct.pack(">QQ", values[0], values[1])


def build_random_long_term_nonce(prefix: bytes = b"CurveCPV") -> bytes:
    return prefix[:8].ljust(8, b"\x00") + os.urandom(16)


def crypto_box_keypair() -> tuple[bytes, bytes]:
    bindings = _bindings()
    return bindings.crypto_box_keypair()


def crypto_box_beforenm(public_key: bytes, secret_key: bytes) -> bytes:
    return _bindings().crypto_box_beforenm(public_key, secret_key)


def crypto_box_old(message: bytes, nonce: bytes, public_key: bytes, secret_key: bytes) -> bytes:
    if len(message) < CRYPTO_BOX_ZEROBYTES:
        raise ValueError("message too short for legacy crypto_box padding")
    ciphertext = _bindings().crypto_box(message[CRYPTO_BOX_ZEROBYTES :], nonce, public_key, secret_key)
    return (b"\x00" * CRYPTO_BOX_BOXZEROBYTES) + ciphertext


def crypto_box_open_old(ciphertext: bytes, nonce: bytes, public_key: bytes, secret_key: bytes) -> bytes:
    if len(ciphertext) < CRYPTO_BOX_BOXZEROBYTES:
        raise ValueError("ciphertext too short for legacy crypto_box padding")
    message = _bindings().crypto_box_open(ciphertext[CRYPTO_BOX_BOXZEROBYTES :], nonce, public_key, secret_key)
    return (b"\x00" * CRYPTO_BOX_ZEROBYTES) + message


def crypto_box_afternm_old(message: bytes, nonce: bytes, shared_key: bytes) -> bytes:
    if len(message) < CRYPTO_BOX_ZEROBYTES:
        raise ValueError("message too short for legacy crypto_box padding")
    ciphertext = _bindings().crypto_box_afternm(message[CRYPTO_BOX_ZEROBYTES :], nonce, shared_key)
    return (b"\x00" * CRYPTO_BOX_BOXZEROBYTES) + ciphertext


def crypto_box_open_afternm_old(ciphertext: bytes, nonce: bytes, shared_key: bytes) -> bytes:
    if len(ciphertext) < CRYPTO_BOX_BOXZEROBYTES:
        raise ValueError("ciphertext too short for legacy crypto_box padding")
    message = _bindings().crypto_box_open_afternm(ciphertext[CRYPTO_BOX_BOXZEROBYTES :], nonce, shared_key)
    return (b"\x00" * CRYPTO_BOX_ZEROBYTES) + message


def crypto_scalarmult_base(secret: bytes) -> bytes:
    return _bindings().crypto_scalarmult_base(secret)


def crypto_scalarmult(secret: bytes, point: bytes) -> bytes:
    return _bindings().crypto_scalarmult(secret, point)


def crypto_hash(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _rotate_left(value: int, bits: int) -> int:
    return ((value << bits) & 0xFFFFFFFF) | (value >> (32 - bits))


def _quarter_round(state: list[int], a: int, b: int, c: int, d: int) -> None:
    state[b] ^= _rotate_left((state[a] + state[d]) & 0xFFFFFFFF, 7)
    state[c] ^= _rotate_left((state[b] + state[a]) & 0xFFFFFFFF, 9)
    state[d] ^= _rotate_left((state[c] + state[b]) & 0xFFFFFFFF, 13)
    state[a] ^= _rotate_left((state[d] + state[c]) & 0xFFFFFFFF, 18)


def _hsalsa20(key: bytes, nonce: bytes) -> bytes:
    if len(key) != 32:
        raise ValueError("XSalsa20 key must be 32 bytes")
    if len(nonce) != 16:
        raise ValueError("HSalsa20 nonce prefix must be 16 bytes")

    constants = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
    key_words = list(struct.unpack("<8I", key))
    nonce_words = list(struct.unpack("<4I", nonce))
    state = [
        constants[0],
        key_words[0],
        key_words[1],
        key_words[2],
        key_words[3],
        constants[1],
        nonce_words[0],
        nonce_words[1],
        nonce_words[2],
        nonce_words[3],
        constants[2],
        key_words[4],
        key_words[5],
        key_words[6],
        key_words[7],
        constants[3],
    ]

    working = state.copy()
    for _ in range(10):
        _quarter_round(working, 0, 4, 8, 12)
        _quarter_round(working, 5, 9, 13, 1)
        _quarter_round(working, 10, 14, 2, 6)
        _quarter_round(working, 15, 3, 7, 11)
        _quarter_round(working, 0, 1, 2, 3)
        _quarter_round(working, 5, 6, 7, 4)
        _quarter_round(working, 10, 11, 8, 9)
        _quarter_round(working, 15, 12, 13, 14)

    return struct.pack(
        "<8I",
        working[0],
        working[5],
        working[10],
        working[15],
        working[6],
        working[7],
        working[8],
        working[9],
    )


@lru_cache(maxsize=1)
def _salsa20_module():
    try:
        from Crypto.Cipher import Salsa20  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "PyCryptodome is required for XSalsa20 stream operations. Install the project requirements first."
        ) from exc
    return Salsa20


def crypto_stream_xor(data: bytes, nonce: bytes, key: bytes) -> bytes:
    if len(nonce) < 24:
        raise ValueError("XSalsa20 nonce must be at least 24 bytes")
    if len(key) != 32:
        raise ValueError("XSalsa20 key must be 32 bytes")

    nonce24 = nonce[:24]
    subkey = _hsalsa20(key, nonce24[:16])
    cipher = _salsa20_module().new(key=subkey, nonce=nonce24[16:])
    return cipher.encrypt(data)
