# Patch das skills — perfil `leitor` (aplicar em Configurações > Capacidades)

As skills instaladas ainda descrevem **três** perfis. São 4 edições, em 2 skills.
Cada bloco é um "procure isto → troque por aquilo".

---

## 1. Skill `gaps-tomada-decisao` → `references/banco-cockpit.md`

### 1.1 PROCURE (por volta da linha 56)

```md
**`cockpit.usuarios_login`** — `email`, `senha_hash` (`scrypt$salt$hash`),
`nome`, `ativo`, `is_admin`, `last_login`. Verificação em `verify_scrypt`
(r=8, p=1, N testado entre 16384/32768/8192/65536/4096 se `SCRYPT_N` não setado).

**`cockpit.usuario_clientes`** — `email` × `customer` liberado. Regra em
`allowed_customers()` (idêntica à skill **`controle-acesso-por-cliente`**):

- `is_admin=true` → `None` = vê **todos** os clientes.
- usuário comum → conjunto de `customer` liberados (pode ser vazio = não vê
  nada). Clientes não liberados **somem** da lista e os tickets retornam 403
  (`deny_customer` / `deny_uuid`).
```

### 1.2 TROQUE POR

```md
**`cockpit.usuarios_login`** — `email`, `senha_hash` (`scrypt$salt$hash`),
`nome`, `ativo`, `perfil`, `is_admin`, `last_login`. Verificação em
`verify_scrypt` (r=8, p=1, N testado entre 16384/32768/8192/65536/4096 se
`SCRYPT_N` não setado). `is_admin` é **derivado** de `perfil = 'admin'` pelo
trigger `cockpit.sync_perfil_is_admin` — ele existe porque o cockpit Next.js
ainda lê essa coluna. Nunca escreva os dois na mão: escreva `perfil`.

**Os quatro perfis** (`sql/0006_perfis_acesso.sql` + `sql/0007_perfil_leitor.sql`,
constraint `usuarios_login_perfil_chk`):

| Perfil | Clientes que vê | Decide/estima (`cockpit.decisoes`) | Altera o Tasks SC | Administra acessos |
|---|---|---|---|---|
| `admin`   | **todos** | ✅ | ✅ | ✅ |
| `comum`   | só os liberados | ✅ | ✅ | ❌ |
| `cliente` | só os liberados | ✅ | ❌ (403) | ❌ |
| `leitor`  | só os liberados | ❌ (403) | ❌ (403) | ❌ |

`leitor` = **somente visualização**: lê e exporta (CSV/JSON/PDF são gerados no
browser), mas não grava nada. Única escrita: a própria senha
(`POST /api/conta/senha`) — é sobre a conta dele.

**Dois portões, um por DESTINO da escrita** — não os confunda:

- `require_tasks_write()` → o que SAI para o Tasks SC. Barra `cliente` e
  `leitor`. Em `/api/ticket/<uuid>/update`, `.../history`, `/api/tags/sync`,
  `/api/refresh`, `/api/gmail/draft`.
- `require_write()` → o que grava no NOSSO banco (`cockpit.decisoes`). Barra só
  o `leitor`. Em `/api/decisoes`, `/api/decisoes/importar`,
  `/api/ticket/<uuid>/decidir`.

Ambos usam o usuário **REAL** da sessão; quem barra o "ver como" é o
`deny_simulacao()` (409). O front esconde as ações lendo `somente_leitura` /
`pode_decidir` / `pode_escrever_tasks` do `GET /api/me` — isso é **cosmético**.

**`cockpit.usuario_clientes`** — `email` × `customer` liberado. Regra em
`allowed_customers()` (idêntica à skill **`controle-acesso-por-cliente`**):

- `is_admin=true` → `None` = vê **todos** os clientes.
- demais perfis → conjunto de `customer` liberados (pode ser vazio = não vê
  nada). Clientes não liberados **somem** da lista e os tickets retornam 403
  (`deny_customer` / `deny_uuid`).

**Adicionar um perfil novo** (receita completa em `sql/README-perfis.md` do
repo): constraint → `PERFIS`/`PERFIL_LABEL`/`PERFIS_SO_LEITURA` → portão →
flag no `/api/me` → dois `<select>` do `admin.html` + `.pill` → faixa nas telas
→ `DEPLOY.md § 5`.
```

---

## 2. Skill `gaps-tomada-decisao` → `SKILL.md`

### 2.1 PROCURE (por volta da linha 131)

