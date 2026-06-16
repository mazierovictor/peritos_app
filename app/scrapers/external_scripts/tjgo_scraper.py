"""
TJGO - Agenda Eletrônica (SIGO) - Web Scraper
==============================================
Coleta e-mails de órgãos jurisdicionais do TJGO a partir da API JSON pública:
  https://sigo-backend.tjgo.jus.br/api/agenda/publico/localidades

Gera tjgo_guia_judiciario.xlsx com as colunas: Cidade | Órgão | E-mail

Dependências:
    pip install requests openpyxl
"""
from __future__ import annotations

import time
import unicodedata
import logging

import requests
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

# ──────────────────────────────────────────────
# Configurações
# ──────────────────────────────────────────────
API_URL = "https://sigo-backend.tjgo.jus.br/api/agenda/publico/localidades"
OUTPUT_FILE = "tjgo_guia_judiciario.xlsx"
PAGE_SIZE = 1000              # o servidor limita ~2000 por página
DELAY_BETWEEN_REQUESTS = 1    # segundos entre páginas (respeite o servidor)
MAX_RETRIES = 4
RETRY_DELAY = 5              # segundos entre tentativas

# Allowlist jurisdicional (sem acento, minúsculas). Calibrada contra os dados
# reais: 'secretaria' fica DE FORA (captura secretarias administrativas);
# 'forum' fica DENTRO (senão perde os fóruns das comarcas).
ALLOWED_ORGANS = [
    "vara",
    "juizado",
    "jurisdicional",
    "cejusc",
    "turma recursal",
    "forum",
    "contadoria",
    "tribunal do juri",
    "auditoria militar",
]

# ─── Override pela UI (não altera a lógica; só substitui a lista se houver config) ───
try:
    import json as _json_ui
    with open("scraper_config.json", encoding="utf-8") as _f_ui:
        _UI_CFG = _json_ui.load(_f_ui)
    if isinstance(_UI_CFG.get("palavras_chave"), list) and _UI_CFG["palavras_chave"]:
        ALLOWED_ORGANS = [str(x) for x in _UI_CFG["palavras_chave"]]
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# ──────────────────────────────────────────────
# Filtragem (idêntica aos demais scrapers)
# ──────────────────────────────────────────────
_EXC_CRIMINAL = (
    "criminal", "criminais", "crime", "penal", "penais", "penas",
    "execucao penal", "execucoes penais", "socioeducativ", "socieducativ",
    "do juri", "de juri", "juiz de garantias", "juizo de garantias",
)
_EXC_INFANCIA = ("infancia", "juventude")
_CIVEL_OVERRIDE = (
    "civel", "civil", "fazenda", "fiscal", "fiscais", "familia", "sucessoes", "orfaos",
    "empresarial", "unica", "unico", "jurisdicional", "precatoria", "divida",
    "falencia", "recupera", "acidente",
)


def normalize_text(text: str) -> str:
    """Remove acentos e converte para minúsculas para facilitar a busca."""
    text = unicodedata.normalize("NFD", text or "")
    text = text.encode("ascii", "ignore").decode("utf-8")
    return text.lower()


def _excluir_orgao(nome_norm: str) -> bool:
    """True se a unidade for exclusivamente criminal/penal ou de infância/juventude.
    Recebe o nome JÁ normalizado (minúsculo, sem acento)."""
    suspeito = (any(t in nome_norm for t in _EXC_CRIMINAL)
                or any(t in nome_norm for t in _EXC_INFANCIA))
    if not suspeito:
        return False
    return not any(t in nome_norm for t in _CIVEL_OVERRIDE)


def is_organ_allowed(orgao_name: str) -> bool:
    """Mantém qualquer vara/juizado/unidade jurisdicional (genérica inclusive) e
    os órgãos de apoio configurados em ALLOWED_ORGANS; descarta criminal/infância
    puros."""
    norm_name = normalize_text(orgao_name).strip()
    if _excluir_orgao(norm_name):
        return False
    if "vara" in norm_name or "juizado" in norm_name or "jurisdicional" in norm_name:
        return True
    return any(keyword in norm_name for keyword in ALLOWED_ORGANS)
