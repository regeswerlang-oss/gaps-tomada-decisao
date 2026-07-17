# Deploy — Gaps · Tomada de Decisão (Vercel + Supabase)

Porte do `gaps_server.py` (porta 8090 local) para **funções serverless na
Vercel**, lendo/gravando no **Supabase existente** (schema `cockpit`) e
mantendo **paridade total** com o Tasks SC (leitura/escrita ao vivo).

O que muda em relação ao local:

| Antes (local) | Agora (Vercel) |
|---|---|
| `Dados/clientes.json`, `<cliente>_tickets.json` | tabelas `cockpit.clientes` / `cockpit.tickets` (já sincronizadas) |
| `decisoes_<cliente>.json` | tabela `cockpit.decisoes` (persistente, multiusuário) |
| `drive_index.json` na pasta | `cockpit.integration_config['drive_index']` (fallback: `web/drive_index.json`) |
| `gmail_server.py :8081` | rascunho salvo em `cockpit.email_drafts` (envio real via Gmail API = v2) |
| sem login | **login** contra `cockpit.usuarios_login` (scrypt) |
| Tasks SC ao vivo | **igual** — OAuth + GET→merge→PUT direto na `api.tscst.com.br` |

---

## 1. Coloque os HTMLs

Copie `gaps-decisao.html` e `gaps-reuniao.html` para a pasta **`web/`**
(veja `web/_COLOQUE_OS_HTMLS_AQUI.md`). Não use `public/`.

## 2. Suba para o GitHub

```bash
cd gaps-vercel
git init && git add . && git commit -m "Gaps Tomada de Decisão — Vercel"
git branch -M main
git remote add origin git@github.com:SEU_USER/gaps-tomada-decisao.git
git push -u origin main
```

## 3. Importe na Vercel

1. vercel.com → **Add New… → Project** → importe o repositório.
2. Framework Preset: **Other** (o `vercel.json` já configura tudo).
3. **Não** precisa de Build Command. Root Directory = a pasta do repo.

## 4. Variáveis de ambiente (Vercel → Project → Settings → Environment Variables)

| Nome | Valor |
|---|---|
| `DATABASE_URL` | connection string do **pooler** do Supabase (porta **6543**) — Settings → Database → Connection string → *Transaction pooler* |
| `TASKS_USERNAME` | `actvs\reges.werlang` (com **uma** barra) |
| `TASKS_PASSWORD` | senha do AD |
| `TASKS_SC_BASE_URL` | `https://api.tscst.com.br/restAPI` |
| `SESSION_SECRET` | hex aleatório: `python3 -c "import secrets;print(secrets.token_hex(32))"` |

Aplique a todos os ambientes (Production, Preview, Development). Depois de
setar/alterar, faça **Redeploy**.

> A senha do banco e a `TASKS_PASSWORD` só existem aqui (backend). Nunca vão
> para o frontend nem para o Git.

## 5. Usuários e controle de acesso

Tudo se faz pela tela **`/admin`** (botão **⚙ Acessos** no header, visível só
para admin). O `scripts/set_password.py` continua servindo como saída de
emergência — se você se trancar do lado de fora, ele gera o SQL para colar no
SQL Editor do Supabase.

### Migration (uma vez)

`sql/0006_perfis_acesso.sql` — cria `perfil` em `usuarios_login`, o trigger que
mantém `perfil` ⇄ `is_admin` coerentes (o cockpit Next.js ainda lê `is_admin`) e
a tabela `usuario_clientes`. É idempotente. Aplicada em 17/07/2026.

### Os três perfis

| Perfil | Clientes que vê | Decide/estima | Altera o Tasks SC | Administra acessos |
|---|---|---|---|---|
| **admin** | todos | ✅ | ✅ | ✅ |
| **comum** | só os liberados | ✅ | ✅ | ❌ |
| **cliente** | só os liberados | ✅ | ❌ (HTTP 403) | ❌ |

**Modo estrito**: usuário comum/cliente **sem nenhum cliente liberado não vê
nada** — o cliente some do combo e os tickets retornam 403. Marcar "todos" no
front não fura: o recorte é server-side, em `allowed_customers()`.

O perfil **cliente** é barrado por `require_tasks_write()` nas rotas
`POST /api/ticket/<uuid>/update`, `.../history`, `/api/tags/sync`,
`/api/refresh` e `/api/gmail/draft`. Decisão/estimativa (`/api/decisoes`)
continua liberada — ela grava em `cockpit.decisoes`, não no Tasks SC.

### Fluxo de cadastro

1. `/admin` → **Novo usuário**: e-mail, nome, perfil e senha inicial (≥ 8
   caracteres, hash scrypt gerado no servidor — a senha em claro nunca é gravada).
2. Na linha do usuário → **Clientes** → marque o que ele enxerga → **Salvar
   liberação**.
3. **Editar** troca nome/perfil e redefine senha; **Inativar** corta o login.
4. Cada um troca a própria senha em `/admin` → **Minha conta**.

Auto-proteções: o admin não consegue se inativar nem tirar o próprio
`is_admin` — nem pela tela, nem pela API.

Para conferir a visão de alguém sem saber a senha dele, use o **👁 Ver como**
no header do board (só admin; escrita bloqueada durante a simulação).

## 6. Deploy e teste

1. Abra a URL da Vercel → cai em **/login**.
2. Entre → board `gaps-decisao.html`.
3. Cheque: filtro de cliente, cards por tag/etapa, drawer, estimativa,
   ajuste de tags (PUT ao vivo no Tasks SC), NotebookLM/PERSONALIZAÇÃO,
   e a tela de reunião gravando decisões.

---

## Endpoints (iguais ao gaps_server.py, mais os de auth)

```
GET  /                    /gaps-decisao.html   /gaps-reuniao.html   (login)
GET  /admin                                                         (login)
GET  /login    POST /api/login   POST /api/logout   GET /api/me
GET  /api/admin/usuarios               POST /api/admin/usuarios          (admin)
POST /api/admin/usuarios/<email>/ativo    /perfil    /senha    /clientes (admin)
GET  /api/admin/clientes                                                 (admin)
POST /api/conta/senha                          {atual, nova}
GET  /api/clientes
GET  /api/tickets?cliente=digitro
GET  /api/decisoes?cliente=digitro     POST /api/decisoes?cliente=digitro
GET  /api/drive-index
GET  /api/ticket/<uuid>                GET /api/ticket/<uuid>/history
GET  /api/tags-catalog?search=
POST /api/ticket/<uuid>/update         POST /api/ticket/<uuid>/history
POST /api/refresh?cliente=DIGITRO
GET  /api/gmail/health                 POST /api/gmail/draft
```

## Rodar local (opcional)

```bash
cp .env.example .env      # preencha os valores (aspas simples no username!)
pip install -r requirements.txt
python3 api/index.py      # http://localhost:8090
```

## Notas / limites da v1

- **Refresh**: além do sync noturno do cockpit (já ativo), `/api/refresh`
  faz upsert ao vivo dos tickets do cliente direto do Tasks SC.
- **Gmail**: o botão salva o rascunho em `cockpit.email_drafts`. O envio/rascunho
  real no Gmail depende de credenciais da Gmail API (v2).
- **Timeout**: `maxDuration=60s` no `vercel.json`. No plano Hobby o teto pode
  ser menor; se `/api/refresh` estourar, reduza o `pageSize` ou rode por cliente.
- **Espelho de edições**: após um PUT no Tasks SC, os campos alterados são
  espelhados em `cockpit.tickets`/`ticket_tags` (best-effort) para o board
  refletir na hora; a normalização completa vem no próximo sync.
