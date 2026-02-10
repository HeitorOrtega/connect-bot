import os
import base64
from cryptography.fernet import Fernet

def _get_master_key_bytes() -> bytes:

    key = (os.getenv("MASTER_KEY") or "").strip()
    if not key:
        raise RuntimeError("MASTER_KEY não configurada (Railway Variables ou .env)")

    try:
        kb = key.encode("utf-8")
        Fernet(kb)  
        return kb
    except Exception:
        pass

    raw = key.encode("utf-8")
    raw32 = raw.ljust(32, b"0")[:32]
    return base64.urlsafe_b64encode(raw32)

def _fernet() -> Fernet:
    return Fernet(_get_master_key_bytes())

def encrypt_str(s: str) -> str:
    return _fernet().encrypt(s.encode("utf-8")).decode("utf-8")

def decrypt_str(token: str) -> str:
    return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
