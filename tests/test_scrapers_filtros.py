"""
Testes da política de filtragem dos scrapers (external_scripts/).

Política acordada (uniforme entre todos os scrapers):
  - Mantém TODA vara/juizado/órgão judicial, inclusive varas genéricas
    numeradas ("2ª Vara") — que em comarcas pequenas/cumulativas são onde o
    perito atua.
  - Exclui APENAS o que é exclusivamente criminal/penal ou de infância e
    juventude.
  - Unidades cumulativas que também têm competência cível
    (ex.: "Vara Cível e Criminal") são MANTIDAS.

Os scrapers rodam como scripts isolados (o runner copia 1 arquivo .py para um
tmp e executa). Por isso cada um carrega suas próprias funções; aqui carregamos
cada módulo por caminho e testamos o predicado de filtragem de cada um.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

BASE = pathlib.Path(__file__).resolve().parents[1] / "app" / "scrapers" / "external_scripts"


def _load(name: str):
    path = BASE / f"{name}_scraper.py"
    spec = importlib.util.spec_from_file_location(f"_scr_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Nome do órgão -> deve ser mantido? (True = registrar / False = descartar)
CASOS = [
    ("2ª Vara", True),                       # genérica numerada (o bug relatado)
    ("Vara", True),                          # genérica sem qualificador
    ("3ª Vara Cível", True),
    ("Vara Única", True),
    ("Unidade Jurisdicional Única", True),   # nome real de comarca cumulativa (TJMG)
    ("Vara de Família", True),               # mantida pela política
    ("Vara da Fazenda Pública", True),
    ("Juizado Especial Cível", True),
    ("Vara Cível e Criminal", True),         # cumulativa -> mantém
    # nomes cumulativos REAIS coletados do TJMG (a maioria das comarcas do interior):
    ("1ª Vara Cível, Criminal e da Infância e da Juventude", True),
    ("Vara Criminal e de Execuções Fiscais", True),   # criminal + fiscal -> mantém
    ("1ª Vara Criminal", False),             # criminal puro -> exclui
    ("Vara de Execução Penal", False),       # penal puro -> exclui
    ("Vara da Infância e Juventude", False), # infância -> exclui
]

# Scrapers cujo predicado recebe o NOME do órgão e devolve bool.
PREDICADO = {
    "tjmg":  "is_organ_allowed",
    "tjrs":  "is_organ_allowed",
    "tjsp":  "is_organ_allowed",
    "tjsc":  "is_organ_allowed",
    "tjpa":  "is_valid_organ",
    "tjdft": "is_valid_organ",
    "tjal":  "is_valid_organ",
    "tjgo":  "is_organ_allowed",
}


@pytest.mark.parametrize("sigla,fn_name", sorted(PREDICADO.items()))
@pytest.mark.parametrize("nome,esperado", CASOS)
def test_predicado_filtragem(sigla, fn_name, nome, esperado):
    mod = _load(sigla)
    fn = getattr(mod, fn_name)
    assert fn(nome) is esperado, (
        f"{sigla}.{fn_name}({nome!r}) = {fn(nome)!r}, esperado {esperado!r}"
    )


# ── TJMT: filtro embutido na árvore; testamos as funções e o end-to-end ──────

def _tjmt_html_comarca(nome_vara: str) -> str:
    """HTML mínimo de uma comarca contendo uma única vara com e-mail."""
    return f"""
    <div id="root">
      <app-tree-view-item>
        <h4>Comarca de Exemplo</h4>
        <div class="conteudo">
          <app-tree-view-item>
            <h4>{nome_vara}</h4>
            <div class="conteudo">
              Contato: <a href="mailto:teste@tjmt.jus.br">teste@tjmt.jus.br</a>
            </div>
          </app-tree-view-item>
        </div>
      </app-tree-view-item>
    </div>
    """


@pytest.mark.parametrize("nome,esperado", CASOS)
def test_tjmt_process_tree(nome, esperado):
    mod = _load("tjmt")
    recs = mod.process_tree(_tjmt_html_comarca(nome))
    capturou = len(recs) > 0
    assert capturou is esperado, (
        f"tjmt process_tree(comarca com {nome!r}) capturou={capturou}, "
        f"esperado={esperado} (registros={recs})"
    )


def test_tjmt_is_vara_relevante_generica():
    mod = _load("tjmt")
    assert mod.is_vara_relevante("2ª Vara") is True
    assert mod.is_vara_relevante("Vara") is True
