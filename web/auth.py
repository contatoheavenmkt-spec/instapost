"""
Auth: usuários, senhas (pbkdf2), sessões via cookie, e convites por link.

Owner é criado no primeiro boot (ver `ensure_owner_seed`).
Signup público é bloqueado: só com token de convite válido.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

from core.paths import USERS_FILE, INVITES_FILE, SECRET_FILE

OWNER_DEFAULT_EMAIL = os.environ.get("INSTAPOST_OWNER_EMAIL", "admin@localhost")
OWNER_DEFAULT_PASSWORD = os.environ.get("INSTAPOST_OWNER_PASSWORD", "")

PBKDF2_ITERATIONS = 200_000


# ----- secret -----

def get_or_create_secret() -> str:
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    s = secrets.token_urlsafe(48)
    SECRET_FILE.write_text(s, encoding="utf-8")
    try:
        if os.name == "nt":
            pass
        else:
            os.chmod(SECRET_FILE, 0o600)
    except Exception:
        pass
    return s


# ----- password hashing -----

def hash_password(password: str, salt: Optional[str] = None) -> dict:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return {"salt": salt, "hash": dk.hex(), "algo": "pbkdf2_sha256", "iters": PBKDF2_ITERATIONS}


def verify_password(password: str, stored: dict) -> bool:
    if not stored:
        return False
    salt = stored.get("salt", "")
    iters = stored.get("iters", PBKDF2_ITERATIONS)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iters)
    return secrets.compare_digest(dk.hex(), stored.get("hash", ""))


# ----- users store -----

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_users() -> list[dict]:
    if not USERS_FILE.exists():
        return []
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_users(users: list[dict]) -> None:
    USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


def find_user(email: str) -> Optional[dict]:
    email = email.lower().strip()
    for u in load_users():
        if u["email"].lower() == email:
            return u
    return None


def create_user(email: str, password: str, role: str = "member", invited_by: Optional[str] = None) -> dict:
    users = load_users()
    email = email.lower().strip()
    if any(u["email"].lower() == email for u in users):
        raise ValueError("Email já cadastrado")
    if not password or len(password) < 6:
        raise ValueError("Senha precisa ter pelo menos 6 caracteres")
    user = {
        "email": email,
        "role": role,
        "password": hash_password(password),
        "created_at": now_iso(),
        "last_seen": None,
        "invited_by": invited_by,
    }
    users.append(user)
    save_users(users)
    return user


def update_last_seen(email: str) -> None:
    users = load_users()
    changed = False
    for u in users:
        if u["email"].lower() == email.lower():
            u["last_seen"] = now_iso()
            changed = True
            break
    if changed:
        save_users(users)


def delete_user(email: str) -> bool:
    users = load_users()
    new = [u for u in users if u["email"].lower() != email.lower()]
    if len(new) == len(users):
        return False
    save_users(new)
    return True


def change_password(email: str, new_password: str) -> bool:
    users = load_users()
    for u in users:
        if u["email"].lower() == email.lower():
            u["password"] = hash_password(new_password)
            save_users(users)
            return True
    return False


def ensure_owner_seed() -> None:
    """Cria o owner default se não houver nenhum owner cadastrado.
    Senha vem de INSTAPOST_OWNER_PASSWORD env var. Se não definida,
    gera uma aleatória e imprime no console (única vez)."""
    users = load_users()
    if any(u.get("role") == "owner" for u in users):
        return
    password = OWNER_DEFAULT_PASSWORD
    generated = False
    if not password:
        password = secrets.token_urlsafe(16)
        generated = True
    try:
        create_user(OWNER_DEFAULT_EMAIL, password, role="owner")
        if generated:
            print(f"\n{'='*60}")
            print(f"  OWNER CRIADO — guarde estas credenciais!")
            print(f"  Email:  {OWNER_DEFAULT_EMAIL}")
            print(f"  Senha:  {password}")
            print(f"  (defina INSTAPOST_OWNER_EMAIL e INSTAPOST_OWNER_PASSWORD")
            print(f"   como variáveis de ambiente para evitar senhas aleatórias)")
            print(f"{'='*60}\n")
    except ValueError:
        for u in users:
            if u["email"].lower() == OWNER_DEFAULT_EMAIL.lower():
                u["role"] = "owner"
        save_users(users)


def public_user(u: dict) -> dict:
    return {
        "email": u["email"],
        "role": u.get("role", "member"),
        "created_at": u.get("created_at"),
        "last_seen": u.get("last_seen"),
        "invited_by": u.get("invited_by"),
    }


# ----- invites -----

def load_invites() -> list[dict]:
    if not INVITES_FILE.exists():
        return []
    try:
        return json.loads(INVITES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_invites(invites: list[dict]) -> None:
    INVITES_FILE.write_text(json.dumps(invites, ensure_ascii=False, indent=2), encoding="utf-8")


def create_invite(created_by_email: str, role: str = "member") -> dict:
    invites = load_invites()
    inv = {
        "token": secrets.token_urlsafe(24),
        "role": role,
        "created_by": created_by_email,
        "created_at": now_iso(),
        "used_at": None,
        "used_by": None,
    }
    invites.append(inv)
    save_invites(invites)
    return inv


def find_invite(token: str) -> Optional[dict]:
    for inv in load_invites():
        if inv["token"] == token:
            return inv
    return None


def revoke_invite(token: str) -> bool:
    invites = load_invites()
    new = [i for i in invites if i["token"] != token]
    if len(new) == len(invites):
        return False
    save_invites(new)
    return True


def consume_invite(token: str, email: str) -> dict:
    invites = load_invites()
    target = None
    for inv in invites:
        if inv["token"] == token:
            target = inv
            break
    if not target:
        raise HTTPException(404, "Convite inválido ou expirado")
    if target.get("used_at"):
        raise HTTPException(400, "Esse convite já foi usado")
    target["used_at"] = now_iso()
    target["used_by"] = email.lower()
    save_invites(invites)
    return target


# ----- session dependencies -----

def current_user(request: Request) -> Optional[dict]:
    email = request.session.get("email")
    if not email:
        return None
    u = find_user(email)
    if u:
        update_last_seen(email)
    return u


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(401, "Login necessário")
    return u


def require_owner(request: Request) -> dict:
    u = require_user(request)
    if u.get("role") != "owner":
        raise HTTPException(403, "Apenas o owner pode fazer isso")
    return u


# ----- extension tokens (auth alternativa pra extensão Chrome) -----

def find_user_by_extension_token(token: str) -> Optional[dict]:
    """Acha user pelo extension_token. Constant-time check pra evitar timing attack."""
    if not token:
        return None
    for u in load_users():
        ut = u.get("extension_token")
        if ut and secrets.compare_digest(ut, token):
            return u
    return None


def rotate_extension_token(email: str) -> str:
    """Gera novo token pro user, sobrescrevendo qualquer antigo. Retorna o token novo."""
    users = load_users()
    new_token = secrets.token_urlsafe(32)
    found = False
    for u in users:
        if u["email"].lower() == email.lower():
            u["extension_token"] = new_token
            u["extension_token_at"] = now_iso()
            found = True
            break
    if not found:
        raise HTTPException(404, "Usuário não encontrado")
    save_users(users)
    return new_token


def revoke_extension_token(email: str) -> bool:
    """Remove o token (logs out extensão). Retorna True se algo mudou."""
    users = load_users()
    changed = False
    for u in users:
        if u["email"].lower() == email.lower():
            if u.get("extension_token"):
                u.pop("extension_token", None)
                u.pop("extension_token_at", None)
                changed = True
            break
    if changed:
        save_users(users)
    return changed


def require_user_or_extension(request: Request) -> dict:
    """Aceita sessão (cookie) OU Authorization: Bearer <extension_token>.
    Usado em endpoints que a extensão consome — extensão não tem cookie de sessão."""
    # Tenta sessão primeiro (caso usuário esteja no painel mesmo)
    u = current_user(request)
    if u:
        return u
    # Tenta Bearer token
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        u = find_user_by_extension_token(token)
        if u:
            return u
    raise HTTPException(401, "Login ou extension token necessário")
