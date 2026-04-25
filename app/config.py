"""
Configuração via variáveis de ambiente (lê .env automaticamente em dev).

Para `session_secret`, se a env var não for definida (ou for o placeholder
padrão), uma chave forte é gerada e persistida em DATA_DIR/.session_secret.
Assim o deploy "funciona sozinho" e a sessão sobrevive a redeploys.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


_DEFAULT_SESSION_SECRET = "dev-secret-trocar"
_SESSION_SECRET_FILENAME = ".session_secret"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    session_secret: str = _DEFAULT_SESSION_SECRET
    fernet_key: str = ""

    data_dir: str = "./data"

    admin_email: str = ""
    admin_nome: str = ""
    admin_senha: str = ""

    user2_email: str = ""
    user2_nome: str = ""
    user2_senha: str = ""

    def resolve_session_secret(self) -> str:
        if self.session_secret and self.session_secret != _DEFAULT_SESSION_SECRET:
            return self.session_secret
        path = Path(self.data_dir) / _SESSION_SECRET_FILENAME
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_urlsafe(48)
        path.write_text(value, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return value


settings = Settings()
