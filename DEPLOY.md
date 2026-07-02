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

## 5. Usuários de acesso

Já existem 3 na allowlist `cockpit.usuarios_login` (reges, percio, juciane).
Se souber a senha deles, o login já funciona (o verificador detecta o N do
scrypt automaticamente). Para criar/redefinir uma senha:

```bash
python3 scripts/set_password.py fulano@totvs.com.br "SenhaForte" "Nome Fulano"
# copie o SQL impresso e rode no Supabase → SQL Editor
```

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
GET  /login    POST /api/login   POST /api/logout   GET /api/me
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
