"""
Agendador de scrapers via APScheduler.

Cada agendamento tem:
  - nome:         rótulo amigável
  - tipo:         'scraper'
  - alvo:         sigla do TJ (ou 'todos')
  - frequencia:   uma_vez | diario | semanal | mensal
  - hora:         "HH:MM"
  - data:         "YYYY-MM-DD"  (frequencia = uma_vez)
  - dia_semana:   0-6 (0=segunda)  (frequencia = semanal)
  - dia_mes:      1-28             (frequencia = mensal)

O fuso usado é America/Sao_Paulo, então o usuário escolhe o horário local.

Cada execução é registrada em `cron_runs` para auditoria.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .db import get_conn


log = logging.getLogger("peritos.scheduler")

TZ = "America/Sao_Paulo"
_scheduler: Optional[BackgroundScheduler] = None


# ─── Log de execuções (cron_runs) ──────────────────────────────────────

def _registrar_inicio(ag_id: int | None, nome: str | None, tipo: str | None,
                      fonte: str = "agendamento") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO cron_runs (ag_id, nome, tipo, fonte, status) "
            "VALUES (?, ?, ?, ?, 'rodando')",
            (ag_id, nome, tipo, fonte),
        )
        return cur.lastrowid


def _registrar_fim(run_id: int, status: str, mensagem: str | None = None) -> None:
    if not run_id:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE cron_runs SET finalizado_em = CURRENT_TIMESTAMP, "
            "status = ?, mensagem = ? WHERE id = ?",
            (status, (mensagem or None), run_id),
        )


def _registrar_evento_externo(ag_id: int | None, nome: str | None, tipo: str | None,
                              status: str, mensagem: str) -> None:
    """Registra eventos que não passaram por _executar_job (missed, error externo)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO cron_runs (ag_id, nome, tipo, fonte, status, mensagem, finalizado_em) "
            "VALUES (?, ?, ?, 'agendamento', ?, ?, CURRENT_TIMESTAMP)",
            (ag_id, nome, tipo, status, mensagem[:500] if mensagem else None),
        )


# ─── Execução do job ───────────────────────────────────────────────────

