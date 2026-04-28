import smtplib
from datetime import datetime, time

import pytest
from app.campanhas import parse_dias_semana, format_dias_semana
from app.campanhas import classificar_erro_smtp, ErroSmtp
from app.campanhas import (
    Acao, AcaoTipo, proxima_acao, EstadoCampanha,
)
from app.campanhas import criar, listar, obter
from app.campanhas import (
    iniciar, pausar, retomar, cancelar, marcar_concluida, editar,
)
from app.campanhas import enviados_hoje_campanha, enviados_hoje_perfil, montar_estado_campanha
from app.campanhas import selecionar_proximo_contato


def test_parse_dias_semana_basico():
    assert parse_dias_semana("0,1,2,3,4") == {0, 1, 2, 3, 4}


def test_parse_dias_semana_vazio_e_espacos():
    assert parse_dias_semana("") == set()
    assert parse_dias_semana("0, 1 ,  6") == {0, 1, 6}


def test_parse_dias_semana_invalidos_levanta():
    with pytest.raises(ValueError):
        parse_dias_semana("7")
    with pytest.raises(ValueError):
        parse_dias_semana("a,b")
    with pytest.raises(ValueError):
        parse_dias_semana("-1")


def test_format_dias_semana_ordena_e_dedup():
    assert format_dias_semana({4, 0, 1, 1, 2, 3}) == "0,1,2,3,4"
    assert format_dias_semana(set()) == ""


def test_classificar_auth_e_fatal():
    e = smtplib.SMTPAuthenticationError(535, b"5.7.8 username and password not accepted")
    assert classificar_erro_smtp(e) is ErroSmtp.FATAL


def test_classificar_535_em_outra_excecao_e_fatal():
    e = smtplib.SMTPResponseException(535, "Authentication credentials invalid")
    assert classificar_erro_smtp(e) is ErroSmtp.FATAL


def test_classificar_disconnect_e_transiente():
    assert classificar_erro_smtp(smtplib.SMTPServerDisconnected("eof")) is ErroSmtp.TRANSIENTE


def test_classificar_connect_error_e_transiente():
    assert classificar_erro_smtp(smtplib.SMTPConnectError(421, "service unavailable")) is ErroSmtp.TRANSIENTE


def test_classificar_recipient_refused_e_por_contato():
    e = smtplib.SMTPRecipientsRefused({"x@y.com": (550, b"no such user")})
    assert classificar_erro_smtp(e) is ErroSmtp.POR_CONTATO


def test_classificar_timeout_e_transiente():
    assert classificar_erro_smtp(TimeoutError("timed out")) is ErroSmtp.TRANSIENTE


def test_classificar_desconhecido_e_por_contato():
    assert classificar_erro_smtp(RuntimeError("???")) is ErroSmtp.POR_CONTATO


# ---------------------------------------------------------------------------
# Task 5 — proxima_acao
# ---------------------------------------------------------------------------

def _ec(**kw):
    """Helper para construir EstadoCampanha com defaults razoáveis."""
    base = dict(
        id=1,
        status="ativa",
        total_alvo=1000,
        por_dia=200,
        enviados_total=300,
        enviados_hoje=50,
        enviados_hoje_perfil=50,
        perfil_limite_diario=250,
        dias_semana={0, 1, 2, 3, 4},     # seg-sex
        janela_inicio=time(9, 0),
        janela_fim=time(17, 0),
    )
    base.update(kw)
    return EstadoCampanha(**base)


def test_proxima_acao_dentro_da_janela_em_dia_valido_dispara_envio():
    now = datetime(2026, 4, 28, 14, 0)  # terça (weekday=1), 14:00
    a = proxima_acao(_ec(), now)
    assert a.tipo is AcaoTipo.ENVIAR


def test_proxima_acao_antes_da_janela_dorme_ate_inicio():
    now = datetime(2026, 4, 28, 8, 30)  # terça, 8:30
    a = proxima_acao(_ec(), now)
    assert a.tipo is AcaoTipo.DORMIR_ATE
    assert a.dormir_ate == datetime(2026, 4, 28, 9, 0)


