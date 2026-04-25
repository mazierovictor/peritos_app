"""
Criptografia simétrica (Fernet) para guardar senhas SMTP no banco.

A chave é resolvida na seguinte ordem:
  1. Variável de ambiente FERNET_KEY (se válida)
  2. Arquivo persistido em DATA_DIR/.fernet_key (gerado no primeiro start)

Persistir no volume `/data` faz com que a chave sobreviva a redeploys
sem exigir que o usuário gere/configure manualmente.
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


_KEY_FILENAME = ".fernet_key"


def _is_valid_fernet_key(key: bytes) -> bool:
    try:
        Fernet(key)
        return True
    except (ValueError, TypeError):
        return False


def _key_file_path() -> Path:
    return Path(settings.data_dir) / _KEY_FILENAME


def _load_or_create_persisted_key() -> bytes:
    path = _key_file_path()
    if path.exists():
        key = path.read_bytes().strip()
        if _is_valid_fernet_key(key):
            return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _resolve_key() -> bytes:
    env_key = (settings.fernet_key or "").encode().strip()
    if env_key and _is_valid_fernet_key(env_key):
        return env_key
    return _load_or_create_persisted_key()


def _fernet() -> Fernet:
    return Fernet(_resolve_key())


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("Falha ao descriptografar — FERNET_KEY mudou?") from e
