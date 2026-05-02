"""
Aplicação FastAPI — ponto de entrada com todas as rotas.

Estrutura das rotas:
  /                          → redireciona conforme login
  /login, /logout            → autenticação
  /painel                    → dashboard
  /scrapers                  → lista TJs e dispara scraping
  /contatos                  → CRUD de contatos
  /perfis                    → CRUD de perfis de remetente
  /campanhas                 → disparo de campanha
  /historico                 → log de envios
  /agendamentos              → cron interno
  /healthz                   → health check
"""
from __future__ import annotations

import ipaddress
import re
import shutil
from datetime import datetime, time as _time, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .auth import (
    LoginRequired,
    autenticar,
    garantir_usuarios_iniciais,
    redirect_to_login,
    requer_login,
    usuario_atual,
)
from .config import settings
from .crypto import encrypt
from .db import get_conn, init_db
from . import bounce_checker, campanhas, mailer, scheduler
from .scrapers import configs as scraper_configs
from .scrapers import registry as scraper_registry
from .scrapers import runner as scraper_runner


BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


_TZ_SP = ZoneInfo("America/Sao_Paulo")


def _filtro_local_dt(valor, fmt: str = "%d/%m/%Y %H:%M") -> str:
    """Converte timestamp UTC (string SQLite ou datetime) para America/Sao_Paulo."""
    if not valor:
        return "—"
    if isinstance(valor, str):
        try:
            dt = datetime.fromisoformat(valor.replace("Z", "+00:00"))
        except ValueError:
            return valor
    elif isinstance(valor, datetime):
        dt = valor
    else:
        return str(valor)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_TZ_SP).strftime(fmt)


templates.env.filters["local_dt"] = _filtro_local_dt

app = FastAPI(title="Peritos — Painel")

app.add_middleware(SessionMiddleware, secret_key=settings.resolve_session_secret(), https_only=False)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ─── Modelos padrão de e-mail (apenas como pré-preenchimento ao criar perfil) ───

MODELO_TEXTO_PADRAO = """\
Excelentíssimo(a) Senhor(a) Juiz(a),

Meu nome é $remetente e me coloco à disposição deste Juízo para atuar como Perito.

OBS.: Já possuo cadastro de Auxiliar da Justiça validado junto ao $sistema.

Atenciosamente,
$remetente
$email_remetente
"""

MODELO_HTML_PADRAO = """\
<p>Excelentíssimo(a) Senhor(a) Juiz(a),</p>
<p>Meu nome é <strong>$remetente</strong> e me coloco à disposição deste Juízo para atuar como Perito.</p>
<p><strong>OBS.:</strong> Já possuo cadastro de Auxiliar da Justiça validado junto ao $sistema.</p>
<p>Atenciosamente,<br><strong>$remetente</strong><br>$email_remetente</p>
"""


# ─── Lifecycle ─────────────────────────────────────────────────────────

@app.on_event("startup")
def _startup() -> None:
    init_db()
    garantir_usuarios_iniciais()
    scheduler.iniciar()
    campanhas.reidratar()


@app.on_event("shutdown")
def _shutdown() -> None:
    scheduler.parar()


@app.exception_handler(LoginRequired)
async def _login_required_handler(_: Request, __: LoginRequired):
    return redirect_to_login()


# ─── Helpers ───────────────────────────────────────────────────────────

def _ufs_e_tribunais() -> tuple[list[str], list[str]]:
    with get_conn() as conn:
        ufs = [r["estado"] for r in conn.execute(
            "SELECT DISTINCT estado FROM contatos WHERE estado IS NOT NULL "
            "AND tribunal != '_teste' ORDER BY estado"
        )]
        tribunais = [r["tribunal"] for r in conn.execute(
            "SELECT DISTINCT tribunal FROM contatos WHERE tribunal IS NOT NULL "
            "AND tribunal != '_teste' ORDER BY tribunal"
        )]
    return ufs, tribunais