def test_proxima_acao_depois_da_janela_dorme_ate_proximo_dia_valido():
    now = datetime(2026, 4, 28, 17, 30)  # terça, 17:30
    a = proxima_acao(_ec(), now)
    assert a.tipo is AcaoTipo.DORMIR_ATE
    assert a.dormir_ate == datetime(2026, 4, 29, 9, 0)  # quarta 9h


def test_proxima_acao_em_fim_de_semana_pula_para_segunda():
    now = datetime(2026, 5, 2, 10, 0)  # sábado (5), 10:00
    a = proxima_acao(_ec(), now)
    assert a.tipo is AcaoTipo.DORMIR_ATE
    assert a.dormir_ate == datetime(2026, 5, 4, 9, 0)  # segunda 9h


def test_proxima_acao_quota_diaria_atingida_pula_dia():
    now = datetime(2026, 4, 28, 14, 0)
    a = proxima_acao(_ec(enviados_hoje=200), now)  # bateu por_dia
    assert a.tipo is AcaoTipo.DORMIR_ATE
    assert a.dormir_ate == datetime(2026, 4, 29, 9, 0)


def test_proxima_acao_quota_perfil_atingida_pula_dia():
    now = datetime(2026, 4, 28, 14, 0)
    # perfil já enviou 250 (limite); campanha ainda quer mandar
    a = proxima_acao(_ec(enviados_hoje_perfil=250), now)
    assert a.tipo is AcaoTipo.DORMIR_ATE
    assert a.dormir_ate == datetime(2026, 4, 29, 9, 0)


def test_proxima_acao_total_alvo_atingido_conclui():
    now = datetime(2026, 4, 28, 14, 0)
    a = proxima_acao(_ec(enviados_total=1000), now)
    assert a.tipo is AcaoTipo.CONCLUIR


def test_proxima_acao_status_diferente_de_ativa_sai():
    now = datetime(2026, 4, 28, 14, 0)
    a = proxima_acao(_ec(status="pausada"), now)
    assert a.tipo is AcaoTipo.SAIR
    a = proxima_acao(_ec(status="cancelada"), now)
    assert a.tipo is AcaoTipo.SAIR


def test_intervalo_calculado_pela_janela_e_quota_restante():
    """Quanto mais perto do fim da janela, menor o intervalo."""
    now = datetime(2026, 4, 28, 9, 0)  # início da janela, 8h restantes
    a = proxima_acao(_ec(enviados_hoje=0, por_dia=200), now)
    assert a.tipo is AcaoTipo.ENVIAR
    # 8h * 3600 / 200 = 144s por envio
    assert 130 <= a.intervalo_seg <= 160

    now2 = datetime(2026, 4, 28, 16, 30)  # 30min restantes
    a2 = proxima_acao(_ec(enviados_hoje=190, por_dia=200), now2)
    assert a2.tipo is AcaoTipo.ENVIAR
    # 30min * 60 / 10 = 180s
    assert 170 <= a2.intervalo_seg <= 200


def test_intervalo_minimo_10s():
    """Mesmo com janela apertada, não envia mais rápido que a cada 10s."""
    now = datetime(2026, 4, 28, 16, 59, 50)  # 10s da janela, mas 50 enviar
    a = proxima_acao(_ec(enviados_hoje=150, por_dia=200), now)
    assert a.tipo is AcaoTipo.ENVIAR
    assert a.intervalo_seg >= 10


# ---------------------------------------------------------------------------
# Task 6 — criar / listar / obter
# ---------------------------------------------------------------------------

def test_criar_campanha_minima(db_temp, perfil_id):
    cid = criar(
        nome="TJSP janeiro",
        perfil_id=perfil_id,
        filtros={"estado": "SP", "tribunal": "tjsp"},
        total_alvo=1000,
        por_dia=200,
        dias_semana={0, 1, 2, 3, 4},
        janela_inicio=time(9, 0),
        janela_fim=time(17, 0),
    )
    assert cid > 0
    c = obter(cid)
    assert c["nome"] == "TJSP janeiro"
    assert c["status"] == "rascunho"
    assert c["total_alvo"] == 1000
    assert c["por_dia"] == 200
    assert c["dias_semana"] == "0,1,2,3,4"
    assert c["janela_inicio"] == "09:00"
    assert c["filtro_estado"] == "SP"
    assert c["enviados_total"] == 0