```md
- **`controle-acesso-por-cliente`** — a regra de `allowed_customers()` (admin vê
  tudo; usuário comum só os `customer` liberados em `cockpit.usuario_clientes`).
```

### 2.2 TROQUE POR

```md
- **`controle-acesso-por-cliente`** — a regra de `allowed_customers()` (admin vê
  tudo; os demais perfis só os `customer` liberados em
  `cockpit.usuario_clientes`). Aqui a skill ganha **4 perfis**: `admin`,
  `comum`, `cliente` (não altera o Tasks SC) e `leitor` (somente visualização —
  não grava nada). Matriz e portões em `references/banco-cockpit.md`.
```

---

## 3. Skill `controle-acesso-por-cliente` → `SKILL.md`

### 3.1 PROCURE (seção "Regras de negócio (decisões padrão)")

```md
## Regras de negócio (decisões padrão)

- **Admin vê tudo** (ignora as liberações).
- **Sem liberação = não vê nada** (usuário comum sem nenhum cliente marcado não
  vê cliente nem ticket, mesmo marcando "todos"). Modo estrito.
- Filtro aplicado em **todas as telas** do dashboard.
- `AUTH_SECRET` ausente (dev/local sem login) ⇒ **não restringe** (evita lockout).

Essas decisões são configuráveis — confirme com o usuário antes de portar
(quem é admin; sem-liberação vê nada ou vê tudo; quais telas).
```

### 3.2 TROQUE POR

```md
## Regras de negócio (decisões padrão)

- **Admin vê tudo** (ignora as liberações).
- **Sem liberação = não vê nada** (usuário sem nenhum cliente marcado não vê
  cliente nem ticket, mesmo marcando "todos"). Modo estrito.
- Filtro aplicado em **todas as telas** do dashboard.
- `AUTH_SECRET` ausente (dev/local sem login) ⇒ **não restringe** (evita lockout).

Essas decisões são configuráveis — confirme com o usuário antes de portar
(quem é admin; sem-liberação vê nada ou vê tudo; quais telas).

### Perfil ≠ liberação — são dois eixos

A liberação responde **"quais clientes ele vê"**. O perfil responde **"o que ele
pode gravar"**. Não misture os dois num campo só. Referência viva: o app
`gaps-vercel` (skill `gaps-tomada-decisao`), que usa 4 perfis:

| Perfil | Clientes que vê | Grava no banco do app | Grava no sistema de origem | Administra acessos |
|---|---|---|---|---|
| `admin`   | **todos** | ✅ | ✅ | ✅ |
| `comum`   | só os liberados | ✅ | ✅ | ❌ |
| `cliente` | só os liberados | ✅ | ❌ | ❌ |
| `leitor`  | só os liberados | ❌ | ❌ | ❌ |

Modelagem que se provou: coluna `perfil text` com `check` + trigger derivando o
`is_admin` legado (`is_admin := perfil = 'admin'`), para os dashboards antigos
que ainda leem `is_admin` não quebrarem. Ver `reference/0005_acessos.sql`.

**Um portão por DESTINO da escrita**, não um portão por perfil: o que sai para o
sistema de origem (barra `cliente` + `leitor`) e o que grava no banco do próprio
app (barra `leitor`). Assim, adicionar um perfil é escolher portões, não caçar
`if` espalhado. O único write que o `leitor` mantém é a **própria senha** — é
sobre a conta dele, não sobre os dados.

Perfil somente-leitura: declare a lista (`PERFIS_SO_LEITURA`) em vez de comparar
string solta — o próximo perfil read-only entra sem varrer o código.
```

---

## 4. Skill `controle-acesso-por-cliente` → `SKILL.md`, seção "Armadilhas"

### 4.1 ACRESCENTE ao fim da lista de armadilhas

```md
- **E-mail no path chega percent-encoded**: se a tela admin chamar
  `POST /usuarios/${encodeURIComponent(email)}/clientes`, o `@` vira `%40`. Em
  função serverless (Vercel/Flask) o `PATH_INFO` pode chegar **cru**, e o
  `lower(email)=...` não casa com o banco — a tela morre com um enganoso
  **"Usuário não encontrado" (404)** em TODAS as ações da linha (clientes,
  senha, perfil, ativo), enquanto a listagem funciona (o e-mail dela vai no
  corpo). Defesa: `unquote` no normalizador de e-mail, que é o ponto único, e
  ecoar o e-mail recebido na mensagem do 404. Custa uma linha e economiza uma
  tarde.
```
