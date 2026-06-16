# Scraper TJGO Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Criar o scraper do TJGO que coleta e-mails de órgãos jurisdicionais via API JSON pública do SIGO e integrá-lo ao `peritos_app`.

**Architecture:** Um script Python isolado (`tjgo_scraper.py`) consome a API pública paginada `https://sigo-backend.tjgo.jus.br/api/agenda/publico/localidades` com `requests`, filtra órgãos pela mesma política dos outros scrapers (exclui criminal/penal/infância puro; allowlist jurisdicional configurável), e grava `tjgo_guia_judiciario.xlsx` (Cidade | Órgão | E-mail). O `peritos_app` o registra em `registry.py`/`configs.py` e o executa via o runner existente, que importa o XLSX no banco.

**Tech Stack:** Python 3.14, `requests`, `openpyxl`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-06-16-tjgo-scraper-design.md`

---

## File Structure

- **Create** `app/scrapers/external_scripts/tjgo_scraper.py` — script canônico executado pelo runner. Responsável por: filtragem, fetch paginado, montagem das linhas e geração do XLSX.
- **Create** `tests/test_tjgo_scraper.py` — testes unitários de `extract_rows` e `fetch_all_localidades` (sem rede; usando dados mock e sessão fake).
- **Create** `tjgo_scraper.py` (raiz do projeto `webscraping_tjmg/`) — cópia idêntica do canônico, por consistência com os outros scrapers.
- **Modify** `tests/test_scrapers_filtros.py` — registrar `"tjgo": "is_organ_allowed"` no dict `PREDICADO`.
- **Modify** `app/scrapers/registry.py` — adicionar a entrada `ScraperInfo` do TJGO.
- **Modify** `app/scrapers/configs.py` — adicionar `SCHEMAS["tjgo"]` e `DEFAULTS["tjgo"]`.

Todos os comandos abaixo assumem o diretório de trabalho `peritos_app/` (onde fica `pytest.ini` e o pacote `app/`). O git é o repositório de `peritos_app` (branch `feat/tjgo-scraper`).

---

### Task 1: Filtragem jurisdicional do TJGO (TDD via teste de filtros)

**Files:**
- Modify: `tests/test_scrapers_filtros.py` (dict `PREDICADO`)
- Create: `app/scrapers/external_scripts/tjgo_scraper.py`

- [ ] **Step 1: Registrar o predicado do TJGO no teste de filtros**

Em `tests/test_scrapers_filtros.py`, no dict `PREDICADO`, adicionar a linha do `tjgo` (mantendo as demais):

```python
PREDICADO = {
    "tjmg":  "is_organ_allowed",
    "tjrs":  "is_organ_allowed",
    "tjsp":  "is_organ_allowed",
    "tjsc":  "is_organ_allowed",
    "tjpa":  "is_valid_organ",
    "tjdft": "is_valid_organ",
    "tjal":  "is_valid_organ",
    "tjgo":  "is_organ_allowed",
}
```

- [ ] **Step 2: Rodar o teste e verificar que falha**

Run: `python -m pytest tests/test_scrapers_filtros.py -q -k tjgo`
Expected: FAIL na coleta/execução — `_load("tjgo")` não encontra o arquivo
`app/scrapers/external_scripts/tjgo_scraper.py` (FileNotFoundError).

- [ ] **Step 3: Criar o script com o bloco de configuração e filtragem**

Criar `app/scrapers/external_scripts/tjgo_scraper.py` com o conteúdo abaixo
(este é o cabeçalho + filtragem; fetch/excel/main são adicionados nas tarefas
seguintes). O helper `_EXC_CRIMINAL`/`_EXC_INFANCIA`/`_CIVEL_OVERRIDE`/
`_excluir_orgao` é **idêntico** ao dos outros scrapers (ver memória
`politica-filtragem-scrapers`).

```python
"""
TJGO - Agenda Eletrônica (SIGO) - Web Scraper
==============================================
Coleta e-mails de órgãos jurisdicionais do TJGO a partir da API JSON pública:
  https://sigo-backend.tjgo.jus.br/api/agenda/publico/localidades

Gera tjgo_guia_judiciario.xlsx com as colunas: Cidade | Órgão | E-mail

Dependências:
    pip install requests openpyxl
"""
from __future__ import annotations

import time
import unicodedata
import logging

import requests
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

