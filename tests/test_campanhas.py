import pytest
from app.campanhas import parse_dias_semana, format_dias_semana


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