def test_listar_ordena_por_status_depois_data(db_temp, perfil_id):
    a = criar(nome="A", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
              dias_semana={0,1,2,3,4}, janela_inicio=time(9,0), janela_fim=time(17,0))
    b = criar(nome="B", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
              dias_semana={0,1,2,3,4}, janela_inicio=time(9,0), janela_fim=time(17,0))
    nomes = [c["nome"] for c in listar()]
    # ambas rascunho — empata em status, ordena por id desc (mais nova primeiro)
    assert nomes == ["B", "A"]


def test_listar_traz_perfil_nome(db_temp, perfil_id):
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    out = listar()
    assert out[0]["perfil_nome"] == "Teste"


def test_obter_inexistente_retorna_none(db_temp):
    assert obter(99999) is None


def test_criar_com_dias_vazios_levanta(db_temp, perfil_id):
    with pytest.raises(ValueError):
        criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
              dias_semana=set(), janela_inicio=time(9,0), janela_fim=time(17,0))


def test_criar_com_janela_invertida_levanta(db_temp, perfil_id):
    with pytest.raises(ValueError):
        criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
              dias_semana={0}, janela_inicio=time(17,0), janela_fim=time(9,0))


def test_criar_com_por_dia_maior_que_limite_perfil_levanta(db_temp, perfil_id):
    # perfil tem limite_diario=250 (fixture)
    with pytest.raises(ValueError):
        criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=1000, por_dia=300,
              dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))


# ---------------------------------------------------------------------------
# Task 7 — transições de estado
# ---------------------------------------------------------------------------

