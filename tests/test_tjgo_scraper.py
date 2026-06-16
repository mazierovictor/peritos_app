"""Testes do scraper TJGO (extração e fetch), sem acesso à rede."""
from __future__ import annotations

import importlib.util
import pathlib

BASE = pathlib.Path(__file__).resolve().parents[1] / "app" / "scrapers" / "external_scripts"


def _load():
    path = BASE / "tjgo_scraper.py"
    spec = importlib.util.spec_from_file_location("_scr_tjgo", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _loc(nome, email, cidade="Goiânia"):
    return {"nome": nome, "email": email, "predio": {"cidade": cidade}}


def test_extract_rows_mapeia_colunas_e_filtra():
    mod = _load()
    localidades = [
        _loc("2ª Vara Cível de Formosa", "vara2@tjgo.jus.br", "Formosa"),
        _loc("Núcleo de Telecomunicações", "divitel@tjgo.jus.br"),   # administrativo -> fora
        _loc("1ª Vara Criminal de Goiânia", "crim@tjgo.jus.br"),     # criminal puro -> fora
        _loc("Fórum de Abadiânia", "forum@tjgo.jus.br", "Abadiânia"),
        _loc("Vara sem email", ""),                                   # sem e-mail -> fora
        _loc("Diretoria Administrativa", "texto-sem-arroba"),         # e-mail inválido -> fora
    ]
    rows = mod.extract_rows(localidades)
    assert rows == [
        {"cidade": "Formosa", "orgao": "2ª Vara Cível de Formosa", "email": "vara2@tjgo.jus.br"},
        {"cidade": "Abadiânia", "orgao": "Fórum de Abadiânia", "email": "forum@tjgo.jus.br"},
    ]


def test_extract_rows_predio_ausente_vira_cidade_vazia():
    mod = _load()
    rows = mod.extract_rows([{"nome": "Vara Única", "email": "vu@tjgo.jus.br", "predio": None}])
    assert rows == [{"cidade": "", "orgao": "Vara Única", "email": "vu@tjgo.jus.br"}]
