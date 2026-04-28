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
from dataclasses import dataclass
from datetime import datetime, time, timedelta


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


@dataclass
class EstadoCampanha:
    """Snapshot do estado de uma campanha + counters do dia para decidir a próxima ação."""
    id: int
    status: str
    total_alvo: int
    por_dia: int
    enviados_total: int
    enviados_hoje: int           # da campanha, hoje
    enviados_hoje_perfil: int    # do perfil, hoje (todos os envios não-teste)
    perfil_limite_diario: int
    dias_semana: set[int]
    janela_inicio: time
    janela_fim: time


class AcaoTipo(enum.Enum):
    ENVIAR = "enviar"
    DORMIR_ATE = "dormir_ate"
    CONCLUIR = "concluir"
    SAIR = "sair"


@dataclass
class Acao:
    tipo: AcaoTipo
    dormir_ate: datetime | None = None     # válido para DORMIR_ATE
    intervalo_seg: float | None = None     # válido para ENVIAR


def _proximo_dia_valido(now: datetime, dias_semana: set[int],
                        janela_inicio: time) -> datetime:
    """Próximo datetime em que estamos em um dia da semana permitido, na hora de início da janela."""
    candidato = (now + timedelta(days=1)).replace(
        hour=janela_inicio.hour, minute=janela_inicio.minute,
        second=0, microsecond=0,
    )
    for _ in range(8):  # no pior caso, 7 dias até achar
        if candidato.weekday() in dias_semana:
            return candidato
        candidato += timedelta(days=1)
    return candidato  # fallback (não deve ocorrer se dias_semana não for vazio)


def proxima_acao(c: EstadoCampanha, now: datetime) -> Acao:
    if c.status != "ativa":
        return Acao(AcaoTipo.SAIR)
    if c.enviados_total >= c.total_alvo:
        return Acao(AcaoTipo.CONCLUIR)
    if not c.dias_semana:
        return Acao(AcaoTipo.SAIR)  # campanha mal configurada

    if now.weekday() not in c.dias_semana:
        return Acao(AcaoTipo.DORMIR_ATE,
                    dormir_ate=_proximo_dia_valido(now, c.dias_semana, c.janela_inicio))

    inicio_dt = now.replace(hour=c.janela_inicio.hour, minute=c.janela_inicio.minute,
                            second=0, microsecond=0)
    fim_dt = now.replace(hour=c.janela_fim.hour, minute=c.janela_fim.minute,
                         second=0, microsecond=0)

    if now < inicio_dt:
        return Acao(AcaoTipo.DORMIR_ATE, dormir_ate=inicio_dt)
    if now >= fim_dt:
        return Acao(AcaoTipo.DORMIR_ATE,
                    dormir_ate=_proximo_dia_valido(now, c.dias_semana, c.janela_inicio))

    quota = min(
        c.por_dia - c.enviados_hoje,
        c.perfil_limite_diario - c.enviados_hoje_perfil,
        c.total_alvo - c.enviados_total,
    )
    if quota <= 0:
        return Acao(AcaoTipo.DORMIR_ATE,
                    dormir_ate=_proximo_dia_valido(now, c.dias_semana, c.janela_inicio))

    seg_ate_fim = (fim_dt - now).total_seconds()
    intervalo = max(10.0, seg_ate_fim / quota)
    return Acao(AcaoTipo.ENVIAR, intervalo_seg=intervalo)
