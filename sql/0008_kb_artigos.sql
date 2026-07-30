-- ═══════════════════════════════════════════════════════════════════════════
-- 0008_kb_artigos.sql — Base de Conhecimento Protheus por cliente
-- Schema: cockpit (compartilhado com o cockpit Next.js)
-- Idempotente: pode rodar mais de uma vez.
--
-- Conceito: artigos de rotinas/usabilidades do Protheus, indexados por
--   Módulo → Assunto → Artigo, POR CLIENTE (customer). O consultor
--   (admin/comum) cria e edita; o cliente (perfis 'cliente' e 'leitor')
--   só lê os artigos publicados. Serve como base de Projeto e
--   Funcionalidades compartilhada com o cliente.
-- ═══════════════════════════════════════════════════════════════════════════

create extension if not exists pgcrypto;

create table if not exists cockpit.kb_artigos (
  id          uuid primary key default gen_random_uuid(),
  customer    text not null,                 -- ex.: 000348D0 (Digitro)
  modulo      text not null,                 -- ex.: FINANCEIRO, COMPRAS, FATURAMENTO
  assunto     text not null,                 -- ex.: Comissões, Contas a Pagar
  titulo      text not null,
  corpo_html  text,                          -- conteúdo do artigo (HTML)
  ordem       integer not null default 0,    -- ordenação dentro do assunto
  publicado   boolean not null default true, -- false = rascunho (só editor vê)
  created_by  text,
  created_at  timestamptz not null default now(),
  updated_by  text,
  updated_at  timestamptz not null default now()
);

create index if not exists ix_kb_artigos_customer
  on cockpit.kb_artigos (customer, modulo, assunto, ordem);

-- Defesa em profundidade: só o backend (DATABASE_URL/service role) toca aqui.
alter table cockpit.kb_artigos enable row level security;

-- ───────────────────────────────────────────────────────────────────────────
-- Seed — 3 artigos de exemplo da DIGITRO (customer 000348D0).
-- Idempotente: só insere se ainda não existir artigo com o mesmo título.
-- ───────────────────────────────────────────────────────────────────────────
insert into cockpit.kb_artigos (customer, modulo, assunto, titulo, corpo_html, ordem, publicado, created_by)
select '000348D0', 'FATURAMENTO', 'Comissões',
       'Visão geral do processo de Comissões no Protheus',
       $html$
<h2>Visão geral</h2>
<p>No Protheus, a comissão nasce no <strong>cadastro do vendedor (MATA040)</strong> e é
aplicada na venda: pedido de venda → faturamento → título financeiro. O cálculo padrão
considera o percentual do vendedor e as regras definidas por produto/cliente, e a
liberação do pagamento da comissão pode ocorrer por <em>emissão</em>, <em>faturamento</em>
ou <em>baixa do título</em> (recebimento), conforme parametrização.</p>

<h2>Rotinas envolvidas</h2>
<ul>
  <li><strong>MATA040</strong> — Cadastro de Vendedores (percentuais e regras).</li>
  <li><strong>MATA410</strong> — Pedido de Venda (vendedor e comissão por item).</li>
  <li><strong>MATA460/MATA461</strong> — Faturamento (geração do documento de saída).</li>
  <li><strong>MATA490</strong> — Manutenção de Comissões (consulta, ajuste e estorno).</li>
  <li><strong>FINA040/FINA070</strong> — Contas a Pagar (pagamento da comissão ao vendedor).</li>
</ul>

<h2>Fluxo padrão</h2>
<ol>
  <li>Vendedor cadastrado com percentual de comissão (MATA040).</li>
  <li>Pedido de venda informa o(s) vendedor(es) — até 5 por pedido — e o % por item.</li>
  <li>No faturamento, o sistema grava a comissão (tabela SE3).</li>
  <li>A liberação segue o parâmetro <code>MV_COMIS…</code> definido no projeto
      (emissão × baixa do título).</li>
  <li>MATA490 permite conferir, ajustar e estornar comissões antes do pagamento.</li>
</ol>

<h2>Pontos de atenção no projeto Digitro</h2>
<ul>
  <li>Regras específicas de comissão tratadas como GAP têm documentação própria
      (ver Tasks 00011816 / 00013021 / 00013025).</li>
  <li>O que não estiver descrito aqui como personalização segue o comportamento
      <strong>padrão</strong> do Protheus.</li>
