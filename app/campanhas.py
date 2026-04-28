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
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .db import get_conn


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


# ---------------------------------------------------------------------------
# CRUD: criar / obter / listar
# ---------------------------------------------------------------------------

def _format_hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _validar_payload(*, total_alvo: int, por_dia: int,
                     dias_semana: set[int],
                     janela_inicio: time, janela_fim: time,
                     perfil_limite_diario: int) -> None:
    if total_alvo <= 0:
        raise ValueError("total_alvo deve ser > 0")
    if por_dia <= 0:
        raise ValueError("por_dia deve ser > 0")
    if por_dia > perfil_limite_diario:
        raise ValueError(
            f"por_dia ({por_dia}) excede o limite diário do perfil "
            f"({perfil_limite_diario})"
        )
    if not dias_semana:
        raise ValueError("Pelo menos um dia da semana é obrigatório")
    if janela_inicio >= janela_fim:
        raise ValueError("janela_inicio deve ser menor que janela_fim")


def _carregar_limite_perfil(conn, perfil_id: int) -> int:
    row = conn.execute(
        "SELECT limite_diario FROM perfis_remetente WHERE id = ?",
        (perfil_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Perfil {perfil_id} não encontrado")
    return int(row["limite_diario"])


def criar(*,
    nome: str,
    perfil_id: int,
    filtros: dict,
    total_alvo: int,
    por_dia: int,
    dias_semana: set[int],
    janela_inicio: time,
    janela_fim: time,
) -> int:
    with get_conn() as conn:
        limite = _carregar_limite_perfil(conn, perfil_id)
    _validar_payload(
        total_alvo=total_alvo, por_dia=por_dia,
        dias_semana=dias_semana,
        janela_inicio=janela_inicio, janela_fim=janela_fim,
        perfil_limite_diario=limite,
    )
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO campanhas "
            "(nome, perfil_id, filtro_estado, filtro_tribunal, "
            " total_alvo, por_dia, dias_semana, janela_inicio, janela_fim, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'rascunho')",
            (
                nome.strip(), perfil_id,
                filtros.get("estado") or None, filtros.get("tribunal") or None,
                total_alvo, por_dia,
                format_dias_semana(dias_semana),
                _format_hhmm(janela_inicio), _format_hhmm(janela_fim),
            ),
        )
        return cur.lastrowid


def obter(campanha_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM campanhas WHERE id = ?", (campanha_id,)
        ).fetchone()
    return dict(row) if row else None


_ORDEM_STATUS = {"ativa": 0, "pausada": 1, "rascunho": 2,
                 "concluida": 3, "cancelada": 4}


def listar() -> list[dict]:
    """Lista campanhas com nome do perfil resolvido. Ordenada por status, depois id desc."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.*, p.nome AS perfil_nome, p.email_remetente AS perfil_email "
            "FROM campanhas c JOIN perfis_remetente p ON p.id = c.perfil_id "
            "ORDER BY c.id DESC"
        ).fetchall()
    out = [dict(r) for r in rows]
    out.sort(key=lambda c: (_ORDEM_STATUS.get(c["status"], 99), -c["id"]))
    return out


# ---------------------------------------------------------------------------
# Runtime state (in-memory only) + thread management
# ---------------------------------------------------------------------------

# Estado runtime das threads vivas. Chave = id da campanha.
_threads_runtime: dict[int, "RuntimeEstado"] = {}
_lock = threading.Lock()


@dataclass
class RuntimeEstado:
    """Snapshot pequeno do que a thread está fazendo agora (memória apenas)."""
    campanha_id: int
    iniciado_em: datetime
    ultimo_envio_em: datetime | None = None
    proximo_envio_em: datetime | None = None
    mensagem: str = ""


def _subir_thread(campanha_id: int) -> None:
    """
    Cria daemon thread que executa loop_campanha. Stub na Task 7;
    implementação real na Task 11.
    """
    pass  # substituído na Task 11


# ---------------------------------------------------------------------------
# Transições de estado
# ---------------------------------------------------------------------------

def iniciar(campanha_id: int) -> None:
    c = obter(campanha_id)
    if c is None:
        raise ValueError(f"Campanha {campanha_id} não encontrada")
    if c["status"] not in ("rascunho", "pausada"):
        raise ValueError(f"Não pode iniciar campanha em status {c['status']!r}")
    # garante unicidade por perfil
    with get_conn() as conn:
        outro = conn.execute(
            "SELECT id FROM campanhas WHERE perfil_id = ? "
            "AND status IN ('ativa','pausada') AND id != ?",
            (c["perfil_id"], campanha_id),
        ).fetchone()
    if outro:
        raise ValueError(
            f"Perfil já tem campanha ativa/pausada (id {outro['id']})"
        )
    with get_conn() as conn:
        conn.execute(
            "UPDATE campanhas SET status='ativa', pausa_motivo=NULL, "
            "iniciada_em=COALESCE(iniciada_em, CURRENT_TIMESTAMP) "
            "WHERE id = ?",
            (campanha_id,),
        )
    _subir_thread(campanha_id)


def pausar(campanha_id: int, motivo: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE campanhas SET status='pausada', pausa_motivo=? "
            "WHERE id = ? AND status='ativa'",
            (motivo, campanha_id),
        )
    # a thread, se viva, vai detectar no próximo dormir cooperativo


def retomar(campanha_id: int) -> None:
    c = obter(campanha_id)
    if c is None or c["status"] != "pausada":
        raise ValueError("Só pode retomar campanha pausada")
    with get_conn() as conn:
        conn.execute(
            "UPDATE campanhas SET status='ativa', pausa_motivo=NULL "
            "WHERE id = ?", (campanha_id,),
        )
    _subir_thread(campanha_id)


def cancelar(campanha_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE campanhas SET status='cancelada' "
            "WHERE id = ? AND status IN ('rascunho','ativa','pausada')",
            (campanha_id,),
        )


def marcar_concluida(campanha_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE campanhas SET status='concluida', "
            "concluida_em=CURRENT_TIMESTAMP "
            "WHERE id = ? AND status='ativa'",
            (campanha_id,),
        )


def editar(campanha_id: int, *,
    nome: str, filtros: dict,
    total_alvo: int, por_dia: int,
    dias_semana: set[int],
    janela_inicio: time, janela_fim: time,
) -> None:
    c = obter(campanha_id)
    if c is None:
        raise ValueError(f"Campanha {campanha_id} não encontrada")
    if c["status"] != "rascunho":
        raise ValueError("Só é possível editar campanhas em rascunho")
    with get_conn() as conn:
        limite = _carregar_limite_perfil(conn, c["perfil_id"])
    _validar_payload(
        total_alvo=total_alvo, por_dia=por_dia,
        dias_semana=dias_semana,
        janela_inicio=janela_inicio, janela_fim=janela_fim,
        perfil_limite_diario=limite,
    )
    with get_conn() as conn:
        conn.execute(
            "UPDATE campanhas SET nome=?, filtro_estado=?, filtro_tribunal=?, "
            "total_alvo=?, por_dia=?, dias_semana=?, "
            "janela_inicio=?, janela_fim=? WHERE id = ?",
            (
                nome.strip(),
                filtros.get("estado") or None, filtros.get("tribunal") or None,
                total_alvo, por_dia,
                format_dias_semana(dias_semana),
                _format_hhmm(janela_inicio), _format_hhmm(janela_fim),
                campanha_id,
            ),
        )
