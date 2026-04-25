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

import shutil
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
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
from . import mailer, scheduler
from .scrapers import configs as scraper_configs
from .scrapers import registry as scraper_registry
from .scrapers import runner as scraper_runner


BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Peritos — Painel")

app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, https_only=False)
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
            "SELECT DISTINCT estado FROM contatos WHERE estado IS NOT NULL ORDER BY estado"
        )]
        tribunais = [r["tribunal"] for r in conn.execute(
            "SELECT DISTINCT tribunal FROM contatos WHERE tribunal IS NOT NULL ORDER BY tribunal"
        )]
    return ufs, tribunais


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
        total_contatos = conn.execute("SELECT COUNT(*) c FROM contatos").fetchone()["c"]
        total_envios = conn.execute("SELECT COUNT(*) c FROM envios WHERE status = 'ok'").fetchone()["c"]
        envios_hoje = conn.execute(
            "SELECT COUNT(*) c FROM envios WHERE status = 'ok' "
            "AND date(enviado_em) = date('now', 'localtime')"
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


@app.get("/scrapers/run/{run_id}", response_class=HTMLResponse)
def scrapers_run(run_id: int, request: Request, user: dict = Depends(requer_login)):
    run = scraper_runner.get_run(run_id)
    if not run:
        raise HTTPException(404, "Execução não encontrada")
    return templates.TemplateResponse("scraper_run.html", {
        "request": request, "user": user, "run": run,
    })


@app.get("/scrapers/run/{run_id}/log", response_class=HTMLResponse)
def scrapers_run_log(run_id: int, user: dict = Depends(requer_login)):
    run = scraper_runner.get_run(run_id)
    if not run:
        raise HTTPException(404)
    return HTMLResponse(f"<pre id='log' class='log'>{(run['log'] or '')}</pre>")


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
    return templates.TemplateResponse("perfis.html", {
        "request": request, "user": user, "perfis": [dict(r) for r in rows],
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
            "curriculo_path, limite_diario) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], nome, email_remetente, smtp_host, smtp_port,
             encrypt(smtp_senha), assunto, corpo_texto, corpo_html, assinatura,
             curriculo_path or None, limite_diario),
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
            "corpo_html = ?, assinatura = ?, curriculo_path = ?, limite_diario = ? "
            "WHERE id = ?",
            (nome, email_remetente, smtp_host, smtp_port, senha_enc, assunto,
             corpo_texto, corpo_html, assinatura, curriculo_path, limite_diario, pid),
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


# ─── Campanhas ─────────────────────────────────────────────────────────

@app.get("/campanhas", response_class=HTMLResponse)
def campanhas_form(request: Request, user: dict = Depends(requer_login)):
    with get_conn() as conn:
        perfis = [dict(r) for r in conn.execute(
            "SELECT * FROM perfis_remetente WHERE usuario_id = ? ORDER BY nome",
            (user["id"],),
        )]
    ufs, tribunais = _ufs_e_tribunais()
    ativas = []
    for p in perfis:
        e = mailer.estado(p["id"])
        if e and not e["terminado"]:
            ativas.append(e)
    return templates.TemplateResponse("campanhas.html", {
        "request": request, "user": user,
        "perfis": perfis, "ufs": ufs, "tribunais": tribunais,
        "execucoes_ativas": ativas,
    })


@app.post("/campanhas/disparar")
def campanhas_disparar(
    user: dict = Depends(requer_login),
    perfil_id: int = Form(...),
    estado: str = Form(""),
    tribunal: str = Form(""),
    total_alvo: int = Form(50),
):
    with get_conn() as conn:
        own = conn.execute(
            "SELECT 1 FROM perfis_remetente WHERE id = ? AND usuario_id = ?",
            (perfil_id, user["id"]),
        ).fetchone()
    if not own:
        raise HTTPException(403)
    filtros = {"estado": estado or None, "tribunal": tribunal or None}
    mailer.disparar(perfil_id, total_alvo, filtros)
    return RedirectResponse(url=f"/campanhas/acompanhar/{perfil_id}", status_code=303)


@app.get("/campanhas/acompanhar/{perfil_id}", response_class=HTMLResponse)
def campanhas_acompanhar(perfil_id: int, request: Request, user: dict = Depends(requer_login)):
    return templates.TemplateResponse("campanha_acompanhar.html", {
        "request": request, "user": user,
        "perfil_id": perfil_id, "estado": mailer.estado(perfil_id),
    })


@app.get("/campanhas/estado/{perfil_id}", response_class=HTMLResponse)
def campanhas_estado(perfil_id: int, request: Request, user: dict = Depends(requer_login)):
    return templates.TemplateResponse("_campanha_estado.html", {
        "request": request, "user": user,
        "perfil_id": perfil_id, "estado": mailer.estado(perfil_id),
    })


@app.post("/campanhas/cancelar/{perfil_id}")
def campanhas_cancelar(perfil_id: int, user: dict = Depends(requer_login)):
    mailer.cancelar(perfil_id)
    return RedirectResponse(url=f"/campanhas/acompanhar/{perfil_id}", status_code=303)


# ─── Histórico ─────────────────────────────────────────────────────────

@app.get("/historico", response_class=HTMLResponse)
def historico(
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

    where = ["1=1"]
    args: list = []
    if perfis_ids:
        where.append(f"e.perfil_remetente_id IN ({','.join('?' * len(perfis_ids))})")
        args.extend(perfis_ids)
    else:
        where.append("0=1")
    if perfil_id and perfil_id in perfis_ids:
        where.append("e.perfil_remetente_id = ?"); args.append(perfil_id)
    if status in ("ok", "erro"):
        where.append("e.status = ?"); args.append(status)
    if desde:
        where.append("date(e.enviado_em) >= date(?)"); args.append(desde)
    if ate:
        where.append("date(e.enviado_em) <= date(?)"); args.append(ate)
    sql_where = " AND ".join(where)

    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM envios e WHERE {sql_where}", args).fetchone()["c"]
        contagem = {
            "ok": conn.execute(
                f"SELECT COUNT(*) c FROM envios e WHERE {sql_where} AND e.status = 'ok'", args
            ).fetchone()["c"],
            "erro": conn.execute(
                f"SELECT COUNT(*) c FROM envios e WHERE {sql_where} AND e.status = 'erro'", args
            ).fetchone()["c"],
        }
        rows = conn.execute(
            f"""
            SELECT e.id, e.enviado_em, e.status, e.erro_mensagem,
                   c.email, c.cidade, c.comarca, c.estado, c.tribunal,
                   p.nome AS perfil_nome
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

    return templates.TemplateResponse("historico.html", {
        "request": request, "user": user,
        "perfis": perfis,
        "registros": [dict(r) for r in rows], "total": total, "contagem": contagem,
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
    if ag.get("tipo") == "campanha":
        partes = []
        if ag.get("filtro_tribunal"):
            partes.append((ag["filtro_tribunal"] or "").upper())
        if ag.get("filtro_estado"):
            partes.append(ag["filtro_estado"])
        filtro = " · ".join(partes) if partes else "todos"
        return f"Campanha · {filtro} · {ag.get('quantidade') or 0}/exec"
    return ag.get("tipo") or "—"


def _ctx_agendamento_form(user: dict, request: Request, erro: str | None = None) -> dict:
    with get_conn() as conn:
        perfis = [dict(r) for r in conn.execute(
            "SELECT id, nome, email_remetente, limite_diario "
            "FROM perfis_remetente WHERE usuario_id = ? ORDER BY nome",
            (user["id"],),
        )]
    ufs, tribunais = _ufs_e_tribunais()
    return {
        "request": request, "user": user, "erro": erro,
        "scrapers": scraper_registry.listar(),
        "perfis": perfis, "ufs": ufs, "tribunais": tribunais,
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
    perfil_id: str = Form(""),
    filtro_estado: str = Form(""),
    filtro_tribunal: str = Form(""),
    quantidade: int = Form(50),
    frequencia: str = Form(...),
    hora: str = Form(...),
    data: str = Form(""),
    dia_semana: str = Form(""),
    dia_mes: str = Form(""),
):
    erro: str | None = None

    if tipo == "scraper":
        if not alvo:
            erro = "Escolha qual scraper rodar."
    elif tipo == "campanha":
        if not perfil_id:
            erro = "Escolha o perfil de remetente."
        else:
            with get_conn() as conn:
                ok = conn.execute(
                    "SELECT 1 FROM perfis_remetente WHERE id = ? AND usuario_id = ?",
                    (int(perfil_id), user["id"]),
                ).fetchone()
            if not ok:
                erro = "Perfil de remetente inválido."
    else:
        erro = "Tipo de agendamento inválido."

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

    pid = int(perfil_id) if perfil_id else None
    ds = int(dia_semana) if dia_semana != "" else None
    dm = int(dia_mes) if dia_mes != "" else None
    alvo_efetivo = alvo if tipo == "scraper" else ""

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agendamentos (nome, tipo, alvo, perfil_id, filtro_estado, "
            "filtro_tribunal, quantidade, frequencia, hora, data, dia_semana, "
            "dia_mes, cron, ativo) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 1)",
            (nome.strip(), tipo, alvo_efetivo, pid,
             filtro_estado or None, filtro_tribunal or None, int(quantidade),
             frequencia, hora, data or None, ds, dm),
        )
    scheduler.recarregar()
    return RedirectResponse(url="/agendamentos", status_code=303)


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


# ─── Health ────────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz():
    return {"ok": True}
