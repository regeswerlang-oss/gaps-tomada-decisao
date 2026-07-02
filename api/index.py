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
  POST /api/login             → {email, senha} → cookie de sessão
  POST /api/logout            → limpa a sessão
  GET  /api/me                → sessão atual

Dados (Supabase) — exigem login
  GET  /api/clientes                       → cockpit.clientes
  GET  /api/tickets?cliente=digitro        → cockpit.tickets (+ tags)
  GET  /api/decisoes?cliente=digitro       → cockpit.decisoes
  POST /api/decisoes?cliente=digitro       → upsert cockpit.decisoes
  GET  /api/drive-index                    → cockpit.integration_config['drive_index']

Tasks SC (ao vivo) — exigem login
  GET  /api/ticket/<uuid>                  → detalhe do ticket
  GET  /api/ticket/<uuid>/history          → histórico (+ NOTEBOOKLM:/PERSONALIZACAO:)
  GET  /api/tags-catalog[?search=]         → catálogo de tags
  POST /api/ticket/<uuid>/update           → GET→merge→PUT + espelho no Supabase
  POST /api/ticket/<uuid>/history          → grava ocorrência (PERSONALIZACAO:/avulsa)
  POST /api/refresh?cliente=DIGITRO        → re-sincroniza tickets do cliente ao vivo

Gmail
  GET  /api/gmail/health                   → status do modo de rascunho
  POST /api/gmail/draft                    → salva rascunho em cockpit.email_drafts
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

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


def make_session(email: str, nome: str) -> str:
    exp = int(time.time()) + SESSION_TTL
    raw = json.dumps({"e": email, "n": nome, "x": exp}, ensure_ascii=False)
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
    s = read_session()
    return s.get("e") if s else None


def require_auth():
    """Retorna None se autenticado, ou uma Response 401 se não."""
    if current_user():
        return None
    return _err(401, "Não autenticado.")


# ── Controle de acesso por CLIENTE (customer) ────────────────────────────────
# Regra (modo estrito, igual ao dashboard Next.js do cockpit):
#   admin           → None  = vê TODOS os clientes.
#   usuário comum   → set de customers liberados (pode ser vazio = não vê nada).
def allowed_customers():
    email = current_user()
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


def verify_scrypt(stored: str, senha: str) -> bool:
    """Formato: scrypt$<salt hex 16B>$<hash hex 64B>. r=8, p=1; N auto-detectado."""
    try:
        scheme, salt_hex, hash_hex = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    salt = bytes.fromhex(salt_hex)
    dklen = len(hash_hex) // 2
    r = int(os.environ.get("SCRYPT_R", 8))
    p = int(os.environ.get("SCRYPT_P", 1))
    env_n = os.environ.get("SCRYPT_N")
    n_candidates = [int(env_n)] if env_n else [16384, 32768, 8192, 65536, 4096]
    for n in n_candidates:
        try:
            dk = hashlib.scrypt(senha.encode(), salt=salt, n=n, r=r, p=p,
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


def tasks_update(uuid, changes):
    """GET → merge → PUT (a API não tem PATCH)."""
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
    tag_ids = [t["id"] for t in (tag_data.get("items") or [])] if tcode == 200 else []
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
        "observer": [], "tags": list(tag_ids),
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
    return _json({"ok": True, "email": s["e"], "nome": s.get("n")})


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
              "contorno": "workaround", "recusar": "refuse", "pendente": None}
