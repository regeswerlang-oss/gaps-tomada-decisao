# Perfis de acesso — de-para e onde cada regra mora

Fonte da verdade: `api/index.py` (constantes `PERFIS` / `PERFIS_SO_LEITURA` e os
portões) + `cockpit.usuarios_login.perfil`. Esta página existe para o de-para
não se perder entre o banco, o backend e as telas.

## Matriz

| Perfil | Clientes que vê | Decide/estima (`cockpit.decisoes`) | Altera o Tasks SC | Administra acessos | Própria senha |
|---|---|---|---|---|---|
| `admin`   | **todos** | ✅ | ✅ | ✅ | ✅ |
| `comum`   | só os liberados | ✅ | ✅ | ❌ | ✅ |
| `cliente` | só os liberados | ✅ | ❌ | ❌ | ✅ |
| `leitor`  | só os liberados | ❌ | ❌ | ❌ | ✅ |

`leitor` = **somente visualização**. Lê os GAPs dos clientes liberados
(histórico, NotebookLM, decisão e estimativa já registradas) e exporta
CSV/JSON/PDF — que são gerados no browser, sem passar pelo servidor. Não grava
nada. A única escrita que sobra é a própria senha, que é sobre a conta dele.

## Os quatro portões (backend, sempre pelo usuário REAL da sessão)

| Portão | Barra quem | Onde é usado |
|---|---|---|
| `require_admin()` | todos menos `admin` | `/api/admin/*`, `/api/usuarios`, `/api/view-as` |
| `require_tasks_write()` | `cliente`, `leitor` | escritas que SAEM para o Tasks SC: `/api/ticket/<uuid>/update`, `.../history`, `/api/tags/sync`, `/api/refresh`, `/api/gmail/draft` |
| `require_write()` | `leitor` | escritas no NOSSO banco: `/api/decisoes`, `/api/decisoes/importar`, `/api/ticket/<uuid>/decidir` |
| `deny_simulacao()` | quem está no "ver como" | toda escrita |

Recorte por cliente: `allowed_customers()` (modo estrito — sem liberação, não vê
nada) + `deny_customer()` / `deny_uuid()`.

## Como adicionar um perfil novo

1. `sql/000N_*.sql`: ampliar a constraint `usuarios_login_perfil_chk`.
   O trigger `sync_perfil_is_admin` não precisa mudar — `is_admin` é derivado de
   `perfil = 'admin'`.
2. `api/index.py`: `PERFIS`, `PERFIL_LABEL` e (se for só-leitura)
   `PERFIS_SO_LEITURA`; ajustar o portão certo.
3. `GET /api/me`: expor a flag que o front usa para esconder as ações.
4. `web/admin.html`: as duas `<select>` de perfil (novo usuário e editar), a
   legenda e o CSS `.pill.<perfil>`.
5. `web/gaps-decisao.html` / `web/gaps-reuniao.html`: faixa de aviso + esconder
   as ações — **cosmético**; quem barra é o backend.
6. Atualizar `DEPLOY.md § 5`, o `README.md` e esta página.

## Armadilha registrada

O front manda o e-mail no path (`encodeURIComponent`). Na função serverless da
Vercel o `%40` chegava **cru** e o `lower()` não casava com o e-mail do banco —
todas as ações da linha do usuário morriam com um enganoso
`404 Usuário não encontrado`. Defesa: `_norm_email()` faz `unquote` quando há
`%`. Se voltar a aparecer, o 404 agora ecoa o e-mail recebido.
