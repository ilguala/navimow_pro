"""p:101 envelope crypto for the Segway Navimow private cloud.

This is a FAITHFUL port of the proven working reference implementation
(scratchpad/p101_client.py). The constants below are app-wide values shared by
every user of the official app -- they are NOT user secrets. Do not "improve"
the crypto: it matches the server byte-for-byte and is proven live.

Recipe (proven):
    reqKey = 16 random ASCII-uppercase bytes (per request)
    k      = base64(RSA-1024 PKCS#1 v1.5 type-2 wrap of reqKey, WRAP_PUB_PEM)
    PT     = {"data": base64(business_json), keyDataOne..Four, platform:2, timeStamp}
    d      = base64(AES-128-CBC(reqKey, IV=0, PKCS7(PT)))
    h      = MD5(PT) hex lowercase
    envelope = {"d","h","k","p":"101","t":"0"}   (Content-Type: text/html, ninebot-version: 1)
    response {"r","s","v"}:  r = AES-128-CBC(SESSION_KEY, IV=0) -> {"data": base64(biz)}

Uses the `cryptography` library, which Home Assistant bundles (no pycryptodome).
The functions here are synchronous and CPU-bound; callers must run them off the
event loop (Home Assistant does this via hass.async_add_executor_job).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_pem_public_key

# App-wide (NOT user secrets) -- copied verbatim from the proven client.
SESSION_KEY = bytes.fromhex("d0db95e2b4b2eeb99af3cfb638386209")

WRAP_PUB_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDRiEqrCME5SI2er9B+ZDweKWGe
TWnMn2dFG8rt+M6iKDv4Lui4p6BdHX2dbTgCRHNpJNz1tAsOGSPfcmcIkGxt3x+a
gs5YEmpkQq0zEYBSw8Stin2WVaPrUur00dEYr0qlNqDWIbMIuDOG554Sk11mUjY/
rzN0+TxJ5YNsU3kbwQIDAQAB
-----END PUBLIC KEY-----"""

_pub = load_pem_public_key(WRAP_PUB_PEM).public_numbers()
PUB_N: int = _pub.n
PUB_E: int = _pub.e

# Fixed "keyData" fields carried in the plaintext envelope (app-wide constants).
KD = {
    "keyDataOne": "4c9239e5377",
    "keyDataTwo": "c416f9ed",
    "keyDataThree": "230d8ee5",
    "keyDataFour": "53a22?h}",
}


def _pkcs7(b: bytes) -> bytes:
    pad = 16 - (len(b) % 16)
    return b + bytes([pad]) * pad


def _unpkcs7(b: bytes) -> bytes:
    return b[: -b[-1]]


def _aes_cbc_enc(key: bytes, data: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).encryptor()
    return enc.update(_pkcs7(data)) + enc.finalize()


def _aes_cbc_dec(key: bytes, data: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).decryptor()
    return _unpkcs7(dec.update(data) + dec.finalize())


def _pkcs1_type2(payload: bytes, ksize: int = 128) -> bytes:
    ps = bytearray()
    while len(ps) < ksize - 3 - len(payload):
        b = os.urandom(1)[0]
        if b:
            ps.append(b)
    return bytes([0, 2]) + bytes(ps) + bytes([0]) + payload


def _rsa_wrap(aes_key: bytes) -> bytes:
    m = int.from_bytes(_pkcs1_type2(aes_key), "big")
    return pow(m, PUB_E, PUB_N).to_bytes(128, "big")


def _build_pt(business: dict) -> bytes:
    data_b64 = base64.b64encode(
        json.dumps(business, separators=(",", ":")).encode()
    ).decode()
    return json.dumps(
        {"data": data_b64, **KD, "platform": 2, "timeStamp": int(time.time())},
        separators=(",", ":"),
    ).encode()


def pack(business: dict) -> dict:
    """Build the request envelope {d,h,k,p,t} for a business payload."""
    pt = _build_pt(business)
    req_key = bytes(0x41 + (b % 26) for b in os.urandom(16))
    return {
        "d": base64.b64encode(_aes_cbc_enc(req_key, pt)).decode(),
        "h": hashlib.md5(pt).hexdigest(),
        "k": base64.b64encode(_rsa_wrap(req_key)).decode(),
        "p": "101",
        "t": "0",
    }


def decode_response(j: dict) -> dict:
    """Decode a {r,s,v} response envelope back to the business JSON dict.

    If the payload is not an {r,...} envelope (e.g. a plain error), returns it
    unchanged so the caller can inspect any error fields.
    """
    if not isinstance(j, dict) or "r" not in j:
        return j
    pt = _aes_cbc_dec(SESSION_KEY, base64.b64decode(j["r"]))  # already unpadded
    return json.loads(base64.b64decode(json.loads(pt)["data"]))
