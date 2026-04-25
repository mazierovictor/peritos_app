"""
Verificação de bounces via IMAP.

Conecta na caixa do `email_remetente` (mesma senha SMTP), busca DSNs
(Delivery Status Notifications) novas, casa cada uma com o `envios.message_id`
gravado no envio, e atualiza o status:

  - hard bounce (5.x.x)  → status='bounce', contato marcado inválido
  - soft bounce (4.x.x)  → status='bounce_soft' (não mexe no contato)

Usa UID incremental (perfis_remetente.imap_ultimo_uid) para não reprocessar.
"""
from __future__ import annotations

import email
import imaplib
import re
import threading
from datetime import datetime, timedelta
from email.message import Message
from email.utils import parsedate_to_datetime

from .crypto import decrypt
from .db import get_conn


_lock = threading.Lock()
_em_andamento: set[int] = set()


def _imap_host_default(smtp_host: str) -> str:
    """Deriva o host IMAP a partir do SMTP. smtp.gmail.com → imap.gmail.com."""
    h = (smtp_host or "").strip().lower()
    if not h:
        return ""
    if h.startswith("smtp."):
        return "imap." + h[len("smtp."):]
    if h.startswith("smtp-mail."):
        return "outlook.office365.com"
    return h


def _resolve_imap(perfil: dict) -> tuple[str, int]:
    host = (perfil.get("imap_host") or "").strip() or _imap_host_default(perfil["smtp_host"])
    port = int(perfil.get("imap_port") or 993)
    return host, port


_RE_STATUS = re.compile(r"^Status:\s*([245]\.\d+\.\d+)", re.IGNORECASE | re.MULTILINE)
_RE_DIAG = re.compile(r"^Diagnostic-Code:\s*(.+?)(?=^\S|\Z)", re.IGNORECASE | re.MULTILINE | re.DOTALL)
_RE_ORIG_MSGID = re.compile(r"^(?:Original-)?Message-ID:\s*(<[^>]+>)", re.IGNORECASE | re.MULTILINE)