# ──────────────────────────────────────────────
# Configurações
# ──────────────────────────────────────────────
API_URL = "https://sigo-backend.tjgo.jus.br/api/agenda/publico/localidades"
OUTPUT_FILE = "tjgo_guia_judiciario.xlsx"
PAGE_SIZE = 1000              # o servidor limita ~2000 por página
DELAY_BETWEEN_REQUESTS = 1    # segundos entre páginas (respeite o servidor)
MAX_RETRIES = 4
RETRY_DELAY = 5              # segundos entre tentativas

# Allowlist jurisdicional (sem acento, minúsculas). Calibrada contra os dados
# reais: 'secretaria' fica DE FORA (captura secretarias administrativas);
# 'forum' fica DENTRO (senão perde os fóruns das comarcas).
ALLOWED_ORGANS = [
    "vara",
    "juizado",
    "jurisdicional",
    "cejusc",
    "turma recursal",
    "forum",
    "contadoria",
    "tribunal do juri",
    "auditoria militar",
]

# ─── Override pela UI (não altera a lógica; só substitui a lista se houver config) ───
try:
    import json as _json_ui
    with open("scraper_config.json", encoding="utf-8") as _f_ui:
        _UI_CFG = _json_ui.load(_f_ui)
    if isinstance(_UI_CFG.get("palavras_chave"), list) and _UI_CFG["palavras_chave"]:
        ALLOWED_ORGANS = [str(x) for x in _UI_CFG["palavras_chave"]]
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# ──────────────────────────────────────────────
# Filtragem (idêntica aos demais scrapers)
# ──────────────────────────────────────────────
_EXC_CRIMINAL = (
    "criminal", "criminais", "crime", "penal", "penais", "penas",
    "execucao penal", "execucoes penais", "socioeducativ", "socieducativ",
    "do juri", "de juri", "juiz de garantias", "juizo de garantias",
)
_EXC_INFANCIA = ("infancia", "juventude")
_CIVEL_OVERRIDE = (
    "civel", "civil", "fazenda", "fiscal", "fiscais", "familia", "sucessoes", "orfaos",
    "empresarial", "unica", "unico", "jurisdicional", "precatoria", "divida",
    "falencia", "recupera", "acidente",
)


def normalize_text(text: str) -> str:
    """Remove acentos e converte para minúsculas para facilitar a busca."""
    text = unicodedata.normalize("NFD", text or "")
    text = text.encode("ascii", "ignore").decode("utf-8")
    return text.lower()


def _excluir_orgao(nome_norm: str) -> bool:
    """True se a unidade for exclusivamente criminal/penal ou de infância/juventude.
    Recebe o nome JÁ normalizado (minúsculo, sem acento)."""
    suspeito = (any(t in nome_norm for t in _EXC_CRIMINAL)
                or any(t in nome_norm for t in _EXC_INFANCIA))
    if not suspeito:
        return False
    return not any(t in nome_norm for t in _CIVEL_OVERRIDE)


def is_organ_allowed(orgao_name: str) -> bool:
    """Mantém qualquer vara/juizado/unidade jurisdicional (genérica inclusive) e
    os órgãos de apoio configurados em ALLOWED_ORGANS; descarta criminal/infância
    puros."""
    norm_name = normalize_text(orgao_name).strip()
    if _excluir_orgao(norm_name):
        return False
    if "vara" in norm_name or "juizado" in norm_name or "jurisdicional" in norm_name:
        return True
    return any(keyword in norm_name for keyword in ALLOWED_ORGANS)
```

- [ ] **Step 4: Rodar o teste e verificar que passa**

Run: `python -m pytest tests/test_scrapers_filtros.py -q -k tjgo`
Expected: PASS (14 casos do `tjgo`). Confirma que varas genéricas numeradas são
mantidas, criminal/penal/infância puro é descartado e cumulativas cíveis são mantidas.

- [ ] **Step 5: Commit**

```bash
git add tests/test_scrapers_filtros.py app/scrapers/external_scripts/tjgo_scraper.py
git commit -m "feat(tjgo): filtragem jurisdicional do scraper TJGO"
```

---

### Task 2: Extração das linhas (filtra + mapeia colunas)

**Files:**
- Modify: `app/scrapers/external_scripts/tjgo_scraper.py` (adicionar `extract_rows`)
- Create: `tests/test_tjgo_scraper.py`

- [ ] **Step 1: Escrever o teste de `extract_rows`**

Criar `tests/test_tjgo_scraper.py` com:

```python
"""Testes do scraper TJGO (extração e fetch), sem acesso à rede."""
from __future__ import annotations

