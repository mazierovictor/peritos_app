# Campanhas Persistentes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Substituir o modelo atual "campanha = execução única + agendamento separado" por um modelo de campanha persistente com `total_alvo`, `por_dia`, `dias_semana`, janela horária, status e progresso visível.

**Architecture:** Nova tabela `campanhas` é a fonte da verdade do estado persistente. Um worker daemon thread por campanha ativa, criado via `iniciar/retomar` e reidratado no startup do FastAPI. Helpers puros em `app/campanhas.py` decidem "próxima ação agora" a partir de `(campanha, agora)` para facilitar teste. Mailer continua focado em "como mandar um e-mail"; orquestração vive em `campanhas.py`. Tipo `campanha` deixa de existir em `/agendamentos`.

**Tech Stack:** Python 3.12, FastAPI, SQLite, APScheduler (existente, sem mudança aqui), Jinja2 + HTMX para UI, pytest para testes.

**Spec:** `docs/superpowers/specs/2026-04-28-campanhas-persistentes-design.md`

---

## File Structure

**Criar:**
- `app/campanhas.py` — funções puras (cálculo de quota/intervalo/próximo dia, classificação de erro), CRUD da tabela `campanhas`, transições de estado, `loop_campanha`, `reidratar`. Aproximadamente 400 linhas; mantém o orquestrador isolado do mailer.
- `app/templates/campanha_form.html` — formulário criar/editar campanha
- `app/templates/campanha_detalhe.html` — página de transparência (header + 6 blocos)
- `app/templates/_campanha_detalhe_corpo.html` — fragmento HTMX para auto-refresh de 5s
- `tests/__init__.py`, `tests/conftest.py`, `tests/test_campanhas.py` — pytest com fixtures
- `pytest.ini` — config mínima

**Modificar:**
- `app/db.py` — `SCHEMA` ganha `campanhas` + índice parcial único; `_migrar` ganha `ALTER TABLE envios ADD COLUMN campanha_id` + índice + `DELETE FROM agendamentos WHERE tipo='campanha'`
- `app/mailer.py` — extrair `_construir_mensagem`/`_enviar_um` para serem chamáveis a partir de `campanhas.py`; remover `disparar()` antigo, `CampanhaEstado`, `_em_andamento`, `_loop_envio`, `cancelar`, `estado` (tudo que era do modelo one-shot)
- `app/scheduler.py` — remover branch `tipo == "campanha"` em `_executar_job`
- `app/main.py` —
  - Remover rotas: `POST /campanhas/disparar`, `GET /campanhas/acompanhar/{perfil_id}`, `GET /campanhas/estado/{perfil_id}`, `POST /campanhas/cancelar/{perfil_id}`
  - Reescrever `GET /campanhas` para nova lista
  - Adicionar: `GET /campanhas/nova`, `POST /campanhas/nova`, `GET /campanhas/{id}`, `GET /campanhas/{id}/parcial`, `POST /campanhas/{id}/iniciar`, `POST /campanhas/{id}/pausar`, `POST /campanhas/{id}/retomar`, `POST /campanhas/{id}/cancelar`, `POST /campanhas/{id}/editar`
  - Remover branch `tipo == "campanha"` em `_o_que`, `_quando`, `agendamentos_novo_submit`, `_ctx_agendamento_form`
  - No startup do FastAPI, chamar `campanhas.reidratar()` após `scheduler.iniciar()`
  - Adicionar filtro `campanha_id` em `GET /historico`
- `app/templates/agendamento_form.html` — remover bloco `bloco_campanha` e o JS associado; remover opção `campanha` do select
- `app/templates/campanhas.html` — sobrescrever (vira a nova lista)
- `app/templates/historico.html` — adicionar filtro de campanha
- `requirements.txt` — adicionar `pytest>=8` (e `freezegun>=1.5` para testar lógica que depende de "agora")

**Deletar:**
- `app/templates/campanha_acompanhar.html`
- `app/templates/_campanha_estado.html`

---

## Test Strategy

Onde temos lógica determinística pura (parsing, decisão de próxima ação dado um `now`, classificação de erro, cálculo de quota/intervalo, transições válidas/inválidas), usamos **pytest com TDD** — escreve teste falhando, implementa mínimo, passa.

Onde envolve threading real / SMTP / FastAPI / HTMX (loop da thread, conexão SMTP, render de templates), validamos **manualmente** rodando `uvicorn app.main:app --reload` e exercitando o fluxo no navegador. Isso é explícito em cada task que cai nessa categoria.

Não vamos refatorar todo o mailer.py existente para "mais testável" — o objetivo é entregar a feature, não rescrever a base.

---

## Task 1 — Setup pytest e fixture de DB

**Files:**
- Modify: `requirements.txt`
- Create: `pytest.ini`, `tests/__init__.py`, `tests/conftest.py`, `tests/test_smoke.py`

- [ ] **Step 1: Adicionar pytest e freezegun a `requirements.txt`**

Acrescentar ao final:

```
pytest>=8.3.0
freezegun>=1.5.0
```

- [ ] **Step 2: Instalar localmente**

Run: `pip install pytest freezegun`

- [ ] **Step 3: Criar `pytest.ini`**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -ra -q
```

- [ ] **Step 4: Criar `tests/__init__.py` (vazio)**

```python
```

- [ ] **Step 5: Criar `tests/conftest.py`**

```python
"""
Fixtures compartilhadas. Cada teste roda contra um SQLite temporário
isolado, com schema criado do zero, e aponta o mailer/campanhas para esse banco.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def db_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    Cria um diretório de dados temporário, aponta `settings.data_dir`
    para ele e inicializa o schema. Retorna o Path do diretório.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))

    # Importa db DEPOIS do monkeypatch para que get_conn use o novo path.
    from app import db
    db.init()
    return tmp_path


@pytest.fixture
def perfil_id(db_temp):
    """Insere um perfil de remetente válido e retorna o id."""
    from app.db import get_conn

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO perfis_remetente "
            "(usuario_id, nome, email_remetente, smtp_host, smtp_port, "
            " smtp_senha_enc, assunto, corpo_texto, corpo_html, limite_diario) "
            "VALUES (1, 'Teste', 'teste@ex.com', 'smtp.ex.com', 587, "
            " 'enc', 'assunto', 'txt', '<p>html</p>', 250)",
        )
        return cur.lastrowid
```

- [ ] **Step 6: Criar `tests/test_smoke.py` com um teste do schema**

```python
def test_schema_cria_tabela_campanhas(db_temp):
    from app.db import get_conn
    with get_conn() as conn:
        nomes = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "campanhas" in nomes  # vai falhar até a Task 2
```

- [ ] **Step 7: Rodar e ver falhar**

Run: `pytest tests/test_smoke.py -v`
Expected: FAIL — `assert 'campanhas' in {...}` (tabela ainda não existe)

- [ ] **Step 8: Verificar que o `db.py` expõe `init`**

Run: `grep -n "^def init" app/db.py`
Esperado: existe uma função `init()` que cria o schema. Se não existir com esse nome, ajustar o `conftest.py` para chamar a função correta (provavelmente `inicializar()` ou `criar_tabelas()`).

- [ ] **Step 9: Commit**

```bash
git add requirements.txt pytest.ini tests/
git commit -m "chore: setup pytest com fixtures de db temporário"
```

---

## Task 2 — Schema da tabela `campanhas` + alterações em `envios` e `agendamentos`

**Files:**
- Modify: `app/db.py:14-133` (constante `SCHEMA` e função `_migrar`)
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Adicionar mais asserts ao smoke test**

Substituir `tests/test_smoke.py`:

```python
def test_schema_cria_tabela_campanhas(db_temp):
    from app.db import get_conn
    with get_conn() as conn:
        nomes = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        cols_envios = {r["name"] for r in conn.execute("PRAGMA table_info(envios)")}
        cols_camp = {r["name"] for r in conn.execute("PRAGMA table_info(campanhas)")}
        indices = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}

    assert "campanhas" in nomes
    assert "campanha_id" in cols_envios
    esperadas = {
        "id", "nome", "perfil_id", "filtro_estado", "filtro_tribunal",
        "total_alvo", "por_dia", "dias_semana",
        "janela_inicio", "janela_fim",
        "status", "pausa_motivo",
        "enviados_total",
        "criada_em", "iniciada_em", "concluida_em",
    }
    assert esperadas.issubset(cols_camp)
    assert "idx_campanhas_perfil_unica" in indices


def test_unicidade_campanha_ativa_por_perfil(db_temp, perfil_id):
    """Não pode haver duas campanhas em status ativa/pausada para o mesmo perfil."""
    import sqlite3
    from app.db import get_conn

    sql = (
        "INSERT INTO campanhas "
        "(nome, perfil_id, total_alvo, por_dia, dias_semana, "
        " janela_inicio, janela_fim, status) "
        "VALUES (?, ?, 100, 50, '0,1,2,3,4', '09:00', '17:00', 'ativa')"
    )
    with get_conn() as conn:
        conn.execute(sql, ("c1", perfil_id))

    with get_conn() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, ("c2", perfil_id))


def test_legacy_agendamento_campanha_e_deletado(db_temp):
    """Migração apaga linhas antigas de agendamento tipo='campanha'."""
    from app.db import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agendamentos (nome, tipo, alvo, cron) "
            "VALUES ('legacy', 'campanha', '', '')"
        )
    # roda migrar de novo simulando reboot
    from app import db as dbmod
    dbmod._migrar()
    with get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM agendamentos WHERE tipo='campanha'"
        ).fetchone()["c"]
    assert n == 0
```

Adicionar `import pytest` no topo.

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_smoke.py -v`
Expected: 3 FAILs (campanhas não existe, campanha_id não existe, etc.)

- [ ] **Step 3: Adicionar tabela `campanhas` à constante `SCHEMA` em `app/db.py`**

Inserir antes do fechamento da string `SCHEMA`, depois do `CREATE INDEX idx_cron_runs_iniciado`:

```sql
CREATE TABLE IF NOT EXISTS campanhas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nome            TEXT NOT NULL,
    perfil_id       INTEGER NOT NULL REFERENCES perfis_remetente(id) ON DELETE RESTRICT,
    filtro_estado   TEXT,
    filtro_tribunal TEXT,
    total_alvo      INTEGER NOT NULL,
    por_dia         INTEGER NOT NULL,
    dias_semana     TEXT NOT NULL,
    janela_inicio   TEXT NOT NULL,
    janela_fim      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'rascunho',
    pausa_motivo    TEXT,
    enviados_total  INTEGER NOT NULL DEFAULT 0,
    criada_em       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    iniciada_em     TIMESTAMP,
    concluida_em    TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_campanhas_perfil_unica
ON campanhas(perfil_id) WHERE status IN ('ativa', 'pausada');
```

- [ ] **Step 4: Adicionar migrações em `_migrar()`**

Localizar `_migrar` em `app/db.py` (~linha 136). Após o bloco de `cols_envios` que adiciona `message_id`, `bounce_em` etc., adicionar:

```python
        # Coluna campanha_id em envios (se ainda não existe)
        if "campanha_id" not in cols_envios:
            conn.execute("ALTER TABLE envios ADD COLUMN campanha_id INTEGER")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_envios_campanha "
            "ON envios(campanha_id)"
        )

        # Limpa agendamentos legados do tipo campanha (substituídos pela tabela campanhas)
        conn.execute("DELETE FROM agendamentos WHERE tipo = 'campanha'")
```

> Cuidado: o `cols_envios` é lido antes — se essa migração rodar uma segunda vez, o set `cols_envios` ainda é a foto da primeira leitura. Para esta nova coluna, é seguro porque `ALTER TABLE` está dentro de `if`. Em runs subsequentes, `cols_envios` vai ter `campanha_id` no banco mas o `if` ainda usa a leitura inicial. Para robustez, releia: substituir o `if "campanha_id" not in cols_envios` por:
>
> ```python
>         cols_envios_atual = {r["name"] for r in conn.execute("PRAGMA table_info(envios)")}
>         if "campanha_id" not in cols_envios_atual:
>             conn.execute("ALTER TABLE envios ADD COLUMN campanha_id INTEGER")
> ```

Use a versão robusta.

- [ ] **Step 5: Rodar testes**

Run: `pytest tests/test_smoke.py -v`
Expected: 3 PASS

- [ ] **Step 6: Commit**

```bash
git add app/db.py tests/test_smoke.py
git commit -m "feat(db): tabela campanhas + envios.campanha_id + cleanup de agendamentos legados"
```

---

## Task 3 — Helpers puros: parse/format de `dias_semana`

**Files:**
- Create: `app/campanhas.py` (novo módulo, vai ser preenchido nas próximas tasks)
- Test: `tests/test_campanhas.py`

- [ ] **Step 1: Criar esqueleto de `app/campanhas.py`**

```python
"""
Orquestração de campanhas persistentes: CRUD, transições de estado,
loop do worker (daemon thread por campanha ativa), reidratação no boot.

