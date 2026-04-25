"""
Criptografia simétrica (Fernet) para guardar senhas SMTP no banco.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


def _fernet() -> Fernet:
    key = settings.fernet_key.encode() if settings.fernet_key else b""
    if not key:
        raise RuntimeError(
            "FERNET_KEY não configurada. Gere uma com:\n"
            "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key)


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("Falha ao descriptografar — FERNET_KEY mudou?") from e
