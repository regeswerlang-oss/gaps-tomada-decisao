-- ═══════════════════════════════════════════════════════════════════════════
-- 0006_perfis_acesso.sql — perfis de acesso + clientes autorizados
-- Schema: cockpit (compartilhado com o cockpit Next.js — por isso is_admin fica)
-- Idempotente: pode rodar mais de uma vez.
-- ═══════════════════════════════════════════════════════════════════════════

-- 1) Colunas em usuarios_login ─────────────────────────────────────────────
alter table cockpit.usuarios_login add column if not exists is_admin    boolean;
alter table cockpit.usuarios_login add column if not exists ativo       boolean;
alter table cockpit.usuarios_login add column if not exists perfil      text;
alter table cockpit.usuarios_login add column if not exists updated_at  timestamptz;
alter table cockpit.usuarios_login add column if not exists created_at  timestamptz;
alter table cockpit.usuarios_login add column if not exists created_by  text;

update cockpit.usuarios_login set is_admin = false where is_admin is null;
update cockpit.usuarios_login set ativo    = true  where ativo    is null;

-- backfill do perfil a partir do is_admin que já existia
update cockpit.usuarios_login
   set perfil = case when coalesce(is_admin, false) then 'admin' else 'comum' end
 where perfil is null;

alter table cockpit.usuarios_login alter column is_admin set default false;
alter table cockpit.usuarios_login alter column ativo    set default true;
alter table cockpit.usuarios_login alter column perfil   set default 'comum';
alter table cockpit.usuarios_login alter column is_admin set not null;
alter table cockpit.usuarios_login alter column ativo    set not null;
alter table cockpit.usuarios_login alter column perfil   set not null;

alter table cockpit.usuarios_login drop constraint if exists usuarios_login_perfil_chk;
alter table cockpit.usuarios_login add  constraint usuarios_login_perfil_chk
  check (perfil in ('admin', 'comum', 'cliente'));

-- 2) perfil ⇄ is_admin sempre coerentes ────────────────────────────────────
-- O cockpit Next.js ainda lê `is_admin`. Quem escrever num, atualiza o outro.
create or replace function cockpit.sync_perfil_is_admin() returns trigger as $$
begin
  if TG_OP = 'INSERT' then
    new.perfil := coalesce(new.perfil,
                    case when coalesce(new.is_admin, false) then 'admin' else 'comum' end);
    new.is_admin := (new.perfil = 'admin');
  else
    if new.perfil is distinct from old.perfil then
      new.is_admin := (new.perfil = 'admin');          -- perfil manda
    elsif new.is_admin is distinct from old.is_admin then
      new.perfil := case when new.is_admin then 'admin'
                         when old.perfil = 'admin' then 'comum'
                         else old.perfil end;           -- is_admin manda
    end if;
  end if;
  new.updated_at := now();
  return new;
end $$ language plpgsql;

drop trigger if exists trg_sync_perfil on cockpit.usuarios_login;
create trigger trg_sync_perfil
  before insert or update on cockpit.usuarios_login
  for each row execute function cockpit.sync_perfil_is_admin();

-- 3) Clientes autorizados por usuário ──────────────────────────────────────
create table if not exists cockpit.usuario_clientes (
  email      text not null,
  customer   text not null,
  created_at timestamptz not null default now(),
  created_by text,
  primary key (email, customer)
);
create index if not exists ix_usuario_clientes_email
  on cockpit.usuario_clientes (lower(email));

-- Defesa em profundidade: só o backend (service_role / DATABASE_URL) toca aqui.
alter table cockpit.usuario_clientes enable row level security;

-- 4) PostgREST precisa reler o schema depois de DDL ────────────────────────
notify pgrst, 'reload schema';
