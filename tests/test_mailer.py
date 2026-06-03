"""
Regressão do cabeçalho From.

O endereço do remetente precisa aparecer em texto plano no header From mesmo
quando o nome tem acentos. Antes, uma f-string crua fazia o nome ("Perícias")
codificar o cabeçalho inteiro como um único encoded-word RFC 2047, escondendo o
<endereço>; o Gmail então rejeitava com 550-5.7.1 "missing a valid address in
From: header" (RFC 5322).
"""
from __future__ import annotations

from email import message_from_string
from email.header import decode_header, make_header
from email.utils import parseaddr

from app.mailer import enviar_um_contato


class _FakeSMTP:
    """Captura a mensagem em vez de enviá-la pela rede."""

    def __init__(self):
        self.enviados: list[tuple] = []

    def sendmail(self, from_addr, to_addrs, msg):
        self.enviados.append((from_addr, to_addrs, msg))


def _perfil(nome: str) -> dict:
    return {
        "nome": nome,
        "email_remetente": "victor@mspericias.com",
        "assunto": "Assunto",
        "corpo_texto": "Olá",
        "corpo_html": "<p>Olá</p>",
    }


def test_from_header_mantem_endereco_com_nome_acentuado():
    server = _FakeSMTP()
    perfil = _perfil("Victor Maziero | M&S Perícias")

    enviar_um_contato(server, perfil, {"email": "destino@ex.com"}, "tok")

    raw = server.enviados[0][2]
    msg = message_from_string(raw)

    nome, endereco = parseaddr(msg["From"])
    assert endereco == "victor@mspericias.com"
    assert str(make_header(decode_header(nome))) == "Victor Maziero | M&S Perícias"

    # Reply-To reaproveita o mesmo remetente e também depende do endereço válido.
    assert parseaddr(msg["Reply-To"])[1] == "victor@mspericias.com"
