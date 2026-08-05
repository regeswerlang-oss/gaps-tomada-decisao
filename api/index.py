#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gaps — Tomada de Decisão · backend serverless (Vercel)
======================================================
Porte do gaps_server.py (porta 8090) para funções serverless na Vercel,
lendo/gravando no Supabase existente (schema `cockpit`) e mantendo paridade
total com o Tasks SC (api.tscst.com.br) para leitura/escrita ao vivo.

Um único app Flask (WSGI) responde a TODAS as rotas — o vercel.json faz
rewrite de `/(.*)` para esta função, então servimos também os HTMLs (com
porta de login) sem CORS.

Rotas
-----
Auth / páginas
  GET  /                      → gaps-decisao.html (exige login) senão /login
  GET  /gaps-decisao.html     → idem (exige login)
  GET  /gaps-reuniao.html     → tela de reunião (exige login)
  GET  /login                 → login.html (público)
  GET  /admin                 → admin.html (tela de acessos; exige login)
  POST /api/login             → {email, senha} → cookie de sessão
  POST /api/logout            → limpa a sessão
  GET  /api/me                → sessão atual (+ perfil)

Acessos — perfis admin | comum | cliente | leitor
  Matriz (o que cada um GRAVA; todos leem só os clientes liberados, menos o
  admin, que vê tudo):
                 Tasks SC   decisão/estimativa   admin de acessos   própria senha
    admin           sim            sim                 sim               sim
    comum           sim            sim                 não               sim
    cliente         não            sim                 não               sim
    leitor          não            não                 não               sim
  Portões: require_tasks_write() (barra cliente+leitor) · require_write()
  (barra leitor) · require_admin() · deny_simulacao() (barra o 'ver como').
  GET  /api/admin/usuarios                     → usuários + clientes liberados
  POST /api/admin/usuarios                     → cria/edita {email,nome,perfil,senha?}
  POST /api/admin/usuarios/<email>/ativo       → {ativo}
  POST /api/admin/usuarios/<email>/perfil      → {perfil}
  POST /api/admin/usuarios/<email>/senha       → {senha} (admin redefine)
  POST /api/admin/usuarios/<email>/clientes    → {customers:[...]} (checklist)
  GET  /api/admin/clientes                     → catálogo p/ o checklist
  POST /api/conta/senha                        → {atual, nova} (própria senha)

Dados (Supabase) — exigem login
  GET  /api/clientes                       → cockpit.clientes
  GET  /api/tickets?cliente=digitro        → cockpit.tickets (+ tags)
  GET  /api/decisoes?cliente=digitro       → cockpit.decisoes
  POST /api/decisoes?cliente=digitro       → upsert cockpit.decisoes
  GET  /api/drive-index                    → cockpit.integration_config['drive_index']

Base de Conhecimento Protheus (kb.html — Módulo → Assunto → Artigo, por cliente):
  GET  /kb                                 → kb.html (login)
  GET  /api/kb/artigos?cliente=digitro     → lista artigos (leitor só vê publicado)
  POST /api/kb/artigos?cliente=digitro     → cria/edita artigo (só admin/comum)
  POST /api/kb/artigos/<id>/excluir        → exclui artigo (só admin/comum)
  GET  /api/kb/link?cliente=digitro        → get-or-create link público (só editor)
  POST /api/kb/link?cliente=digitro        → {acao: renovar|ativar|desativar}
  GET  /kb/publico/<token>                 → kb.html público (sem login, só leitura)
  GET  /api/kb/publico/<token>/artigos     → artigos publicados do dono do token

Tasks SC (ao vivo) — exigem login
  GET  /api/ticket/<uuid>[?tags=1]         → detalhe do ticket (tags=1: tags ao
                                             vivo + espelho + catálogo)
  GET  /api/ticket/<uuid>/history          → histórico (+ NOTEBOOKLM:/PERSONALIZACAO:)
  GET  /api/tags-catalog[?search=]         → catálogo de tags
  POST /api/ticket/<uuid>/update           → GET→merge→PUT + espelho no Supabase
  POST /api/ticket/<uuid>/history          → grava ocorrência (PERSONALIZACAO:/avulsa)
  POST /api/refresh?cliente=DIGITRO        → re-sincroniza tickets do cliente ao vivo

Gmail
  GET  /api/gmail/health                   → status do modo de rascunho
  GET  /api/gmail/credencial               → a conta Gmail conectada (sem a senha)
  POST /api/gmail/credencial               → conecta (valida por IMAP antes de gravar)
  POST /api/gmail/credencial/remover       → desconecta
  POST /api/gmail/draft                    → rascunho no Gmail do usuário (IMAP
                                             APPEND); sem credencial, cai em
                                             cockpit.email_drafts
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import imaplib
import json
import os
import re
import secrets
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, Response, request, redirect, make_response
from werkzeug.exceptions import HTTPException

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
# Pasta dos HTMLs/assets. NÃO se chama "public" de propósito: o Vercel serve
# "public/" estaticamente ANTES da função, o que furaria a porta de login.
# Aqui tudo passa pelo Flask e respeita a autenticação.
PUBLIC_DIR = BASE_DIR / "web"

DATABASE_URL = os.environ.get("DATABASE_URL", "")
TASKS_BASE = os.environ.get("TASKS_SC_BASE_URL", "https://api.tscst.com.br/restAPI").rstrip("/")
TASKS_USER = os.environ.get("TASKS_USERNAME", "")
TASKS_PASS = os.environ.get("TASKS_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-insecure-secret-change-me")
SESSION_TTL = 12 * 3600  # 12h
COOKIE_NAME = "gaps_sess"

NLM_PREFIX = "NOTEBOOKLM:"
TEC_PREFIX = "PERSONALIZACAO:"
ETAPA_TAGS = ["GAP", "LEVANTAR REQUISITOS", "LEVANTAMENTO", "ORCAMENTO PENDENTE"]
CLASS_TAGS = ["NECESSARIO", "DESEJAVEL", "INDEFINIDO", "OPCIONAL", "PRIORIDADE"]

# Campos aceitos no PUT do Tasks SC (idêntico à skill api-tasks-totvs-sc)
ALLOWED_PUT = {
    "description", "user_assigned", "assigned_customer", "due_date", "start_date",
    "end_date", "start_time", "end_time", "reminder_date", "time_estimate",
    "priority", "title", "status", "tags", "observer", "milestone", "progress",
    "ticket_customer", "issue_totvs", "ticket_totvs", "service",
    "service_description", "activity", "project",
}

app = Flask(__name__)


class _VercelRewritePath:
    """Restaura o caminho ORIGINAL da requisição.

    Armadilha real (2026-07-30): o `vercel.json` faz
    `rewrites: [{source:"/(.*)", destination:"/api/index"}]`. A Vercel entrega
    à função o caminho de DESTINO — ou seja, `PATH_INFO` chega SEMPRE como
    `/api/index`, para `/`, `/login`, `/api/health`, tudo. Nenhuma rota do
    Flask casa e todas caem no catch-all `/<path:asset>`, que responde
    `{"ok": false, "error": "Rota de API desconhecida."}` — o site inteiro 404.

    Contrato: o `vercel.json` manda o caminho real em `?__path=/…`; aqui a
    gente devolve para o `PATH_INFO` e tira o `__path` da query, antes do
    roteamento. Fallback: header `x-vercel-original-path`, se existir.
    Só age quando `PATH_INFO` é o caminho da própria função — se um dia a
    Vercel voltar a preservar o path, este middleware fica inerte.
    """

    FUNC_PATHS = ("/api/index", "/api/index/", "/api/index.py")

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        if environ.get("PATH_INFO", "") in self.FUNC_PATHS:
            pares = parse_qsl(environ.get("QUERY_STRING", ""), keep_blank_values=True)
            real, resto = None, []
            for k, v in pares:
                if k == "__path" and real is None:
                    real = v
                else:
                    resto.append((k, v))
            if not real:
                real = environ.get("HTTP_X_VERCEL_ORIGINAL_PATH") or None
            if real:
                if not real.startswith("/"):
                    real = "/" + real
                environ["PATH_INFO"] = real
                environ["QUERY_STRING"] = urlencode(resto)
                environ["RAW_URI"] = real + (("?" + environ["QUERY_STRING"]) if environ["QUERY_STRING"] else "")
        return self.wsgi_app(environ, start_response)