</ul>
$html$,
       1, true, 'reges.werlang@gmail.com'
where not exists (
  select 1 from cockpit.kb_artigos
   where customer='000348D0'
     and titulo='Visão geral do processo de Comissões no Protheus');

insert into cockpit.kb_artigos (customer, modulo, assunto, titulo, corpo_html, ordem, publicado, created_by)
select '000348D0', 'FINANCEIRO', 'Contas a Pagar',
       'Aprovação e liberação de pagamentos (FINA050 / FINA070)',
       $html$
<h2>Visão geral</h2>
<p>O ciclo do Contas a Pagar no Protheus vai da <strong>inclusão do título
(FINA050)</strong> — manual ou gerado pelo recebimento de compras — até a
<strong>baixa (FINA070)</strong> ou o <strong>borderô de pagamento (FINA240)</strong>.
Quando o controle de alçadas está ativo, títulos e pagamentos passam por
<strong>aprovação</strong> antes da liberação.</p>

<h2>Passo a passo — pagamento com aprovação</h2>
<ol>
  <li><strong>Inclusão do título</strong>: FINA050 (ou automático via documento de
      entrada MATA103, já com a condição de pagamento).</li>
  <li><strong>Autorização</strong>: o aprovador confere e autoriza os pagamentos do
      dia/período conforme a alçada definida.</li>
  <li><strong>Borderô</strong>: FINA240 agrupa os títulos autorizados para remessa
      bancária (CNAB).</li>
  <li><strong>Baixa</strong>: com o retorno bancário processado (ou baixa manual
      FINA070), o título é liquidado e contabilizado.</li>
</ol>

<h2>Pontos de atenção no projeto Digitro</h2>
<ul>
  <li>O fluxo de autorização de pagamentos foi tema da reunião de escopo de
      17/07/2026 — o desenho aprovado prevê aprovação formal antes da remessa.</li>
  <li>Contabilização e conciliação bancária seguem o padrão (ver artigos do
      assunto Tesouraria quando publicados).</li>
</ul>
$html$,
       1, true, 'reges.werlang@gmail.com'
where not exists (
  select 1 from cockpit.kb_artigos
   where customer='000348D0'
     and titulo='Aprovação e liberação de pagamentos (FINA050 / FINA070)');

insert into cockpit.kb_artigos (customer, modulo, assunto, titulo, corpo_html, ordem, publicado, created_by)
select '000348D0', 'COMPRAS', 'Solicitação e Pedido de Compra',
       'Do pedido de compra ao recebimento (MATA110 → MATA121 → MATA103)',
       $html$
<h2>Visão geral</h2>
<p>O processo de compras padrão do Protheus encadeia:
<strong>Solicitação de Compra (MATA110)</strong> →
<strong>Pedido de Compra (MATA121)</strong> →
<strong>Documento de Entrada / Recebimento (MATA103)</strong>.
Com o controle de alçadas ativo, o pedido passa por <strong>liberação
(MATA097)</strong> antes de ser enviado ao fornecedor.</p>

<h2>Passo a passo</h2>
<ol>
  <li><strong>MATA110</strong> — a área solicitante registra a necessidade
      (produto, quantidade, prazo).</li>
  <li><strong>MATA121</strong> — o comprador transforma a solicitação em pedido,
      informando fornecedor, preço e condição de pagamento.</li>
  <li><strong>MATA097</strong> — aprovação do pedido conforme alçada (valor/grupo
      de aprovação).</li>
  <li><strong>MATA103</strong> — no recebimento físico/fiscal, o documento de
      entrada amarra pedido × nota fiscal, atualiza estoque e gera o título no
      Contas a Pagar.</li>
</ol>

<h2>Pontos de atenção</h2>
<ul>
  <li>Divergências de preço/quantidade no recebimento seguem as tolerâncias
      parametrizadas; fora da tolerância o documento é bloqueado.</li>
  <li>Itens de importação e serviços têm tratamentos específicos (módulos
      Importação/GFE quando aplicáveis).</li>
</ul>
$html$,
       1, true, 'reges.werlang@gmail.com'
where not exists (
  select 1 from cockpit.kb_artigos
   where customer='000348D0'
     and titulo='Do pedido de compra ao recebimento (MATA110 → MATA121 → MATA103)');

-- PostgREST precisa reler o schema depois de DDL
notify pgrst, 'reload schema';