def _normalizar_msgid(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    if s.startswith("<") and s.endswith(">"):
        return s
    m = re.search(r"<[^>]+>", s)
    return m.group(0) if m else None


def _extrair_dsn(msg: Message) -> tuple[str | None, str | None, str | None]:
    """
    Retorna (status_code, original_message_id, diagnostic).
    Aceita DSN multipart/report ou bounces "tradicionais" tipo MAILER-DAEMON.
    """
    status = None
    orig_msgid = None
    diag = None

    # 1. multipart/report — RFC 3464
    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()

            if ctype == "message/delivery-status":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    texto = payload.decode("utf-8", errors="replace")
                else:
                    texto = str(payload or "")
                m = _RE_STATUS.search(texto)
                if m and not status:
                    status = m.group(1)
                m = _RE_DIAG.search(texto)
                if m and not diag:
                    diag = " ".join(m.group(1).split())[:500]

            elif ctype in ("message/rfc822", "text/rfc822-headers"):
                payload = part.get_payload()
                if isinstance(payload, list) and payload:
                    inner = payload[0]
                    mid = inner.get("Message-ID") if hasattr(inner, "get") else None
                    if mid and not orig_msgid:
                        orig_msgid = _normalizar_msgid(str(mid))
                else:
                    raw = part.get_payload(decode=True)
                    if isinstance(raw, bytes):
                        texto = raw.decode("utf-8", errors="replace")
                        m = _RE_ORIG_MSGID.search(texto)
                        if m and not orig_msgid:
                            orig_msgid = _normalizar_msgid(m.group(1))

    # 2. fallback — varre o corpo inteiro como texto
    if not orig_msgid or not status:
        try:
            corpo_bytes = msg.as_bytes()
            corpo = corpo_bytes.decode("utf-8", errors="replace")
        except Exception:
            corpo = ""
        if not status:
            m = _RE_STATUS.search(corpo)
            if m:
                status = m.group(1)
        if not orig_msgid:
            m = _RE_ORIG_MSGID.search(corpo)
            if m:
                orig_msgid = _normalizar_msgid(m.group(1))
        if not diag:
            m = _RE_DIAG.search(corpo)
            if m:
                diag = " ".join(m.group(1).split())[:500]

    return status, orig_msgid, diag


def _bounce_em(msg: Message) -> str | None:
    """Retorna a data do bounce normalizada em UTC (formato SQLite)."""
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        from datetime import timezone
        dt = parsedate_to_datetime(str(raw))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _eh_dsn(msg: Message) -> bool:
    ctype = (msg.get_content_type() or "").lower()
    if ctype == "multipart/report":
        return True
    # msg.get() pode retornar Header object para campos MIME-encoded (ex: =?UTF-8?B?...?=).
    # str() funciona pra Header e pra string normal.
    sender = str(msg.get("From") or "").lower()
    if "mailer-daemon" in sender or "postmaster" in sender:
        return True
    subject = str(msg.get("Subject") or "").lower()
    return any(t in subject for t in ("undeliver", "delivery status", "delivery failure", "returned mail", "não entregue"))


def _atualizar_envio(
    message_id: str, status_code: str | None, diag: str | None, bounce_em: str | None
) -> tuple[bool, int | None]:
    """
    Casa o bounce com um envio pelo message_id.
    Retorna (encontrou, contato_id_se_hard_bounce).
    """
    eh_hard = bool(status_code and status_code.startswith("5"))
    novo_status = "bounce" if eh_hard else "bounce_soft"

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, contato_id FROM envios WHERE message_id = ? AND status = 'ok' LIMIT 1",
            (message_id,),
        ).fetchone()
        if not row:
            return False, None
        conn.execute(
            "UPDATE envios SET status = ?, bounce_em = COALESCE(?, CURRENT_TIMESTAMP), "
            "bounce_codigo = ?, bounce_diagnostico = ? WHERE id = ?",
            (novo_status, bounce_em, status_code, diag, row["id"]),
        )
        if eh_hard:
            conn.execute(
                "UPDATE contatos SET invalido = 1, observacao = COALESCE(observacao, '') "
                "|| 'Bounce ' || COALESCE(?, '5.x.x') || '; ' WHERE id = ?",
                (status_code, row["contato_id"]),
            )
            return True, row["contato_id"]
        return True, None


def _carregar_perfil(perfil_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM perfis_remetente WHERE id = ?", (perfil_id,)
        ).fetchone()
    return dict(row) if row else None


def _atualizar_ultimo_uid(perfil_id: int, uid: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE perfis_remetente SET imap_ultimo_uid = ? WHERE id = ?",
            (uid, perfil_id),
        )


def _registrar_run_inicio(perfil_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO bounce_runs (perfil_id, status) VALUES (?, 'rodando')",
            (perfil_id,),
        )
        return cur.lastrowid


def _registrar_run_fim(run_id: int, status: str, bounces: int, lidas: int, erro: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE bounce_runs SET finalizado_em = CURRENT_TIMESTAMP, status = ?, "
            "bounces_novos = ?, mensagens_lidas = ?, erro = ? WHERE id = ?",
            (status, bounces, lidas, erro, run_id),
        )


def verificar(perfil_id: int) -> dict:
    """
    Verifica DSNs novas para um perfil. Retorna {status, bounces, lidas, erro}.
    Trava por perfil para não rodar concorrente.
    """
    with _lock:
        if perfil_id in _em_andamento:
            return {"status": "ja_rodando", "bounces": 0, "lidas": 0, "erro": None}
        _em_andamento.add(perfil_id)

    run_id = _registrar_run_inicio(perfil_id)
    try:
        return _verificar_interno(perfil_id, run_id)
    finally:
        with _lock:
            _em_andamento.discard(perfil_id)


