"""A deliberately small password login for one admin user.

No usernames, no accounts, no password reset flow: this console is for one
household. The password is hashed with PBKDF2 (stdlib only, no extra
dependency) and the session is a signed cookie via Starlette's
SessionMiddleware.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from .db import get_settings, set_settings

_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored or ":" not in stored:
        return False
    salt_hex, digest_hex = stored.split(":", 1)
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(digest_hex)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return hmac.compare_digest(actual, expected)


def is_first_run() -> bool:
    return not get_settings().get("admin_password_hash")


def set_admin_password(password: str) -> None:
    set_settings({"admin_password_hash": hash_password(password)})


def check_admin_password(password: str) -> bool:
    return verify_password(password, get_settings().get("admin_password_hash", ""))
