import base64
import json
from pathlib import Path


def _aes_available() -> bool:
    try:
        from Crypto.Cipher import AES  # noqa: F401

        return True
    except ImportError:
        return False


def cockpit_key(home: Path):
    path = home / ".antigravity_cockpit" / "secure-account-storage.key"
    if not path.is_file() or not _aes_available():
        return None
    from Crypto.Cipher import AES  # noqa: F401

    raw = path.read_text(encoding="utf-8").strip()
    return base64.b64decode(raw)


def load_maybe_encrypted(path: Path, key):
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("algorithm") != "AES-256-GCM" or key is None:
        return data
    from Crypto.Cipher import AES

    nonce = base64.b64decode(data["nonce"])
    blob = base64.b64decode(data["ciphertext"])
    plain = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(blob[:-16], blob[-16:])
    return json.loads(plain)
