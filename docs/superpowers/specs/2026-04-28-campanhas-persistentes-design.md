# Campanhas persistentes com quota diária e janela de envio

**Data:** 2026-04-28
**Status:** spec aprovado, aguardando plano de implementação

## Problema

Hoje no `peritos_app`, "campanha" e "agendamento" são dois conceitos separados que confundem o usuário:

- **Campanha** (`/campanhas`) = uma execução única. Você dispara, ela manda até bater o limite diário do perfil ou o `total_alvo` (1–500), e termina. Estado vive só na memória da thread; some no redeploy.
- **Agendamento** (`/agendamentos`) = repete um disparo de campanha em frequência fixa (diário/semanal/mensal). Cada execução é um "tiro" novo, sem noção de progresso acumulado.

O usuário quer pensar em **objetivos**: "mandar 1.000 e-mails de TJSP, no ritmo de 200 por dia, em dias úteis, e poder acompanhar com transparência se está saindo direito". Esse modelo não existe — exige montar agendamento + cuidar manualmente do total + olhar histórico para saber o progresso.

## Visão geral da solução

**Campanha vira o objeto principal e persistente.** Ela passa a ter:

- Meta total (`total_alvo`)
- Quota diária (`por_dia`)
- Dias da semana permitidos (`dias_semana`)
- Janela horária dentro do dia (`janela_inicio`, `janela_fim`)
- Status ciclo de vida: `rascunho` → `ativa` → (`pausada` ↔ `ativa`)* → `concluida` | `cancelada`

O sistema toca a campanha sozinho até completar `total_alvo`, espalhando os envios uniformemente ao longo da janela em cada dia ativo. **Apenas uma campanha ativa ou pausada por perfil de remetente** — quem precisar paralelismo cria outro perfil.

Agendamentos passam a ser usados **só para scrapers**. O tipo `campanha` é removido de `/agendamentos`.

## Decisões de design (com os "porquês")

### 1. Apenas uma campanha por perfil
Mantém o limite diário do perfil sob controle (sem contenda entre múltiplas campanhas no mesmo Gmail) e elimina decisões complexas de "quem prioriza" quando duas campanhas competem pela mesma quota SMTP. Simples, e o usuário pode contornar criando perfis adicionais.

### 2. Janela horária com envios espalhados
Em vez de despejar 200 e-mails em ~50min começando às 09:00, a campanha distribui os 200 ao longo da janela escolhida (ex.: 09:00–17:00, ~25/h). Comportamento mais "humano" para o Gmail, e a UI consegue mostrar próximo envio em "x minutos".

### 3. Worker = daemon thread por campanha ativa, com reidratação no boot
Escolhido pelo usuário em vez de tick global. Em redeploy/restart, o app lê todas as campanhas com `status='ativa'` e sobe uma thread por campanha. Pausadas ficam paradas até intervenção manual.

### 4. Sleep cooperativo
A thread dorme em chunks de 30s entre verificações de status, para responder em até 30s a comandos de pausar/cancelar vindos da UI.

### 5. Persistência por envio
Cada envio bem-sucedido grava a linha em `envios` e atualiza `campanhas.enviados_total` na mesma transação. Crash perde no máximo o envio em vôo — o banco continua sendo a fonte da verdade.

### 6. Tratamento de erros estratificado
- **Erro por contato** (e-mail inválido, recipient refused, hard bounce): registra erro, marca contato inválido se for o caso, segue.
- **Erro transiente** (timeout, conexão caída, 4xx temporário): retry no mesmo contato com backoff `30s, 2min, 10min`. Se 3 contatos seguidos derem transiente, **pausa** com motivo `"rede/SMTP instável"`.
- **Erro fatal** (`SMTPAuthenticationError`, `535`, `530`, conta suspensa): **pausa imediata** com motivo. Sem retry automático — exige intervenção.

### 7. Limpeza do legado
Remove o tipo `campanha` de `/agendamentos`. Não há agendamentos do tipo `campanha` rodando hoje, então a migração apenas executa `DELETE FROM agendamentos WHERE tipo='campanha'`.

## Modelo de dados

### Nova tabela `campanhas`

