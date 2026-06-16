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


def test_extract_rows_split_email_barra():
    """Quando a API retorna dois e-mails separados por '/', gera uma linha por e-mail."""
    mod = _load()
    rows = mod.extract_rows([
        _loc("Vara Cível de Anápolis", "vara1@tjgo.jus.br/vara2@tjgo.jus.br", "Anápolis"),
    ])
    assert rows == [
        {"cidade": "Anápolis", "orgao": "Vara Cível de Anápolis", "email": "vara1@tjgo.jus.br"},
        {"cidade": "Anápolis", "orgao": "Vara Cível de Anápolis", "email": "vara2@tjgo.jus.br"},
    ]


def test_extract_rows_lowercase_email():
    """E-mails com letras maiúsculas são normalizados para minúsculas."""
    mod = _load()
    rows = mod.extract_rows([
        _loc("Vara da Fazenda", "VaraFazenda@TJGO.JUS.BR", "Goiânia"),
    ])
    assert rows == [
        {"cidade": "Goiânia", "orgao": "Vara da Fazenda", "email": "varafazenda@tjgo.jus.br"},
    ]


class _FakeResp:
    def __init__(self, payload, fail=False):
        self._payload = payload
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            import requests
            raise requests.RequestException("boom")

    def json(self):
        return self._payload


class _FakeSession:
    """Sessão fake: serve páginas e pode falhar N vezes antes de cada sucesso."""
    def __init__(self, paginas, falhas_por_pagina=0):
        self._paginas = paginas
        self._falhas = falhas_por_pagina
        self._falhas_restantes = falhas_por_pagina
        self.chamadas = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.chamadas += 1
        if self._falhas_restantes > 0:
            self._falhas_restantes -= 1
            return _FakeResp(None, fail=True)
        self._falhas_restantes = self._falhas
        page = params["page"]
        return _FakeResp(self._paginas[page])


def _pagina(itens, has_next):
    return {"success": True, "data": itens,
            "page": {"number": 0, "size": 1000, "totalElements": 0,
                     "totalPages": 0, "hasNext": has_next, "hasPrevious": False}}


def test_fetch_pagina_ate_hasnext_false(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
    paginas = [
        _pagina([{"nome": "A"}], has_next=True),
        _pagina([{"nome": "B"}], has_next=False),
    ]
    sess = _FakeSession(paginas)
    todos = mod.fetch_all_localidades(sess)
    assert [d["nome"] for d in todos] == ["A", "B"]


def test_fetch_faz_retry_e_recupera(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
    paginas = [_pagina([{"nome": "A"}], has_next=False)]
    sess = _FakeSession(paginas, falhas_por_pagina=2)  # falha 2x, sucede na 3ª
    todos = mod.fetch_all_localidades(sess)
    assert [d["nome"] for d in todos] == ["A"]
    assert sess.chamadas == 3


def test_fetch_estoura_apos_max_retries(monkeypatch):
    import pytest
    mod = _load()
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
    paginas = [_pagina([{"nome": "A"}], has_next=False)]
    sess = _FakeSession(paginas, falhas_por_pagina=99)  # nunca sucede
    with pytest.raises(RuntimeError):
        mod.fetch_all_localidades(sess)