class _StripGapsPrefix:
    """Compat: telas antigas chamam /gaps/api/... — removemos o prefixo /gaps
    antes do roteamento, para que /gaps/api/x e /api/x apontem ao mesmo lugar."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        p = environ.get("PATH_INFO", "")
        if p == "/gaps" or p.startswith("/gaps/"):
            environ["PATH_INFO"] = p[len("/gaps"):] or "/"
        return self.wsgi_app(environ, start_response)


app.wsgi_app = _StripGapsPrefix(app.wsgi_app)
app.wsgi_app = _VercelRewritePath(app.wsgi_app)  # deve rodar ANTES do strip /gaps

# ─────────────────────────────────────────────────────────────────────────────
# Helpers gerais
# ─────────────────────────────────────────────────────────────────────────────
def _json(obj, code=200):
    return Response(json.dumps(obj, ensure_ascii=False, default=str),
                    status=code, mimetype="application/json")


def _err(code, msg):
    return _json({"ok": False, "error": msg}, code)


def _strip_html(s):
    return re.sub(r"<[^>]+>", " ", s or "").strip()


def _slug(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _slug_first(s):
    """slug do primeiro token — 'DIGITRO TECNOLOGIA' -> 'digitro'."""
    first = (s or "").strip().split()
    return _slug(first[0]) if first else ""


# ─────────────────────────────────────────────────────────────────────────────
# Postgres (Supabase)
# ─────────────────────────────────────────────────────────────────────────────
def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurada.")
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    conn.autocommit = True
    return conn


def q(sql, params=None, one=False):
    with db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            if cur.description is None:
                return None
            rows = cur.fetchall()
            return (rows[0] if rows else None) if one else rows


def execute(sql, params=None):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())


# ─────────────────────────────────────────────────────────────────────────────
# Sessão / login
# ─────────────────────────────────────────────────────────────────────────────
def _sign(payload: str) -> str:
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return sig


def make_session(email: str, nome: str, view_as: str | None = None) -> str:
    exp = int(time.time()) + SESSION_TTL
    d = {"e": email, "n": nome, "x": exp}
    if view_as:
        d["v"] = view_as        # admin simulando a visão de outro usuário
    raw = json.dumps(d, ensure_ascii=False)
    b = base64.urlsafe_b64encode(raw.encode()).decode()
    return f"{b}.{_sign(b)}"


def read_session():
    tok = request.cookies.get(COOKIE_NAME, "")
    if not tok or "." not in tok:
        return None
    b, sig = tok.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(b)):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(b.encode()).decode())
    except Exception:
        return None
    if int(data.get("x", 0)) < int(time.time()):
        return None
    return data


def current_user():
    """Usuário REAL do login (nunca o simulado). É quem responde por auditoria."""
    s = read_session()
    return s.get("e") if s else None


def is_admin(email):
    if not email:
        return False
    r = q("select coalesce(is_admin,false) adm from cockpit.usuarios_login "
          "where lower(email)=%s", (email.lower(),), one=True)
    return bool(r and r["adm"])


# ── Perfis de acesso ─────────────────────────────────────────────────────────
# admin   → vê todos os clientes e administra os acessos.
# comum   → só os clientes liberados, mas escreve tudo neles (Task, tags,
#           ocorrência, decisão).
# cliente → só os clientes liberados; LÊ e DECIDE/ESTIMA (cockpit.decisoes),
#           mas não altera nada no Tasks SC.
# leitor  → só os clientes liberados; LÊ e mais nada. Não decide, não estima,
#           não toca no Tasks SC. Único write: a própria senha.
#
# Dois portões, um por "destino" da escrita:
#   require_tasks_write() → escritas que SAEM para o Tasks SC  (barra cliente e leitor)
#   require_write()       → escritas no nosso banco: decisão/estimativa (barra leitor)
PERFIS = ("admin", "comum", "cliente", "leitor")
PERFIL_LABEL = {"admin": "Administrador", "comum": "Comum",
                "cliente": "Cliente", "leitor": "Leitor (somente leitura)"}
# Perfis que não gravam NADA (exceto a própria senha).
PERFIS_SO_LEITURA = ("leitor",)


def perfil_de(email):
    """Perfil do usuário. Faz fallback pelo is_admin caso a coluna ainda esteja
    vazia (banco sem a migration 0006)."""
    if not email:
        return None
    r = q("""select coalesce(nullif(perfil,''),
                    case when coalesce(is_admin,false) then 'admin' else 'comum' end) as p
             from cockpit.usuarios_login where lower(email)=%s""",
          (email.lower(),), one=True)
    return r["p"] if r else None


def effective_user():
    """Usuário cuja VISÃO vale. Só honra o 'ver como' se quem está logado for
    admin de verdade — a simulação jamais amplia acesso, apenas restringe."""
    s = read_session()
    if not s:
        return None
    alvo = s.get("v")
    if alvo and is_admin(s.get("e")):
        return alvo
    return s.get("e")


def simulando():
    return bool(current_user()) and effective_user() != current_user()


def deny_simulacao():
    """Escrita é bloqueada durante a simulação: você não grava no lugar de outro."""
    if simulando():
        return _err(409, "Você está no modo 'ver como'. Saia da simulação para gravar.")
    return None


def require_admin():
    if not current_user():
        return _err(401, "Não autenticado.")
    if not is_admin(current_user()):
        return _err(403, "Apenas administradores.")
    return None


def require_auth():
    """Retorna None se autenticado, ou uma Response 401 se não."""
    if current_user():
        return None
    return _err(401, "Não autenticado.")


def require_write():
    """Portão de QUALQUER escrita de conteúdo — inclusive a decisão/estimativa,
    que fica no nosso banco (cockpit.decisoes) e não sai para o Tasks SC.
    Só o perfil 'leitor' é barrado aqui. Usa o usuário REAL — durante o 'ver
    como' quem barra é o deny_simulacao().

    Exceção deliberada: /api/conta/senha. Trocar a própria senha é uma escrita,
    mas é sobre a própria conta — o leitor precisa poder.
    """
    email = current_user()
    if not email:
        return _err(401, "Não autenticado.")
    if perfil_de(email) in PERFIS_SO_LEITURA:
        return _err(403, "Seu perfil (Leitor) é somente de visualização: "
                         "você consulta os GAPs, mas não grava nada.")
    return None


def require_tasks_write():
    """Portão das escritas que saem deste app para o Tasks SC: alterar a Task,
    tags, ocorrência, catálogo, refresh e rascunho de e-mail. O perfil 'cliente'
    consulta e decide, mas não mexe no Tasks SC; o 'leitor' não faz nem uma
    coisa nem outra. Usa o usuário REAL — durante o 'ver como' quem barra é o
    deny_simulacao()."""
    email = current_user()
    if not email:
        return _err(401, "Não autenticado.")
    perfil = perfil_de(email)
    if perfil in PERFIS_SO_LEITURA:
        return _err(403, "Seu perfil (Leitor) é somente de visualização: "
                         "você consulta os GAPs, mas não grava nada.")
    if perfil == "cliente":
        return _err(403, "Seu perfil (Cliente) permite consultar e decidir, "
                         "mas não alterar dados no Tasks SC.")
    return None


# ── Controle de acesso por CLIENTE (customer) ────────────────────────────────
# Regra (modo estrito, igual ao dashboard Next.js do cockpit):
#   admin           → None  = vê TODOS os clientes.
#   usuário comum   → set de customers liberados (pode ser vazio = não vê nada).
def allowed_customers():
    email = effective_user()      # respeita o "ver como"
    if not email:
        return set()
    row = q("select coalesce(is_admin,false) as adm from cockpit.usuarios_login "
            "where lower(email)=%s", (email.lower(),), one=True)
    if row and row["adm"]:
        return None
    rows = q("select customer from cockpit.usuario_clientes where lower(email)=%s",
             (email.lower(),))
    return {r["customer"] for r in rows}


def deny_customer(customer):
    """None se o usuário pode ver este customer; senão Response 403."""
    allowed = allowed_customers()
    if allowed is None or customer in allowed:
        return None
    return _err(403, "Sem acesso a este cliente.")


def deny_uuid(uuid):
    """Bloqueia acesso a um ticket cujo customer não está liberado."""
    allowed = allowed_customers()
    if allowed is None:
        return None
    row = q("select customer from cockpit.tickets where uuid_ticket=%s",
            (uuid.upper(),), one=True)
    cust = row["customer"] if row else None
    if cust and cust in allowed:
        return None
    return _err(403, "Sem acesso a este ticket.")


def hash_scrypt(senha: str) -> str:
    """Gera `scrypt$<salt hex>$<hash hex>` no MESMO formato do scripts/set_password.py
    e do Node do Cockpit: o salt entra como STRING (o próprio hex em UTF-8),
    N=16384, r=8, p=1, dklen=64. Assim a senha criada aqui vale nos dois apps.
    A senha em claro nunca é gravada nem logada."""
    salt_hex = secrets.token_hex(16)
    dk = hashlib.scrypt(senha.encode(), salt=salt_hex.encode(), n=16384, r=8, p=1,
                        dklen=64, maxmem=132 * 1024 * 1024)
    return f"scrypt${salt_hex}${dk.hex()}"


def valida_senha(senha: str):
    """None se a senha serve; senão a mensagem do problema."""
    if len(senha or "") < 8:
        return "A senha precisa ter ao menos 8 caracteres."
    return None


def verify_scrypt(stored: str, senha: str) -> bool:
    """Formato: scrypt$<salt hex>$<hash hex>.

    COMPATÍVEL com o Node do Cockpit: `scryptSync(pw, saltString, 64)` passa o
    salt como STRING (o próprio hex em UTF-8), N=16384, r=8, p=1. Tentamos essa
    variante primeiro (a real) e, como fallback, o salt decodificado de hex
    (formato antigo do set_password.py do Gaps).
    """
    try:
        scheme, salt_hex, hash_hex = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    dklen = len(hash_hex) // 2
    r = int(os.environ.get("SCRYPT_R", 8))
    p = int(os.environ.get("SCRYPT_P", 1))
    env_n = os.environ.get("SCRYPT_N")
    n_candidates = [int(env_n)] if env_n else [16384, 32768, 8192, 65536, 4096]
    salt_variants = [salt_hex.encode()]          # utf8 do hex (Node/Cockpit) ← real
    try:
        salt_variants.append(bytes.fromhex(salt_hex))   # hex decodificado (legado)
    except ValueError:
        pass
    pw = senha.encode()
    for salt in salt_variants:
        for n in n_candidates:
            try:
                dk = hashlib.scrypt(pw, salt=salt, n=n, r=r, p=p,
                                    dklen=dklen, maxmem=132 * 1024 * 1024)
            except Exception:
                continue
            if hmac.compare_digest(dk.hex(), hash_hex):
                return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Tasks SC — OAuth + chamadas
# ─────────────────────────────────────────────────────────────────────────────
_token_cache = {"tok": None, "exp": 0}


def tasks_token(force=False):
    now = time.time()
    if not force and _token_cache["tok"] and _token_cache["exp"] - 120 > now:
        return _token_cache["tok"]
    r = requests.post(
        f"{TASKS_BASE}/api/oauth2/v1/token",
        data={"grant_type": "password", "username": TASKS_USER, "password": TASKS_PASS},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"OAuth Tasks SC falhou HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    _token_cache["tok"] = d["access_token"]
    _token_cache["exp"] = now + int(d.get("expires_in", 3600))
    return _token_cache["tok"]


def tasks_request(method, path, params=None, body=None, _retry=True):
    """path relativo a {BASE}/custom/tscst/tasks — ex.: '/tickets/<uuid>'."""
    url = f"{TASKS_BASE}/custom/tscst/tasks{path}"
    tok = tasks_token()
    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    r = requests.request(method, url, params=params, json=body, headers=headers, timeout=60)
    if r.status_code == 401 and _retry:
        tasks_token(force=True)
        return tasks_request(method, path, params, body, _retry=False)
    try:
        data = r.json() if r.text else {}
    except ValueError:
        data = r.text
    err = None if r.status_code < 400 else (
        (data.get("message") if isinstance(data, dict) else str(data)) or f"HTTP {r.status_code}")
    return data, r.status_code, err


_TAG_CATALOG_CACHE = {"map": None, "exp": 0}


def _catalog_pages(page=1, max_pages=200, search=None):
    """Itera o catálogo de tags do Tasks SC a partir de `page`.

    CUIDADO (aprendido na dor): este endpoint devolve UMA LINHA POR ASSOCIAÇÃO —
    a mesma tag repete para cada ticket que a usa — ordenado por nome. Varrer
    tudo em tempo de request é inviável (estoura o timeout). Por isso o catálogo
    é sincronizado para `cockpit.tags_catalogo` e lido de lá.
    """
    while page <= max_pages:
        params = {"page": page, "pageSize": 200, "order": "tag", "fields": "id,tag"}
        if search:
            params["search"] = search
        data, code, _ = tasks_request("GET", "/tickets/tags", params=params)
        if code != 200 or not isinstance(data, dict):
            return
        for it in (data.get("items") or []):
            if it.get("tag") and it.get("id"):
                yield page, str(it["tag"]).strip(), it["id"]
        if not data.get("hasNext"):
            return
        page += 1


def _tags_catalog_map(force=False):
    """{NOME_UPPER: id} lido do SUPABASE (instantâneo)."""
    now = time.time()
    if not force and _TAG_CATALOG_CACHE["map"] is not None and _TAG_CATALOG_CACHE["exp"] > now:
        return _TAG_CATALOG_CACHE["map"]
    rows = q("select id, tag from cockpit.tags_catalogo")
    m = {str(r["tag"]).strip().upper(): r["id"] for r in (rows or [])}
    _TAG_CATALOG_CACHE["map"] = m
    _TAG_CATALOG_CACHE["exp"] = now + 300
    return m


def _find_tag_id(nome):
    """id de UMA tag pelo nome: 1) catálogo no Supabase; 2) busca direcionada na
    API (tag nova, ainda não sincronizada) — e nesse caso já grava no catálogo."""
    alvo = str(nome).strip().upper()
    mp = _tags_catalog_map()
    if alvo in mp:
        return mp[alvo]
    for _pg, n, tid in _catalog_pages(max_pages=8, search=str(nome).strip()):
        if n.upper() == alvo:
            try:
                execute("""insert into cockpit.tags_catalogo (id, tag) values (%s,%s)
                           on conflict (id) do update set tag=excluded.tag, synced_at=now()""",
                        (tid, n))
            except Exception:
                pass
            mp[alvo] = tid
            return tid
    return None


def _catalog_upsert(items):
    """Grava em `cockpit.tags_catalogo` as tags cruas vindas do Tasks SC
    (`[{id, tag}]`) e devolve os NOMES que ainda não estavam no catálogo.

    É como uma tag nova nascida em QUALQUER ticket passa a existir para todos os
    outros (autocomplete do painel de tags), sem esperar o `POST /api/tags/sync`.
    """
    pares, vistos = [], set()
    for t in (items or []):
        tid, nome = t.get("id"), str(t.get("tag") or "").strip()
        if not tid or not nome or tid in vistos:
            continue
        vistos.add(tid)
        pares.append((tid, nome))
    if not pares:
        return []
    mp = _tags_catalog_map()
    novas = [n for _i, n in pares if n.upper() not in mp]
    try:
        with db() as c, c.cursor() as cur:
            psycopg2.extras.execute_values(cur, """
                insert into cockpit.tags_catalogo (id, tag, synced_at) values %s
                on conflict (id) do update set tag=excluded.tag, synced_at=now()
            """, pares, template="(%s,%s,now())", page_size=200)
    except Exception:
        return []
    if novas:
        _TAG_CATALOG_CACHE["map"] = None      # invalida: a próxima leitura relê
    return novas


def _resolve_tag_ids(values, current_tag_items):
    """A tela manda NOMES; o PUT exige IDs. Usa as tags atuais do ticket (nada se
    perde) + o catálogo. Devolve as desconhecidas para avisar, nunca descartar."""
    name2id = {}
    for t in (current_tag_items or []):
        if t.get("tag"):
            name2id[str(t["tag"]).strip().upper()] = t["id"]
    out, seen, desconhecidas = [], set(), []
    for x in values:
        s = str(x).strip()
        if not s:
            continue
        tid = s if re.fullmatch(r"\d{3,}", s) else (name2id.get(s.upper()) or _find_tag_id(s))
        if not tid:
            desconhecidas.append(s)
            continue
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out, desconhecidas


def _observers_fetch(uuid):
    """Observadores AO VIVO do Tasks SC. Devolve (items_crus, codigos).

    A API não documenta o nome da chave do código do observador — o portal
    devolve o registro inteiro. Por isso tentamos, em ordem, os nomes que
    aparecem nas outras rotas do módulo. `raw` volta na resposta da rota
    /observers para conferência quando algo não bater.
    """
    data, code, _ = tasks_request("GET", f"/observers/ticket/{uuid}")
    items = (data.get("items") or []) if (code == 200 and isinstance(data, dict)) else []
    codigos = []
    for it in items:
        if isinstance(it, str):
            cod = it.strip()
        else:
            cod = ""
            for k in ("observer", "user", "user_code", "code", "id", "user_assigned"):
                v = it.get(k)
                if isinstance(v, str) and v.strip():
                    cod = v.strip()
                    break
        if cod and cod not in codigos:
            codigos.append(cod)
    return items, codigos


def _observer_nome(item):
    for k in ("observer_description", "user_name", "name", "description",
              "user_assigned_description"):
        v = (item or {}).get(k) if isinstance(item, dict) else None
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def tasks_update(uuid, changes):
    """GET → merge → PUT (a API não tem PATCH).

    TAGS: aceite APENAS o delta — `tags_add` / `tags_remove` (nomes). O delta é
    aplicado sobre a lista AO VIVO do Tasks SC.

    Por que não aceitar a lista inteira (`tags`)? Porque o PUT substitui todas as
    tags do ticket. Se a tela mandasse a lista vinda do espelho (Supabase) e esse
    espelho estivesse desatualizado, qualquer tag que existisse só no Tasks SC
    seria APAGADA sem ninguém pedir. Foi exatamente o que aconteceu com a
    RESTRICAO TECNICA. Com o delta, o que não foi citado é preservado.
    """
    changes = dict(changes)
    tags_add = [str(x).strip() for x in (changes.pop("tags_add", None) or []) if str(x).strip()]
    tags_rem = [str(x).strip() for x in (changes.pop("tags_remove", None) or []) if str(x).strip()]
    if "tags" in changes:
        raise ValueError(
            "Alteração de tags deve usar tags_add/tags_remove (delta). Enviar a "
            "lista completa é inseguro: apagaria do Tasks SC as tags ausentes do espelho.")
    unknown = set(changes) - ALLOWED_PUT
    if unknown:
        raise ValueError(f"Campos não suportados no PUT: {sorted(unknown)}")
    cur_data, code, err = tasks_request("GET", f"/tickets/{uuid}")
    if code != 200:
        raise RuntimeError(f"GET ticket falhou: {err}")
    items = cur_data.get("items") or []
    if not items:
        raise RuntimeError("Ticket não encontrado.")
    current = items[0]
    tag_data, tcode, terr = tasks_request("GET", f"/tickets/tags/{uuid}")
    tag_data_items = tag_data.get("items") or [] if tcode == 200 else []
    tag_ids = [t["id"] for t in tag_data_items]
    # Observadores: o PUT substitui a lista inteira. Se mandássemos [] fixo (como
    # era antes), toda gravação de status/tag/estimativa apagaria silenciosamente
    # os observadores da Task. Hidrata do Tasks SC e só troca quando pedido.
    try:
        _, obs_atuais = _observers_fetch(uuid)
    except Exception:
        obs_atuais = []
    if not obs_atuais:
        cur_obs = current.get("observer")
        if isinstance(cur_obs, list):
            obs_atuais = [str(o).strip() for o in cur_obs if str(o).strip()]

    if tags_add or tags_rem:
        vivos = [str(t.get("tag") or "").strip() for t in tag_data_items]
        vivos = [v for v in vivos if v]
        rem_up = {r.upper() for r in tags_rem}
        nomes = [v for v in vivos if v.upper() not in rem_up]     # preserva o resto
        for a in tags_add:
            if a.upper() not in [n.upper() for n in nomes]:
                nomes.append(a)
        ids, desconhecidas = _resolve_tag_ids(nomes, tag_data_items)
        if desconhecidas:
            raise ValueError(
                "Tag(s) inexistente(s) no catálogo do Tasks SC: " + ", ".join(desconhecidas)
                + ". Crie a tag no Tasks SC primeiro (aqui só dá para usar tags já cadastradas).")
        changes["tags"] = ids

    payload = {
        "uuid": current["uuid"], "id": current["id"],
        "title": current.get("title", "") or "",
        "description": current.get("description", "") or "",
        "customer": current["customer"],
        "status": current.get("status", "001") or "001",
        "status_description": current.get("status_description", "") or "",
        "service": current.get("service", "") or "",
        "service_description": current.get("service_description", "") or "",
        "user_assigned": current.get("user_assigned", "") or "",
        "assigned_customer": current.get("assigned_customer") or None,
        "observer": list(obs_atuais), "tags": list(tag_ids),
        "start_date": current.get("start_date", "") or "",
        "start_time": current.get("start_time", "") or "",
        "end_date": current.get("end_date", "") or "",
        "end_time": current.get("end_time", "") or "",
        "due_date": current.get("due_date", "") or "",
        "reminder_date": current.get("reminder_date", "") or "",
        "issue_totvs": current.get("issue_totvs", "") or "",
        "ticket_totvs": current.get("ticket_totvs", "") or "",
        "ticket_customer": current.get("ticket_customer", "") or "",
        "time_estimate": current.get("time_estimate", 1) or 1,
        "priority": current.get("priority", 1) or 1,
        "progress": current.get("progress", 5) or 0,
        "milestone": bool(current.get("milestone", False)),
        "project": current.get("project") or None,
        "activity": current.get("activity", "") or "",
        "time_spent": current.get("time_spent", 0) or 0,
        "_id_reference": "", "_keepChecklist": False, "obsArq": "",
    }
    payload.update(changes)
    if payload["assigned_customer"] == "":
        payload["assigned_customer"] = None
    if not isinstance(payload.get("observer"), list):
        payload["observer"] = []
    payload["observer"] = [str(o).strip() for o in payload["observer"] if str(o).strip()]
    if payload["project"] == "":
        payload["project"] = None
    data, code, err = tasks_request("PUT", "/tickets", body=payload)
    if code >= 400:
        raise RuntimeError(f"PUT /tickets falhou HTTP {code}: {err}")
    return data


# ─────────────────────────────────────────────────────────────────────────────
# Páginas (HTML) com porta de login
# ─────────────────────────────────────────────────────────────────────────────
def serve_file(name, ctype="text/html; charset=utf-8"):
    f = PUBLIC_DIR / name
    if not f.exists():
        return _err(404, f"{name} não encontrado no deploy.")
    return Response(f.read_bytes(), mimetype=ctype)


@app.get("/login")
def page_login():
    return serve_file("login.html")


@app.get("/")
def page_root():
    if not current_user():
        return redirect("/login", code=302)
    return serve_file("gaps-decisao.html")


@app.get("/gaps-decisao.html")
def page_decisao():
    if not current_user():
        return redirect("/login", code=302)
    return serve_file("gaps-decisao.html")


@app.get("/gaps-reuniao.html")
def page_reuniao():
    if not current_user():
        return redirect("/login", code=302)
    return serve_file("gaps-reuniao.html")


@app.get("/gaps-import.html")
def page_import():
    if not current_user():
        return redirect("/login", code=302)
    return serve_file("gaps-import.html")


@app.get("/kb")
@app.get("/kb.html")
def page_kb():
    """Base de Conhecimento Protheus por cliente (Módulo → Assunto → Artigo)."""
    if not current_user():
        return redirect("/login", code=302)
    return serve_file("kb.html")


@app.get("/admin")
@app.get("/admin.html")
def page_admin():
    """Tela de acessos. Exige login; o gate de admin é por rota de API — assim
    o não-admin ainda usa o bloco 'Minha conta' da mesma página."""
    if not current_user():
        return redirect("/login", code=302)
    return serve_file("admin.html")


# ─────────────────────────────────────────────────────────────────────────────
# Auth API
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/login")
def api_login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    senha = body.get("senha") or body.get("password") or ""
    if not email or not senha:
        return _err(400, "Informe e-mail e senha.")
    row = q("select email, nome, senha_hash, ativo from cockpit.usuarios_login "
            "where lower(email)=%s", (email,), one=True)
    if not row or not row["ativo"]:
        return _err(401, "Usuário não autorizado.")
    if not verify_scrypt(row["senha_hash"], senha):
        return _err(401, "Credenciais inválidas.")
    try:
        execute("update cockpit.usuarios_login set last_login=now() where email=%s",
                (row["email"],))
    except Exception:
        pass
    resp = make_response(_json({"ok": True, "email": row["email"], "nome": row["nome"]}))
    resp.set_cookie(COOKIE_NAME, make_session(row["email"], row["nome"] or ""),
                    max_age=SESSION_TTL, httponly=True, secure=True, samesite="Lax", path="/")
    return resp


@app.post("/api/logout")
def api_logout():
    resp = make_response(_json({"ok": True}))
    resp.set_cookie(COOKIE_NAME, "", max_age=0, path="/")
    return resp


@app.get("/api/me")
def api_me():
    s = read_session()
    if not s:
        return _err(401, "Não autenticado.")
    real = s["e"]
    adm = is_admin(real)
    alvo = s.get("v") if (s.get("v") and adm) else None
    perfil = perfil_de(real)
    return _json({"ok": True, "email": real, "nome": s.get("n"), "is_admin": adm,
                  "perfil": perfil, "perfil_label": PERFIL_LABEL.get(perfil, perfil),
                  # O front usa isto só para ESCONDER o que o backend já barraria.
                  # A regra de verdade está em require_tasks_write/require_write.
                  "pode_escrever_tasks": perfil not in ("cliente",) + PERFIS_SO_LEITURA,
                  "pode_decidir": perfil not in PERFIS_SO_LEITURA,
                  "somente_leitura": perfil in PERFIS_SO_LEITURA,
                  "view_as": alvo, "efetivo": alvo or real,
                  "perfil_efetivo": perfil_de(alvo) if alvo else perfil})


@app.get("/api/usuarios")
def api_usuarios():
    """Usuários para o seletor 'ver como' — só admin."""
    if (r := require_admin()):
        return r
    rows = q("""select u.email, u.nome, coalesce(u.is_admin,false) as is_admin, u.ativo,
                       (select count(*) from cockpit.usuario_clientes uc
                         where lower(uc.email)=lower(u.email)) as n_clientes
                from cockpit.usuarios_login u order by u.nome""")
    return _json({"ok": True, "usuarios": rows})


@app.post("/api/view-as")
def api_view_as():
    """Liga/desliga a simulação de visão. Só admin. Body: {email} ou {email:null}."""
    if (r := require_admin()):
        return r
    body = request.get_json(silent=True) or {}
    alvo = _norm_email(body.get("email")) or None
    if alvo:
        u = q("select email from cockpit.usuarios_login where lower(email)=%s",
              (alvo,), one=True)
        if not u:
            return _err(404, "Usuário não encontrado.")
        alvo = u["email"]
    s = read_session()
    resp = make_response(_json({"ok": True, "view_as": alvo}))
    resp.set_cookie(COOKIE_NAME, make_session(s["e"], s.get("n"), view_as=alvo),
                    max_age=SESSION_TTL, httponly=True, secure=True,
                    samesite="Lax", path="/")
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Administração de acessos — só admin (tela /admin)
# ─────────────────────────────────────────────────────────────────────────────
def _norm_email(s):
    """Normaliza o e-mail vindo do corpo OU da URL.

    Armadilha real: o front manda `encodeURIComponent(email)` no path
    (`.../usuarios/diego.rcosta%40totvs.com.br/clientes`). O WSGI deveria
    entregar o PATH_INFO já decodificado, mas na função serverless da Vercel o
    `%40` chegava cru — o `lower()` comparava "diego.rcosta%40totvs.com.br"
    com o e-mail do banco e dava "Usuário não encontrado" em TODAS as ações da
    linha do usuário (clientes, senha, perfil, ativo). O unquote aqui é o ponto
    único de defesa e é inofensivo para e-mail já decodificado.
    """
    s = (s or "").strip()
    if "%" in s:
        s = unquote(s)
    return s.strip().lower()


def _usuario(email):
    return q("""select email, nome, perfil, coalesce(is_admin,false) as is_admin, ativo
                from cockpit.usuarios_login where lower(email)=%s""",
             (_norm_email(email),), one=True)


@app.get("/api/admin/clientes")
def api_admin_clientes():
    """Catálogo COMPLETO de clientes — sem o filtro de acesso, porque é a lista
    de onde o admin escolhe o que liberar."""
    if (r := require_admin()):
        return r
    rows = q("""select c.customer, c.nome, count(t.uuid_ticket) as n_tickets
                from cockpit.clientes c
                left join cockpit.tickets t on t.customer = c.customer
                group by c.customer, c.nome order by c.nome""")
    return _json({"ok": True, "clientes": rows})


@app.get("/api/admin/usuarios")
def api_admin_usuarios():
    """Lista completa para a tela de acessos, já com os clientes liberados."""
    if (r := require_admin()):
        return r
    users = q("""select email, nome, perfil, coalesce(is_admin,false) as is_admin,
                        ativo, last_login, created_at
                 from cockpit.usuarios_login
                 order by (perfil='admin') desc, nome nulls last, email""")
    libs = q("select email, customer from cockpit.usuario_clientes")
    por_email = {}
    for l in libs:
        por_email.setdefault(_norm_email(l["email"]), []).append(l["customer"])
    for u in users:
        u["clientes"] = sorted(por_email.get(_norm_email(u["email"]), []))
        u["perfil_label"] = PERFIL_LABEL.get(u["perfil"], u["perfil"])
    return _json({"ok": True, "usuarios": users, "eu": current_user()})


@app.post("/api/admin/usuarios")
def api_admin_usuario_salvar():
    """Cria ou edita um usuário. Body: {email, nome, perfil, senha?, ativo?}.
    A senha só é exigida na criação; se vier na edição, redefine."""
    if (r := require_admin()):
        return r
    if (sim := deny_simulacao()):
        return sim
    body = request.get_json(silent=True) or {}
    email = _norm_email(body.get("email"))
    if not email or "@" not in email:
        return _err(400, "Informe um e-mail válido.")
    nome = (body.get("nome") or "").strip() or email.split("@")[0]
    perfil = (body.get("perfil") or "comum").strip().lower()
    if perfil not in PERFIS:
        return _err(400, f"Perfil inválido. Use: {', '.join(PERFIS)}.")
    senha = body.get("senha") or ""
    existente = _usuario(email)

    if not existente and not senha:
        return _err(400, "Defina uma senha inicial para o novo usuário.")
    if senha and (msg := valida_senha(senha)):
        return _err(400, msg)

    # Auto-proteção: o admin não se rebaixa nem se inativa pela própria tela.
    ativo = body.get("ativo")
    ativo = True if ativo is None else bool(ativo)
    if existente and email == _norm_email(current_user()):
        if perfil != "admin":
            return _err(400, "Você não pode tirar o seu próprio acesso de administrador.")
        if not ativo:
            return _err(400, "Você não pode inativar o seu próprio usuário.")

    if existente:
        if senha:
            execute("""update cockpit.usuarios_login
                          set nome=%s, perfil=%s, ativo=%s, senha_hash=%s
                        where lower(email)=%s""",
                    (nome, perfil, ativo, hash_scrypt(senha), email))
        else:
            execute("""update cockpit.usuarios_login
                          set nome=%s, perfil=%s, ativo=%s where lower(email)=%s""",
                    (nome, perfil, ativo, email))
        acao = "atualizado"
    else:
        execute("""insert into cockpit.usuarios_login
                     (email, nome, perfil, ativo, senha_hash, created_by)
                   values (%s,%s,%s,%s,%s,%s)""",
                (email, nome, perfil, ativo, hash_scrypt(senha), current_user()))
        acao = "criado"
    return _json({"ok": True, "acao": acao, "usuario": _usuario(email)})


@app.post("/api/admin/usuarios/<path:email>/ativo")
def api_admin_usuario_ativo(email):
    """Ativa/inativa. Body: {ativo:true|false}."""
    if (r := require_admin()):
        return r
    if (sim := deny_simulacao()):
        return sim
    email = _norm_email(email)
    if not _usuario(email):
        return _err(404, f"Usuário não encontrado: {email}")
    ativo = bool((request.get_json(silent=True) or {}).get("ativo"))
    if email == _norm_email(current_user()) and not ativo:
        return _err(400, "Você não pode inativar o seu próprio usuário.")
    execute("update cockpit.usuarios_login set ativo=%s where lower(email)=%s",
            (ativo, email))
    return _json({"ok": True, "usuario": _usuario(email)})


@app.post("/api/admin/usuarios/<path:email>/perfil")
def api_admin_usuario_perfil(email):
    """Troca o perfil. Body: {perfil:'admin'|'comum'|'cliente'}. O trigger do
    banco mantém o is_admin coerente (o cockpit Next.js ainda lê essa coluna)."""
    if (r := require_admin()):
        return r
    if (sim := deny_simulacao()):
        return sim
    email = _norm_email(email)
    if not _usuario(email):
        return _err(404, f"Usuário não encontrado: {email}")
    perfil = (request.get_json(silent=True) or {}).get("perfil", "")
    perfil = str(perfil).strip().lower()
    if perfil not in PERFIS:
        return _err(400, f"Perfil inválido. Use: {', '.join(PERFIS)}.")
    if email == _norm_email(current_user()) and perfil != "admin":
        return _err(400, "Você não pode tirar o seu próprio acesso de administrador.")
    execute("update cockpit.usuarios_login set perfil=%s where lower(email)=%s",
            (perfil, email))
    return _json({"ok": True, "usuario": _usuario(email)})


@app.post("/api/admin/usuarios/<path:email>/senha")
def api_admin_usuario_senha(email):
    """Admin redefine a senha de alguém. Body: {senha}."""
    if (r := require_admin()):
        return r
    if (sim := deny_simulacao()):
        return sim
    email = _norm_email(email)
    if not _usuario(email):
        return _err(404, f"Usuário não encontrado: {email}")
    senha = (request.get_json(silent=True) or {}).get("senha") or ""
    if (msg := valida_senha(senha)):
        return _err(400, msg)
    execute("update cockpit.usuarios_login set senha_hash=%s where lower(email)=%s",
            (hash_scrypt(senha), email))
    return _json({"ok": True})


@app.post("/api/admin/usuarios/<path:email>/clientes")
def api_admin_usuario_clientes(email):
    """Substitui a lista de clientes liberados. Body: {customers:[...]}.
    Admin ignora a lista (vê tudo), mas guardamos assim mesmo — se ele virar
    'comum' amanhã, a liberação já está lá."""
    if (r := require_admin()):
        return r
    if (sim := deny_simulacao()):
        return sim
    email = _norm_email(email)
    u = _usuario(email)
    if not u:
        return _err(404, f"Usuário não encontrado: {email}")
    body = request.get_json(silent=True) or {}
    pedidos = body.get("customers")
    if not isinstance(pedidos, list):
        return _err(400, "Envie 'customers' como lista.")
    validos = {r["customer"] for r in q("select customer from cockpit.clientes")}
    escolhidos = sorted({str(c).strip() for c in pedidos if str(c).strip() in validos})
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("delete from cockpit.usuario_clientes where lower(email)=%s",
                        (email,))
            if escolhidos:
                psycopg2.extras.execute_values(
                    cur, """insert into cockpit.usuario_clientes
                              (email, customer, created_by) values %s
                            on conflict (email, customer) do nothing""",
                    [(u["email"], c, current_user()) for c in escolhidos])
    return _json({"ok": True, "email": u["email"], "clientes": escolhidos,
                  "total": len(escolhidos)})


@app.post("/api/conta/senha")
def api_conta_senha():
    """O próprio usuário troca a senha. Body: {atual, nova}."""
    if (r := require_auth()):
        return r
    if (sim := deny_simulacao()):
        return sim
    body = request.get_json(silent=True) or {}
    atual, nova = body.get("atual") or "", body.get("nova") or ""
    email = current_user()
    row = q("select email, senha_hash from cockpit.usuarios_login where lower(email)=%s",
            (_norm_email(email),), one=True)
    if not row:
        return _err(404, "Usuário não encontrado.")
    if not verify_scrypt(row["senha_hash"], atual):
        return _err(401, "A senha atual não confere.")
    if (msg := valida_senha(nova)):
        return _err(400, msg)
    if verify_scrypt(row["senha_hash"], nova):
        return _err(400, "A nova senha é igual à atual.")
    execute("update cockpit.usuarios_login set senha_hash=%s where email=%s",
            (hash_scrypt(nova), row["email"]))
    return _json({"ok": True})


@app.errorhandler(Exception)
def _on_error(e):
    if isinstance(e, HTTPException):
        return e
    return _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


@app.get("/api/health")
def api_health():
    """Diagnóstico público: confere env vars e testa a conexão ao banco."""
    info = {
        "ok": True,
        "env": {
            "DATABASE_URL": bool(DATABASE_URL),
            "TASKS_USERNAME": bool(TASKS_USER),
            "TASKS_PASSWORD": bool(TASKS_PASS),
            "TASKS_SC_BASE_URL": TASKS_BASE,
            "SESSION_SECRET": SESSION_SECRET != "dev-insecure-secret-change-me",
        },
        "db": False,
    }
    # pista do host do banco, sem expor senha
    try:
        host = re.search(r"@([^/:?]+)", DATABASE_URL)
        info["db_host"] = host.group(1) if host else None
        info["db_port"] = (re.search(r":(\d+)/", DATABASE_URL) or [None, None])[1]
    except Exception:
        pass
    try:
        row = q("select 1 as ok", one=True)
        info["db"] = bool(row and row.get("ok") == 1)
    except Exception as e:
        info["ok"] = False
        info["db_error"] = f"{type(e).__name__}: {e}"
    return _json(info, 200)


# ─────────────────────────────────────────────────────────────────────────────
# Dados — Supabase
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/clientes")
def api_clientes():
    if (r := require_auth()):
        return r
    rows = q("""
        select c.customer, c.nome, c.tipo, c.saude,
               count(t.uuid_ticket) as n_tickets
        from cockpit.clientes c
        left join cockpit.tickets t on t.customer = c.customer
        group by c.customer, c.nome, c.tipo, c.saude
        order by c.nome
    """)
    allowed = allowed_customers()
    clientes = []
    for r in rows:
        if allowed is not None and r["customer"] not in allowed:
            continue  # cliente não liberado some da lista
        clientes.append({
            "customer": r["customer"], "nome": r["nome"], "tipo": r["tipo"],
            "saude": r["saude"], "n_tickets": r["n_tickets"],
            "chave": _slug_first(r["nome"]),
        })
    return _json({"ok": True, "clientes": clientes})


def _resolve_customer(chave):
    """Aceita código customer, slug do 1º token do nome, ou slug do nome completo."""
    chave = (chave or "").strip()
    rows = q("select customer, nome from cockpit.clientes")
    lc = chave.lower()
    for r in rows:
        if r["customer"].lower() == lc:
            return r["customer"], r["nome"]
    for r in rows:
        if _slug_first(r["nome"]) == _slug(chave):
            return r["customer"], r["nome"]
    for r in rows:
        if _slug(r["nome"]) == _slug(chave):
            return r["customer"], r["nome"]
    return None, None


def _derive_from_tags(tags):
    up = [(t or "").upper() for t in tags]
    etapa = next((e for e in ETAPA_TAGS if e in up), None)
    classe = next((c for c in CLASS_TAGS if c in up), None)
    return etapa, classe


@app.get("/api/tickets")
def api_tickets():
    if (r := require_auth()):
        return r
    chave = request.args.get("cliente", "digitro")
    customer, nome = _resolve_customer(chave)
    if not customer:
        return _err(404, f"Cliente '{chave}' não encontrado.")
    if (d := deny_customer(customer)):
        return d
    rows = q("""
        select t.*,
               (select array_agg(tt.raw_tag order by tt.raw_tag)
                  from cockpit.ticket_tags tt where tt.uuid_ticket = t.uuid_ticket) as tags,
               (select max(synced_at) from cockpit.tickets where customer=%s) as _sync
        from cockpit.tickets t
        where t.customer = %s
        order by (t.raw->>'id')
    """, (customer, customer))
    tickets = []
    atualizado = None
    for r in rows:
        atualizado = atualizado or r.get("_sync")
        raw = r.get("raw") or {}
        tags = list(r.get("tags") or [])
        det, dcl = _derive_from_tags(tags)
        obj = dict(raw)  # começa do ticket cru do Tasks SC (id, uuid, title, ...)
        obj.update({
            "uuid": raw.get("uuid") or r["uuid_ticket"],
            "uuid_ticket": r["uuid_ticket"],
            "id": raw.get("id"),
            "title": raw.get("title") or r.get("titulo"),
            "titulo": r.get("titulo"),
            "descricao": r.get("descricao"),
            "customer": customer,
            "cliente": r.get("cliente") or nome,
            "tags": tags,
            "etapa_gap": r.get("etapa_gap") or det,
            "classificacao_gap": r.get("classificacao_gap") or dcl,
            "classificacao": r.get("classificacao_gap") or dcl,
            "etapa": r.get("etapa_gap") or det,
            "tipo_atividade": r.get("tipo_atividade"),
            "produto": r.get("produto"),
            "competencia": r.get("competencia"),
            "projeto": r.get("projeto"),
            "apoio": r.get("apoio"),
            "status_tasks": r.get("status_tasks"),
            "status_temporario": r.get("status_temporario"),
            "prioridade": r.get("prioridade"),
            "time_estimate": r.get("time_estimate"),
            "estimativa": r.get("time_estimate"),
            "due_date": str(r["due_date"]) if r.get("due_date") else "",
            "user_assigned": r.get("user_assigned"),
            "assigned_customer": r.get("assigned_customer"),
            "aging_dias": r.get("aging_dias"),
            "atrasado": r.get("atrasado"),
            "bloqueado": r.get("bloqueado"),
        })
        tickets.append(obj)
    mtime = None
    if atualizado:
        try:
            mtime = atualizado.astimezone().strftime("%d/%m/%Y %H:%M")
        except Exception:
            mtime = str(atualizado)
    return _json({"ok": True, "cliente": _slug_first(nome), "customer": customer,
                  "nome": nome, "atualizado_em": mtime, "tickets": tickets})


# ─────────────────────────────────────────────────────────────────────────────
# Decisões — Supabase (paridade: substitui o decisoes_<cliente>.json local)
# ─────────────────────────────────────────────────────────────────────────────
_DEC_DB2UI = {"aprovar": "approve", "segunda_fase": "phase2",
              "contorno": "workaround", "entendimento_projeto": "entend",
              "recusar": "refuse", "pendente": None}
_DEC_UI2DB = {"approve": "aprovar", "phase2": "segunda_fase",
              "workaround": "contorno", "entend": "entendimento_projeto",
              "refuse": "recusar",
              None: "pendente", "": "pendente"}


@app.get("/api/decisoes")
def api_decisoes_get():
    if (r := require_auth()):
        return r
    chave = request.args.get("cliente", "digitro")
    customer, nome = _resolve_customer(chave)
    if not customer:
        return _err(404, f"Cliente '{chave}' não encontrado.")
    if (d := deny_customer(customer)):
        return d
    rows = q("""
        select d.uuid_ticket, d.decisao, d.estimativa, d.observacao, d.classe,
               d.decided_by, d.updated_at, t.raw->>'id' as ticket_id
        from cockpit.decisoes d
        join cockpit.tickets t on t.uuid_ticket = d.uuid_ticket
        where t.customer = %s
    """, (customer,))
    decisoes = {}
    atualizado = None
    for r in rows:
        atualizado = max(atualizado, r["updated_at"]) if atualizado else r["updated_at"]
        entry = {
            "decisao": _DEC_DB2UI.get(r["decisao"], None),
            "nota": r["observacao"],
            "estimativa": float(r["estimativa"]) if r["estimativa"] is not None else None,
            "classe": r["classe"],
            "por": r["decided_by"],
            "ts": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        decisoes[r["uuid_ticket"]] = entry
        if r["ticket_id"]:
            decisoes[r["ticket_id"]] = entry  # aceita chave por id também
    mtime = atualizado.astimezone().strftime("%d/%m/%Y %H:%M") if atualizado else None
    return _json({"ok": True, "cliente": _slug_first(nome), "customer": customer,
                  "decisoes": decisoes, "atualizado_em": mtime})


def _resolve_uuid(key, customer):
    """Chave da decisão pode ser uuid_ticket ou o id (00011816)."""
    if re.match(r"^[0-9A-Fa-f-]{20,}$", key):
        return key.upper()
    row = q("select uuid_ticket from cockpit.tickets where customer=%s and raw->>'id'=%s",
            (customer, key), one=True)
    return row["uuid_ticket"] if row else None


@app.post("/api/decisoes")
def api_decisoes_post():
    if (r := require_auth()):
        return r
    if (w := require_write()):
        return w
    if (sim := deny_simulacao()):
        return sim
    chave = request.args.get("cliente", "digitro")
    customer, nome = _resolve_customer(chave)
    if not customer:
        return _err(404, f"Cliente '{chave}' não encontrado.")
    if (d := deny_customer(customer)):
        return d
    body = request.get_json(silent=True) or {}
    decisoes = body.get("decisoes")
    if not isinstance(decisoes, dict):
        return _err(400, "Campo 'decisoes' (objeto) é obrigatório.")
    user = current_user()
    total = 0
    for key, val in decisoes.items():
        uuid = _resolve_uuid(key, customer)
        if not uuid:
            continue
        val = val or {}
        dec_ui = val.get("decisao")
        dec_db = _DEC_UI2DB.get(dec_ui, "pendente")
        est = val.get("estimativa")
        est = float(est) if est not in (None, "") else None
        nota = val.get("nota") or val.get("observacao")
        classe = val.get("classe")
        execute("""
            insert into cockpit.decisoes
              (uuid_ticket, decisao, estimativa, observacao, classe, decided_by, decided_at, updated_at)
            values (%s, %s, %s, %s, %s, %s, now(), now())
            on conflict (uuid_ticket) do update set
              decisao=excluded.decisao, estimativa=excluded.estimativa,
              observacao=excluded.observacao, classe=excluded.classe,
              decided_by=excluded.decided_by, decided_at=now(), updated_at=now()
        """, (uuid, dec_db, est, nota, classe, user))
        total += 1
    return _json({"ok": True, "cliente": _slug_first(nome), "total": total})


# ─────────────────────────────────────────────────────────────────────────────
# Alinhamento do GAP — cockpit.gap_alinhamentos
# A narrativa de cada GAP em quatro campos, na ordem em que a conversa acontece:
#   questionamento_cliente → argumentacao_interna → alinhamento_reuniao → retorno_cliente
# Vive SÓ no Supabase: nada aqui sai para o Tasks SC (por isso require_write,
# e não require_tasks_write). A argumentação interna nunca chega ao perfil
# 'cliente' — nem no 'ver como'. Migration: sql/0010_gap_alinhamentos.sql.
# ─────────────────────────────────────────────────────────────────────────────
ALIN_CAMPOS = ("questionamento_cliente", "argumentacao_interna",
               "alinhamento_reuniao", "retorno_cliente")
ALIN_INTERNO = "argumentacao_interna"


def alin_ve_interno():
    """A argumentação é nossa. O perfil 'cliente' não a recebe — e como olhamos
    o effective_user, o admin em 'ver como cliente' também não, que é justamente
    como se confere o que o cliente enxerga."""
    return perfil_de(effective_user()) != "cliente"


def _alin_payload(row):
    """Row → JSON do alinhamento, já podado do que o perfil não pode ver."""
    interno_ok = alin_ve_interno()
    out = {"existe": bool(row), "ve_interno": interno_ok}
    for c in ALIN_CAMPOS:
        out[c] = (row.get(c) if row else None)
    if not interno_ok:
        out[ALIN_INTERNO] = None
    out["updated_by"] = row.get("updated_by") if row else None
    out["updated_at"] = row["updated_at"].isoformat() if (row and row.get("updated_at")) else None
    return out


@app.get("/api/alinhamento/<uuid>")
def api_alinhamento_get(uuid):
    if (r := require_auth()):
        return r
    if (g := deny_uuid(uuid)):
        return g
    row = q("""select uuid_ticket, task_id, customer,
                      questionamento_cliente, argumentacao_interna,
                      alinhamento_reuniao, retorno_cliente,
                      created_by, created_at, updated_by, updated_at
               from cockpit.gap_alinhamentos where uuid_ticket=%s""",
            (uuid.upper(),), one=True)
    return _json({"ok": True, "uuid": uuid.upper(), **_alin_payload(row)})


@app.post("/api/alinhamento/<uuid>")
def api_alinhamento_post(uuid):
    """Gravação PARCIAL: só as chaves presentes no body são tocadas. Dois
    consultores editando campos diferentes do mesmo GAP não se atropelam."""
    if (r := require_auth()):
        return r
    if (w := require_write()):
        return w
    if (sim := deny_simulacao()):
        return sim
    if (g := deny_uuid(uuid)):
        return g
    body = request.get_json(silent=True) or {}
    campos = {c: (body[c] or None) for c in ALIN_CAMPOS if c in body}
    if not alin_ve_interno():
        campos.pop(ALIN_INTERNO, None)   # quem não lê o interno também não escreve
    if not campos:
        return _err(400, "Nenhum campo de alinhamento informado.")
    tk = q("select customer, raw->>'id' as task_id from cockpit.tickets "
           "where uuid_ticket=%s", (uuid.upper(),), one=True)
    if not tk:
        return _err(404, "Ticket não encontrado no espelho do cockpit.")
    user = current_user()
    cols = list(campos.keys())
    # INSERT com os campos enviados; no conflito, atualiza só esses mesmos.
    sql = f"""
        insert into cockpit.gap_alinhamentos
          (uuid_ticket, task_id, customer, {", ".join(cols)}, created_by, updated_by)
        values (%s, %s, %s, {", ".join(["%s"] * len(cols))}, %s, %s)
        on conflict (uuid_ticket) do update set
          task_id = coalesce(cockpit.gap_alinhamentos.task_id, excluded.task_id),
          customer = coalesce(cockpit.gap_alinhamentos.customer, excluded.customer),
          {", ".join(f"{c} = excluded.{c}" for c in cols)},
          updated_by = excluded.updated_by
    """
    execute(sql, (uuid.upper(), tk["task_id"], tk["customer"],
                  *[campos[c] for c in cols], user, user))
    row = q("""select questionamento_cliente, argumentacao_interna,
                      alinhamento_reuniao, retorno_cliente, updated_by, updated_at
               from cockpit.gap_alinhamentos where uuid_ticket=%s""",
            (uuid.upper(),), one=True)
    return _json({"ok": True, "uuid": uuid.upper(),
                  "gravados": cols, **_alin_payload(row)})


@app.get("/api/alinhamentos")
def api_alinhamentos_lista():
    """Mapa enxuto por cliente, para o board marcar quem já tem alinhamento.
    Não devolve texto — só quais campos estão preenchidos."""
    if (r := require_auth()):
        return r
    chave = request.args.get("cliente", "digitro")
    customer, nome = _resolve_customer(chave)
    if not customer:
        return _err(404, f"Cliente '{chave}' não encontrado.")
    if (d := deny_customer(customer)):
        return d
    interno_ok = alin_ve_interno()
    rows = q("""select a.uuid_ticket, a.task_id, a.updated_by, a.updated_at,
                       (a.questionamento_cliente is not null and a.questionamento_cliente <> '') as tem_quest,
                       (a.argumentacao_interna   is not null and a.argumentacao_interna   <> '') as tem_arg,
                       (a.alinhamento_reuniao    is not null and a.alinhamento_reuniao    <> '') as tem_reuniao,
                       (a.retorno_cliente        is not null and a.retorno_cliente        <> '') as tem_retorno
                from cockpit.gap_alinhamentos a
                join cockpit.tickets t on t.uuid_ticket = a.uuid_ticket
                where t.customer = %s""", (customer,))
    mapa = {}
    for r_ in rows:
        campos = {"questionamento_cliente": r_["tem_quest"],
                  "alinhamento_reuniao": r_["tem_reuniao"],
                  "retorno_cliente": r_["tem_retorno"]}
        if interno_ok:
            campos["argumentacao_interna"] = r_["tem_arg"]
        entry = {
            "preenchidos": sum(1 for v in campos.values() if v),
            "campos": campos,
            "por": r_["updated_by"],
            "ts": r_["updated_at"].isoformat() if r_["updated_at"] else None,
        }
        if not any(campos.values()):
            continue
        mapa[r_["uuid_ticket"]] = entry
        if r_["task_id"]:
            mapa[r_["task_id"]] = entry
    return _json({"ok": True, "cliente": _slug_first(nome), "customer": customer,
                  "alinhamentos": mapa})


# ─────────────────────────────────────────────────────────────────────────────
# Base de Conhecimento Protheus — cockpit.kb_artigos (Módulo → Assunto → Artigo)
# Consultor (admin/comum) escreve; perfis 'cliente' e 'leitor' só leem os
# artigos publicados. Tudo recortado por customer (allowed_customers).
# ─────────────────────────────────────────────────────────────────────────────
KB_PERFIS_EDITAM = ("admin", "comum")


def _kb_pode_editar():
    """Editor de verdade: perfil admin/comum E fora do modo 'ver como'."""
    return (not simulando()) and perfil_de(current_user()) in KB_PERFIS_EDITAM


def require_kb_write():
    """Escrita na Base de Conhecimento: só consultor (admin/comum)."""
    if (r := require_auth()):
        return r
    if perfil_de(current_user()) not in KB_PERFIS_EDITAM:
        return _err(403, "Seu perfil é somente de leitura na Base de Conhecimento.")
    return None


@app.get("/api/kb/artigos")
def api_kb_list():
    if (r := require_auth()):
        return r
    chave = request.args.get("cliente", "digitro")
    customer, nome = _resolve_customer(chave)
    if not customer:
        return _err(404, f"Cliente '{chave}' não encontrado.")
    if (d := deny_customer(customer)):
        return d
    editor = _kb_pode_editar()
    # Leitores (cliente/leitor — e o 'ver como') só enxergam o publicado.
    filtro = "" if editor else " and publicado"
    rows = q(f"""
        select id::text as id, modulo, assunto, titulo, corpo_html, ordem,
               publicado, updated_by, updated_at
        from cockpit.kb_artigos
        where customer = %s{filtro}
        order by modulo, assunto, ordem, titulo
    """, (customer,))
    artigos = []
    for r in rows:
        artigos.append({
            "id": r["id"], "modulo": r["modulo"], "assunto": r["assunto"],
            "titulo": r["titulo"], "corpo_html": r["corpo_html"] or "",
            "ordem": r["ordem"], "publicado": r["publicado"],
            "atualizado_por": r["updated_by"],
            "atualizado_em": (r["updated_at"].astimezone().strftime("%d/%m/%Y %H:%M")
                              if r["updated_at"] else None),
        })
    return _json({"ok": True, "cliente": _slug_first(nome), "customer": customer,
                  "nome_cliente": nome, "pode_editar": editor, "artigos": artigos})


@app.post("/api/kb/artigos")
def api_kb_save():
    """Cria ou edita um artigo. Body: {id?, modulo, assunto, titulo,
    corpo_html, ordem?, publicado?}. Sem id → insere; com id → atualiza."""
    if (w := require_kb_write()):
        return w
    if (sim := deny_simulacao()):
        return sim
    chave = request.args.get("cliente", "digitro")
    customer, nome = _resolve_customer(chave)
    if not customer:
        return _err(404, f"Cliente '{chave}' não encontrado.")
    if (d := deny_customer(customer)):
        return d
    body = request.get_json(silent=True) or {}
    modulo = (body.get("modulo") or "").strip().upper()
    assunto = (body.get("assunto") or "").strip()
    titulo = (body.get("titulo") or "").strip()
    if not modulo or not assunto or not titulo:
        return _err(400, "Informe módulo, assunto e título.")
    corpo = body.get("corpo_html") or ""
    try:
        ordem = int(body.get("ordem") or 0)
    except (TypeError, ValueError):
        ordem = 0
    publicado = bool(body.get("publicado", True))
    user = current_user()
    art_id = (body.get("id") or "").strip()
    if art_id:
        row = q("""
            update cockpit.kb_artigos
               set modulo=%s, assunto=%s, titulo=%s, corpo_html=%s, ordem=%s,
                   publicado=%s, updated_by=%s, updated_at=now()
             where id=%s::uuid and customer=%s
            returning id::text as id
        """, (modulo, assunto, titulo, corpo, ordem, publicado, user,
              art_id, customer), one=True)
        if not row:
            return _err(404, "Artigo não encontrado para este cliente.")
    else:
        row = q("""
            insert into cockpit.kb_artigos
              (customer, modulo, assunto, titulo, corpo_html, ordem, publicado,
               created_by, updated_by)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning id::text as id
        """, (customer, modulo, assunto, titulo, corpo, ordem, publicado,
              user, user), one=True)
    return _json({"ok": True, "id": row["id"]})


# ── Link público (somente leitura) por cliente ──────────────────────────────
def _kb_link_row(customer):
    return q("select customer, token, ativo from cockpit.kb_links where customer=%s",
             (customer,), one=True)


@app.get("/api/kb/link")
def api_kb_link():
    """Retorna (criando se preciso) o link público da base do cliente.
    Só editor (admin/comum) enxerga o token."""
    if (w := require_kb_write()):
        return w
    chave = request.args.get("cliente", "digitro")
    customer, nome = _resolve_customer(chave)
    if not customer:
        return _err(404, f"Cliente '{chave}' não encontrado.")
    if (d := deny_customer(customer)):
        return d
    row = _kb_link_row(customer)
    if not row:
        row = q("""insert into cockpit.kb_links (customer, created_by)
                   values (%s, %s)
                   on conflict (customer) do update set updated_at=now()
                   returning customer, token, ativo""",
                (customer, current_user()), one=True)
    return _json({"ok": True, "customer": customer, "cliente": _slug_first(nome),
                  "token": row["token"], "ativo": row["ativo"],
                  "path": f"/kb/publico/{row['token']}"})


@app.post("/api/kb/link")
def api_kb_link_acao():
    """Ações sobre o link público. Body: {acao: 'renovar'|'ativar'|'desativar'}.
    'renovar' gera token novo (o antigo para de funcionar na hora)."""
    if (w := require_kb_write()):
        return w
    if (sim := deny_simulacao()):
        return sim
    chave = request.args.get("cliente", "digitro")
    customer, nome = _resolve_customer(chave)
    if not customer:
        return _err(404, f"Cliente '{chave}' não encontrado.")
    if (d := deny_customer(customer)):
        return d
    acao = ((request.get_json(silent=True) or {}).get("acao") or "").strip().lower()
    if acao not in ("renovar", "ativar", "desativar"):
        return _err(400, "Ação inválida. Use renovar, ativar ou desativar.")
    if not _kb_link_row(customer):
        q("""insert into cockpit.kb_links (customer, created_by) values (%s, %s)
             on conflict (customer) do nothing returning customer""",
          (customer, current_user()), one=True)
    if acao == "renovar":
        row = q("""update cockpit.kb_links
                     set token = replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''),
                         ativo = true, updated_at = now()
                   where customer=%s
                   returning token, ativo""", (customer,), one=True)
    else:
        row = q("""update cockpit.kb_links
                     set ativo = %s, updated_at = now()
                   where customer=%s
                   returning token, ativo""", (acao == "ativar", customer), one=True)
    return _json({"ok": True, "customer": customer, "token": row["token"],
                  "ativo": row["ativo"], "path": f"/kb/publico/{row['token']}"})


def _kb_customer_por_token(token):
    row = q("""select l.customer, c.nome
               from cockpit.kb_links l
               join cockpit.clientes c on c.customer = l.customer
               where l.token=%s and l.ativo""", (token,), one=True)
    return (row["customer"], row["nome"]) if row else (None, None)


@app.get("/kb/publico/<token>")
def page_kb_publico(token):
    """Base de conhecimento pública (somente leitura) — sem login."""
    customer, _ = _kb_customer_por_token(token)
    if not customer:
        return _err(404, "Link inválido ou desativado.")
    return serve_file("kb.html")


@app.get("/api/kb/publico/<token>/artigos")
def api_kb_publico(token):
    """Artigos publicados do cliente dono do token — sem login."""
    customer, nome = _kb_customer_por_token(token)
    if not customer:
        return _err(404, "Link inválido ou desativado.")
    rows = q("""
        select id::text as id, modulo, assunto, titulo, corpo_html, ordem,
               publicado, updated_at
        from cockpit.kb_artigos
        where customer = %s and publicado
        order by modulo, assunto, ordem, titulo
    """, (customer,))
    artigos = [{
        "id": r["id"], "modulo": r["modulo"], "assunto": r["assunto"],
        "titulo": r["titulo"], "corpo_html": r["corpo_html"] or "",
        "ordem": r["ordem"], "publicado": True, "atualizado_por": None,
        "atualizado_em": (r["updated_at"].astimezone().strftime("%d/%m/%Y %H:%M")
                          if r["updated_at"] else None),
    } for r in rows]
    return _json({"ok": True, "cliente": _slug_first(nome), "customer": customer,
                  "nome_cliente": nome, "pode_editar": False, "publico": True,
                  "artigos": artigos})


@app.post("/api/kb/artigos/<art_id>/excluir")
def api_kb_delete(art_id):
    if (w := require_kb_write()):
        return w
    if (sim := deny_simulacao()):
        return sim
    chave = request.args.get("cliente", "digitro")
    customer, _ = _resolve_customer(chave)
    if not customer:
        return _err(404, f"Cliente '{chave}' não encontrado.")
    if (d := deny_customer(customer)):
        return d
    row = q("""delete from cockpit.kb_artigos
               where id=%s::uuid and customer=%s
               returning id::text as id""", (art_id, customer), one=True)
    if not row:
        return _err(404, "Artigo não encontrado para este cliente.")
    return _json({"ok": True, "id": row["id"]})


# ─────────────────────────────────────────────────────────────────────────────
# Importação de decisões (JSON) + auto-tag + comparação de estimativa
# ─────────────────────────────────────────────────────────────────────────────
def _decisao_tags_map():
    row = q("select value from cockpit.integration_config where key='decisao_tags'", one=True)
    mp = row["value"] if row else {}
    if isinstance(mp, str):
        mp = json.loads(mp)
    return mp or {}


@app.get("/api/decisao-config")
def api_decisao_config():
    if (r := require_auth()):
        return r
    return _json({"ok": True, "map": _decisao_tags_map()})


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@app.post("/api/decisoes/importar")
def api_decisoes_importar():
    """Recebe o JSON exportado (schema olim-gaps-decisions), casa cada código com
    a Task (padding p/ 8 dígitos), grava em cockpit.decisoes e devolve a
    conciliação com a comparação de horas (Task × JSON). NÃO aplica tags nem
    altera estimativa — isso é feito depois, sob confirmação, item a item."""
    if (r := require_auth()):
        return r
    if (w := require_write()):
        return w
    if (sim := deny_simulacao()):
        return sim
    chave = request.args.get("cliente", "")
    customer, nome = _resolve_customer(chave)
    if not customer:
        return _err(404, f"Cliente '{chave}' não encontrado.")
    if (d := deny_customer(customer)):
        return d
    body = request.get_json(silent=True) or {}
    decisions = body.get("decisions") or {}
    if not isinstance(decisions, dict) or not decisions:
        return _err(400, "JSON sem o campo 'decisions'.")
    rows = q("select uuid_ticket, raw->>'id' as tid, time_estimate, titulo "
             "from cockpit.tickets where customer=%s", (customer,))
    byid = {(r["tid"] or ""): r for r in rows}
    user = current_user()
    itens, nao_enc = [], []
    for code, info in decisions.items():
        info = info or {}
        tid = str(code).zfill(8)
        row = byid.get(tid) or byid.get(str(code))
        if not row:
            nao_enc.append({"code": code, "title": info.get("title"),
                            "decisao": info.get("decision")})
            continue
        uuid = row["uuid_ticket"]
        dec_ui = info.get("decision")
        dec_db = _DEC_UI2DB.get(dec_ui, "pendente")
        json_h = _num(info.get("estimativa_horas"))
        nota = info.get("note")
        classe = info.get("classification")
        execute("""
            insert into cockpit.decisoes
              (uuid_ticket, decisao, estimativa, observacao, classe, decided_by, decided_at, updated_at)
            values (%s,%s,%s,%s,%s,%s,now(),now())
            on conflict (uuid_ticket) do update set
              decisao=excluded.decisao, estimativa=excluded.estimativa,
              observacao=excluded.observacao, classe=excluded.classe,
              decided_by=excluded.decided_by, updated_at=now()
        """, (uuid, dec_db, json_h, nota, classe, user))
        task_h = _num(row["time_estimate"])
        diff = None if (json_h is None or task_h is None) else round(json_h - task_h, 2)
        itens.append({"code": code, "id": tid, "uuid": uuid,
                      "title": row["titulo"] or info.get("title"),
                      "decisao": dec_ui, "json_horas": json_h, "task_horas": task_h,
                      "diff": diff, "modulo": info.get("modulo"),
                      "classe": classe})
    itens.sort(key=lambda x: (x["diff"] is None, -abs(x["diff"] or 0)))
    return _json({"ok": True, "cliente": _slug_first(nome), "customer": customer,
                  "total": len(itens), "nao_encontrados": nao_enc, "itens": itens})


@app.post("/api/ticket/<uuid>/decidir")
def api_ticket_decidir(uuid):
    """Grava a decisão em cockpit.decisoes E aplica a tag da decisão no Tasks SC
    (auto-tag). Usado tanto na decisão AO VIVO quanto no 'aplicar' da importação.
    Body: {decisao, estimativa?, nota?, classe?, aplicar_tag?(default true)}."""
    if (r := require_auth()):
        return r
    if (w := require_write()):
        return w
    if (sim := deny_simulacao()):
        return sim
    if (g := deny_uuid(uuid)):
        return g
    body = request.get_json(silent=True) or {}
    dec_ui = body.get("decisao")
    dec_db = _DEC_UI2DB.get(dec_ui, "pendente")
    est = _num(body.get("estimativa"))
    # grava a decisão (fonte da verdade no Supabase)
    execute("""
        insert into cockpit.decisoes
          (uuid_ticket, decisao, estimativa, observacao, classe, decided_by, decided_at, updated_at)
        values (%s,%s,%s,%s,%s,%s,now(),now())
        on conflict (uuid_ticket) do update set
          decisao=excluded.decisao,
          estimativa=coalesce(excluded.estimativa, cockpit.decisoes.estimativa),
          observacao=coalesce(excluded.observacao, cockpit.decisoes.observacao),
          classe=coalesce(excluded.classe, cockpit.decisoes.classe),
          decided_by=excluded.decided_by, updated_at=now()
    """, (uuid.upper(), dec_db, est, body.get("nota"), body.get("classe"), current_user()))
    # Auto-tag no Tasks SC. O perfil 'cliente' decide (a decisão acima já está
    # gravada no Supabase), mas não carimba tag na Task — por isso a auto-tag é
    # pulada em vez de a rota inteira ser barrada.
    tag_aplicada = None
    if perfil_de(current_user()) == "cliente":
        return _json({"ok": True, "uuid": uuid, "decisao": dec_ui, "tag": None,
                      "aviso": "Decisão gravada. A tag no Tasks SC não foi aplicada "
                               "porque o seu perfil (Cliente) não altera o Tasks SC."})
    if body.get("aplicar_tag", True) and dec_ui:
        mp = _decisao_tags_map()
        tag = mp.get(dec_ui)
        if tag:
            try:
                remover = []
                if mp.get("swap"):   # tira as outras tags de decisão
                    remover = [str(v) for k, v in mp.items()
                               if k != "swap" and str(v).upper() != tag.upper()]
                tasks_update(uuid, {"tags_add": [tag], "tags_remove": remover})
                _resync_tags(uuid)
                tag_aplicada = tag
            except Exception as e:
                return _json({"ok": True, "uuid": uuid, "tag": None,
                              "aviso": f"Decisão gravada, mas falhou aplicar a tag: {e}"})
    return _json({"ok": True, "uuid": uuid, "decisao": dec_ui, "tag": tag_aplicada})


# ─────────────────────────────────────────────────────────────────────────────
# Drive index — cockpit.integration_config['drive_index']
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/drive-index")
def api_drive_index():
    if (r := require_auth()):
        return r
    row = q("select value from cockpit.integration_config where key='drive_index'", one=True)
    if row and row["value"]:
        val = row["value"]
        if isinstance(val, str):
            val = json.loads(val)
        return _json({"ok": True, **val})
    f = PUBLIC_DIR / "drive_index.json"
    if f.exists():
        return _json({"ok": True, **json.loads(f.read_text(encoding="utf-8"))})
    return _json({"ok": True, "clientes": {}})


# ─────────────────────────────────────────────────────────────────────────────
# Tasks SC — leituras ao vivo
# ─────────────────────────────────────────────────────────────────────────────
def _is_prefixed(entry, prefix):
    d = entry.get("details") or ""
    plain = _strip_html(d[:120]).upper()
    return plain.startswith(prefix) or prefix in d[:80].upper()


@app.get("/api/ticket/<uuid>")
def api_ticket_detail(uuid):
    if (r := require_auth()):
        return r
    if (g := deny_uuid(uuid)):
        return g
    data, code, err = tasks_request("GET", f"/tickets/{uuid}")
    if code != 200:
        return _err(code or 500, f"Falha lendo ticket: {err}")
    items = (data.get("items") or []) if isinstance(data, dict) else []
    if not items:
        return _err(404, "Ticket não encontrado.")
    out = {"ok": True, "ticket": items[0]}
    # ?tags=1 — é o que o botão 🔄 Sincronizar usa. O GET /tickets/<uuid> não
    # devolve os NOMES das tags (o board vive de cockpit.ticket_tags, que só o
    # sync periódico atualizava). Aqui puxamos as tags ao vivo, espelhamos no
    # ticket e alimentamos o catálogo com o que for tag nova — assim ela fica
    # disponível no autocomplete dos demais tickets na hora.
    if request.args.get("tags") in ("1", "true", "sim"):
        tdata, tcode, terr = tasks_request("GET", f"/tickets/tags/{uuid}")
        if tcode == 200 and isinstance(tdata, dict):
            titems = [t for t in (tdata.get("items") or [])
                      if str(t.get("tag") or "").strip()]
            out["tags"] = [str(t["tag"]).strip() for t in titems]
            out["tags_items"] = [{"id": t.get("id"), "tag": str(t["tag"]).strip()}
                                 for t in titems]
            out["tags_novas"] = _catalog_upsert(titems)   # entram no catálogo
            _resync_tags(uuid, titems)                    # espelha no ticket
        else:
            out["tags_erro"] = terr or f"HTTP {tcode}"
    return _json(out)


@app.get("/api/ticket/<uuid>/history")
def api_ticket_history(uuid):
    if (r := require_auth()):
        return r
    if (g := deny_uuid(uuid)):
        return g
    data, code, err = tasks_request("GET", f"/tickets/history/list/{uuid}",
                                    params={"order": "-date,-time", "_t": int(time.time())})
    if code != 200 or not isinstance(data, dict):
        return _err(code or 500, f"Falha lendo histórico: {err}")
    items = data.get("items") or []
    # anexa as Observações salvas no banco (fallback quando o Tasks SC recusou o
    # texto). Ficam no topo, mais recentes primeiro; como as de personalização
    # começam com a marca PERSONALIZACAO:, caem no bucket `tec` automaticamente.
    try:
        obs = q("""select details, autor, created_at from cockpit.observacoes
                   where uuid_ticket=%s order by created_at desc""", (uuid.upper(),))
        formatted = []
        for o in (obs or []):
            ca = o.get("created_at")
            formatted.append({
                "details": o["details"],
                "date": ca.strftime("%Y%m%d") if ca else "",
                "time": ca.strftime("%H:%M") if ca else "",
                "user_name": ((o.get("autor") or "") + " · (banco)").strip(" ·"),
                "type": "1", "uuid_history": "", "_db": True,
            })
        items = formatted + items
    except Exception:
        pass
    nlm = [i for i in items if _is_prefixed(i, NLM_PREFIX)]
    tec = [i for i in items if _is_prefixed(i, TEC_PREFIX)]
    return _json({"ok": True, "uuid": uuid, "items": items, "nlm": nlm, "tec": tec})


@app.get("/api/ticket/<uuid>/observers")
def api_ticket_observers(uuid):
    """Observadores da Task, ao vivo. `raw` volta junto para diagnóstico: se um
    dia a API mudar o nome da chave do código, dá para ver aqui sem adivinhar."""
    if (r := require_auth()):
        return r
    if (g := deny_uuid(uuid)):
        return g
    try:
        items, codigos = _observers_fetch(uuid)
    except Exception as e:
        return _err(502, f"Falha lendo observadores: {e}")
    lista = []
    if len(items) == len(codigos):
        for it, cod in zip(items, codigos):
            lista.append({"id": cod, "nome": _observer_nome(it) or cod})
    else:
        lista = [{"id": c, "nome": c} for c in codigos]
    return _json({"ok": True, "uuid": uuid, "codigos": codigos,
                  "observadores": lista, "raw": items})


@app.get("/api/pessoas")
def api_pessoas():
    """Combos de pessoas do drawer.

    - `internos`  → consultores TOTVS. Derivados de cockpit.tickets (código +
      descrição que o próprio Tasks SC já gravou no `raw`), respeitando o
      recorte de clientes do usuário. Sem seed manual para envelhecer.
    - `cliente`   → usuários do cadastro do cliente, ao vivo em
      GET /assigned_users?customer=… (mesma fonte do combo do portal).
    """
    if (r := require_auth()):
        return r
    chave = request.args.get("cliente", "").strip()
    customer, _nome = _resolve_customer(chave) if chave else (None, None)
    if chave and not customer:
        return _err(404, f"Cliente '{chave}' não encontrado.")
    if customer and (d := deny_customer(customer)):
        return d

    permitidos = allowed_customers()
    sql = """
        select t.user_assigned as id,
               max(coalesce(nullif(t.raw->>'user_assigned_description',''),
                            t.user_assigned)) as nome
          from cockpit.tickets t
         where coalesce(t.user_assigned,'') <> ''
    """
    params = []
    if permitidos is not None:
        if not permitidos:
            return _json({"ok": True, "internos": [], "cliente": []})
        sql += " and t.customer = any(%s)"
        params.append(list(permitidos))
    sql += " group by t.user_assigned order by 2"
    try:
        internos = [{"id": r["id"], "nome": r["nome"]} for r in (q(sql, params or None) or [])]
    except Exception:
        internos = []

    do_cliente = []
    if customer:
        data, code, _ = tasks_request("GET", "/assigned_users",
                                      params={"customer": customer, "page": 1,
                                              "pageSize": 200, "search": ""})
        if code == 200 and isinstance(data, dict):
            for it in (data.get("items") or []):
                cod = ""
                for k in ("assigned_customer", "user", "code", "id"):
                    v = it.get(k)
                    if isinstance(v, str) and v.strip():
                        cod = v.strip()
                        break
                nome = ""
                for k in ("assigned_customer_description", "name", "user_name", "description"):
                    v = it.get(k)
                    if isinstance(v, str) and v.strip():
                        nome = v.strip()
                        break
                if cod:
                    do_cliente.append({"id": cod, "nome": nome or cod})
    return _json({"ok": True, "customer": customer,
                  "internos": internos, "cliente": do_cliente})


@app.get("/api/tags-catalog")
def api_tags_catalog():
    """Autocomplete: lê o catálogo do SUPABASE (instantâneo). Se estiver vazio,
    avisa para rodar o sync (POST /api/tags/sync)."""
    if (r := require_auth()):
        return r
    search = request.args.get("search", "").strip().upper()
    rows = q("select id, tag from cockpit.tags_catalogo order by upper(tag)")
    items = [{"id": r["id"], "tag": r["tag"]} for r in (rows or [])]
    if search:
        items = [i for i in items if search in i["tag"].upper()]
    return _json({"ok": True, "items": items, "total": len(items),
                  "vazio": not items})


@app.post("/api/tags/sync")
def api_tags_sync():
    """Sincroniza o catálogo de tags da API para cockpit.tags_catalogo.

    Processa páginas a partir de ?page=N dentro de um orçamento de tempo (~40s)
    e devolve {next_page, done}. O chamador repete até done=true. As tags mudam
    raramente, então isso roda de vez em quando (ou por cron)."""
    if (r := require_auth()):
        return r
    if (w := require_tasks_write()):
        return w
    if (sim := deny_simulacao()):
        return sim
    page = int(request.args.get("page", 1))
    ini = time.time()
    vistos, ultima, done = {}, page, True
    for pg, nome, tid in _catalog_pages(page=page):
        vistos[tid] = nome
        ultima = pg
        if time.time() - ini > 40:      # orçamento de tempo do serverless
            done = False
            break
    if vistos:
        with db() as c, c.cursor() as cur:
            psycopg2.extras.execute_values(cur, """
                insert into cockpit.tags_catalogo (id, tag, synced_at) values %s
                on conflict (id) do update set tag=excluded.tag, synced_at=now()
            """, [(i, n) for i, n in vistos.items()],
                template="(%s,%s,now())", page_size=500)
    _TAG_CATALOG_CACHE["map"] = None     # invalida o cache
    total = q("select count(*) n from cockpit.tags_catalogo", one=True)["n"]
    return _json({"ok": True, "page": page, "next_page": ultima + 1,
                  "done": done, "tags_no_catalogo": total})


# ─────────────────────────────────────────────────────────────────────────────
# Tasks SC — escritas ao vivo (+ espelho best-effort no Supabase)
# ─────────────────────────────────────────────────────────────────────────────
def _mirror_ticket(uuid, changes):
    """Espelha campos alterados em cockpit.tickets (best-effort)."""
    colmap = {"title": "titulo", "description": "descricao", "time_estimate": "time_estimate",
              "due_date": "due_date", "user_assigned": "user_assigned",
              "assigned_customer": "assigned_customer", "priority": "prioridade"}
    sets, vals = [], []
    for k, col in colmap.items():
        if k in changes:
            sets.append(f"{col}=%s")
            vals.append(changes[k] or None)
    if sets:
        vals.append(uuid)
        try:
            execute(f"update cockpit.tickets set {', '.join(sets)}, updated_at=now() "
                    f"where uuid_ticket=%s", vals)
        except Exception:
            pass
    if changes.get("tags_add") or changes.get("tags_remove") or "tags" in changes:
        _resync_tags(uuid)


def _resync_tags(uuid, items=None):
    """Após alterar/reler tags no Tasks SC, reescreve cockpit.ticket_tags.

    `items` = a resposta crua de `/tickets/tags/<uuid>` já em mãos (evita uma
    segunda chamada à API quando quem chama acabou de buscá-la).
    """
    try:
        if items is None:
            tdata, tcode, _ = tasks_request("GET", f"/tickets/tags/{uuid}")
            if tcode != 200:
                return
            items = tdata.get("items") or []
        names = [(t.get("tag") or "").strip() for t in items]
        names = [n for n in names if n]
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("delete from cockpit.ticket_tags where uuid_ticket=%s", (uuid,))
                for n in names:
                    cur.execute("insert into cockpit.ticket_tags (uuid_ticket, raw_tag) "
                                "values (%s, %s)", (uuid, n))
    except Exception:
        pass


@app.post("/api/ticket/<uuid>/update")
def api_ticket_update(uuid):
    if (r := require_auth()):
        return r
    if (w := require_tasks_write()):
        return w
    if (sim := deny_simulacao()):
        return sim
    if (g := deny_uuid(uuid)):
        return g
    changes = request.get_json(silent=True)
    if not isinstance(changes, dict) or not changes:
        return _err(400, "Body vazio — envie os campos a alterar.")
    try:
        result = tasks_update(uuid, changes)
    except ValueError as e:
        return _err(400, str(e))
    except Exception as e:
        return _err(502, str(e))
    _mirror_ticket(uuid, changes)
    # devolve a lista de tags que REALMENTE ficou no Tasks SC — o front usa isso
    # como verdade, em vez de confiar no espelho.
    tags_fim = None
    if changes.get("tags_add") or changes.get("tags_remove"):
        td, tc, _ = tasks_request("GET", f"/tickets/tags/{uuid}")
        if tc == 200:
            tags_fim = [str(t.get("tag") or "").strip() for t in (td.get("items") or [])]
    return _json({"ok": True, "result": result, "tags": tags_fim})


@app.post("/api/ticket/<uuid>/history")
def api_ticket_history_post(uuid):
    if (r := require_auth()):
        return r
    if (w := require_tasks_write()):
        return w
    if (sim := deny_simulacao()):
        return sim
    if (g := deny_uuid(uuid)):
        return g
    body = request.get_json(silent=True) or {}
    html = (body.get("body_html") or "").strip()
    if not html or not _strip_html(html):
        return _err(400, "Texto vazio — escreva antes de gravar.")
    if body.get("raw"):
        details = html
        type_ = str(body.get("type") or "1")
        if type_ not in ("0", "1", "2"):
            type_ = "1"
    else:
        stamp = time.strftime("%d/%m/%Y %H:%M")
        details = (f"<div><b>{TEC_PREFIX}</b> Especificação técnica da personalização "
                   f"· atualizada em {stamp}</div><div><br></div>{html}")
        type_ = "1"
    payload = {"type": type_, "uuid_ticket": uuid, "uuid_history": "",
               "details": details, "duration": ""}
    data, code, err = tasks_request("POST", "/tickets/history", body=payload)
    if code >= 400:
        # O campo do Tasks SC recusou (HTTP 400 típico de texto grande demais).
        # Em vez de perder o conteúdo, gravamos como Observação no banco (Supabase)
        # e a tela volta a exibir via GET /history (bucket tec/histórico).
        tipo_obs = "historico" if body.get("raw") else "personalizacao"
        try:
            execute("""insert into cockpit.observacoes (uuid_ticket, tipo, details, autor)
                       values (%s,%s,%s,%s)""",
                    (uuid.upper(), tipo_obs, details, current_user()))
        except Exception as e:
            return _err(code or 500,
                        f"Falha gravando no Tasks SC ({err}) e no banco ({e}).")
        return _json({"ok": True, "uuid": uuid, "details": details,
                      "saved_to": "db",
                      "aviso": ("O Tasks SC recusou o texto (HTTP 400 — provável "
                                "limite de tamanho do campo). Salvo como Observação "
                                "no banco; aparece aqui normalmente.")})
    # espelho best-effort em cockpit.ocorrencias
    try:
        uhist = ""
        if isinstance(data, dict):
            uhist = data.get("uuid_history") or (data.get("items") or [{}])[0].get("uuid_history", "")
        if uhist:
            execute("""insert into cockpit.ocorrencias
                       (uuid_history, uuid_ticket, tipo, details, autor, origem, occurred_at)
                       values (%s,%s,%s,%s,%s,'gaps-vercel',now())
                       on conflict (uuid_history) do nothing""",
                    (uhist, uuid, type_, details, current_user()))
    except Exception:
        pass
    return _json({"ok": True, "uuid": uuid, "details": details, "history": data})


# ─────────────────────────────────────────────────────────────────────────────
# Refresh — re-sincroniza tickets do cliente ao vivo (upsert raw + tags)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/refresh")
def api_refresh():
    """Recarrega a base do cliente.

    NÃO puxa mais a lista da API do Tasks SC. Motivo (bug real, 2026-07-13): a
    API IGNORA o filtro ?customer=, devolvendo tickets de vários clientes — e a
    versão anterior gravava todos com o customer PEDIDO, carimbando tickets de
    outros clientes como se fossem do selecionado (400 linhas corrompidas).

    Quem mantém cockpit.tickets fresco é o cron `tickets-sync-15m` do Supabase,
    que grava cada ticket com o customer que vem no próprio payload. Aqui só
    devolvemos o estado atual — o front recarrega a lista do banco.
    """
    if (r := require_auth()):
        return r
    if (w := require_tasks_write()):
        return w
    if (sim := deny_simulacao()):
        return sim
    chave = request.args.get("cliente", "DIGITRO")
    customer, nome = _resolve_customer(chave)
    if not customer:
        return _err(404, f"Cliente '{chave}' não encontrado.")
    if (d := deny_customer(customer)):
        return d
    row = q("""select count(*) n, max(synced_at) s
               from cockpit.tickets where customer=%s""", (customer,), one=True)
    return _json({"ok": True, "cliente": _slug_first(nome), "customer": customer,
                  "tickets": row["n"], "sincronizado_em": row["s"],
                  "info": "Base sincronizada automaticamente a cada 15 min."})


# ─────────────────────────────────────────────────────────────────────────────
# Gmail — rascunho DE VERDADE na caixa do usuário, via IMAP APPEND
#
# Por que IMAP e não a Gmail API: o gmail_server.py do totvs-dashboard já fazia
# assim (App Password de 16 caracteres + APPEND na pasta de Rascunhos) e isso
# dispensa Google Cloud, OAuth, tela de consentimento e refresh token. Cada
# usuário guarda a PRÓPRIA credencial (cockpit.gmail_credenciais, senha cifrada)
# para o rascunho nascer na caixa certa, com o remetente certo.
# Sem credencial, cai no modo antigo: salva em cockpit.email_drafts.
# ─────────────────────────────────────────────────────────────────────────────
IMAP_HOST = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("GMAIL_IMAP_PORT", "993"))


def _fernet():
    """Chave derivada do SESSION_SECRET — não exige env var nova. Trocar o
    SESSION_SECRET invalida as senhas guardadas (é preciso reconectar)."""
    from cryptography.fernet import Fernet          # import tardio: só quem usa paga
    key = base64.urlsafe_b64encode(
        hashlib.sha256(("gmail-cred:" + SESSION_SECRET).encode()).digest())
    return Fernet(key)


def _cred_get(usuario):
    """{gmail_email, app_password} do usuário, decifrado. None se não tem.

    Tolera a tabela ainda não existir (migration 0011 não rodada): nesse caso o
    app segue funcionando no modo antigo em vez de estourar 500 na tela.
    """
    try:
        row = q("select gmail_email, senha_cif from cockpit.gmail_credenciais where usuario=%s",
                (usuario,), one=True)
    except Exception:
        return None
    if not row:
        return None
    try:
        senha = _fernet().decrypt(row["senha_cif"].encode()).decode()
    except Exception:
        return None            # SESSION_SECRET mudou ou registro corrompido
    return {"gmail_email": row["gmail_email"], "app_password": senha}


def _imap_login(gmail_email, app_password, timeout=20):
    m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=timeout)
    m.login(gmail_email, str(app_password).replace(" ", ""))   # o Google mostra a senha em blocos de 4
    return m


def _drafts_folder(mail):
    """Acha a pasta de Rascunhos independente do idioma da conta: primeiro pela
    flag especial \\Drafts do LIST, depois pelos nomes conhecidos."""
    try:
        typ, data = mail.list()
        if typ == "OK":
            for raw in data or []:
                line = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
                if "\\Drafts" in line:
                    m = re.search(r'"([^"]+)"\s*$', line)
                    if m:
                        return m.group(1)
    except Exception:
        pass
    return "[Gmail]/Drafts"


def _gmail_draft_imap(cred, to, cc, subject, body_html):
    """Cria o rascunho. Devolve (info, erro)."""
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.utils import formatdate, make_msgid

    remetente = cred["gmail_email"]
    msg = MIMEMultipart("alternative")
    msg["From"] = remetente
    if to:
        msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = (subject or "").strip() or "(sem assunto)"
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    texto = _strip_html(body_html or "").strip() or "(conteúdo em HTML — abra no Gmail para ver)"
    msg.attach(MIMEText(texto, "plain", "utf-8"))
    msg.attach(MIMEText(body_html or "", "html", "utf-8"))
    raw = msg.as_bytes()

    try:
        mail = _imap_login(remetente, cred["app_password"])
    except Exception as e:
        return None, f"login IMAP falhou: {e}"
    try:
        pasta = _drafts_folder(mail)
        typ, resp = mail.append(f'"{pasta}"', "(\\Draft)",
                                imaplib.Time2Internaldate(time.time()), raw)
        if typ != "OK":
            return None, f"APPEND falhou: {resp!r}"
        return {"folder": pasta, "bytes": len(raw), "from": remetente}, None
    except Exception as e:
        return None, str(e)
    finally:
        try:
            mail.logout()
        except Exception:
            pass


@app.get("/api/gmail/health")
def api_gmail_health():
    if (r := require_auth()):
        return r
    cred = _cred_get(current_user())
    return _json({"ok": True, "configured": True,
                  "mode": "imap-draft" if cred else "draft-store",
                  "gmail_conectado": bool(cred),
                  "gmail_email": cred["gmail_email"] if cred else None,
                  "info": ("Rascunhos vão para a sua caixa do Gmail." if cred else
                           "Sem credencial Gmail: os rascunhos ficam em cockpit.email_drafts. "
                           "Conecte a sua conta em Minha Conta.")})


@app.get("/api/gmail/credencial")
def api_gmail_cred_get():
    if (r := require_auth()):
        return r
    try:
        row = q("""select gmail_email, validado_em, updated_at
                     from cockpit.gmail_credenciais where usuario=%s""",
                (current_user(),), one=True)
    except Exception as e:
        # tabela ausente = migration 0011 pendente. Dizer isso é mais útil
        # do que um "não foi possível verificar" genérico na tela.
        return _json({"ok": True, "configurado": False, "gmail_email": None,
                      "validado_em": None, "indisponivel": str(e)[:200]})
    return _json({"ok": True, "configurado": bool(row),
                  "gmail_email": row["gmail_email"] if row else None,
                  "validado_em": str(row["validado_em"]) if row and row["validado_em"] else None})


@app.post("/api/gmail/credencial")
def api_gmail_cred_set():
    """Salva a credencial DEPOIS de provar que ela funciona (login IMAP real).
    Melhor descobrir aqui do que na hora de gerar o rascunho."""
    if (r := require_auth()):
        return r
    if (w := require_write()):
        return w
    if (sim := deny_simulacao()):
        return sim
    body = request.get_json(silent=True) or {}
    email_gmail = str(body.get("gmail_email") or "").strip()
    senha = str(body.get("app_password") or "").replace(" ", "").strip()
    if not email_gmail or "@" not in email_gmail:
        return _err(400, "Informe o endereço Gmail.")
    if len(senha) < 16:
        return _err(400, "A App Password do Google tem 16 caracteres. "
                         "Gere em https://myaccount.google.com/apppasswords (exige 2FA ligado).")
    try:
        m = _imap_login(email_gmail, senha, timeout=15)
        m.logout()
    except Exception as e:
        return _err(400, f"O Gmail recusou essa credencial: {e}. Confira o e-mail e "
                         f"use uma App Password (a senha normal da conta não serve).")
    try:
        cif = _fernet().encrypt(senha.encode()).decode()
    except Exception as e:
        return _err(500, f"Falha cifrando a senha: {e}")
    execute("""insert into cockpit.gmail_credenciais
                 (usuario, gmail_email, senha_cif, validado_em, updated_at)
               values (%s,%s,%s,now(),now())
               on conflict (usuario) do update
                 set gmail_email=excluded.gmail_email, senha_cif=excluded.senha_cif,
                     validado_em=now(), updated_at=now()""",
            (current_user(), email_gmail, cif))
    return _json({"ok": True, "gmail_email": email_gmail})


@app.post("/api/gmail/credencial/remover")
def api_gmail_cred_del():
    if (r := require_auth()):
        return r
    if (sim := deny_simulacao()):
        return sim
    execute("delete from cockpit.gmail_credenciais where usuario=%s", (current_user(),))
    return _json({"ok": True})


@app.post("/api/gmail/draft")
def api_gmail_draft():
    if (r := require_auth()):
        return r
    if (w := require_tasks_write()):
        return w
    if (sim := deny_simulacao()):
        return sim
    body = request.get_json(silent=True) or {}
    _u = body.get("uuid_ticket") or body.get("uuid")
    if _u and (g := deny_uuid(_u)):
        return g
    tipo = body.get("tipo") or "custom"
    if tipo not in ("cobrar_cliente", "confirmar_andamento", "cobrar_responsavel",
                    "gaps_filtrados", "custom"):
        tipo = "custom"
    to = body.get("destinatario") or body.get("to") or ""
    cc = body.get("cc") or ""
    assunto = body.get("assunto") or body.get("subject") or ""
    # bodyHtml estava de fora desta lista — o board mandava esse nome e o corpo
    # era gravado como NULL. Aceita todos os apelidos usados pelas telas.
    corpo = (body.get("corpo_html") or body.get("bodyHtml")
             or body.get("body") or body.get("html") or "")

    # O registro em cockpit.email_drafts é HISTÓRICO — o entregável é o rascunho
    # no Gmail. `tipo` é um enum no banco (cockpit.draft_tipo): se o valor ainda
    # não foi acrescentado lá, cai para 'custom' e, no pior caso, segue sem o
    # histórico. Nunca deixe isso impedir o rascunho de nascer.
    def _guarda(tp):
        row = q("""
            insert into cockpit.email_drafts
              (uuid_ticket, tipo, destinatario, assunto, corpo_html, status, created_by)
            values (%s,%s,%s,%s,%s,'rascunho',%s)
            returning id
        """, (_u, tp, to, assunto, corpo, current_user()), one=True)
        return row["id"] if row else None

    draft_id, draft_erro = None, None
    try:
        draft_id = _guarda(tipo)
    except Exception as e:
        try:
            draft_id = _guarda("custom")
        except Exception as e2:
            draft_erro = f"{e} / {e2}"

    guardado = " (histórico não gravado)" if draft_erro else ""
    cred = _cred_get(current_user())
    if not cred:
        if draft_erro:
            return _err(500, f"Sem conta Gmail conectada e o histórico também falhou: {draft_erro}")
        return _json({"ok": True, "id": draft_id, "mode": "saved-to-db",
                      "gmail": False,
                      "info": "Rascunho salvo no banco. Para ele nascer direto no seu Gmail, "
                              "conecte a sua conta em Minha Conta → Gmail."})
    info, erro = _gmail_draft_imap(cred, to, cc, assunto, corpo)
    if erro:
        return _json({"ok": True, "id": draft_id, "mode": "saved-to-db",
                      "gmail": False, "gmail_erro": erro, "draft_erro": draft_erro,
                      "info": f"O Gmail recusou: {erro}"})
    return _json({"ok": True, "id": draft_id, "mode": "imap-draft", "gmail": True,
                  "detalhe": info, "draft_erro": draft_erro,
                  "info": "Rascunho criado no seu Gmail — abra os Rascunhos para "
                          "revisar e enviar." + guardado})


# ─────────────────────────────────────────────────────────────────────────────
# Static assets do /public (fallback) + 404
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/<path:asset>")
def static_assets(asset):
    if asset.startswith("api/"):
        return _err(404, "Rota de API desconhecida.")
    safe = (PUBLIC_DIR / asset).resolve()
    if PUBLIC_DIR in safe.parents and safe.exists() and safe.is_file():
        # páginas sensíveis exigem login
        if safe.name in ("gaps-decisao.html", "gaps-reuniao.html", "gaps-import.html", "kb.html") and not current_user():
            return redirect("/login", code=302)
        ext = safe.suffix.lower()
        ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
                 ".css": "text/css", ".json": "application/json",
                 ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml"}.get(
                     ext, "application/octet-stream")
        return Response(safe.read_bytes(), mimetype=ctype)
    return _err(404, "Não encontrado.")


# ─────────────────────────────────────────────────────────────────────────────
# Local dev
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8090)), debug=True)