A tabela `campanhas` (em db.py) é a fonte da verdade do estado persistente.
O estado runtime das threads vivas fica em `_threads_runtime` (memória).
"""
from __future__ import annotations
```

- [ ] **Step 2: Criar `tests/test_campanhas.py` com testes para parse_dias_semana / format_dias_semana**

```python
import pytest
from app.campanhas import parse_dias_semana, format_dias_semana


def test_parse_dias_semana_basico():
    assert parse_dias_semana("0,1,2,3,4") == {0, 1, 2, 3, 4}


def test_parse_dias_semana_vazio_e_espacos():
    assert parse_dias_semana("") == set()
    assert parse_dias_semana("0, 1 ,  6") == {0, 1, 6}


def test_parse_dias_semana_invalidos_levanta():
    with pytest.raises(ValueError):
        parse_dias_semana("7")
    with pytest.raises(ValueError):
        parse_dias_semana("a,b")
    with pytest.raises(ValueError):
        parse_dias_semana("-1")


def test_format_dias_semana_ordena_e_dedup():
    assert format_dias_semana({4, 0, 1, 1, 2, 3}) == "0,1,2,3,4"
    assert format_dias_semana(set()) == ""
```

- [ ] **Step 3: Rodar e ver falhar**

Run: `pytest tests/test_campanhas.py -v`
Expected: FAIL com `ImportError: cannot import name 'parse_dias_semana'`

- [ ] **Step 4: Implementar em `app/campanhas.py`**

```python
def parse_dias_semana(s: str) -> set[int]:
    """Converte CSV '0,1,2' em set {0,1,2}. 0=segunda, 6=domingo."""
    if not s.strip():
        return set()
    out: set[int] = set()
    for tok in s.split(","):
        tok = tok.strip()
        v = int(tok)  # ValueError se não-inteiro
        if v < 0 or v > 6:
            raise ValueError(f"Dia da semana fora do intervalo 0-6: {v}")
        out.add(v)
    return out


def format_dias_semana(dias: set[int]) -> str:
    return ",".join(str(d) for d in sorted(dias))
```

- [ ] **Step 5: Rodar e ver passar**

Run: `pytest tests/test_campanhas.py -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add app/campanhas.py tests/test_campanhas.py
git commit -m "feat(campanhas): helpers parse/format de dias_semana"
```

---

## Task 4 — Helper puro: `classificar_erro_smtp`

**Files:**
- Modify: `app/campanhas.py`
- Test: `tests/test_campanhas.py`

- [ ] **Step 1: Adicionar testes**

Acrescentar a `tests/test_campanhas.py`:

```python
import smtplib
from app.campanhas import classificar_erro_smtp, ErroSmtp


def test_classificar_auth_e_fatal():
    e = smtplib.SMTPAuthenticationError(535, b"5.7.8 username and password not accepted")
    assert classificar_erro_smtp(e) is ErroSmtp.FATAL


def test_classificar_535_em_outra_excecao_e_fatal():
    e = smtplib.SMTPResponseException(535, "Authentication credentials invalid")
    assert classificar_erro_smtp(e) is ErroSmtp.FATAL


def test_classificar_disconnect_e_transiente():
    assert classificar_erro_smtp(smtplib.SMTPServerDisconnected("eof")) is ErroSmtp.TRANSIENTE


def test_classificar_connect_error_e_transiente():
    assert classificar_erro_smtp(smtplib.SMTPConnectError(421, "service unavailable")) is ErroSmtp.TRANSIENTE


def test_classificar_recipient_refused_e_por_contato():
    e = smtplib.SMTPRecipientsRefused({"x@y.com": (550, b"no such user")})
    assert classificar_erro_smtp(e) is ErroSmtp.POR_CONTATO


def test_classificar_timeout_e_transiente():
    assert classificar_erro_smtp(TimeoutError("timed out")) is ErroSmtp.TRANSIENTE


def test_classificar_desconhecido_e_por_contato():
    assert classificar_erro_smtp(RuntimeError("???")) is ErroSmtp.POR_CONTATO
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_campanhas.py -v`
Expected: 7 FAIL (ErroSmtp/classificar_erro_smtp não definidos)

- [ ] **Step 3: Implementar em `app/campanhas.py`**

Adicionar imports no topo (logo após o docstring) e a função:

```python
import enum
import re
import smtplib


class ErroSmtp(enum.Enum):
    FATAL = "fatal"             # auth falhou ou conta suspensa — pausa imediata
    TRANSIENTE = "transiente"   # rede/timeout — retry com backoff
    POR_CONTATO = "por_contato" # destinatário inválido — segue, sem pausa


_FATAL_PATTERNS = re.compile(
    r"535|530|account.*disabled|invalid.*credentials|authentication.*failed",
    re.IGNORECASE,
)


def classificar_erro_smtp(exc: BaseException) -> ErroSmtp:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ErroSmtp.FATAL
    if isinstance(exc, smtplib.SMTPResponseException):
        msg = f"{exc.smtp_code} {exc.smtp_error!s}"
        if _FATAL_PATTERNS.search(msg):
            return ErroSmtp.FATAL
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return ErroSmtp.POR_CONTATO
    if isinstance(exc, (
        smtplib.SMTPServerDisconnected,
        smtplib.SMTPConnectError,
        TimeoutError,
        ConnectionError,
    )):
        return ErroSmtp.TRANSIENTE
    if isinstance(exc, smtplib.SMTPException):
        return ErroSmtp.POR_CONTATO  # fallback razoável p/ outros SMTPxxx
    return ErroSmtp.POR_CONTATO
```

- [ ] **Step 4: Rodar e ver passar**

Run: `pytest tests/test_campanhas.py -v`
Expected: PASS (todos)

- [ ] **Step 5: Commit**

```bash
git add app/campanhas.py tests/test_campanhas.py
git commit -m "feat(campanhas): classificar_erro_smtp (fatal | transiente | por_contato)"
```

---

## Task 5 — Helper puro: `proxima_acao` (decisão "o que fazer agora?")

Esta é a função-chave da tasca 11 (loop). Aqui é só a decisão pura, dada a campanha e um `now`. Sem efeito colateral.

**Files:**
- Modify: `app/campanhas.py`
- Test: `tests/test_campanhas.py`

- [ ] **Step 1: Adicionar testes**

```python
from datetime import datetime, time
from app.campanhas import (
    Acao, AcaoTipo, proxima_acao, EstadoCampanha,
)


def _ec(**kw):
    """Helper para construir EstadoCampanha com defaults razoáveis."""
    base = dict(
        id=1,
        status="ativa",
        total_alvo=1000,
        por_dia=200,
        enviados_total=300,
        enviados_hoje=50,
        enviados_hoje_perfil=50,
        perfil_limite_diario=250,
        dias_semana={0, 1, 2, 3, 4},     # seg-sex
        janela_inicio=time(9, 0),
        janela_fim=time(17, 0),
    )
    base.update(kw)
    return EstadoCampanha(**base)


def test_proxima_acao_dentro_da_janela_em_dia_valido_dispara_envio():
    now = datetime(2026, 4, 28, 14, 0)  # terça (weekday=1), 14:00
    a = proxima_acao(_ec(), now)
    assert a.tipo is AcaoTipo.ENVIAR


def test_proxima_acao_antes_da_janela_dorme_ate_inicio():
    now = datetime(2026, 4, 28, 8, 30)  # terça, 8:30
    a = proxima_acao(_ec(), now)
    assert a.tipo is AcaoTipo.DORMIR_ATE
    assert a.dormir_ate == datetime(2026, 4, 28, 9, 0)


def test_proxima_acao_depois_da_janela_dorme_ate_proximo_dia_valido():
    now = datetime(2026, 4, 28, 17, 30)  # terça, 17:30
    a = proxima_acao(_ec(), now)
    assert a.tipo is AcaoTipo.DORMIR_ATE
    assert a.dormir_ate == datetime(2026, 4, 29, 9, 0)  # quarta 9h


def test_proxima_acao_em_fim_de_semana_pula_para_segunda():
    now = datetime(2026, 5, 2, 10, 0)  # sábado (5), 10:00
    a = proxima_acao(_ec(), now)
    assert a.tipo is AcaoTipo.DORMIR_ATE
    assert a.dormir_ate == datetime(2026, 5, 4, 9, 0)  # segunda 9h


def test_proxima_acao_quota_diaria_atingida_pula_dia():
    now = datetime(2026, 4, 28, 14, 0)
    a = proxima_acao(_ec(enviados_hoje=200), now)  # bateu por_dia
    assert a.tipo is AcaoTipo.DORMIR_ATE
    assert a.dormir_ate == datetime(2026, 4, 29, 9, 0)


def test_proxima_acao_quota_perfil_atingida_pula_dia():
    now = datetime(2026, 4, 28, 14, 0)
    # perfil já enviou 250 (limite); campanha ainda quer mandar
    a = proxima_acao(_ec(enviados_hoje_perfil=250), now)
    assert a.tipo is AcaoTipo.DORMIR_ATE
    assert a.dormir_ate == datetime(2026, 4, 29, 9, 0)


def test_proxima_acao_total_alvo_atingido_conclui():
    now = datetime(2026, 4, 28, 14, 0)
    a = proxima_acao(_ec(enviados_total=1000), now)
    assert a.tipo is AcaoTipo.CONCLUIR


def test_proxima_acao_status_diferente_de_ativa_sai():
    now = datetime(2026, 4, 28, 14, 0)
    a = proxima_acao(_ec(status="pausada"), now)
    assert a.tipo is AcaoTipo.SAIR
    a = proxima_acao(_ec(status="cancelada"), now)
    assert a.tipo is AcaoTipo.SAIR


def test_intervalo_calculado_pela_janela_e_quota_restante():
    """Quanto mais perto do fim da janela, menor o intervalo."""
    now = datetime(2026, 4, 28, 9, 0)  # início da janela, 8h restantes
    a = proxima_acao(_ec(enviados_hoje=0, por_dia=200), now)
    assert a.tipo is AcaoTipo.ENVIAR
    # 8h * 3600 / 200 = 144s por envio
    assert 130 <= a.intervalo_seg <= 160

    now2 = datetime(2026, 4, 28, 16, 30)  # 30min restantes
    a2 = proxima_acao(_ec(enviados_hoje=190, por_dia=200), now2)
    assert a2.tipo is AcaoTipo.ENVIAR
    # 30min * 60 / 10 = 180s
    assert 170 <= a2.intervalo_seg <= 200


def test_intervalo_minimo_10s():
    """Mesmo com janela apertada, não envia mais rápido que a cada 10s."""
    now = datetime(2026, 4, 28, 16, 59, 50)  # 10s da janela, mas 50 enviar
    a = proxima_acao(_ec(enviados_hoje=150, por_dia=200), now)
    assert a.tipo is AcaoTipo.ENVIAR
    assert a.intervalo_seg >= 10
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_campanhas.py -v`
Expected: 10 FAIL

- [ ] **Step 3: Implementar em `app/campanhas.py`**

Adicionar:

```python
from dataclasses import dataclass
from datetime import datetime, time, timedelta


@dataclass
class EstadoCampanha:
    """Snapshot do estado de uma campanha + counters do dia para decidir a próxima ação."""
    id: int
    status: str
    total_alvo: int
    por_dia: int
    enviados_total: int
    enviados_hoje: int           # da campanha, hoje
    enviados_hoje_perfil: int    # do perfil, hoje (todos os envios não-teste)
    perfil_limite_diario: int
    dias_semana: set[int]
    janela_inicio: time
    janela_fim: time


class AcaoTipo(enum.Enum):
    ENVIAR = "enviar"
    DORMIR_ATE = "dormir_ate"
    CONCLUIR = "concluir"
    SAIR = "sair"


@dataclass
class Acao:
    tipo: AcaoTipo
    dormir_ate: datetime | None = None     # válido para DORMIR_ATE
    intervalo_seg: float | None = None     # válido para ENVIAR


def _proximo_dia_valido(now: datetime, dias_semana: set[int],
                        janela_inicio: time) -> datetime:
    """Próximo datetime em que estamos em um dia da semana permitido, na hora de início da janela."""
    candidato = (now + timedelta(days=1)).replace(
        hour=janela_inicio.hour, minute=janela_inicio.minute,
        second=0, microsecond=0,
    )
    for _ in range(8):  # no pior caso, 7 dias até achar
        if candidato.weekday() in dias_semana:
            return candidato
        candidato += timedelta(days=1)
    return candidato  # fallback (não deve ocorrer se dias_semana não for vazio)


def proxima_acao(c: EstadoCampanha, now: datetime) -> Acao:
    if c.status != "ativa":
        return Acao(AcaoTipo.SAIR)
    if c.enviados_total >= c.total_alvo:
        return Acao(AcaoTipo.CONCLUIR)
    if not c.dias_semana:
        return Acao(AcaoTipo.SAIR)  # campanha mal configurada

    if now.weekday() not in c.dias_semana:
        return Acao(AcaoTipo.DORMIR_ATE,
                    dormir_ate=_proximo_dia_valido(now, c.dias_semana, c.janela_inicio))

    inicio_dt = now.replace(hour=c.janela_inicio.hour, minute=c.janela_inicio.minute,
                            second=0, microsecond=0)
    fim_dt = now.replace(hour=c.janela_fim.hour, minute=c.janela_fim.minute,
                         second=0, microsecond=0)

    if now < inicio_dt:
        return Acao(AcaoTipo.DORMIR_ATE, dormir_ate=inicio_dt)
    if now >= fim_dt:
        return Acao(AcaoTipo.DORMIR_ATE,
                    dormir_ate=_proximo_dia_valido(now, c.dias_semana, c.janela_inicio))

    quota = min(
        c.por_dia - c.enviados_hoje,
        c.perfil_limite_diario - c.enviados_hoje_perfil,
        c.total_alvo - c.enviados_total,
    )
    if quota <= 0:
        return Acao(AcaoTipo.DORMIR_ATE,
                    dormir_ate=_proximo_dia_valido(now, c.dias_semana, c.janela_inicio))

    seg_ate_fim = (fim_dt - now).total_seconds()
    intervalo = max(10.0, seg_ate_fim / quota)
    return Acao(AcaoTipo.ENVIAR, intervalo_seg=intervalo)
```

- [ ] **Step 4: Rodar e ver passar**

Run: `pytest tests/test_campanhas.py -v`
Expected: todos PASS

- [ ] **Step 5: Commit**

```bash
git add app/campanhas.py tests/test_campanhas.py
git commit -m "feat(campanhas): proxima_acao decide enviar/dormir/concluir/sair"
```

---

## Task 6 — CRUD: criar, listar, obter

**Files:**
- Modify: `app/campanhas.py`
- Test: `tests/test_campanhas.py`

- [ ] **Step 1: Adicionar testes**

```python
from datetime import time
from app.campanhas import criar, listar, obter


def test_criar_campanha_minima(db_temp, perfil_id):
    cid = criar(
        nome="TJSP janeiro",
        perfil_id=perfil_id,
        filtros={"estado": "SP", "tribunal": "tjsp"},
        total_alvo=1000,
        por_dia=200,
        dias_semana={0, 1, 2, 3, 4},
        janela_inicio=time(9, 0),
        janela_fim=time(17, 0),
    )
    assert cid > 0
    c = obter(cid)
    assert c["nome"] == "TJSP janeiro"
    assert c["status"] == "rascunho"
    assert c["total_alvo"] == 1000
    assert c["por_dia"] == 200
    assert c["dias_semana"] == "0,1,2,3,4"
    assert c["janela_inicio"] == "09:00"
    assert c["filtro_estado"] == "SP"
    assert c["enviados_total"] == 0


def test_listar_ordena_por_status_depois_data(db_temp, perfil_id):
    a = criar(nome="A", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
              dias_semana={0,1,2,3,4}, janela_inicio=time(9,0), janela_fim=time(17,0))
    b = criar(nome="B", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
              dias_semana={0,1,2,3,4}, janela_inicio=time(9,0), janela_fim=time(17,0))
    nomes = [c["nome"] for c in listar()]
    # ambas rascunho — empata em status, ordena por id desc (mais nova primeiro)
    assert nomes == ["B", "A"]


def test_listar_traz_perfil_nome(db_temp, perfil_id):
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    out = listar()
    assert out[0]["perfil_nome"] == "Teste"


def test_obter_inexistente_retorna_none(db_temp):
    assert obter(99999) is None


def test_criar_com_dias_vazios_levanta(db_temp, perfil_id):
    with pytest.raises(ValueError):
        criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
              dias_semana=set(), janela_inicio=time(9,0), janela_fim=time(17,0))


def test_criar_com_janela_invertida_levanta(db_temp, perfil_id):
    with pytest.raises(ValueError):
        criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
              dias_semana={0}, janela_inicio=time(17,0), janela_fim=time(9,0))


def test_criar_com_por_dia_maior_que_limite_perfil_levanta(db_temp, perfil_id):
    # perfil tem limite_diario=250 (fixture)
    with pytest.raises(ValueError):
        criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=1000, por_dia=300,
              dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_campanhas.py -v`
Expected: FAIL (criar/listar/obter não existem)

- [ ] **Step 3: Implementar em `app/campanhas.py`**

```python
from .db import get_conn


def _format_hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _validar_payload(*, total_alvo: int, por_dia: int,
                      dias_semana: set[int],
                      janela_inicio: time, janela_fim: time,
                      perfil_limite_diario: int) -> None:
    if total_alvo <= 0:
        raise ValueError("total_alvo deve ser > 0")
    if por_dia <= 0:
        raise ValueError("por_dia deve ser > 0")
    if por_dia > perfil_limite_diario:
        raise ValueError(
            f"por_dia ({por_dia}) excede o limite diário do perfil "
            f"({perfil_limite_diario})"
        )
    if not dias_semana:
        raise ValueError("Pelo menos um dia da semana é obrigatório")
    if janela_inicio >= janela_fim:
        raise ValueError("janela_inicio deve ser menor que janela_fim")


def _carregar_limite_perfil(conn, perfil_id: int) -> int:
    row = conn.execute(
        "SELECT limite_diario FROM perfis_remetente WHERE id = ?",
        (perfil_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Perfil {perfil_id} não encontrado")
    return int(row["limite_diario"])


def criar(*,
    nome: str,
    perfil_id: int,
    filtros: dict,
    total_alvo: int,
    por_dia: int,
    dias_semana: set[int],
    janela_inicio: time,
    janela_fim: time,
) -> int:
    with get_conn() as conn:
        limite = _carregar_limite_perfil(conn, perfil_id)
    _validar_payload(
        total_alvo=total_alvo, por_dia=por_dia,
        dias_semana=dias_semana,
        janela_inicio=janela_inicio, janela_fim=janela_fim,
        perfil_limite_diario=limite,
    )
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO campanhas "
            "(nome, perfil_id, filtro_estado, filtro_tribunal, "
            " total_alvo, por_dia, dias_semana, janela_inicio, janela_fim, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'rascunho')",
            (
                nome.strip(), perfil_id,
                filtros.get("estado") or None, filtros.get("tribunal") or None,
                total_alvo, por_dia,
                format_dias_semana(dias_semana),
                _format_hhmm(janela_inicio), _format_hhmm(janela_fim),
            ),
        )
        return cur.lastrowid


def obter(campanha_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM campanhas WHERE id = ?", (campanha_id,)
        ).fetchone()
    return dict(row) if row else None


_ORDEM_STATUS = {"ativa": 0, "pausada": 1, "rascunho": 2,
                 "concluida": 3, "cancelada": 4}


def listar() -> list[dict]:
    """Lista campanhas com nome do perfil resolvido. Ordenada por status, depois id desc."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.*, p.nome AS perfil_nome, p.email_remetente AS perfil_email "
            "FROM campanhas c JOIN perfis_remetente p ON p.id = c.perfil_id "
            "ORDER BY c.id DESC"
        ).fetchall()
    out = [dict(r) for r in rows]
    out.sort(key=lambda c: (_ORDEM_STATUS.get(c["status"], 99), -c["id"]))
    return out
