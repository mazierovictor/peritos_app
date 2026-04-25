"""
Configuração editável por scraper.

Cada scraper que suporta personalização tem APENAS um campo:
  - palavras_chave: lista de strings.

Cada scraper interpreta essa lista do jeito dele:
  - TJMG / TJRS: tipos de órgão aceitos (substitui ALLOWED_ORGANS)
  - TJSP / TJSC: termos de busca enviados ao site (substitui SEARCH_TERMS)
  - TJRJ:        atribuições filtradas (substitui ATRIBUICOES)
  - TJRN:        tipos de unidade (substitui FILTER_LIST)
  - TJMT:        núcleos / palavras-chave (substitui NUCLEO_KEYWORDS)

A lógica do scraper original continua intocada — só a lista é trocada.

Os DEFAULTS abaixo refletem o conteúdo original das constantes nos
scripts em external_scripts/. Se o usuário não salvar nada, a config
usada é exatamente o default — comportamento idêntico ao código original.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..db import get_conn


@dataclass(frozen=True)
class ConfigSchema:
    label: str = ""
    descricao: str = ""

    @property
    def disponivel(self) -> bool:
        return bool(self.label)


SCHEMAS: dict[str, ConfigSchema] = {
    "tjmg":  ConfigSchema("Tipos de órgão aceitos",
                          "Só são gravados na base os órgãos cujo nome contém alguma destas palavras."),
    "tjrs":  ConfigSchema("Tipos de órgão aceitos",
                          "Filtra os órgãos retornados de cada comarca."),
    "tjsp":  ConfigSchema("Termos de busca",
                          "Cada termo é enviado como busca no portal — quanto mais termos, mais resultados."),
    "tjsc":  ConfigSchema("Termos de busca",
                          "Cada termo é enviado como busca no portal."),
    "tjmt":  ConfigSchema("Núcleos / palavras-chave",
                          "Termos usados para filtrar núcleos no site do TJMT."),
    # Sem configuração editável (TJRJ/TJRN têm códigos numéricos junto;
    # demais não têm filtro de palavras):
    "tjrj":  ConfigSchema(),
    "tjrn":  ConfigSchema(),
    "tjro":  ConfigSchema(),
    "tjdft": ConfigSchema(),
    "tjal":  ConfigSchema(),
    "tjpa":  ConfigSchema(),
}


DEFAULTS: dict[str, list[str]] = {
    "tjmg": [
        "secretaria", "forum", "administracao",
        "vara civel", "vara de fazenda publica", "vara de familia",
        "vara unica", "contadoria",
    ],
    "tjrs": [
        "secretaria", "forum", "administracao", "vara civel",
        "vara da fazenda publica", "vara de familia", "vara unica", "contadoria",
    ],
    "tjsp": [
        "vara civel", "vara da fazenda publica", "vara de familia",
        "vara unica", "secretaria", "forum", "contadoria",
    ],
    "tjsc": [
        "vara civel", "vara da fazenda publica", "vara de familia",
        "vara unica", "secretaria", "forum", "contadoria",
    ],
    "tjrj": [
        "Cível", "Família", "Fazenda Pública", "Cartório",
    ],
    "tjrn": [
        "vara civel", "vara da fazenda publica", "vara de familia",
        "vara unica", "secretaria",
    ],
    "tjmt": [],
    "tjro":  [],
    "tjdft": [],
    "tjal":  [],
    "tjpa":  [],
}


def palavras_chave(sigla: str) -> list[str]:
    """Retorna as palavras-chave salvas para o scraper, ou as defaults."""
    sigla = sigla.lower()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT config_json FROM scraper_configs WHERE sigla = ?", (sigla,)
        ).fetchone()
    if row:
        try:
            data = json.loads(row["config_json"])
            if isinstance(data, dict) and isinstance(data.get("palavras_chave"), list):
                return [str(x) for x in data["palavras_chave"]]
        except Exception:
            pass
    return list(DEFAULTS.get(sigla, []))


def salvar_palavras_chave(sigla: str, palavras: list[str]) -> None:
    sigla = sigla.lower()
    payload = json.dumps({"palavras_chave": palavras}, ensure_ascii=False)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO scraper_configs (sigla, config_json) VALUES (?, ?) "
            "ON CONFLICT(sigla) DO UPDATE SET config_json = excluded.config_json, "
            "atualizado_em = CURRENT_TIMESTAMP",
            (sigla, payload),
        )


def resetar(sigla: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM scraper_configs WHERE sigla = ?", (sigla.lower(),))


def schema(sigla: str) -> ConfigSchema:
    return SCHEMAS.get(sigla.lower(), ConfigSchema())


def get_runtime_config(sigla: str) -> dict[str, Any]:
    """Config a ser despejada como JSON no cwd do subprocess do scraper."""
    return {"palavras_chave": palavras_chave(sigla)}
