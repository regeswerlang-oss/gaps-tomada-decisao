-- ═══════════════════════════════════════════════════════════════════════════
-- 0009_kb_link_publico.sql — link público (somente leitura) da Base de
-- Conhecimento, por cliente. Schema: cockpit. Idempotente.
--
-- Um token secreto por customer. A URL /kb/publico/<token> abre a base do
-- cliente SEM login, exibindo apenas artigos publicados. O token é gerado
-- pelo backend (get-or-create em /api/kb/link) e pode ser renovado ou
-- desativado a qualquer momento.
-- ═══════════════════════════════════════════════════════════════════════════

create extension if not exists pgcrypto;

create table if not exists cockpit.kb_links (
  customer   text primary key,              -- ex.: 000348D0 (Digitro)
  token      text not null unique
             default replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''),
  ativo      boolean not null default true,
  created_by text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Defesa em profundidade: só o backend (DATABASE_URL/service role) toca aqui.
alter table cockpit.kb_links enable row level security;

-- PostgREST precisa reler o schema depois de DDL
notify pgrst, 'reload schema';