```sql
CREATE TABLE IF NOT EXISTS campanhas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nome            TEXT NOT NULL,
    perfil_id       INTEGER NOT NULL REFERENCES perfis_remetente(id) ON DELETE RESTRICT,
    filtro_estado   TEXT,
    filtro_tribunal TEXT,
    total_alvo      INTEGER NOT NULL,
    por_dia         INTEGER NOT NULL,
    dias_semana     TEXT NOT NULL,                  -- CSV de inteiros 0-6, 0=segunda
    janela_inicio   TEXT NOT NULL,                  -- "HH:MM"
    janela_fim      TEXT NOT NULL,                  -- "HH:MM"
    status          TEXT NOT NULL DEFAULT 'rascunho',
                                                    -- 'rascunho' | 'ativa' | 'pausada'
                                                    -- | 'concluida' | 'cancelada'
    pausa_motivo    TEXT,
    enviados_total  INTEGER NOT NULL DEFAULT 0,
    criada_em       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    iniciada_em     TIMESTAMP,
    concluida_em    TIMESTAMP
);

-- Garante a regra "uma por perfil ativa/pausada":
CREATE UNIQUE INDEX IF NOT EXISTS idx_campanhas_perfil_unica
ON campanhas(perfil_id)
WHERE status IN ('ativa', 'pausada');
```

### Mudanças em tabelas existentes

```sql
ALTER TABLE envios ADD COLUMN campanha_id INTEGER NULL REFERENCES campanhas(id);
CREATE INDEX IF NOT EXISTS idx_envios_campanha ON envios(campanha_id);

DELETE FROM agendamentos WHERE tipo = 'campanha';
```

Colunas extras de `agendamentos` (`perfil_id`, `filtro_estado`, `filtro_tribunal`, `quantidade`) ficam — não atrapalham, não vale a pena remover em SQLite.

## Worker

### Reidratação no boot

Em `app.main` no startup do FastAPI, depois de `scheduler.iniciar()`, chamar `mailer.reidratar_campanhas()`:

```python
def reidratar_campanhas() -> None:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM campanhas WHERE status = 'ativa'"
        ).fetchall()
    for r in rows:
        _subir_thread(r["id"])
```

Campanhas em `pausada` permanecem paradas até o usuário clicar **Retomar**.

### Loop principal (pseudocódigo)

```
def loop_campanha(campanha_id):
    while True:
        c = carregar_campanha(campanha_id)
        if c.status != 'ativa':
            sair_limpo(c.status)            # pausada/cancelada/concluida
        if c.enviados_total >= c.total_alvo:
            marcar_concluida(c.id)
            sair_limpo('concluida')

        agora = datetime.now(TZ)

        if agora.weekday() not in c.dias_semana:
            dormir_cooperativo_até_próximo_dia_válido()
            continue
        if agora.time() < c.janela_inicio:
            dormir_cooperativo_até(c.janela_inicio)
            continue
        if agora.time() >= c.janela_fim:
            dormir_cooperativo_até_próximo_dia_válido()
            continue

        enviados_hoje_camp   = contar_envios_hoje_da_campanha(c.id)
        enviados_hoje_perfil = contar_envios_hoje_do_perfil(c.perfil_id)
        quota = min(
            c.por_dia        - enviados_hoje_camp,
            c.perfil.limite_diario - enviados_hoje_perfil,
            c.total_alvo     - c.enviados_total,
        )
        if quota <= 0:
            dormir_cooperativo_até_próximo_dia_válido()
            continue

        seg_até_fim = (c.janela_fim - agora).total_seconds()
        intervalo = max(10, seg_até_fim / quota) + jitter(0, 5)

        try:
            enviar_proximo_contato(c)        # com retries internos
        except FalhaFatalSMTP as e:
            pausar(c.id, motivo=str(e))
            sair_limpo('pausada')
        except FalhaTransienteRepetida as e:
            pausar(c.id, motivo=f"Rede/SMTP instável: {e}")
            sair_limpo('pausada')

        dormir_cooperativo(intervalo)
```

`dormir_cooperativo` dorme em chunks de 30s, e a cada chunk:
1. Recarrega `status` da campanha do banco;
2. Se virou `pausada`, `cancelada` ou `concluida`, retorna imediatamente para o `while`, que sai limpo.

### Conexão SMTP

Aberta sob demanda na primeira chamada de `enviar_proximo_contato` após dormir longo. Mantida viva enquanto o intervalo entre envios for ≤ 5min; fechada quando o próximo dorme passar disso, reaberta no próximo envio. Evita conexão idle/morta.

### Comandos da UI → thread

`pausar(id)`, `retomar(id)`, `cancelar(id)`:
1. UPDATE em `campanhas.status` no banco
2. Se `retomar`, sobe nova thread (a antiga já saiu)
3. Se `pausar`/`cancelar`, a thread em execução vai detectar no próximo `dormir_cooperativo` (≤ 30s) e sair