def _perfis_para_form(user_id: int) -> list[dict]:
    """Lista perfis do usuário e marca quais estão em uso por outra campanha."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT p.id, p.nome, p.email_remetente, p.limite_diario, "
            "       (SELECT nome FROM campanhas "
            "        WHERE perfil_id = p.id AND status IN ('ativa','pausada') "
            "        LIMIT 1) AS uso_por "
            "FROM perfis_remetente p "
            "WHERE p.usuario_id = ? ORDER BY p.nome",
            (user_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["disabled"] = d["uso_por"] is not None
        out.append(d)
    return out


def _parse_form_dias(valores: list[str]) -> set[int]:
    out: set[int] = set()
    for v in valores:
        try:
            i = int(v)
            if 0 <= i <= 6:
                out.add(i)
        except ValueError:
            pass
    return out


def _parse_form_hora(s: str) -> _time:
    h, m = s.split(":")
    return _time(int(h), int(m))


def _mapping_uf_tribunal() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Retorna (tribunais_por_uf, ufs_por_tribunal) para cascade nos forms."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT estado, tribunal FROM contatos "
            "WHERE estado IS NOT NULL AND tribunal IS NOT NULL "
            "AND tribunal != '_teste' "
            "ORDER BY estado, tribunal"
        ).fetchall()
    tribunais_por_uf: dict[str, list[str]] = {}
    ufs_por_tribunal: dict[str, list[str]] = {}
    for r in rows:
        uf, tj = r["estado"], r["tribunal"]
        tribunais_por_uf.setdefault(uf, []).append(tj)
        ufs_por_tribunal.setdefault(tj, []).append(uf)
    return tribunais_por_uf, ufs_por_tribunal


def _curriculos_dir() -> Path:
    p = Path(settings.data_dir) / "curriculos"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ─── Login ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if usuario_atual(request):
        return RedirectResponse(url="/painel", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, erro: str | None = None):
    if usuario_atual(request):
        return RedirectResponse(url="/painel", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "erro": erro})


@app.post("/login")
def login_submit(request: Request, email: str = Form(...), senha: str = Form(...)):
    user = autenticar(email, senha)
    if not user:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "erro": "Email ou senha inválidos."},
            status_code=401,
        )
    request.session["user_id"] = user["id"]
    return RedirectResponse(url="/painel", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ─── Painel ────────────────────────────────────────────────────────────

@app.get("/painel", response_class=HTMLResponse)
def painel(request: Request, user: dict = Depends(requer_login)):
    with get_conn() as conn:
        total_contatos = conn.execute(
            "SELECT COUNT(*) c FROM contatos WHERE tribunal != '_teste'"
        ).fetchone()["c"]
        total_envios = conn.execute(
            "SELECT COUNT(*) c FROM envios e "
            "JOIN contatos c ON c.id = e.contato_id "
            "WHERE e.status = 'ok' AND c.tribunal != '_teste'"
        ).fetchone()["c"]
        envios_hoje = conn.execute(
            "SELECT COUNT(*) c FROM envios e "
            "JOIN contatos c ON c.id = e.contato_id "
            "WHERE e.status = 'ok' AND c.tribunal != '_teste' "
            "AND date(e.enviado_em) = date('now', 'localtime')"
        ).fetchone()["c"]
        ultima = conn.execute(
            "SELECT tribunal, finalizado_em, status FROM scraper_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    return templates.TemplateResponse("painel.html", {
        "request": request, "user": user,
        "total_contatos": total_contatos, "total_envios": total_envios,
        "envios_hoje": envios_hoje,
        "ultima_execucao": dict(ultima) if ultima else None,
    })


# ─── Scrapers ──────────────────────────────────────────────────────────

@app.get("/scrapers", response_class=HTMLResponse)
def scrapers_lista(request: Request, user: dict = Depends(requer_login)):
    scrapers = scraper_registry.listar()
    config_disponivel = {s.sigla: scraper_configs.schema(s.sigla).disponivel for s in scrapers}
    ultimas = scraper_runner.ultima_run_por_tribunal()
    rodando = [u for u in ultimas.values() if u["status"] == "rodando"]
    return templates.TemplateResponse("scrapers.html", {
        "request": request, "user": user,
        "scrapers": scrapers,
        "ultimas": ultimas,
        "config_disponivel": config_disponivel,
        "rodando": rodando,
    })


@app.get("/scrapers/{sigla}/config", response_class=HTMLResponse)
def scraper_config_form(sigla: str, request: Request,
                        user: dict = Depends(requer_login),
                        salvo: int = 0):
    info = scraper_registry.get(sigla)
    if not info:
        raise HTTPException(404)
    schema = scraper_configs.schema(sigla)
    palavras_atuais = scraper_configs.palavras_chave(sigla)
    defaults = scraper_configs.DEFAULTS.get(sigla.lower(), [])
    usando_personalizado = palavras_atuais != defaults
    return templates.TemplateResponse("scraper_config.html", {
        "request": request, "user": user, "info": info, "schema": schema,
        "palavras_atuais": palavras_atuais, "defaults": defaults,
        "usando_personalizado": usando_personalizado, "salvo": bool(salvo),
    })


@app.post("/scrapers/{sigla}/config")
def scraper_config_submit(sigla: str, user: dict = Depends(requer_login),
                          palavras_chave: str = Form("")):
    if not scraper_registry.get(sigla):
        raise HTTPException(404)
    if not scraper_configs.schema(sigla).disponivel:
        raise HTTPException(400, "Este scraper não aceita configuração pela UI.")
    palavras = [linha.strip() for linha in palavras_chave.splitlines() if linha.strip()]
    if not palavras:
        scraper_configs.resetar(sigla)
    else:
        scraper_configs.salvar_palavras_chave(sigla, palavras)
    return RedirectResponse(url=f"/scrapers/{sigla}/config?salvo=1", status_code=303)


@app.post("/scrapers/{sigla}/config/reset")
def scraper_config_reset(sigla: str, user: dict = Depends(requer_login)):
    if not scraper_registry.get(sigla):
        raise HTTPException(404)
    scraper_configs.resetar(sigla)
    return RedirectResponse(url=f"/scrapers/{sigla}/config?salvo=1", status_code=303)


@app.post("/scrapers/{sigla}/disparar")
def scrapers_disparar(sigla: str, user: dict = Depends(requer_login)):
    try:
        run_id = scraper_runner.disparar(sigla)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url=f"/scrapers/run/{run_id}", status_code=303)


@app.post("/scrapers/disparar-todos")
def scrapers_disparar_todos(user: dict = Depends(requer_login)):
    scraper_runner.disparar_todos()
    return RedirectResponse(url="/scrapers", status_code=303)


@app.post("/scrapers/parar-todos")
def scrapers_parar_todos(user: dict = Depends(requer_login)):
    scraper_runner.parar_todos()
    return RedirectResponse(url="/scrapers", status_code=303)


@app.get("/scrapers/{sigla}/importar", response_class=HTMLResponse)
def scrapers_importar_form(sigla: str, request: Request,
                           user: dict = Depends(requer_login),
                           erro: str | None = None):
    info = scraper_registry.get(sigla)
    if not info:
        raise HTTPException(404)
    return templates.TemplateResponse("scraper_importar.html", {
        "request": request, "user": user, "info": info, "erro": erro,
    })


@app.post("/scrapers/{sigla}/importar")
async def scrapers_importar_submit(sigla: str, request: Request,
                                    user: dict = Depends(requer_login),
                                    arquivo: UploadFile = File(...)):
    info = scraper_registry.get(sigla)
    if not info:
        raise HTTPException(404)

    if not arquivo.filename or not arquivo.filename.lower().endswith(".xlsx"):
        return templates.TemplateResponse("scraper_importar.html", {
            "request": request, "user": user, "info": info,
            "erro": "Envie um arquivo .xlsx (Excel).",
        })

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        shutil.copyfileobj(arquivo.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        run_id, _, _ = scraper_runner.importar_xlsx_manual(sigla, tmp_path)
    except Exception as e:
        try: tmp_path.unlink()
        except Exception: pass
        return templates.TemplateResponse("scraper_importar.html", {
            "request": request, "user": user, "info": info,
            "erro": f"Falha ao importar: {e}",
        })
    finally:
        try: tmp_path.unlink()
        except Exception: pass

    return RedirectResponse(url=f"/scrapers/run/{run_id}", status_code=303)


@app.get("/scrapers/run/{run_id}", response_class=HTMLResponse)
def scrapers_run(run_id: int, request: Request, user: dict = Depends(requer_login)):
    run = scraper_runner.get_run(run_id)
    if not run:
        raise HTTPException(404, "Execução não encontrada")
    return templates.TemplateResponse("scraper_run.html", {
        "request": request, "user": user, "run": run,
    })


@app.get("/scrapers/run/{run_id}/log", response_class=HTMLResponse)
def scrapers_run_log(run_id: int, request: Request, user: dict = Depends(requer_login)):
    run = scraper_runner.get_run(run_id)
    if not run:
        raise HTTPException(404)
    # Retorna o partial inteiro (cards + log). Quando run.status != 'rodando',
    # o partial sai sem hx-trigger e o htmx para de pollar sozinho.
    return templates.TemplateResponse("_scraper_run_corpo.html", {
        "request": request, "user": user, "run": run,
    })


# ─── Contatos ──────────────────────────────────────────────────────────

def _where_contatos(q: str, estado: str, tribunal: str, invalido: str) -> tuple[str, list]:
    where = ["1=1"]
    args: list = []
    if q:
        where.append("(email LIKE ? OR cidade LIKE ? OR comarca LIKE ? OR orgao LIKE ?)")
        like = f"%{q}%"
        args.extend([like, like, like, like])
    if estado:
        where.append("estado = ?"); args.append(estado)
    if tribunal:
        where.append("tribunal = ?"); args.append(tribunal)
    if invalido in ("0", "1"):
        where.append("invalido = ?"); args.append(int(invalido))
    return " AND ".join(where), args


@app.get("/contatos", response_class=HTMLResponse)
def contatos_lista(
    request: Request,
    user: dict = Depends(requer_login),
    q: str = "", estado: str = "", tribunal: str = "", invalido: str = "",
    pagina: int = 1,
):
    por_pagina = 50
    pagina = max(1, pagina)

    sql_where, args = _where_contatos(q, estado, tribunal, invalido)

    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM contatos WHERE {sql_where}", args).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM contatos WHERE {sql_where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*args, por_pagina, (pagina - 1) * por_pagina],
        ).fetchall()
    contatos = [dict(r) for r in rows]
    ufs, tribunais = _ufs_e_tribunais()

    filtros = {"q": q, "estado": estado, "tribunal": tribunal, "invalido": invalido}

    def qs_pag(p: int) -> str:
        return urlencode({**filtros, "pagina": p})

    return templates.TemplateResponse("contatos.html", {
        "request": request, "user": user,
        "contatos": contatos, "total": total,
        "ufs": ufs, "tribunais": tribunais,
        "filtros": filtros, "pagina": pagina, "por_pagina": por_pagina,
        "paginas_total": max(1, (total + por_pagina - 1) // por_pagina),
        "qs_pag": qs_pag,
    })


@app.post("/contatos/{cid}/toggle")
def contatos_toggle(cid: int, user: dict = Depends(requer_login)):
    with get_conn() as conn:
        conn.execute("UPDATE contatos SET invalido = 1 - invalido WHERE id = ?", (cid,))
    return RedirectResponse(url="/contatos", status_code=303)


@app.post("/contatos/{cid}/excluir")
def contatos_excluir(cid: int, user: dict = Depends(requer_login)):
    with get_conn() as conn:
        conn.execute("DELETE FROM contatos WHERE id = ?", (cid,))
    return RedirectResponse(url="/contatos", status_code=303)


# ─── Ações em lote ─────────────────────────────────────────────────────

from fastapi import Body  # noqa: E402

def _aplicar_lote(ids: list[str], sql_update_or_delete: str) -> int:
    ids_int = [int(i) for i in ids if str(i).isdigit()]
    if not ids_int:
        return 0
    placeholders = ",".join("?" * len(ids_int))
    with get_conn() as conn:
        cur = conn.execute(sql_update_or_delete.format(ph=placeholders), ids_int)
        return cur.rowcount or 0


def _aplicar_lote_filtro(filtros: dict, sql_update_or_delete: str) -> int:
    sql_where, args = _where_contatos(
        filtros.get("q", ""), filtros.get("estado", ""),
        filtros.get("tribunal", ""), filtros.get("invalido", ""),
    )
    with get_conn() as conn:
        cur = conn.execute(sql_update_or_delete.format(where=sql_where), args)
        return cur.rowcount or 0


def _redirect_back(filtros: dict) -> RedirectResponse:
    qs = urlencode({k: v for k, v in filtros.items() if v})
    url = f"/contatos?{qs}" if qs else "/contatos"
    return RedirectResponse(url=url, status_code=303)


@app.post("/contatos/lote/excluir")
async def contatos_lote_excluir(request: Request, user: dict = Depends(requer_login)):
    form = await request.form()
    ids = form.getlist("ids")
    _aplicar_lote(ids, "DELETE FROM contatos WHERE id IN ({ph})")
    return _redirect_back(dict(form))


@app.post("/contatos/lote/invalidar")
async def contatos_lote_invalidar(request: Request, user: dict = Depends(requer_login)):
    form = await request.form()
    ids = form.getlist("ids")
    _aplicar_lote(ids, "UPDATE contatos SET invalido = 1 WHERE id IN ({ph})")
    return _redirect_back(dict(form))


@app.post("/contatos/lote/validar")
async def contatos_lote_validar(request: Request, user: dict = Depends(requer_login)):
    form = await request.form()
    ids = form.getlist("ids")
    _aplicar_lote(ids, "UPDATE contatos SET invalido = 0 WHERE id IN ({ph})")
    return _redirect_back(dict(form))


@app.post("/contatos/lote-filtro/excluir")
async def contatos_lote_filtro_excluir(request: Request, user: dict = Depends(requer_login)):
    form = await request.form()
    _aplicar_lote_filtro(dict(form), "DELETE FROM contatos WHERE {where}")
    return _redirect_back(dict(form))


@app.post("/contatos/lote-filtro/invalidar")
async def contatos_lote_filtro_invalidar(request: Request, user: dict = Depends(requer_login)):
    form = await request.form()
    _aplicar_lote_filtro(dict(form), "UPDATE contatos SET invalido = 1 WHERE {where}")
    return _redirect_back(dict(form))


@app.post("/contatos/lote-filtro/validar")
async def contatos_lote_filtro_validar(request: Request, user: dict = Depends(requer_login)):
    form = await request.form()
    _aplicar_lote_filtro(dict(form), "UPDATE contatos SET invalido = 0 WHERE {where}")
    return _redirect_back(dict(form))


# ─── Perfis de remetente ───────────────────────────────────────────────

@app.get("/perfis", response_class=HTMLResponse)
def perfis_lista(request: Request, user: dict = Depends(requer_login)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM perfis_remetente WHERE usuario_id = ? ORDER BY id DESC",
            (user["id"],),
        ).fetchall()
    perfis = []
    for r in rows:
        d = dict(r)
        d["ultima_bounce_run"] = bounce_checker.ultima_run(d["id"])
        perfis.append(d)
    return templates.TemplateResponse("perfis.html", {
        "request": request, "user": user, "perfis": perfis,
    })


@app.get("/perfis/novo", response_class=HTMLResponse)
def perfil_novo_form(request: Request, user: dict = Depends(requer_login), erro: str | None = None):
    return templates.TemplateResponse("perfil_form.html", {
        "request": request, "user": user, "perfil": None, "erro": erro,
        "modelo_texto_padrao": MODELO_TEXTO_PADRAO,
        "modelo_html_padrao": MODELO_HTML_PADRAO,
    })


@app.post("/perfis/novo")
async def perfil_novo_submit(
    request: Request,
    user: dict = Depends(requer_login),
    nome: str = Form(...),
    email_remetente: str = Form(...),
    smtp_host: str = Form(...),
    smtp_port: int = Form(...),
    smtp_senha: str = Form(...),
    assunto: str = Form(...),
    corpo_texto: str = Form(...),
    corpo_html: str = Form(...),
    assinatura: str = Form(""),
    limite_diario: int = Form(200),
    imap_host: str = Form(""),
    imap_port: int = Form(993),
    imap_ativo: int = Form(0),
    curriculo: UploadFile | None = File(None),
):
    curriculo_path = ""
    if curriculo and curriculo.filename:
        destino = _curriculos_dir() / f"u{user['id']}_{curriculo.filename}"
        with open(destino, "wb") as out:
            shutil.copyfileobj(curriculo.file, out)
        curriculo_path = str(destino)

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO perfis_remetente (usuario_id, nome, email_remetente, smtp_host, "
            "smtp_port, smtp_senha_enc, assunto, corpo_texto, corpo_html, assinatura, "
            "curriculo_path, limite_diario, imap_host, imap_port, imap_ativo) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], nome, email_remetente, smtp_host, smtp_port,
             encrypt(smtp_senha), assunto, corpo_texto, corpo_html, assinatura,
             curriculo_path or None, limite_diario,
             imap_host.strip() or None, imap_port, 1 if imap_ativo else 0),
        )
    return RedirectResponse(url="/perfis", status_code=303)


@app.get("/perfis/{pid}/editar", response_class=HTMLResponse)
def perfil_editar_form(pid: int, request: Request, user: dict = Depends(requer_login), erro: str | None = None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM perfis_remetente WHERE id = ? AND usuario_id = ?",
            (pid, user["id"]),
        ).fetchone()
    if not row:
        raise HTTPException(404)
    return templates.TemplateResponse("perfil_form.html", {
        "request": request, "user": user, "perfil": dict(row), "erro": erro,
        "modelo_texto_padrao": MODELO_TEXTO_PADRAO,
        "modelo_html_padrao": MODELO_HTML_PADRAO,
    })


@app.post("/perfis/{pid}/editar")
async def perfil_editar_submit(
    pid: int,
    user: dict = Depends(requer_login),
    nome: str = Form(...),
    email_remetente: str = Form(...),
    smtp_host: str = Form(...),
    smtp_port: int = Form(...),
    smtp_senha: str = Form(""),
    assunto: str = Form(...),
    corpo_texto: str = Form(...),
    corpo_html: str = Form(...),
    assinatura: str = Form(""),
    limite_diario: int = Form(200),
    imap_host: str = Form(""),
    imap_port: int = Form(993),
    imap_ativo: int = Form(0),
    curriculo: UploadFile | None = File(None),
):
    with get_conn() as conn:
        atual = conn.execute(
            "SELECT * FROM perfis_remetente WHERE id = ? AND usuario_id = ?",
            (pid, user["id"]),
        ).fetchone()
        if not atual:
            raise HTTPException(404)

        senha_enc = encrypt(smtp_senha) if smtp_senha else atual["smtp_senha_enc"]
        curriculo_path = atual["curriculo_path"]
        if curriculo and curriculo.filename:
            destino = _curriculos_dir() / f"u{user['id']}_{curriculo.filename}"
            with open(destino, "wb") as out:
                shutil.copyfileobj(curriculo.file, out)
            curriculo_path = str(destino)

        conn.execute(
            "UPDATE perfis_remetente SET nome = ?, email_remetente = ?, smtp_host = ?, "
            "smtp_port = ?, smtp_senha_enc = ?, assunto = ?, corpo_texto = ?, "
            "corpo_html = ?, assinatura = ?, curriculo_path = ?, limite_diario = ?, "
            "imap_host = ?, imap_port = ?, imap_ativo = ? "
            "WHERE id = ?",
            (nome, email_remetente, smtp_host, smtp_port, senha_enc, assunto,
             corpo_texto, corpo_html, assinatura, curriculo_path, limite_diario,
             imap_host.strip() or None, imap_port, 1 if imap_ativo else 0, pid),
        )
    return RedirectResponse(url="/perfis", status_code=303)


@app.post("/perfis/{pid}/excluir")
def perfil_excluir(pid: int, user: dict = Depends(requer_login)):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM perfis_remetente WHERE id = ? AND usuario_id = ?",
            (pid, user["id"]),
        )
    return RedirectResponse(url="/perfis", status_code=303)


@app.post("/perfis/{pid}/verificar-bounces")
def perfil_verificar_bounces(pid: int, user: dict = Depends(requer_login)):
    with get_conn() as conn:
        own = conn.execute(
            "SELECT 1 FROM perfis_remetente WHERE id = ? AND usuario_id = ?",
            (pid, user["id"]),
        ).fetchone()
    if not own:
        raise HTTPException(403)
    bounce_checker.verificar(pid)
    return RedirectResponse(url="/perfis", status_code=303)


# ─── Campanhas ─────────────────────────────────────────────────────────

@app.get("/campanhas", response_class=HTMLResponse)
def campanhas_lista(request: Request, user: dict = Depends(requer_login)):
    items = campanhas.listar()
    with get_conn() as conn:
        perfis_do_user = {r["id"] for r in conn.execute(
            "SELECT id FROM perfis_remetente WHERE usuario_id = ?", (user["id"],)
        ).fetchall()}
    items = [c for c in items if c["perfil_id"] in perfis_do_user]
    for c in items:
        if c["status"] in ("ativa", "pausada"):
            c["enviados_hoje"] = campanhas.enviados_hoje_campanha(c["id"])
    return templates.TemplateResponse("campanhas.html", {
        "request": request, "user": user, "campanhas": items,
    })


@app.get("/campanhas/contar")
def campanhas_contar(
    perfil_id: int,
    estado: str = "",
    tribunal: str = "",
    user: dict = Depends(requer_login),
):
    """Conta contatos elegíveis para os filtros + perfil. Usado pelo form para
    mostrar o limite dinâmico de total_alvo."""
    with get_conn() as conn:
        own = conn.execute(
            "SELECT 1 FROM perfis_remetente WHERE id = ? AND usuario_id = ?",
            (perfil_id, user["id"]),
        ).fetchone()
    if not own:
        raise HTTPException(403)
    n = mailer.contar_contatos_elegiveis(
        {"estado": estado or None, "tribunal": tribunal or None},
        perfil_id,
    )
    return {"disponiveis": n}


@app.get("/campanhas/nova", response_class=HTMLResponse)
def campanhas_nova_form(request: Request, user: dict = Depends(requer_login),
                        erro: str | None = None):
    ufs, tribunais = _ufs_e_tribunais()
    return templates.TemplateResponse("campanha_form.html", {
        "request": request, "user": user, "erro": erro,
        "perfis": _perfis_para_form(user["id"]),
        "ufs": ufs, "tribunais": tribunais,
        "campanha": None,
    })


@app.post("/campanhas/nova")
def campanhas_nova_submit(
    request: Request,
    user: dict = Depends(requer_login),
    nome: str = Form(...),
    perfil_id: int = Form(...),
    estado: str = Form(""),
    tribunal: str = Form(""),
    total_alvo: int = Form(...),
    por_dia: int = Form(...),
    dias_semana: list[str] = Form(default=[]),
    janela_inicio: str = Form(...),
    janela_fim: str = Form(...),
):
    with get_conn() as conn:
        own = conn.execute(
            "SELECT 1 FROM perfis_remetente WHERE id = ? AND usuario_id = ?",
            (perfil_id, user["id"]),
        ).fetchone()
    if not own:
        raise HTTPException(403)
    try:
        cid = campanhas.criar(
            nome=nome,
            perfil_id=perfil_id,
            filtros={"estado": estado, "tribunal": tribunal},
            total_alvo=total_alvo,
            por_dia=por_dia,
            dias_semana=_parse_form_dias(dias_semana),
            janela_inicio=_parse_form_hora(janela_inicio),
            janela_fim=_parse_form_hora(janela_fim),
        )
    except ValueError as e:
        ufs, tribunais = _ufs_e_tribunais()
        return templates.TemplateResponse("campanha_form.html", {
            "request": request, "user": user, "erro": str(e),
            "perfis": _perfis_para_form(user["id"]),
            "ufs": ufs, "tribunais": tribunais, "campanha": None,
        })
    return RedirectResponse(url=f"/campanhas/{cid}", status_code=303)


def _confere_ownership(user_id: int, campanha_id: int) -> dict:
    c = campanhas.obter(campanha_id)
    if c is None:
        raise HTTPException(404)
    with get_conn() as conn:
        own = conn.execute(
            "SELECT 1 FROM perfis_remetente WHERE id = ? AND usuario_id = ?",
            (c["perfil_id"], user_id),
        ).fetchone()
    if not own:
        raise HTTPException(403)
    return c


_DIAS_SEMANA_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]


def _calcular_proximos_7_dias(c: dict, enviados_hoje: int) -> list[dict]:
    from datetime import date, timedelta
    dias = campanhas.parse_dias_semana(c["dias_semana"])
    out = []
    hoje = date.today()
    for i in range(7):
        d = hoje + timedelta(days=i)
        wd = d.weekday()
        item = {"data": d.strftime("%d/%m"), "dia_nome": _DIAS_SEMANA_PT[wd]}
        if c["status"] == "concluida":
            item["tipo"] = "concluida"
        elif wd not in dias:
            item["tipo"] = "fora"
        else:
            item["tipo"] = "envio"
            falta = c["total_alvo"] - c["enviados_total"]
            if i == 0:
                item["previsto"] = max(0, min(c["por_dia"] - enviados_hoje, falta))
            else:
                item["previsto"] = max(0, min(c["por_dia"], falta))
        out.append(item)
    return out


def _estimar_conclusao(c: dict, enviados_hoje: int) -> str | None:
    if c["status"] not in ("ativa", "pausada"):
        return None
    falta = c["total_alvo"] - c["enviados_total"]
    if falta <= 0:
        return None
    dias = campanhas.parse_dias_semana(c["dias_semana"])
    if not dias:
        return None
    cabe_hoje = max(0, c["por_dia"] - enviados_hoje)
    falta -= cabe_hoje
    if falta <= 0:
        return "hoje"
    dias_uteis_necessarios = -(-falta // c["por_dia"])  # ceil
    from datetime import date, timedelta
    cur = date.today()
    contados = 0
    while contados < dias_uteis_necessarios:
        cur += timedelta(days=1)
        if cur.weekday() in dias:
            contados += 1
    return cur.strftime("%a %d/%m/%Y")


def _texto_status_worker(c: dict, enviados_hoje: int) -> str:
    if c["status"] == "rascunho":
        return "Rascunho — clique em Iniciar"
    if c["status"] == "pausada":
        return "Pausada — clique em Retomar"
    if c["status"] == "cancelada":
        return "Cancelada"
    if c["status"] == "concluida":
        return "Concluída"
    if enviados_hoje >= c["por_dia"]:
        return "Quota diária atingida — retomará no próximo dia configurado"
    return "Ativa — aguardando próximo envio"


def _ctx_detalhe(user: dict, campanha_id: int) -> dict:
    """Monta o contexto para campanha_detalhe / parcial."""
    c = _confere_ownership(user["id"], campanha_id)
    with get_conn() as conn:
        p = conn.execute(
            "SELECT nome, email_remetente FROM perfis_remetente WHERE id = ?",
            (c["perfil_id"],),
        ).fetchone()
        c["perfil_nome"] = p["nome"]
        c["perfil_email"] = p["email_remetente"]

        enviados_hoje = campanhas.enviados_hoje_campanha(campanha_id)
        progresso_pct = (c["enviados_total"] * 100 // c["total_alvo"]) if c["total_alvo"] else 0

        rows_hist = conn.execute(
            "SELECT date(enviado_em, 'localtime') AS data, "
            "  SUM(status='ok') AS enviados, "
            "  SUM(status='erro') AS erros, "
            "  SUM(status IN ('bounce','bounce_soft')) AS bounces "
            "FROM envios WHERE campanha_id = ? "
            "GROUP BY 1 ORDER BY 1 DESC LIMIT 30",
            (campanha_id,),
        ).fetchall()
        historico_por_dia = []
        for r in rows_hist:
            row = dict(r)
            ab = conn.execute(
                "SELECT COUNT(*) c FROM envios e "
                "JOIN aberturas a ON a.envio_id = e.id "
                "WHERE e.campanha_id = ? AND date(e.enviado_em,'localtime')=?",
                (campanha_id, row["data"]),
            ).fetchone()
            row["aberturas"] = ab["c"]
            historico_por_dia.append(row)

        rows_ult = conn.execute(
            "SELECT strftime('%H:%M', e.enviado_em, 'localtime') AS hora, "
            "  c2.email, e.status FROM envios e "
            "JOIN contatos c2 ON c2.id = e.contato_id "
            "WHERE e.campanha_id = ? ORDER BY e.id DESC LIMIT 10",
            (campanha_id,),
        ).fetchall()
        ultimos = [dict(r) for r in rows_ult]

    proximos_7 = _calcular_proximos_7_dias(c, enviados_hoje)
    estimativa = _estimar_conclusao(c, enviados_hoje)
    status_worker = _texto_status_worker(c, enviados_hoje)

    return {
        "campanha": c,
        "enviados_hoje": enviados_hoje,
        "progresso_pct": progresso_pct,
        "historico_por_dia": historico_por_dia,
        "ultimos": ultimos,
        "proximos_7": proximos_7,
        "estimativa_conclusao": estimativa,
        "status_worker_texto": status_worker,
    }


@app.get("/campanhas/{campanha_id}", response_class=HTMLResponse)
def campanha_detalhe(campanha_id: int, request: Request,
                      user: dict = Depends(requer_login)):
    ctx = _ctx_detalhe(user, campanha_id)
    ctx.update(request=request, user=user)
    return templates.TemplateResponse("campanha_detalhe.html", ctx)


@app.get("/campanhas/{campanha_id}/parcial", response_class=HTMLResponse)
def campanha_detalhe_parcial(campanha_id: int, request: Request,
                              user: dict = Depends(requer_login)):
    ctx = _ctx_detalhe(user, campanha_id)
    ctx.update(request=request, user=user)
    return templates.TemplateResponse("_campanha_detalhe_corpo.html", ctx)


@app.post("/campanhas/{campanha_id}/iniciar")
def campanha_iniciar(campanha_id: int, user: dict = Depends(requer_login)):
    _confere_ownership(user["id"], campanha_id)
    try:
        campanhas.iniciar(campanha_id)
    except ValueError as e:
        return RedirectResponse(url=f"/campanhas/{campanha_id}?erro={e}", status_code=303)
    return RedirectResponse(url=f"/campanhas/{campanha_id}", status_code=303)


@app.post("/campanhas/{campanha_id}/pausar")
def campanha_pausar(campanha_id: int, user: dict = Depends(requer_login)):
    _confere_ownership(user["id"], campanha_id)
    campanhas.pausar(campanha_id, motivo="Pausada manualmente")
    return RedirectResponse(url=f"/campanhas/{campanha_id}", status_code=303)


@app.post("/campanhas/{campanha_id}/retomar")
def campanha_retomar(campanha_id: int, user: dict = Depends(requer_login)):
    _confere_ownership(user["id"], campanha_id)
    try:
        campanhas.retomar(campanha_id)
    except ValueError:
        pass
    return RedirectResponse(url=f"/campanhas/{campanha_id}", status_code=303)


@app.post("/campanhas/{campanha_id}/cancelar")
def campanha_cancelar(campanha_id: int, user: dict = Depends(requer_login)):
    _confere_ownership(user["id"], campanha_id)
    campanhas.cancelar(campanha_id)
    return RedirectResponse(url=f"/campanhas/{campanha_id}", status_code=303)


@app.get("/campanhas/{campanha_id}/editar", response_class=HTMLResponse)
def campanha_editar_form(campanha_id: int, request: Request,
                          user: dict = Depends(requer_login),
                          erro: str | None = None):
    c = _confere_ownership(user["id"], campanha_id)
    ufs, tribunais = _ufs_e_tribunais()
    return templates.TemplateResponse("campanha_form.html", {
        "request": request, "user": user, "erro": erro,
        "perfis": _perfis_para_form(user["id"]),
        "ufs": ufs, "tribunais": tribunais,
        "campanha": c,
    })


@app.post("/campanhas/{campanha_id}/editar")
def campanha_editar(
    campanha_id: int, request: Request,
    user: dict = Depends(requer_login),
    nome: str = Form(...),
    estado: str = Form(""),
    tribunal: str = Form(""),
    total_alvo: int = Form(...),
    por_dia: int = Form(...),
    dias_semana: list[str] = Form(default=[]),
    janela_inicio: str = Form(...),
    janela_fim: str = Form(...),
):
    _confere_ownership(user["id"], campanha_id)
    try:
        campanhas.editar(
            campanha_id,
            nome=nome,
            filtros={"estado": estado, "tribunal": tribunal},
            total_alvo=total_alvo,
            por_dia=por_dia,
            dias_semana=_parse_form_dias(dias_semana),
            janela_inicio=_parse_form_hora(janela_inicio),
            janela_fim=_parse_form_hora(janela_fim),
        )
    except ValueError as e:
        c = campanhas.obter(campanha_id)
        ufs, tribunais = _ufs_e_tribunais()
        return templates.TemplateResponse("campanha_form.html", {
            "request": request, "user": user, "erro": str(e),
            "perfis": _perfis_para_form(user["id"]),
            "ufs": ufs, "tribunais": tribunais, "campanha": c,
        })
    return RedirectResponse(url=f"/campanhas/{campanha_id}", status_code=303)


# ─── Envio de teste ────────────────────────────────────────────────────

@app.get("/teste", response_class=HTMLResponse)
def teste_form(request: Request, user: dict = Depends(requer_login),
               perfil_id: str = "", erro: str | None = None):
    with get_conn() as conn:
        perfis = [dict(r) for r in conn.execute(
            "SELECT * FROM perfis_remetente WHERE usuario_id = ? ORDER BY nome",
            (user["id"],),
        )]
    perfil_sel = None
    if perfil_id:
        for p in perfis:
            if str(p["id"]) == perfil_id:
                perfil_sel = p
                break
    if not perfil_sel and perfis:
        perfil_sel = perfis[0]
    return templates.TemplateResponse("teste.html", {
        "request": request, "user": user,
        "perfis": perfis, "perfil_sel": perfil_sel, "erro": erro,
        "tracking_url": (settings.tracking_base_url or "").rstrip("/"),
    })


@app.post("/teste/enviar")
def teste_enviar(
    user: dict = Depends(requer_login),
    perfil_id: int = Form(...),
    email_destino: str = Form(...),
    assunto: str = Form(...),
    corpo_texto: str = Form(...),
    corpo_html: str = Form(...),
):
    with get_conn() as conn:
        own = conn.execute(
            "SELECT 1 FROM perfis_remetente WHERE id = ? AND usuario_id = ?",
            (perfil_id, user["id"]),
        ).fetchone()
    if not own:
        raise HTTPException(403)

    envio_id, erro = mailer.enviar_teste(
        perfil_id, email_destino, assunto, corpo_texto, corpo_html,
    )
    if erro and not envio_id:
        params = urlencode({"perfil_id": perfil_id, "erro": erro})
        return RedirectResponse(url=f"/teste?{params}", status_code=303)
    return RedirectResponse(url=f"/teste/historico/envio/{envio_id}", status_code=303)


# ─── Histórico de testes (restrito ao menu Enviar teste) ───────────────

@app.get("/teste/historico", response_class=HTMLResponse)
def teste_historico(
    request: Request,
    user: dict = Depends(requer_login),
    perfil_id: str = "", status: str = "",
    desde: str = "", ate: str = "", pagina: int = 1,
):
    por_pagina = 100
    pagina = max(1, pagina)

    with get_conn() as conn:
        perfis = [dict(r) for r in conn.execute(
            "SELECT id, nome FROM perfis_remetente WHERE usuario_id = ? ORDER BY nome",
            (user["id"],),
        )]
        perfis_ids = [str(p["id"]) for p in perfis]

    where = ["1=1", "c.tribunal = '_teste'"]
    args: list = []
    if perfis_ids:
        where.append(f"e.perfil_remetente_id IN ({','.join('?' * len(perfis_ids))})")
        args.extend(perfis_ids)
    else:
        where.append("0=1")
    if perfil_id and perfil_id in perfis_ids:
        where.append("e.perfil_remetente_id = ?"); args.append(perfil_id)
    if status in ("ok", "erro", "bounce", "bounce_soft"):
        where.append("e.status = ?"); args.append(status)
    if desde:
        where.append("date(e.enviado_em) >= date(?)"); args.append(desde)
    if ate:
        where.append("date(e.enviado_em) <= date(?)"); args.append(ate)
    sql_where = " AND ".join(where)

    base_from = "FROM envios e JOIN contatos c ON c.id = e.contato_id"

    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) c {base_from} WHERE {sql_where}", args).fetchone()["c"]
        contagem = {
            "ok": conn.execute(
                f"SELECT COUNT(*) c {base_from} WHERE {sql_where} AND e.status = 'ok'", args
            ).fetchone()["c"],
            "erro": conn.execute(
                f"SELECT COUNT(*) c {base_from} WHERE {sql_where} AND e.status = 'erro'", args
            ).fetchone()["c"],
            "bounce": conn.execute(
                f"SELECT COUNT(*) c {base_from} WHERE {sql_where} AND e.status = 'bounce'", args
            ).fetchone()["c"],
            "bounce_soft": conn.execute(
                f"SELECT COUNT(*) c {base_from} WHERE {sql_where} AND e.status = 'bounce_soft'", args
            ).fetchone()["c"],
            "abertos": conn.execute(
                f"SELECT COUNT(DISTINCT a.envio_id) c {base_from} "
                f"JOIN aberturas a ON a.envio_id = e.id "
                f"WHERE {sql_where} AND a.tipo = 'cliente'", args
            ).fetchone()["c"],
        }
        rows = conn.execute(
            f"""
            SELECT e.id, e.enviado_em, e.status, e.erro_mensagem,
                   e.bounce_em, e.bounce_codigo, e.bounce_diagnostico,
                   c.email,
                   p.nome AS perfil_nome,
                   (SELECT COUNT(*) FROM aberturas a WHERE a.envio_id = e.id AND a.tipo = 'cliente') AS aberturas,
                   (SELECT MIN(aberta_em) FROM aberturas a WHERE a.envio_id = e.id AND a.tipo = 'cliente') AS primeira_abertura
              FROM envios e
              JOIN contatos c ON c.id = e.contato_id
              JOIN perfis_remetente p ON p.id = e.perfil_remetente_id
             WHERE {sql_where}
             ORDER BY e.id DESC
             LIMIT ? OFFSET ?
            """,
            [*args, por_pagina, (pagina - 1) * por_pagina],
        ).fetchall()

    filtros = {"perfil_id": perfil_id, "status": status, "desde": desde, "ate": ate}

    def qs_pag(p: int) -> str:
        return urlencode({**filtros, "pagina": p})

    return templates.TemplateResponse("teste_historico.html", {
        "request": request, "user": user,
        "perfis": perfis,
        "registros": [dict(r) for r in rows], "total": total, "contagem": contagem,
        "filtros": filtros, "pagina": pagina, "por_pagina": por_pagina,
        "paginas_total": max(1, (total + por_pagina - 1) // por_pagina),
        "qs_pag": qs_pag,
    })


@app.get("/teste/historico/envio/{envio_id}", response_class=HTMLResponse)
def teste_historico_envio(envio_id: int, request: Request, user: dict = Depends(requer_login)):
    ctx = _ctx_envio(envio_id, user, request)
    if (ctx["envio"].get("tribunal") or "") != "_teste":
        raise HTTPException(404)
    ctx["voltar_url"] = "/teste/historico"
    ctx["voltar_label"] = "Histórico de testes"
    return templates.TemplateResponse("historico_envio.html", ctx)


# ─── Histórico ─────────────────────────────────────────────────────────

@app.get("/historico", response_class=HTMLResponse)
def historico(
    request: Request,
    user: dict = Depends(requer_login),
    perfil_id: str = "", status: str = "",
    desde: str = "", ate: str = "", pagina: int = 1,
    campanha_id: str = "",
):
    por_pagina = 100
    pagina = max(1, pagina)

    with get_conn() as conn:
        perfis = [dict(r) for r in conn.execute(
            "SELECT id, nome FROM perfis_remetente WHERE usuario_id = ? ORDER BY nome",
            (user["id"],),
        )]
        perfis_ids = [str(p["id"]) for p in perfis]

    where = ["1=1", "c.tribunal != '_teste'"]
    args: list = []
    if perfis_ids:
        where.append(f"e.perfil_remetente_id IN ({','.join('?' * len(perfis_ids))})")
        args.extend(perfis_ids)
    else:
        where.append("0=1")
    if perfil_id and perfil_id in perfis_ids:
        where.append("e.perfil_remetente_id = ?"); args.append(perfil_id)
    if status in ("ok", "erro", "bounce", "bounce_soft"):
        where.append("e.status = ?"); args.append(status)
    if desde:
        where.append("date(e.enviado_em) >= date(?)"); args.append(desde)
    if ate:
        where.append("date(e.enviado_em) <= date(?)"); args.append(ate)
    if campanha_id and campanha_id.isdigit():
        where.append("e.campanha_id = ?"); args.append(int(campanha_id))
    sql_where = " AND ".join(where)

    base_from = "FROM envios e JOIN contatos c ON c.id = e.contato_id"

    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) c {base_from} WHERE {sql_where}", args).fetchone()["c"]
        contagem = {
            "ok": conn.execute(
                f"SELECT COUNT(*) c {base_from} WHERE {sql_where} AND e.status = 'ok'", args
            ).fetchone()["c"],
            "erro": conn.execute(
                f"SELECT COUNT(*) c {base_from} WHERE {sql_where} AND e.status = 'erro'", args
            ).fetchone()["c"],
            "bounce": conn.execute(
                f"SELECT COUNT(*) c {base_from} WHERE {sql_where} AND e.status = 'bounce'", args
            ).fetchone()["c"],
            "bounce_soft": conn.execute(
                f"SELECT COUNT(*) c {base_from} WHERE {sql_where} AND e.status = 'bounce_soft'", args
            ).fetchone()["c"],
            "abertos": conn.execute(
                f"SELECT COUNT(DISTINCT a.envio_id) c {base_from} "
                f"JOIN aberturas a ON a.envio_id = e.id "
                f"WHERE {sql_where} AND a.tipo = 'cliente'", args
            ).fetchone()["c"],
        }
        rows = conn.execute(
            f"""
            SELECT e.id, e.enviado_em, e.status, e.erro_mensagem,
                   e.bounce_em, e.bounce_codigo, e.bounce_diagnostico,
                   c.email, c.cidade, c.comarca, c.orgao, c.estado, c.tribunal,
                   p.nome AS perfil_nome,
                   (SELECT COUNT(*) FROM aberturas a WHERE a.envio_id = e.id AND a.tipo = 'cliente') AS aberturas,
                   (SELECT MIN(aberta_em) FROM aberturas a WHERE a.envio_id = e.id AND a.tipo = 'cliente') AS primeira_abertura,
                   (SELECT MIN(aberta_em) FROM aberturas a WHERE a.envio_id = e.id AND a.tipo = 'proxy')   AS entrega_em
              FROM envios e
              JOIN contatos c ON c.id = e.contato_id
              JOIN perfis_remetente p ON p.id = e.perfil_remetente_id
             WHERE {sql_where}
             ORDER BY e.id DESC
             LIMIT ? OFFSET ?
            """,
            [*args, por_pagina, (pagina - 1) * por_pagina],
        ).fetchall()
        campanhas_disponiveis = [dict(r) for r in conn.execute(
            "SELECT id, nome FROM campanhas ORDER BY id DESC"
        ).fetchall()]

    filtros = {"perfil_id": perfil_id, "status": status, "desde": desde, "ate": ate, "campanha_id": campanha_id}

    def qs_pag(p: int) -> str:
        return urlencode({**filtros, "pagina": p})

    return templates.TemplateResponse("historico.html", {
        "request": request, "user": user,
        "perfis": perfis,
        "registros": [dict(r) for r in rows], "total": total, "contagem": contagem,
        "filtros": filtros, "pagina": pagina, "por_pagina": por_pagina,
        "paginas_total": max(1, (total + por_pagina - 1) // por_pagina),
        "qs_pag": qs_pag,
        "campanhas_disponiveis": campanhas_disponiveis,
        "filtro_campanha_id": campanha_id,
    })


# ─── Detalhe de um envio (timeline) ────────────────────────────────────

def _ctx_envio(envio_id: int, user: dict, request: Request) -> dict:
    """Carrega envio + aberturas + decide se ainda vale pollar."""
    with get_conn() as conn:
        envio = conn.execute(
            """
            SELECT e.*, c.email AS contato_email, c.cidade, c.comarca, c.orgao,
                   c.estado, c.tribunal, c.sistema, c.invalido,
                   p.nome AS perfil_nome, p.email_remetente, p.usuario_id
              FROM envios e
              JOIN contatos c ON c.id = e.contato_id
              JOIN perfis_remetente p ON p.id = e.perfil_remetente_id
             WHERE e.id = ?
            """,
            (envio_id,),
        ).fetchone()
        if not envio or envio["usuario_id"] != user["id"]:
            raise HTTPException(404)
        # Aberturas reais (cliente) — leituras do destinatário
        aberturas = [dict(r) for r in conn.execute(
            "SELECT * FROM aberturas WHERE envio_id = ? AND tipo = 'cliente' "
            "ORDER BY aberta_em ASC",
            (envio_id,),
        )]
        # Entrega = primeiro hit do proxy do provedor (Gmail/Yahoo/MS)
        entrega = conn.execute(
            "SELECT MIN(aberta_em) AS quando, ip, user_agent FROM aberturas "
            "WHERE envio_id = ? AND tipo = 'proxy'",
            (envio_id,),
        ).fetchone()
        entrega = dict(entrega) if entrega and entrega["quando"] else None
        # Todos os hits para debug visual (proxy + cliente)
        hits_brutos = [dict(r) for r in conn.execute(
            "SELECT * FROM aberturas WHERE envio_id = ? ORDER BY aberta_em ASC",
            (envio_id,),
        )]

    # Polling sempre ativo enquanto status indica que pode mudar.
    # O usuário tem botão "pausar" se quiser parar.
    poll_ativo = envio["status"] in ("ok", "bounce_soft")

    return {
        "request": request, "user": user,
        "envio": dict(envio), "aberturas": aberturas, "entrega": entrega,
        "hits_brutos": hits_brutos,
        "poll_ativo": poll_ativo,
        "tracking_url": (settings.tracking_base_url or "").rstrip("/"),
    }


@app.get("/historico/envio/{envio_id}", response_class=HTMLResponse)
def historico_envio(envio_id: int, request: Request, user: dict = Depends(requer_login)):
    ctx = _ctx_envio(envio_id, user, request)
    # Envios de teste vivem só no menu /teste — redireciona para a view de lá.
    if (ctx["envio"].get("tribunal") or "") == "_teste":
        return RedirectResponse(url=f"/teste/historico/envio/{envio_id}", status_code=303)
    return templates.TemplateResponse("historico_envio.html", ctx)


@app.get("/historico/envio/{envio_id}/atualizar", response_class=HTMLResponse)
def historico_envio_atualizar(envio_id: int, request: Request, user: dict = Depends(requer_login)):
    """Endpoint htmx: retorna só o partial pra auto-refresh da timeline."""
    ctx = _ctx_envio(envio_id, user, request)
    return templates.TemplateResponse("_envio_corpo.html", ctx)


# ─── Histórico por vara (agregado) ─────────────────────────────────────

@app.get("/historico/por-vara", response_class=HTMLResponse)
def historico_por_vara(
    request: Request,
    user: dict = Depends(requer_login),
    perfil_id: str = "", estado: str = "", tribunal: str = "",
    q: str = "", ordenar: str = "ultimo",
    pagina: int = 1,
):
    por_pagina = 50
    pagina = max(1, pagina)

    with get_conn() as conn:
        perfis = [dict(r) for r in conn.execute(
            "SELECT id, nome FROM perfis_remetente WHERE usuario_id = ? ORDER BY nome",
            (user["id"],),
        )]
    perfis_ids = [str(p["id"]) for p in perfis]

    where = ["c.tribunal != '_teste'"]
    args: list = []
    if perfis_ids:
        where.append(f"e.perfil_remetente_id IN ({','.join('?' * len(perfis_ids))})")
        args.extend(perfis_ids)
    else:
        where.append("0=1")
    if perfil_id and perfil_id in perfis_ids:
        where.append("e.perfil_remetente_id = ?"); args.append(perfil_id)
    if estado:
        where.append("c.estado = ?"); args.append(estado)
    if tribunal:
        where.append("c.tribunal = ?"); args.append(tribunal)
    if q:
        where.append("(c.comarca LIKE ? OR c.orgao LIKE ? OR c.cidade LIKE ?)")
        like = f"%{q}%"
        args.extend([like, like, like])
    sql_where = " AND ".join(where)

    order_col = {
        "ultimo":   "ultimo_envio DESC",
        "enviados": "enviados DESC",
        "abertos":  "abertos DESC",
        "vara":     "comarca COLLATE NOCASE ASC",
    }.get(ordenar, "ultimo_envio DESC")

    sql = f"""
      SELECT
        c.tribunal, c.estado, c.cidade, c.comarca, c.orgao,
        COUNT(DISTINCT e.id) AS enviados,
        SUM(CASE WHEN e.status = 'ok'          THEN 1 ELSE 0 END) AS ok,
        SUM(CASE WHEN e.status = 'erro'        THEN 1 ELSE 0 END) AS erros,
        SUM(CASE WHEN e.status = 'bounce'      THEN 1 ELSE 0 END) AS bounces,
        SUM(CASE WHEN e.status = 'bounce_soft' THEN 1 ELSE 0 END) AS bounces_soft,
        COUNT(DISTINCT a.envio_id)             AS abertos,
        MAX(e.enviado_em)                      AS ultimo_envio,
        MAX(a.aberta_em)                       AS ultima_abertura
      FROM envios e
      JOIN contatos c ON c.id = e.contato_id
      LEFT JOIN aberturas a ON a.envio_id = e.id AND a.tipo = 'cliente'
     WHERE {sql_where}
     GROUP BY c.tribunal, c.estado, c.cidade, c.comarca, c.orgao
     ORDER BY {order_col}
     LIMIT ? OFFSET ?
    """
    sql_count = f"""
      SELECT COUNT(*) AS c FROM (
        SELECT 1
          FROM envios e
          JOIN contatos c ON c.id = e.contato_id
         WHERE {sql_where}
         GROUP BY c.tribunal, c.estado, c.cidade, c.comarca, c.orgao
      )
    """

    with get_conn() as conn:
        total = conn.execute(sql_count, args).fetchone()["c"]
        rows = conn.execute(sql, [*args, por_pagina, (pagina - 1) * por_pagina]).fetchall()

        # Totais gerais aplicando os mesmos filtros
        totais = conn.execute(f"""
          SELECT
            COUNT(DISTINCT e.id) AS enviados,
            COUNT(DISTINCT a.envio_id) AS abertos,
            SUM(CASE WHEN e.status = 'bounce' THEN 1 ELSE 0 END) AS bounces
          FROM envios e
          JOIN contatos c ON c.id = e.contato_id
          LEFT JOIN aberturas a ON a.envio_id = e.id AND a.tipo = 'cliente'
         WHERE {sql_where}
        """, args).fetchone()

    ufs, tribunais = _ufs_e_tribunais()
    filtros = {
        "perfil_id": perfil_id, "estado": estado, "tribunal": tribunal,
        "q": q, "ordenar": ordenar,
    }

    def qs_pag(p: int) -> str:
        return urlencode({**filtros, "pagina": p})

    return templates.TemplateResponse("historico_por_vara.html", {
        "request": request, "user": user,
        "registros": [dict(r) for r in rows], "total": total,
        "totais": dict(totais) if totais else {"enviados": 0, "abertos": 0, "bounces": 0},
        "perfis": perfis, "ufs": ufs, "tribunais": tribunais,
        "filtros": filtros, "pagina": pagina, "por_pagina": por_pagina,
        "paginas_total": max(1, (total + por_pagina - 1) // por_pagina),
        "qs_pag": qs_pag,
    })


# ─── Agendamentos ──────────────────────────────────────────────────────

_DIAS_SEMANA = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]


def _quando(ag: dict) -> str:
    hora = ag.get("hora") or "—"
    freq = ag.get("frequencia")
    if freq == "uma_vez":
        try:
            d = ag.get("data") or ""
            ano, mes, dia = d.split("-")
            return f"Em {dia}/{mes}/{ano} às {hora}"
        except Exception:
            return f"Em {ag.get('data')} às {hora}"
    if freq == "diario":
        return f"Todos os dias às {hora}"
    if freq == "semanal":
        ds = ag.get("dia_semana")
        nome = _DIAS_SEMANA[ds] if ds is not None and 0 <= ds < 7 else "?"
        return f"Toda {nome.lower()} às {hora}"
    if freq == "mensal":
        dm = ag.get("dia_mes")
        return f"Todo dia {dm} do mês às {hora}"
    return "—"


def _o_que(ag: dict) -> str:
    if ag.get("tipo") == "scraper":
        alvo = (ag.get("alvo") or "").lower()
        if alvo == "todos":
            return "Scraper · todos os tribunais (em sequência)"
        return f"Scraper {alvo.upper()}"
    return ag.get("tipo") or "—"


def _ctx_agendamento_form(user: dict, request: Request, erro: str | None = None) -> dict:
    return {
        "request": request, "user": user, "erro": erro,
        "scrapers": scraper_registry.listar(),
    }


@app.get("/agendamentos", response_class=HTMLResponse)
def agendamentos_lista(request: Request, user: dict = Depends(requer_login)):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM agendamentos ORDER BY id DESC").fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["proxima"]   = scheduler.proxima_execucao(d["id"]) if d["ativo"] else None
        d["descricao"] = _quando(d)
        d["o_que"]     = _o_que(d)
        items.append(d)
    return templates.TemplateResponse("agendamentos.html", {
        "request": request, "user": user, "agendamentos": items,
    })


@app.get("/agendamentos/novo", response_class=HTMLResponse)
def agendamentos_novo_form(request: Request, user: dict = Depends(requer_login), erro: str | None = None):
    return templates.TemplateResponse("agendamento_form.html",
                                      _ctx_agendamento_form(user, request, erro))


@app.post("/agendamentos/novo")
def agendamentos_novo_submit(
    request: Request,
    user: dict = Depends(requer_login),
    nome: str = Form(...),
    tipo: str = Form(...),
    alvo: str = Form(""),
    frequencia: str = Form(...),
    hora: str = Form(...),
    data: str = Form(""),
    dia_semana: str = Form(""),
    dia_mes: str = Form(""),
):
    erro: str | None = None

    if tipo != "scraper":
        erro = "Tipo de agendamento inválido (apenas 'scraper')."
    elif not alvo:
        erro = "Escolha qual scraper rodar."

    if erro is None:
        if frequencia == "uma_vez" and not data:
            erro = "Para 'apenas uma vez', informe a data."
        elif frequencia == "semanal" and dia_semana == "":
            erro = "Para 'toda semana', escolha o dia da semana."
        elif frequencia == "mensal" and dia_mes == "":
            erro = "Para 'todo mês', informe o dia do mês."

    if erro:
        return templates.TemplateResponse("agendamento_form.html",
                                          _ctx_agendamento_form(user, request, erro))

    ds = int(dia_semana) if dia_semana != "" else None
    dm = int(dia_mes) if dia_mes != "" else None

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agendamentos (nome, tipo, alvo, perfil_id, filtro_estado, "
            "filtro_tribunal, quantidade, frequencia, hora, data, dia_semana, "
            "dia_mes, cron, ativo) "
            "VALUES (?, 'scraper', ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, '', 1)",
            (nome.strip(), alvo,
             frequencia, hora, data or None, ds, dm),
        )
    scheduler.recarregar()
    return RedirectResponse(url="/agendamentos", status_code=303)


@app.get("/agendamentos/log", response_class=HTMLResponse)
def agendamentos_log(
    request: Request,
    user: dict = Depends(requer_login),
    ag_id: str = "", status: str = "", pagina: int = 1,
):
    por_pagina = 100
    pagina = max(1, pagina)

    where = ["1=1"]
    args: list = []
    if ag_id and ag_id.isdigit():
        where.append("ag_id = ?"); args.append(int(ag_id))
    if status in ("ok", "erro", "missed", "rodando"):
        where.append("status = ?"); args.append(status)
    sql_where = " AND ".join(where)

    with get_conn() as conn:
        agendamentos = [dict(r) for r in conn.execute(
            "SELECT id, nome FROM agendamentos ORDER BY id DESC"
        )]
        total = conn.execute(
            f"SELECT COUNT(*) c FROM cron_runs WHERE {sql_where}", args
        ).fetchone()["c"]
        contagem = {
            "ok": conn.execute(
                f"SELECT COUNT(*) c FROM cron_runs WHERE {sql_where} AND status = 'ok'", args
            ).fetchone()["c"],
            "erro": conn.execute(
                f"SELECT COUNT(*) c FROM cron_runs WHERE {sql_where} AND status = 'erro'", args
            ).fetchone()["c"],
            "missed": conn.execute(
                f"SELECT COUNT(*) c FROM cron_runs WHERE {sql_where} AND status = 'missed'", args
            ).fetchone()["c"],
        }
        rows = conn.execute(
            f"SELECT * FROM cron_runs WHERE {sql_where} "
            f"ORDER BY id DESC LIMIT ? OFFSET ?",
            [*args, por_pagina, (pagina - 1) * por_pagina],
        ).fetchall()

    filtros = {"ag_id": ag_id, "status": status}

    def qs_pag(p: int) -> str:
        return urlencode({**filtros, "pagina": p})

    return templates.TemplateResponse("agendamentos_log.html", {
        "request": request, "user": user,
        "agendamentos": agendamentos,
        "registros": [dict(r) for r in rows],
        "total": total, "contagem": contagem,
        "filtros": filtros, "pagina": pagina, "por_pagina": por_pagina,
        "paginas_total": max(1, (total + por_pagina - 1) // por_pagina),
        "qs_pag": qs_pag,
        "scheduler_status": scheduler.status_scheduler(),
    })


@app.post("/agendamentos/{aid}/toggle")
def agendamentos_toggle(aid: int, user: dict = Depends(requer_login)):
    with get_conn() as conn:
        conn.execute("UPDATE agendamentos SET ativo = 1 - ativo WHERE id = ?", (aid,))
    scheduler.recarregar()
    return RedirectResponse(url="/agendamentos", status_code=303)


@app.post("/agendamentos/{aid}/excluir")
def agendamentos_excluir(aid: int, user: dict = Depends(requer_login)):
    with get_conn() as conn:
        conn.execute("DELETE FROM agendamentos WHERE id = ?", (aid,))
    scheduler.recarregar()
    return RedirectResponse(url="/agendamentos", status_code=303)


# ─── Tracking pixel (público, sem login) ───────────────────────────────

# PNG 1x1 transparente (67 bytes)
_PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63600100000005000146cdc1390000000049454e44ae426082"
)


# User-Agents conhecidos de proxies / pré-fetch — esses hits são "entrega",
# não leitura real. Quando o e-mail chega na caixa, Gmail/Yahoo/etc baixam
# todas as imagens em servidor próprio antes do destinatário abrir.
_PROXY_UA_RE = re.compile(
    r"(GoogleImageProxy|YahooMailProxy|BingPreview|MicrosoftPreview|"
    r"SkypeUriPreview|OutlookSafe|ggpht\.com|via Google|FeedFetcher-Google|"
    r"Apple-Mail|MailScanner)",
    re.IGNORECASE,
)

# Ranges de IP usados pelo proxy de imagens do Gmail/Google.
# Detecção por IP pega casos onde o UA está camuflado (Gmail mobile às vezes).
_PROXY_IP_NETS = [
    # Google
    ipaddress.ip_network("66.102.0.0/20"),
    ipaddress.ip_network("66.249.64.0/19"),
    ipaddress.ip_network("209.85.128.0/17"),
    ipaddress.ip_network("173.194.0.0/16"),
    ipaddress.ip_network("64.233.160.0/19"),
    ipaddress.ip_network("72.14.192.0/18"),
    ipaddress.ip_network("64.18.0.0/20"),
    # Yahoo
    ipaddress.ip_network("98.137.0.0/16"),
    ipaddress.ip_network("87.248.96.0/19"),
    # Microsoft
    ipaddress.ip_network("40.92.0.0/15"),
    ipaddress.ip_network("40.107.0.0/16"),
    ipaddress.ip_network("52.100.0.0/14"),
]


def _ip_eh_proxy(ip: str) -> bool:
    if not ip or ip in ("?", ""):
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _PROXY_IP_NETS)


def _classificar_hit(user_agent: str, ip: str = "") -> str:
    """Retorna 'proxy' (entrega via pré-fetch) ou 'cliente' (leitura real)."""
    if _PROXY_UA_RE.search(user_agent or ""):
        return "proxy"
    if _ip_eh_proxy(ip):
        return "proxy"
    return "cliente"


@app.get("/o/{token}.png")
def tracking_pixel(token: str, request: Request):
    """Registra abertura do email; retorna sempre PNG 1x1 transparente."""
    ip = request.client.host if request.client else "?"
    # Quando atrás de proxy (EasyPanel/Traefik), o IP real vem no X-Forwarded-For
    xff = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if xff:
        ip = xff
    ua = request.headers.get("user-agent", "")[:300]
    tipo = _classificar_hit(ua, ip)
    if token and len(token) <= 64:
        try:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT id FROM envios WHERE tracking_token = ? LIMIT 1", (token,)
                ).fetchone()
                if row:
                    conn.execute(
                        "INSERT INTO aberturas (envio_id, ip, user_agent, tipo) VALUES (?, ?, ?, ?)",
                        (row["id"], ip, ua, tipo),
                    )
                    print(f"[tracking] OK envio={row['id']} tipo={tipo} ip={ip} ua={ua[:80]!r}", flush=True)
                else:
                    print(f"[tracking] TOKEN_NAO_ENCONTRADO token={token} ip={ip}", flush=True)
        except Exception as e:
            print(f"[tracking] ERRO token={token}: {e!r}", flush=True)
    else:
        print(f"[tracking] TOKEN_INVALIDO token={token!r}", flush=True)
    return Response(
        content=_PIXEL_PNG,
        media_type="image/png",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


# ─── Health ────────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz():
    return {"ok": True}
