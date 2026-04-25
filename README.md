# Peritos — Painel

Aplicação web para automatizar o ciclo:

1. **Buscar** e-mails de varas/comarcas em sites de Tribunais (web scraping)
2. **Armazenar** os contatos numa base centralizada
3. **Disparar** e-mails de apresentação por perfil de remetente, com limite diário e histórico

Construída em **FastAPI + SQLite + HTMX**, pensada para rodar em um único container Docker no EasyPanel.

---

## Funcionalidades

- 🔐 Login (dois usuários — você e sua esposa, cadastrados via variáveis de ambiente no primeiro start)
- 🕷️ **Scrapers** dos 11 Tribunais já incluídos (TJMG, TJSP, TJRJ, TJPA, TJRN, TJDFT, TJAL, TJRS, TJRO, TJMT, TJSC*) — execução em segundo plano com log em tempo real
- 📒 **Base de contatos** com filtros (UF, tribunal, busca textual), marcar como inválido, excluir
- ✉️ **Perfis de remetente** independentes (cada um com seu Gmail, currículo PDF, assunto e corpo do e-mail editáveis)
- 📤 **Campanhas de envio** com filtros, limite diário, pausa entre envios e cancelamento em andamento
- 📊 **Histórico** de envios com filtros por perfil/status/data
- 📬 **Verificação de bounces (IMAP)** — lê DSNs na caixa do remetente, casa pelo `Message-ID` e marca contatos com bounce permanente (5.x.x) como inválidos automaticamente
- ⏰ **Agendamentos cron** para rodar scrapers automaticamente

\* TJSC tem CAPTCHA manual e fica indisponível na versão web.

---

## Deploy no EasyPanel

### 1. Subir o código

Coloque a pasta `peritos_app/` num repositório Git (privado). Pode usar GitHub, GitLab ou Gitea.

### 2. Criar o app no EasyPanel

No painel do EasyPanel:

1. **+ Service → App**
2. Nome: `peritos`
3. **Source**: GitHub/GitLab → seu repositório
4. **Build**: tipo `Dockerfile` (o EasyPanel detecta automaticamente)
5. **Port**: `8000`

### 3. Configurar volume persistente

Em **Mounts**:

- Tipo: `Volume`
- Volume name: `peritos-data`
- Mount path: `/data`

Isto preserva o banco SQLite e os PDFs de currículo entre redeploys.

### 4. Variáveis de ambiente

Em **Environment**, defina pelo menos:

```
DATA_DIR=/data
ADMIN_EMAIL=victor@exemplo.com
ADMIN_NOME=Victor Maziero
ADMIN_SENHA=<senha forte para o primeiro login>
USER2_EMAIL=tati@exemplo.com
USER2_NOME=Tati
USER2_SENHA=<senha forte>
```

`SESSION_SECRET` e `FERNET_KEY` são **opcionais** — se você não definir,
o app gera valores fortes no primeiro start e os persiste em
`/data/.session_secret` e `/data/.fernet_key`. Como o `/data` é volume,
as chaves sobrevivem a redeploys. Só defina manualmente se quiser
controlar a chave (ex.: replicar em outra instância).

> **Importante**: depois do primeiro login, troque as senhas iniciais por outras (em uma versão futura — por ora elas valem só pra primeiro acesso).

> **Não apague o volume `/data`**. Ele guarda o banco SQLite, currículos
> e a `FERNET_KEY` que criptografa as senhas SMTP. Se a chave mudar,
> os perfis cadastrados ficam inutilizáveis.

### 5. Domínio

Em **Domains**:

