"""
Envio de e-mails: funções públicas para campanhas persistentes e envio de teste.
"""
from __future__ import annotations

import os
import re
import secrets
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path
from string import Template

from .config import settings
from .crypto import decrypt
from .db import get_conn


# Validação de formato de email antes de enviar
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

def email_valido(email: str) -> bool:
    return bool(email and _EMAIL_RE.match(email.strip()))


# Erros SMTP que indicam que o destinatário não existe (devemos marcar como inválido)
_RE_BOUNCE_PERMANENTE = re.compile(
    r"550|551|553|554|user.*(unknown|not.*exist|not.*found)|mailbox.*(unavailable|not.*found)|no such user|recipient.*rejected",
    re.IGNORECASE,
)

def eh_bounce_permanente(mensagem_erro: str) -> bool:
    return bool(_RE_BOUNCE_PERMANENTE.search(mensagem_erro))


def _aplicar_template(template: str, contato: dict, perfil: dict) -> str:
    """Substitui {variaveis} no template do e-mail."""
    valores = {
        "cidade":     (contato.get("cidade")  or "").upper(),
        "comarca":    (contato.get("comarca") or "").upper(),
        "orgao":      (contato.get("orgao")   or "").upper(),
        "estado":     (contato.get("estado")  or "").upper(),
        "tribunal":   (contato.get("tribunal") or "").upper(),
        "sistema":    (contato.get("sistema")  or "").upper(),
        "remetente":  perfil["nome"],
        "email_remetente": perfil["email_remetente"],
        "assinatura": perfil.get("assinatura") or "",
    }
    try:
        return Template(template).safe_substitute(valores)
    except Exception:
        return template


def _enviados_hoje(perfil_id: int) -> int:
    """Conta envios de hoje para o perfil, IGNORANDO envios de teste."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM envios e "
            "JOIN contatos c ON c.id = e.contato_id "
            "WHERE e.perfil_remetente_id = ? AND e.status = 'ok' "
            "AND c.tribunal != '_teste' "
            "AND date(e.enviado_em) = date('now', 'localtime')",
            (perfil_id,),
        ).fetchone()
    return row["c"]


def _ja_enviado(contato_id: int, perfil_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM envios WHERE contato_id = ? AND perfil_remetente_id = ? AND status = 'ok' LIMIT 1",
            (contato_id, perfil_id),
        ).fetchone()
    return row is not None


def registrar_envio(
    contato_id: int, perfil_id: int, status: str,
    erro: str | None, message_id: str | None = None,
    tracking_token: str | None = None,
    campanha_id: int | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO envios (contato_id, perfil_remetente_id, status, erro_mensagem, "
            "message_id, tracking_token, campanha_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (contato_id, perfil_id, status, erro, message_id, tracking_token, campanha_id),
        )


def marcar_contato_invalido(contato_id: int) -> None:
    """Marca o contato como inválido para que ele não seja tentado novamente."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE contatos SET invalido = 1, observacao = COALESCE(observacao, '') "
            "|| 'Marcado inválido após bounce permanente; ' WHERE id = ?",
            (contato_id,),
        )


def carregar_perfil(perfil_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM perfis_remetente WHERE id = ?", (perfil_id,)
        ).fetchone()
    return dict(row) if row else None


def selecionar_contatos(filtros: dict, limite: int, perfil_id: int) -> list[dict]:
    """Retorna contatos elegíveis: válidos, não enviados ainda por este perfil, conforme filtros."""
    where = ["c.invalido = 0"]
    args: list = []

    if filtros.get("estado"):
        where.append("c.estado = ?")
        args.append(filtros["estado"])
    if filtros.get("tribunal"):
        where.append("c.tribunal = ?")
        args.append(filtros["tribunal"])

    sql = f"""
      SELECT c.* FROM contatos c
      WHERE {' AND '.join(where)}
        AND NOT EXISTS (
          SELECT 1 FROM envios e
          WHERE e.contato_id = c.id AND e.perfil_remetente_id = ? AND e.status = 'ok'
        )
      ORDER BY c.id ASC
      LIMIT ?
    """
    args.extend([perfil_id, limite])

    with get_conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def _injetar_pixel(corpo_html: str, token: str) -> str:
    """Insere um pixel 1x1 transparente no fim do HTML pra rastrear abertura."""
    base = (settings.tracking_base_url or "").rstrip("/")
    if not base or not token:
        return corpo_html
    pixel = (
        f'<img src="{base}/o/{token}.png" width="1" height="1" '
        f'alt="" style="display:none;border:0;outline:none;text-decoration:none;" />'
    )
    if "</body>" in corpo_html.lower():
        i = corpo_html.lower().rfind("</body>")
        return corpo_html[:i] + pixel + corpo_html[i:]
    return corpo_html + pixel


