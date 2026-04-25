"""
Agendador de scrapers via APScheduler. Os jobs são (re)carregados do banco no startup
e quando agendamentos são criados/alterados/excluídos.
"""
from __future__ import annotations

from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import get_conn
from .scrapers import runner as scraper_runner


_scheduler: Optional[BackgroundScheduler] = None


def _executar_job(tipo: str, alvo: str) -> None:
    if tipo == "scraper":
        try:
            scraper_runner.disparar(alvo)
        except Exception:
            pass


def _registrar_no_scheduler(ag: dict) -> None:
    assert _scheduler is not None
    if not ag["ativo"]:
        return
    try:
        partes = ag["cron"].split()
        if len(partes) != 5:
            return
        minute, hour, day, month, day_of_week = partes
        trigger = CronTrigger(
            minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week,
        )
        _scheduler.add_job(
            _executar_job,
            trigger=trigger,
            args=[ag["tipo"], ag["alvo"]],
            id=f"ag-{ag['id']}",
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
    _scheduler = BackgroundScheduler(timezone="UTC")
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
    return job.next_run_time.strftime("%Y-%m-%d %H:%M:%S %Z")