```

- [ ] **Step 4: Rodar e ver passar**

Run: `pytest tests/test_campanhas.py -v`
Expected: todos PASS

- [ ] **Step 5: Commit**

```bash
git add app/campanhas.py tests/test_campanhas.py
git commit -m "feat(campanhas): criar/listar/obter com validação"
```

---

## Task 7 — Transições de estado: iniciar / pausar / retomar / cancelar / concluir / editar

**Files:**
- Modify: `app/campanhas.py`
- Test: `tests/test_campanhas.py`

- [ ] **Step 1: Adicionar testes**

Acrescentar em `tests/test_campanhas.py`:

```python
from app.campanhas import (
    iniciar, pausar, retomar, cancelar, marcar_concluida, editar,
)


def test_iniciar_de_rascunho_vai_para_ativa(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    iniciar(cid)
    assert obter(cid)["status"] == "ativa"


def test_iniciar_quando_outra_ativa_no_perfil_levanta(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    a = criar(nome="A", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
              dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    b = criar(nome="B", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
              dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    iniciar(a)
    with pytest.raises(ValueError):
        iniciar(b)


def test_pausar_de_ativa_seta_motivo(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    iniciar(cid)
    pausar(cid, motivo="Teste de pausa")
    c = obter(cid)
    assert c["status"] == "pausada"
    assert c["pausa_motivo"] == "Teste de pausa"


def test_retomar_de_pausada_volta_para_ativa(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    iniciar(cid)
    pausar(cid, motivo="x")
    retomar(cid)
    c = obter(cid)
    assert c["status"] == "ativa"
    assert c["pausa_motivo"] is None


def test_cancelar_de_qualquer_estado_nao_terminal(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    cancelar(cid)
    assert obter(cid)["status"] == "cancelada"


def test_editar_so_em_rascunho(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    editar(cid, nome="Y", filtros={}, total_alvo=20, por_dia=10,
           dias_semana={1, 2}, janela_inicio=time(8,0), janela_fim=time(18,0))
    assert obter(cid)["nome"] == "Y"

    iniciar(cid)
    with pytest.raises(ValueError):
        editar(cid, nome="Z", filtros={}, total_alvo=20, por_dia=10,
               dias_semana={1}, janela_inicio=time(8,0), janela_fim=time(18,0))


def test_marcar_concluida(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    iniciar(cid)
    marcar_concluida(cid)
    c = obter(cid)
    assert c["status"] == "concluida"
    assert c["concluida_em"] is not None
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_campanhas.py -v`
Expected: FAIL

- [ ] **Step 3: Implementar em `app/campanhas.py`**

Adicionar (note: `_subir_thread` é stub aqui — Task 11 traz a thread de verdade):

```python
import threading


# Estado runtime das threads vivas. Chave = id da campanha.
_threads_runtime: dict[int, "RuntimeEstado"] = {}
_lock = threading.Lock()


@dataclass
class RuntimeEstado:
    """Snapshot pequeno do que a thread está fazendo agora (memória apenas)."""
    campanha_id: int
    iniciado_em: datetime
    ultimo_envio_em: datetime | None = None
    proximo_envio_em: datetime | None = None
    mensagem: str = ""


def _subir_thread(campanha_id: int) -> None:
    """
    Cria daemon thread que executa loop_campanha. Stub na Task 7;
    implementação real na Task 11.
    """
    pass  # substituído na Task 11


def iniciar(campanha_id: int) -> None:
    c = obter(campanha_id)
    if c is None:
        raise ValueError(f"Campanha {campanha_id} não encontrada")
    if c["status"] not in ("rascunho", "pausada"):
        raise ValueError(f"Não pode iniciar campanha em status {c['status']!r}")
    # garante unicidade por perfil
    with get_conn() as conn:
        outro = conn.execute(
            "SELECT id FROM campanhas WHERE perfil_id = ? "
            "AND status IN ('ativa','pausada') AND id != ?",
            (c["perfil_id"], campanha_id),
        ).fetchone()
    if outro:
        raise ValueError(
            f"Perfil já tem campanha ativa/pausada (id {outro['id']})"
        )
    with get_conn() as conn:
        conn.execute(
            "UPDATE campanhas SET status='ativa', pausa_motivo=NULL, "
            "iniciada_em=COALESCE(iniciada_em, CURRENT_TIMESTAMP) "
            "WHERE id = ?",
            (campanha_id,),
        )
    _subir_thread(campanha_id)


def pausar(campanha_id: int, motivo: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE campanhas SET status='pausada', pausa_motivo=? "
            "WHERE id = ? AND status='ativa'",
            (motivo, campanha_id),
        )
    # a thread, se viva, vai detectar no próximo dormir cooperativo


def retomar(campanha_id: int) -> None:
    c = obter(campanha_id)
    if c is None or c["status"] != "pausada":
        raise ValueError(f"Só pode retomar campanha pausada")
    with get_conn() as conn:
        conn.execute(
            "UPDATE campanhas SET status='ativa', pausa_motivo=NULL "
            "WHERE id = ?", (campanha_id,),
        )
    _subir_thread(campanha_id)


def cancelar(campanha_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE campanhas SET status='cancelada' "
            "WHERE id = ? AND status IN ('rascunho','ativa','pausada')",
            (campanha_id,),
        )


def marcar_concluida(campanha_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE campanhas SET status='concluida', "
            "concluida_em=CURRENT_TIMESTAMP "
            "WHERE id = ? AND status='ativa'",
            (campanha_id,),
        )


def editar(campanha_id: int, *,
    nome: str, filtros: dict,
    total_alvo: int, por_dia: int,
    dias_semana: set[int],
    janela_inicio: time, janela_fim: time,
) -> None:
    c = obter(campanha_id)
    if c is None:
        raise ValueError(f"Campanha {campanha_id} não encontrada")
    if c["status"] != "rascunho":
        raise ValueError("Só é possível editar campanhas em rascunho")
    with get_conn() as conn:
        limite = _carregar_limite_perfil(conn, c["perfil_id"])
    _validar_payload(
        total_alvo=total_alvo, por_dia=por_dia,
        dias_semana=dias_semana,
        janela_inicio=janela_inicio, janela_fim=janela_fim,
        perfil_limite_diario=limite,
    )
    with get_conn() as conn:
        conn.execute(
            "UPDATE campanhas SET nome=?, filtro_estado=?, filtro_tribunal=?, "
            "total_alvo=?, por_dia=?, dias_semana=?, "
            "janela_inicio=?, janela_fim=? WHERE id = ?",
            (
                nome.strip(),
                filtros.get("estado") or None, filtros.get("tribunal") or None,
                total_alvo, por_dia,
                format_dias_semana(dias_semana),
                _format_hhmm(janela_inicio), _format_hhmm(janela_fim),
                campanha_id,
            ),
        )
```

- [ ] **Step 4: Rodar e ver passar**

Run: `pytest tests/test_campanhas.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/campanhas.py tests/test_campanhas.py
git commit -m "feat(campanhas): transicoes iniciar/pausar/retomar/cancelar/editar"
```

---

## Task 8 — Refatorar mailer para extrair `enviar_um_contato` reutilizável

Objetivo: deixar `mailer.py` com uma função pública pequena `enviar_um_contato(server, perfil, contato, tracking_token)` que `campanhas.loop_campanha` pode chamar. Remover o `disparar()` antigo, `CampanhaEstado`, `_em_andamento`, `_loop_envio`, `cancelar`, `estado` (o modelo one-shot inteiro, que será substituído).

`enviar_teste` continua existindo (página /teste).

**Files:**
- Modify: `app/mailer.py`

- [ ] **Step 1: Tornar `_enviar_um` público (renomear para `enviar_um_contato`)**

Em `app/mailer.py`, linha ~203, renomear `def _enviar_um(...)` para `def enviar_um_contato(...)`. Atualizar chamadores dentro do mesmo arquivo.

- [ ] **Step 2: Tornar `_registrar_envio` público (renomear para `registrar_envio`)**

Aceita um novo argumento `campanha_id` no final, opcional. Update no INSERT:

```python
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
```

Atualizar todas as chamadas internas a `_registrar_envio` no arquivo para `registrar_envio`. Onde for envio de teste, manter `campanha_id=None`.

- [ ] **Step 3: Expor `selecionar_contatos`, `marcar_contato_invalido`, `email_valido`, `eh_bounce_permanente`, `carregar_perfil`**

Renomear `_selecionar_contatos` → `selecionar_contatos`, `_marcar_contato_invalido` → `marcar_contato_invalido`, `_email_valido` → `email_valido`, `_eh_bounce_permanente` → `eh_bounce_permanente`, `_carregar_perfil` → `carregar_perfil`. Atualizar referências internas.

- [ ] **Step 4: Remover o modelo one-shot**

Apagar do arquivo:

- A classe `CampanhaEstado` (linhas ~49-70)
- `_em_andamento` e `_lock` (linhas ~45-46)
- Funções `estado`, `cancelar`, `_loop_envio`, `disparar` (toda a lógica one-shot)
- Imports não usados: `random`, `threading`, `time`, `date` se não forem usados em outro lugar do mailer

Verificação rápida: `grep -n "CampanhaEstado\|_em_andamento\|_loop_envio\|^def disparar\|^def estado\|^def cancelar" app/mailer.py` deve retornar vazio depois.

- [ ] **Step 5: Confirmar que `enviar_teste` continua funcionando**

Run: `python -c "from app import mailer; print(mailer.enviar_teste, mailer.enviar_um_contato, mailer.registrar_envio)"`
Esperado: imprime os 3 sem erro de import.

- [ ] **Step 6: Rodar pytest pra garantir que não quebrou nada**

Run: `pytest -v`
Esperado: todos os testes existentes seguem passando (não toca em campanhas.py).

- [ ] **Step 7: Commit**

```bash
git add app/mailer.py
git commit -m "refactor(mailer): expoe enviar_um_contato/registrar_envio e remove modelo one-shot"
```

---

## Task 9 — Funções de leitura: `enviados_hoje_campanha`, `enviados_hoje_perfil`, `montar_estado_campanha`

**Files:**
- Modify: `app/campanhas.py`
- Test: `tests/test_campanhas.py`

- [ ] **Step 1: Adicionar testes**

```python
from app.campanhas import enviados_hoje_campanha, enviados_hoje_perfil, montar_estado_campanha


def _inserir_envio(conn, contato_id, perfil_id, *, campanha_id=None,
                   status="ok", quando_iso="now"):
    """Helper: insere uma linha em envios. quando_iso=None usa now."""
    if quando_iso == "now":
        conn.execute(
            "INSERT INTO envios (contato_id, perfil_remetente_id, "
            "campanha_id, status) VALUES (?, ?, ?, ?)",
            (contato_id, perfil_id, campanha_id, status),
        )
    else:
        conn.execute(
            "INSERT INTO envios (contato_id, perfil_remetente_id, "
            "campanha_id, status, enviado_em) VALUES (?, ?, ?, ?, ?)",
            (contato_id, perfil_id, campanha_id, status, quando_iso),
        )


def _criar_contato(conn, email="x@y.com", tribunal="tjsp"):
    cur = conn.execute(
        "INSERT INTO contatos (email, tribunal) VALUES (?, ?)",
        (email, tribunal),
    )
    return cur.lastrowid


def test_enviados_hoje_campanha_conta_so_ok_e_de_hoje(db_temp, perfil_id):
    from app.db import get_conn
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=100, por_dia=10,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    with get_conn() as conn:
        c1 = _criar_contato(conn, "a@x.com")
        c2 = _criar_contato(conn, "b@x.com")
        c3 = _criar_contato(conn, "c@x.com")
        _inserir_envio(conn, c1, perfil_id, campanha_id=cid)
        _inserir_envio(conn, c2, perfil_id, campanha_id=cid)
        _inserir_envio(conn, c3, perfil_id, campanha_id=cid, status="erro")
        # ontem, não conta
        _inserir_envio(conn, c1, perfil_id, campanha_id=cid,
                       quando_iso="2020-01-01 12:00:00")
    assert enviados_hoje_campanha(cid) == 2


def test_enviados_hoje_perfil_ignora_teste(db_temp, perfil_id):
    from app.db import get_conn
    with get_conn() as conn:
        c1 = _criar_contato(conn, "a@x.com", tribunal="tjsp")
        c2 = _criar_contato(conn, "b@x.com", tribunal="_teste")
        _inserir_envio(conn, c1, perfil_id)
        _inserir_envio(conn, c2, perfil_id)
    assert enviados_hoje_perfil(perfil_id) == 1


def test_montar_estado_campanha_combina_tudo(db_temp, perfil_id, monkeypatch):
    monkeypatch.setattr("app.campanhas._subir_thread", lambda cid: None)
    cid = criar(nome="X", perfil_id=perfil_id, filtros={}, total_alvo=100, por_dia=10,
                dias_semana={0,1,2,3,4}, janela_inicio=time(9,0), janela_fim=time(17,0))
    iniciar(cid)
    e = montar_estado_campanha(cid)
    assert e.id == cid
    assert e.status == "ativa"
    assert e.por_dia == 10
    assert e.perfil_limite_diario == 250
    assert e.dias_semana == {0,1,2,3,4}
    assert e.janela_inicio == time(9, 0)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_campanhas.py -v`
Expected: FAIL (funções não definidas)

- [ ] **Step 3: Implementar em `app/campanhas.py`**

```python
def enviados_hoje_campanha(campanha_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM envios "
            "WHERE campanha_id = ? AND status = 'ok' "
            "AND date(enviado_em) = date('now', 'localtime')",
            (campanha_id,),
        ).fetchone()
    return int(row["c"])


def enviados_hoje_perfil(perfil_id: int) -> int:
    """Total de envios 'ok' do perfil hoje, EXCLUINDO testes."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM envios e "
            "JOIN contatos c2 ON c2.id = e.contato_id "
            "WHERE e.perfil_remetente_id = ? AND e.status = 'ok' "
            "AND c2.tribunal != '_teste' "
            "AND date(e.enviado_em) = date('now', 'localtime')",
            (perfil_id,),
        ).fetchone()
    return int(row["c"])


def montar_estado_campanha(campanha_id: int) -> EstadoCampanha:
    c = obter(campanha_id)
    if c is None:
        raise ValueError(f"Campanha {campanha_id} não encontrada")
    with get_conn() as conn:
        limite = _carregar_limite_perfil(conn, c["perfil_id"])
    return EstadoCampanha(
        id=c["id"],
        status=c["status"],
        total_alvo=c["total_alvo"],
        por_dia=c["por_dia"],
        enviados_total=c["enviados_total"],
        enviados_hoje=enviados_hoje_campanha(campanha_id),
        enviados_hoje_perfil=enviados_hoje_perfil(c["perfil_id"]),
        perfil_limite_diario=limite,
        dias_semana=parse_dias_semana(c["dias_semana"]),
        janela_inicio=_parse_hhmm(c["janela_inicio"]),
        janela_fim=_parse_hhmm(c["janela_fim"]),
    )
```

- [ ] **Step 4: Rodar e ver passar**

Run: `pytest tests/test_campanhas.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/campanhas.py tests/test_campanhas.py
git commit -m "feat(campanhas): contadores de envio do dia + montar_estado_campanha"
```

---

## Task 10 — `selecionar_proximo_contato` (uso interno do loop)

**Files:**
- Modify: `app/campanhas.py`
- Test: `tests/test_campanhas.py`

- [ ] **Step 1: Adicionar testes**

```python
from app.campanhas import selecionar_proximo_contato


def test_selecionar_proximo_contato_respeita_filtros(db_temp, perfil_id):
    from app.db import get_conn
    cid = criar(nome="X", perfil_id=perfil_id,
                filtros={"estado": "SP", "tribunal": "tjsp"},
                total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    with get_conn() as conn:
        conn.execute("INSERT INTO contatos (email, tribunal, estado) "
                     "VALUES ('a@x.com', 'tjsp', 'SP')")
        conn.execute("INSERT INTO contatos (email, tribunal, estado) "
                     "VALUES ('b@x.com', 'tjmg', 'MG')")  # não casa
        conn.execute("INSERT INTO contatos (email, tribunal, estado, invalido) "
                     "VALUES ('c@x.com', 'tjsp', 'SP', 1)")  # inválido
    contato = selecionar_proximo_contato(cid)
    assert contato["email"] == "a@x.com"


def test_selecionar_proximo_contato_pula_ja_enviados(db_temp, perfil_id):
    from app.db import get_conn
    cid = criar(nome="X", perfil_id=perfil_id, filtros={},
                total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    with get_conn() as conn:
        c1 = _criar_contato(conn, "a@x.com")
        c2 = _criar_contato(conn, "b@x.com")
        # c1 já recebeu OK desse perfil — pula
        _inserir_envio(conn, c1, perfil_id)
    contato = selecionar_proximo_contato(cid)
    assert contato["email"] == "b@x.com"


def test_selecionar_proximo_sem_contato_retorna_none(db_temp, perfil_id):
    cid = criar(nome="X", perfil_id=perfil_id, filtros={},
                total_alvo=10, por_dia=5,
                dias_semana={0}, janela_inicio=time(9,0), janela_fim=time(17,0))
    assert selecionar_proximo_contato(cid) is None
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_campanhas.py -v`
Expected: FAIL

- [ ] **Step 3: Implementar em `app/campanhas.py`**

```python
from . import mailer


def selecionar_proximo_contato(campanha_id: int) -> dict | None:
    c = obter(campanha_id)
    if c is None:
        return None
    filtros = {
        "estado": c["filtro_estado"],
        "tribunal": c["filtro_tribunal"],
    }
    contatos = mailer.selecionar_contatos(filtros, limite=1, perfil_id=c["perfil_id"])
    return contatos[0] if contatos else None
```

- [ ] **Step 4: Rodar e ver passar**

Run: `pytest tests/test_campanhas.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/campanhas.py tests/test_campanhas.py
git commit -m "feat(campanhas): selecionar_proximo_contato delegando ao mailer"
```

---

## Task 11 — Loop da thread (`loop_campanha`) + `_subir_thread` real

Aqui vem a parte com efeito colateral (threads, SMTP, sleep). Não vamos testar com pytest — validamos manualmente. Mas mantemos a função pequena delegando às puras já testadas.

**Files:**
- Modify: `app/campanhas.py`

- [ ] **Step 1: Implementar conexão SMTP encapsulada**

Adicionar em `app/campanhas.py`:

```python
import logging
import secrets
import smtplib
import time as time_mod

from .crypto import decrypt

log = logging.getLogger("peritos.campanhas")


class _SmtpSession:
    """Mantém a conexão SMTP do perfil, reabre se cair, fecha em sleeps longos."""
    def __init__(self, perfil: dict):
        self.perfil = perfil
        self._server: smtplib.SMTP | None = None

    def garantir_conectado(self) -> smtplib.SMTP:
        if self._server is not None:
            try:
                self._server.noop()
                return self._server
            except Exception:
                self._server = None
        senha = decrypt(self.perfil["smtp_senha_enc"])
        s = smtplib.SMTP(self.perfil["smtp_host"], self.perfil["smtp_port"], timeout=30)
        s.starttls()
        s.login(self.perfil["email_remetente"], senha)
        self._server = s
        return s

    def fechar(self) -> None:
        if self._server is not None:
            try:
                self._server.quit()
            except Exception:
                pass
            self._server = None
```

- [ ] **Step 2: Implementar `loop_campanha` e `_subir_thread`**

Substituir o stub `_subir_thread` por:

```python
def _dormir_cooperativo(segundos: float, campanha_id: int) -> bool:
    """
    Dorme em chunks de 30s, recarregando o status da campanha em cada chunk.
    Retorna True se completou, False se o status mudou e o loop deve sair.
    """
    fim = time_mod.monotonic() + segundos
    while True:
        agora_mono = time_mod.monotonic()
        if agora_mono >= fim:
            return True
        chunk = min(30.0, fim - agora_mono)
        time_mod.sleep(chunk)
        c = obter(campanha_id)
        if c is None or c["status"] != "ativa":
            return False


def _incrementar_enviados(campanha_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE campanhas SET enviados_total = enviados_total + 1 WHERE id = ?",
            (campanha_id,),
        )


def loop_campanha(campanha_id: int) -> None:
    """Roda na thread daemon. Sai quando status != 'ativa' ou alvo atingido."""
    log.info("[campanha %s] thread iniciada", campanha_id)
    try:
        c = obter(campanha_id)
        if c is None:
            return
        perfil = mailer.carregar_perfil(c["perfil_id"])
        if perfil is None:
            pausar(campanha_id, motivo="Perfil não encontrado")
            return

        smtp = _SmtpSession(perfil)
        falhas_transientes_seguidas = 0

        try:
            while True:
                estado = montar_estado_campanha(campanha_id)
                acao = proxima_acao(estado, datetime.now())

                if acao.tipo is AcaoTipo.SAIR:
                    log.info("[campanha %s] saindo (status=%s)", campanha_id, estado.status)
                    return
                if acao.tipo is AcaoTipo.CONCLUIR:
                    marcar_concluida(campanha_id)
                    log.info("[campanha %s] concluida", campanha_id)
                    return
                if acao.tipo is AcaoTipo.DORMIR_ATE:
                    smtp.fechar()  # libera conexão durante sleep longo
                    seg = max(1.0, (acao.dormir_ate - datetime.now()).total_seconds())
                    log.debug("[campanha %s] dormindo até %s (%.0fs)",
                              campanha_id, acao.dormir_ate, seg)
                    if not _dormir_cooperativo(seg, campanha_id):
                        return
                    continue

                # AcaoTipo.ENVIAR
                contato = selecionar_proximo_contato(campanha_id)
                if contato is None:
                    log.info("[campanha %s] sem contatos elegíveis — concluindo", campanha_id)
                    marcar_concluida(campanha_id)
                    return

                if not mailer.email_valido(contato["email"]):
                    mailer.registrar_envio(
                        contato["id"], c["perfil_id"], "erro",
                        "Email com formato inválido", None, None, campanha_id,
                    )
                    mailer.marcar_contato_invalido(contato["id"])
                    continue

                # Tenta enviar com retry para transientes
                msg_id = None
                token = secrets.token_urlsafe(16)
                tentativas = 0
                while True:
                    tentativas += 1
                    try:
                        server = smtp.garantir_conectado()
                        msg_id = mailer.enviar_um_contato(server, perfil, contato, token)
                        break
                    except Exception as e:
                        cls = classificar_erro_smtp(e)
                        msg_erro = str(e)[:500]
                        log.warning("[campanha %s] envio falhou (%s): %s",
                                    campanha_id, cls.value, msg_erro)
                        if cls is ErroSmtp.FATAL:
                            pausar(campanha_id, motivo=f"Falha de autenticação: {msg_erro}")
                            return
                        if cls is ErroSmtp.POR_CONTATO:
                            mailer.registrar_envio(
                                contato["id"], c["perfil_id"], "erro",
                                msg_erro, None, None, campanha_id,
                            )
                            if mailer.eh_bounce_permanente(msg_erro):
                                mailer.marcar_contato_invalido(contato["id"])
                            falhas_transientes_seguidas = 0
                            break
                        # TRANSIENTE
                        smtp.fechar()
                        if tentativas >= 3:
                            mailer.registrar_envio(
                                contato["id"], c["perfil_id"], "erro",
                                f"Transiente (3 tentativas): {msg_erro}",
                                None, None, campanha_id,
                            )
                            falhas_transientes_seguidas += 1
                            if falhas_transientes_seguidas >= 3:
                                pausar(campanha_id,
                                       motivo=f"Rede/SMTP instável: {msg_erro}")
                                return
                            break
                        # backoff: 30s, 2min
                        backoff = 30.0 if tentativas == 1 else 120.0
                        if not _dormir_cooperativo(backoff, campanha_id):
                            return
                        continue

                if msg_id is not None:
                    mailer.registrar_envio(
                        contato["id"], c["perfil_id"], "ok",
                        None, msg_id, token, campanha_id,
                    )
                    _incrementar_enviados(campanha_id)
                    falhas_transientes_seguidas = 0

                if not _dormir_cooperativo(acao.intervalo_seg or 10.0, campanha_id):
                    return

        finally:
            smtp.fechar()
    except Exception:
        log.exception("[campanha %s] erro inesperado no loop", campanha_id)
        pausar(campanha_id, motivo="Erro inesperado no worker — ver logs")


def _subir_thread(campanha_id: int) -> None:  # noqa: F811  (substitui o stub)
    with _lock:
        atual = _threads_runtime.get(campanha_id)
        if atual is not None:
            return  # já tem thread viva
        _threads_runtime[campanha_id] = RuntimeEstado(
            campanha_id=campanha_id,
            iniciado_em=datetime.now(),
        )

    def _wrapped():
        try:
            loop_campanha(campanha_id)
        finally:
            with _lock:
                _threads_runtime.pop(campanha_id, None)

    t = threading.Thread(target=_wrapped, daemon=True, name=f"campanha-{campanha_id}")
    t.start()
```

> Note: o `def _subir_thread` aparece duas vezes no arquivo agora. Remova o stub anterior (Task 7) — deixe só a versão real.

- [ ] **Step 3: Verificar import e que pytest ainda passa**

Run: `pytest -v`
Esperado: PASS. Os testes monkeypatcham `_subir_thread` para no-op, então essa nova implementação não roda em testes.

- [ ] **Step 4: Commit**

```bash
git add app/campanhas.py
git commit -m "feat(campanhas): loop_campanha (daemon thread) com retry/pausa estratificados"
```

---

## Task 12 — Reidratação no boot

**Files:**
- Modify: `app/campanhas.py`, `app/main.py`

- [ ] **Step 1: Adicionar `reidratar()` em `app/campanhas.py`**

```python
def reidratar() -> None:
    """No startup, sobe thread para cada campanha em status='ativa'."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM campanhas WHERE status = 'ativa'"
        ).fetchall()
    for r in rows:
        log.info("[campanha %s] reidratando", r["id"])
        _subir_thread(r["id"])
```

- [ ] **Step 2: Chamar no startup do FastAPI**

Em `app/main.py`, localizar onde `scheduler.iniciar()` é chamado (provavelmente em uma função `startup` ou `lifespan`). Adicionar logo após:

```python
from . import campanhas
campanhas.reidratar()
```

(Se o `import campanhas` ainda não existe no topo de main.py, adicionar.)

- [ ] **Step 3: Smoke test manual**

Run: `uvicorn app.main:app --reload --port 8000`
Esperado: app sobe sem erro. Logs mostram "[campanha N] reidratando" só se houver campanha ativa (não há ainda — é só checar que a chamada não quebra).
Encerrar com Ctrl+C.

- [ ] **Step 4: Commit**

```bash
git add app/campanhas.py app/main.py
git commit -m "feat(campanhas): reidratar campanhas ativas no startup"
```

---

## Task 13 — Limpeza: remover `tipo='campanha'` de scheduler/main/agendamento_form

**Files:**
- Modify: `app/scheduler.py`, `app/main.py`, `app/templates/agendamento_form.html`

- [ ] **Step 1: Remover branch em `app/scheduler.py::_executar_job`**

Localizar o `elif ag["tipo"] == "campanha":` (~linha 99-115). Apagar o bloco inteiro até o próximo `else:`. Resultado:

```python
        if ag["tipo"] == "scraper":
            from .scrapers import runner as scraper_runner
            alvo = (ag.get("alvo") or "").lower()
            if alvo == "todos":
                scraper_runner.disparar_todos()
                _registrar_fim(run_id, "ok", "Scrapers (todos) disparados")
            else:
                scraper_runner.disparar(ag["alvo"])
                _registrar_fim(run_id, "ok", f"Scraper {alvo.upper()} disparado")
        else:
            _registrar_fim(run_id, "erro", f"Tipo desconhecido: {ag.get('tipo')}")
```

- [ ] **Step 2: Remover branch em `app/main.py::_o_que`**

Localizar `_o_que` (~linha 1189). Apagar o bloco `if ag.get("tipo") == "campanha":`. Resultado:

```python
def _o_que(ag: dict) -> str:
    if ag.get("tipo") == "scraper":
        alvo = (ag.get("alvo") or "").lower()
        if alvo == "todos":
            return "Scraper · todos os tribunais (em sequência)"
        return f"Scraper {alvo.upper()}"
    return ag.get("tipo") or "—"
```

- [ ] **Step 3: Simplificar `agendamentos_novo_submit`**

Em `app/main.py`, localizar a função (~linha 1247). Remover validações específicas a `campanha`:

- Apagar o bloco `elif tipo == "campanha":` ... até `if not ok: erro = "Perfil de remetente inválido."`
- Apagar `else: erro = "Tipo de agendamento inválido."`
- Tornar o `if tipo == "scraper":` único, sem `else`. Se `tipo` não for `scraper`, retornar erro `"Tipo inválido (apenas scraper é suportado)."`

Resultado da validação:

```python
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
```

E remover os parâmetros `perfil_id`, `filtro_estado`, `filtro_tribunal`, `quantidade` da assinatura do endpoint (não vão mais ser enviados pelo form). Atualizar o INSERT para passar `NULL` neles (as colunas continuam existindo por compatibilidade):

```python
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agendamentos (nome, tipo, alvo, perfil_id, filtro_estado, "
            "filtro_tribunal, quantidade, frequencia, hora, data, dia_semana, "
            "dia_mes, cron, ativo) "
            "VALUES (?, 'scraper', ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, '', 1)",
            (nome.strip(), alvo,
             frequencia, hora, data or None, ds, dm),
        )
```

- [ ] **Step 4: Simplificar `_ctx_agendamento_form`**

Remover `perfis`, `tribunais_por_uf`, `ufs_por_tribunal` do contexto retornado (não são mais usados pelo template). Manter apenas `scrapers` e os básicos.

```python
def _ctx_agendamento_form(user: dict, request: Request, erro: str | None = None) -> dict:
    return {
        "request": request, "user": user, "erro": erro,
        "scrapers": scraper_registry.listar(),
    }
```

- [ ] **Step 5: Simplificar `app/templates/agendamento_form.html`**

Reescrever o conteúdo do form. Esqueleto:

```html
{% extends "base.html" %}
{% block titulo %}Novo agendamento — Peritos{% endblock %}
{% block conteudo %}
<a href="/agendamentos" class="voltar">← Agendamentos</a>

<header class="pagina-header">
  <div>
    <div class="eyebrow">Cadastrando</div>
    <h1>Novo agendamento</h1>
    <p class="sub">Agendamentos rodam <strong>scrapers</strong> automaticamente. Para envio de e-mail, use <a href="/campanhas">Campanhas</a>.</p>
  </div>
</header>

{% if erro %}<div class="alerta-erro">{{ erro }}</div>{% endif %}

<form method="post" action="/agendamentos/novo" class="form-largo">
  <input type="hidden" name="tipo" value="scraper">

  <fieldset>
    <legend>Identificação</legend>
    <label>Nome
      <input type="text" name="nome" placeholder="Ex.: TJMG diário" required>
    </label>
  </fieldset>

  <fieldset>
    <legend>Scraper</legend>
    <label>Tribunal
      <select name="alvo" required>
        <option value="todos">⚡ Todos os tribunais (em sequência)</option>
        {% for s in scrapers %}{% if not s.manual %}
        <option value="{{ s.sigla }}">{{ s.sigla|upper }} — {{ s.nome }}</option>
        {% endif %}{% endfor %}
      </select>
      <small class="cinza">Escolher "Todos" roda os tribunais um após o outro — pode levar várias horas.</small>
    </label>
  </fieldset>

  <fieldset>
    <legend>Quando rodar</legend>
    <label>Frequência
      <select name="frequencia" id="campo_freq" required>
        <option value="uma_vez">Apenas uma vez</option>
        <option value="diario" selected>Todos os dias</option>
        <option value="semanal">Toda semana</option>
        <option value="mensal">Todo mês</option>
      </select>
    </label>
    <div class="grade-2">
      <label>Hora
        <input type="time" name="hora" value="03:00" required>
      </label>
      <label id="campo_data" style="display:none">Data
        <input type="date" name="data">
      </label>
      <label id="campo_dia_semana" style="display:none">Dia da semana
        <select name="dia_semana">
          <option value="0">Segunda</option>
          <option value="1">Terça</option>
          <option value="2">Quarta</option>
          <option value="3">Quinta</option>
          <option value="4">Sexta</option>
          <option value="5">Sábado</option>
          <option value="6">Domingo</option>
        </select>
      </label>
      <label id="campo_dia_mes" style="display:none">Dia do mês (1–28)
        <input type="number" name="dia_mes" min="1" max="28" value="1">
      </label>
    </div>
    <small class="cinza">Horário considerado: <strong>fuso de Brasília (America/Sao_Paulo)</strong>.</small>
  </fieldset>

  <button type="submit" class="btn-primario">Criar agendamento</button>
</form>

<script>
  (function () {
    const freqSel = document.getElementById('campo_freq');
    const cData = document.getElementById('campo_data');
    const cSem  = document.getElementById('campo_dia_semana');
    const cMes  = document.getElementById('campo_dia_mes');
    function atualizarFreq() {
      cData.style.display = (freqSel.value === 'uma_vez') ? '' : 'none';
      cSem.style.display  = (freqSel.value === 'semanal') ? '' : 'none';
      cMes.style.display  = (freqSel.value === 'mensal')  ? '' : 'none';
    }
    freqSel.addEventListener('change', atualizarFreq);
    atualizarFreq();
  })();
</script>
{% endblock %}
```

- [ ] **Step 6: Smoke test manual**

Run: `uvicorn app.main:app --reload --port 8000`
- Acessar `/agendamentos/novo` no navegador. Confirmar que não há mais a opção "Disparar campanha de e-mail".
- Tentar criar um agendamento de scraper qualquer; ele deve aparecer na lista.

- [ ] **Step 7: Commit**

```bash
git add app/scheduler.py app/main.py app/templates/agendamento_form.html
git commit -m "refactor: remover tipo='campanha' de /agendamentos (apenas scraper)"
```

---

## Task 14 — Templates da nova UI de campanhas

**Files:**
- Modify: `app/templates/campanhas.html` (sobrescrever)
- Create: `app/templates/campanha_form.html`, `app/templates/campanha_detalhe.html`, `app/templates/_campanha_detalhe_corpo.html`
- Delete: `app/templates/campanha_acompanhar.html`, `app/templates/_campanha_estado.html`

- [ ] **Step 1: Sobrescrever `app/templates/campanhas.html` (lista)**

```html
{% extends "base.html" %}
{% block titulo %}Campanhas — Peritos{% endblock %}
{% block conteudo %}
<header class="pagina-header">
  <div>
    <div class="eyebrow">Disparo persistente</div>
    <h1>Campanhas</h1>
    <p class="sub">Cada campanha tem objetivo total, quota diária e janela de envio. O sistema toca sozinho até concluir.</p>
  </div>
  <div class="pagina-header-acoes">
    <a href="/campanhas/nova" class="btn-primario">+ Nova campanha</a>
  </div>
</header>

{% if not campanhas %}
<div class="bloco vazio" style="padding:32px; text-align:center">
  Nenhuma campanha ainda. <a href="/campanhas/nova">Criar a primeira</a>.
</div>
{% else %}
<table class="tabela espacada">
  <thead>
    <tr>
      <th>Nome</th>
      <th>Perfil</th>
      <th>Status</th>
      <th>Progresso</th>
      <th>Hoje</th>
      <th></th>
    </tr>
  </thead>
  <tbody>
    {% for c in campanhas %}
    <tr>
      <td><strong>{{ c.nome }}</strong></td>
      <td>{{ c.perfil_nome }}<br><small class="cinza">{{ c.perfil_email }}</small></td>
      <td><span class="status status-{{ c.status }}">{{ c.status }}</span></td>
      <td>
        <div class="progress" title="{{ c.enviados_total }} de {{ c.total_alvo }}">
          <div class="progress-bar" style="width: {{ (c.enviados_total * 100 // c.total_alvo) if c.total_alvo else 0 }}%"></div>
        </div>
        <small class="cinza">{{ c.enviados_total }}/{{ c.total_alvo }}</small>
      </td>
      <td>
        {% if c.status in ('ativa', 'pausada') %}
          {{ c.enviados_hoje }}/{{ c.por_dia }}
        {% else %}—{% endif %}
      </td>
      <td><a href="/campanhas/{{ c.id }}" class="btn-secundario">Abrir →</a></td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% endif %}
{% endblock %}
```

- [ ] **Step 2: Criar `app/templates/campanha_form.html` (criar/editar)**

```html
{% extends "base.html" %}
{% block titulo %}{{ "Editar" if campanha else "Nova" }} campanha — Peritos{% endblock %}
{% block conteudo %}
<a href="/campanhas{% if campanha %}/{{ campanha.id }}{% endif %}" class="voltar">← Voltar</a>

<header class="pagina-header">
  <div>
    <div class="eyebrow">{{ "Editando" if campanha else "Cadastrando" }}</div>
    <h1>{{ "Editar campanha" if campanha else "Nova campanha" }}</h1>
  </div>
</header>

{% if erro %}<div class="alerta-erro">{{ erro }}</div>{% endif %}

{% set acao = "/campanhas/" ~ campanha.id ~ "/editar" if campanha else "/campanhas/nova" %}
<form method="post" action="{{ acao }}" class="form-largo">
  <fieldset>
    <legend>Identificação</legend>
    <label>Nome
      <input type="text" name="nome" required value="{{ campanha.nome if campanha else '' }}"
             placeholder="Ex.: TJSP janeiro">
    </label>
    <label>Perfil de remetente
      <select name="perfil_id" required {% if campanha %}disabled{% endif %}>
        {% for p in perfis %}
        <option value="{{ p.id }}"
          {% if p.disabled %}disabled{% endif %}
          {% if campanha and campanha.perfil_id == p.id %}selected{% endif %}>
          {{ p.nome }} &lt;{{ p.email_remetente }}&gt; — limite {{ p.limite_diario }}/dia
          {% if p.disabled %}(em uso por "{{ p.uso_por }}"){% endif %}
        </option>
        {% endfor %}
      </select>
      {% if campanha %}<input type="hidden" name="perfil_id" value="{{ campanha.perfil_id }}">{% endif %}
    </label>
  </fieldset>

  <fieldset>
    <legend>Filtros de contato</legend>
    <div class="grade-2">
      <label>UF
        <select name="estado">
          <option value="">— todos —</option>
          {% for uf in ufs %}
            <option value="{{ uf }}" {% if campanha and campanha.filtro_estado == uf %}selected{% endif %}>{{ uf }}</option>
          {% endfor %}
        </select>
      </label>
      <label>Tribunal
        <select name="tribunal">
          <option value="">— todos —</option>
          {% for tj in tribunais %}
            <option value="{{ tj }}" {% if campanha and campanha.filtro_tribunal == tj %}selected{% endif %}>{{ tj|upper }}</option>
          {% endfor %}
        </select>
      </label>
    </div>
  </fieldset>

  <fieldset>
    <legend>Meta e ritmo</legend>
    <div class="grade-2">
      <label>Total a enviar
        <input type="number" name="total_alvo" min="1" required
               value="{{ campanha.total_alvo if campanha else 1000 }}">
      </label>
      <label>Por dia (máx)
        <input type="number" name="por_dia" min="1" required
               value="{{ campanha.por_dia if campanha else 200 }}">
        <small class="cinza">Não pode passar do limite diário do perfil escolhido.</small>
      </label>
    </div>
  </fieldset>

  <fieldset>
    <legend>Dias da semana</legend>
    {% set selecionados = (campanha.dias_semana or '0,1,2,3,4').split(',') %}
    {% for valor, rotulo in [('0','Seg'),('1','Ter'),('2','Qua'),('3','Qui'),('4','Sex'),('5','Sáb'),('6','Dom')] %}
    <label class="inline">
      <input type="checkbox" name="dias_semana" value="{{ valor }}"
             {% if valor in selecionados %}checked{% endif %}> {{ rotulo }}
    </label>
    {% endfor %}
  </fieldset>

  <fieldset>
    <legend>Janela horária</legend>
    <div class="grade-2">
      <label>Início
        <input type="time" name="janela_inicio" required
               value="{{ campanha.janela_inicio if campanha else '09:00' }}">
      </label>
      <label>Fim
        <input type="time" name="janela_fim" required
               value="{{ campanha.janela_fim if campanha else '17:00' }}">
      </label>
    </div>
    <small class="cinza">Os envios serão espalhados ao longo dessa janela. Fuso de Brasília.</small>
  </fieldset>

  <button type="submit" class="btn-primario">{{ "Salvar" if campanha else "Criar como rascunho" }}</button>
</form>
{% endblock %}
```

- [ ] **Step 3: Criar `app/templates/campanha_detalhe.html`**

```html
{% extends "base.html" %}
{% block titulo %}{{ campanha.nome }} — Campanha{% endblock %}
{% block conteudo %}
<a href="/campanhas" class="voltar">← Campanhas</a>

<header class="pagina-header">
  <div>
    <div class="eyebrow">Campanha</div>
    <h1>{{ campanha.nome }}</h1>
    <p class="sub">{{ campanha.perfil_nome }} &lt;{{ campanha.perfil_email }}&gt;
      {% if campanha.filtro_estado or campanha.filtro_tribunal %}
        · {{ (campanha.filtro_tribunal or '')|upper }}{% if campanha.filtro_estado %} · {{ campanha.filtro_estado }}{% endif %}
      {% endif %}
    </p>
  </div>
  <div class="pagina-header-acoes">
    {% if campanha.status == 'rascunho' %}
      <a href="/campanhas/{{ campanha.id }}/editar" class="btn-secundario">Editar</a>
      <form method="post" action="/campanhas/{{ campanha.id }}/iniciar" class="inline">
        <button class="btn-primario">Iniciar</button>
      </form>
    {% elif campanha.status == 'ativa' %}
      <form method="post" action="/campanhas/{{ campanha.id }}/pausar" class="inline">
        <button class="btn-secundario">Pausar</button>
      </form>
    {% elif campanha.status == 'pausada' %}
      <form method="post" action="/campanhas/{{ campanha.id }}/retomar" class="inline">
        <button class="btn-primario">Retomar</button>
      </form>
    {% endif %}
    {% if campanha.status in ('rascunho','ativa','pausada') %}
      <form method="post" action="/campanhas/{{ campanha.id }}/cancelar" class="inline"
            onsubmit="return confirm('Cancelar a campanha?');">
        <button class="btn-link erro">Cancelar</button>
      </form>
    {% endif %}
  </div>
</header>

<div id="corpo"
     {% if campanha.status == 'ativa' %}
       hx-get="/campanhas/{{ campanha.id }}/parcial"
       hx-trigger="every 5s"
       hx-swap="outerHTML"
     {% endif %}>
  {% include "_campanha_detalhe_corpo.html" %}
</div>
{% endblock %}
```

- [ ] **Step 4: Criar `app/templates/_campanha_detalhe_corpo.html`**

```html
<div id="corpo"
     {% if campanha.status == 'ativa' %}
       hx-get="/campanhas/{{ campanha.id }}/parcial"
       hx-trigger="every 5s"
       hx-swap="outerHTML"
     {% endif %}>

{% if campanha.pausa_motivo %}
<div class="alerta-erro" style="margin-bottom:16px">
  <strong>⚠️ Pausada:</strong> {{ campanha.pausa_motivo }}
</div>
{% endif %}

<section class="bloco">
  <h2>Progresso geral</h2>
  <div class="progress grande">
    <div class="progress-bar" style="width: {{ progresso_pct }}%"></div>
  </div>
  <p>
    <strong>{{ campanha.enviados_total }}</strong> de
    <strong>{{ campanha.total_alvo }}</strong>
    ({{ progresso_pct }}%)
    {% if estimativa_conclusao %}
      · estimativa de conclusão: <strong>{{ estimativa_conclusao }}</strong>
    {% endif %}
  </p>
</section>

<section class="bloco">
  <h2>Hoje</h2>
  {% if campanha.status in ('ativa','pausada') %}
    <p>
      <strong>{{ enviados_hoje }}/{{ campanha.por_dia }}</strong> enviados ·
      janela <strong>{{ campanha.janela_inicio }}–{{ campanha.janela_fim }}</strong>
    </p>
    <p class="cinza">{{ status_worker_texto }}</p>
  {% else %}
    <p class="cinza">Campanha {{ campanha.status }}.</p>
  {% endif %}
</section>

<section class="bloco">
  <h2>Próximos 7 dias</h2>
  <table class="tabela">
    <thead><tr><th>Data</th><th>Dia</th><th>Previsto</th></tr></thead>
    <tbody>
      {% for d in proximos_7 %}
      <tr>
        <td>{{ d.data }}</td>
        <td>{{ d.dia_nome }}</td>
        <td>
          {% if d.tipo == 'envio' %}{{ d.previsto }}
          {% elif d.tipo == 'fora' %}<span class="cinza">fora dos dias</span>
          {% elif d.tipo == 'concluida' %}<span class="cinza">concluída</span>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</section>

<section class="bloco">
  <h2>Histórico por dia</h2>
  {% if historico_por_dia %}
  <table class="tabela">
    <thead><tr><th>Data</th><th>Enviados</th><th>Erros</th><th>Bounces</th><th>Aberturas</th></tr></thead>
    <tbody>
      {% for h in historico_por_dia %}
      <tr>
        <td><a href="/historico?campanha_id={{ campanha.id }}">{{ h.data }}</a></td>
        <td>{{ h.enviados }}</td>
        <td>{{ h.erros }}</td>
        <td>{{ h.bounces }}</td>
        <td>{{ h.aberturas }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p class="cinza">Nenhum envio registrado ainda.</p>
  {% endif %}
</section>

<section class="bloco">
  <h2>Últimos envios</h2>
  {% if ultimos %}
  <table class="tabela">
    <thead><tr><th>Hora</th><th>E-mail</th><th>Status</th></tr></thead>
    <tbody>
      {% for u in ultimos %}
      <tr>
        <td>{{ u.hora }}</td>
        <td>{{ u.email }}</td>
        <td><span class="status status-{{ u.status }}">{{ u.status }}</span></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  <p><a href="/historico?campanha_id={{ campanha.id }}">Ver tudo →</a></p>
  {% else %}
  <p class="cinza">Nenhum envio registrado ainda.</p>
  {% endif %}
</section>

</div>
```

- [ ] **Step 5: Deletar templates antigos**

Run:
```bash
rm app/templates/campanha_acompanhar.html app/templates/_campanha_estado.html
```

- [ ] **Step 6: Commit**

```bash
git add app/templates/
git commit -m "feat(ui): novos templates de lista, formulario e detalhe de campanha"
```

---

## Task 15 — Rotas novas em `app/main.py`

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Remover rotas antigas de campanha**

Apagar do `app/main.py` as funções:
- `campanhas_form` (rota `GET /campanhas` antiga)
- `campanhas_disparar` (rota `POST /campanhas/disparar`)
- `campanhas_acompanhar` (rota `GET /campanhas/acompanhar/{perfil_id}`)
- `campanhas_estado` (rota `GET /campanhas/estado/{perfil_id}`)
- `campanhas_cancelar` (rota `POST /campanhas/cancelar/{perfil_id}`)

(Linhas ~676-735.)

- [ ] **Step 2: Adicionar import em `app/main.py`**

No bloco de imports do app:

```python
from . import campanhas
```

- [ ] **Step 3: Adicionar helper para perfis (com indicação de "em uso")**

Em `app/main.py`, junto aos outros helpers (perto de `_ufs_e_tribunais`):

```python
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
```

- [ ] **Step 4: Adicionar rotas novas — listar e formulário**

```python
@app.get("/campanhas", response_class=HTMLResponse)
def campanhas_lista(request: Request, user: dict = Depends(requer_login)):
    items = campanhas.listar()
    # filtra para mostrar só do usuário logado (a tabela campanhas não tem
    # usuario_id explícito, mas perfil_id já está restrito)
    perfis_do_user = {
        r["id"] for r in
        get_conn().__enter__().execute(
            "SELECT id FROM perfis_remetente WHERE usuario_id = ?", (user["id"],)
        ).fetchall()
    }
    items = [c for c in items if c["perfil_id"] in perfis_do_user]
    # adiciona enviados_hoje on-the-fly para a coluna "Hoje"
    for c in items:
        if c["status"] in ("ativa", "pausada"):
            c["enviados_hoje"] = campanhas.enviados_hoje_campanha(c["id"])
    return templates.TemplateResponse("campanhas.html", {
        "request": request, "user": user, "campanhas": items,
    })


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
```

> Nota: A leitura "perfis do usuário" no listar campanhas usa `get_conn().__enter__()` de forma feia. Substituir pelo padrão usado no resto do arquivo:
>
> ```python
>     with get_conn() as conn:
>         perfis_do_user = {r["id"] for r in conn.execute(
>             "SELECT id FROM perfis_remetente WHERE usuario_id = ?", (user["id"],)
>         ).fetchall()}
> ```

- [ ] **Step 5: Adicionar rota POST de criar**

```python
from datetime import time as _time


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
    # Confere ownership do perfil
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
```

- [ ] **Step 6: Rotas de detalhe e parcial**

```python
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


def _ctx_detalhe(user: dict, campanha_id: int) -> dict:
    """Monta o contexto para campanha_detalhe / parcial."""
    c = _confere_ownership(user["id"], campanha_id)
    # Hidrata com nome/email do perfil
    with get_conn() as conn:
        p = conn.execute(
            "SELECT nome, email_remetente FROM perfis_remetente WHERE id = ?",
            (c["perfil_id"],),
        ).fetchone()
        c["perfil_nome"] = p["nome"]
        c["perfil_email"] = p["email_remetente"]

        enviados_hoje = campanhas.enviados_hoje_campanha(campanha_id)
        progresso_pct = (c["enviados_total"] * 100 // c["total_alvo"]) if c["total_alvo"] else 0

        # Histórico por dia (últimos 30)
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

        # Últimos envios
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
    # quanto ainda cabe hoje
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
    # ativa
    if enviados_hoje >= c["por_dia"]:
        return "Quota diária atingida — retomará no próximo dia configurado"
    return "Ativa — aguardando próximo envio"


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
```

- [ ] **Step 7: Rotas de transição**

```python
@app.post("/campanhas/{campanha_id}/iniciar")
def campanha_iniciar(campanha_id: int, user: dict = Depends(requer_login)):
    _confere_ownership(user["id"], campanha_id)
    try:
        campanhas.iniciar(campanha_id)
    except ValueError as e:
        # redireciona com erro como query (simplificação)
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
        ctx = _ctx_detalhe(user, campanha_id)
        ufs, tribunais = _ufs_e_tribunais()
        ctx.update(
            request=request, user=user, erro=str(e),
            perfis=_perfis_para_form(user["id"]),
            ufs=ufs, tribunais=tribunais,
        )
        return templates.TemplateResponse("campanha_form.html", ctx)
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
```

- [ ] **Step 8: Smoke test manual completo**

Run: `uvicorn app.main:app --reload --port 8000`

Checklist (faça login e teste cada um):
1. `/campanhas` carrega vazio sem erro
2. `/campanhas/nova` carrega o form
3. Submit do form → redireciona para `/campanhas/{id}`, status `rascunho`, mostrando 0/total
4. Botão Editar muda total_alvo, salva
5. Botão Iniciar muda status para `ativa`. Logs do uvicorn mostram `[campanha N] thread iniciada`
6. Botão Pausar muda status para `pausada` em até ~30s
7. Botão Retomar volta para `ativa`
8. Botão Cancelar muda para `cancelada`
9. Tentar criar 2ª campanha no mesmo perfil enquanto a 1ª está ativa → o select mostra o perfil desabilitado

> A campanha de fato enviar e-mails depende de SMTP real. Não exigimos isso no smoke — basta confirmar transições e UI.

- [ ] **Step 9: Commit**

```bash
git add app/main.py
git commit -m "feat(routes): rotas de campanha (lista, criar, editar, detalhe, transicoes)"
```

---

## Task 16 — Filtro por campanha em `/historico`

**Files:**
- Modify: `app/main.py` (rota `/historico`), `app/templates/historico.html`

- [ ] **Step 1: Identificar a rota `/historico` em `app/main.py`**

Run: `grep -n "@app.get(\"/historico\"" app/main.py`

Localizar a função e os parâmetros de filtro existentes (status, perfil, datas).

- [ ] **Step 2: Adicionar parâmetro `campanha_id`**

Adicionar à assinatura:

```python
campanha_id: str = "",
```

E ao WHERE da query:

```python
if campanha_id and campanha_id.isdigit():
    where.append("e.campanha_id = ?"); args.append(int(campanha_id))
```

Adicionar ao contexto retornado:

```python
"campanhas_disponiveis": [dict(r) for r in conn.execute(
    "SELECT id, nome FROM campanhas ORDER BY id DESC"
).fetchall()],
"filtro_campanha_id": campanha_id,
```

- [ ] **Step 3: Adicionar select no template `historico.html`**

Localizar o `<form>` de filtros e adicionar antes do botão de submit:

```html
<label>Campanha
  <select name="campanha_id">
    <option value="">— todas —</option>
    {% for c in campanhas_disponiveis %}
      <option value="{{ c.id }}" {% if filtro_campanha_id == c.id|string %}selected{% endif %}>
        {{ c.nome }}
      </option>
    {% endfor %}
  </select>
</label>
```

- [ ] **Step 4: Smoke test manual**

Run: `uvicorn app.main:app --reload --port 8000`
- Acessar `/historico` — confirmar que o filtro de campanha aparece
- Selecionar uma campanha — URL ganha `?campanha_id=N`, lista filtra (vazia se ainda não enviou nada)

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/templates/historico.html
git commit -m "feat(historico): filtro por campanha"
```

---

## Task 17 — Auto-revisão e validação manual final

**Files:** nenhum.

- [ ] **Step 1: Rodar suite completa**

Run: `pytest -v`
Esperado: todos passam.

- [ ] **Step 2: Smoke test completo (manual, 10 min)**

Run: `uvicorn app.main:app --reload --port 8000`

Roteiro:
1. Login OK
2. `/perfis` mostra os perfis
3. `/agendamentos/novo` — não tem mais opção "campanha"
4. `/campanhas` vazio
5. Cria campanha "Teste-1" total=10, por_dia=2, dias=hoje, janela cobre agora
6. Detalhe mostra 0/10, status rascunho, 6 blocos visíveis
7. Edita total para 12 — salva, exibe novo valor
8. Inicia — status vira ativa, log mostra "thread iniciada"
9. (Se SMTP funcionar) Aguarda envios; barra avança; bloco "Hoje" atualiza via HTMX
10. Pausa — em ~30s status vira pausada
11. Retoma — volta para ativa
12. Cancela — vira cancelada
13. Cria 2ª campanha no mesmo perfil enquanto a 1ª está cancelada → consegue
14. `/historico?campanha_id=N` funciona

- [ ] **Step 3: Reidratação no boot**

- Crie uma campanha, inicie
- `Ctrl+C` no uvicorn
- Suba de novo: `uvicorn app.main:app --reload --port 8000`
- Logs mostram `[campanha N] reidratando`
- Dashboard segue refletindo "ativa" e a quota do dia continua sendo respeitada

- [ ] **Step 4: Verificação de cobertura do spec**

Reler `docs/superpowers/specs/2026-04-28-campanhas-persistentes-design.md` lado a lado com a UI/código:

- ✅ Campanha persistente com `total_alvo, por_dia, dias_semana, janela_*, status, pausa_motivo, enviados_total`
- ✅ Uma por perfil ativa/pausada (índice parcial único)
- ✅ Worker = daemon thread + reidratação
- ✅ Sleep cooperativo (chunks de 30s)
- ✅ Erros: fatal pausa, transiente faz retry, por_contato segue
- ✅ UI: lista, formulário, detalhe com 6 blocos, auto-refresh 5s
- ✅ `/agendamentos` sem opção campanha
- ✅ `/historico` com filtro por campanha
- ✅ Tracking pixel, IMAP, histórico por vara — sem mudança

Se algum item ficar pendente, abrir um followup task aqui.

- [ ] **Step 5: Commit final (vazio se nada mudou) + tag opcional**

```bash
git status
# se algo: git add . && git commit -m "chore: ajustes finais pos-validacao"
```

---

## Self-Review Notes

- **Spec coverage:** todos os requisitos da seção "Critérios de sucesso" do spec viram tarefas (1, 2 → schema; 3-7, 9 → CRUD/transições; 11 → loop com retry; 12 → reidratação; 14-15 → UI; 13 → /agendamentos; 16 → /historico; 17 → validação).
- **Placeholders:** sem TBD/TODO. Cada step tem código completo ou comando exato.
- **Type consistency:** `EstadoCampanha`, `Acao`, `AcaoTipo`, `ErroSmtp` definidos na Task 5 / Task 4 e usados em Tasks 9, 11. Funções `obter`, `criar`, `iniciar`, etc. definidas em ordem antes de serem usadas.
- **Risco conhecido:** o teste de unicidade na Task 2 cria uma campanha com `status='ativa'` direto via SQL — a Task 7 introduz a regra "iniciar não permite duas". Está OK porque os testes são separados (um valida o índice do banco, o outro a função Python).
