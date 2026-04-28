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
    erro_mensagem       TEXT,
    message_id          TEXT,
    bounce_em           TIMESTAMP,
    bounce_codigo       TEXT,
    bounce_diagnostico  TEXT
);
CREATE INDEX IF NOT EXISTS idx_envios_contato ON envios(contato_id);
CREATE INDEX IF NOT EXISTS idx_envios_data ON envios(enviado_em);

CREATE TABLE IF NOT EXISTS aberturas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    envio_id    INTEGER NOT NULL REFERENCES envios(id) ON DELETE CASCADE,
    aberta_em   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ip          TEXT,
    user_agent  TEXT,
    tipo        TEXT
);
CREATE INDEX IF NOT EXISTS idx_aberturas_envio ON aberturas(envio_id);

CREATE TABLE IF NOT EXISTS bounce_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    perfil_id         INTEGER NOT NULL REFERENCES perfis_remetente(id) ON DELETE CASCADE,
    iniciado_em       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finalizado_em     TIMESTAMP,
    status            TEXT NOT NULL DEFAULT 'rodando',
    bounces_novos     INTEGER NOT NULL DEFAULT 0,
    mensagens_lidas   INTEGER NOT NULL DEFAULT 0,
    erro              TEXT
);

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

CREATE TABLE IF NOT EXISTS scraper_configs (
    sigla       TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agendamentos (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    nome      TEXT NOT NULL DEFAULT '',
    tipo      TEXT NOT NULL,
    alvo      TEXT NOT NULL,
    cron      TEXT NOT NULL,
    ativo     INTEGER NOT NULL DEFAULT 1,
    criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cron_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ag_id          INTEGER,
    nome           TEXT,
    tipo           TEXT,
    fonte          TEXT NOT NULL DEFAULT 'agendamento',
    iniciado_em    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finalizado_em  TIMESTAMP,
    status         TEXT NOT NULL DEFAULT 'rodando',
    mensagem       TEXT
);
CREATE INDEX IF NOT EXISTS idx_cron_runs_ag ON cron_runs(ag_id);
CREATE INDEX IF NOT EXISTS idx_cron_runs_iniciado ON cron_runs(iniciado_em);

CREATE TABLE IF NOT EXISTS campanhas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nome            TEXT NOT NULL,
    perfil_id       INTEGER NOT NULL REFERENCES perfis_remetente(id) ON DELETE RESTRICT,
    filtro_estado   TEXT,
    filtro_tribunal TEXT,
    total_alvo      INTEGER NOT NULL,
    por_dia         INTEGER NOT NULL,
    dias_semana     TEXT NOT NULL,
    janela_inicio   TEXT NOT NULL,
    janela_fim      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'rascunho',
    pausa_motivo    TEXT,
    enviados_total  INTEGER NOT NULL DEFAULT 0,
    criada_em       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    iniciada_em     TIMESTAMP,
    concluida_em    TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_campanhas_perfil_unica
ON campanhas(perfil_id) WHERE status IN ('ativa', 'pausada');
"""


def _migrar() -> None:
    """Migrações idempotentes para schemas que mudaram entre versões."""
    with get_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(agendamentos)")}
        novas = [
            ("nome",            "TEXT NOT NULL DEFAULT ''"),
            ("frequencia",      "TEXT NOT NULL DEFAULT 'diario'"),
            ("hora",             "TEXT NOT NULL DEFAULT '03:00'"),
            ("data",             "TEXT"),
            ("dia_semana",       "INTEGER"),
            ("dia_mes",          "INTEGER"),
            ("perfil_id",        "INTEGER"),
            ("filtro_estado",    "TEXT"),
            ("filtro_tribunal",  "TEXT"),
            ("quantidade",       "INTEGER"),
        ]
        for col, ddl in novas:
            if col not in cols:
                conn.execute(f"ALTER TABLE agendamentos ADD COLUMN {col} {ddl}")

        cols_envios = {r["name"] for r in conn.execute("PRAGMA table_info(envios)")}
        novas_envios = [
            ("message_id",         "TEXT"),
            ("bounce_em",          "TIMESTAMP"),
            ("bounce_codigo",      "TEXT"),
            ("bounce_diagnostico", "TEXT"),
            ("tracking_token",     "TEXT"),
        ]
        for col, ddl in novas_envios:
            if col not in cols_envios:
                conn.execute(f"ALTER TABLE envios ADD COLUMN {col} {ddl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_envios_message_id ON envios(message_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_envios_tracking_token ON envios(tracking_token)")

        cols_aberturas = {r["name"] for r in conn.execute("PRAGMA table_info(aberturas)")}
        if "tipo" not in cols_aberturas:
            conn.execute("ALTER TABLE aberturas ADD COLUMN tipo TEXT")
        # Re-classifica registros (sem tipo OU classificados antes da
        # detecção por IP) — agora também olha IP de proxy do Google/Yahoo/MS.
        # Idempotente: roda toda partida, mas só atualiza linhas com diff.
        conn.execute("""
            UPDATE aberturas SET tipo = 'proxy'
            WHERE (tipo IS NULL OR tipo = 'cliente') AND (
                  user_agent LIKE '%GoogleImageProxy%'
              OR user_agent LIKE '%YahooMailProxy%'
              OR user_agent LIKE '%BingPreview%'
              OR user_agent LIKE '%MicrosoftPreview%'
              OR user_agent LIKE '%SkypeUriPreview%'
              OR user_agent LIKE '%OutlookSafe%'
              OR user_agent LIKE '%ggpht.com%'
              OR user_agent LIKE '%FeedFetcher-Google%'
              OR ip LIKE '66.102.%' OR ip LIKE '66.249.%'
              OR ip LIKE '209.85.1%' OR ip LIKE '209.85.2%'
              OR ip LIKE '173.194.%'
              OR ip LIKE '64.233.1%' OR ip LIKE '64.233.2%'
              OR ip LIKE '72.14.%'
              OR ip LIKE '64.18.%'
              OR ip LIKE '98.137.%' OR ip LIKE '87.248.%'
              OR ip LIKE '40.92.%' OR ip LIKE '40.107.%'
              OR ip LIKE '52.100.%' OR ip LIKE '52.101.%'
              OR ip LIKE '52.102.%' OR ip LIKE '52.103.%'
            )
        """)
        # Marca como 'cliente' qualquer linha que sobrou sem tipo
        conn.execute("UPDATE aberturas SET tipo = 'cliente' WHERE tipo IS NULL")

        # Coluna campanha_id em envios (relê PRAGMA para ser idempotente em reruns)
        cols_envios_atual = {r["name"] for r in conn.execute("PRAGMA table_info(envios)")}
        if "campanha_id" not in cols_envios_atual:
            conn.execute("ALTER TABLE envios ADD COLUMN campanha_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_envios_campanha "
            "ON envios(campanha_id)"
        )

        # Limpa agendamentos legados do tipo campanha (substituídos pela tabela campanhas)
        conn.execute("DELETE FROM agendamentos WHERE tipo = 'campanha'")

        cols_perfis = {r["name"] for r in conn.execute("PRAGMA table_info(perfis_remetente)")}
        novas_perfis = [
            ("imap_host",        "TEXT"),
            ("imap_port",        "INTEGER NOT NULL DEFAULT 993"),
            ("imap_ativo",       "INTEGER NOT NULL DEFAULT 1"),
            ("imap_ultimo_uid",  "INTEGER NOT NULL DEFAULT 0"),
        ]
        for col, ddl in novas_perfis:
            if col not in cols_perfis:
                conn.execute(f"ALTER TABLE perfis_remetente ADD COLUMN {col} {ddl}")


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
    _migrar()
