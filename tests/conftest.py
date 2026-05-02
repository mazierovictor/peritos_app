"""
Fixtures compartilhadas. Cada teste roda contra um SQLite temporário
isolado, com schema criado do zero, e aponta o mailer/campanhas para esse banco.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def db_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    Cria um diretório de dados temporário, aponta `settings.data_dir`
    para ele e inicializa o schema. Retorna o Path do diretório.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))

    # Importa db DEPOIS do monkeypatch para que get_conn use o novo path.
    from app import db
    db.init_db()
    return tmp_path


def _inserir_perfil_basico() -> int:
    from app.db import get_conn

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO usuarios (id, email, nome, senha_hash) "
            "VALUES (1, 'usuario@ex.com', 'Usuario Teste', 'hash')",
        )
        cur = conn.execute(
            "INSERT INTO perfis_remetente "
            "(usuario_id, nome, email_remetente, smtp_host, smtp_port, "
            " smtp_senha_enc, assunto, corpo_texto, corpo_html, limite_diario) "
            "VALUES (1, 'Teste', 'teste@ex.com', 'smtp.ex.com', 587, "
            " 'enc', 'assunto', 'txt', '<p>html</p>', 250)",
        )
        return cur.lastrowid


@pytest.fixture
def perfil_id(db_temp):
    """Perfil + pool de contatos suficiente para os testes que criam campanha
    (a validação de disponibilidade exige total_alvo <= contatos elegíveis)."""
    from app.db import get_conn

    pid = _inserir_perfil_basico()
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO contatos (email, estado, tribunal) VALUES (?, ?, ?)",
            [(f"alvo{i}@ex.com", "SP", "tjsp") for i in range(1100)],
        )
    return pid


@pytest.fixture
def perfil_id_sem_contatos(db_temp):
    """Variante para testes que precisam montar a tabela `contatos` do zero."""
    return _inserir_perfil_basico()
