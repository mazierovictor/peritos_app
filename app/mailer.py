"""
Envio de e-mails: roda em thread daemon, respeita limite diário do perfil,
faz pausa entre envios e grava cada tentativa na tabela `envios`.
"""
from __future__ import annotations

import os
import random
import re
import smtplib
import threading
import time
from datetime import date
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

def _email_valido(email: str) -> bool:
    return bool(email and _EMAIL_RE.match(email.strip()))


# Erros SMTP que indicam que o destinatário não existe (devemos marcar como inválido)
_RE_BOUNCE_PERMANENTE = re.compile(
    r"550|551|553|554|user.*(unknown|not.*exist|not.*found)|mailbox.*(unavailable|not.*found)|no such user|recipient.*rejected",
    re.IGNORECASE,
)

def _eh_bounce_permanente(mensagem_erro: str) -> bool:
    return bool(_RE_BOUNCE_PERMANENTE.search(mensagem_erro))


# Estado global das campanhas em andamento (uma de cada vez por perfil)
_em_andamento: dict[int, "CampanhaEstado"] = {}
_lock = threading.Lock()


class CampanhaEstado:
    def __init__(self, perfil_id: int, total_alvo: int):
        self.perfil_id = perfil_id
        self.total_alvo = total_alvo
        self.enviados = 0
        self.erros = 0
        self.iniciado = False
        self.terminado = False
        self.cancelar = False
        self.mensagem = ""

    def to_dict(self) -> dict:
        return {
            "perfil_id": self.perfil_id,
            "total_alvo": self.total_alvo,
            "enviados": self.enviados,
            "erros": self.erros,
            "iniciado": self.iniciado,
            "terminado": self.terminado,
            "cancelar": self.cancelar,
            "mensagem": self.mensagem,
        }


def estado(perfil_id: int) -> dict | None:
    e = _em_andamento.get(perfil_id)
    return e.to_dict() if e else None


def cancelar(perfil_id: int) -> bool:
    e = _em_andamento.get(perfil_id)
    if e and not e.terminado:
        e.cancelar = True
        return True
    return False


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
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM envios WHERE perfil_remetente_id = ? "
            "AND status = 'ok' AND date(enviado_em) = date('now', 'localtime')",
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


def _registrar_envio(
    contato_id: int, perfil_id: int, status: str,
    erro: str | None, message_id: str | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO envios (contato_id, perfil_remetente_id, status, erro_mensagem, message_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (contato_id, perfil_id, status, erro, message_id),
        )


def _marcar_contato_invalido(contato_id: int) -> None:
    """Marca o contato como inválido para que ele não seja tentado novamente."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE contatos SET invalido = 1, observacao = COALESCE(observacao, '') "
            "|| 'Marcado inválido após bounce permanente; ' WHERE id = ?",
            (contato_id,),
        )


def _carregar_perfil(perfil_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM perfis_remetente WHERE id = ?", (perfil_id,)
        ).fetchone()
    return dict(row) if row else None


def _selecionar_contatos(filtros: dict, limite: int, perfil_id: int) -> list[dict]:
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


def _enviar_um(server: smtplib.SMTP, perfil: dict, contato: dict) -> str:
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
    corpo_html = _aplicar_template(perfil["corpo_html"], contato, perfil)

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


def _loop_envio(estado_obj: CampanhaEstado, filtros: dict) -> None:
    perfil = _carregar_perfil(estado_obj.perfil_id)
    if not perfil:
        estado_obj.mensagem = "Perfil não encontrado."
        estado_obj.terminado = True
        return

    enviados_hoje = _enviados_hoje(perfil["id"])
    restante_hoje = max(0, perfil["limite_diario"] - enviados_hoje)
    if restante_hoje <= 0:
        estado_obj.mensagem = "Limite diário já atingido."
        estado_obj.terminado = True
        return

    quantos = min(restante_hoje, estado_obj.total_alvo)
    contatos = _selecionar_contatos(filtros, quantos, perfil["id"])

    if not contatos:
        estado_obj.mensagem = "Nenhum contato elegível encontrado."
        estado_obj.terminado = True
        return

    try:
        senha = decrypt(perfil["smtp_senha_enc"])
        server = smtplib.SMTP(perfil["smtp_host"], perfil["smtp_port"], timeout=30)
        server.starttls()
        server.login(perfil["email_remetente"], senha)
    except Exception as e:
        estado_obj.mensagem = f"Falha SMTP: {e}"
        estado_obj.terminado = True
        return

    estado_obj.iniciado = True

    try:
        for contato in contatos:
            if estado_obj.cancelar:
                estado_obj.mensagem = "Cancelado pelo usuário."
                break

            # Pular se o email tem formato inválido (evita bounce que prejudica reputação)
            if not _email_valido(contato["email"]):
                _registrar_envio(contato["id"], perfil["id"], "erro", "Email com formato inválido")
                _marcar_contato_invalido(contato["id"])
                estado_obj.erros += 1
                continue

            try:
                msg_id = _enviar_um(server, perfil, contato)
                _registrar_envio(contato["id"], perfil["id"], "ok", None, msg_id)
                estado_obj.enviados += 1
            except smtplib.SMTPRecipientsRefused as e:
                msg_erro = str(e)[:500]
                _registrar_envio(contato["id"], perfil["id"], "erro", msg_erro)
                _marcar_contato_invalido(contato["id"])
                estado_obj.erros += 1
            except Exception as e:
                msg_erro = str(e)[:500]
                _registrar_envio(contato["id"], perfil["id"], "erro", msg_erro)
                if _eh_bounce_permanente(msg_erro):
                    _marcar_contato_invalido(contato["id"])
                estado_obj.erros += 1

            pausa = 10 + random.uniform(0, 5)
            for _ in range(int(pausa * 10)):
                if estado_obj.cancelar:
                    break
                time.sleep(0.1)
        if not estado_obj.mensagem:
            estado_obj.mensagem = "Concluído."
    finally:
        try:
            server.quit()
        except Exception:
            pass
        estado_obj.terminado = True


def disparar(perfil_id: int, total_alvo: int, filtros: dict) -> CampanhaEstado:
    with _lock:
        atual = _em_andamento.get(perfil_id)
        if atual and not atual.terminado:
            return atual
        novo = CampanhaEstado(perfil_id, total_alvo)
        _em_andamento[perfil_id] = novo
    t = threading.Thread(
        target=_loop_envio, args=(novo, filtros), daemon=True,
        name=f"campanha-{perfil_id}",
    )
    t.start()
    return novo
