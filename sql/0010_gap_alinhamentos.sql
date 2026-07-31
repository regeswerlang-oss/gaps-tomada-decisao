-- =====================================================================
-- Gaps · Tomada de Decisão — Alinhamento do GAP
-- Migration 0010 · cria cockpit.gap_alinhamentos (+ histórico de versões)
--
-- Rodar no SQL Editor do Supabase (projeto do schema `cockpit`).
-- Idempotente: pode rodar mais de uma vez sem quebrar.
-- =====================================================================

create schema if not exists cockpit;

-- ---------------------------------------------------------------------
-- Tabela principal: 1 linha por ticket (GAP)
-- ---------------------------------------------------------------------
create table if not exists cockpit.gap_alinhamentos (
    uuid_ticket             text primary key,
    task_id                 text,          -- número da Task (ex. 00010891)
    customer                text,          -- código do cliente (ex. 000348D0)

    -- os 4 campos do pedido
    questionamento_cliente  text,          -- (3) o que o cliente questionou
    argumentacao_interna    text,          -- (2) argumentação TOTVS SC — NUNCA vai ao cliente
    alinhamento_reuniao     text,          -- (4) o que foi alinhado na reunião TOTVS SC × Cliente
    retorno_cliente         text,          -- (1) retorno FORMAL ao cliente

    -- auditoria
    created_by              text,
    created_at              timestamptz not null default now(),
    updated_by              text,
    updated_at              timestamptz not null default now()
);

comment on table  cockpit.gap_alinhamentos                        is 'Alinhamento comercial/técnico de cada GAP: questionamento do cliente, argumentação interna, alinhamento da reunião e retorno formal.';
comment on column cockpit.gap_alinhamentos.questionamento_cliente is 'EXTERNO-ORIGEM: o questionamento levantado pelo cliente sobre o GAP.';
comment on column cockpit.gap_alinhamentos.argumentacao_interna   is 'INTERNO: argumentação da TOTVS SC. NAO expor no dashboard comercial nem em e-mail ao cliente.';
comment on column cockpit.gap_alinhamentos.alinhamento_reuniao    is 'EXTERNO: o que foi acordado na reunião entre TOTVS SC e Cliente.';
comment on column cockpit.gap_alinhamentos.retorno_cliente        is 'EXTERNO: retorno formal a ser comunicado ao cliente.';

create index if not exists ix_gap_alin_customer on cockpit.gap_alinhamentos (customer);
create index if not exists ix_gap_alin_task     on cockpit.gap_alinhamentos (task_id);
create index if not exists ix_gap_alin_updated  on cockpit.gap_alinhamentos (updated_at desc);

-- ---------------------------------------------------------------------
-- Histórico: guarda a versão ANTERIOR a cada alteração
-- (permite auditar "quem mudou o retorno ao cliente e quando")
-- ---------------------------------------------------------------------
create table if not exists cockpit.gap_alinhamentos_hist (
    id                      bigserial primary key,
    uuid_ticket             text not null,
    task_id                 text,
    customer                text,
    questionamento_cliente  text,
    argumentacao_interna    text,
    alinhamento_reuniao     text,
    retorno_cliente         text,
    versao_de               timestamptz,   -- updated_at da versão arquivada
    arquivado_em            timestamptz not null default now(),
    arquivado_por           text
);

create index if not exists ix_gap_alin_hist_ticket on cockpit.gap_alinhamentos_hist (uuid_ticket, arquivado_em desc);

-- ---------------------------------------------------------------------
-- Trigger: mantém updated_at e arquiva a versão anterior
-- ---------------------------------------------------------------------
create or replace function cockpit.fn_gap_alinhamentos_audit()
returns trigger
language plpgsql
as $$
begin
    insert into cockpit.gap_alinhamentos_hist (
        uuid_ticket, task_id, customer,
        questionamento_cliente, argumentacao_interna,
        alinhamento_reuniao, retorno_cliente,
        versao_de, arquivado_por
    ) values (
        old.uuid_ticket, old.task_id, old.customer,
        old.questionamento_cliente, old.argumentacao_interna,
        old.alinhamento_reuniao, old.retorno_cliente,
        old.updated_at, new.updated_by
    );

    new.updated_at := now();
    new.created_at := old.created_at;
    new.created_by := coalesce(old.created_by, new.created_by);
    return new;
end;
$$;

drop trigger if exists tg_gap_alinhamentos_audit on cockpit.gap_alinhamentos;
create trigger tg_gap_alinhamentos_audit
    before update on cockpit.gap_alinhamentos
    for each row
    when (
        old.questionamento_cliente is distinct from new.questionamento_cliente
     or old.argumentacao_interna   is distinct from new.argumentacao_interna
     or old.alinhamento_reuniao    is distinct from new.alinhamento_reuniao
     or old.retorno_cliente        is distinct from new.retorno_cliente
    )
    execute function cockpit.fn_gap_alinhamentos_audit();

-- ---------------------------------------------------------------------
-- View de consumo para o DASHBOARD COMERCIAL (fase 2)
-- Expõe SOMENTE os campos externos — a argumentação interna fica de fora.
-- ---------------------------------------------------------------------
create or replace view cockpit.vw_gap_alinhamento_externo as
select
    a.uuid_ticket,
    a.task_id,
    a.customer,
    a.questionamento_cliente,
    a.alinhamento_reuniao,
    a.retorno_cliente,
    a.updated_at,
    a.updated_by
from cockpit.gap_alinhamentos a;

comment on view cockpit.vw_gap_alinhamento_externo is 'Visão segura do alinhamento para o dashboard comercial: sem argumentacao_interna.';

-- ---------------------------------------------------------------------
-- Conferência
-- ---------------------------------------------------------------------
-- select count(*) from cockpit.gap_alinhamentos;
-- select * from cockpit.gap_alinhamentos order by updated_at desc limit 20;
