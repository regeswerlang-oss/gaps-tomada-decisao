-- =====================================================================
-- Gaps · Tomada de Decisão — credencial Gmail por usuário
-- Migration 0011 · cria cockpit.gmail_credenciais
--
-- Rodar no SQL Editor do Supabase (projeto do schema `cockpit`).
-- Idempotente: pode rodar mais de uma vez sem quebrar.
--
-- Para que serve
--   O botão "E-mail dos GAPs" cria um RASCUNHO DE VERDADE na caixa do próprio
--   consultor, via IMAP APPEND na pasta de Rascunhos do Gmail — mesma técnica
--   do gmail_server.py (dashboard2) do totvs-dashboard. Para isso cada usuário
--   guarda aqui o seu e-mail e a sua App Password de 16 caracteres
--   (https://myaccount.google.com/apppasswords — exige 2FA ligado).
--
-- Segurança
--   A App Password NUNCA é gravada em claro: vai cifrada (Fernet/AES-128-CBC +
--   HMAC) com uma chave derivada do SESSION_SECRET da Vercel. Quem lê o banco
--   sem o SESSION_SECRET não consegue usar a senha. O backend também nunca
--   devolve a senha para o front — só diz se está configurada.
-- =====================================================================

create schema if not exists cockpit;

create table if not exists cockpit.gmail_credenciais (
    usuario       text primary key,        -- e-mail de login em cockpit.usuarios_login
    gmail_email   text not null,           -- a conta Gmail que cria o rascunho
    senha_cif     text not null,           -- App Password cifrada (token Fernet)
    validado_em   timestamptz,             -- último login IMAP bem-sucedido
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

comment on table  cockpit.gmail_credenciais is
  'App Password do Gmail por usuário, cifrada — usada para criar rascunhos via IMAP APPEND.';
comment on column cockpit.gmail_credenciais.senha_cif is
  'Token Fernet. Chave derivada do SESSION_SECRET da Vercel — trocar o SESSION_SECRET invalida todas.';

-- Toque de higiene: o schema inteiro roda com RLS ligado (ver 31/07). O acesso
-- é sempre pelo backend, com a connection string de serviço.
alter table cockpit.gmail_credenciais enable row level security;

-- ---------------------------------------------------------------------
-- Conferência
-- ---------------------------------------------------------------------
-- select usuario, gmail_email, validado_em, updated_at
--   from cockpit.gmail_credenciais order by updated_at desc;
