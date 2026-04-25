"""
Runner de scrapers: executa o .py original como subprocess num diretório temporário,
captura stdout/stderr no log da execução, e importa o XLSX gerado pro banco.

A execução roda em uma thread daemon para não bloquear a request HTTP.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import openpyxl

from ..config import settings
from ..db import get_conn
from . import configs as scraper_configs
from .registry import SCRAPERS, ScraperInfo, get


def _scripts_dir() -> Path:
    return Path(__file__).parent / "external_scripts"


# Estado global de execução para permitir cancelamento
_processos_ativos: dict[int, subprocess.Popen] = {}
_cancelar_sequencia: bool = False
_lock = threading.Lock()


def _tmp_dir(run_id: int) -> Path:
    p = Path(settings.data_dir) / "scraping_tmp" / f"run_{run_id}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _criar_run(sigla: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO scraper_runs (tribunal, status, log) VALUES (?, 'rodando', '')",
            (sigla,),
        )
        return cur.lastrowid


def _finalizar_run(run_id: int, status: str, log: str, novos: int, atualizados: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE scraper_runs SET status = ?, finalizado_em = CURRENT_TIMESTAMP, "
            "log = ?, contatos_novos = ?, contatos_atualizados = ? WHERE id = ?",
            (status, log[-200000:], novos, atualizados, run_id),
        )


def _append_log(run_id: int, texto: str) -> None:
    with get_conn() as conn:
        atual = conn.execute("SELECT log FROM scraper_runs WHERE id = ?", (run_id,)).fetchone()
        novo = ((atual["log"] or "") + texto)[-200000:]
        conn.execute("UPDATE scraper_runs SET log = ? WHERE id = ?", (novo, run_id))


def _detectar_colunas(headers: list[str]) -> dict[str, int]:
    """Mapeia cabeçalhos do XLSX → índice da coluna. Aceita variações de capitalização."""
    norm = {(h or "").strip().lower(): i for i, h in enumerate(headers)}
    out: dict[str, int] = {}
    for chave, possiveis in {
        "cidade":  ["cidade", "comarca", "municipio", "município"],
        "comarca": ["comarca"],
        "orgao":   ["órgão", "orgao", "vara", "unidade"],
        "email":   ["email", "e-mail", "endereço de email", "endereco de email"],
    }.items():
        for p in possiveis:
            if p in norm:
                out[chave] = norm[p]
                break
    return out


def _importar_xlsx(xlsx_path: Path, info: ScraperInfo) -> tuple[int, int]:
    """Lê o XLSX gerado pelo scraper e faz upsert na tabela contatos."""
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.active

    rows = ws.iter_rows(values_only=True)
    headers = [str(c) if c is not None else "" for c in next(rows, [])]
    cols = _detectar_colunas(headers)

    if "email" not in cols:
        wb.close()
        raise ValueError(f"Coluna de email não encontrada no XLSX. Cabeçalhos: {headers}")

    novos = 0
    atualizados = 0

    with get_conn() as conn:
        for row in rows:
            email = (row[cols["email"]] or "").strip() if cols.get("email") is not None else ""
            if not email or "@" not in email:
                continue

            cidade  = (row[cols["cidade"]]  or "").strip() if "cidade"  in cols else None
            comarca = (row[cols["comarca"]] or "").strip() if "comarca" in cols else cidade
            orgao   = (row[cols["orgao"]]   or "").strip() if "orgao"   in cols else None

            existente = conn.execute(
                "SELECT id FROM contatos WHERE email = ? AND tribunal = ?",
                (email, info.sigla),
            ).fetchone()

            if existente:
                conn.execute(
                    "UPDATE contatos SET cidade = ?, comarca = ?, orgao = ?, "
                    "estado = ?, sistema = ?, scraping_em = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (cidade, comarca, orgao, info.estado, info.sistema, existente["id"]),
                )
                atualizados += 1
            else:
                conn.execute(
                    "INSERT INTO contatos (email, cidade, comarca, orgao, estado, "
                    "tribunal, sistema) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (email, cidade, comarca, orgao, info.estado, info.sigla, info.sistema),
                )
                novos += 1
    wb.close()
    return novos, atualizados


def _executar(run_id: int, info: ScraperInfo) -> None:
    """Roda o .py original como subprocess. Streama log pro banco. Importa XLSX no final."""
    workdir = _tmp_dir(run_id)
    script_origem = _scripts_dir() / info.script
    script_local = workdir / info.script
    shutil.copy2(script_origem, script_local)

    # grava a config editada pela UI no cwd; o scraper lê dali
    cfg = scraper_configs.get_runtime_config(info.sigla)
    with open(workdir / "scraper_config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

    cmd = [sys.executable, "-u", info.script]
    env = {"PYTHONIOENCODING": "utf-8"}
    import os as _os
    full_env = {**_os.environ, **env}

    _append_log(run_id, f"$ {' '.join(cmd)}\n(cwd: {workdir})\n\n")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=full_env,
        )
        with _lock:
            _processos_ativos[run_id] = proc
        assert proc.stdout is not None
        buffer: list[str] = []
        for linha in proc.stdout:
            buffer.append(linha)
            if len(buffer) >= 5:
                _append_log(run_id, "".join(buffer))
                buffer.clear()
        if buffer:
            _append_log(run_id, "".join(buffer))

        codigo = proc.wait()
        with _lock:
            _processos_ativos.pop(run_id, None)
        if codigo != 0:
            mensagem = "[cancelado pelo usuário]" if codigo < 0 else f"[exit code: {codigo}]"
            _finalizar_run(run_id, "erro", _ler_log(run_id) + "\n" + mensagem, 0, 0)
            return

        xlsx_path = workdir / info.xlsx
        if not xlsx_path.exists():
            _finalizar_run(run_id, "erro", _ler_log(run_id) + f"\nXLSX não gerado: {info.xlsx}", 0, 0)
            return

        _append_log(run_id, f"\nImportando {info.xlsx} pro banco...\n")
        novos, atualizados = _importar_xlsx(xlsx_path, info)
        _finalizar_run(
            run_id, "ok",
            _ler_log(run_id) + f"\nImportação concluída: {novos} novos, {atualizados} atualizados.\n",
            novos, atualizados,
        )
    except Exception as e:
        _finalizar_run(run_id, "erro", _ler_log(run_id) + f"\nErro: {e!r}\n", 0, 0)
    finally:
        with _lock:
            _processos_ativos.pop(run_id, None)
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass


def _ler_log(run_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT log FROM scraper_runs WHERE id = ?", (run_id,)).fetchone()
    return row["log"] if row else ""


def disparar(sigla: str) -> int:
    """Dispara um scraper em background e retorna o run_id pra acompanhar."""
    info = get(sigla)
    if info is None:
        raise ValueError(f"Scraper desconhecido: {sigla}")
    if info.manual:
        raise ValueError(f"Scraper {sigla} requer interação manual e não pode ser executado pela web.")

    run_id = _criar_run(sigla)
    t = threading.Thread(target=_executar, args=(run_id, info), daemon=True, name=f"scraper-{sigla}")
    t.start()
    return run_id


def _executar_todos_sequencial() -> None:
    """Roda cada scraper não-manual, um após o outro, na mesma thread."""
    global _cancelar_sequencia
    for s in SCRAPERS.values():
        if _cancelar_sequencia:
            break
        if s.manual:
            continue
        try:
            run_id = _criar_run(s.sigla)
            _executar(run_id, s)
        except Exception:
            # erros já são gravados em _executar; segue pro próximo
            pass


def disparar_todos() -> None:
    """Dispara todos os scrapers (exceto manuais) em sequência, em background."""
    global _cancelar_sequencia
    _cancelar_sequencia = False
    t = threading.Thread(
        target=_executar_todos_sequencial, daemon=True, name="scraper-todos"
    )
    t.start()


def parar_todos() -> int:
    """
    Para qualquer execução em andamento. Retorna quantos processos foram
    interrompidos.
    """
    global _cancelar_sequencia
    _cancelar_sequencia = True

    with _lock:
        ativos = list(_processos_ativos.items())

    parados = 0
    for run_id, proc in ativos:
        try:
            proc.terminate()  # SIGTERM
            parados += 1
        except Exception:
            pass

    # dá um instante e força quem não morreu
    if ativos:
        import time as _t
        _t.sleep(2)
        with _lock:
            ainda_ativos = list(_processos_ativos.items())
        for _, proc in ainda_ativos:
            try:
                proc.kill()  # SIGKILL
            except Exception:
                pass

    return parados


def importar_xlsx_manual(sigla: str, xlsx_path: Path) -> tuple[int, int, int]:
    """
    Importa um XLSX gerado fora da web (rodando o scraper localmente).
    Cria uma entrada em scraper_runs com status='ok' e log explicativo.
    Retorna (run_id, novos, atualizados).
    """
    info = get(sigla)
    if info is None:
        raise ValueError(f"Scraper desconhecido: {sigla}")

    run_id = _criar_run(sigla)
    _append_log(run_id, f"Importação manual de XLSX ({xlsx_path.name})\n")
    try:
        novos, atualizados = _importar_xlsx(xlsx_path, info)
        _finalizar_run(
            run_id, "ok",
            _ler_log(run_id) + f"\nImportação concluída: {novos} novos, {atualizados} atualizados.\n",
            novos, atualizados,
        )
        return run_id, novos, atualizados
    except Exception as e:
        _finalizar_run(run_id, "erro", _ler_log(run_id) + f"\nErro: {e!r}\n", 0, 0)
        raise


def get_run(run_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, tribunal, iniciado_em, finalizado_em, status, "
            "contatos_novos, contatos_atualizados, log FROM scraper_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    return dict(row) if row else None


def ultima_run_por_tribunal() -> dict[str, dict]:
    out: dict[str, dict] = {}
    with get_conn() as conn:
        for row in conn.execute(
            "SELECT tribunal, MAX(id) as id FROM scraper_runs GROUP BY tribunal"
        ):
            r = conn.execute(
                "SELECT id, tribunal, iniciado_em, finalizado_em, status, "
                "contatos_novos, contatos_atualizados FROM scraper_runs WHERE id = ?",
                (row["id"],),
            ).fetchone()
            if r:
                out[r["tribunal"]] = dict(r)
    return out
