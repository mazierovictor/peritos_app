"""
Login simples por sessão (cookie assinado pelo SessionMiddleware).
"""
from __future__ import annotations

from typing import Optional

import bcrypt
from fastapi import Request
from fastapi.responses import RedirectResponse

from .db import get_conn


def hash_senha(senha: str) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()


def verificar_senha(senha: str, senha_hash: str) -> bool:
    try:
        return bcrypt.checkpw(senha.encode(), senha_hash.encode())
    except ValueError:
        return False


def autenticar(email: str, senha: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, nome, senha_hash FROM usuarios WHERE email = ?",
            (email.lower().strip(),),
        ).fetchone()
    if row and verificar_senha(senha, row["senha_hash"]):
        return {"id": row["id"], "email": row["email"], "nome": row["nome"]}
    return None


def usuario_atual(request: Request) -> Optional[dict]:
    uid = request.session.get("user_id")
    if not uid:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, email, nome FROM usuarios WHERE id = ?", (uid,)
        ).fetchone()
    return dict(row) if row else None


def requer_login(request: Request):
    """Use como Depends(requer_login). Redireciona pro /login se não autenticado."""
    user = usuario_atual(request)
    if not user:
        raise LoginRequired()
    return user


class LoginRequired(Exception):
    pass


def redirect_to_login() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


def criar_usuario(email: str, nome: str, senha: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO usuarios (email, nome, senha_hash) VALUES (?, ?, ?)",
            (email.lower().strip(), nome.strip(), hash_senha(senha)),
        )
        return cur.lastrowid


def garantir_usuarios_iniciais() -> None:
    """Cria usuários definidos no .env se ainda não existem. Idempotente."""
    from .config import settings

    candidatos = [
        (settings.admin_email, settings.admin_nome, settings.admin_senha),
        (settings.user2_email, settings.user2_nome, settings.user2_senha),
    ]
    with get_conn() as conn:
        for email, nome, senha in candidatos:
            if not (email and nome and senha):
                continue
            existe = conn.execute(
                "SELECT 1 FROM usuarios WHERE email = ?", (email.lower().strip(),)
            ).fetchone()
            if not existe:
                conn.execute(
                    "INSERT INTO usuarios (email, nome, senha_hash) VALUES (?, ?, ?)",
                    (email.lower().strip(), nome.strip(), hash_senha(senha)),
                )
