"""
Configuração via variáveis de ambiente (lê .env automaticamente em dev).
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    session_secret: str = "dev-secret-trocar"
    fernet_key: str = ""

    data_dir: str = "./data"

    admin_email: str = ""
    admin_nome: str = ""
    admin_senha: str = ""

    user2_email: str = ""
    user2_nome: str = ""
    user2_senha: str = ""


settings = Settings()