- Adicione `peritos.mspericias.com`
- Marque **HTTPS** (Let's Encrypt automático)

No DNS do `mspericias.com`, crie um registro `A` apontando `peritos` para o IP da sua VPS.

### 6. Deploy

Clique em **Deploy**. Acompanhe o build (uns 3-5 min na primeira vez por causa do Chromium).

Quando subir, acesse `https://peritos.mspericias.com` e faça login.

---

## Primeiros passos no app

1. **Login** com `ADMIN_EMAIL` / `ADMIN_SENHA`.
2. **Perfis → + Novo perfil**: cadastre você mesmo (Victor) com:
   - Senha de app do Gmail (gere em <https://myaccount.google.com/apppasswords>)
   - Texto do e-mail (use `$cidade`, `$comarca`, `$sistema`, `$remetente` como variáveis)
   - Anexe seu currículo PDF
3. **Scrapers**: clique "Rodar" no TJ desejado. Acompanhe o log em tempo real.
4. **Contatos**: confira o que entrou na base, marque inválidos.
5. **Campanhas**: escolha o perfil, filtre por UF/TJ, defina quantos enviar e dispare.
6. **Histórico**: veja o que foi enviado, com erro ou ok.
7. **Agendamentos** (opcional): cron `0 3 * * 1` roda toda segunda às 03:00 UTC.

---

## Desenvolvimento local

```bash
cd peritos_app
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edite .env preenchendo SESSION_SECRET, FERNET_KEY, ADMIN_*

uvicorn app.main:app --reload
```

Abra <http://127.0.0.1:8000>.

Os scrapers que usam Selenium (TJRS, TJRO, TJMT, TJSC) precisam de **Chrome/Chromium** instalado localmente — em produção isso já vem no container.

---

## Estrutura

```
peritos_app/
├── Dockerfile              # build do container (Python 3.12 + Chromium)
├── requirements.txt
├── .env.example
├── app/
│   ├── main.py             # rotas FastAPI
│   ├── config.py           # settings via env
│   ├── db.py               # SQLite + schema
│   ├── auth.py             # login + sessão
│   ├── crypto.py           # Fernet (senhas SMTP)
│   ├── mailer.py           # envio em background com limite diário
│   ├── scheduler.py        # APScheduler (cron interno)
│   ├── scrapers/
│   │   ├── registry.py     # lista dos 11 TJs
│   │   ├── runner.py       # executa scraper em subprocess
│   │   └── external_scripts/
│   │       └── tj*_scraper.py  # scripts originais
│   ├── templates/          # HTML + HTMX
│   └── static/style.css
└── data/                   # (criada em runtime — montar como volume)
    ├── peritos.db
    └── curriculos/
```

---

## Backup

Pra fazer backup, basta copiar o conteúdo do volume `/data` (SQLite + currículos):

```bash
# pelo EasyPanel: Service → Console
tar czf /tmp/peritos-backup.tgz /data
# baixe o /tmp/peritos-backup.tgz pra sua máquina
```

---

## Verificação de bounces (IMAP)

Cada perfil pode ter o IMAP ativado (checkbox no formulário). Quando ativo:

- A cada **30 min** (e na inicialização do app), o sistema conecta via IMAP na caixa do remetente usando a mesma senha SMTP cadastrada
- Busca incremental por UID (não reprocessa mensagens já vistas; 1ª execução limita aos últimos 30 dias)
- Detecta DSNs (`multipart/report`, `MAILER-DAEMON`, etc.), extrai `Status: X.Y.Z` e o `Message-ID` original
- Casa com o envio em `envios.message_id` e atualiza:
  - **5.x.x** (hard bounce) → status `bounce` + `contatos.invalido = 1` (não tenta de novo)
  - **4.x.x** (soft bounce) → status `bounce_soft` (preserva o contato)
- Marca a DSN como lida (`\Seen`) para não reprocessar
- O painel **/perfis** mostra o status da última run e tem botão "verificar agora"
- O **/historico** ganha filtros e contadores separados de `bounce`/`bounce_soft`

**Host IMAP**: pode ser deixado em branco no perfil — o sistema deriva trocando `smtp.` por `imap.` (Gmail/Yahoo) ou usando `outlook.office365.com` para Outlook.

---

## Notas técnicas

- **Banco**: SQLite com WAL ativado. Bom para até centenas de milhares de contatos. Migrar pra Postgres só se a base crescer muito (e nesse caso só trocar `db.py`).
- **Senhas SMTP** ficam criptografadas com Fernet. A chave fica em variável de ambiente — nunca no banco.
- **Envios** rodam em thread daemon dentro do mesmo processo (sem Redis/Celery). Pausa de ~10s + jitter entre envios. Limite diário aplicado por perfil.
- **Selenium**: o Dockerfile já instala `chromium` e `chromium-driver`. Os scrapers que usam Selenium rodam em modo headless.
- **TJSC**: tem CAPTCHA, então fica desabilitado na UI web. Pra atualizar TJSC, rode o script original na sua máquina e exporte/importe manualmente (numa versão futura podemos fazer um upload do XLSX).
