#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
set_password.py — gera o hash scrypt no formato usado por cockpit.usuarios_login
(scrypt$<salt hex 16B>$<hash hex 64B>) e imprime o SQL de upsert.

Uso:
    python3 scripts/set_password.py reges.werlang@totvs.com.br "MinhaSenha" "Reges Werlang"

Depois cole o SQL no Supabase (SQL Editor) OU rode via psql. Os parâmetros
scrypt (N=16384, r=8, p=1, dklen=64) batem com o verificador do backend.
"""
import hashlib
import os
import sys

N, R, P, DKLEN = 16384, 8, 1, 64


def make_hash(senha: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(senha.encode(), salt=salt, n=N, r=R, p=P, dklen=DKLEN,
                        maxmem=132 * 1024 * 1024)
    return f"scrypt${salt.hex()}${dk.hex()}"


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("uso: set_password.py <email> <senha> [nome]")
        sys.exit(1)
    email = sys.argv[1].strip().lower()
    senha = sys.argv[2]
    nome = sys.argv[3] if len(sys.argv) > 3 else email.split("@")[0]
    h = make_hash(senha)
    print("-- cole no SQL Editor do Supabase:")
    print(f"""insert into cockpit.usuarios_login (email, senha_hash, nome, ativo)
values ('{email}', '{h}', '{nome.replace("'", "''")}', true)
on conflict (email) do update set senha_hash=excluded.senha_hash,
  nome=excluded.nome, ativo=true, updated_at=now();""")