def test_iniciar_de_rascunho_vai_para_ativa(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    iniciar(cid)
    assert obter(cid)["status"] == "ativa"


def test_iniciar_quando_outra_ativa_no_perfil_levanta(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    a = criar(nome="A", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
              dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    b = criar(nome="B", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
              dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    iniciar(a)
    with pytest.raises(ValueError):
        iniciar(b)


def test_pausar_de_ativa_seta_motivo(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    iniciar(cid)
    pausar(cid, motivo="Teste de pausa")
    c = obter(cid)
    assert c["status"] == "pausada"
    assert c["pausa_motivo"] == "Teste de pausa"


def test_retomar_de_pausada_volta_para_ativa(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    iniciar(cid)
    pausar(cid, motivo="x")
    retomar(cid)
    c = obter(cid)
    assert c["status"] == "ativa"
    assert c["pausa_motivo"] is None


def test_cancelar_de_qualquer_estado_nao_terminal(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    cancelar(cid)
    assert obter(cid)["status"] == "cancelada"


def test_editar_so_em_rascunho(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    editar(cid, nome="Y", filtros={}, total_alvo=20, por_dia=10,
           dias_semana={1, 2}, janela_inicio=time(8,0), janela_fim=time(18,0))
    assert obter(cid)["nome"] == "Y"

    iniciar(cid)
    with pytest.raises(ValueError):
        editar(cid, nome="Z", filtros={}, total_alvo=20, por_dia=10,
               dias_semana={1}, janela_inicio=time(8,0), janela_fim=time(18,0))


def test_marcar_concluida(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    iniciar(cid)
    marcar_concluida(cid)
    c = obter(cid)
    assert c["status"] == "concluida"
    assert c["concluida_em"] is not None


# ---------------------------------------------------------------------------
# Task 9 — enviados_hoje_campanha, enviados_hoje_perfil, montar_estado_campanha
# ---------------------------------------------------------------------------

def _inserir_envio(conn, contato_id, perfil_id, *, campanha_id=None,
                   status="ok", quando_iso="now"):
    """Helper: insere uma linha em envios. quando_iso=None usa now."""
    if quando_iso == "now":
        conn.execute(
            "INSERT INTO envios (contato_id, perfil_remetente_id, "
            "campanha_id, status) VALUES (?, ?, ?, ?)",
            (contato_id, perfil_id, campanha_id, status),
        )
    else:
        conn.execute(
            "INSERT INTO envios (contato_id, perfil_remetente_id, "
            "campanha_id, status, enviado_em) VALUES (?, ?, ?, ?, ?)",
            (contato_id, perfil_id, campanha_id, status, quando_iso),
        )


def _criar_contato(conn, email="x@y.com", tribunal="tjsp"):
    cur = conn.execute(
        "INSERT INTO contatos (email, tribunal) VALUES (?, ?)",
        (email, tribunal),
    )
    return cur.lastrowid


def test_enviados_hoje_campanha_conta_so_ok_e_de_hoje(db_temp, perfil_id):
    from app.db import get_conn
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=100, por_dia=10,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    with get_conn() as conn:
        c1 = _criar_contato(conn, "a@x.com")
        c2 = _criar_contato(conn, "b@x.com")
        c3 = _criar_contato(conn, "c@x.com")
        _inserir_envio(conn, c1, perfil_id, campanha_id=cid)
        _inserir_envio(conn, c2, perfil_id, campanha_id=cid)
        _inserir_envio(conn, c3, perfil_id, campanha_id=cid, status="erro")
        # ontem, não conta
        _inserir_envio(conn, c1, perfil_id, campanha_id=cid,
                       quando_iso="2020-01-01 12:00:00")
    assert enviados_hoje_campanha(cid) == 2


def test_enviados_hoje_perfil_ignora_teste(db_temp, perfil_id):
    from app.db import get_conn
    with get_conn() as conn:
        c1 = _criar_contato(conn, "a@x.com", tribunal="tjsp")
        c2 = _criar_contato(conn, "b@x.com", tribunal="_teste")
        _inserir_envio(conn, c1, perfil_id)
        _inserir_envio(conn, c2, perfil_id)
    assert enviados_hoje_perfil(perfil_id) == 1


def test_montar_estado_campanha_combina_tudo(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=100, por_dia=10,
                dias_semana={0,1,2,3,4}, janela_inicio=time(9,0), janela_fim=time(17,0))
    iniciar(cid)
    e = montar_estado_campanha(cid)
    assert e.id == cid
    assert e.status == "ativa"
    assert e.por_dia == 10
    assert e.perfil_limite_diario == 250
    assert e.dias_semana == {0,1,2,3,4}
    assert e.janela_inicio == time(9, 0)


# ---------------------------------------------------------------------------
# Task 10 — selecionar_proximo_contato
# ---------------------------------------------------------------------------

def test_selecionar_proximo_contato_respeita_filtros(db_temp, perfil_id):
    from app.db import get_conn
    cid = criar(nome="X", perfil_id=perfil_id,
                filtros={"estado": "SP", "tribunal": "tjsp"},
                total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    with get_conn() as conn:
        conn.execute("INSERT INTO contatos (email, tribunal, estado) "
                     "VALUES ('a@x.com', 'tjsp', 'SP')")
        conn.execute("INSERT INTO contatos (email, tribunal, estado) "
                     "VALUES ('b@x.com', 'tjmg', 'MG')")  # não casa
        conn.execute("INSERT INTO contatos (email, tribunal, estado, invalido) "
                     "VALUES ('c@x.com', 'tjsp', 'SP', 1)")  # inválido
    contato = selecionar_proximo_contato(cid)
    assert contato["email"] == "a@x.com"


def test_selecionar_proximo_contato_pula_ja_enviados(db_temp, perfil_id):
    from app.db import get_conn
    cid = criar(nome="X", perfil_id=perfil_id, filtros={},
                total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    with get_conn() as conn:
        c1 = _criar_contato(conn, "a@x.com")
        c2 = _criar_contato(conn, "b@x.com")
        # c1 já recebeu OK desse perfil — pula
        _inserir_envio(conn, c1, perfil_id)
    contato = selecionar_proximo_contato(cid)
    assert contato["email"] == "b@x.com"


def test_selecionar_proximo_sem_contato_retorna_none(db_temp, perfil_id):
    cid = criar(nome="X", perfil_id=perfil_id, filtros={},
                total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    assert selecionar_proximo_contato(cid) is None
