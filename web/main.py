"""
Web UI do Insta Poster — FastAPI.

Roda em http://localhost:8000 (e também responde no IP local da máquina,
útil pra abrir do celular na mesma rede).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

# Garante que o root do projeto está no sys.path pra importar core/
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.jobs import manager as job_manager  # noqa: E402
from web import auth  # noqa: E402
from web import scheduler as scheduler_mod  # noqa: E402
from web.shortener import manager as link_manager  # noqa: E402
from web.workers import manager as worker_manager  # noqa: E402
from web.remote_jobs import manager as rjob_manager  # noqa: E402
from web.finance import manager as finance_manager, CATEGORIES as FINANCE_CATEGORIES  # noqa: E402
from core.media import generate_thumbnail  # noqa: E402
from core.paths import (  # noqa: E402
    ACCOUNTS_FILE, PENDING_DIR, POSTED_DIR, SESSIONS_DIR, LOGS_DIR, data_path,
)

# Inicia thread do scheduler
schedule_manager = scheduler_mod.init(job_manager)

ACCOUNTS_EXAMPLE = ROOT / "accounts.example.json"
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
# Demais paths (ACCOUNTS_FILE, PENDING_DIR, etc) vêm de core/paths.py

app = FastAPI(title="Insta Poster", version="0.1.0")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Rotas que dispensam login do PAINEL (login, signup, estáticos, health, redirect curto, API do worker)
# API do worker tem auth própria via header X-Worker-Token (em vez de cookie de sessão)
# API da extensão tem auth via Authorization: Bearer <extension_token>
PUBLIC_PATH_PREFIXES = ("/login", "/signup/", "/static/", "/api/health", "/r/", "/api/worker/", "/api/sessions/")


class RequireLoginMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Workspace ativo: setta no contextvar a partir da sessão (default se faltar)
        from core import paths as _paths
        ws_slug = (request.session.get("workspace") or _paths.DEFAULT_WORKSPACE)
        _paths.set_workspace(ws_slug)

        if path == "/api/health" or any(path.startswith(p) for p in PUBLIC_PATH_PREFIXES):
            return await call_next(request)
        email = request.session.get("email")
        if not email or not auth.find_user(email):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Login necessário"}, status_code=401)
            return RedirectResponse(f"/login?next={path}", status_code=303)
        auth.update_last_seen(email)
        return await call_next(request)


# Middlewares são executados na ordem reversa do add_middleware:
# o último adicionado é o mais externo. Como nosso middleware precisa
# de request.session, SessionMiddleware deve ser adicionado DEPOIS.
app.add_middleware(RequireLoginMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.get_or_create_secret(),
    session_cookie="ip_session",
    max_age=60 * 60 * 24 * 30,  # 30 dias
    same_site="lax",
    # HTTPS-only só em prod (atrás de reverse proxy com TLS)
    https_only=os.environ.get("HTTPS_ONLY", "").lower() in ("1", "true", "yes"),
)

# CORS pra extensão Chrome: aceita qualquer origem chrome-extension://*
# (extensão é instalada com ID aleatório, não dá pra whitelistar pré-fixo)
from fastapi.middleware.cors import CORSMiddleware as _CORS
app.add_middleware(
    _CORS,
    allow_origin_regex=r"^chrome-extension://.+$",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=False,  # extensão usa Bearer token, não cookies
)
auth.ensure_owner_seed()


def _ctx(request: Request, **extra) -> dict:
    u = auth.current_user(request)
    from core import paths as _paths
    from web.workspaces import manager as ws_manager
    current_ws_slug = request.session.get("workspace") or _paths.DEFAULT_WORKSPACE
    current_ws = ws_manager.get(current_ws_slug) or ws_manager.get(_paths.DEFAULT_WORKSPACE)
    base = {
        "user": auth.public_user(u) if u else None,
        "is_owner": bool(u and u.get("role") == "owner"),
        "current_workspace": current_ws.to_dict() if current_ws else None,
        "all_workspaces": ws_manager.list(),
    }
    base.update(extra)
    return base


# ---------- helpers ----------

VALID_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

# Lock global pro accounts.json. RLock pra suportar nested load/save dentro de
# um mesmo bloco transactional. Sem isso, 2 requests (UI + worker_result + scheduler)
# faziam read-modify-write concorrente e a 2ª escrita sobrescrevia a 1ª silenciosamente.
import threading as _t
_accounts_lock = _t.RLock()


def _normalize_proxy(raw: str) -> str:
    """Converte formatos comuns de proxy pra URL padrão que requests/instagrapi entendem.

    Aceita (com ou sem scheme prefix):
      - http://user:pass@host:port           (já no formato URL — devolve igual)
      - socks5://user:pass@host:port         (idem)
      - http://host:port:user:pass           (alguém colou DataImpulse + http://)
      - host:port:user:pass                  (DataImpulse, Bright Data raw)
      - user:pass@host:port                  (sem scheme — vira http://)
      - host:port                            (sem auth — vira http://)
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        scheme = scheme.lower()
        if scheme not in ("http", "https", "socks4", "socks5", "socks5h"):
            scheme = "http"
    else:
        scheme = "http"
        rest = raw
    if "@" in rest:
        return f"{scheme}://{rest}"
    parts = rest.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"{scheme}://{user}:{password}@{host}:{port}"
    if len(parts) == 2:
        return f"{scheme}://{rest}"
    return raw


def load_accounts() -> list[dict]:
    """Carrega contas com decrypt automático de senha/totp_secret + normalização
    de proxy. Migração transparente: detecta plaintext, devolve como está
    (próximo save vai encriptar).

    Garante que TODOS os consumers (test-proxy, worker dispatch, login, scheduler)
    vejam senha em PLAINTEXT na memória e proxy normalizado.
    """
    with _accounts_lock:
        if not ACCOUNTS_FILE.exists():
            return []
        try:
            accs = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
        # Decrypt password + totp_secret + normaliza proxy
        from core.credentials import decrypt as _decrypt
        for a in accs:
            pwd = a.get("password")
            if pwd:
                decrypted = _decrypt(pwd)
                if decrypted is not None:
                    a["password"] = decrypted
            secret = a.get("totp_secret")
            if secret:
                decrypted = _decrypt(secret)
                if decrypted is not None:
                    a["totp_secret"] = decrypted
            raw_proxy = a.get("proxy")
            if raw_proxy:
                normalized = _normalize_proxy(raw_proxy)
                if normalized and normalized != raw_proxy:
                    a["proxy"] = normalized
        return accs


def save_accounts(accounts: list[dict]) -> None:
    """Atomic write: escreve em .tmp e dá rename. Encripta senha + totp_secret
    ANTES de escrever (em disco fica encrypted, em memória continua plaintext)."""
    with _accounts_lock:
        from core.credentials import encrypt as _encrypt
        # Deep copy parcial — só os campos sensíveis. Não muta o dict in-memory.
        to_persist = []
        for a in accounts:
            copy = dict(a)
            if copy.get("password"):
                copy["password"] = _encrypt(copy["password"])
            if copy.get("totp_secret"):
                copy["totp_secret"] = _encrypt(copy["totp_secret"])
            to_persist.append(copy)
        path = Path(str(ACCOUNTS_FILE))
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(to_persist, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)


from contextlib import contextmanager as _contextmanager

@_contextmanager
def accounts_transaction():
    """Read-modify-write atômico do accounts.json.

    Use SEMPRE que precisar modificar accounts dentro de um endpoint ou thread,
    em vez de chamar load_accounts() + ... + save_accounts() separadamente.

    Exemplo:
        with accounts_transaction() as accs:
            for a in accs:
                if a["username"] == target:
                    a["foo"] = "bar"
                    break

    O save é automático no exit. Lock segura outros writers durante o bloco.
    """
    with _accounts_lock:
        accs = load_accounts()
        yield accs
        save_accounts(accs)


def session_status(username: str) -> str:
    return "saved" if (SESSIONS_DIR / f"{username}.json").exists() else "missing"


MEDIA_EXTS = {".mp4", ".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _is_video_thumb(p: Path) -> bool:
    """Detecta se um .jpg/.jpeg é thumb de vídeo (não foto real).
    Cobre 2 padrões: 'video.jpg' (sibling de video.mp4) e 'video.mp4.jpg' (legacy)."""
    suffix = p.suffix.lower()
    if suffix not in (".jpg", ".jpeg"):
        return False
    # Pattern 1: name termina em .mp4.<ext> → claramente thumb auto-gerada
    if p.stem.lower().endswith(".mp4"):
        return True
    # Pattern 2: existe um irmão .mp4 com mesmo stem
    if p.with_suffix(".mp4").exists():
        return True
    return False


# Cache em memória pra list_videos (invalidado por mtime do dir ou explicitamente)
_videos_cache: dict[str, tuple[float, list[dict]]] = {}  # key -> (dir_mtime, result)
_VIDEOS_CACHE_TTL = 4.0  # seg — auto-expira mesmo sem mudança detectada


def _videos_cache_invalidate():
    """Limpa cache (chame após upload/delete)."""
    _videos_cache.clear()


def list_videos(folder: Path) -> list[dict]:
    """Lista todas as mídias da pasta (vídeo + foto).

    Otimização: cacheia resultado em memória por workspace+folder. Invalida
    automaticamente se o mtime do diretório mudou (criação/remoção de arquivo)
    OU após TTL curto.
    """
    from core.poster import load_meta, detect_media_kind
    folder_key = folder.name  # "pending" ou "posted"
    # Cache key inclui o path absoluto (workspace-aware)
    cache_key = str(folder)
    try:
        dir_mtime = folder.stat().st_mtime
    except OSError:
        dir_mtime = 0.0
    now = time.time()
    cached = _videos_cache.get(cache_key)
    if cached:
        cached_mtime, cached_result = cached
        # Hit válido se dir não mudou E não passou do TTL
        if cached_mtime == dir_mtime and (now - cached_mtime) < _VIDEOS_CACHE_TTL + 60:
            return cached_result
    out = []
    items = [p for p in folder.iterdir()
             if p.is_file()
             and p.suffix.lower() in MEDIA_EXTS
             and not p.name.endswith(".meta.json")
             and not _is_video_thumb(p)]

    for media in sorted(items, key=lambda p: p.stat().st_mtime, reverse=True):
        is_photo = media.suffix.lower() in PHOTO_EXTS
        txt = media.with_suffix(".txt")
        # Thumb: pra vídeo é .jpg; pra foto é o próprio arquivo
        if is_photo:
            thumb_exists = True
            thumb_url = f"/api/videos/{folder_key}/{media.name}/stream"  # foto já é a thumb
        else:
            thumb = media.with_suffix(".jpg")
            thumb_exists = thumb.exists()
            thumb_url = f"/api/videos/{folder_key}/{media.name}/thumb"

        caption = ""
        if txt.exists():
            try:
                caption = txt.read_text(encoding="utf-8").strip()
            except Exception:
                caption = "<erro lendo legenda>"

        meta = load_meta(str(media))
        stat = media.stat()
        out.append({
            "name": media.name,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "caption": caption,
            "has_caption": txt.exists(),
            "has_thumb": thumb_exists,
            "thumb_url": thumb_url,
            "stream_url": f"/api/videos/{folder_key}/{media.name}/stream",
            "folder": folder_key,
            "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "media_type": "photo" if is_photo else "video",
            "kind": meta.get("kind", "story" if is_photo else "reel"),
            "link_url": meta.get("link_url"),
            "link_text": meta.get("link_text") or "Clique aqui",
        })
    # Cacheia resultado (workspace-aware via cache_key = path absoluto)
    _videos_cache[cache_key] = (dir_mtime, out)
    return out


def safe_name(name: str) -> str:
    """Normaliza o nome do arquivo: remove acentos, troca caracteres especiais por _.
    Aceita praticamente qualquer nome de arquivo do mundo real (espaço, parênteses,
    acentos), só bloqueia path traversal."""
    base = Path(name).name
    if not base or base in (".", ".."):
        raise HTTPException(400, "Nome de arquivo inválido")
    # Normaliza acentos (NFKD separa o caractere do diacrítico, depois removemos)
    base = unicodedata.normalize("NFKD", base)
    base = base.encode("ascii", "ignore").decode("ascii")
    # Substitui qualquer coisa que não seja alphanum/ponto/underscore/hífen por _
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    # Colapsa underscores múltiplos
    base = re.sub(r"_+", "_", base).strip("_")
    if not base:
        raise HTTPException(400, "Nome ficou vazio após sanitização")
    return base


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ---------- onboarding ----------

def _compute_onboarding_status(request: Request, user: dict) -> dict:
    """Detecta progresso do onboarding lendo estado do sistema.
    Cada step retorna {done: bool, hint: str, value: int|str opcional}.
    """
    from core import paths as _paths
    from web.workers import manager as worker_manager
    from web.remote_jobs import manager as rjob_manager

    # Step 1: pelo menos 1 conta cadastrada
    accounts = load_accounts()
    active = [a for a in accounts if a.get("active", True)]

    # Step 2: worker VPS online (qualquer worker online conta)
    workers = worker_manager.list(hide_token=True)
    online_workers = [w for w in workers if w.get("online")]

    # Step 3: token de extensão gerado
    u = auth.find_user(user["email"]) if user else None
    has_ext_token = bool(u and u.get("extension_token"))

    # Step 4: pelo menos 1 conta com session manual salva (cookies)
    sessions_with_cookies = 0
    ws_slug = _paths.get_workspace()
    sessions_path = _paths.sessions_dir(ws_slug)
    for a in active:
        sf = sessions_path / f"{a['username']}.json"
        if sf.exists():
            try:
                jd = json.loads(sf.read_text(encoding="utf-8"))
                if jd.get("manually_saved") or jd.get("from_chrome") or jd.get("from_extension"):
                    sessions_with_cookies += 1
            except Exception:
                pass

    # Step 5: pelo menos 1 mídia na biblioteca
    pending_count = 0
    if PENDING_DIR.exists():
        for p in PENDING_DIR.iterdir():
            if p.is_file() and p.suffix.lower() in MEDIA_EXTS and not p.name.endswith(".meta.json"):
                pending_count += 1

    # Step 6: pelo menos 1 post bem-sucedido
    has_posted = any(
        j.operation == "post" and j.status == "done" and j.workspace_slug == ws_slug
        for j in rjob_manager.snapshot_values()
    )

    steps = [
        {
            "key": "account",
            "title": "Cadastre sua primeira conta Insta",
            "done": len(active) > 0,
            "hint": f"{len(active)} conta(s) ativa(s)" if active else "Vá em Contas → Nova conta",
            "link": "/accounts",
        },
        {
            "key": "worker",
            "title": "Conecte o worker (VPS ou local)",
            "done": len(online_workers) > 0,
            "hint": f"{len(online_workers)} worker(s) online" if online_workers else "Cadastre em Meu PC e suba o worker",
            "link": "/workers",
        },
        {
            "key": "ext_token",
            "title": "Gere o token de extensão",
            "done": has_ext_token,
            "hint": "Token gerado" if has_ext_token else "Vá em Contas → Extensão → Gerar token",
            "link": "/accounts",
        },
        {
            "key": "save_session",
            "title": "Salve cookies de pelo menos 1 conta",
            "done": sessions_with_cookies > 0,
            "hint": f"{sessions_with_cookies} conta(s) com cookies" if sessions_with_cookies else "Use extensão Chrome OU botão Smartphone no painel",
            "link": "/accounts",
        },
        {
            "key": "library",
            "title": "Suba sua primeira mídia",
            "done": pending_count > 0,
            "hint": f"{pending_count} mídia(s) na biblioteca" if pending_count else "Vá em Biblioteca → Subir mídia",
            "link": "/videos",
        },
        {
            "key": "first_post",
            "title": "Faça seu primeiro post",
            "done": has_posted,
            "hint": "Pelo menos 1 post bem-sucedido" if has_posted else "Use Disparos → Disparar manual OU agende",
            "link": "/jobs",
        },
    ]

    done_count = sum(1 for s in steps if s["done"])
    return {
        "steps": steps,
        "done_count": done_count,
        "total": len(steps),
        "percent": int(done_count * 100 / max(1, len(steps))),
        "completed": done_count == len(steps),
    }


@app.get("/api/onboarding/status")
def api_onboarding_status(request: Request, user=Depends(auth.require_user)):
    return _compute_onboarding_status(request, user)


@app.get("/onboarding", response_class=HTMLResponse)
def page_onboarding(request: Request, user=Depends(auth.require_user)):
    status = _compute_onboarding_status(request, user)
    return templates.TemplateResponse(
        request,
        "onboarding.html",
        _ctx(request, active="onboarding", onboarding=status),
    )


# ---------- pages ----------

@app.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request):
    accounts = load_accounts()
    pending = list_videos(PENDING_DIR)
    posted = list_videos(POSTED_DIR)
    jobs = job_manager.list()[:10]

    active_accounts = [a for a in accounts if a.get("active", True)]
    # Conta como "conectada" se TEM sessão local NO SERVIDOR ou se foi conectada via worker
    # (worker salva sessão no PC do usuário, não no servidor)
    connected_accounts = [
        a for a in active_accounts
        if session_status(a["username"]) == "saved" or a.get("connected_via_worker_id")
    ]

    # Próximos agendamentos (status pending) — só do workspace ativo
    from core import paths as _paths
    all_schedules = schedule_manager.list(workspace_slug=_paths.get_workspace())
    upcoming_schedules = [s for s in all_schedules if s["status"] == "pending"][:5]
    upcoming_count = sum(1 for s in all_schedules if s["status"] == "pending")

    # Onboarding status (mostra banner no topo se < 100%)
    user_obj = auth.current_user(request)
    onboarding_status = _compute_onboarding_status(request, user_obj) if user_obj else None

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(
            request,
            active="dashboard",
            accounts=accounts,
            active_accounts=active_accounts,
            connected_accounts=connected_accounts,
            pending=pending,
            posted=posted,
            jobs=jobs,
            upcoming_schedules=upcoming_schedules,
            upcoming_count=upcoming_count,
            host_ip=local_ip(),
            onboarding=onboarding_status,
        ),
    )


@app.get("/accounts", response_class=HTMLResponse)
def page_accounts(request: Request):
    accounts = load_accounts()
    for a in accounts:
        a["session"] = session_status(a["username"])
    return templates.TemplateResponse(
        request, "accounts.html", _ctx(request, active="accounts", accounts=accounts),
    )


@app.get("/videos", response_class=HTMLResponse)
def page_videos(request: Request):
    return templates.TemplateResponse(
        request,
        "videos.html",
        _ctx(
            request,
            active="videos",
            pending=list_videos(PENDING_DIR),
            posted=list_videos(POSTED_DIR),
        ),
    )


@app.get("/jobs", response_class=HTMLResponse)
def page_jobs(request: Request):
    accounts = load_accounts()
    pending = list_videos(PENDING_DIR)
    # Só contas ativas E com sessão (servidor OU worker) entram no disparo manual via /jobs
    connected = [
        a for a in accounts
        if a.get("active", True)
        and (session_status(a["username"]) == "saved" or a.get("connected_via_worker_id"))
    ]
    total_active = sum(1 for a in accounts if a.get("active", True))
    return templates.TemplateResponse(
        request,
        "jobs.html",
        _ctx(
            request,
            active="jobs",
            jobs=job_manager.list(),
            accounts=connected,
            total_active=total_active,
            videos=pending,
        ),
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def page_job_detail(request: Request, job_id: str):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado")
    return templates.TemplateResponse(
        request, "job_detail.html", _ctx(request, active="jobs", job=job.to_dict()),
    )


@app.get("/schedule", response_class=HTMLResponse)
def page_schedule(request: Request):
    accounts = load_accounts()
    pending_videos = list_videos(PENDING_DIR)
    # FIX: conectada = sessão NO servidor OU worker_id (default do user é worker)
    connected = [
        a["username"] for a in accounts
        if a.get("active", True)
        and (session_status(a["username"]) == "saved" or a.get("connected_via_worker_id"))
    ]
    return templates.TemplateResponse(
        request,
        "schedule.html",
        _ctx(
            request,
            active="schedule",
            connected_accounts=connected,
            pending_videos=pending_videos,
        ),
    )


@app.get("/logs", response_class=HTMLResponse)
def page_logs(request: Request, date: Optional[str] = None):
    files = sorted([p.name for p in LOGS_DIR.glob("*.log")], reverse=True)
    target = date or (files[0] if files else None)
    content = ""
    if target:
        log_path = LOGS_DIR / target
        if log_path.exists():
            try:
                content = log_path.read_text(encoding="utf-8")
            except Exception as e:
                content = f"Erro lendo log: {e}"
    return templates.TemplateResponse(
        request,
        "logs.html",
        _ctx(
            request,
            active="logs",
            files=files,
            selected=target,
            content=content,
        ),
    )


# ---------- API: accounts ----------

class AccountIn(BaseModel):
    username: str
    password: str
    proxy: Optional[str] = None
    active: bool = True
    totp_secret: Optional[str] = None


class BulkUsernames(BaseModel):
    usernames: list[str]


# Threshold padrão pra considerar conta "nova" (em aquecimento)
NEW_ACCOUNT_THRESHOLD_HOURS = 24


def _hours_since_first_post(a: dict) -> Optional[float]:
    """Horas desde a 1ª postagem da conta. None se nunca postou."""
    posted = a.get("posted_media") or []
    if not posted:
        return None
    try:
        from datetime import datetime as _dt
        first_iso = min(p.get("posted_at", "") for p in posted if p.get("posted_at"))
        if not first_iso:
            return None
        first_dt = _dt.fromisoformat(first_iso)
        if first_dt.tzinfo is None:
            first_dt = first_dt.astimezone()
        now_dt = _dt.now(first_dt.tzinfo)
        return round((now_dt - first_dt).total_seconds() / 3600.0, 2)
    except Exception:
        return None


def _is_account_warming_up(a: dict) -> bool:
    """Conta está em aquecimento (modo conservador) se:
    - skip_warmup NÃO está ativo (override manual do usuário) E
    - Nunca postou (posted_media vazio) OU
    - 1ª postagem foi há menos de NEW_ACCOUNT_THRESHOLD_HOURS horas.
    """
    if a.get("skip_warmup", False):
        return False  # usuário marcou: pular aquecimento, postar normal já
    hours = _hours_since_first_post(a)
    if hours is None:
        return True  # nunca postou = muito nova
    return hours < NEW_ACCOUNT_THRESHOLD_HOURS


def _account_view(a: dict) -> dict:
    return {
        "username": a["username"],
        "active": a.get("active", True),
        "proxy": a.get("proxy"),
        "session": session_status(a["username"]),
        "has_totp": bool(a.get("totp_secret")),
        "connected_via_worker_id": a.get("connected_via_worker_id"),
        "connected_via_worker_name": a.get("connected_via_worker_name"),
        "connected_at": a.get("connected_at"),
        # Automações
        "auto_like_enabled": bool(a.get("auto_like_enabled", False)),
        "auto_like_max_per_day": int(a.get("auto_like_max_per_day", 40)),
        "auto_like_today_count": int(a.get("auto_like_today_count", 0)),
        "auto_follow_back_enabled": bool(a.get("auto_follow_back_enabled", False)),
        "auto_follow_back_max_per_day": int(a.get("auto_follow_back_max_per_day", 10)),
        "auto_follow_back_today_count": int(a.get("auto_follow_back_today_count", 0)),
        # Destaques automáticos
        "auto_highlight_enabled": bool(a.get("auto_highlight_enabled", False)),
        "auto_highlight_title": a.get("auto_highlight_title", ""),
        # Sync com feed central
        "sync_enabled": bool(a.get("sync_enabled", False)),
        "sync_interval_hours": int(a.get("sync_interval_hours", 8)),
        "sync_last_post_at": a.get("sync_last_post_at"),
        "sync_completed": bool(a.get("sync_completed", False)),
        "posted_media_count": len(a.get("posted_media", []) or []),
        # Aquecimento (conta nova) — calculado a partir de posted_media
        "is_warming_up": _is_account_warming_up(a),
        "hours_since_first_post": _hours_since_first_post(a),
        "skip_warmup": bool(a.get("skip_warmup", False)),
        # Bloqueio detectado (rate limit / disabled — sério)
        "blocked": bool(a.get("blocked", False)),
        "blocked_at": a.get("blocked_at"),
        "blocked_reason": a.get("blocked_reason"),
        # Verificação requerida (challenge/checkpoint — resolúvel manual no app)
        "needs_verification": bool(a.get("needs_verification", False)),
        "verification_at": a.get("verification_at"),
        "verification_reason": a.get("verification_reason"),
        # Cookies expiraram — só refazer Save Sessão via extensão/Smartphone
        "needs_session_renewal": bool(a.get("needs_session_renewal", False)),
        "session_renewal_at": a.get("session_renewal_at"),
        "session_renewal_reason": a.get("session_renewal_reason"),
        # Pausa do worker (quando user tá usando a conta manualmente no browser)
        "worker_paused_until": a.get("worker_paused_until"),
        # Health / shadow ban detector
        "shadowban_suspected": bool(a.get("shadowban_suspected", False)),
        "shadowban_at": a.get("shadowban_at"),
        "shadowban_reason": a.get("shadowban_reason"),
        "health_score": int(a.get("health_score", 50)),
        "follower_count": int(a.get("follower_count", 0)),
    }


# ---------- DETECÇÃO DE BLOQUEIO ----------

# Erros que indicam "precisa verificação manual" (Instagram pediu challenge,
# checkpoint, ou similar). Conta volta a funcionar depois que o user resolve no app.
# RESOLÚVEIS pelo user.
CHALLENGE_PATTERNS = (
    "challenge_required", "challengeresolve",
    "checkpoint_required", "checkpoint",
    "podemos enviar um email",                   # IG pt-BR
    "we can send you an email",                  # IG en
    "two-factor", "two_factor",
    "verify your account", "verifique sua conta",
    "suspicious login", "tentativa de login suspeita",
    "unusual activity", "atividade incomum",
    "step_name",                                 # bloks challenge resolver
    "não foi possível encontrar uma conta",      # IG mente quando IP flagrado
)

# Erros que indicam bloqueio MAIS SÉRIO (rate limit, conta desabilitada, IP banido).
# Diferente de challenge: aqui não tem ação clara pro user resolver na hora.
BLOCK_PATTERNS = (
    "feedback_required",
    "login_required",
    "please_wait", "please wait",
    "try_again_later", "try again later",
    "account_disabled", "account disabled",
    "user_has_logged_out",
    "instagram bloqueou",  # nossa string custom em core/profile.py
)

# Erros que indicam "cookies expiraram, refaça Save Sessão" — NÃO é ban, NÃO é
# challenge. Conta volta a funcionar instantâneo quando user atualiza os cookies
# via extensão Chrome OU botão Smartphone. Estado mais leve dos 3.
SESSION_RENEWAL_PATTERNS = (
    "manualreconnectneeded",                  # nome da exception class
    "sem sessão válida",                      # msg do raise em session.py
    "sessão manual de @",                     # msg do raise em session.py (sessão manual expirou)
    "fresh login api desativado",             # msg explicativa
    "salvar sessão",                          # genérico — captura instruções de Save Sessão
    "refaça login no chrome",                 # variante da mesma msg
)


def _is_challenge_error(error_msg: Optional[str]) -> Optional[str]:
    """Retorna o padrão casado se for erro de challenge/verificação, senão None.
    Tem prioridade sobre _is_block_error — challenge é o estado mais específico."""
    if not error_msg:
        return None
    low = error_msg.lower()
    for pat in CHALLENGE_PATTERNS:
        if pat in low:
            return pat
    return None


def _is_block_error(error_msg: Optional[str]) -> Optional[str]:
    """Retorna o padrão casado se for erro de bloqueio (rate/disabled), senão None."""
    if not error_msg:
        return None
    low = error_msg.lower()
    for pat in BLOCK_PATTERNS:
        if pat in low:
            return pat
    return None


def _is_session_renewal_error(error_msg: Optional[str]) -> Optional[str]:
    """Retorna padrão casado se for erro de cookies expirados / Save Sessão necessário.
    Mais leve que block/challenge — só precisa renovar cookies, conta tá OK."""
    if not error_msg:
        return None
    low = error_msg.lower()
    for pat in SESSION_RENEWAL_PATTERNS:
        if pat in low:
            return pat
    return None


@app.get("/api/accounts")
def api_list_accounts():
    return [_account_view(a) for a in load_accounts()]


@app.post("/api/accounts")
def api_add_account(payload: AccountIn):
    # Normaliza username pra lowercase (Instagram é case-insensitive)
    username = payload.username.strip().lower()
    if not username or not payload.password:
        raise HTTPException(400, "username e password obrigatórios")

    accounts = load_accounts()
    if any(a["username"].lower() == username for a in accounts):
        raise HTTPException(409, f"Conta @{username} já existe")

    accounts.append({
        "username": username,
        "password": payload.password,
        "proxy": (payload.proxy or "").strip() or None,
        "active": payload.active,
        "totp_secret": (payload.totp_secret or "").strip() or None,
    })
    save_accounts(accounts)
    return {"ok": True, "account": _account_view(accounts[-1])}


class TotpIn(BaseModel):
    totp_secret: Optional[str] = None


@app.post("/api/accounts/{username}/totp")
def api_update_totp(username: str, payload: TotpIn):
    with accounts_transaction() as accounts:
        for a in accounts:
            if a["username"] == username:
                a["totp_secret"] = (payload.totp_secret or "").strip() or None
                return {"ok": True, "has_totp": bool(a["totp_secret"])}
    raise HTTPException(404, "Conta não encontrada")


class ProxyIn(BaseModel):
    proxy: Optional[str] = None


# _normalize_proxy() definido em cima junto com load_accounts (precisa estar
# disponível pra normalização defensiva on-read).


@app.post("/api/accounts/{username}/test-proxy")
def api_test_proxy(username: str):
    """Testa o proxy salvo pra essa conta:
    1. Faz request pra ipify via o proxy → confirma egress
    2. Mede latência
    3. Compara com seu IP "real" (sem proxy)

    Retorna: ip_via_proxy, ip_sem_proxy, latency_ms, ok
    """
    import time as _t
    import requests as _rq

    # Acha a conta + proxy
    accounts = load_accounts()
    proxy = None
    found = False
    for a in accounts:
        if a["username"] == username:
            found = True
            proxy = a.get("proxy")
            break
    if not found:
        raise HTTPException(404, "Conta não encontrada")
    if not proxy:
        raise HTTPException(400, "Essa conta não tem proxy configurado")

    # DEFENSIVO: normaliza + aplica sticky session por conta na hora de testar.
    # Assim o user vê o IP REAL que aquela conta usaria pra postar (com sticky),
    # não o IP genérico rotativo.
    proxy_raw = proxy
    proxy = _normalize_proxy(proxy_raw)
    try:
        from core.proxy_sticky import make_sticky
        sticky = make_sticky(proxy, username)
    except Exception:
        sticky = proxy
    proxy = sticky or proxy

    proxies = {"http": proxy, "https": proxy}
    result = {"ok": False, "proxy_saved": proxy_raw, "proxy_used": proxy, "sticky_applied": (proxy != proxy_raw)}

    # 1) IP sem proxy (pra comparar)
    try:
        r0 = _rq.get("https://api.ipify.org?format=json", timeout=5)
        result["ip_sem_proxy"] = r0.json().get("ip")
    except Exception as e:
        result["ip_sem_proxy"] = f"erro: {e}"

    # 2) IP via proxy
    t0 = _t.time()
    try:
        r = _rq.get(
            "https://api.ipify.org?format=json",
            proxies=proxies,
            timeout=10,
        )
        elapsed_ms = int((_t.time() - t0) * 1000)
        result["latency_ms"] = elapsed_ms
        if r.status_code == 200:
            result["ip_via_proxy"] = r.json().get("ip")
            result["ok"] = True
            # Compara: proxy realmente roteia se o IP for diferente
            if result["ip_via_proxy"] == result.get("ip_sem_proxy"):
                result["warning"] = "IP via proxy = IP sem proxy (proxy pode estar bypassando)"
        else:
            result["error"] = f"HTTP {r.status_code}"
    except _rq.exceptions.ProxyError as e:
        result["error"] = f"ProxyError: {str(e)[:200]}"
    except _rq.exceptions.ConnectionError as e:
        result["error"] = f"ConnectionError: {str(e)[:200]}"
    except _rq.exceptions.Timeout:
        result["error"] = "Timeout (proxy lento ou inacessível)"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"

    return result


@app.post("/api/accounts/{username}/proxy")
def api_update_proxy(username: str, payload: ProxyIn):
    """Atualiza proxy de uma conta. String vazia ou null remove o proxy.

    Auto-converte formatos comuns:
      socks5://user:pass@host:port  (já correto)
      http://user:pass@host:port    (já correto)
      host:port:user:pass           (DataImpulse, Bright Data) → vira http://user:pass@host:port
      user:pass@host:port           (sem scheme)              → vira http://
      host:port                     (sem auth)                → vira http://
    """
    raw = (payload.proxy or "").strip()
    new_proxy = _normalize_proxy(raw) or None
    # Validação final (após conversão)
    if new_proxy:
        if not re.match(r"^(socks5h?|socks4|http|https)://", new_proxy):
            raise HTTPException(400, f"Não consegui detectar formato do proxy. Recebi: '{raw}'. Use http://user:pass@host:porta ou socks5://...")
        if "@" in new_proxy:
            after_at = new_proxy.rsplit("@", 1)[1]
            if ":" not in after_at:
                raise HTTPException(400, "Faltou :porta no final do proxy")
        else:
            host_part = new_proxy.split("://", 1)[1]
            if ":" not in host_part:
                raise HTTPException(400, "Faltou :porta no final do proxy")
    with accounts_transaction() as accounts:
        for a in accounts:
            if a["username"] == username:
                a["proxy"] = new_proxy
                return {"ok": True, "proxy": new_proxy, "normalized_from": raw if new_proxy != raw else None}
    raise HTTPException(404, "Conta não encontrada")


class BulkProxyIn(BaseModel):
    proxy: str
    # Se setado, só substitui contas cujo proxy ATUAL contém essa string
    # (ex: "dataimpulse" ou "73eef5bb" pra trocar só DataImpulse). Vazio = todas.
    match: Optional[str] = None
    # Se True, devolve só o que mudaria sem salvar nada.
    dry_run: bool = False


@app.post("/api/accounts/bulk-set-proxy")
def api_bulk_set_proxy(payload: BulkProxyIn, user=Depends(auth.require_user)):
    """Aplica um proxy a TODAS as contas (ou só as que batem o filtro `match`).

    Use cases:
      - Migrar todo mundo do DataImpulse pro novo provedor: match="dataimpulse"
        (ou parte do user-prefix antigo, ex: "73eef5bb")
      - Aplicar mesmo proxy em todas as contas novas: match=null
    """
    raw = (payload.proxy or "").strip()
    if not raw:
        raise HTTPException(400, "proxy obrigatório")
    new_proxy = _normalize_proxy(raw)
    if not new_proxy or not re.match(r"^(socks5h?|socks4|http|https)://", new_proxy):
        raise HTTPException(400, f"Formato de proxy inválido. Recebi: '{raw}'")
    # Validação final (porta presente)
    if "@" in new_proxy:
        after_at = new_proxy.rsplit("@", 1)[1]
        if ":" not in after_at:
            raise HTTPException(400, "Faltou :porta no final do proxy")
    else:
        host_part = new_proxy.split("://", 1)[1]
        if ":" not in host_part:
            raise HTTPException(400, "Faltou :porta no final do proxy")

    match_filter = (payload.match or "").strip().lower() or None
    changes = []
    unchanged = 0
    skipped_no_match = 0

    if payload.dry_run:
        # Simulação: lê sem transação
        for a in load_accounts():
            current = (a.get("proxy") or "").strip()
            if match_filter and match_filter not in current.lower():
                skipped_no_match += 1
                continue
            if current == new_proxy:
                unchanged += 1
                continue
            changes.append({
                "username": a["username"],
                "from": current or None,
                "to": new_proxy,
            })
        return {
            "ok": True,
            "dry_run": True,
            "would_change": len(changes),
            "unchanged": unchanged,
            "skipped_no_match": skipped_no_match,
            "preview": changes[:20],
            "total_preview_truncated": len(changes) > 20,
        }

    # Aplicação real
    with accounts_transaction() as accounts:
        for a in accounts:
            current = (a.get("proxy") or "").strip()
            if match_filter and match_filter not in current.lower():
                skipped_no_match += 1
                continue
            if current == new_proxy:
                unchanged += 1
                continue
            a["proxy"] = new_proxy
            changes.append({
                "username": a["username"],
                "from": current or None,
                "to": new_proxy,
            })

    print(f"[bulk-proxy] aplicado em {len(changes)} contas (match={match_filter!r}) por {user.get('email','?')}")
    return {
        "ok": True,
        "dry_run": False,
        "changed": len(changes),
        "unchanged": unchanged,
        "skipped_no_match": skipped_no_match,
        "applied_proxy": new_proxy,
        "details": changes,
    }


# ============================================================================
# SESSIONS UPLOAD (extensão Chrome) — endpoint pra extensão enviar cookies
# ============================================================================

class ExtensionSessionIn(BaseModel):
    workspace_slug: Optional[str] = None  # se não enviar, usa ws ativo do user
    username: str                          # username IG
    cookies: dict                          # dict de cookies do instagram.com
    user_agent: Optional[str] = None       # do browser que capturou (debug)


@app.options("/api/sessions/upload")
def api_sessions_upload_preflight():
    """CORS preflight pra extensão — middleware CORS já trata, mas explícito é mais seguro."""
    return {"ok": True}


@app.post("/api/sessions/upload")
def api_sessions_upload(payload: ExtensionSessionIn, request: Request):
    """Recebe cookies da extensão Chrome e salva como session.json no workspace.

    Auth: Authorization: Bearer <extension_token> OU sessão de painel.
    Replica EXATAMENTE o formato que o worker.py produz em _save_session_from_chrome,
    pra session.py carregar como sessão MANUAL (manually_saved=True).
    """
    user = auth.require_user_or_extension(request)

    safe_username = "".join(c for c in (payload.username or "").lower() if c.isalnum() or c in "._-")
    if not safe_username:
        raise HTTPException(400, "username inválido")

    cookies = payload.cookies or {}
    sessionid = cookies.get("sessionid")
    ds_user_id = cookies.get("ds_user_id")
    if not sessionid or not ds_user_id:
        raise HTTPException(400, "Cookies obrigatórios faltando (sessionid + ds_user_id). Vc tá logado no instagram.com nesse browser?")

    # Resolve workspace: payload > sessão > default
    from core import paths as _paths
    ws_slug = (payload.workspace_slug or "").strip() or _paths.get_workspace() or "default"
    # Sanitize ws slug
    if not all(c.isalnum() or c in "-_" for c in ws_slug):
        raise HTTPException(400, "workspace_slug inválido")

    # Verifica que o workspace existe (cria se não — defensivo)
    sessions_dir_path = _paths.sessions_dir(ws_slug)

    # Garante que a conta exista no workspace
    accounts_file = _paths.accounts_file(ws_slug)
    if accounts_file.exists():
        try:
            accs = json.loads(accounts_file.read_text(encoding="utf-8"))
            if not any(a.get("username", "").lower() == safe_username for a in accs):
                raise HTTPException(404, f"Conta @{safe_username} não existe no workspace '{ws_slug}'. Cadastra ela no painel primeiro.")
        except HTTPException:
            raise
        except Exception:
            pass

    import time as _time
    import uuid as _uuid

    # session.json no formato esperado pelo instagrapi + flags manually_saved
    session_data = {
        "uuids": {
            "phone_id": str(_uuid.uuid4()),
            "uuid": str(_uuid.uuid4()),
            "client_session_id": str(_uuid.uuid4()),
            "advertising_id": str(_uuid.uuid4()),
            "android_device_id": str(_uuid.uuid4()),
            "request_id": str(_uuid.uuid4()),
            "tray_session_id": str(_uuid.uuid4()),
        },
        "mid": cookies.get("mid", ""),
        "ig_u_rur": cookies.get("rur"),
        "ig_www_claim": "",
        "authorization_data": {
            "ds_user_id": ds_user_id,
            "sessionid": sessionid,
        },
        "cookies": cookies,
        "last_login": _time.time(),
        # Device padrão pra montar headers do instagrapi (mesmo do worker.py)
        "device_settings": {
            "app_version": "428.0.0.47.67",
            "android_version": 34,
            "android_release": "14",
            "dpi": "480dpi",
            "resolution": "1344x2992",
            "manufacturer": "Google/google",
            "device": "husky",
            "model": "Pixel 8 Pro",
            "cpu": "husky",
            "version_code": "961145276",
        },
        "user_agent": "Instagram 428.0.0.47.67 Android (34/14; 480dpi; 1344x2992; Google/google; Pixel 8 Pro; husky; husky; pt_BR; 961145276)",
        "country": "BR",
        "country_code": 55,
        "locale": "pt_BR",
        "timezone_offset": -10800,
        "manually_saved": True,
        "from_chrome": True,
        "from_extension": True,
        "captured_user_agent": (payload.user_agent or "")[:240],
        "uploaded_by": user.get("email"),
        "saved_at": int(_time.time()),
    }

    # Path final no workspace
    session_file = sessions_dir_path / f"{safe_username}.json"
    # Backup do anterior se existir
    if session_file.exists():
        try:
            backup = session_file.with_suffix(".json.bak")
            backup.write_bytes(session_file.read_bytes())
        except Exception:
            pass

    session_file.write_text(json.dumps(session_data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[sessions] upload OK: @{safe_username} ws={ws_slug} by={user.get('email')} ({len(cookies)} cookies, sid=...{sessionid[-8:]})")
    return {
        "ok": True,
        "username": safe_username,
        "workspace_slug": ws_slug,
        "cookies_count": len(cookies),
        "sessionid_preview": "..." + sessionid[-12:],
    }


@app.get("/api/sessions/extension-bundle")
def api_extension_bundle(request: Request):
    """Zipa a pasta /extension e devolve como download. Permite usuário baixar
    sem precisar acessar SSH. Sempre pega versão mais nova do disco.

    Auth: exige login OU extension token. Mesmo que o ZIP não tenha segredos,
    evita scraping aleatório do endpoint."""
    auth.require_user_or_extension(request)
    import io as _io
    import zipfile as _zf
    from pathlib import Path as _P
    code_root = _P(__file__).resolve().parent.parent
    ext_dir = code_root / "extension"
    if not ext_dir.exists():
        raise HTTPException(404, "extension/ não encontrado no servidor")
    buf = _io.BytesIO()
    with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as zf:
        for f in ext_dir.rglob("*"):
            if f.is_file() and not f.name.endswith(".pyc"):
                # Pula make_icons.py — só dev precisa
                if f.name == "make_icons.py":
                    continue
                arcname = f.relative_to(ext_dir).as_posix()
                zf.write(f, arcname)
    buf.seek(0)
    from fastapi.responses import Response
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="insta-poster-extension.zip"'},
    )


@app.get("/api/sessions/extension-info")
def api_extension_info(request: Request):
    """Endpoint pra extensão validar token + obter contexto inicial.
    Devolve workspaces, contas + proxy (decomposto) + has_session (se tem
    cookies salvos na VPS). Auth via Bearer extension_token.
    """
    user = auth.require_user_or_extension(request)
    from web.workspaces import manager as ws_manager
    from core import paths as _paths
    workspaces = ws_manager.list()
    out_accounts = []
    for ws in workspaces:
        accs_file = _paths.accounts_file(ws["slug"])
        if not accs_file.exists():
            continue
        sessions_path = _paths.sessions_dir(ws["slug"])
        try:
            accs = json.loads(accs_file.read_text(encoding="utf-8"))
            for a in accs:
                if a.get("active", True):
                    # Decompoe proxy URL pra extensão usar via chrome.proxy API
                    raw_proxy = (a.get("proxy") or "").strip()
                    proxy_info = None
                    if raw_proxy:
                        normalized = _normalize_proxy(raw_proxy)
                        if normalized:
                            try:
                                from urllib.parse import urlparse as _urlparse
                                p = _urlparse(normalized)
                                proxy_info = {
                                    "scheme": p.scheme or "http",
                                    "host": p.hostname or "",
                                    "port": p.port or 80,
                                    "username": p.username or "",
                                    "password": p.password or "",
                                }
                            except Exception:
                                proxy_info = None
                    # Flag: tem cookies na VPS pra essa conta?
                    safe_user = "".join(c for c in (a.get("username") or "").lower() if c.isalnum() or c in "._-")
                    session_file = sessions_path / f"{safe_user}.json"
                    has_session = False
                    session_saved_at = None
                    if session_file.exists():
                        try:
                            sd = json.loads(session_file.read_text(encoding="utf-8"))
                            sid = (sd.get("authorization_data") or {}).get("sessionid") or (sd.get("cookies") or {}).get("sessionid")
                            if sid:
                                has_session = True
                                session_saved_at = sd.get("saved_at")
                        except Exception:
                            pass
                    out_accounts.append({
                        "username": a.get("username"),
                        "workspace_slug": ws["slug"],
                        "workspace_name": ws.get("name") or ws["slug"],
                        "proxy": proxy_info,
                        "has_session": has_session,
                        "session_saved_at": session_saved_at,
                    })
        except Exception:
            pass
    return {
        "ok": True,
        "user_email": user.get("email"),
        "workspaces": [{"slug": ws["slug"], "name": ws.get("name") or ws["slug"]} for ws in workspaces],
        "accounts": out_accounts,
    }


@app.get("/api/sessions/cookies")
def api_get_session_cookies(
    request: Request,
    username: str,
    workspace_slug: Optional[str] = None,
):
    """Baixa cookies salvos da conta pra extensão injetar no Chrome local.
    Permite "abrir já logado" em qualquer browser com a extensão.

    Auth: Bearer extension_token. Workspace-scoped.
    Retorna lista no formato chrome.cookies.set (name/value/domain/path/
    secure/httpOnly/sameSite/expirationDate).
    """
    auth.require_user_or_extension(request)

    safe_user = "".join(c for c in (username or "").lower() if c.isalnum() or c in "._-")
    if not safe_user:
        raise HTTPException(400, "username inválido")

    from core import paths as _paths
    ws = (workspace_slug or "").strip() or _paths.get_workspace() or "default"
    if not all(c.isalnum() or c in "-_" for c in ws):
        raise HTTPException(400, "workspace_slug inválido")

    session_file = _paths.sessions_dir(ws) / f"{safe_user}.json"
    if not session_file.exists():
        raise HTTPException(404, "Sessão ainda não foi salva pra essa conta — faça Save Sessão primeiro")

    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(500, "session.json corrompido — refaça Save Sessão")

    # Coleta cookies do dict + authorization_data (worker às vezes guarda em ambos)
    cookies_dict = dict(data.get("cookies") or {})
    auth_data = data.get("authorization_data") or {}
    for k in ("sessionid", "ds_user_id"):
        if auth_data.get(k) and not cookies_dict.get(k):
            cookies_dict[k] = auth_data[k]

    if not cookies_dict.get("sessionid"):
        raise HTTPException(400, "Sessão sem sessionid — cookies corrompidos, refaça Save Sessão")

    # Mapeia pra formato chrome.cookies.set
    # HttpOnly só pros cookies que o IG marca como httponly (descobertos via inspect)
    HTTP_ONLY_COOKIES = {"sessionid"}  # único certo HttpOnly
    import time as _t
    expires = int(_t.time()) + 90 * 24 * 3600  # 90 dias
    cookies_out = []
    for name, value in cookies_dict.items():
        if not value:
            continue
        cookies_out.append({
            "name": name,
            "value": str(value),
            "domain": ".instagram.com",
            "path": "/",
            "secure": True,
            "httpOnly": name in HTTP_ONLY_COOKIES,
            "sameSite": "lax",
            "expirationDate": expires,
        })

    return {
        "ok": True,
        "username": safe_user,
        "workspace_slug": ws,
        "cookies": cookies_out,
        "count": len(cookies_out),
        "saved_at": data.get("saved_at"),
        "from_extension": bool(data.get("from_extension")),
    }


@app.post("/api/me/extension-token/rotate")
def api_rotate_extension_token(user=Depends(auth.require_user)):
    """Gera novo token pra extensão (sobrescreve antigo se existir).
    Token só aparece nesta resposta — guarda em local seguro."""
    token = auth.rotate_extension_token(user["email"])
    return {"ok": True, "extension_token": token}


@app.post("/api/me/extension-token/revoke")
def api_revoke_extension_token(user=Depends(auth.require_user)):
    """Remove token (extensão para de funcionar até nova rotação)."""
    revoked = auth.revoke_extension_token(user["email"])
    return {"ok": True, "revoked": revoked}


@app.get("/api/me/extension-token")
def api_get_extension_token_status(user=Depends(auth.require_user)):
    """Devolve só se tem token configurado (NÃO devolve o token em si por segurança).
    UI usa pra mostrar 'Gerar token' vs 'Rotacionar/Revogar'."""
    u = auth.find_user(user["email"])
    has_token = bool(u and u.get("extension_token"))
    return {
        "ok": True,
        "has_token": has_token,
        "token_at": u.get("extension_token_at") if u else None,
    }


@app.post("/api/accounts/normalize-proxies")
def api_normalize_all_proxies():
    """Bulk fix: normaliza TODOS os proxies já salvos pra formato URL.
    Útil quando você importou várias contas com proxy no formato
    'host:port:user:pass' (DataImpulse) — converte tudo de uma vez."""
    fixed = []
    skipped = 0
    with accounts_transaction() as accounts:
        for a in accounts:
            raw = (a.get("proxy") or "").strip()
            if not raw:
                continue
            normalized = _normalize_proxy(raw)
            if normalized and normalized != raw:
                a["proxy"] = normalized
                fixed.append({"username": a["username"], "from": raw, "to": normalized})
            else:
                skipped += 1
    return {"ok": True, "fixed_count": len(fixed), "skipped_count": skipped, "fixed": fixed}


@app.get("/api/accounts/{username}/totp-code")
def api_show_totp(username: str):
    """Retorna credenciais COMPLETAS da conta pra login manual:
    senha + código TOTP atual + segundos restantes. Pra usar quando
    abre Chrome via botão Smartphone e precisa preencher manualmente."""
    accounts = load_accounts()
    for a in accounts:
        if a["username"] == username:
            password = a.get("password") or ""
            secret = a.get("totp_secret")
            result = {
                "username": username,
                "password": password,
                "has_password": bool(password),
                "has_totp": bool(secret),
                "code": None,
                "seconds_left": None,
            }
            if secret:
                try:
                    from instagrapi import Client
                    import time as _time
                    result["code"] = Client().totp_generate_code(secret.replace(" ", "").replace("-", "").upper())
                    # TOTP padrão tem janela de 30s; calcula quanto falta
                    result["seconds_left"] = 30 - int(_time.time()) % 30
                except Exception as e:
                    result["totp_error"] = f"Chave inválida: {e}"
            return result
    raise HTTPException(404, "Conta não encontrada")


# ---------- BROWSER LAUNCHER (Chrome mobile com fingerprint isolado) ----------

@app.get("/api/accounts/{username}/launcher.bat")
def api_account_launcher_bat(username: str):
    """Gera um arquivo .BAT (Windows) que abre Chrome com:
    - Profile isolado pra essa conta (cookies não compartilham)
    - User-Agent mobile do device fingerprint da conta
    - Janela tamanho celular (375x812)
    - Instagram já aberto

    Funciona em conjunto com o sistema: mesma device fingerprint que o
    worker usa pra postar = mesma fingerprint que o Chrome usa pra browsing
    manual. Coerência total — Instagram vê 1 celular único por conta.
    """
    from fastapi.responses import PlainTextResponse
    from core.devices import device_for_account

    # Sanitiza username (path safety)
    safe_uname = "".join(c for c in username if c.isalnum() or c in "._-")
    if not safe_uname:
        raise HTTPException(400, "Username inválido")

    device = device_for_account(username)
    user_agent = device["user_agent"]
    model = f"{device['manufacturer']} {device['model']}"

    # Tamanho aproximado de celular vertical
    width, height = 412, 870

    bat_content = f"""@echo off
REM Insta Poster - launcher Chrome pra @{safe_uname}
REM Device fingerprint: {model} (Android {device['android_release']})
REM Profile isolado em %USERPROFILE%\\InstaposterProfiles\\{safe_uname}
REM
REM Esse arquivo abre o Chrome com User-Agent mobile + cookies dedicados
REM dessa conta. Voce loga 1x e fica salvo no profile.

setlocal

set "PROFILE_DIR=%USERPROFILE%\\InstaposterProfiles\\{safe_uname}"
if not exist "%PROFILE_DIR%" mkdir "%PROFILE_DIR%"

set "CHROME="
if exist "%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe" set "CHROME=%ProgramFiles%\\Google\\Chrome\\Application\\chrome.exe"
if exist "%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe" set "CHROME=%ProgramFiles(x86)%\\Google\\Chrome\\Application\\chrome.exe"
if exist "%LocalAppData%\\Google\\Chrome\\Application\\chrome.exe" set "CHROME=%LocalAppData%\\Google\\Chrome\\Application\\chrome.exe"

if "%CHROME%"=="" (
    echo Chrome nao encontrado. Instale o Google Chrome primeiro.
    pause
    exit /b 1
)

echo Abrindo Instagram pra @{safe_uname}...
echo Device: {model}
echo.

start "" "%CHROME%" ^
    --user-data-dir="%PROFILE_DIR%" ^
    --user-agent="{user_agent}" ^
    --window-size={width},{height} ^
    --no-first-run ^
    --no-default-browser-check ^
    --disable-features=Translate ^
    https://www.instagram.com/

endlocal
"""
    return PlainTextResponse(
        bat_content,
        headers={
            "Content-Type": "application/x-bat; charset=utf-8",
            "Content-Disposition": f'attachment; filename="open_{safe_uname}.bat"',
        },
    )


@app.get("/api/accounts/{username}/launcher.sh")
def api_account_launcher_sh(username: str):
    """Versão Bash do launcher (macOS/Linux)."""
    from fastapi.responses import PlainTextResponse
    from core.devices import device_for_account

    safe_uname = "".join(c for c in username if c.isalnum() or c in "._-")
    if not safe_uname:
        raise HTTPException(400, "Username inválido")

    device = device_for_account(username)
    user_agent = device["user_agent"]
    model = f"{device['manufacturer']} {device['model']}"
    width, height = 412, 870

    sh_content = f"""#!/bin/bash
# Insta Poster - launcher Chrome pra @{safe_uname}
# Device: {model} (Android {device['android_release']})

PROFILE_DIR="$HOME/InstaposterProfiles/{safe_uname}"
mkdir -p "$PROFILE_DIR"

# Detecta Chrome no macOS ou Linux
CHROME=""
if command -v google-chrome >/dev/null 2>&1; then
    CHROME="google-chrome"
elif command -v google-chrome-stable >/dev/null 2>&1; then
    CHROME="google-chrome-stable"
elif [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
    CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
elif command -v chromium >/dev/null 2>&1; then
    CHROME="chromium"
else
    echo "Chrome nao encontrado. Instale o Google Chrome primeiro."
    exit 1
fi

echo "Abrindo Instagram pra @{safe_uname}..."
echo "Device: {model}"

"$CHROME" \\
    --user-data-dir="$PROFILE_DIR" \\
    --user-agent="{user_agent}" \\
    --window-size={width},{height} \\
    --no-first-run \\
    --no-default-browser-check \\
    "https://www.instagram.com/" &
"""
    return PlainTextResponse(
        sh_content,
        headers={
            "Content-Type": "application/x-sh; charset=utf-8",
            "Content-Disposition": f'attachment; filename="open_{safe_uname}.sh"',
        },
    )


@app.post("/api/accounts/{username}/toggle")
def api_toggle_account(username: str):
    accounts = load_accounts()
    for a in accounts:
        if a["username"] == username:
            a["active"] = not a.get("active", True)
            save_accounts(accounts)
            return {"ok": True, "active": a["active"]}
    raise HTTPException(404, "Conta não encontrada")


@app.post("/api/accounts/{username}/toggle-warmup")
def api_toggle_warmup(username: str):
    """Liga/desliga o pular-aquecimento. Se skip_warmup=True, a conta
    entra direto no pool de diversificar sem esperar 24h da 1ª postagem."""
    accounts = load_accounts()
    for a in accounts:
        if a["username"] == username:
            a["skip_warmup"] = not a.get("skip_warmup", False)
            save_accounts(accounts)
            return {"ok": True, "skip_warmup": a["skip_warmup"]}
    raise HTTPException(404, "Conta não encontrada")


def _purge_account_data(username: str, workspace_slug: Optional[str] = None) -> dict:
    """Limpa TUDO que pertence a uma conta. Idempotente — pode chamar várias vezes.

    Inclui:
    - session.json + .bak
    - <user>_sticky.txt (sticky session tracker)
    - health/<user>.json (snapshots de shadow ban)
    - profile_pics/<user>.* (avatares baixados)
    - remote_jobs com essa account
    - schedules com essa account
    - links criados pra essa account
    - ip_pool: release ownership

    Retorna dict com contagens do que foi removido (debug + audit log).
    """
    from core import paths as _paths
    from core import ip_pool as _ip_pool
    from web.shortener import manager as _link_manager

    ws = workspace_slug or _paths.get_workspace() or "default"
    safe_user = "".join(c for c in (username or "").lower() if c.isalnum() or c in "._-")
    if not safe_user:
        return {"error": "username inválido"}

    counts = {"session": 0, "sticky": 0, "health": 0, "pics": 0, "links": 0, "jobs": 0, "schedules": 0, "ip_pool": 0}

    # session.json + .bak
    sessions_path = _paths.sessions_dir(ws)
    for fname in (f"{safe_user}.json", f"{safe_user}.json.bak"):
        p = sessions_path / fname
        if p.exists():
            try:
                p.unlink()
                counts["session"] += 1
            except Exception:
                pass

    # sticky session tracker
    sticky = sessions_path / f"{safe_user}_sticky.txt"
    if sticky.exists():
        try:
            sticky.unlink()
            counts["sticky"] = 1
        except Exception:
            pass

    # health snapshots
    health_dir = _paths.workspace_root(ws) / "health"
    if health_dir.exists():
        health_file = health_dir / f"{safe_user}.json"
        if health_file.exists():
            try:
                health_file.unlink()
                counts["health"] = 1
            except Exception:
                pass

    # profile pics — vários nomes possíveis (timestamped)
    pics_dir = _paths.profile_pics_dir(ws)
    if pics_dir.exists():
        for p in pics_dir.glob(f"{safe_user}_*"):
            try:
                p.unlink()
                counts["pics"] += 1
            except Exception:
                pass
        for p in pics_dir.glob(f"{safe_user}.*"):
            try:
                p.unlink()
                counts["pics"] += 1
            except Exception:
                pass

    # links
    try:
        counts["links"] = _link_manager.delete_by_account(safe_user, workspace_slug=ws)
    except Exception as e:
        print(f"[purge] erro limpando links de @{safe_user}: {e}")

    # remote jobs
    try:
        counts["jobs"] = rjob_manager.delete_by_account(safe_user, workspace_slug=ws)
    except Exception as e:
        print(f"[purge] erro limpando jobs de @{safe_user}: {e}")

    # schedules
    try:
        counts["schedules"] = schedule_manager.delete_by_account(safe_user, workspace_slug=ws)
    except Exception as e:
        print(f"[purge] erro limpando schedules de @{safe_user}: {e}")

    # ip_pool — release ownership (não deleta IPs, só remove username dos owners)
    try:
        _ip_pool.release_account(safe_user)
        counts["ip_pool"] = 1
    except Exception as e:
        print(f"[purge] erro liberando ip_pool de @{safe_user}: {e}")

    total = sum(v for k, v in counts.items() if isinstance(v, int))
    print(f"[purge] @{safe_user} (ws={ws}) removido: {counts} (total={total})")
    return counts


@app.post("/api/accounts/{username}/delete")
def api_delete_account(username: str):
    """Deleta conta + purga TODO dado associado (session, jobs, schedules, links,
    health, pics, ip_pool). Sem deixar órfãos pra trás."""
    accounts = load_accounts()
    new = [a for a in accounts if a["username"] != username]
    if len(new) == len(accounts):
        raise HTTPException(404, "Conta não encontrada")
    save_accounts(new)
    # Purga tudo associado
    purged = _purge_account_data(username)
    return {"ok": True, "purged": purged}


# ---------- IMPORTAÇÃO EM MASSA ----------

class BulkImportIn(BaseModel):
    text: str
    dry_run: bool = False  # se True, só faz preview sem importar
    connect_after: bool = False  # se True, dispara test_login após importar


import re as _re_acct

# 2FA seed: base32 só [A-Z2-7], geralmente 16 ou 32 chars
_RE_TOTP_SECRET = _re_acct.compile(r"^[A-Z2-7]{16,64}$")
# Username Insta: minúsculas/dígitos/._
_RE_USERNAME = _re_acct.compile(r"^[a-z0-9._]{1,30}$", _re_acct.IGNORECASE)
# Numero solto tipo "1", "2)", "3:"
_RE_JUST_NUMBER = _re_acct.compile(r"^\d{1,4}[\.\)\-:]?$")


def _classify_field(s: str) -> str:
    """Identifica o tipo do campo: '2fa', 'username', 'password' ou 'unknown'."""
    s = s.strip()
    if _RE_TOTP_SECRET.match(s):
        return "2fa"
    # Username Insta NÃO pode ter espaços/símbolos exceto . _
    if _RE_USERNAME.match(s) and not any(c in s for c in (" ", ":", "|", ";")):
        return "username"
    # Resto é senha (qualquer coisa que não bate em 2fa nem username)
    return "password"


def _parse_account_block(lines: list[str]) -> Optional[dict]:
    """Parse 3-4 linhas que pertencem ao mesmo bloco (1 conta).

    Aceita ordem ARBITRÁRIA: tenta identificar qual linha é 2fa/user/senha.
    Formato comum do vendedor:
        2FA_SECRET      (32 chars MAIÚSCULOS)
        username
        password
    Ou com número no início (ignorado):
        1
        2FA_SECRET
        username
        password
    """
    # Remove números soltos (1, 2, 3...) e linhas vazias
    cleaned = []
    for ln in lines:
        s = ln.strip()
        if not s or _RE_JUST_NUMBER.match(s):
            continue
        cleaned.append(s)

    if len(cleaned) < 2:
        return None

    # Classifica cada linha
    fields = {"2fa": None, "username": None, "password": None}
    unclassified = []
    for s in cleaned:
        cat = _classify_field(s)
        if cat == "2fa" and not fields["2fa"]:
            fields["2fa"] = s
        elif cat == "username" and not fields["username"]:
            fields["username"] = s
        elif cat == "password" and not fields["password"]:
            fields["password"] = s
        else:
            unclassified.append(s)

    # Se sobraram unclassified e ainda tem slots vazios, tenta preencher
    for s in unclassified:
        if not fields["password"]:
            fields["password"] = s
        elif not fields["username"]:
            fields["username"] = s

    if not fields["username"] or not fields["password"]:
        return None  # falta info crítica

    return {
        "username": fields["username"],
        "password": fields["password"],
        "totp_secret": fields["2fa"],
    }


def _parse_account_line(line: str) -> Optional[dict]:
    """Parse uma SINGLE LINE em conta (formato user:senha:2fa).
    Pra formato multi-linha use _parse_account_block."""
    line = (line or "").strip()
    if not line or line.startswith("#") or line.startswith("//"):
        return None

    parts = None
    for sep in [":", "|", ";", "\t"]:
        if sep in line:
            parts = [p.strip() for p in line.split(sep)]
            break
    if parts is None:
        return None  # sem separador → não é single-line

    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None

    # Detecta automaticamente qual posição tem o 2FA (base32)
    fields = {"2fa": None, "username": None, "password": None}
    for p in parts:
        cat = _classify_field(p)
        if cat == "2fa" and not fields["2fa"]:
            fields["2fa"] = p
        elif cat == "username" and not fields["username"]:
            fields["username"] = p
        elif not fields["password"]:
            fields["password"] = p

    # Fallback: se não classificou, ordem padrão user:senha:2fa
    if not fields["username"] and len(parts) >= 1:
        fields["username"] = parts[0]
    if not fields["password"] and len(parts) >= 2:
        fields["password"] = parts[1]
    if not fields["2fa"] and len(parts) >= 3:
        fields["2fa"] = parts[2]

    if not fields["username"] or not fields["password"]:
        return None

    return {
        "username": fields["username"],
        "password": fields["password"],
        "totp_secret": fields["2fa"],
    }


def _parse_accounts_text(text: str) -> dict:
    """Parse texto suportando 2 formatos:

    1. SINGLE-LINE (1 conta por linha):
       user:senha:2fa
       user|senha|2fa

    2. MULTI-LINE BLOCKS (1 conta em 3-4 linhas, separadas por linha em branco):
       1
       BLBMV56NILPQYHPHO47LBJYF6S7CWMYU
       juliafreireua374
       elMSoYMQakO

    Detecta automaticamente qual formato baseado em: se há separador ":|;tab" → single-line.
    Senão, agrupa por linhas em branco.

    Retorna {valid: [...], invalid: [...], format_detected: "single"|"blocks"}.
    """
    text = text or ""
    has_separator = any(sep in text for sep in [":", "|", ";", "\t"])
    valid = []
    invalid = []
    seen_usernames = set()

    if has_separator:
        # SINGLE-LINE mode
        for i, raw_line in enumerate(text.splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            if _RE_JUST_NUMBER.match(line):
                continue  # pula numeração solta
            parsed = _parse_account_line(line)
            if not parsed:
                invalid.append({"line": i, "raw": raw_line[:80], "error": "formato não reconhecido"})
                continue
            if parsed["username"].lower() in seen_usernames:
                invalid.append({"line": i, "raw": raw_line[:80], "error": f"@{parsed['username']} duplicada na lista"})
                continue
            seen_usernames.add(parsed["username"].lower())
            valid.append(parsed)
        return {"valid": valid, "invalid": invalid, "format_detected": "single"}

    # MULTI-LINE BLOCKS mode (formato do vendedor real)
    # Split por linhas em branco
    lines = text.splitlines()
    blocks = []
    current_block = []
    current_block_start_line = 1
    for i, ln in enumerate(lines, 1):
        if ln.strip() == "":
            if current_block:
                blocks.append((current_block_start_line, current_block))
            current_block = []
            current_block_start_line = i + 1
        else:
            current_block.append(ln)
    if current_block:
        blocks.append((current_block_start_line, current_block))

    for start_line, block_lines in blocks:
        # Linhas tipo "1", "2)" sozinhas viram bloco sem conteúdo útil → pula
        non_number_lines = [l for l in block_lines if l.strip() and not _RE_JUST_NUMBER.match(l.strip())]
        if not non_number_lines:
            continue
        parsed = _parse_account_block(block_lines)
        if not parsed:
            invalid.append({
                "line": start_line,
                "raw": " | ".join(l.strip() for l in block_lines[:3])[:80],
                "error": f"bloco com {len(non_number_lines)} linha(s) — não consegui identificar user/senha",
            })
            continue
        if parsed["username"].lower() in seen_usernames:
            invalid.append({"line": start_line, "raw": parsed["username"], "error": f"@{parsed['username']} duplicada na lista"})
            continue
        seen_usernames.add(parsed["username"].lower())
        valid.append(parsed)
    return {"valid": valid, "invalid": invalid, "format_detected": "blocks"}


@app.post("/api/accounts/bulk-import")
def api_bulk_import(payload: BulkImportIn, user=Depends(auth.require_user)):
    """Importa lista de contas em massa. Aceita formatos:
    user:senha:2fa | user|senha|2fa | user;senha;2fa | user[TAB]senha[TAB]2fa

    dry_run=True: só faz preview sem salvar.
    connect_after=True: dispara test_login pra cada conta importada (com stagger).
    """
    parsed = _parse_accounts_text(payload.text)
    valid = parsed["valid"]
    invalid = parsed["invalid"]

    accounts = load_accounts()
    existing_usernames = {a["username"].lower() for a in accounts}

    to_add = []
    skipped_existing = []
    for entry in valid:
        if entry["username"].lower() in existing_usernames:
            skipped_existing.append(entry["username"])
        else:
            to_add.append(entry)

    if payload.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "would_add_count": len(to_add),
            "would_add": [e["username"] for e in to_add],
            "skipped_existing_count": len(skipped_existing),
            "skipped_existing": skipped_existing,
            "invalid_count": len(invalid),
            "invalid": invalid,
            "total_lines_parsed": len(valid) + len(invalid),
        }

    # Aplica importação
    added = []
    for entry in to_add:
        accounts.append({
            # Normaliza username pra lowercase (consistência)
            "username": entry["username"].strip().lower(),
            "password": entry["password"],
            "totp_secret": entry["totp_secret"],
            "proxy": None,
            "active": True,
        })
        added.append(entry["username"])
    if added:
        save_accounts(accounts)

    # Se connect_after, cria jobs test_login com stagger
    connect_jobs_created = 0
    if payload.connect_after and added:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from core.retry import humanlike_delay
        now_utc = _dt.now(_tz.utc)
        # Stagger exponencial (long-tail, mimics real user) em vez de uniforme
        # (linear, padrão detectável). Acumulado entre contas: 1ª em 0s, 2ª em
        # 30-180s, 3ª no anterior+30-180s, etc.
        cumulative_s = 0
        for idx, username in enumerate(added):
            acc = next((a for a in accounts if a["username"] == username), None)
            if not acc:
                continue
            if idx > 0:
                cumulative_s += humanlike_delay(min_s=30, mean_s=75, max_s=180)
            scheduled_for = (now_utc + _td(seconds=cumulative_s)).isoformat(timespec="seconds")
            try:
                rjob_manager.create({
                    "operation": "test_login",
                    "account_username": acc["username"],
                    "account_password": acc["password"],
                    "account_totp_secret": acc.get("totp_secret"),
                    "account_proxy": acc.get("proxy"),
                    "scheduled_for": scheduled_for,
                    "created_by": f"bulk-import:{user['email']}",
                })
                connect_jobs_created += 1
            except Exception as e:
                print(f"[bulk-import] erro criando job pra @{username}: {e}")

    return {
        "ok": True,
        "dry_run": False,
        "added_count": len(added),
        "added": added,
        "skipped_existing_count": len(skipped_existing),
        "skipped_existing": skipped_existing,
        "invalid_count": len(invalid),
        "invalid": invalid,
        "connect_jobs_created": connect_jobs_created,
    }


class BulkConnectIn(BaseModel):
    usernames: Optional[list[str]] = None  # se None, conecta todas desconectadas
    only_disconnected: bool = True  # se True, ignora as que já tem connected_via_worker_id


@app.post("/api/accounts/bulk-connect")
def api_bulk_connect(payload: BulkConnectIn, user=Depends(auth.require_user)):
    """Dispara test_login pra N contas em massa com stagger (30-90s entre cada).

    Por padrão pula contas já conectadas (only_disconnected=True).
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import random as _r

    accounts = load_accounts()

    if payload.usernames:
        target_usernames = [u for u in payload.usernames if u]
    else:
        target_usernames = [a["username"] for a in accounts if a.get("active", True)]

    if payload.only_disconnected:
        target_usernames = [
            u for u in target_usernames
            if not next((a for a in accounts if a["username"] == u), {}).get("connected_via_worker_id")
        ]

    now_utc = _dt.now(_tz.utc)
    created = []
    for idx, username in enumerate(target_usernames):
        acc = next((a for a in accounts if a["username"] == username), None)
        if not acc:
            continue
        delay_s = idx * _r.randint(30, 90)
        scheduled_for = (now_utc + _td(seconds=delay_s)).isoformat(timespec="seconds")
        try:
            rj = rjob_manager.create({
                "operation": "test_login",
                "account_username": acc["username"],
                "account_password": acc["password"],
                "account_totp_secret": acc.get("totp_secret"),
                "account_proxy": acc.get("proxy"),
                "scheduled_for": scheduled_for,
                "created_by": f"bulk-connect:{user['email']}",
            })
            created.append({"username": username, "job_id": rj.id, "scheduled_for": scheduled_for})
        except Exception as e:
            print(f"[bulk-connect] erro pra @{username}: {e}")

    return {
        "ok": True,
        "count": len(created),
        "jobs": created,
        "estimated_total_minutes": (len(created) * 60) / 60,  # aprox
    }


@app.post("/api/accounts/bulk-delete")
def api_bulk_delete(payload: BulkUsernames):
    accounts = load_accounts()
    targets = set(payload.usernames)
    new = [a for a in accounts if a["username"] not in targets]
    save_accounts(new)
    removed = []
    purge_summary = {}
    for u in targets:
        purged = _purge_account_data(u)
        removed.append(u)
        purge_summary[u] = purged
    return {"ok": True, "removed": removed, "remaining": len(new), "purge_summary": purge_summary}


@app.post("/api/admin/cleanup-orphans")
def api_cleanup_orphans(user=Depends(auth.require_owner)):
    """Sweep manual: encontra e remove dados órfãos. Cada bloco é independente —
    se 1 falhar, os outros continuam (defensivo)."""
    import traceback as _tb

    summary = {
        "workspaces_scanned": 0,
        "jobs_removed": 0,
        "schedules_removed": 0,
        "session_files_removed": 0,
        "sticky_files_removed": 0,
        "health_files_removed": 0,
        "links_removed": 0,
        "errors": [],
    }

    # ===== Constrói mapa de contas válidas =====
    valid_accounts_by_ws: dict = {}
    try:
        from core import paths as _paths
        from web.workspaces import manager as _ws_manager
        ws_list = _ws_manager.list()
        for ws in ws_list:
            slug = ws.get("slug") if isinstance(ws, dict) else getattr(ws, "slug", None)
            if not slug:
                continue
            try:
                accs_file = _paths.accounts_file(slug)
                if not accs_file.exists():
                    valid_accounts_by_ws[slug] = set()
                    continue
                accs = json.loads(accs_file.read_text(encoding="utf-8"))
                valid_accounts_by_ws[slug] = {
                    (a.get("username") or "").lower()
                    for a in accs if a.get("username")
                }
                summary["workspaces_scanned"] += 1
            except Exception as e:
                summary["errors"].append(f"ws {slug}: {e}")
                valid_accounts_by_ws[slug] = set()
    except Exception as e:
        summary["errors"].append(f"map de workspaces: {e}")
        print(f"[cleanup-orphans] FATAL map: {_tb.format_exc()}")
        return {"ok": False, "error": str(e), "summary": summary}

    # ===== 1) Jobs órfãos =====
    try:
        summary["jobs_removed"] = rjob_manager.purge_orphans(valid_accounts_by_ws)
    except Exception as e:
        summary["errors"].append(f"jobs: {e}")
        print(f"[cleanup-orphans] purge_orphans jobs: {_tb.format_exc()}")

    # ===== 2) Schedules órfãos =====
    try:
        summary["schedules_removed"] = schedule_manager.purge_orphans(valid_accounts_by_ws)
    except Exception as e:
        summary["errors"].append(f"schedules: {e}")
        print(f"[cleanup-orphans] purge_orphans schedules: {_tb.format_exc()}")

    # ===== 3) Arquivos órfãos por workspace =====
    try:
        from core import paths as _paths
        for slug, valid_users in valid_accounts_by_ws.items():
            try:
                sessions_dir_p = _paths.sessions_dir(slug)
                if sessions_dir_p.exists():
                    for f in sessions_dir_p.iterdir():
                        if not f.is_file():
                            continue
                        base = f.stem
                        if base.endswith("_sticky"):
                            user = base[:-len("_sticky")]
                            if user.lower() not in valid_users:
                                try: f.unlink(); summary["sticky_files_removed"] += 1
                                except Exception: pass
                        elif f.suffix == ".json":
                            user = base
                            if user.lower() not in valid_users:
                                try: f.unlink(); summary["session_files_removed"] += 1
                                except Exception: pass
                health_dir = _paths.workspace_root(slug) / "health"
                if health_dir.exists():
                    for f in health_dir.iterdir():
                        if f.is_file() and f.suffix == ".json":
                            user = f.stem
                            if user.lower() not in valid_users:
                                try: f.unlink(); summary["health_files_removed"] += 1
                                except Exception: pass
            except Exception as e:
                summary["errors"].append(f"files ws={slug}: {e}")
    except Exception as e:
        summary["errors"].append(f"files (geral): {e}")
        print(f"[cleanup-orphans] files: {_tb.format_exc()}")

    # ===== 4) Links órfãos =====
    try:
        from web.shortener import manager as _link_manager
        all_users = set()
        for users in valid_accounts_by_ws.values():
            all_users.update(users)
        # Usa lock + items diretamente — sem método público pra remover por filtro
        # Encapsulamos pra ser robusto se algum atributo mudar
        if hasattr(_link_manager, "_lock") and hasattr(_link_manager, "_items"):
            with _link_manager._lock:
                slugs_to_delete = []
                for slug, link in list(_link_manager._items.items()):
                    acc = getattr(link, "account", None)
                    if acc and acc.lower() not in all_users:
                        slugs_to_delete.append(slug)
                for slug in slugs_to_delete:
                    _link_manager._items.pop(slug, None)
                if slugs_to_delete:
                    try:
                        _link_manager._save()
                    except Exception as e:
                        summary["errors"].append(f"links save: {e}")
                summary["links_removed"] = len(slugs_to_delete)
    except Exception as e:
        summary["errors"].append(f"links: {e}")
        print(f"[cleanup-orphans] links: {_tb.format_exc()}")

    summary["errors"] = summary["errors"][:20]
    print(f"[cleanup-orphans] feito por {user.get('email', '?')}: {summary}")
    return {"ok": True, "summary": summary}


@app.post("/api/accounts/{username}/test-login")
def api_test_login(username: str):
    accounts = load_accounts()
    if not any(a["username"] == username for a in accounts):
        raise HTTPException(404, "Conta não está em accounts.json")
    job = job_manager.start(
        kind="test_login",
        args=["test_login.py", username],
        label=f"login @{username}",
    )
    return {"ok": True, "job_id": job.id}


# --------- PROFILE / AUTOMATIONS via worker -----------

PROFILE_PICS_DIR = data_path("profile_pics")
PROFILE_PICS_DIR.mkdir(parents=True, exist_ok=True)


class EditProfileIn(BaseModel):
    accounts: list[str]              # multi-conta
    biography: Optional[str] = None
    full_name: Optional[str] = None
    external_url: Optional[str] = None


@app.post("/api/accounts/edit-profile")
def api_edit_profile(payload: EditProfileIn, user=Depends(auth.require_user)):
    """Cria 1 remote_job de edit_profile pra cada conta da lista."""
    if not payload.accounts:
        raise HTTPException(400, "Lista de contas vazia")
    accounts = load_accounts()
    created = []
    for uname in payload.accounts:
        acc = next((a for a in accounts if a["username"] == uname), None)
        if not acc:
            continue
        params = {}
        if payload.biography is not None:
            params["biography"] = payload.biography
        if payload.full_name is not None:
            params["full_name"] = payload.full_name
        if payload.external_url is not None:
            params["external_url"] = payload.external_url
        rj = rjob_manager.create({
            "operation": "edit_profile",
            "params": params,
            "account_username": acc["username"],
            "account_password": acc["password"],
            "account_totp_secret": acc.get("totp_secret"),
            "account_proxy": acc.get("proxy"),
            "created_by": user["email"],
        })
        created.append(rj.id)
    return {"ok": True, "count": len(created), "job_ids": created}


@app.post("/api/accounts/change-picture")
async def api_change_picture(
    request: Request,
    accounts: str = Form(...),  # JSON list
    image: UploadFile = File(...),
    user=Depends(auth.require_user),
):
    """Recebe upload de foto + lista de contas. Salva foto + cria N jobs."""
    try:
        target_usernames = json.loads(accounts)
        assert isinstance(target_usernames, list)
    except Exception:
        raise HTTPException(400, "campo 'accounts' deve ser JSON array de usernames")
    if not target_usernames:
        raise HTTPException(400, "Lista de contas vazia")

    # Salva foto com timestamp
    import time as _t
    ext = Path(image.filename or "pic.jpg").suffix.lower() or ".jpg"
    fname = f"pic_{int(_t.time())}_{secrets.token_hex(4)}{ext}"
    target = PROFILE_PICS_DIR / fname
    with target.open("wb") as f:
        while chunk := await image.read(1024 * 1024):
            f.write(chunk)

    accounts_list = load_accounts()
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    image_url = f"{base}/api/worker/profile-pic/{fname}"

    created = []
    for uname in target_usernames:
        acc = next((a for a in accounts_list if a["username"] == uname), None)
        if not acc:
            continue
        rj = rjob_manager.create({
            "operation": "change_picture",
            "params": {"image_url": image_url},
            "account_username": acc["username"],
            "account_password": acc["password"],
            "account_totp_secret": acc.get("totp_secret"),
            "account_proxy": acc.get("proxy"),
            "media_url": image_url,
            "created_by": user["email"],
        })
        created.append(rj.id)
    return {"ok": True, "count": len(created), "job_ids": created, "image_name": fname}


@app.get("/api/worker/profile-pic/{name}")
def api_worker_profile_pic(name: str):
    """Serve foto pro worker baixar (rota pública sem auth — worker valida via token no header)."""
    name = safe_name(name)
    p = PROFILE_PICS_DIR / name
    if not p.exists():
        raise HTTPException(404, "Foto não encontrada")
    from fastapi.responses import FileResponse
    ext = p.suffix.lower()
    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else ("image/png" if ext == ".png" else "image/webp")
    return FileResponse(p, media_type=mime)


@app.get("/api/accounts/{username}/profile-info")
def api_get_profile_info(username: str, user=Depends(auth.require_user)):
    """Cria job pra worker buscar info atual do perfil."""
    accounts = load_accounts()
    acc = next((a for a in accounts if a["username"] == username), None)
    if not acc:
        raise HTTPException(404, "Conta não encontrada")
    rj = rjob_manager.create({
        "operation": "get_profile_info",
        "account_username": acc["username"],
        "account_password": acc["password"],
        "account_totp_secret": acc.get("totp_secret"),
        "account_proxy": acc.get("proxy"),
        "created_by": user["email"],
    })
    return {"ok": True, "job_id": rj.id}


class AutomationsIn(BaseModel):
    auto_like_enabled: Optional[bool] = None
    auto_like_max_per_day: Optional[int] = None
    auto_follow_back_enabled: Optional[bool] = None
    auto_follow_back_max_per_day: Optional[int] = None
    auto_highlight_enabled: Optional[bool] = None
    auto_highlight_title: Optional[str] = None
    sync_enabled: Optional[bool] = None
    sync_interval_hours: Optional[int] = None


@app.post("/api/accounts/{username}/automations")
def api_update_automations(username: str, payload: AutomationsIn, user=Depends(auth.require_user)):
    """Liga/desliga automações da conta + limites diários."""
    accounts = load_accounts()
    for a in accounts:
        if a["username"] == username:
            if payload.auto_like_enabled is not None:
                a["auto_like_enabled"] = bool(payload.auto_like_enabled)
            if payload.auto_like_max_per_day is not None:
                a["auto_like_max_per_day"] = max(0, min(100, int(payload.auto_like_max_per_day)))
            if payload.auto_follow_back_enabled is not None:
                a["auto_follow_back_enabled"] = bool(payload.auto_follow_back_enabled)
            if payload.auto_follow_back_max_per_day is not None:
                a["auto_follow_back_max_per_day"] = max(0, min(50, int(payload.auto_follow_back_max_per_day)))
            if payload.auto_highlight_enabled is not None:
                a["auto_highlight_enabled"] = bool(payload.auto_highlight_enabled)
            if payload.auto_highlight_title is not None:
                a["auto_highlight_title"] = (payload.auto_highlight_title or "").strip()[:30]
            if payload.sync_enabled is not None:
                a["sync_enabled"] = bool(payload.sync_enabled)
                # Reset completed flag se o usuário religar manualmente
                if payload.sync_enabled:
                    a["sync_completed"] = False
            if payload.sync_interval_hours is not None:
                a["sync_interval_hours"] = max(1, min(72, int(payload.sync_interval_hours)))
            save_accounts(accounts)
            return {"ok": True, "account": _account_view(a)}
    raise HTTPException(404, "Conta não encontrada")


# ---------- SYNC / BACKFILL ----------

def _build_media_pool(accounts: list[dict]) -> list[dict]:
    """Constrói o 'pool central' de mídias = união das posted_media de TODAS as contas,
    ordenado pela 1ª vez que cada nome apareceu (cronológico — mais antigo primeiro).

    Cada item: {name, kind, first_posted_at, posted_by_count}
    """
    pool: dict[str, dict] = {}
    for a in accounts:
        for item in (a.get("posted_media") or []):
            name = item.get("name")
            if not name:
                continue
            posted_at = item.get("posted_at") or ""
            if name not in pool:
                pool[name] = {
                    "name": name,
                    "kind": item.get("kind", "reel"),
                    "first_posted_at": posted_at,
                    "posted_by_count": 1,
                }
            else:
                pool[name]["posted_by_count"] += 1
                # Mantém a data mais antiga
                if posted_at and (not pool[name]["first_posted_at"] or posted_at < pool[name]["first_posted_at"]):
                    pool[name]["first_posted_at"] = posted_at
                    pool[name]["kind"] = item.get("kind", pool[name]["kind"])
    return sorted(pool.values(), key=lambda x: x["first_posted_at"] or "")


def _account_posted_names(a: dict) -> set[str]:
    return {item["name"] for item in (a.get("posted_media") or []) if item.get("name")}


def _stagger_times(count: int, seconds_per_job: int = 60) -> list[str]:
    """Gera lista de timestamps ISO UTC pra stagger N jobs.

    Distribui os jobs em janela = count * seconds_per_job (default 1min por job).
    Adiciona jitter aleatório (±15s) pra não parecer ritmo de robô.
    O primeiro job sai em ~5s (não bloqueia 1min se for só 1).
    Retorna lista de tamanho `count`, sorted ascendente.
    """
    import random as _r
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    if count <= 0:
        return []
    now = _dt.now(_tz.utc)
    times = []
    for i in range(count):
        base = 5 + (i * seconds_per_job)
        jitter = _r.uniform(-15, 15) if i > 0 else 0
        delta = max(0, base + jitter)
        times.append((now + _td(seconds=delta)).isoformat(timespec="seconds"))
    return sorted(times)


@app.get("/api/accounts/{username}/sync-info")
def api_sync_info(username: str, user=Depends(auth.require_user)):
    """Retorna pool central + progresso de sync da conta + próximas mídias da fila."""
    accounts = load_accounts()
    acc = next((a for a in accounts if a["username"] == username), None)
    if not acc:
        raise HTTPException(404, "Conta não encontrada")
    pool = _build_media_pool(accounts)
    already = _account_posted_names(acc)
    pending_in_pool = [m for m in pool if m["name"] not in already]
    # Só conta mídias que ainda existem no servidor (pending ou posted)
    pending_available = [
        m for m in pending_in_pool
        if (PENDING_DIR / m["name"]).exists() or (POSTED_DIR / m["name"]).exists()
    ]
    return {
        "username": username,
        "sync_enabled": bool(acc.get("sync_enabled", False)),
        "sync_interval_hours": int(acc.get("sync_interval_hours", 8)),
        "sync_last_post_at": acc.get("sync_last_post_at"),
        "sync_completed": bool(acc.get("sync_completed", False)),
        "pool_total": len(pool),
        "posted_count": len(already),
        "pending_count": len(pending_in_pool),
        "pending_available_count": len(pending_available),
        "next_media": [m["name"] for m in pending_available[:5]],
    }


@app.post("/api/accounts/{username}/connect-via-worker")
def api_connect_via_worker(username: str, user=Depends(auth.require_user)):
    """Cria um remote job de test_login — worker pega, loga, salva sessão local
    no PC do worker. Não posta nada. Útil pra pré-aquecer várias contas."""
    accounts = load_accounts()
    account = next((a for a in accounts if a["username"] == username), None)
    if not account:
        raise HTTPException(404, f"Conta @{username} não cadastrada")

    job = rjob_manager.create({
        "operation": "test_login",
        "account_username": account["username"],
        "account_password": account["password"],
        "account_totp_secret": account.get("totp_secret"),
        "account_proxy": account.get("proxy"),
        "video_name": "",
        "media_type": "video",
        "kind": "reel",
        "caption": "",
        "media_url": "",
        "created_by": user["email"],
    })
    return {"ok": True, "job_id": job.id}


@app.post("/api/accounts/{username}/clear-session")
def api_clear_session(username: str):
    session_file = SESSIONS_DIR / f"{username}.json"
    if session_file.exists():
        session_file.unlink()
    return {"ok": True}


@app.post("/api/accounts/check-all")
def api_check_all_accounts(user=Depends(auth.require_user)):
    """Dispara test_login em todas as contas ativas E conectadas via worker.
    Resultado de cada job passa pelo api_worker_job_result, que detecta padrões
    de bloqueio e marca a conta automaticamente. Sem auto-schedule — só manual."""
    accounts = load_accounts()
    targets = [
        a for a in accounts
        if a.get("active", True) and a.get("connected_via_worker_id")
    ]
    if not targets:
        raise HTTPException(400, "Nenhuma conta ativa conectada via worker")

    created = []
    for acc in targets:
        rj = rjob_manager.create({
            "operation": "test_login",
            "account_username": acc["username"],
            "account_password": acc["password"],
            "account_totp_secret": acc.get("totp_secret"),
            "account_proxy": acc.get("proxy"),
            "created_by": user["email"],
        })
        created.append(rj.id)
    return {
        "ok": True,
        "count": len(created),
        "job_ids": created,
        "checked_usernames": [a["username"] for a in targets],
    }


@app.post("/api/accounts/{username}/clear-shadowban")
def api_clear_shadowban(username: str, user=Depends(auth.require_user)):
    """Desmarca conta como shadow ban suspect (manual override)."""
    accounts = load_accounts()
    for a in accounts:
        if a["username"] == username:
            a["shadowban_suspected"] = False
            a["shadowban_at"] = None
            a["shadowban_reason"] = None
            save_accounts(accounts)
            return {"ok": True, "account": _account_view(a)}
    raise HTTPException(404, "Conta não encontrada")


@app.post("/api/accounts/{username}/check-health")
def api_check_health_now(username: str, user=Depends(auth.require_user)):
    """Forca disparar collect_insights pra essa conta agora (sem esperar tick diario)."""
    accounts = load_accounts()
    acc = next((a for a in accounts if a["username"] == username), None)
    if not acc:
        raise HTTPException(404, "Conta não encontrada")
    if not acc.get("connected_via_worker_id"):
        raise HTTPException(400, "Conta não conectada via worker")
    rj = rjob_manager.create({
        "operation": "collect_insights",
        "account_username": acc["username"],
        "account_password": acc["password"],
        "account_totp_secret": acc.get("totp_secret"),
        "account_proxy": acc.get("proxy"),
        "created_by": user["email"],
    })
    return {"ok": True, "job_id": rj.id}


@app.get("/api/accounts/{username}/health")
def api_get_health(username: str, user=Depends(auth.require_user)):
    """Retorna histórico de saúde da conta (snapshots + análise)."""
    from web import health as _health
    history = _health.load_history(username)
    analysis = _health.analyze(username)
    # Devolve só últimos 30 snapshots pra UI (resto serve pra calcular)
    return {
        "username": username,
        "analysis": analysis,
        "history": history[-30:],
        "total_snapshots": len(history),
    }


@app.post("/api/accounts/{username}/clear-block")
def api_clear_block(username: str, user=Depends(auth.require_user)):
    """Desmarca a conta como bloqueada — use quando resolver o bloqueio manualmente
    (passou no challenge, mudou senha, etc)."""
    with accounts_transaction() as accounts:
        for a in accounts:
            if a["username"] == username:
                a["blocked"] = False
                a["blocked_at"] = None
                a["blocked_reason"] = None
                return {"ok": True, "account": _account_view(a)}
    raise HTTPException(404, "Conta não encontrada")


@app.post("/api/accounts/{username}/pause-worker")
def api_pause_worker(username: str, minutes: int = 30, user=Depends(auth.require_user)):
    """Pausa worker pra essa conta por N minutos (default 30).
    Usado quando o user vai mexer manualmente no browser — evita conflito
    'duas devices logando ao mesmo tempo' que dispara challenge no IG."""
    from datetime import datetime as _dt_p, timedelta as _td_p, timezone as _tz_p
    minutes = max(1, min(240, int(minutes)))  # clamp 1..240
    until = (_dt_p.now(_tz_p.utc) + _td_p(minutes=minutes)).isoformat(timespec="seconds")
    with accounts_transaction() as accounts:
        for a in accounts:
            if a["username"] == username:
                a["worker_paused_until"] = until
                return {"ok": True, "worker_paused_until": until, "minutes": minutes}
    raise HTTPException(404, "Conta não encontrada")


@app.post("/api/accounts/{username}/unpause-worker")
def api_unpause_worker(username: str, user=Depends(auth.require_user)):
    """Cancela a pausa do worker antes do tempo expirar."""
    with accounts_transaction() as accounts:
        for a in accounts:
            if a["username"] == username:
                a["worker_paused_until"] = None
                return {"ok": True}
    raise HTTPException(404, "Conta não encontrada")


@app.post("/api/accounts/{username}/clear-verification")
def api_clear_verification(username: str, user=Depends(auth.require_user)):
    """Desmarca needs_verification — use quando você fez login manual no app
    Instagram e passou pelo challenge/checkpoint."""
    with accounts_transaction() as accounts:
        for a in accounts:
            if a["username"] == username:
                a["needs_verification"] = False
                a["verification_at"] = None
                a["verification_reason"] = None
                return {"ok": True, "account": _account_view(a)}
    raise HTTPException(404, "Conta não encontrada")


@app.post("/api/accounts/{username}/clear-session-renewal")
def api_clear_session_renewal(username: str, user=Depends(auth.require_user)):
    """Desmarca needs_session_renewal — use depois de refazer Save Sessão
    via extensão Chrome OU botão Smartphone."""
    with accounts_transaction() as accounts:
        for a in accounts:
            if a["username"] == username:
                a["needs_session_renewal"] = False
                a["session_renewal_at"] = None
                a["session_renewal_reason"] = None
                return {"ok": True, "account": _account_view(a)}
    raise HTTPException(404, "Conta não encontrada")


# ---------- API: videos ----------

@app.get("/api/videos")
def api_list_videos():
    return {
        "pending": list_videos(PENDING_DIR),
        "posted": list_videos(POSTED_DIR),
    }


@app.post("/api/videos/upload-bulk")
async def api_upload_video_bulk(
    videos: list[UploadFile] = File(...),
    caption: str = Form(""),
    kind: str = Form("reel"),
    link_url: Optional[str] = Form(None),
):
    """Upload de N vídeos/fotos numa única request. MUITO mais rápido que N calls.

    Aceita kind+caption+link_url comuns aplicados a TODOS os arquivos. Fotos
    forçam kind=story automaticamente (limitação do Instagram).
    """
    if not videos:
        raise HTTPException(400, "Nenhum arquivo enviado")
    from core.poster import save_meta
    saved = []
    errors = []
    pending_dir = PENDING_DIR  # snapshot do proxy

    for video in videos:
        if not video.filename:
            errors.append({"name": "(sem nome)", "error": "arquivo sem nome"})
            continue
        try:
            name = safe_name(video.filename)
            ext = Path(name).suffix.lower()
            if ext not in MEDIA_EXTS:
                errors.append({"name": video.filename, "error": f"tipo {ext} não suportado"})
                continue
            target = pending_dir / name
            if target.exists():
                errors.append({"name": name, "error": "arquivo com esse nome já existe"})
                continue

            with target.open("wb") as f:
                while chunk := await video.read(1024 * 1024):
                    f.write(chunk)

            # Caption no .txt
            if caption:
                (pending_dir / (Path(name).stem + ".txt")).write_text(caption, encoding="utf-8")

            # Meta — SEMPRE salva (fix bug: antes só salvava se story OU tinha link,
            # então reel sem link ficava sem meta.json e o disparo lia kind=reel
            # mesmo pra fotos perdidas. Agora salva sempre pra ter origem verdadeira.)
            is_photo = ext in PHOTO_EXTS
            final_kind = "story" if is_photo else (kind if kind in ("reel", "story") else "reel")
            save_meta(str(target), {
                "kind": final_kind,
                "link_url": (link_url or "").strip() or None,
                "link_text": "Clique aqui",
            })

            # Thumb pra vídeo (async-friendly: melhor falhar silenciosamente que bloquear)
            if ext in VIDEO_EXTS:
                try:
                    generate_thumbnail(target)
                except Exception as e:
                    print(f"[upload-bulk] thumb falhou {name}: {e}")

            saved.append({"name": name, "kind": final_kind})
        except Exception as e:
            errors.append({"name": video.filename, "error": str(e)})

    _videos_cache_invalidate()
    return {
        "ok": True,
        "saved": saved,
        "errors": errors,
        "saved_count": len(saved),
        "error_count": len(errors),
    }


@app.post("/api/videos/upload")
async def api_upload_video(
    video: UploadFile = File(...),
    caption: str = Form(""),
):
    if not video.filename:
        raise HTTPException(400, "Arquivo sem nome")

    name = safe_name(video.filename)
    ext = Path(name).suffix.lower()
    if ext not in MEDIA_EXTS:
        raise HTTPException(400, f"Tipo não suportado ({ext}). Aceita: mp4, jpg, jpeg, png, webp")

    target = PENDING_DIR / name
    if target.exists():
        raise HTTPException(409, f"Já existe arquivo com nome {name}")

    with target.open("wb") as f:
        while chunk := await video.read(1024 * 1024):
            f.write(chunk)

    # Legenda no .txt com mesmo stem
    (PENDING_DIR / (Path(name).stem + ".txt")).write_text(caption, encoding="utf-8")

    # Meta — sempre salva pra ter kind explícito desde o upload
    from core.poster import save_meta as _save_meta
    is_photo = ext in PHOTO_EXTS
    _save_meta(str(target), {
        "kind": "story" if is_photo else "reel",
        "link_url": None,
        "link_text": "Clique aqui",
    })

    # Thumbnail só pra vídeo (foto já é a própria thumb)
    if ext in VIDEO_EXTS:
        try:
            generate_thumbnail(target)
        except Exception as e:
            print(f"[upload] thumb falhou: {e}")

    _videos_cache_invalidate()
    return {"ok": True, "name": name}


@app.post("/api/videos/{name}/caption")
def api_update_caption(name: str, caption: str = Form("")):
    name = safe_name(name)
    media = PENDING_DIR / name
    if not media.exists():
        raise HTTPException(404, "Mídia não encontrada em pending")
    media.with_suffix(".txt").write_text(caption, encoding="utf-8")
    return {"ok": True}


class MediaMetaIn(BaseModel):
    kind: str  # "reel" | "story"
    link_url: Optional[str] = None
    link_text: Optional[str] = None  # texto do sticker (default "Clique aqui")


@app.post("/api/videos/{name}/meta")
def api_update_meta(name: str, payload: MediaMetaIn):
    name = safe_name(name)
    media = PENDING_DIR / name
    if not media.exists():
        raise HTTPException(404, "Mídia não encontrada em pending")
    if payload.kind not in ("reel", "story"):
        raise HTTPException(400, "kind inválido (use 'reel' ou 'story')")
    # Reel só aceita vídeo
    ext = media.suffix.lower()
    if payload.kind == "reel" and ext not in VIDEO_EXTS:
        raise HTTPException(400, "Reel só aceita vídeo (.mp4). Use story pra fotos.")
    from core.poster import save_meta
    link = (payload.link_url or "").strip() or None
    link_txt = (payload.link_text or "").strip() or "Clique aqui"
    save_meta(str(media), {"kind": payload.kind, "link_url": link, "link_text": link_txt})
    return {"ok": True, "kind": payload.kind, "link_url": link, "link_text": link_txt}


@app.post("/api/videos/{name}/delete")
def api_delete_video(name: str):
    name = safe_name(name)
    media = PENDING_DIR / name
    if not media.exists():
        raise HTTPException(404, "Mídia não encontrada em pending")
    media.unlink()
    # Auxiliares: .txt, .jpg (thumb pra mp4), .meta.json
    for sib_name in [Path(name).stem + ".txt", Path(name).stem + ".jpg", name + ".meta.json"]:
        sib = PENDING_DIR / sib_name
        if sib.exists():
            sib.unlink()
    # Limpa variantes anti-cluster (variantes por conta)
    try:
        from core.anticluster import cleanup_variants_for
        cleanup_variants_for(name)
    except Exception:
        pass
    _videos_cache_invalidate()
    return {"ok": True}


@app.post("/api/videos/{name}/regenerate-thumb")
def api_regenerate_thumb(name: str):
    name = safe_name(name)
    mp4 = PENDING_DIR / name
    if not mp4.exists():
        mp4 = POSTED_DIR / name
    if not mp4.exists():
        raise HTTPException(404, "Vídeo não encontrado")
    generate_thumbnail(mp4)
    return {"ok": True}


@app.get("/api/videos/{folder}/{name}/thumb")
def api_video_thumb(folder: str, name: str):
    if folder not in ("pending", "posted"):
        raise HTTPException(404, "Pasta inválida")
    name = safe_name(name)
    base = PENDING_DIR if folder == "pending" else POSTED_DIR
    media = base / name
    ext = media.suffix.lower()
    from fastapi.responses import FileResponse

    # Foto: serve a própria foto como thumb
    if ext in PHOTO_EXTS:
        if not media.exists():
            raise HTTPException(404, "Foto não existe")
        mime = "image/png" if ext == ".png" else ("image/webp" if ext == ".webp" else "image/jpeg")
        return FileResponse(media, media_type=mime, headers={"Cache-Control": "public, max-age=86400, immutable"})

    # Vídeo: gera/serve .jpg
    thumb = media.with_suffix(".jpg")
    if not thumb.exists():
        if not media.exists():
            raise HTTPException(404, "Vídeo não existe")
        try:
            generate_thumbnail(media)
        except Exception:
            raise HTTPException(500, "Não foi possível gerar prévia")
    return FileResponse(thumb, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400, immutable"})


@app.get("/api/videos/{folder}/{name}/stream")
def api_video_stream(folder: str, name: str):
    if folder not in ("pending", "posted"):
        raise HTTPException(404, "Pasta inválida")
    name = safe_name(name)
    base = PENDING_DIR if folder == "pending" else POSTED_DIR
    media = base / name
    if not media.exists():
        raise HTTPException(404, "Mídia não encontrada")
    ext = media.suffix.lower()
    mime_map = {
        ".mp4": "video/mp4",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
    }
    from fastapi.responses import FileResponse
    return FileResponse(media, media_type=mime_map.get(ext, "application/octet-stream"))


# ---------- API: jobs ----------

class RunIn(BaseModel):
    account: Optional[str] = None
    video: Optional[str] = None
    dry_run: bool = False


@app.post("/api/jobs/run")
def api_run_post(payload: RunIn):
    args = ["post.py"]
    label_bits = []
    if payload.account and payload.account.strip():
        args += ["--conta", payload.account.strip()]
        label_bits.append(f"@{payload.account.strip()}")
    if payload.video and payload.video.strip():
        args += ["--video", payload.video.strip()]
        label_bits.append(payload.video.strip())
    if payload.dry_run:
        args.append("--dry-run")
        label_bits.append("dry-run")
    label = " · ".join(label_bits) if label_bits else "todas as contas, todos os vídeos"
    job = job_manager.start(kind="post", args=args, label=label)
    return {"ok": True, "job_id": job.id}


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str):
    ok = job_manager.cancel(job_id)
    if not ok:
        raise HTTPException(400, "Job não está em execução")
    return {"ok": True}


class JobInputIn(BaseModel):
    value: str


@app.post("/api/jobs/{job_id}/input")
def api_send_input(job_id: str, payload: JobInputIn):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado")
    if not job.send_input(payload.value):
        raise HTTPException(400, "Job não está esperando input ou já terminou")
    return {"ok": True}


@app.get("/api/jobs")
def api_list_jobs():
    return job_manager.list()


@app.get("/api/jobs/{job_id}.json")
def api_job_state(job_id: str, since: int = 0):
    job = job_manager.get(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado")
    d = job.to_dict()
    if since > 0:
        d["lines"] = d["lines"][since:]
    return JSONResponse(d)


@app.get("/api/health")
def health():
    return {"ok": True, "version": app.version, "host_ip": local_ip()}


# ---------- SCHEDULES ----------

class ScheduleIn(BaseModel):
    video: str
    account: Optional[str] = None  # None = todas as conectadas
    scheduled_at: str  # ISO; sem tz é interpretado como local


@app.get("/api/schedules")
def api_list_schedules():
    # Isolamento por workspace ativo
    from core import paths as _paths
    return schedule_manager.list(workspace_slug=_paths.get_workspace())


@app.post("/api/schedules")
def api_create_schedule(payload: ScheduleIn, user=Depends(auth.require_user)):
    # Validações
    video_name = safe_name(payload.video)
    if not (PENDING_DIR / video_name).exists():
        raise HTTPException(404, f"Vídeo '{video_name}' não está em pending")

    try:
        when = scheduler_mod.parse_iso(payload.scheduled_at)
    except Exception:
        raise HTTPException(400, "Data inválida (use ISO 8601 ou YYYY-MM-DDTHH:MM)")

    now = scheduler_mod.now_local()
    if (when - now).total_seconds() < 60:
        raise HTTPException(400, "Agendamento precisa ser pelo menos 1 minuto no futuro")

    account = (payload.account or "").strip() or None
    if account:
        accounts = load_accounts()
        acc_obj = next((a for a in accounts if a["username"] == account), None)
        if not acc_obj:
            raise HTTPException(404, f"Conta @{account} não existe")
        # FIX: aceita conta conectada via worker também (não só sessão no servidor)
        is_connected = (
            session_status(account) == "saved"
            or acc_obj.get("connected_via_worker_id")
        )
        if not is_connected:
            raise HTTPException(400, f"Conta @{account} não está conectada — conecte primeiro")

        # Conflito: 2 schedules pra MESMA conta com < 5 min de diferença
        conflicts = schedule_manager.conflicts(account, when)
        if conflicts:
            other = conflicts[0]
            raise HTTPException(
                409,
                f"Conflito: já existe agendamento pra @{account} em {other.scheduled_at[:16]} "
                f"(menos de 5 min de diferença).",
            )

    sched = schedule_manager.create(
        video=video_name,
        account=account,
        scheduled_at=when,
        created_by=user["email"],
    )
    return {"ok": True, "schedule": sched.to_dict()}


@app.post("/api/schedules/{schedule_id}/cancel")
def api_cancel_schedule(schedule_id: str):
    if not schedule_manager.cancel(schedule_id):
        raise HTTPException(400, "Schedule não pode ser cancelado (já rodou ou foi removido)")
    return {"ok": True}


@app.post("/api/schedules/{schedule_id}/delete")
def api_delete_schedule(schedule_id: str):
    if not schedule_manager.delete(schedule_id):
        raise HTTPException(404, "Schedule não encontrado")
    return {"ok": True}


# ---------- SHORTENER ----------

class LinkIn(BaseModel):
    target_url: str
    label: Optional[str] = None
    slug: Optional[str] = None  # opcional — se vazio, gera aleatório


def _link_dict_view(d: dict, request: Request) -> dict:
    """Adiciona short_url à dict de link.
    Prioriza env PUBLIC_BASE_URL (prod), senão usa base do request (dev)."""
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    d["short_url"] = f"{base}/r/{d['slug']}"
    return d


@app.get("/links", response_class=HTMLResponse)
def page_links(request: Request):
    return templates.TemplateResponse(
        request, "links.html", _ctx(request, active="links"),
    )


@app.get("/api/links")
def api_list_links(
    request: Request,
    since: Optional[str] = None,    # ISO (ex: 2026-05-20T00:00:00Z)
    until: Optional[str] = None,    # ISO (ex: 2026-05-21T00:00:00Z)
):
    """Lista links. Se since/until fornecidos, recalcula click_count_filtered
    contando só cliques na janela (mantém click_count total intacto).
    Filtra pelo workspace ativo — isolamento.

    click_count_filtered EXCLUI bots E dedupes — só cliques humanos reais.
    """
    from core import paths as _paths
    out = []
    raw_links = link_manager.list(workspace_slug=_paths.get_workspace())
    for d in raw_links:
        d = _link_dict_view(d, request)
        if since or until:
            clicks = d.get("clicks") or []
            # Só conta cliques REAIS (não-bot E não-duplicate) dentro da janela
            filt = [
                c for c in clicks
                if (not since or (c.get("ts") and c["ts"] >= since))
                and (not until or (c.get("ts") and c["ts"] <= until))
                and not c.get("is_bot")
                and not c.get("is_duplicate")
            ]
            d["click_count_filtered"] = len(filt)
            d["clicks_filtered"] = filt
        else:
            d["click_count_filtered"] = d.get("click_count", 0)
        out.append(d)
    return out


@app.post("/api/links")
def api_create_link(payload: LinkIn, request: Request, user=Depends(auth.require_user)):
    try:
        link = link_manager.create(
            target_url=payload.target_url,
            label=payload.label,
            slug=payload.slug,
            created_by=user["email"],
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "link": _link_dict_view(link.to_dict(), request)}


@app.post("/api/links/{slug}/toggle")
def api_toggle_link(slug: str):
    if link_manager.get(slug) is None:
        raise HTTPException(404, "Link não encontrado")
    active = link_manager.toggle_active(slug)
    return {"ok": True, "active": active}


@app.post("/api/links/{slug}/delete")
def api_delete_link(slug: str):
    if not link_manager.delete(slug):
        raise HTTPException(404, "Link não encontrado")
    return {"ok": True}


@app.get("/r/{slug}")
def public_redirect(slug: str, request: Request):
    """Rota pública (não exige login) — registra clique e redireciona."""
    ip = request.client.host if request.client else ""
    target = link_manager.track_click(
        slug=slug,
        ip=ip,
        user_agent=request.headers.get("user-agent", ""),
        referrer=request.headers.get("referer", ""),
    )
    if not target:
        return HTMLResponse(
            "<h1 style='font-family:system-ui;color:#333'>Link inválido ou desativado</h1>",
            status_code=404,
        )
    return RedirectResponse(target, status_code=302)


# ---------- WORKERS (admin) ----------

class WorkerIn(BaseModel):
    name: str


@app.get("/workers", response_class=HTMLResponse)
def page_workers(request: Request, owner=Depends(auth.require_owner)):
    return templates.TemplateResponse(
        request, "workers.html", _ctx(request, active="workers"),
    )


@app.get("/api/workers")
def api_list_workers(owner=Depends(auth.require_owner)):
    # Hide token na listagem geral (mostra só quando cria — pra copiar uma vez)
    return worker_manager.list(hide_token=True)


@app.post("/api/workers")
def api_create_worker(payload: WorkerIn, owner=Depends(auth.require_owner)):
    w = worker_manager.create(name=payload.name, created_by=owner["email"])
    # Retorna COM token (única vez que mostra)
    return {"ok": True, "worker": w.to_dict(hide_token=False)}


@app.post("/api/workers/{worker_id}/revoke")
def api_revoke_worker(worker_id: str, owner=Depends(auth.require_owner)):
    if not worker_manager.revoke(worker_id):
        raise HTTPException(404, "Worker não encontrado")
    return {"ok": True}


# ---------- WORKER API (consumido pelos workers — auth via X-Worker-Token) ----------

def _require_worker(request: Request):
    """Dependency que valida X-Worker-Token e retorna o Worker."""
    token = request.headers.get("X-Worker-Token", "").strip()
    if not token:
        raise HTTPException(401, "X-Worker-Token header obrigatório")
    w = worker_manager.by_token(token)
    if not w:
        raise HTTPException(401, "Token inválido")
    return w


class WorkerHeartbeatIn(BaseModel):
    platform: Optional[str] = None


@app.post("/api/worker/heartbeat")
def api_worker_heartbeat(payload: WorkerHeartbeatIn, request: Request):
    token = request.headers.get("X-Worker-Token", "").strip()
    if not token:
        raise HTTPException(401, "X-Worker-Token header obrigatório")
    ip = request.client.host if request.client else ""
    w = worker_manager.heartbeat(token=token, ip=ip, platform=payload.platform or "")
    if not w:
        raise HTTPException(401, "Token inválido")
    return {"ok": True, "worker_id": w.id, "name": w.name}


@app.get("/api/worker/jobs/next")
def api_worker_next_job(request: Request):
    w = _require_worker(request)
    job = rjob_manager.claim_next(worker_id=w.id)
    if not job:
        return {"job": None}
    # Inclui credenciais (worker precisa pra logar no Insta)
    d = job.to_dict(include_secrets=True, include_logs=False)
    # Reescreve a media_url pra apontar pra rota worker (com ?account= pra disparar variante anti-cluster)
    if job.video_name:
        d["media_url"] = (
            f"{str(request.base_url).rstrip('/')}/api/worker/media/{job.video_name}"
            f"?account={job.account_username}"
        )
    return {"job": d}


@app.get("/api/worker/media/{name}")
def api_worker_media(name: str, request: Request, account: Optional[str] = None):
    """Download de mídia pelo worker. Auth via X-Worker-Token.

    Se ?account=<username> for passado, devolve a VARIANTE única daquela conta
    (anti-cluster: cada conta recebe um arquivo levemente diferente).
    Sem ?account=, devolve o original (legacy / preview).
    """
    _require_worker(request)
    name = safe_name(name)
    # Tenta pending primeiro, depois posted (caso arquivado entre criação e execução)
    for folder in (PENDING_DIR, POSTED_DIR):
        media = folder / name
        if media.exists():
            # Se conta foi passada, gera/serve variante anti-cluster
            if account:
                try:
                    from core.anticluster import variant_for_account
                    media = variant_for_account(media, account.strip())
                except Exception as e:
                    print(f"[worker_media] anticluster falhou pra {account}/{name}: {e}")
            ext = media.suffix.lower()
            mime_map = {
                ".mp4": "video/mp4",
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp",
            }
            from fastapi.responses import FileResponse
            return FileResponse(media, media_type=mime_map.get(ext, "application/octet-stream"))
    raise HTTPException(404, "Mídia não encontrada")


class WorkerLogIn(BaseModel):
    line: str


@app.post("/api/worker/jobs/{job_id}/log")
def api_worker_job_log(job_id: str, payload: WorkerLogIn, request: Request):
    w = _require_worker(request)
    if not rjob_manager.append_log(job_id=job_id, worker_id=w.id, line=payload.line):
        raise HTTPException(404, "Job não encontrado ou não atribuído a esse worker")
    return {"ok": True}


class WorkerStepIn(BaseModel):
    step: str  # downloading | logging | posting | finishing


@app.post("/api/worker/jobs/{job_id}/step")
def api_worker_job_step(job_id: str, payload: WorkerStepIn, request: Request):
    """Worker reporta a etapa atual do job (pra Kanban granular)."""
    w = _require_worker(request)
    if not rjob_manager.set_step(job_id=job_id, worker_id=w.id, step=payload.step):
        raise HTTPException(404, "Job não encontrado, etapa inválida ou não atribuído a esse worker")
    return {"ok": True}


@app.post("/api/worker/jobs/{job_id}/start")
def api_worker_job_start(job_id: str, request: Request):
    w = _require_worker(request)
    if not rjob_manager.mark_running(job_id=job_id, worker_id=w.id):
        raise HTTPException(404, "Job não encontrado ou não atribuído a esse worker")
    return {"ok": True}


class WorkerResultIn(BaseModel):
    success: bool
    media_id: Optional[str] = None
    error_msg: Optional[str] = None
    result_data: Optional[dict] = None


@app.post("/api/worker/jobs/{job_id}/result")
def api_worker_job_result(job_id: str, payload: WorkerResultIn, request: Request):
    w = _require_worker(request)
    job = rjob_manager.get(job_id)
    # CRÍTICO pra isolamento: setta o workspace do JOB (não da sessão do request).
    # Worker tem token global e bate no endpoint sem cookie de sessão — sem isso,
    # accounts_transaction() abaixo iria escrever no accounts.json do workspace
    # DEFAULT (ou do último set_workspace da thread) em vez do dono do job.
    if job:
        from core import paths as _paths
        _paths.set_workspace(job.workspace_slug or "default")
    ok = rjob_manager.report_result(
        job_id=job_id, worker_id=w.id,
        success=payload.success,
        media_id=payload.media_id,
        error_msg=payload.error_msg,
        result_data=payload.result_data,
    )
    if not ok:
        raise HTTPException(404, "Job não encontrado ou não atribuído a esse worker")

    # Side-effects pós-resultado:
    # Failure: classifica o erro em 3 níveis (prioridade: challenge > block > session_renewal):
    #   - challenge/verificação (IG pediu code) → needs_verification=True (amarelo "VERIFICAR")
    #   - bloqueio serio (rate/disabled/banned) → blocked=True (vermelho "BLOQUEADA")
    #   - cookies expiraram (refaz Save Sessão) → needs_session_renewal=True (laranja "REFAZER SESSÃO")
    # Os 3 sao mutuamente exclusivos — UI mostra so o estado mais grave.
    if (not payload.success) and job and payload.error_msg:
        chal_pattern = _is_challenge_error(payload.error_msg)
        block_pattern = None if chal_pattern else _is_block_error(payload.error_msg)
        renewal_pattern = None if (chal_pattern or block_pattern) else _is_session_renewal_error(payload.error_msg)
        if chal_pattern or block_pattern or renewal_pattern:
            try:
                with accounts_transaction() as accounts:
                    for a in accounts:
                        if a["username"] == job.account_username:
                            now_iso = scheduler_mod.now_local().isoformat(timespec="seconds")
                            if chal_pattern:
                                a["needs_verification"] = True
                                a["verification_at"] = now_iso
                                a["verification_reason"] = f"{chal_pattern} — {payload.error_msg[:200]}"
                                print(f"[verify] @{a['username']} precisa verificação ({chal_pattern})")
                            elif block_pattern:
                                a["blocked"] = True
                                a["blocked_at"] = now_iso
                                a["blocked_reason"] = f"{block_pattern} — {payload.error_msg[:200]}"
                                print(f"[block] @{a['username']} marcada como bloqueada ({block_pattern})")
                            else:  # renewal_pattern
                                a["needs_session_renewal"] = True
                                a["session_renewal_at"] = now_iso
                                a["session_renewal_reason"] = f"{renewal_pattern} — {payload.error_msg[:160]}"
                                print(f"[session] @{a['username']} precisa refazer Save Sessão ({renewal_pattern})")
                            break
            except Exception as e:
                print(f"[worker_result] erro marcando estado: {e}")

    # 1) Sucesso em qualquer op = marca conta como "conectada via worker"
    # 2) auto_follow_back: atualiza cache seen_followers no accounts.json
    # 3) post: registra mídia em posted_media (pra dedup + sync)
    if payload.success and job:
        try:
          with accounts_transaction() as accounts:
            for a in accounts:
                if a["username"] == job.account_username:
                    a["connected_via_worker_id"] = w.id
                    a["connected_via_worker_name"] = w.name
                    a["connected_at"] = scheduler_mod.now_local().isoformat(timespec="seconds")
                    # Se estava marcada como bloqueada e voltou a funcionar, limpa
                    if a.get("blocked"):
                        a["blocked"] = False
                        a["blocked_at"] = None
                        a["blocked_reason"] = None
                        print(f"[block] @{a['username']} desmarcada (voltou a funcionar)")
                    # Mesma coisa pra needs_verification — login OK = challenge resolvido
                    if a.get("needs_verification"):
                        a["needs_verification"] = False
                        a["verification_at"] = None
                        a["verification_reason"] = None
                        print(f"[verify] @{a['username']} verificação desmarcada (login OK)")
                    # Idem pra needs_session_renewal — job sucesso = cookies funcionando
                    if a.get("needs_session_renewal"):
                        a["needs_session_renewal"] = False
                        a["session_renewal_at"] = None
                        a["session_renewal_reason"] = None
                        print(f"[session] @{a['username']} renovação desmarcada (cookies OK)")
                    if job.operation == "auto_follow_back" and payload.result_data:
                        new_seen = payload.result_data.get("seen_followers")
                        if isinstance(new_seen, list) and new_seen:
                            a["auto_follow_back_seen_followers"] = new_seen
                    if job.operation == "post" and job.video_name:
                        posted = a.get("posted_media") or []
                        # Procura entry existente pra esse video
                        existing = next((p for p in posted if p.get("name") == job.video_name), None)
                        now_iso_str = scheduler_mod.now_local().isoformat(timespec="seconds")
                        if existing:
                            # Incrementa contador de repetições
                            existing["count"] = int(existing.get("count", 1)) + 1
                            existing["posted_at"] = now_iso_str  # data do post mais recente
                            existing["media_id"] = payload.media_id  # último media_id
                        else:
                            posted.append({
                                "name": job.video_name,
                                "kind": job.kind or "reel",
                                "posted_at": now_iso_str,
                                "media_id": payload.media_id,
                                "count": 1,
                            })
                        a["posted_media"] = posted
                    # Health tracker: collect_insights → salva snapshot + analisa
                    if job.operation == "collect_insights" and payload.result_data:
                        try:
                            from web import health as _health
                            analysis = _health.record(
                                username=a["username"],
                                snapshot=payload.result_data,
                            )
                            # Atualiza campos resumidos no accounts.json
                            a["follower_count"] = int(payload.result_data.get("follower_count") or 0)
                            a["health_score"] = int(analysis.get("health_score", 50))
                            if analysis.get("suspected"):
                                # So marca se ainda nao tava marcado (preserva data original)
                                if not a.get("shadowban_suspected"):
                                    a["shadowban_suspected"] = True
                                    a["shadowban_at"] = scheduler_mod.now_local().isoformat(timespec="seconds")
                                a["shadowban_reason"] = analysis.get("reason") or "queda anormal detectada"
                            else:
                                # Se voltou ao normal, limpa
                                if a.get("shadowban_suspected"):
                                    a["shadowban_suspected"] = False
                                    a["shadowban_at"] = None
                                    a["shadowban_reason"] = None
                                    print(f"[health] @{a['username']} recuperou — desmarcada")
                        except Exception as he:
                            print(f"[health] erro analisando @{a['username']}: {he}")
                        # Se veio do sync_loop, atualiza marcador
                        if (job.created_by or "").startswith("sync:"):
                            a["sync_last_post_at"] = scheduler_mod.now_local().isoformat(timespec="seconds")
                    break
            # save automático no exit do accounts_transaction context manager
        except Exception as e:
            print(f"[worker_result] erro side-effects: {e}")

    return {"ok": True}


# ---------- REMOTE JOBS (criado pelo painel, executado por worker) ----------

class RemoteJobIn(BaseModel):
    video: str                       # nome do arquivo em pending/
    account: Optional[str] = None    # username (legacy single-conta)
    accounts: Optional[list[str]] = None  # multi-conta: cria N jobs (1 por conta)
    skip_duplicates: bool = True     # pula contas que já postaram essa mídia
    kind_override: Optional[str] = None  # "reel" | "story" — sobrescreve meta.kind do arquivo


@app.get("/api/remote-jobs")
def api_list_remote_jobs(user=Depends(auth.require_user)):
    # Filtra pelo workspace ativo na sessão — isolamento entre workspaces.
    from core import paths as _paths
    return rjob_manager.list(workspace_slug=_paths.get_workspace())


@app.get("/api/remote-jobs/{job_id}")
def api_get_remote_job(job_id: str, user=Depends(auth.require_user)):
    j = rjob_manager.get(job_id)
    if not j:
        raise HTTPException(404, "Job não encontrado")
    # Isolamento por workspace: só devolve se job pertence ao ws ativo
    from core import paths as _paths
    if j.workspace_slug != _paths.get_workspace():
        raise HTTPException(404, "Job não encontrado")
    return j.to_dict(include_secrets=False)


@app.post("/api/remote-jobs")
def api_create_remote_job(payload: RemoteJobIn, request: Request, user=Depends(auth.require_user)):
    # Valida video
    video_name = safe_name(payload.video)
    media_path = PENDING_DIR / video_name
    if not media_path.exists():
        raise HTTPException(404, f"Mídia '{video_name}' não está em pending")

    # Resolve lista de contas alvo (multi-account ou single)
    accounts = load_accounts()

    # Modo "todas conectadas via worker"
    if payload.accounts == ["__all_worker__"]:
        target_usernames = [
            a["username"] for a in accounts
            if a.get("active", True) and a.get("connected_via_worker_id")
        ]
        if not target_usernames:
            raise HTTPException(400, "Nenhuma conta conectada via worker")
    elif payload.accounts:
        target_usernames = payload.accounts
    elif payload.account:
        target_usernames = [payload.account]
    else:
        raise HTTPException(400, "Forneça 'account' ou 'accounts'")

    targets = []
    for uname in target_usernames:
        a = next((a for a in accounts if a["username"] == uname), None)
        if not a:
            raise HTTPException(404, f"Conta @{uname} não existe")
        targets.append(a)

    # Filtra duplicatas (contas que já postaram essa mídia)
    skipped = []
    if payload.skip_duplicates:
        filtered = []
        for acc in targets:
            if video_name in _account_posted_names(acc):
                skipped.append(acc["username"])
            else:
                filtered.append(acc)
        targets = filtered

    # Lê meta + caption do arquivo
    from core.poster import load_meta, detect_media_kind, load_caption
    meta = load_meta(str(media_path))
    media_type = detect_media_kind(str(media_path))
    caption = load_caption(str(media_path))

    # Resolve kind FINAL: override > meta > default por media_type
    # REGRA INVIOLÁVEL: foto SEMPRE story (Insta não aceita foto como reel)
    if media_type == "photo":
        effective_kind = "story"
        if payload.kind_override == "reel":
            raise HTTPException(400, "Foto não pode virar Reel — só Story")
    elif payload.kind_override and payload.kind_override in ("reel", "story"):
        effective_kind = payload.kind_override
    else:
        effective_kind = meta.get("kind") or "reel"

    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")

    raw_link = meta.get("link_url")
    link_text = meta.get("link_text") or "Clique aqui"
    created = []

    for acc in targets:
        # media_url COM ?account= pra disparar a variante anti-cluster por conta
        media_url = f"{base}/api/worker/media/{video_name}?account={acc['username']}"
        # Se for story+link, gera 1 short link único por conta (anti-cluster)
        link_url = raw_link
        if effective_kind == "story" and raw_link:
            try:
                shortened = link_manager.create(
                    target_url=raw_link,
                    label=f"story · @{acc['username']}",
                    account=acc["username"],
                    created_by=user["email"],
                )
                link_url = f"{base}/r/{shortened.slug}"
            except Exception as e:
                print(f"[remote_job] shortener falhou pra {acc['username']}: {e}")

        # Se conta tem auto-highlight ligado, passa o título pro worker
        highlight_title = None
        if effective_kind == "story" and acc.get("auto_highlight_enabled") and acc.get("auto_highlight_title"):
            highlight_title = acc["auto_highlight_title"]

        job = rjob_manager.create({
            "operation": "post",
            "params": {"auto_highlight_title": highlight_title} if highlight_title else {},
            "account_username": acc["username"],
            "account_password": acc["password"],
            "account_totp_secret": acc.get("totp_secret"),
            "account_proxy": acc.get("proxy"),
            "video_name": video_name,
            "media_type": media_type,
            "kind": effective_kind,  # já resolvido com override > meta > default
            "caption": caption,
            "link_url": link_url,
            "link_text": link_text,
            "media_url": media_url,
            "created_by": user["email"],
        })
        created.append(job.id)

    return {
        "ok": True,
        "count": len(created),
        "job_ids": created,
        "first_job_id": created[0] if created else None,
        "skipped": skipped,
    }


# ---------- DISPARO DIVERSIFICADO (1 vídeo único por conta) ----------

class DiversifiedDispatchIn(BaseModel):
    accounts: Optional[list[str]] = None  # se None, usa todas ativas+worker-conectadas
    videos: Optional[list[str]] = None    # se None, usa todos os pendentes (pool)
    max_per_account: int = 1              # quantos posts criar por conta (default 1)
    kind_filter: str = "all"              # 'all' | 'reel' | 'story'


def _build_pending_pool(only_names: Optional[list[str]] = None, kind_filter: str = "all") -> list[str]:
    """Lista vídeos em pending/ ordenados cronologicamente (mais antigos primeiro).

    kind_filter: 'all' | 'reel' | 'story' — filtra o pool por tipo.
    """
    from core.poster import load_meta
    items = []
    if not PENDING_DIR.exists():
        return []
    for p in PENDING_DIR.iterdir():
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf not in MEDIA_EXTS:
            continue
        if p.name.endswith(".meta.json"):
            continue
        if _is_video_thumb(p):
            continue
        if only_names and p.name not in only_names:
            continue
        if kind_filter and kind_filter != "all":
            try:
                m = load_meta(str(p))
                k = m.get("kind") or ("story" if suf in PHOTO_EXTS else "reel")
                if k != kind_filter:
                    continue
            except Exception:
                pass
        items.append((p.name, p.stat().st_mtime))
    items.sort(key=lambda x: x[1])
    return [n for n, _ in items]


@app.post("/api/remote-jobs/dispatch-diversified")
def api_dispatch_diversified(payload: DiversifiedDispatchIn, request: Request, user=Depends(auth.require_user)):
    """Distribui vídeos DIFERENTES entre as contas.

    Algoritmo:
      - Pool = vídeos do payload.videos (ou todos os pending)
      - Pra cada conta, escolhe o próximo vídeo do pool que ela NÃO postou
        (sequencial pela ordem cronológica do pool)
      - Não repete vídeo na mesma rodada (cada vídeo usado 1x antes de reciclar)
      - Se todas as contas já postaram todos os vídeos do pool → retorna
        all_completed=true e count=0 (sem criar nada)
    """
    accounts = load_accounts()

    # Lista de contas (contas em sync ficam isoladas — não entram aqui)
    if payload.accounts:
        target_usernames = payload.accounts
    else:
        target_usernames = [
            a["username"] for a in accounts
            if a.get("active", True)
            and a.get("connected_via_worker_id")
            and not a.get("sync_enabled")
        ]
    if not target_usernames:
        raise HTTPException(400, "Nenhuma conta selecionada / conectada via worker (contas em sync mode são isoladas)")

    targets = []
    for uname in target_usernames:
        a = next((a for a in accounts if a["username"] == uname), None)
        if not a:
            raise HTTPException(404, f"Conta @{uname} não existe")
        targets.append(a)

    # Pool de vídeos
    only_names = None
    if payload.videos:
        only_names = [safe_name(v) for v in payload.videos]
    pool = _build_pending_pool(only_names=only_names, kind_filter=payload.kind_filter or "all")
    if not pool:
        raise HTTPException(400, "Nenhum vídeo pendente disponível")

    # Estado: vídeos já usados nesta rodada (pra não dar o mesmo vídeo pra contas diferentes
    # no mesmo disparo). Se acabar antes das contas, recicla.
    used_in_round: set[str] = set()
    assignments: list[tuple[dict, str]] = []  # (account, video_name)
    accounts_completed: list[str] = []         # contas que ja postaram tudo do pool

    max_per_acc = max(1, min(20, int(payload.max_per_account or 1)))

    # ANTI-DUPLICATE: jobs em pending/claimed/running ATUAIS contam como "já pegos"
    # pra cada conta, evitando criar 2x o mesmo vídeo na fila enquanto worker
    # ainda não terminou o primeiro. Scoped por workspace pra não cruzar dados.
    from core import paths as _paths
    _ws_now = _paths.get_workspace()
    pending_per_acc: dict[str, set[str]] = {}
    for j in rjob_manager._items.values():
        if (
            j.operation == "post"
            and j.status in ("pending", "claimed", "running")
            and j.video_name
            and j.workspace_slug == _ws_now
        ):
            pending_per_acc.setdefault(j.account_username, set()).add(j.video_name)

    # ROTAÇÃO REAL: cada conta começa em posição DIFERENTE do pool (circular).
    # Resultado: contas pegam vídeos diferentes em ordens diferentes a cada rodada.
    targets_sorted = sorted(targets, key=lambda a: a.get("username", ""))
    pool_len = len(pool)

    for idx, acc in enumerate(targets_sorted):
        posted_count = len(acc.get("posted_media") or [])
        already = _account_posted_names(acc)
        in_flight = pending_per_acc.get(acc["username"], set())
        forbidden = already | in_flight

        start_offset = (posted_count + idx) % max(1, pool_len)

        for slot in range(max_per_acc):
            chosen = None
            # 1ª tentativa: ainda não usado na rodada E permitido pra essa conta
            for step in range(pool_len):
                candidate = pool[(start_offset + step) % pool_len]
                if candidate in forbidden:
                    continue
                if candidate not in used_in_round:
                    chosen = candidate
                    break
            # 2ª tentativa: aceita reciclar (já usado na rodada por outra conta)
            if chosen is None:
                for step in range(pool_len):
                    candidate = pool[(start_offset + step) % pool_len]
                    if candidate not in forbidden:
                        chosen = candidate
                        break

            if chosen is None:
                # Conta postou TUDO que o pool oferece — completed
                if acc["username"] not in accounts_completed:
                    accounts_completed.append(acc["username"])
                break

            used_in_round.add(chosen)
            forbidden.add(chosen)
            assignments.append((acc, chosen))
            start_offset = (start_offset + 1) % max(1, pool_len)

    if not assignments:
        # Todas as contas já postaram todos os vídeos do pool — sinal de reset
        return {
            "ok": True,
            "count": 0,
            "all_completed": True,
            "pool_size": len(pool),
            "accounts_count": len(targets),
            "accounts_completed": [a["username"] for a in targets],
            "message": "Todas as contas já postaram todos os vídeos do pool. Use /api/remote-jobs/reset-posted pra começar de novo.",
        }

    # Cria 1 remote_job por assignment, com stagger (1 job/min) pra ritmo humano
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    from core.poster import load_meta, detect_media_kind, load_caption

    stagger = _stagger_times(len(assignments), seconds_per_job=60)
    created = []
    by_account: dict[str, list[str]] = {}
    for idx, (acc, video_name) in enumerate(assignments):
        media_path = PENDING_DIR / video_name
        meta = load_meta(str(media_path))
        media_type = detect_media_kind(str(media_path))
        caption = load_caption(str(media_path))
        link_url = meta.get("link_url")
        link_text = meta.get("link_text") or "Clique aqui"

        # Anti-cluster: 1 short URL único por conta se for story+link
        if meta.get("kind") == "story" and link_url:
            try:
                shortened = link_manager.create(
                    target_url=link_url,
                    label=f"diversificado · @{acc['username']}",
                    account=acc["username"],
                    created_by=user["email"],
                )
                link_url = f"{base}/r/{shortened.slug}"
            except Exception as e:
                print(f"[diversified] shortener falhou pra {acc['username']}: {e}")

        highlight_title = None
        if meta.get("kind") == "story" and acc.get("auto_highlight_enabled") and acc.get("auto_highlight_title"):
            highlight_title = acc["auto_highlight_title"]

        media_url = f"{base}/api/worker/media/{video_name}?account={acc['username']}"
        job = rjob_manager.create({
            "operation": "post",
            "params": {"auto_highlight_title": highlight_title} if highlight_title else {},
            "account_username": acc["username"],
            "account_password": acc["password"],
            "account_totp_secret": acc.get("totp_secret"),
            "account_proxy": acc.get("proxy"),
            "video_name": video_name,
            "media_type": media_type,
            # FIX: foto SEMPRE vira story (regra inviolável do Instagram)
            "kind": "story" if media_type == "photo" else (meta.get("kind") or "reel"),
            "caption": caption,
            "link_url": link_url,
            "link_text": link_text,
            "media_url": media_url,
            "created_by": f"diversified:{user['email']}",
            "scheduled_for": stagger[idx] if idx < len(stagger) else None,
        })
        created.append(job.id)
        by_account.setdefault(acc["username"], []).append(video_name)

    return {
        "ok": True,
        "count": len(created),
        "all_completed": False,
        "job_ids": created,
        "pool_size": len(pool),
        "accounts_count": len(targets),
        "accounts_completed": accounts_completed,
        "assignments": by_account,
    }


class ResetPostedIn(BaseModel):
    accounts: Optional[list[str]] = None  # se None E only_completed=False, reseta TODAS
    only_completed: bool = True           # default: só reseta quem já postou TUDO do pool


# ---------- AUTO-LOOP DE DISPARO DIVERSIFICADO (por workspace) ----------

from web import diversify as _diversify  # noqa: E402


class DiversifySettingsIn(BaseModel):
    enabled: Optional[bool] = None
    interval_hours: Optional[int] = None
    max_per_account: Optional[int] = None
    kind_filter: Optional[str] = None  # 'all' | 'reel' | 'story'
    repetitions_per_video: Optional[int] = None  # 1-10 (default 3: 3x mesmo vídeo na conta)
    new_account_threshold_hours: Optional[int] = None  # 1-168 (default 24h: conta < 24h = nova)
    new_account_interval_hours: Optional[int] = None  # 1-72 (default 6h ritmo nova)


@app.get("/api/diversify-loop")
def api_diversify_settings(user=Depends(auth.require_user)):
    """Retorna settings do auto-loop pro workspace ativo."""
    settings = _diversify.load()
    # Calcula próxima rodada estimada
    next_run_at = None
    if settings.get("enabled") and settings.get("last_run_at"):
        try:
            from datetime import datetime as _dt, timedelta as _td
            last = _dt.fromisoformat(settings["last_run_at"])
            next_dt = last + _td(hours=int(settings.get("interval_hours", 6)))
            next_run_at = next_dt.isoformat(timespec="seconds")
        except Exception:
            pass
    return {**settings, "next_run_at": next_run_at}


@app.post("/api/diversify-loop")
def api_diversify_settings_save(payload: DiversifySettingsIn, user=Depends(auth.require_user)):
    """Atualiza settings do auto-loop pro workspace ativo."""
    update = {}
    if payload.enabled is not None:
        update["enabled"] = bool(payload.enabled)
        # Quando religa, zera completed_at pra o loop não pular
        if payload.enabled:
            update["completed_at"] = None
    if payload.interval_hours is not None:
        update["interval_hours"] = int(payload.interval_hours)
    if payload.max_per_account is not None:
        update["max_per_account"] = int(payload.max_per_account)
    if payload.kind_filter is not None:
        update["kind_filter"] = payload.kind_filter
    if payload.repetitions_per_video is not None:
        update["repetitions_per_video"] = int(payload.repetitions_per_video)
    if payload.new_account_threshold_hours is not None:
        update["new_account_threshold_hours"] = int(payload.new_account_threshold_hours)
    if payload.new_account_interval_hours is not None:
        update["new_account_interval_hours"] = int(payload.new_account_interval_hours)
    settings = _diversify.save(update)
    return {"ok": True, "settings": settings}


@app.post("/api/remote-jobs/reset-posted")
def api_reset_posted(payload: ResetPostedIn, user=Depends(auth.require_user)):
    """Zera o posted_media de N contas — usado pra 'reiniciar a rodada do zero'.

    DEFAULT (only_completed=true): zera SOMENTE contas que ja postaram TUDO
    do pool atual. Contas com posts parciais (incluindo novas que ja postaram
    algo) ficam intactas, continuam de onde pararam.

    Pass only_completed=false pra forcar reset de TODAS as contas selecionadas
    (cuidado: pode fazer conta nova re-postar o que ja postou).
    """
    accounts = load_accounts()

    # Calcula tamanho do pool atual (pra detectar quem completou)
    pool = _build_pending_pool()
    pool_set = set(pool)
    pool_size = len(pool_set)

    target_usernames = payload.accounts
    if not target_usernames:
        target_usernames = [a["username"] for a in accounts if a.get("active", True)]
    target_set = set(target_usernames)

    reset = []
    skipped_partial = []
    for a in accounts:
        if a["username"] not in target_set:
            continue
        had_count = len(a.get("posted_media") or [])

        if payload.only_completed:
            # Conta completou se tem posted_media >= pool_size E
            # cobre todos os videos do pool atual
            posted_names = _account_posted_names(a)
            covers_pool = pool_set.issubset(posted_names) and pool_size > 0
            if not covers_pool:
                skipped_partial.append({
                    "username": a["username"],
                    "posted": had_count,
                    "pool_size": pool_size,
                })
                continue

        a["posted_media"] = []
        # Tambem zera sync_completed pra permitir backfill de novo
        if a.get("sync_completed"):
            a["sync_completed"] = False
        reset.append({"username": a["username"], "cleared": had_count})
    save_accounts(accounts)
    return {
        "ok": True,
        "count": len(reset),
        "reset": reset,
        "skipped_partial": skipped_partial,
        "only_completed_mode": payload.only_completed,
        "pool_size": pool_size,
    }


def _check_job_ws_or_404(job_id: str) -> None:
    """Isolamento: cancel/delete/requeue só funcionam pra jobs do ws ativo.
    Evita user de ws A mexer em jobs de ws B mesmo que tenha o ID."""
    j = rjob_manager.get(job_id)
    if not j:
        raise HTTPException(404, "Job não encontrado")
    from core import paths as _paths
    if j.workspace_slug != _paths.get_workspace():
        raise HTTPException(404, "Job não encontrado")


@app.post("/api/remote-jobs/{job_id}/cancel")
def api_cancel_remote_job(job_id: str, user=Depends(auth.require_user)):
    _check_job_ws_or_404(job_id)
    if not rjob_manager.cancel(job_id):
        raise HTTPException(400, "Job não pode ser cancelado (já rodou ou foi removido)")
    return {"ok": True}


@app.post("/api/remote-jobs/{job_id}/delete")
def api_delete_remote_job(job_id: str, user=Depends(auth.require_user)):
    _check_job_ws_or_404(job_id)
    if not rjob_manager.delete(job_id):
        raise HTTPException(404, "Job não encontrado")
    return {"ok": True}


@app.post("/api/remote-jobs/{job_id}/requeue")
def api_requeue_remote_job(job_id: str, user=Depends(auth.require_user)):
    """Forca 1 job de volta pra fila (limpa scheduled_for + worker_id)."""
    _check_job_ws_or_404(job_id)
    if not rjob_manager.requeue(job_id):
        raise HTTPException(404, "Job não encontrado")
    return {"ok": True}


@app.post("/api/remote-jobs/requeue-stuck")
def api_requeue_stuck(user=Depends(auth.require_user)):
    """Re-enfileira TODOS os jobs presos (stagger futuro, claimed zumbi, pending velho).
    Scoped no ws ativo — não afeta outros workspaces."""
    from core import paths as _paths
    count = rjob_manager.requeue_stuck(workspace_slug=_paths.get_workspace())
    return {"ok": True, "requeued": count}


@app.post("/api/remote-jobs/dedupe")
def api_dedupe_pending(user=Depends(auth.require_user)):
    """Remove jobs duplicados na fila: pra cada (conta, video), mantém apenas 1.
    Scoped no ws ativo — não afeta outros workspaces."""
    from core import paths as _paths
    count = rjob_manager.dedupe_pending(workspace_slug=_paths.get_workspace())
    return {"ok": True, "removed": count}


# ---------- FINANÇAS ----------

class FinanceIn(BaseModel):
    type: str  # "custo" | "venda"
    category: str
    amount: float
    description: Optional[str] = ""
    date: Optional[str] = None  # YYYY-MM-DD; default hoje
    notes: Optional[str] = None


@app.get("/financeiro", response_class=HTMLResponse)
def page_finance(request: Request, user=Depends(auth.require_user)):
    return templates.TemplateResponse(
        request,
        "financeiro.html",
        _ctx(request, active="financeiro", categories=FINANCE_CATEGORIES),
    )


@app.get("/api/finance")
def api_finance_list(
    type: Optional[str] = None,
    month: Optional[str] = None,
    user=Depends(auth.require_user),
):
    return finance_manager.list(type_filter=type, month=month)


@app.get("/api/finance/summary")
def api_finance_summary(month: Optional[str] = None, user=Depends(auth.require_user)):
    return finance_manager.summary(month=month)


@app.post("/api/finance")
def api_finance_create(payload: FinanceIn, user=Depends(auth.require_user)):
    try:
        entry = finance_manager.create(payload.model_dump(), created_by=user["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "entry": entry.to_dict()}


@app.post("/api/finance/{entry_id}/delete")
def api_finance_delete(entry_id: str, user=Depends(auth.require_user)):
    if not finance_manager.delete(entry_id):
        raise HTTPException(404, "Lançamento não encontrado")
    return {"ok": True}


# ---------- AUTH: pages ----------

@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request, next: str = "/", error: Optional[str] = None):
    if request.session.get("email") and auth.find_user(request.session["email"]):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next, "error": error, "user": None, "is_owner": False, "active": None},
    )


@app.post("/login")
def do_login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/")):
    u = auth.find_user(email)
    if not u or not auth.verify_password(password, u.get("password", {})):
        return RedirectResponse(f"/login?error=invalid&next={next or '/'}", status_code=303)
    request.session["email"] = u["email"]
    auth.update_last_seen(u["email"])
    return RedirectResponse(next or "/", status_code=303)


@app.post("/logout")
def do_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/signup/{token}", response_class=HTMLResponse)
def page_signup(request: Request, token: str, error: Optional[str] = None):
    inv = auth.find_invite(token)
    if not inv:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"token": None, "invite": None, "error": "Convite inválido ou expirado", "user": None, "is_owner": False, "active": None},
        )
    if inv.get("used_at"):
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"token": None, "invite": None, "error": "Esse convite já foi usado", "user": None, "is_owner": False, "active": None},
        )
    return templates.TemplateResponse(
        request,
        "signup.html",
        {"token": token, "invite": inv, "error": error, "user": None, "is_owner": False, "active": None},
    )


@app.post("/signup/{token}")
def do_signup(request: Request, token: str, email: str = Form(...), password: str = Form(...)):
    inv = auth.find_invite(token)
    if not inv or inv.get("used_at"):
        return RedirectResponse(f"/signup/{token}?error=convite-invalido", status_code=303)
    try:
        user = auth.create_user(email, password, role=inv.get("role", "member"), invited_by=inv.get("created_by"))
    except ValueError as e:
        return RedirectResponse(f"/signup/{token}?error={e}", status_code=303)
    auth.consume_invite(token, user["email"])
    request.session["email"] = user["email"]
    auth.update_last_seen(user["email"])
    return RedirectResponse("/", status_code=303)


# ---------- TEAM (owner-only) ----------

@app.get("/team", response_class=HTMLResponse)
def page_team(request: Request, owner=Depends(auth.require_owner)):
    return templates.TemplateResponse(
        request, "team.html", _ctx(request, active="team"),
    )


@app.get("/api/team")
def api_team(request: Request, owner=Depends(auth.require_owner)):
    users = [auth.public_user(u) for u in auth.load_users()]
    invites = [
        {
            "token": i["token"],
            "created_by": i["created_by"],
            "created_at": i["created_at"],
            "used_at": i.get("used_at"),
            "used_by": i.get("used_by"),
            "role": i.get("role", "member"),
        }
        for i in auth.load_invites()
        if not i.get("used_at")
    ]
    return {"members": users, "invites": invites, "owner_email": owner["email"]}


@app.post("/api/team/invite")
def api_create_invite(request: Request, owner=Depends(auth.require_owner)):
    inv = auth.create_invite(owner["email"], role="member")
    base = str(request.base_url).rstrip("/")
    return {
        "ok": True,
        "token": inv["token"],
        "link": f"{base}/signup/{inv['token']}",
    }


@app.post("/api/team/invite/{token}/revoke")
def api_revoke_invite(token: str, owner=Depends(auth.require_owner)):
    ok = auth.revoke_invite(token)
    if not ok:
        raise HTTPException(404, "Convite não encontrado")
    return {"ok": True}


@app.post("/api/team/member/{email}/delete")
def api_delete_member(email: str, owner=Depends(auth.require_owner)):
    if email.lower() == owner["email"].lower():
        raise HTTPException(400, "Não dá pra remover o próprio owner")
    target = auth.find_user(email)
    if not target:
        raise HTTPException(404, "Usuário não encontrado")
    if target.get("role") == "owner":
        raise HTTPException(400, "Não dá pra remover outro owner por aqui")
    auth.delete_user(email)
    return {"ok": True}


# ---------- WORKSPACES ----------

from web.workspaces import manager as ws_manager  # noqa: E402


class WorkspaceCreateIn(BaseModel):
    name: str
    slug: Optional[str] = None


class WorkspaceRenameIn(BaseModel):
    name: str


class WorkspaceSwitchIn(BaseModel):
    slug: str


@app.get("/workspaces", response_class=HTMLResponse)
def page_workspaces(request: Request, owner=Depends(auth.require_owner)):
    return templates.TemplateResponse(
        request, "workspaces.html", _ctx(request, active="workspaces"),
    )


@app.get("/api/workspaces")
def api_list_workspaces(user=Depends(auth.require_user)):
    return ws_manager.list()


@app.post("/api/workspaces")
def api_create_workspace(payload: WorkspaceCreateIn, owner=Depends(auth.require_owner)):
    try:
        ws = ws_manager.create(name=payload.name, slug=payload.slug, created_by=owner["email"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "workspace": ws.to_dict()}


@app.post("/api/workspaces/{slug}/rename")
def api_rename_workspace(slug: str, payload: WorkspaceRenameIn, owner=Depends(auth.require_owner)):
    try:
        ws = ws_manager.rename(slug, payload.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "workspace": ws.to_dict()}


@app.post("/api/workspaces/{slug}/delete")
def api_delete_workspace(slug: str, owner=Depends(auth.require_owner)):
    try:
        ok = ws_manager.delete(slug)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "Workspace não encontrado")
    return {"ok": True}


@app.post("/api/workspaces/switch")
def api_switch_workspace(payload: WorkspaceSwitchIn, request: Request, user=Depends(auth.require_user)):
    """Define o workspace ativo na sessão do user."""
    if not ws_manager.exists(payload.slug):
        raise HTTPException(404, f"Workspace '{payload.slug}' não existe")
    request.session["workspace"] = payload.slug
    return {"ok": True, "workspace": payload.slug}
