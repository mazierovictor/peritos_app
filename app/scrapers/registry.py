"""
Registro centralizado dos scrapers disponíveis.

Cada entrada descreve um scraper:
  - sigla:         identificador curto (ex.: "tjmg")
  - nome:          nome amigável
  - estado:        UF do tribunal (usado pra preencher o campo Estado dos contatos)
  - sistema:       sistema processual usado pelo TJ (PJe / eproc / SAJ / etc)
  - script:        nome do arquivo dentro de external_scripts/
  - xlsx:          nome do .xlsx que o script gera no cwd
  - requer_browser: True se o script usa Selenium (o container precisa ter Chromium)
  - manual:        True se o script exige interação humana (CAPTCHA) — fica desabilitado na UI
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScraperInfo:
    sigla: str
    nome: str
    estado: str
    sistema: str
    script: str
    xlsx: str
    requer_browser: bool = False
    manual: bool = False


SCRAPERS: dict[str, ScraperInfo] = {
    s.sigla: s for s in [
        ScraperInfo("tjmg", "TJ Minas Gerais",        "MG", "PJe",   "tjmg_scraper.py",  "tjmg_guia_judiciario.xlsx"),
        ScraperInfo("tjsp", "TJ São Paulo",           "SP", "SAJ",   "tjsp_scraper.py",  "tjsp_guia_judiciario.xlsx"),
        ScraperInfo("tjrj", "TJ Rio de Janeiro",      "RJ", "PJe",   "tjrj_scraper.py",  "tjrj_guia_judiciario.xlsx"),
        ScraperInfo("tjpa", "TJ Pará",                "PA", "PJe",   "tjpa_scraper.py",  "tjpa_guia_judiciario.xlsx"),
        ScraperInfo("tjrn", "TJ Rio Grande do Norte", "RN", "PJe",   "tjrn_scraper.py",  "tjrn_guia_judiciario.xlsx"),
        ScraperInfo("tjdft", "TJ Distrito Federal",   "DF", "PJe",   "tjdft_scraper.py", "tjdft_guia_judiciario.xlsx"),
        ScraperInfo("tjal", "TJ Alagoas",             "AL", "PJe",   "tjal_scraper.py",  "tjal_guia_judiciario.xlsx"),
        ScraperInfo("tjrs", "TJ Rio Grande do Sul",   "RS", "eproc", "tjrs_scraper.py",  "tjrs_guia_judiciario.xlsx", requer_browser=True),
        ScraperInfo("tjro", "TJ Rondônia",            "RO", "PJe",   "tjro_scraper.py",  "tjro_guia_judiciario.xlsx", requer_browser=True),
        ScraperInfo("tjmt", "TJ Mato Grosso",         "MT", "PJe",   "tjmt_scraper.py",  "tjmt_guia_judiciario.xlsx", requer_browser=True),
        ScraperInfo("tjsc", "TJ Santa Catarina",      "SC", "eproc", "tjsc_scraper.py",  "tjsc_guia_judiciario.xlsx", requer_browser=True, manual=True),
    ]
}


def listar() -> list[ScraperInfo]:
    return list(SCRAPERS.values())


def get(sigla: str) -> ScraperInfo | None:
    return SCRAPERS.get(sigla.lower())
