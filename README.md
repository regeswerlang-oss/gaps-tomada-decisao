# Gaps · Tomada de Decisão — Vercel

Versão serverless (Vercel) da tela de **Tomada de Decisão dos Gaps** do
ecossistema TOTVS SC. Um único app Flask (`api/index.py`) responde a todas as
rotas, serve os HTMLs com **porta de login** e conversa com:

- **Supabase** (`cockpit`) → clientes, tickets, tags, decisões, rascunhos.
- **Tasks SC** (`api.tscst.com.br`) → leitura/edição de tickets ao vivo
  (OAuth2 + GET→merge→PUT).

## Estrutura

```
gaps-vercel/
├── api/index.py          # backend serverless (Flask/WSGI) — todas as rotas
├── web/                  # HTMLs servidos pelo Flask (com login)
│   ├── login.html
│   ├── drive_index.json  # fallback do índice do Drive
│   ├── gaps-decisao.html # ← você adiciona
│   └── gaps-reuniao.html # ← você adiciona
├── scripts/set_password.py
├── requirements.txt
├── vercel.json
├── .env.example
└── DEPLOY.md             # passo a passo do deploy
```

Comece por **DEPLOY.md**.

Segurança: `SESSION_SECRET`, `DATABASE_URL` e `TASKS_PASSWORD` ficam só nas
env vars da Vercel. O cookie de sessão é HttpOnly, Secure e assinado (HMAC).
As senhas em `cockpit.usuarios_login` usam scrypt.
