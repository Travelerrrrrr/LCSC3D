"""The official CAS SM2 C1C3C2 and UTF-8/base64 login wire format."""
from __future__ import annotations

import base64
import re

from gmalg import SM2


def encrypt_login_value(value, public_key):
    payload = base64.b64encode(value.encode('utf-8'))
    return SM2(pk=bytes.fromhex(public_key)).encrypt(payload).hex()


def decode_login_values(value, private_key):
    if isinstance(value, str):
        match = re.fullmatch(r'\{secret[^}]*\}([0-9a-fA-F]+)', value)
        if match:
            decoded = SM2(sk=bytes.fromhex(private_key)).decrypt(bytes.fromhex(match[1]))
            return base64.b64decode(decoded, validate=True).decode('utf-8')
        return value
    if isinstance(value, list):
        return [decode_login_values(item, private_key) for item in value]
    if isinstance(value, dict):
        return {key: decode_login_values(item, private_key) for key, item in value.items()}
    return value