def enviar_um_contato(server: smtplib.SMTP, perfil: dict, contato: dict, tracking_token: str) -> str:
    """Envia o e-mail e retorna o Message-ID gerado, para correlação com bounces."""
    msg = MIMEMultipart("mixed")
    sender = f"{perfil['nome']} <{perfil['email_remetente']}>"
    remetente_email = perfil["email_remetente"]

    message_id = make_msgid(domain=remetente_email.split("@")[-1])
    msg["From"] = sender
    msg["To"] = contato["email"]
    msg["Subject"] = _aplicar_template(perfil["assunto"], contato, perfil)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id
    msg["Reply-To"] = sender

    # Cabeçalhos de boa-cidadania anti-spam exigidos por Gmail/Yahoo (desde 2024):
    # permite ao destinatário cancelar com um clique. Reduz chance de spam mark.
    msg["List-Unsubscribe"] = f"<mailto:{remetente_email}?subject=Cancelar%20envios>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["X-Mailer"] = "Peritos"
    msg["Precedence"] = "bulk"
    msg["Auto-Submitted"] = "auto-generated"

    corpo_txt = _aplicar_template(perfil["corpo_texto"], contato, perfil)
    corpo_html = _injetar_pixel(
        _aplicar_template(perfil["corpo_html"], contato, perfil),
        tracking_token,
    )

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(corpo_txt, "plain", "utf-8"))
    alt.attach(MIMEText(corpo_html, "html", "utf-8"))
    msg.attach(alt)

    if perfil.get("curriculo_path"):
        p = Path(perfil["curriculo_path"])
        if p.exists():
            with open(p, "rb") as f:
                part = MIMEApplication(f.read(), Name=p.name)
                part["Content-Disposition"] = f'attachment; filename="{p.name}"'
                msg.attach(part)

    server.sendmail(perfil["email_remetente"], contato["email"], msg.as_string())
    return message_id


def _achar_ou_criar_contato_teste(email: str) -> int:
    """Cria/reusa um contato com tribunal='_teste' para envios de teste."""
    email = (email or "").strip()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM contatos WHERE email = ? AND tribunal = '_teste' LIMIT 1",
            (email,),
        ).fetchone()
        if row:
            # zera o flag de invalido caso o contato tenha levado bounce numa rodada anterior
            conn.execute(
                "UPDATE contatos SET invalido = 0, observacao = 'Envio de teste' WHERE id = ?",
                (row["id"],),
            )
            return row["id"]
        cur = conn.execute(
            "INSERT INTO contatos (email, tribunal, observacao) VALUES (?, '_teste', 'Envio de teste')",
            (email,),
        )
        return cur.lastrowid


def enviar_teste(
    perfil_id: int, email_destino: str,
    assunto: str, corpo_texto: str, corpo_html: str,
) -> tuple[int, str | None]:
    """
    Envia uma única mensagem de teste com tracking pixel ativo.
    Retorna (envio_id, mensagem_de_erro). Se sucesso, erro é None.
    NÃO conta no limite diário.
    """
    perfil = carregar_perfil(perfil_id)
    if not perfil:
        return 0, "Perfil não encontrado."

    if not email_valido(email_destino):
        return 0, "E-mail de destino com formato inválido."

    contato_id = _achar_ou_criar_contato_teste(email_destino)
    contato = {
        "id": contato_id, "email": email_destino,
        "cidade": "TESTE", "comarca": "TESTE", "orgao": "TESTE",
        "estado": "—", "tribunal": "_teste", "sistema": "TESTE",
    }

    # Cria um perfil temporário com os campos override (assunto/corpo)
    perfil_override = dict(perfil)
    perfil_override["assunto"]     = assunto
    perfil_override["corpo_texto"] = corpo_texto
    perfil_override["corpo_html"]  = corpo_html

    try:
        senha = decrypt(perfil["smtp_senha_enc"])
    except Exception as e:
        return 0, f"Falha ao decifrar senha SMTP: {e}"

    try:
        server = smtplib.SMTP(perfil["smtp_host"], perfil["smtp_port"], timeout=30)
        server.starttls()
        server.login(perfil["email_remetente"], senha)
    except Exception as e:
        return 0, f"Falha SMTP (login): {e}"

    token = secrets.token_urlsafe(16)
    try:
        msg_id = enviar_um_contato(server, perfil_override, contato, token)
        registrar_envio(contato_id, perfil_id, "ok", None, msg_id, token)
    except smtplib.SMTPRecipientsRefused as e:
        msg = str(e)[:500]
        registrar_envio(contato_id, perfil_id, "erro", msg, None, token)
        try: server.quit()
        except Exception: pass
        return _ultimo_envio_id_teste(contato_id, perfil_id), f"Destinatário recusado: {msg}"
    except Exception as e:
        msg = str(e)[:500]
        registrar_envio(contato_id, perfil_id, "erro", msg, None, token)
        try: server.quit()
        except Exception: pass
        return _ultimo_envio_id_teste(contato_id, perfil_id), f"Erro no envio: {msg}"

    try: server.quit()
    except Exception: pass

    return _ultimo_envio_id_teste(contato_id, perfil_id), None


def _ultimo_envio_id_teste(contato_id: int, perfil_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM envios WHERE contato_id = ? AND perfil_remetente_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (contato_id, perfil_id),
        ).fetchone()
    return row["id"] if row else 0