`iniciar(id)` (rascunho → ativa): UPDATE + sobe thread.

## Tratamento de erros (detalhamento)

Classificação por mensagem do `smtplib`:

| Tipo | Padrão de match | Ação |
|---|---|---|
| Fatal | `SMTPAuthenticationError` / `535` / `530` / `account.*disabled` / `invalid.*credentials` | Pausa imediata; `pausa_motivo = "Falha de autenticação: …"`; sem retry |
| Transiente | `SMTPServerDisconnected`, `SMTPConnectError`, timeouts, 4xx | Retry no mesmo contato: 30s → 2min → 10min. Se persistir após 3, marca envio como erro e segue. Se 3 contatos seguidos cair em transiente, pausa por "rede/SMTP instável" |
| Por contato | `SMTPRecipientsRefused`, regex `_RE_BOUNCE_PERMANENTE` atual | Registra envio como erro, marca contato inválido se hard bounce, segue |

Toda pausa é registrada em `cron_runs` (com `tipo='campanha'`, `ag_id=NULL`, `nome=campanha.nome`, `mensagem=pausa_motivo`) para auditoria histórica.

## UI / Telas

### `/campanhas` — lista (página principal)

- Cards/linhas por campanha. Ordenação: ativa/pausada primeiro, depois rascunho, depois concluída/cancelada.
- Cada card: nome, perfil, badge de status, barra de progresso `enviados_total/total_alvo`, "hoje 47/200", "próximo às 14:23" (se ativa), botão **Abrir**.
- Topo: filtro por status + botão **+ Nova campanha**.

### `/campanhas/nova` — formulário

Campos:
- **Nome** (livre)
- **Perfil de remetente**: select com todos os perfis do usuário; perfis com campanha ativa/pausada aparecem desabilitados como `Victor — em uso por "TJSP janeiro"`
- **Filtros**: UF + Tribunal (cascade existente)
- **Total alvo** (default `1000`, min `1`)
- **Por dia** (default `min(200, perfil.limite_diario)`, validação ≤ `perfil.limite_diario`)
- **Dias da semana** (7 checkboxes, default seg-sex)
- **Janela início** / **Janela fim** (default `09:00` / `17:00`, validação `inicio < fim`)
- Submit cria com `status='rascunho'` e redireciona para `/campanhas/{id}`

### `/campanhas/{id}` — detalhe (a tela de transparência)

Layout em 6 blocos, atualizando a cada 5s via HTMX:

1. **Header**:
   - Nome + status badge
   - Botões contextuais: `Iniciar` (rascunho), `Pausar` (ativa), `Retomar` (pausada), `Cancelar` (qualquer não-terminal), `Editar` (rascunho)
   - Banner vermelho com `pausa_motivo` se pausada por erro

2. **Progresso geral**:
   - Barra `enviados_total/total_alvo`
   - "Faltam 700 e-mails"
   - Estimativa: "no ritmo atual, conclui em ~4 dias úteis (terça 06/05/2026)"

3. **Hoje**:
   - `47/200 enviados`, `próximo às 14:23`, `último às 14:13 ✓`
   - Intervalo médio observado, "janela termina às 17:00"
   - Status do worker: `🟢 ativo / aguardando próximo envio`, `🟡 fora da janela — retoma 09:00 amanhã`, `⏸ pausada`, `⚪ rascunho`, `✅ concluída`

4. **Próximos 7 dias**:
   - Tabela leve dia-a-dia: data, dia da semana, "previsto: 200" / "fora dos dias" / "concluída neste dia"

5. **Histórico por dia** (últimos 30):
   - Tabela `data | enviados | erros | bounces | aberturas`
   - Cada linha linka para `/historico?campanha_id={id}&data={data}`

6. **Últimos envios** (10):
   - `hora | e-mail | status`
   - Link "ver tudo" → `/historico?campanha_id={id}`

### `/agendamentos` — limpeza

- Formulário perde a opção `campanha` no select de tipo
- Texto da página esclarece: "Agendamentos rodam scrapers automaticamente. Para envio de e-mail, use **Campanhas**"
- Tipo passa a ser implícito = scraper (campo escondido ou removido)

### `/historico` — ganho

- Novo filtro: **Campanha** (dropdown carregado de `SELECT id, nome FROM campanhas ORDER BY criada_em DESC`)
- Coluna **Campanha** opcional na tabela (ou só mostra como link no nome do contato)

## Migrações de código

### Remover / refatorar

