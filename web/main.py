"""
Web UI do Insta Poster — FastAPI.

Roda em http://localhost:8000 (e também responde no IP local da máquina,
útil pra abrir do celular na mesma rede).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
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
from core.media import generate_thumbnail  # noqa: E402
from core.paths import (  # noqa: E402
    ACCOUNTS_FILE, PENDING_DIR, POSTED_DIR, SESSIONS_DIR, LOGS_DIR,
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
PUBLIC_PATH_PREFIXES = ("/login", "/signup/", "/static/", "/api/health", "/r/", "/api/worker/")


class RequireLoginMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
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
auth.ensure_owner_seed()


def _ctx(request: Request, **extra) -> dict:
    u = auth.current_user(request)
    base = {
        "user": auth.public_user(u) if u else None,
        "is_owner": bool(u and u.get("role") == "owner"),
    }
    base.update(extra)
    return base


# ---------- helpers ----------

VALID_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def load_accounts() -> list[dict]:
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_accounts(accounts: list[dict]) -> None:
    ACCOUNTS_FILE.write_text(
        json.dumps(accounts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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


def list_videos(folder: Path) -> list[dict]:
    """Lista todas as mídias da pasta (vídeo + foto)."""
    from core.poster import load_meta, detect_media_kind
    folder_key = folder.name  # "pending" ou "posted"
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
        })
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


# ---------- pages ----------

@app.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request):
    accounts = load_accounts()
    pending = list_videos(PENDING_DIR)
    posted = list_videos(POSTED_DIR)
    jobs = job_manager.list()[:10]

    active_accounts = [a for a in accounts if a.get("active", True)]
    connected_accounts = [a for a in active_accounts if session_status(a["username"]) == "saved"]

    # Próximos agendamentos (status pending)
    all_schedules = schedule_manager.list()
    upcoming_schedules = [s for s in all_schedules if s["status"] == "pending"][:5]
    upcoming_count = sum(1 for s in all_schedules if s["status"] == "pending")

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
    connected = [
        a["username"] for a in accounts
        if a.get("active", True) and session_status(a["username"]) == "saved"
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
    }


@app.get("/api/accounts")
def api_list_accounts():
    return [_account_view(a) for a in load_accounts()]


@app.post("/api/accounts")
def api_add_account(payload: AccountIn):
    username = payload.username.strip()
    if not username or not payload.password:
        raise HTTPException(400, "username e password obrigatórios")

    accounts = load_accounts()
    if any(a["username"].lower() == username.lower() for a in accounts):
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
    accounts = load_accounts()
    for a in accounts:
        if a["username"] == username:
            a["totp_secret"] = (payload.totp_secret or "").strip() or None
            save_accounts(accounts)
            return {"ok": True, "has_totp": bool(a["totp_secret"])}
    raise HTTPException(404, "Conta não encontrada")


@app.get("/api/accounts/{username}/totp-code")
def api_show_totp(username: str):
    accounts = load_accounts()
    for a in accounts:
        if a["username"] == username:
            secret = a.get("totp_secret")
            if not secret:
                raise HTTPException(400, "Essa conta não tem chave 2FA cadastrada")
            try:
                from instagrapi import Client
                import time as _time
                code = Client().totp_generate_code(secret.replace(" ", "").replace("-", "").upper())
                # TOTP padrão tem janela de 30s; calcula quanto falta
                seconds_left = 30 - int(_time.time()) % 30
                return {"code": code, "seconds_left": seconds_left}
            except Exception as e:
                raise HTTPException(400, f"Chave inválida: {e}")
    raise HTTPException(404, "Conta não encontrada")


@app.post("/api/accounts/{username}/toggle")
def api_toggle_account(username: str):
    accounts = load_accounts()
    for a in accounts:
        if a["username"] == username:
            a["active"] = not a.get("active", True)
            save_accounts(accounts)
            return {"ok": True, "active": a["active"]}
    raise HTTPException(404, "Conta não encontrada")


@app.post("/api/accounts/{username}/delete")
def api_delete_account(username: str):
    accounts = load_accounts()
    new = [a for a in accounts if a["username"] != username]
    if len(new) == len(accounts):
        raise HTTPException(404, "Conta não encontrada")
    save_accounts(new)
    session_file = SESSIONS_DIR / f"{username}.json"
    if session_file.exists():
        session_file.unlink()
    return {"ok": True}


@app.post("/api/accounts/bulk-delete")
def api_bulk_delete(payload: BulkUsernames):
    accounts = load_accounts()
    targets = set(payload.usernames)
    new = [a for a in accounts if a["username"] not in targets]
    save_accounts(new)
    removed = []
    for u in targets:
        f = SESSIONS_DIR / f"{u}.json"
        if f.exists():
            f.unlink()
        removed.append(u)
    return {"ok": True, "removed": removed, "remaining": len(new)}


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


# ---------- API: videos ----------

@app.get("/api/videos")
def api_list_videos():
    return {
        "pending": list_videos(PENDING_DIR),
        "posted": list_videos(POSTED_DIR),
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

    # Thumbnail só pra vídeo (foto já é a própria thumb)
    if ext in VIDEO_EXTS:
        try:
            generate_thumbnail(target)
        except Exception as e:
            print(f"[upload] thumb falhou: {e}")

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
    save_meta(str(media), {"kind": payload.kind, "link_url": link})
    return {"ok": True, "kind": payload.kind, "link_url": link}


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
        return FileResponse(media, media_type=mime, headers={"Cache-Control": "public, max-age=3600"})

    # Vídeo: gera/serve .jpg
    thumb = media.with_suffix(".jpg")
    if not thumb.exists():
        if not media.exists():
            raise HTTPException(404, "Vídeo não existe")
        try:
            generate_thumbnail(media)
        except Exception:
            raise HTTPException(500, "Não foi possível gerar prévia")
    return FileResponse(thumb, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})


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
    return schedule_manager.list()


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
        if not any(a["username"] == account for a in accounts):
            raise HTTPException(404, f"Conta @{account} não existe")
        if session_status(account) != "saved":
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
def api_list_links(request: Request):
    return [_link_dict_view(d, request) for d in link_manager.list()]


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
    # Reescreve a media_url pra apontar pra rota worker (que aceita X-Worker-Token)
    d["media_url"] = f"{str(request.base_url).rstrip('/')}/api/worker/media/{job.video_name}"
    return {"job": d}


@app.get("/api/worker/media/{name}")
def api_worker_media(name: str, request: Request):
    """Download de mídia pelo worker. Auth via X-Worker-Token."""
    _require_worker(request)
    name = safe_name(name)
    # Tenta pending primeiro, depois posted (caso arquivado entre criação e execução)
    for folder in (PENDING_DIR, POSTED_DIR):
        media = folder / name
        if media.exists():
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


@app.post("/api/worker/jobs/{job_id}/result")
def api_worker_job_result(job_id: str, payload: WorkerResultIn, request: Request):
    w = _require_worker(request)
    job = rjob_manager.get(job_id)
    ok = rjob_manager.report_result(
        job_id=job_id, worker_id=w.id,
        success=payload.success,
        media_id=payload.media_id,
        error_msg=payload.error_msg,
    )
    if not ok:
        raise HTTPException(404, "Job não encontrado ou não atribuído a esse worker")

    # Side-effect: marca conta como "conectada via worker" quando o
    # worker reporta sucesso em test_login ou em qualquer post.
    # Assim o painel /accounts mostra status correto mesmo sem sessão na VPS.
    if payload.success and job:
        try:
            accounts = load_accounts()
            for a in accounts:
                if a["username"] == job.account_username:
                    a["connected_via_worker_id"] = w.id
                    a["connected_via_worker_name"] = w.name
                    a["connected_at"] = scheduler_mod.now_local().isoformat(timespec="seconds")
                    save_accounts(accounts)
                    break
        except Exception as e:
            print(f"[worker_result] erro marcando conta conectada: {e}")

    return {"ok": True}


# ---------- REMOTE JOBS (criado pelo painel, executado por worker) ----------

class RemoteJobIn(BaseModel):
    video: str               # nome do arquivo em pending/
    account: str             # username Instagram


@app.get("/api/remote-jobs")
def api_list_remote_jobs(user=Depends(auth.require_user)):
    return rjob_manager.list()


@app.get("/api/remote-jobs/{job_id}")
def api_get_remote_job(job_id: str, user=Depends(auth.require_user)):
    j = rjob_manager.get(job_id)
    if not j:
        raise HTTPException(404, "Job não encontrado")
    return j.to_dict(include_secrets=False)


@app.post("/api/remote-jobs")
def api_create_remote_job(payload: RemoteJobIn, request: Request, user=Depends(auth.require_user)):
    # Valida video
    video_name = safe_name(payload.video)
    media_path = PENDING_DIR / video_name
    if not media_path.exists():
        raise HTTPException(404, f"Mídia '{video_name}' não está em pending")

    # Valida conta
    accounts = load_accounts()
    account = next((a for a in accounts if a["username"] == payload.account), None)
    if not account:
        raise HTTPException(404, f"Conta @{payload.account} não existe")

    # Lê meta + caption do arquivo
    from core.poster import load_meta, detect_media_kind, load_caption
    meta = load_meta(str(media_path))
    media_type = detect_media_kind(str(media_path))
    caption = load_caption(str(media_path))

    # Monta URL pública pro worker baixar a mídia
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    media_url = f"{base}/api/videos/pending/{video_name}/stream"

    # Se for story+link, gera 1 short link pra essa conta (anti-cluster)
    link_url = meta.get("link_url")
    if meta.get("kind") == "story" and link_url:
        try:
            shortened = link_manager.create(
                target_url=link_url,
                label=f"story · @{payload.account}",
                account=payload.account,
                created_by=user["email"],
            )
            link_url = f"{base}/r/{shortened.slug}"
        except Exception as e:
            print(f"[remote_job] shortener falhou: {e} — usando URL original")

    job = rjob_manager.create({
        "account_username": account["username"],
        "account_password": account["password"],
        "account_totp_secret": account.get("totp_secret"),
        "account_proxy": account.get("proxy"),
        "video_name": video_name,
        "media_type": media_type,
        "kind": meta.get("kind", "reel"),
        "caption": caption,
        "link_url": link_url,
        "media_url": media_url,
        "created_by": user["email"],
    })
    return {"ok": True, "job": job.to_dict(include_secrets=False)}


@app.post("/api/remote-jobs/{job_id}/cancel")
def api_cancel_remote_job(job_id: str, user=Depends(auth.require_user)):
    if not rjob_manager.cancel(job_id):
        raise HTTPException(400, "Job não pode ser cancelado (já rodou ou foi removido)")
    return {"ok": True}


@app.post("/api/remote-jobs/{job_id}/delete")
def api_delete_remote_job(job_id: str, user=Depends(auth.require_user)):
    if not rjob_manager.delete(job_id):
        raise HTTPException(404, "Job não encontrado")
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
