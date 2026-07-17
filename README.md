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
│   ├── admin.html        # usuários, perfis e clientes liberados
│   ├── drive_index.json  # fallback do índice do Drive
│   ├── gaps-decisao.html # ← você adiciona
│   └── gaps-reuniao.html # ← você adiciona
├── scripts/set_password.py
├── sql/                  # migrations idempotentes (0006 perfis, 0007 leitor)
├── requirements.txt
├── vercel.json
├── .env.example
└── DEPLOY.md             # passo a passo do deploy
```

Comece por **DEPLOY.md**.

## Perfis de acesso

| Perfil | Clientes que vê | Decide/estima | Altera o Tasks SC | Administra acessos |
|---|---|---|---|---|
| **admin** | todos | ✅ | ✅ | ✅ |
| **comum** | só os liberados | ✅ | ✅ | ❌ |
| **cliente** | só os liberados | ✅ | ❌ | ❌ |
| **leitor** | só os liberados | ❌ | ❌ | ❌ |

**leitor** = somente visualização: lê os GAPs dos clientes liberados e exporta,
mas não grava nada (só a própria senha). Detalhes e portões em **DEPLOY.md § 5**.

Segurança: `SESSION_SECRET`, `DATABASE_URL` e `TASKS_PASSWORD` ficam só nas
env vars da Vercel. O cookie de sessão é HttpOnly, Secure e assinado (HMAC).
As senhas em `cockpit.usuarios_login` usam scrypt.