import importlib.util
import pathlib

BASE = pathlib.Path(__file__).resolve().parents[1] / "app" / "scrapers" / "external_scripts"


def _load():
    path = BASE / "tjgo_scraper.py"
    spec = importlib.util.spec_from_file_location("_scr_tjgo", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _loc(nome, email, cidade="Goiânia"):
    return {"nome": nome, "email": email, "predio": {"cidade": cidade}}


def test_extract_rows_mapeia_colunas_e_filtra():
    mod = _load()
    localidades = [
        _loc("2ª Vara Cível de Formosa", "vara2@tjgo.jus.br", "Formosa"),
        _loc("Núcleo de Telecomunicações", "divitel@tjgo.jus.br"),   # administrativo -> fora
        _loc("1ª Vara Criminal de Goiânia", "crim@tjgo.jus.br"),     # criminal puro -> fora
        _loc("Fórum de Abadiânia", "forum@tjgo.jus.br", "Abadiânia"),
        _loc("Vara sem email", ""),                                   # sem e-mail -> fora
        _loc("Diretoria Administrativa", "texto-sem-arroba"),         # e-mail inválido -> fora
    ]
    rows = mod.extract_rows(localidades)
    assert rows == [
        {"cidade": "Formosa", "orgao": "2ª Vara Cível de Formosa", "email": "vara2@tjgo.jus.br"},
        {"cidade": "Abadiânia", "orgao": "Fórum de Abadiânia", "email": "forum@tjgo.jus.br"},
    ]


def test_extract_rows_predio_ausente_vira_cidade_vazia():
    mod = _load()
    rows = mod.extract_rows([{"nome": "Vara Única", "email": "vu@tjgo.jus.br", "predio": None}])
    assert rows == [{"cidade": "", "orgao": "Vara Única", "email": "vu@tjgo.jus.br"}]
```

- [ ] **Step 2: Rodar o teste e verificar que falha**

Run: `python -m pytest tests/test_tjgo_scraper.py -q`
Expected: FAIL com `AttributeError: module '_scr_tjgo' has no attribute 'extract_rows'`.

- [ ] **Step 3: Implementar `extract_rows`**

Adicionar ao final de `app/scrapers/external_scripts/tjgo_scraper.py`:

```python
# ──────────────────────────────────────────────
# Extração das linhas a partir das lotações da API
# ──────────────────────────────────────────────
def extract_rows(localidades: list[dict]) -> list[dict]:
    """Filtra as lotações pela política e mapeia para {cidade, orgao, email}.
    Uma linha por órgão que passa no filtro (e-mails repetidos são preservados)."""
    rows: list[dict] = []
    for loc in localidades:
        email = (loc.get("email") or "").strip()
        if not email or "@" not in email:
            continue
        nome = (loc.get("nome") or "").strip()
        if not nome or not is_organ_allowed(nome):
            continue
        predio = loc.get("predio") or {}
        cidade = (predio.get("cidade") or "").strip()
        rows.append({"cidade": cidade, "orgao": nome, "email": email})
    return rows
```

- [ ] **Step 4: Rodar o teste e verificar que passa**

Run: `python -m pytest tests/test_tjgo_scraper.py -q`
Expected: PASS (2 testes).

- [ ] **Step 5: Commit**

```bash
git add app/scrapers/external_scripts/tjgo_scraper.py tests/test_tjgo_scraper.py
git commit -m "feat(tjgo): extract_rows (filtra lotações e mapeia colunas)"
```

---

### Task 3: Fetch paginado com retry

**Files:**
- Modify: `app/scrapers/external_scripts/tjgo_scraper.py` (adicionar `fetch_all_localidades`)
- Modify: `tests/test_tjgo_scraper.py` (testes com sessão fake)

- [ ] **Step 1: Escrever os testes de `fetch_all_localidades`**

Adicionar ao final de `tests/test_tjgo_scraper.py`:

```python
class _FakeResp:
    def __init__(self, payload, fail=False):
        self._payload = payload
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            import requests
            raise requests.RequestException("boom")

    def json(self):
        return self._payload


class _FakeSession:
    """Sessão fake: serve páginas e pode falhar N vezes antes de cada sucesso."""
    def __init__(self, paginas, falhas_por_pagina=0):
        self._paginas = paginas
        self._falhas = falhas_por_pagina
        self._falhas_restantes = falhas_por_pagina
        self.chamadas = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.chamadas += 1
        if self._falhas_restantes > 0:
            self._falhas_restantes -= 1
            return _FakeResp(None, fail=True)
        self._falhas_restantes = self._falhas
        page = params["page"]
        return _FakeResp(self._paginas[page])


def _pagina(itens, has_next):
    return {"success": True, "data": itens,
            "page": {"number": 0, "size": 1000, "totalElements": 0,
                     "totalPages": 0, "hasNext": has_next, "hasPrevious": False}}


def test_fetch_pagina_ate_hasnext_false(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
    paginas = [
        _pagina([{"nome": "A"}], has_next=True),
        _pagina([{"nome": "B"}], has_next=False),
    ]
    sess = _FakeSession(paginas)
    todos = mod.fetch_all_localidades(sess)
    assert [d["nome"] for d in todos] == ["A", "B"]


def test_fetch_faz_retry_e_recupera(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
    paginas = [_pagina([{"nome": "A"}], has_next=False)]
    sess = _FakeSession(paginas, falhas_por_pagina=2)  # falha 2x, sucede na 3ª
    todos = mod.fetch_all_localidades(sess)
    assert [d["nome"] for d in todos] == ["A"]
    assert sess.chamadas == 3


def test_fetch_estoura_apos_max_retries(monkeypatch):
    import pytest
    mod = _load()
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)
    paginas = [_pagina([{"nome": "A"}], has_next=False)]
    sess = _FakeSession(paginas, falhas_por_pagina=99)  # nunca sucede
    with pytest.raises(RuntimeError):
        mod.fetch_all_localidades(sess)
```

- [ ] **Step 2: Rodar os testes e verificar que falham**

Run: `python -m pytest tests/test_tjgo_scraper.py -q -k fetch`
Expected: FAIL com `AttributeError: module '_scr_tjgo' has no attribute 'fetch_all_localidades'`.

- [ ] **Step 3: Implementar `fetch_all_localidades`**

Adicionar ao final de `app/scrapers/external_scripts/tjgo_scraper.py`:

```python
# ──────────────────────────────────────────────
# Fetch paginado da API pública (com retry/backoff)
# ──────────────────────────────────────────────
def fetch_all_localidades(session: requests.Session) -> list[dict]:
    """Pagina por /agenda/publico/localidades até hasNext=False. Retorna a lista
    bruta de lotações. Levanta RuntimeError se uma página falhar após MAX_RETRIES."""
    todos: list[dict] = []
    page = 0
    while True:
        params = {"page": page, "size": PAGE_SIZE}
        payload = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = session.get(API_URL, params=params, headers=HEADERS, timeout=60)
                resp.raise_for_status()
                payload = resp.json()
                break
            except (requests.RequestException, ValueError) as exc:
                if attempt < MAX_RETRIES - 1:
                    log.warning("Falha na página %d: %s. Retentando em %ds (%d/%d)...",
                                page, exc, RETRY_DELAY, attempt + 2, MAX_RETRIES)
                    time.sleep(RETRY_DELAY)
                else:
                    raise RuntimeError(
                        f"Falha ao buscar a página {page} após {MAX_RETRIES} tentativas: {exc}"
                    ) from exc

        data = payload.get("data") or []
        todos.extend(data)
        page_info = payload.get("page") or {}
        log.info("Página %d: %d registros (acumulado: %d/%s)",
                 page, len(data), len(todos), page_info.get("totalElements", "?"))

        if not page_info.get("hasNext"):
            break
        page += 1
        time.sleep(DELAY_BETWEEN_REQUESTS)

    return todos
```

- [ ] **Step 4: Rodar os testes e verificar que passam**

Run: `python -m pytest tests/test_tjgo_scraper.py -q`
Expected: PASS (5 testes no total).

- [ ] **Step 5: Commit**

```bash
git add app/scrapers/external_scripts/tjgo_scraper.py tests/test_tjgo_scraper.py
git commit -m "feat(tjgo): fetch paginado da API com retry/backoff"
```

---

### Task 4: Geração do XLSX + main + smoke real

**Files:**
- Modify: `app/scrapers/external_scripts/tjgo_scraper.py` (adicionar `write_excel`, `main`, `__main__`)

- [ ] **Step 1: Implementar `write_excel` e `main`**

Adicionar ao final de `app/scrapers/external_scripts/tjgo_scraper.py`:

```python
# ──────────────────────────────────────────────
# Geração da planilha Excel
# ──────────────────────────────────────────────
def write_excel(rows: list[dict], filename: str) -> None:
    """Gera a planilha do zero com as colunas Cidade | Órgão | E-mail."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Guia Judiciário TJGO"

    header_font = Font(name="Calibri", bold=True, color="FFFFFF", size=12)
    header_fill = PatternFill("solid", fgColor="003366")
    header_align = Alignment(horizontal="center", vertical="center")

    headers = ["Cidade", "Órgão", "E-mail"]
    column_widths = [30, 65, 40]
    for col_idx, (header_text, width) in enumerate(zip(headers, column_widths), start=1):
        cell = ws.cell(row=1, column=col_idx, value=header_text)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        ws.column_dimensions[cell.column_letter].width = width

    ws.row_dimensions[1].height = 20
    ws.freeze_panes = "A2"

    alt_fill = PatternFill("solid", fgColor="E8F0FE")
    plain_fill = PatternFill("solid", fgColor="FFFFFF")

    for i, record in enumerate(rows, start=2):
        fill = alt_fill if i % 2 == 0 else plain_fill
        for col_idx, key in enumerate(["cidade", "orgao", "email"], start=1):
            cell = ws.cell(row=i, column=col_idx, value=record[key])
            cell.fill = fill
            cell.alignment = Alignment(vertical="center")

    ws.auto_filter.ref = f"A1:C{max(2, ws.max_row)}"
    wb.save(filename)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main() -> None:
    session = requests.Session()
    session.headers.update(HEADERS)

    log.info("Buscando localidades públicas do TJGO...")
    localidades = fetch_all_localidades(session)
    log.info("Total bruto: %d lotações.", len(localidades))

    rows = extract_rows(localidades)
    log.info("Após filtragem: %d órgãos com e-mail.", len(rows))

    write_excel(rows, OUTPUT_FILE)
    log.info("Planilha '%s' gerada com %d linhas.", OUTPUT_FILE, len(rows))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Rodar a suíte unitária (não deve quebrar nada)**

Run: `python -m pytest tests/test_tjgo_scraper.py tests/test_scrapers_filtros.py -q`
Expected: PASS (todos).

- [ ] **Step 3: Smoke real contra a API (verificação manual)**

Run (de dentro de `peritos_app/`):
`python app/scrapers/external_scripts/tjgo_scraper.py`
Expected: logs de paginação ("Página 0: ... 1000 registros", etc.), depois
"Após filtragem: ~641 órgãos com e-mail." e "Planilha 'tjgo_guia_judiciario.xlsx'
gerada com ~641 linhas." Confirma que o arquivo `tjgo_guia_judiciario.xlsx` foi
criado no diretório atual.

Verificar a planilha:
```bash
python -c "import openpyxl; wb=openpyxl.load_workbook('tjgo_guia_judiciario.xlsx'); ws=wb.active; print('linhas:', ws.max_row); print([c.value for c in ws[1]]); print([ws.cell(2,i).value for i in (1,2,3)])"
```
Expected: cabeçalho `['Cidade', 'Órgão', 'E-mail']`, segunda linha com cidade/órgão/e-mail
acentuados corretamente (sem mojibake), e `linhas` na casa de ~640.

- [ ] **Step 4: Remover o XLSX de smoke (não versionar)**

```bash
rm -f tjgo_guia_judiciario.xlsx
```

- [ ] **Step 5: Commit**

```bash
git add app/scrapers/external_scripts/tjgo_scraper.py
git commit -m "feat(tjgo): geração do XLSX e main (script completo)"
```

---

### Task 5: Registrar o scraper no registry

**Files:**
- Modify: `app/scrapers/registry.py` (lista `SCRAPERS`)

- [ ] **Step 1: Adicionar a entrada do TJGO**

Em `app/scrapers/registry.py`, dentro da lista passada à compreensão de `SCRAPERS`,
adicionar a entrada do TJGO (após a linha do `tjsc`):

```python
        ScraperInfo("tjgo", "TJ Goiás",              "GO", "Projudi", "tjgo_scraper.py",  "tjgo_guia_judiciario.xlsx"),
```

- [ ] **Step 2: Verificar que o registry resolve o TJGO**

Run: `python -c "from app.scrapers.registry import get; i=get('tjgo'); print(i.sigla, i.nome, i.estado, i.sistema, i.script, i.xlsx, i.requer_browser, i.manual)"`
Expected: `tjgo TJ Goiás GO Projudi tjgo_scraper.py tjgo_guia_judiciario.xlsx False False`

- [ ] **Step 3: Commit**

```bash
git add app/scrapers/registry.py
git commit -m "feat(tjgo): registrar TJGO no registry de scrapers"
```

---

### Task 6: Configuração editável (allowlist pela UI)

**Files:**
- Modify: `app/scrapers/configs.py` (`SCHEMAS` e `DEFAULTS`)

- [ ] **Step 1: Adicionar o schema e os defaults do TJGO**

Em `app/scrapers/configs.py`, no dict `SCHEMAS`, adicionar:

```python
    "tjgo":  ConfigSchema("Tipos de órgão aceitos",
                          "Só entram na base os órgãos cujo nome contém algum destes termos "
                          "(além de qualquer vara/juizado). Criminal/penal e infância/juventude "
                          "puros são sempre descartados."),
```

E no dict `DEFAULTS`, adicionar (deve refletir o `ALLOWED_ORGANS` do script):

```python
    "tjgo": [
        "vara", "juizado", "jurisdicional", "cejusc", "turma recursal",
        "forum", "contadoria", "tribunal do juri", "auditoria militar",
    ],
```

- [ ] **Step 2: Verificar a config em runtime**

Run: `python -c "from app.scrapers import configs as c; print(c.schema('tjgo').label); print(c.palavras_chave('tjgo')); print(c.get_runtime_config('tjgo'))"`
Expected: `Tipos de órgão aceitos`, depois a lista de termos, depois
`{'palavras_chave': [...]}`.

- [ ] **Step 3: Commit**

```bash
git add app/scrapers/configs.py
git commit -m "feat(tjgo): config editável da allowlist (configs.py)"
```

---

### Task 7: Cópia na raiz do projeto + verificação final

**Files:**
- Create: `../tjgo_scraper.py` (raiz `webscraping_tjmg/`, cópia do canônico)

- [ ] **Step 1: Copiar o script canônico para a raiz**

```bash
cp app/scrapers/external_scripts/tjgo_scraper.py ../tjgo_scraper.py
```

- [ ] **Step 2: Confirmar que as duas cópias são idênticas**

Run: `git -C .. hash-object ../tjgo_scraper.py; git hash-object app/scrapers/external_scripts/tjgo_scraper.py`
Expected: os dois hashes são iguais (conteúdo idêntico). (A cópia da raiz fica no
repositório `webscraping_tjmg/`, fora do git de `peritos_app/`.)

- [ ] **Step 3: Rodar a suíte de testes completa do peritos_app**

Run: `python -m pytest -q`
Expected: PASS — incluindo os novos `tests/test_tjgo_scraper.py` (5) e os casos
`tjgo` em `tests/test_scrapers_filtros.py` (14), sem regressões nos demais.

- [ ] **Step 4: Commit do script da raiz (no git de webscraping_tjmg, se houver)**

O diretório raiz `webscraping_tjmg/` não é um repositório git (apenas `peritos_app/`
é). Portanto a cópia da raiz não é versionada — basta deixá-la no lugar. Nenhum
commit é necessário para este passo.

---

## Self-Review

**Spec coverage:**
- Fonte/endpoint público → Task 1 (URL/HEADERS), Task 3 (fetch). ✓
- Filtragem foco jurisdicional + helper de exclusão → Task 1. ✓
- Configurável pela UI → Task 1 (leitura de `scraper_config.json`) + Task 6 (`configs.py`). ✓
- Saída XLSX 3 colunas, estilo padrão, do zero → Task 4. ✓
- Uma linha por órgão (duplicatas preservadas) → Task 2 (`extract_rows`). ✓
- Integração registry → Task 5; external_script → Tasks 1-4; cópia raiz → Task 7; teste de filtros → Task 1. ✓
- Tratamento de erros (retry/backoff, exit≠0) → Task 3. ✓
- Calibração da allowlist (sem 'secretaria', com 'forum') → Task 1 (comentário + lista) e validada no smoke da Task 4. ✓

**Placeholder scan:** nenhum TBD/TODO; todo código está completo e inline.

**Type consistency:** `is_organ_allowed`, `extract_rows`, `fetch_all_localidades`,
`write_excel`, `main`, `ALLOWED_ORGANS`, `HEADERS`, `API_URL`, `PAGE_SIZE`,
`MAX_RETRIES`, `RETRY_DELAY` usados de forma consistente entre tarefas e testes.
`DEFAULTS["tjgo"]` (Task 6) reflete exatamente `ALLOWED_ORGANS` (Task 1).
