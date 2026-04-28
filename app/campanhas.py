"""
Orquestração de campanhas persistentes: CRUD, transições de estado,
loop do worker (daemon thread por campanha ativa), reidratação no boot.

A tabela `campanhas` (em db.py) é a fonte da verdade do estado persistente.
O estado runtime das threads vivas fica em `_threads_runtime` (memória).
"""
from __future__ import annotations

import enum
import re
import smtplib


class ErroSmtp(enum.Enum):
    FATAL = "fatal"             # auth falhou ou conta suspensa — pausa imediata
    TRANSIENTE = "transiente"   # rede/timeout — retry com backoff
    POR_CONTATO = "por_contato" # destinatário inválido — segue, sem pausa


_FATAL_PATTERNS = re.compile(
    r"535|530|account.*disabled|invalid.*credentials|authentication.*failed",
    re.IGNORECASE,
)


def classificar_erro_smtp(exc: BaseException) -> ErroSmtp:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ErroSmtp.FATAL
    if isinstance(exc, smtplib.SMTPResponseException):
        msg = f"{exc.smtp_code} {exc.smtp_error!s}"
        if _FATAL_PATTERNS.search(msg):
            return ErroSmtp.FATAL
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return ErroSmtp.POR_CONTATO
    if isinstance(exc, (
        smtplib.SMTPServerDisconnected,
        smtplib.SMTPConnectError,
        TimeoutError,
        ConnectionError,
    )):
        return ErroSmtp.TRANSIENTE
    if isinstance(exc, smtplib.SMTPException):
        return ErroSmtp.POR_CONTATO  # fallback razoável p/ outros SMTPxxx
    return ErroSmtp.POR_CONTATO


def parse_dias_semana(s: str) -> set[int]:
    """Converte CSV '0,1,2' em set {0,1,2}. 0=segunda, 6=domingo."""
    if not s.strip():
        return set()
    out: set[int] = set()
    for tok in s.split(","):
        tok = tok.strip()
        v = int(tok)  # ValueError se não-inteiro
        if v < 0 or v > 6:
            raise ValueError(f"Dia da semana fora do intervalo 0-6: {v}")
        out.add(v)
    return out


def format_dias_semana(dias: set[int]) -> str:
    return ",".join(str(d) for d in sorted(dias))
