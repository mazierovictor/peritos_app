# Scraper TJGO + integração ao peritos_app — Design

**Data:** 2026-06-16
**Status:** aprovado para implementação

## Objetivo

Criar um scraper para o Tribunal de Justiça de Goiás (TJGO) que coleta e-mails de
órgãos jurisdicionais e integrá-lo ao `peritos_app`, seguindo o mesmo padrão dos
outros 11 scrapers (registry → external_script → XLSX → importação no banco).

## Fonte de dados

Diferente dos demais TJs (scraping de HTML), o SIGO (Sistema de Gestão de Órgãos)
do TJGO expõe uma **API JSON pública, sem autenticação**, descoberta a partir do
front-end React/Vite que serve o código-fonte em modo de desenvolvimento
(`https://sigo.tjgo.jus.br/agenda-eletronica`).

- **Endpoint:** `GET https://sigo-backend.tjgo.jus.br/api/agenda/publico/localidades`
- **Parâmetros:** `page` (0-based), `size` (limitado a ~2000 pelo servidor).
  Filtros opcionais aceitos pelo backend: `idTipo, nome, cidade, bairro, telefone, email`.
- **Resposta:** `{ success, data: [...], page: { number, size, totalElements, totalPages, hasNext, hasPrevious }, meta, messages }`
- **Volume:** ~4600 lotações no total; 2548 com e-mail válido; 1849 e-mails distintos; 307 cidades.
- **Detalhe por ID (não usado nesta versão):** `GET /api/agenda/publico/lotacoes/{id}`.

### Campos relevantes por registro
- `nome` — nome do órgão/lotação (→ coluna Órgão)
- `email` — e-mail institucional (→ coluna E-mail)
- `predio.cidade` — cidade (→ coluna Cidade)
- `tipo.descricao` — tipo da lotação (pouco discriminante: 1454 "Outros", 273 "Sem Informação", 83 "Forum", 83 "Distrito", 81 "Comarca", 26 "Gabinete Desembargador"); **não** usado para filtrar
- `telefones[]`, `site` — ignorados nesta versão

### Observações técnicas
- **Encoding:** a resposta é UTF-8 correto. O "mojibake" visto no console é
  artefato de exibição do terminal Windows (cp1252), não do dado. `openpyxl`
  grava os acentos corretamente.
- **Estabilidade:** o servidor ocasionalmente encerra a conexão no meio
  (`Response ended prematurely` / `ChunkedEncodingError`). Exige **retry com
  backoff** por página (já é padrão nos outros scrapers).

## Decisão: estratégia de filtragem (foco jurisdicional + configurável)

A base do TJGO retorna a instituição inteira — não só varas. Entre os 2548
registros com e-mail há 568 órgãos administrativos (Núcleos, Diretorias,
Coordenadorias), além de escola judicial, creche, centro de saúde, ouvidorias,
**promotorias do Ministério Público** e **cartórios extrajudiciais**
(Tabelionatos/Registros). Para uma base de captação de perícias, o foco é em
órgãos onde peritos são nomeados.

**Lógica `is_organ_allowed(nome)`:**
1. Se `_excluir_orgao(norm)` → descarta (criminal/penal ou infância/juventude
   puro, **com override cumulativo cível** — reusa o helper idêntico aos outros
   scrapers: `_EXC_CRIMINAL` / `_EXC_INFANCIA` / `_CIVEL_OVERRIDE`).
2. Se o nome contém `vara`, `juizado` ou `jurisdicional` → mantém.
3. Senão, mantém se o nome contém algum termo da **allowlist** (`ALLOWED_ORGANS`).

**Allowlist default (a calibrar com testes):** `vara, juizado, jurisdicional,
cejusc, turma recursal, forum, contadoria, juri, auditoria militar`.

> **Calibração necessária (validada contra dados reais):** o termo genérico
> `secretaria` **não** entra no default — ele captura dezenas de secretarias
> administrativas ("Secretaria Executiva da Diretoria Administrativa",
> "Secretaria da Ouvidoria da Mulher"). Já `forum` **deve** entrar (senão
> "Fórum de Abadiânia" é descartado). Os termos finais serão fixados durante a
> implementação, validando o resultado contra a amostra real e os casos do teste.

**Configurável pela UI:** a allowlist vem de `configs.py` (`palavras_chave`) e é
editável na tela de configuração do scraper, exatamente como TJMG/TJSP. O script
lê `scraper_config.json` do cwd e, se houver `palavras_chave` não-vazia,
substitui o default — sem alterar a lógica.

## Saída XLSX

- Arquivo: `tjgo_guia_judiciario.xlsx` (gerado no cwd).
- Colunas: **Cidade | Órgão | E-mail** (idêntico aos outros; compatível com
  `runner._importar_xlsx` e `_detectar_colunas`).
- Estilo visual padrão: cabeçalho azul (`003366`), freeze panes em `A2`,
  autofilter, linhas com preenchimento alternado.
- **Duplicatas:** uma linha por órgão que passa no filtro, mesmo com e-mail
  repetido (decisão do usuário). O runner faz upsert por `(email, tribunal)`,
  então no banco fica um registro por e-mail.
- Geração **do zero a cada execução** (baixa tudo via API; sem update
  incremental — mais simples que o fluxo HTML do TJMG e suficiente aqui).

## Pontos de integração no peritos_app

1. **`app/scrapers/registry.py`** — adicionar:
   `ScraperInfo("tjgo", "TJ Goiás", "GO", "Projudi", "tjgo_scraper.py", "tjgo_guia_judiciario.xlsx")`
   (`requer_browser=False`, `manual=False`).
2. **`app/scrapers/configs.py`** — `SCHEMAS["tjgo"] = ConfigSchema("Tipos de órgão
   aceitos", "...")` e `DEFAULTS["tjgo"] = [allowlist]`.
3. **`app/scrapers/external_scripts/tjgo_scraper.py`** — o script executado pelo runner.
4. **`tjgo_scraper.py`** na raiz do projeto `webscraping_tjmg/` — cópia, por
   consistência com os outros (a fonte canônica é a de `external_scripts/`).
5. **`tests/test_scrapers_filtros.py`** — registrar `"tjgo": "is_organ_allowed"`
   em `PREDICADO`; o scraper passa nos mesmos `CASOS` já existentes.

## Tratamento de erros

- Retry 3–4× com backoff (≈3–10s) por página; timeout 60s; pequeno delay entre páginas.
- Se a paginação falhar definitivamente, aborta com exit ≠ 0 → o runner marca a
  execução como `erro` e preserva o log.
- Se a estrutura da resposta mudar (ausência de `data`/`page`), erro claro.

## Testes

- **Filtragem:** o predicado `is_organ_allowed` do TJGO entra em
  `test_scrapers_filtros.py` e deve satisfazer todos os `CASOS` (varas genéricas
  mantidas; criminal/penal/infância puro descartado; cumulativas cíveis mantidas).
- **Calibração:** verificação manual da allowlist contra a amostra real durante a
  implementação (não vai para o CI por depender de rede).

## Fora de escopo (YAGNI)

- Coleta de telefone/endereço (só 3 colunas).
- Detalhe por ID (`/lotacoes/{id}`) — a lista já traz e-mail.
- Update incremental do XLSX.
- Uso dos endpoints autenticados ou do SSO Keycloak.
