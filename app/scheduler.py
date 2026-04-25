"""
Agendador de scrapers e campanhas via APScheduler.

Cada agendamento tem:
  - nome:         rótulo amigável
  - tipo:         'scraper' | 'campanha'
  - alvo:         (scraper) sigla do TJ
  - perfil_id, filtro_estado, filtro_tribunal, quantidade  (campanha)
  - frequencia:   uma_vez | diario | semanal | mensal
  - hora:         "HH:MM"
  - data:         "YYYY-MM-DD"  (frequencia = uma_vez)
  - dia_semana:   0-6 (0=segunda)  (frequencia = semanal)
  - dia_mes:      1-28             (frequencia = mensal)

O fuso usado é America/Sao_Paulo, então o usuário escolhe o horário local.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .db import get_conn


TZ = "America/Sao_Paulo"
_scheduler: Optional[BackgroundScheduler] = None


def _executar_job(ag_id: int) -> None:
    """Carrega o agendamento atual do banco e dispara a ação correspondente."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM agendamentos WHERE id = ?", (ag_id,)).fetchone()
    if not row:
        return
    ag = dict(row)

    if ag["tipo"] == "scraper":
        from .scrapers import runner as scraper_runner
        try:
            scraper_runner.disparar(ag["alvo"])
        except Exception:
            pass

    elif ag["tipo"] == "campanha":
        from . import mailer
        if not ag.get("perfil_id"):
            return
        try:
            mailer.disparar(
                ag["perfil_id"],
                int(ag.get("quantidade") or 50),
                {
                    "estado": ag.get("filtro_estado") or None,
                    "tribunal": ag.get("filtro_tribunal") or None,
                },
            )
        except Exception:
            pass


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
    except Exception:
        pass


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


def iniciar() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone=TZ)
    _scheduler.start()
    recarregar()


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