def _executar_job(ag_id: int) -> None:
    """Carrega o agendamento atual do banco e dispara a ação correspondente."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM agendamentos WHERE id = ?", (ag_id,)).fetchone()
    if not row:
        run_id = _registrar_inicio(ag_id, None, None)
        _registrar_fim(run_id, "erro", f"Agendamento {ag_id} não encontrado")
        return
    ag = dict(row)
    run_id = _registrar_inicio(ag["id"], ag.get("nome"), ag.get("tipo"))

    try:
        if ag["tipo"] == "scraper":
            from .scrapers import runner as scraper_runner
            alvo = (ag.get("alvo") or "").lower()
            if alvo == "todos":
                scraper_runner.disparar_todos()
                _registrar_fim(run_id, "ok", "Scrapers (todos) disparados")
            else:
                scraper_runner.disparar(ag["alvo"])
                _registrar_fim(run_id, "ok", f"Scraper {alvo.upper()} disparado")

        else:
            _registrar_fim(run_id, "erro", f"Tipo desconhecido: {ag.get('tipo')}")
    except Exception as e:
        log.exception("Falha ao executar job %s", ag_id)
        _registrar_fim(run_id, "erro", f"{type(e).__name__}: {e}")


def _trigger_para(ag: dict):
    try:
        hh_str, mm_str = (ag.get("hora") or "03:00").split(":")
        hh, mm = int(hh_str), int(mm_str)
    except Exception:
        hh, mm = 3, 0

    freq = ag.get("frequencia") or "diario"

    if freq == "uma_vez":
        if not ag.get("data"):
            return None
        try:
            dt = datetime.fromisoformat(f"{ag['data']}T{hh:02d}:{mm:02d}:00")
        except Exception:
            return None
        return DateTrigger(run_date=dt, timezone=TZ)

    if freq == "diario":
        return CronTrigger(minute=mm, hour=hh, timezone=TZ)

    if freq == "semanal":
        ds = ag.get("dia_semana")
        if ds is None:
            return None
        return CronTrigger(minute=mm, hour=hh, day_of_week=int(ds), timezone=TZ)

    if freq == "mensal":
        dm = ag.get("dia_mes")
        if dm is None:
            return None
        return CronTrigger(minute=mm, hour=hh, day=int(dm), timezone=TZ)

    return None


def _registrar_no_scheduler(ag: dict) -> None:
    assert _scheduler is not None
    if not ag["ativo"]:
        return
    trigger = _trigger_para(ag)
    if trigger is None:
        return
    try:
        _scheduler.add_job(
            _executar_job,
            trigger=trigger,
            args=[ag["id"]],
            id=f"ag-{ag['id']}",
            name=ag.get("nome") or f"agendamento {ag['id']}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
    except Exception as e:
        log.exception("Falha ao registrar agendamento %s no scheduler", ag.get("id"))
        _registrar_evento_externo(
            ag.get("id"), ag.get("nome"), ag.get("tipo"),
            "erro", f"Falha ao registrar no scheduler: {e}",
        )


def recarregar() -> None:
    """Limpa jobs e recarrega do banco."""
    if _scheduler is None:
        return
    for j in list(_scheduler.get_jobs()):
        if j.id.startswith("ag-"):
            _scheduler.remove_job(j.id)
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM agendamentos WHERE ativo = 1").fetchall()
    for r in rows:
        _registrar_no_scheduler(dict(r))


def _job_bounce_checker() -> None:
    """Roda a cada 30 min: verifica DSNs em todos os perfis com imap_ativo=1."""
    from . import bounce_checker
    try:
        bounce_checker.verificar_todos()
    except Exception:
        log.exception("Falha no bounce checker")


def _registrar_jobs_internos() -> None:
    """Jobs fixos do sistema, não vinculados à tabela agendamentos."""
    assert _scheduler is not None
    try:
        _scheduler.add_job(
            _job_bounce_checker,
            trigger=IntervalTrigger(minutes=30, timezone=TZ),
            id="sys-bounce-checker",
            name="Verificação de bounces (IMAP)",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            next_run_time=datetime.now(),
        )
    except Exception:
        log.exception("Falha ao registrar bounce-checker")


# ─── Listeners para erros e jobs perdidos ──────────────────────────────

def _job_id_para_ag(job_id: str) -> int | None:
    if not job_id or not job_id.startswith("ag-"):
        return None
    try:
        return int(job_id.split("-", 1)[1])
    except Exception:
        return None


def _carregar_ag_basico(ag_id: int) -> tuple[str | None, str | None]:
    if not ag_id:
        return None, None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT nome, tipo FROM agendamentos WHERE id = ?", (ag_id,)
        ).fetchone()
    if not row:
        return None, None
    return row["nome"], row["tipo"]


def _on_job_error(event) -> None:
    ag_id = _job_id_para_ag(getattr(event, "job_id", "") or "")
    if ag_id is None:
        return
    nome, tipo = _carregar_ag_basico(ag_id)
    msg = f"Erro no APScheduler: {getattr(event, 'exception', '')}"
    log.error("Job %s falhou: %s", event.job_id, msg)
    _registrar_evento_externo(ag_id, nome, tipo, "erro", msg)


def _on_job_missed(event) -> None:
    ag_id = _job_id_para_ag(getattr(event, "job_id", "") or "")
    if ag_id is None:
        return
    nome, tipo = _carregar_ag_basico(ag_id)
    quando = getattr(event, "scheduled_run_time", None)
    msg = f"Execução perdida (scheduled={quando})"
    log.warning("Job %s missed: %s", event.job_id, msg)
    _registrar_evento_externo(ag_id, nome, tipo, "missed", msg)


def iniciar() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone=TZ)
    _scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
    _scheduler.add_listener(_on_job_missed, EVENT_JOB_MISSED)
    _scheduler.start()
    recarregar()
    _registrar_jobs_internos()
    log.info("Scheduler iniciado com %d jobs", len(_scheduler.get_jobs()))


def parar() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def proxima_execucao(ag_id: int) -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job(f"ag-{ag_id}")
    if not job or not job.next_run_time:
        return None
    return job.next_run_time.strftime("%d/%m/%Y %H:%M")


def status_scheduler() -> dict:
    """Snapshot do estado do scheduler para a página de log."""
    if _scheduler is None:
        return {"rodando": False, "jobs": []}
    jobs = []
    for j in _scheduler.get_jobs():
        jobs.append({
            "id": j.id,
            "nome": j.name,
            "proxima": j.next_run_time.strftime("%d/%m/%Y %H:%M") if j.next_run_time else None,
        })
    return {"rodando": _scheduler.running, "jobs": jobs}
