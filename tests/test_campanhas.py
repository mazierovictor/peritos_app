import smtplib
from datetime import datetime, time

import pytest
from app.campanhas import parse_dias_semana, format_dias_semana
from app.campanhas import classificar_erro_smtp, ErroSmtp
from app.campanhas import (
    Acao, AcaoTipo, proxima_acao, EstadoCampanha,
)


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
