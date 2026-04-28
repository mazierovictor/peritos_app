import pytest


def test_schema_cria_tabela_campanhas(db_temp):
    from app.db import get_conn
    with get_conn() as conn:
        nomes = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        cols_envios = {r["name"] for r in conn.execute("PRAGMA table_info(envios)")}
        cols_camp = {r["name"] for r in conn.execute("PRAGMA table_info(campanhas)")}
        indices = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}

    assert "campanhas" in nomes
    assert "campanha_id" in cols_envios
    esperadas = {
        "id", "nome", "perfil_id", "filtro_estado", "filtro_tribunal",
        "total_alvo", "por_dia", "dias_semana",
        "janela_inicio", "janela_fim",
        "status", "pausa_motivo",
        "enviados_total",
        "criada_em", "iniciada_em", "concluida_em",
    }
    assert esperadas.issubset(cols_camp)
    assert "idx_campanhas_perfil_unica" in indices


def test_unicidade_campanha_ativa_por_perfil(db_temp, perfil_id):
    """Não pode haver duas campanhas em status ativa/pausada para o mesmo perfil."""
    import sqlite3
    from app.db import get_conn

    sql = (
        "INSERT INTO campanhas "
        "(nome, perfil_id, total_alvo, por_dia, dias_semana, "
        " janela_inicio, janela_fim, status) "
        "VALUES (?, ?, 100, 50, '0,1,2,3,4', '09:00', '17:00', 'ativa')"
    )
    with get_conn() as conn:
        conn.execute(sql, ("c1", perfil_id))

    with get_conn() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, ("c2", perfil_id))


def test_legacy_agendamento_campanha_e_deletado(db_temp):
    """Migração apaga linhas antigas de agendamento tipo='campanha'."""
    from app.db import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agendamentos (nome, tipo, alvo, cron) "
            "VALUES ('legacy', 'campanha', '', '')"
        )
    # roda migrar de novo simulando reboot
    from app import db as dbmod
    dbmod._migrar()
    with get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM agendamentos WHERE tipo='campanha'"
        ).fetchone()["c"]
    assert n == 0
