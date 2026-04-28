import smtplib

import pytest
from app.campanhas import parse_dias_semana, format_dias_semana
from app.campanhas import classificar_erro_smtp, ErroSmtp


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
