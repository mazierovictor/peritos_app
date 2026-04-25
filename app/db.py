"""
Camada de banco SQLite. Um único arquivo `peritos.db` dentro de DATA_DIR.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL UNIQUE,
    nome        TEXT NOT NULL,
    senha_hash  TEXT NOT NULL,
    criado_em   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS perfis_remetente (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id      INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    nome            TEXT NOT NULL,
    email_remetente TEXT NOT NULL,
    smtp_host       TEXT NOT NULL DEFAULT 'smtp.gmail.com',
    smtp_port       INTEGER NOT NULL DEFAULT 587,
    smtp_senha_enc  TEXT NOT NULL,
    assunto         TEXT NOT NULL,
    corpo_texto     TEXT NOT NULL,
    corpo_html      TEXT NOT NULL,
    assinatura      TEXT,
    curriculo_path  TEXT,
    limite_diario   INTEGER NOT NULL DEFAULT 200,
    criado_em       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contatos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT NOT NULL,
    cidade       TEXT,
    comarca      TEXT,
    orgao        TEXT,
    estado       TEXT,
    tribunal     TEXT NOT NULL,
    sistema      TEXT,
    invalido     INTEGER NOT NULL DEFAULT 0,
    observacao   TEXT,
    scraping_em  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(email, tribunal)
);
CREATE INDEX IF NOT EXISTS idx_contatos_estado ON contatos(estado);
CREATE INDEX IF NOT EXISTS idx_contatos_tribunal ON contatos(tribunal);

CREATE TABLE IF NOT EXISTS envios (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    contato_id          INTEGER NOT NULL REFERENCES contatos(id) ON DELETE CASCADE,
    perfil_remetente_id INTEGER NOT NULL REFERENCES perfis_remetente(id) ON DELETE CASCADE,
    enviado_em          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status              TEXT NOT NULL,
    erro_mensagem       TEXT
);
CREATE INDEX IF NOT EXISTS idx_envios_contato ON envios(contato_id);
CREATE INDEX IF NOT EXISTS idx_envios_data ON envios(enviado_em);

CREATE TABLE IF NOT EXISTS scraper_runs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    tribunal              TEXT NOT NULL,
    iniciado_em           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finalizado_em         TIMESTAMP,
    status                TEXT NOT NULL DEFAULT 'rodando',
    contatos_novos        INTEGER NOT NULL DEFAULT 0,
    contatos_atualizados  INTEGER NOT NULL DEFAULT 0,
    log                   TEXT
);

CREATE TABLE IF NOT EXISTS agendamentos (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo      TEXT NOT NULL,
    alvo      TEXT NOT NULL,
    cron      TEXT NOT NULL,
    ativo     INTEGER NOT NULL DEFAULT 1,
    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def db_path() -> Path:
    return Path(settings.data_dir) / "peritos.db"


def _connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