def _verificar_interno(perfil_id: int, run_id: int) -> dict:
    perfil = _carregar_perfil(perfil_id)
    if not perfil:
        _registrar_run_fim(run_id, "erro", 0, 0, "perfil não encontrado")
        return {"status": "erro", "bounces": 0, "lidas": 0, "erro": "perfil não encontrado"}

    if not perfil.get("imap_ativo"):
        _registrar_run_fim(run_id, "ignorado", 0, 0, "imap desativado")
        return {"status": "ignorado", "bounces": 0, "lidas": 0, "erro": "imap desativado"}

    host, port = _resolve_imap(perfil)
    if not host:
        _registrar_run_fim(run_id, "erro", 0, 0, "host imap não definido")
        return {"status": "erro", "bounces": 0, "lidas": 0, "erro": "host imap não definido"}

    try:
        senha = decrypt(perfil["smtp_senha_enc"])
    except Exception as e:
        _registrar_run_fim(run_id, "erro", 0, 0, f"senha: {e}")
        return {"status": "erro", "bounces": 0, "lidas": 0, "erro": f"senha: {e}"}

    bounces = 0
    lidas = 0
    ultimo_uid = int(perfil.get("imap_ultimo_uid") or 0)
    maior_uid_visto = ultimo_uid

    try:
        M = imaplib.IMAP4_SSL(host, port, timeout=30)
        try:
            M.login(perfil["email_remetente"], senha)
        except imaplib.IMAP4.error as e:
            _registrar_run_fim(run_id, "erro", 0, 0, f"login imap: {e}")
            return {"status": "erro", "bounces": 0, "lidas": 0, "erro": f"login imap: {e}"}

        try:
            M.select("INBOX")

            if ultimo_uid > 0:
                typ, data = M.uid("SEARCH", None, f"UID {ultimo_uid + 1}:*")
            else:
                # Primeira execução: limita aos últimos 30 dias para não varrer caixa antiga
                desde = (datetime.utcnow() - timedelta(days=30)).strftime("%d-%b-%Y")
                typ, data = M.uid("SEARCH", None, "SINCE", desde)
            if typ != "OK" or not data or not data[0]:
                return {"status": "ok", "bounces": 0, "lidas": 0, "erro": None}

            uids = data[0].split()
            for raw_uid in uids:
                try:
                    uid = int(raw_uid)
                except ValueError:
                    continue
                if uid <= ultimo_uid:
                    continue
                maior_uid_visto = max(maior_uid_visto, uid)

                typ, msg_data = M.uid("FETCH", str(uid), "(RFC822)")
                if typ != "OK" or not msg_data:
                    continue
                raw = None
                for item in msg_data:
                    if isinstance(item, tuple) and len(item) >= 2:
                        raw = item[1]
                        break
                if not raw:
                    continue

                lidas += 1
                msg = email.message_from_bytes(raw)
                if not _eh_dsn(msg):
                    continue

                status_code, orig_msgid, diag = _extrair_dsn(msg)
                if not orig_msgid:
                    continue

                encontrou, _ = _atualizar_envio(
                    orig_msgid, status_code, diag, _bounce_em(msg)
                )
                if encontrou:
                    bounces += 1
                    try:
                        M.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
                    except Exception:
                        pass
        finally:
            try:
                M.logout()
            except Exception:
                pass
    except (imaplib.IMAP4.error, OSError) as e:
        _registrar_run_fim(run_id, "erro", bounces, lidas, str(e)[:500])
        return {"status": "erro", "bounces": bounces, "lidas": lidas, "erro": str(e)}

    if maior_uid_visto > ultimo_uid:
        _atualizar_ultimo_uid(perfil_id, maior_uid_visto)

    _registrar_run_fim(run_id, "ok", bounces, lidas, None)
    return {"status": "ok", "bounces": bounces, "lidas": lidas, "erro": None}


def verificar_todos() -> list[dict]:
    """Roda verificar() em todos os perfis com imap_ativo=1. Usado pelo scheduler."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM perfis_remetente WHERE imap_ativo = 1"
        ).fetchall()
    resultados = []
    for r in rows:
        try:
            res = verificar(r["id"])
        except Exception as e:
            res = {"status": "erro", "bounces": 0, "lidas": 0, "erro": str(e)}
        resultados.append({"perfil_id": r["id"], **res})
    return resultados


def ultima_run(perfil_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM bounce_runs WHERE perfil_id = ? ORDER BY id DESC LIMIT 1",
            (perfil_id,),
        ).fetchone()
    return dict(row) if row else None