- `mailer.disparar(perfil_id, total_alvo, filtros)`: remover esta assinatura. A lógica de envio em loop vira interna ao `loop_campanha`.
- `_em_andamento` e `CampanhaEstado` no `mailer.py`: renomear para `_threads_runtime` e `RuntimeEstado`, deixando explícito que é estado **em memória da thread**, não estado da campanha (esse último vive em `campanhas`).
- Rotas removidas (`main.py`):
  - `POST /campanhas/disparar`
  - `GET /campanhas/acompanhar/{perfil_id}`
  - `GET /campanhas/estado/{perfil_id}`
  - `POST /campanhas/cancelar/{perfil_id}`
- Templates removidos: `campanhas.html` (formulário antigo) — **substituído** por novo `campanhas.html` (lista). `campanha_acompanhar.html` removido — substituído por novo `campanha_detalhe.html`. `_campanha_estado.html` removido (estado vai inline na nova página).
- `scheduler._executar_job`: remover branch `tipo == "campanha"`.
- `main._o_que()` e `main._quando()` em `agendamentos`: remover branch de campanha.
- `agendamento_form.html`: remover bloco `bloco_campanha` e o JS que alterna entre scraper/campanha.

### Adicionar

- Novas rotas (`main.py`):
  - `GET /campanhas` — lista
  - `GET /campanhas/nova` — formulário
  - `POST /campanhas/nova` — cria como rascunho, redireciona para detalhe
  - `GET /campanhas/{id}` — detalhe
  - `GET /campanhas/{id}/parcial` — fragmento HTMX para auto-refresh
  - `POST /campanhas/{id}/iniciar`
  - `POST /campanhas/{id}/pausar`
  - `POST /campanhas/{id}/retomar`
  - `POST /campanhas/{id}/cancelar`
  - `POST /campanhas/{id}/editar` (apenas se `status='rascunho'`)
- Novas funções em `mailer.py`:
  - `criar_campanha(...)`, `editar_campanha(...)`, `iniciar_campanha(id)`, `pausar_campanha(id, motivo)`, `retomar_campanha(id)`, `cancelar_campanha(id)`
  - `reidratar_campanhas()` (chamada no startup)
  - `loop_campanha(id)` (target da thread)
  - Helpers de dormir cooperativo, classificação de erro, cálculo de intervalo
- Templates novos: `campanhas.html` (nova lista), `campanha_form.html`, `campanha_detalhe.html`, `_campanha_detalhe_corpo.html` (fragmento)

## Compatibilidade com features existentes

- **Tracking pixel**: nenhum impacto — é injetado em cada `_enviar_um`, segue funcionando.
- **IMAP bounce checker**: nenhum impacto — opera sobre `envios.message_id`. Bounces marcam status `bounce`/`bounce_soft` e contato inválido. Como o contador `enviados_total` da campanha conta apenas envios `status='ok'`, bounces capturados via DSN não inflam o progresso.
- **Histórico** (`/historico`): ganha filtro por campanha. Sem outras mudanças.
- **Histórico por vara** (`/historico/por-vara`): nenhum impacto.
- **Envio de teste** (`/teste`): nenhum impacto — não usa o caminho de campanha.
- **Limite diário do perfil**: continua sendo respeitado (a campanha nunca pede mais do que `perfil.limite_diario - enviados_hoje_perfil`).

## Critérios de sucesso

1. Usuário cria campanha de 1.000 e-mails, 200/dia, seg-sex, janela 09:00–17:00, em menos de 30 segundos.
2. Página `/campanhas/{id}` mostra a qualquer momento: progresso geral, quanto saiu hoje, hora do próximo envio, status do worker — sem ambiguidade.
3. Após redeploy do container, campanhas ativas voltam a enviar dentro de 1 min.
4. Falha de auth SMTP pausa a campanha em até 30s e mostra a mensagem na UI.
5. Tipo `campanha` deixa de existir em `/agendamentos`.
6. Cada e-mail enviado aparece em `/historico` com o link da campanha que o originou.

## Não-objetivos (fora de escopo)

- **Múltiplas campanhas no mesmo perfil em paralelo** (resolvido com 1 perfil = 1 campanha; quem quiser paralelo cria perfil novo).
- **Editar campanha em vôo** (apenas em status `rascunho`; ativa/pausada não permite editar — só cancelar e recriar).
- **Re-tentar contatos que deram erro permanente** (continuam marcados inválidos; sem mudança).
- **Distribuição por prioridade** entre contatos (continua FIFO `ORDER BY id ASC`).
- **Métricas avançadas** tipo "ritmo médio das últimas 7 campanhas" (não faz parte deste spec).
