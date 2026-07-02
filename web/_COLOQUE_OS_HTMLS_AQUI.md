# Coloque aqui os dois HTMLs

Copie para esta pasta `web/` (mesmo nível deste arquivo):

- `gaps-decisao.html`  → tela principal (board ágil 4 colunas)
- `gaps-reuniao.html`  → tela de reunião de decisão (UX Olim)

O backend Flask serve estes arquivos **com porta de login**. Não os coloque
em `public/` — essa pasta é servida estaticamente pelo Vercel e furaria o login.

Os HTMLs chamam a API nos mesmos caminhos de antes (`/api/clientes`,
`/api/tickets?cliente=`, `/api/ticket/<uuid>/update`, `/api/decisoes`, etc.),
então funcionam sem alterar o JavaScript. Se alguma tela usava
`http://localhost:8090` ou `http://localhost:8081` fixo na URL, troque por
caminho relativo (ex.: `/api/...`).
