-- ═══════════════════════════════════════════════════════════════════════════
-- 0007_perfil_leitor.sql — perfil 'leitor' (somente visualização)
-- Schema: cockpit (compartilhado com o cockpit Next.js)
-- Idempotente: pode rodar mais de uma vez.
--
-- Os 4 perfis, do mais forte ao mais fraco:
--   admin   → vê TODOS os clientes e administra usuários/liberações.
--   comum   → só os clientes liberados; escreve tudo (Task, tags, ocorrência,
--             decisão/estimativa).
--   cliente → só os clientes liberados; LÊ e DECIDE/ESTIMA (cockpit.decisoes),
--             mas não altera nada no Tasks SC.
--   leitor  → só os clientes liberados; LÊ e mais nada. Nenhuma escrita, nem
--             no Tasks SC nem em cockpit.decisoes. Único write permitido: a
--             própria senha (/api/conta/senha).
--
-- O `leitor` NÃO é admin — o trigger sync_perfil_is_admin já cuida disso
-- (is_admin := perfil = 'admin'), então nada a mudar nele.
-- ═══════════════════════════════════════════════════════════════════════════

alter table cockpit.usuarios_login drop constraint if exists usuarios_login_perfil_chk;
alter table cockpit.usuarios_login add  constraint usuarios_login_perfil_chk
  check (perfil in ('admin', 'comum', 'cliente', 'leitor'));

-- PostgREST precisa reler o schema depois de DDL
notify pgrst, 'reload schema';