_DEC_UI2DB = {"approve": "aprovar", "phase2": "segunda_fase",
              "workaround": "contorno", "refuse": "recusar",
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
    return _json({"ok": True, "ticket": items[0]})


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
    nlm = [i for i in items if _is_prefixed(i, NLM_PREFIX)]
    tec = [i for i in items if _is_prefixed(i, TEC_PREFIX)]
    return _json({"ok": True, "uuid": uuid, "items": items, "nlm": nlm, "tec": tec})


@app.get("/api/tags-catalog")
def api_tags_catalog():
    if (r := require_auth()):
        return r
    search = request.args.get("search", "").strip().upper()
    items, page = [], 1
    while page <= 25:
        data, code, err = tasks_request("GET", "/tickets/tags", params={
            "page": page, "pageSize": 200, "order": "tag", "fields": "id,tag"})
        if code != 200 or not isinstance(data, dict):
            if page == 1:
                return _err(code or 500, f"Falha no catálogo: {err}")
            break
        items += data.get("items") or []
        if not data.get("hasNext"):
            break
        page += 1
    if search:
        items = [i for i in items if search in (i.get("tag") or "").upper()]
    return _json({"ok": True, "items": items})


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
    if "tags" in changes:
        _resync_tags(uuid)


def _resync_tags(uuid):
    """Após alterar tags no Tasks SC, reescreve cockpit.ticket_tags."""
    try:
        tdata, tcode, _ = tasks_request("GET", f"/tickets/tags/{uuid}")
        if tcode != 200:
            return
        names = [(t.get("tag") or "").strip() for t in (tdata.get("items") or [])]
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
    return _json({"ok": True, "result": result})


@app.post("/api/ticket/<uuid>/history")
def api_ticket_history_post(uuid):
    if (r := require_auth()):
        return r
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
        return _err(code or 500, f"Falha gravando ocorrência: {err}")
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
    if (r := require_auth()):
        return r
    chave = request.args.get("cliente", "DIGITRO")
    customer, nome = _resolve_customer(chave)
    if not customer:
        return _err(404, f"Cliente '{chave}' não encontrado.")
    if (d := deny_customer(customer)):
        return d
    started = time.time()
    processed = upserted = 0
    page = 1
    try:
        while page <= 60:
            data, code, err = tasks_request("GET", "/tickets", params={
                "customer": customer, "page": page, "pageSize": 100, "order": "id"})
            if code != 200 or not isinstance(data, dict):
                if page == 1:
                    return _err(code or 500, f"Falha lendo tickets: {err}")
                break
            items = data.get("items") or []
            for tk in items:
                processed += 1
                uuid = tk.get("uuid")
                if not uuid:
                    continue
                cli = tk.get("customer_name_slim") or tk.get("customer_name") or nome
                execute("""
                    insert into cockpit.tickets
                       (uuid_ticket, customer, cliente, titulo, status_tasks, time_estimate,
                        due_date, user_assigned, assigned_customer, raw, synced_at, updated_at)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now())
                    on conflict (uuid_ticket) do update set
                       customer=excluded.customer, cliente=excluded.cliente,
                       titulo=excluded.titulo, status_tasks=excluded.status_tasks,
                       time_estimate=excluded.time_estimate, due_date=excluded.due_date,
                       user_assigned=excluded.user_assigned,
                       assigned_customer=excluded.assigned_customer,
                       raw=excluded.raw, synced_at=now(), updated_at=now()
                """, (uuid, customer, cli, tk.get("title"),
                      tk.get("status_description") or tk.get("status"),
                      tk.get("time_estimate") or None,
                      tk.get("due_date") or None, tk.get("user_assigned") or None,
                      tk.get("assigned_customer") or None, json.dumps(tk)))
                upserted += 1
            if not data.get("hasNext"):
                break
            page += 1
        dur = int((time.time() - started) * 1000)
        try:
            execute("""insert into cockpit.sync_log
                       (source, status, started_at, finished_at, duration_ms,
                        tickets_processed, tickets_upserted)
                       values ('gaps-vercel','success', to_timestamp(%s), now(), %s, %s, %s)""",
                    (started, dur, processed, upserted))
        except Exception:
            pass
        return _json({"ok": True, "cliente": _slug_first(nome), "customer": customer,
                      "processed": processed, "upserted": upserted, "duration_ms": dur})
    except Exception as e:
        return _err(500, f"Falha no refresh: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Gmail — salva rascunho em cockpit.email_drafts (modo Vercel-native)
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/gmail/health")
def api_gmail_health():
    if (r := require_auth()):
        return r
    return _json({"ok": True, "configured": True, "mode": "draft-store",
                  "info": "Rascunhos são salvos em cockpit.email_drafts."})


@app.post("/api/gmail/draft")
def api_gmail_draft():
    if (r := require_auth()):
        return r
    body = request.get_json(silent=True) or {}
    _u = body.get("uuid_ticket") or body.get("uuid")
    if _u and (g := deny_uuid(_u)):
        return g
    tipo = body.get("tipo") or "custom"
    if tipo not in ("cobrar_cliente", "confirmar_andamento", "cobrar_responsavel", "custom"):
        tipo = "custom"
    row = q("""
        insert into cockpit.email_drafts
          (uuid_ticket, tipo, destinatario, assunto, corpo_html, status, created_by)
        values (%s,%s,%s,%s,%s,'rascunho',%s)
        returning id
    """, (body.get("uuid_ticket") or body.get("uuid"), tipo,
          body.get("destinatario") or body.get("to"),
          body.get("assunto") or body.get("subject"),
          body.get("corpo_html") or body.get("body") or body.get("html"),
          current_user()), one=True)
    return _json({"ok": True, "id": row["id"] if row else None,
                  "mode": "saved-to-db",
                  "info": "Rascunho salvo. Envio real via Gmail API fica para a v2."})


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
        if safe.name in ("gaps-decisao.html", "gaps-reuniao.html") and not current_user():
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
