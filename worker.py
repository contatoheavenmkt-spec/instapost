"""
Worker do Insta Poster — roda no PC de cada membro da equipe.

Conecta no servidor central via HTTPS, pega jobs da fila, executa
o login Instagram + postagem usando o IP residencial local, e reporta
o resultado de volta.

Config via variáveis de ambiente OU arquivo .env ao lado deste script:

    SERVER_URL=https://instapost.shop
    WORKER_TOKEN=seu-token-gerado-no-painel
    WORKER_NAME="Nome opcional do PC"   # default: hostname

Uso:
    pip install -r worker-requirements.txt
    python worker.py

Mantém rodando enquanto quiser receber jobs. Ctrl+C pra parar.
"""
from __future__ import annotations

import json
import os
import platform
import random
import socket
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import requests

# Lê .env ao lado do script (sem depender de python-dotenv)
# Usa utf-8-sig pra tolerar arquivos com BOM (PowerShell Out-File -Encoding utf8 adiciona)
ENV_FILE = Path(__file__).resolve().parent / ".env"
if ENV_FILE.exists():
    try:
        text = ENV_FILE.read_text(encoding="utf-8-sig")
    except Exception:
        text = ENV_FILE.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")  # tira BOM se sobrou
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SERVER_URL = os.environ.get("SERVER_URL", "").rstrip("/")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "").strip()
WORKER_NAME = os.environ.get("WORKER_NAME", "").strip() or socket.gethostname()
PLATFORM = f"{platform.system()} {platform.release()}"

POLL_INTERVAL_SECONDS = 5
HEARTBEAT_INTERVAL_SECONDS = 30
HTTP_TIMEOUT = 30

# Quantas threads processam jobs em paralelo. Default 1 (serial — modo seguro).
# Subir pra 2 acelera ~2x quando há contas diferentes na fila, com risco moderado de
# flag por logins simultâneos do mesmo IP. Nunca processa 2 jobs da MESMA conta em
# paralelo (lock por username garante isso).
WORKER_CONCURRENCY = max(1, min(3, int(os.environ.get("WORKER_CONCURRENCY", "1"))))

# Cache do Client instagrapi por conta. Se o próximo job for da mesma @, reusa o
# Client em vez de fazer load_settings + get_timeline_feed de novo (economiza 3-8s).
CLIENT_CACHE_TTL_SECONDS = 10 * 60

# Diretório temp pra baixar mídia
TMP_DIR = Path(__file__).resolve().parent / ".worker_tmp"
TMP_DIR.mkdir(exist_ok=True)

# Estado global compartilhado entre threads de jobs
_stop_flag = threading.Event()
_account_locks: dict[str, threading.Lock] = {}
_account_locks_meta_lock = threading.Lock()
_client_cache: dict[str, tuple[object, float]] = {}  # username -> (Client, last_used_ts)
_client_cache_lock = threading.Lock()


def get_account_lock(username: str) -> threading.Lock:
    """Lock por conta — garante que 2 threads do worker nunca postam na mesma @ em paralelo."""
    with _account_locks_meta_lock:
        lock = _account_locks.get(username)
        if lock is None:
            lock = threading.Lock()
            _account_locks[username] = lock
        return lock


def get_cached_client(username: str):
    """Devolve Client cacheado se ainda fresco, senão None."""
    now = time.time()
    with _client_cache_lock:
        entry = _client_cache.get(username)
        if not entry:
            return None
        cl, last_used = entry
        if now - last_used >= CLIENT_CACHE_TTL_SECONDS:
            _client_cache.pop(username, None)
            return None
        # Bump last_used
        _client_cache[username] = (cl, now)
        return cl


def store_client(username: str, cl) -> None:
    with _client_cache_lock:
        _client_cache[username] = (cl, time.time())


def invalidate_client(username: str) -> None:
    with _client_cache_lock:
        _client_cache.pop(username, None)


# ----------- HTTP -----------

def headers() -> dict:
    return {"X-Worker-Token": WORKER_TOKEN}


def post(path: str, json: dict = None, **kwargs):
    return requests.post(SERVER_URL + path, headers=headers(), json=json or {}, timeout=HTTP_TIMEOUT, **kwargs)


def get(path: str, **kwargs):
    return requests.get(SERVER_URL + path, headers=headers(), timeout=HTTP_TIMEOUT, **kwargs)


def heartbeat():
    try:
        r = post("/api/worker/heartbeat", {"platform": PLATFORM})
        if r.status_code == 200:
            return r.json()
        if r.status_code == 401:
            print(f"[heartbeat] TOKEN INVÁLIDO — confira WORKER_TOKEN no .env")
            sys.exit(2)
        print(f"[heartbeat] HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[heartbeat] falhou: {e}")
    return None


def fetch_next_job():
    try:
        r = get("/api/worker/jobs/next")
        if r.status_code != 200:
            return None
        return r.json().get("job")
    except Exception as e:
        print(f"[poll] erro: {e}")
        return None


def log_to_server(job_id: str, line: str):
    """Best-effort — não levanta se falhar."""
    try:
        post(f"/api/worker/jobs/{job_id}/log", {"line": line})
    except Exception:
        pass


def report_result(job_id: str, success: bool, media_id: str = None,
                  error_msg: str = None, result_data: dict = None):
    try:
        payload = {
            "success": success,
            "media_id": media_id,
            "error_msg": error_msg,
        }
        if result_data is not None:
            payload["result_data"] = result_data
        post(f"/api/worker/jobs/{job_id}/result", payload)
    except Exception as e:
        print(f"[result] falhou enviar: {e}")


def mark_started(job_id: str):
    try:
        post(f"/api/worker/jobs/{job_id}/start", {})
    except Exception:
        pass


def report_step(job_id: str, step: str):
    """Notifica o servidor da etapa atual (downloading|logging|posting|finishing).
    Best-effort: se falhar, segue sem quebrar o job."""
    try:
        post(f"/api/worker/jobs/{job_id}/step", {"step": step})
    except Exception:
        pass


# ----------- Download media -----------

def download_media(url: str, dest: Path):
    """Baixa mídia do server, com auth via token."""
    with requests.get(url, headers=headers(), stream=True, timeout=120) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


# ----------- Execução do job -----------

def execute_job(job: dict):
    job_id = job["id"]
    username = job["account_username"]
    operation = job.get("operation", "post")
    params = job.get("params") or {}

    def log(msg):
        print(f"[{job_id[:8]}] {msg}")
        log_to_server(job_id, msg)

    mark_started(job_id)

    # Importa core
    try:
        from core.session import get_client
        from core.poster import post_reel, post_story_photo, post_story_video
        from core.profile import (
            get_profile_info, edit_profile_info, change_profile_picture,
            auto_like_own_recent_comments, auto_follow_back_new_followers,
            add_story_to_highlight, get_latest_own_story_pk,
        )
    except ImportError as e:
        log(f"❌ módulos core não encontrados: {e}")
        log("Rode o worker dentro da pasta do projeto: python worker.py")
        report_result(job_id, False, error_msg=f"import: {e}")
        return

    # Helper: faz login do Instagram (compartilhado entre operations)
    # Reusa Client cacheado se a mesma conta foi usada nos últimos 10min — economiza
    # load_settings + get_timeline_feed (3-8s por job).
    def do_login():
        cached = get_cached_client(username)
        if cached is not None:
            log(f"♻️ sessão Insta em cache (sem relogin)")
            return cached
        report_step(job_id, "logging")
        cl = get_client(
            username=username,
            password=job["account_password"],
            proxy=job.get("account_proxy"),
            totp_secret=job.get("account_totp_secret"),
        )
        store_client(username, cl)
        return cl

    # =====================================================
    # OPERAÇÃO: test_login
    # =====================================================
    if operation == "test_login":
        log(f"conectando @{username} (apenas login, sem post)")
        try:
            cl = do_login()
            info = cl.account_info()
            log(f"✅ conectado como @{info.username} ({info.full_name})")
            report_result(job_id, True, media_id="session_ok")
        except Exception as e:
            log(f"❌ falha conexão: {e}")
            report_result(job_id, False, error_msg=str(e))
        return

    # =====================================================
    # OPERAÇÃO: get_profile_info
    # =====================================================
    if operation == "get_profile_info":
        log(f"buscando info do perfil @{username}")
        try:
            cl = do_login()
            info = get_profile_info(cl)
            if info.get("error"):
                report_result(job_id, False, error_msg=info["error"])
            else:
                log(f"✓ {info.get('follower_count')} seguidores")
                report_result(job_id, True, result_data=info)
        except Exception as e:
            log(f"❌ falha: {e}")
            report_result(job_id, False, error_msg=str(e))
        return

    # =====================================================
    # OPERAÇÃO: edit_profile (bio, nome, link)
    # =====================================================
    if operation == "edit_profile":
        log(f"editando perfil @{username}")
        try:
            cl = do_login()
            r = edit_profile_info(
                cl,
                biography=params.get("biography"),
                full_name=params.get("full_name"),
                external_url=params.get("external_url"),
            )
            if r.get("success"):
                log("✅ perfil atualizado")
                report_result(job_id, True)
            else:
                log(f"❌ {r.get('error')}")
                report_result(job_id, False, error_msg=r.get("error"))
        except Exception as e:
            log(f"❌ exceção: {e}")
            report_result(job_id, False, error_msg=str(e))
        return

    # =====================================================
    # OPERAÇÃO: change_picture (foto de perfil)
    # =====================================================
    if operation == "change_picture":
        media_url = params.get("image_url") or job.get("media_url")
        if not media_url:
            log("❌ image_url ausente nos params")
            report_result(job_id, False, error_msg="image_url ausente")
            return
        log(f"trocando foto de perfil @{username}")
        # Baixa a imagem
        image_path = TMP_DIR / f"{job_id}_profile.jpg"
        try:
            download_media(media_url, image_path)
            log(f"foto baixada ({image_path.stat().st_size // 1024} KB)")
        except Exception as e:
            log(f"❌ download falhou: {e}")
            report_result(job_id, False, error_msg=f"download: {e}")
            return
        try:
            cl = do_login()
            r = change_profile_picture(cl, str(image_path))
            if r.get("success"):
                log("✅ foto de perfil atualizada")
                report_result(job_id, True)
            else:
                log(f"❌ {r.get('error')}")
                report_result(job_id, False, error_msg=r.get("error"))
        except Exception as e:
            log(f"❌ exceção: {e}")
            report_result(job_id, False, error_msg=str(e))
        finally:
            try:
                image_path.unlink()
            except Exception:
                pass
        return

    # =====================================================
    # OPERAÇÃO: auto_like_own (curtir comentários nos próprios posts)
    # =====================================================
    if operation == "auto_like_own":
        max_likes = int(params.get("max_likes", 3))
        log(f"auto-like @{username} (máx {max_likes} comentários)")
        try:
            cl = do_login()
            r = auto_like_own_recent_comments(cl, max_likes=max_likes)
            if r.get("success"):
                log(f"✅ {r.get('liked', 0)} likes (de {r.get('seen', 0)} comentários vistos)")
                report_result(job_id, True, result_data=r)
            else:
                log(f"⚠️ {r.get('error', 'falhou')}")
                report_result(job_id, False, error_msg=r.get("error"), result_data=r)
        except Exception as e:
            log(f"❌ exceção: {e}")
            report_result(job_id, False, error_msg=str(e))
        return

    # =====================================================
    # OPERAÇÃO: auto_follow_back (seguir de volta novos seguidores)
    # =====================================================
    if operation == "auto_follow_back":
        max_follows = int(params.get("max_follows", 2))
        seen = params.get("seen_followers") or []
        log(f"auto-follow-back @{username} (máx {max_follows} novos)")
        try:
            cl = do_login()
            r = auto_follow_back_new_followers(cl, seen_followers=seen, max_follows=max_follows)
            if r.get("success"):
                followed = r.get("followed", [])
                if followed:
                    names = ", ".join(f"@{x['username']}" for x in followed)
                    log(f"✅ seguiu {len(followed)}: {names}")
                else:
                    log(f"✓ nenhum follow ({r.get('note', 'sem novos')})")
                report_result(job_id, True, result_data=r)
            else:
                log(f"⚠️ {r.get('error', 'falhou')}")
                report_result(job_id, False, error_msg=r.get("error"), result_data=r)
        except Exception as e:
            log(f"❌ exceção: {e}")
            report_result(job_id, False, error_msg=str(e))
        return

    # =====================================================
    # OPERAÇÃO: collect_insights (tracker passivo de shadow ban)
    # =====================================================
    if operation == "collect_insights":
        log(f"collecting insights @{username}")
        try:
            cl = do_login()
            # Pega info da conta (followers, media_count)
            info = cl.user_info(cl.user_id)
            follower_count = int(getattr(info, "follower_count", 0) or 0)
            following_count = int(getattr(info, "following_count", 0) or 0)
            media_count = int(getattr(info, "media_count", 0) or 0)

            # Pega últimos 10 posts e calcula media de views
            recent_posts = []
            try:
                medias = cl.user_medias(cl.user_id, amount=10)
                for m in medias:
                    views = 0
                    # Reels e videos têm view_count, posts normais usam play_count fallback
                    for attr in ("view_count", "play_count", "video_view_count"):
                        v = getattr(m, attr, None)
                        if v and int(v) > views:
                            views = int(v)
                    recent_posts.append({
                        "pk": str(m.pk),
                        "views": views,
                        "likes": int(getattr(m, "like_count", 0) or 0),
                        "taken_at": str(getattr(m, "taken_at", "")),
                    })
            except Exception as me:
                log(f"⚠️ user_medias falhou: {me}")

            avg_recent_3 = 0.0
            avg_baseline = 0.0
            if len(recent_posts) >= 4:
                top3 = recent_posts[:3]
                rest = recent_posts[3:10]
                if top3:
                    avg_recent_3 = sum(p["views"] for p in top3) / len(top3)
                if rest:
                    avg_baseline = sum(p["views"] for p in rest) / len(rest)

            from datetime import datetime as _dt_h, timezone as _tz_h
            snapshot = {
                "collected_at": _dt_h.now(_tz_h.utc).isoformat(timespec="seconds"),
                "follower_count": follower_count,
                "following_count": following_count,
                "media_count": media_count,
                "recent_posts": recent_posts,
                "avg_views_last_3": round(avg_recent_3, 1),
                "avg_views_baseline": round(avg_baseline, 1),
            }
            log(f"✓ insights: {follower_count} seg, {len(recent_posts)} posts, avg recent={int(avg_recent_3)} baseline={int(avg_baseline)}")
            report_result(job_id, True, result_data=snapshot)
        except Exception as e:
            log(f"❌ collect_insights falhou: {e}")
            report_result(job_id, False, error_msg=str(e))
        return

    # =====================================================
    # OPERAÇÃO: hashtag_check (teste ativo de shadow ban)
    # Verifica se um post aparece na busca de hashtag (em outra conta)
    # =====================================================
    if operation == "hashtag_check":
        target_pk = params.get("target_pk")
        hashtag = params.get("hashtag", "").lstrip("#")
        log(f"hashtag_check: buscando #{hashtag} pra ver se pk={target_pk} aparece")
        if not target_pk or not hashtag:
            log("❌ target_pk e hashtag obrigatórios")
            report_result(job_id, False, error_msg="params target_pk e hashtag obrigatórios")
            return
        try:
            cl = do_login()
            try:
                medias = cl.hashtag_medias_recent(hashtag, amount=50)
            except Exception:
                medias = []
            found = any(str(m.pk) == str(target_pk) for m in medias)
            log(f"✓ resultado: post {'APARECE' if found else 'NÃO aparece'} na busca de #{hashtag}")
            report_result(job_id, True, result_data={
                "found": found,
                "hashtag": hashtag,
                "target_pk": str(target_pk),
                "checked_count": len(medias),
            })
        except Exception as e:
            log(f"❌ hashtag_check falhou: {e}")
            report_result(job_id, False, error_msg=str(e))
        return

    # =====================================================
    # OPERAÇÃO: post (default)
    # =====================================================
    log(f"iniciando job: {job['video_name']} ({job['kind']}) -> @{username}")

    # Baixa mídia
    report_step(job_id, "downloading")
    media_name = job["video_name"]
    media_path = TMP_DIR / f"{job_id}_{media_name}"
    try:
        log(f"baixando mídia de {job['media_url']}")
        download_media(job["media_url"], media_path)
        log(f"mídia baixada ({media_path.stat().st_size // 1024} KB)")
    except Exception as e:
        log(f"❌ erro baixando mídia: {e}")
        report_result(job_id, False, error_msg=f"download: {e}")
        return

    # Login Instagram (usa cache se a mesma conta foi usada recentemente)
    cached = get_cached_client(username)
    if cached is not None:
        cl = cached
        log(f"♻️ sessão Insta em cache (sem relogin)")
    else:
        report_step(job_id, "logging")
        try:
            log(f"fazendo login no Instagram (sessão local, se existir)")
            cl = get_client(
                username=username,
                password=job["account_password"],
                proxy=job.get("account_proxy"),
                totp_secret=job.get("account_totp_secret"),
            )
            store_client(username, cl)
            log(f"✓ logado como @{username}")
        except Exception as e:
            invalidate_client(username)
            log(f"❌ falha login: {e}")
            report_result(job_id, False, error_msg=f"login: {e}")
            return

    # Posta
    report_step(job_id, "posting")
    try:
        kind = job.get("kind", "reel")
        media_type = job.get("media_type", "video")
        caption = job.get("caption", "")
        link_url = job.get("link_url")
        link_text = job.get("link_text") or "Clique aqui"

        log(f"postando ({kind}, {media_type}){' + link [' + link_text + ']' if link_url else ''}")

        if kind == "story":
            if media_type == "photo":
                # Normaliza foto pra 1080x1920 + JPEG limpo (resolve erros do Insta com
                # fotos do WhatsApp / proporções não-9:16 / EXIF estranho)
                try:
                    from core.media import normalize_image_for_story
                    log("normalizando foto pra story (1080x1920)")
                    normalize_image_for_story(media_path)
                except Exception as ne:
                    log(f"⚠️ normalização falhou ({ne}) — tentando com original")
                result = post_story_photo(cl, str(media_path), caption, link_url, link_text)
            else:
                result = post_story_video(cl, str(media_path), caption, link_url, link_text)
        else:
            if media_type == "photo":
                result = {"success": False, "media_id": None, "error": "Foto não pode virar Reel"}
            else:
                result = post_reel(cl, str(media_path), caption)

        if result.get("success"):
            mid = result.get("media_id")
            if result.get("warning"):
                log(f"⚠️  postado COM RESSALVA: {result.get('warning')}")
            else:
                log(f"✅ postado! media_id={mid}")

            # Se foi Story + tem destaque configurado, adiciona ao destaque
            highlight_info = None
            if kind == "story":
                highlight_title = params.get("auto_highlight_title") or job.get("auto_highlight_title")
                if highlight_title:
                    report_step(job_id, "finishing")
                    # Fallback: se phantom error (sem mid), busca o último story ativo
                    story_pk_for_highlight = mid
                    if not story_pk_for_highlight:
                        log(f"🔍 buscando ID do story recém-postado (fallback phantom error)")
                        story_pk_for_highlight = get_latest_own_story_pk(cl)
                        if story_pk_for_highlight:
                            log(f"✓ encontrado story pk={story_pk_for_highlight}")
                        else:
                            log(f"⚠️ não consegui achar story recente — destaque pulado")

                    if story_pk_for_highlight:
                        log(f"📌 adicionando ao destaque '{highlight_title}'")
                        try:
                            hr = add_story_to_highlight(cl, story_pk_for_highlight, highlight_title)
                            if hr.get("success"):
                                acao = "criado novo" if hr.get("action") == "created_new" else "adicionado ao existente"
                                log(f"✅ destaque {acao}: '{hr.get('highlight_title')}'")
                                highlight_info = hr
                            else:
                                log(f"⚠️ destaque falhou: {hr.get('error')}")
                        except Exception as e:
                            log(f"⚠️ destaque exception: {e}")

            report_result(
                job_id, True,
                media_id=mid,
                result_data={"highlight": highlight_info} if highlight_info else None,
            )
        else:
            log(f"❌ post falhou: {result.get('error')}")
            report_result(job_id, False, error_msg=result.get("error"))

    except Exception as e:
        log(f"❌ exceção: {e}")
        report_result(job_id, False, error_msg=str(e))

    finally:
        # Limpa mídia temp
        try:
            media_path.unlink()
        except Exception:
            pass


# ----------- Local API: HTTP server pra UI abrir Chrome direto -----------
# A UI (instapost.shop) bate em http://127.0.0.1:17777/open-browser?username=X
# pra abrir Chrome com profile isolado + UA mobile do device fingerprint da conta.
# Sem isso, o botão "Smartphone" da UI baixa um .bat que o usuário precisa executar.

LOCAL_API_PORT = 17777


def _find_chrome_path() -> Optional[str]:
    """Acha o executável do Chrome no sistema."""
    sys_name = platform.system()
    if sys_name == "Windows":
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
    elif sys_name == "Darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    from shutil import which
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        p = which(name)
        if p:
            return p
    return None


def _find_session_file(username: str) -> Optional[Path]:
    """Procura o session JSON do instagrapi em workspaces/<ws>/sessions/<user>.json.
    Worker pode rodar em qualquer workspace — escaneamos todos."""
    # DATA_DIR é a raiz dos dados (idêntico ao core.paths.DATA_DIR mas resolvemos
    # aqui pra não depender de import lazy)
    project_root = Path(__file__).resolve().parent
    data_dir = Path(os.environ.get("DATA_DIR", str(project_root)))
    ws_root = data_dir / "workspaces"
    if not ws_root.exists():
        # Fallback: sessions na raiz (estilo pré-workspace)
        legacy = data_dir / "sessions" / f"{username}.json"
        return legacy if legacy.exists() else None
    for ws_dir in ws_root.iterdir():
        if not ws_dir.is_dir():
            continue
        candidate = ws_dir / "sessions" / f"{username}.json"
        if candidate.exists():
            return candidate
    return None


def _extract_cdp_cookies(session_path: Path) -> list[dict]:
    """Lê o session JSON do instagrapi e devolve cookies no formato CDP
    (Network.setCookie). Inclui pelo menos sessionid + ds_user_id + mid."""
    import json as _json
    try:
        data = _json.loads(session_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    auth = data.get("authorization_data") or {}
    sessionid = auth.get("sessionid")
    ds_user_id = auth.get("ds_user_id")
    mid = data.get("mid")

    if not sessionid or not ds_user_id:
        return []

    # Expira em 1 ano (sessionid do IG dura ~90 dias na prática; vence sozinho antes)
    expires = int(time.time()) + 365 * 24 * 3600
    common = {
        "domain": ".instagram.com",
        "path": "/",
        "secure": True,
        "sameSite": "Lax",
        "expires": expires,
    }
    cookies = [
        {"name": "sessionid", "value": sessionid, "httpOnly": True, **common},
        {"name": "ds_user_id", "value": ds_user_id, "httpOnly": False, **common},
    ]
    if mid:
        cookies.append({"name": "mid", "value": mid, "httpOnly": False, **common})
    # csrftoken: se a session JSON tiver no dict cookies, manda. Senão, IG seta sozinho.
    extra_cookies = data.get("cookies") or {}
    for name in ("csrftoken", "ig_did", "rur", "ig_nrcb"):
        v = extra_cookies.get(name)
        if v:
            cookies.append({"name": name, "value": v, "httpOnly": False, **common})
    return cookies


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _inject_cookies_via_cdp(debug_port: int, cookies: list[dict], target_url: str) -> bool:
    """Conecta no Chrome via CDP, injeta cookies e navega pra URL. Lib opcional —
    se websocket-client não tiver instalado, devolve False e Chrome só abre vazio."""
    try:
        import websocket  # websocket-client (pip install websocket-client)
    except ImportError:
        print("[local-api] ⚠️ websocket-client não instalado — Chrome abre sem cookies. "
              "Rode: pip install -r worker-requirements.txt")
        return False

    import json as _json
    import urllib.request

    # Espera o CDP HTTP endpoint ficar disponível (até 10s — Chrome pode demorar
    # pra subir em PCs lentos ou quando profile tem extensions/cookies grandes)
    targets_url = f"http://127.0.0.1:{debug_port}/json"
    targets = None
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(targets_url, timeout=0.5) as r:
                targets = _json.loads(r.read().decode("utf-8"))
                if targets:
                    break
        except Exception:
            time.sleep(0.15)
    if not targets:
        print(f"[local-api] ⚠️ CDP em 127.0.0.1:{debug_port} não respondeu — Chrome abre sem cookies")
        return False

    # Pega o 1º target tipo "page" (aba about:blank que abrimos)
    page_target = next((t for t in targets if t.get("type") == "page"), targets[0])
    ws_url = page_target.get("webSocketDebuggerUrl")
    if not ws_url:
        return False

    try:
        ws = websocket.create_connection(ws_url, timeout=5)
    except Exception as e:
        print(f"[local-api] ⚠️ WS connect falhou: {e}")
        return False

    try:
        msg_id = 0

        def send(method, params=None):
            nonlocal msg_id
            msg_id += 1
            payload = {"id": msg_id, "method": method, "params": params or {}}
            ws.send(_json.dumps(payload))
            # Drena resposta (não bloqueia muito)
            try:
                ws.recv()
            except Exception:
                pass

        send("Network.enable")
        # Limpa cookies antigos do .instagram.com ANTES de setar os novos.
        # Sem isso, profile persistido acumula sessions de tentativas anteriores
        # (sessionid antigo + sessionid novo) — Instagram detecta "cadeia de
        # sessões" e marca como suspeito.
        for cookie_name in ("sessionid", "ds_user_id", "csrftoken", "ig_did",
                            "ig_nrcb", "rur", "shbid", "shbts", "mid"):
            send("Network.deleteCookies", {"name": cookie_name, "domain": ".instagram.com"})
            send("Network.deleteCookies", {"name": cookie_name, "domain": "instagram.com"})
            send("Network.deleteCookies", {"name": cookie_name, "domain": "www.instagram.com"})
        for c in cookies:
            send("Network.setCookie", c)
        send("Page.navigate", {"url": target_url})
        return True
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _kill_chrome_for_profile(profile_dir: Path) -> None:
    """Mata chrome.exe que está usando esse profile dir. Necessário porque se já
    tem Chrome aberto nessa mesma --user-data-dir, o novo launch só vira IPC
    client (ignora --remote-debugging-port). Sem isso, auto-login não funciona
    na 2ª+ vez que abre."""
    if platform.system() != "Windows":
        return
    try:
        # Match: chrome.exe com o nome da profile no cmdline. Username é sanitizado
        # (alnum + ._-) então é seguro pra string match.
        marker = profile_dir.name
        ps_cmd = (
            f"Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*InstaposterProfiles*{marker}*' }} | "
            f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
        )
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, timeout=5,
        )
        # Espera Chrome liberar SingletonLock do profile (~1s na prática)
        time.sleep(1.2)
    except Exception as e:
        print(f"[local-api] kill Chrome anterior falhou (ignorando): {e}")


# ===== Throttle de abertura do launcher (anti-batch-detection) =====
# Instagram flagra "10 contas logando do mesmo IP em 5min" como bot farm.
# Limites: 1 abertura/3min por @ + 1/60s global (evita rajada).
_last_open_per_user: dict[str, float] = {}
_last_open_global: float = 0.0
_throttle_lock = threading.Lock()
THROTTLE_PER_USER_SECONDS = 5 * 60
THROTTLE_GLOBAL_SECONDS = 60


def _check_throttle(username: str) -> Optional[str]:
    """Retorna mensagem de erro se throttle bloqueou, None se OK."""
    global _last_open_global
    now = time.time()
    with _throttle_lock:
        elapsed_global = now - _last_open_global
        if elapsed_global < THROTTLE_GLOBAL_SECONDS:
            return f"Espere {int(THROTTLE_GLOBAL_SECONDS - elapsed_global)}s antes de abrir outra conta (anti-batch-ban global)"
        last_user = _last_open_per_user.get(username, 0)
        elapsed_user = now - last_user
        if elapsed_user < THROTTLE_PER_USER_SECONDS:
            remaining_min = int((THROTTLE_PER_USER_SECONDS - elapsed_user) / 60) + 1
            return f"@{username} foi aberta há pouco. Aguarde {remaining_min}min antes de reabrir (anti-ban)"
        _last_open_global = now
        _last_open_per_user[username] = now
    return None


def _build_proxy_auth_extension(extension_dir: Path, proxy_user: str, proxy_pass: str) -> None:
    """Gera uma extensão Chrome temporária que responde automaticamente ao
    diálogo de auth do proxy. Sem isso, Chrome popa um modal pedindo senha
    a cada launch (chato e quebra automação)."""
    extension_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": 2,
        "name": "Insta Poster Proxy Auth",
        "version": "1.0",
        "permissions": ["proxy", "webRequest", "webRequestBlocking", "<all_urls>"],
        "background": {"scripts": ["bg.js"], "persistent": True},
    }
    bg_js = (
        "chrome.webRequest.onAuthRequired.addListener(\n"
        "  function(details) {\n"
        "    return {authCredentials: {\n"
        f"      username: {json.dumps(proxy_user)},\n"
        f"      password: {json.dumps(proxy_pass)}\n"
        "    }};\n"
        "  },\n"
        "  {urls: ['<all_urls>']},\n"
        "  ['blocking']\n"
        ");\n"
    )
    (extension_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (extension_dir / "bg.js").write_text(bg_js, encoding="utf-8")


def _parse_proxy_for_chrome(proxy_url: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Quebra proxy URL em (chrome_proxy_str, user, pass).
    Chrome --proxy-server não aceita user:pass na URL — precisa de extensão pra auth.
    Retorna: ('http://host:port', 'user', 'pass') ou (raw, None, None) se sem auth."""
    if not proxy_url:
        return None, None, None
    from urllib.parse import urlparse, unquote
    try:
        p = urlparse(proxy_url)
        if not p.scheme or not p.hostname:
            return None, None, None
        port = p.port or (1080 if "socks" in p.scheme else 80)
        chrome_proxy = f"{p.scheme}://{p.hostname}:{port}"
        user = unquote(p.username) if p.username else None
        pwd = unquote(p.password) if p.password else None
        return chrome_proxy, user, pwd
    except Exception:
        return None, None, None


def _open_chrome_for_account(
    username: str,
    reset: bool = False,
    proxy: Optional[str] = None,
    no_proxy: bool = False,
) -> tuple[bool, str]:
    """Abre Chrome com profile isolado + UA desktop + (opcional) sessão pré-injetada
    + (opcional) proxy da conta + flags anti-detect.

    Se reset=True: apaga o profile dir inteiro antes de abrir.
    Se proxy: Chrome sai pelo MESMO IP que o worker usa (IP coerente, sem flag IG).
    """
    safe = "".join(c for c in username if c.isalnum() or c in "._-")
    if not safe:
        return False, "username inválido"

    # Throttle: evita batch-detection (vários logins do mesmo IP em pouco tempo)
    throttle_err = _check_throttle(safe)
    if throttle_err:
        return False, throttle_err

    chrome = _find_chrome_path()
    if not chrome:
        return False, "Chrome não encontrado nesse PC"

    profile_dir = Path.home() / "InstaposterProfiles" / safe

    # Mata Chrome anterior PRIMEIRO (senão não conseguimos deletar profile dir bloqueado)
    _kill_chrome_for_profile(profile_dir)

    # Reset: apaga tudo e abre limpo
    if reset and profile_dir.exists():
        try:
            import shutil
            shutil.rmtree(profile_dir)
            print(f"[local-api] 🧹 profile resetado: {profile_dir}")
        except Exception as e:
            print(f"[local-api] ⚠️ falha apagando profile: {e}")

    profile_dir.mkdir(parents=True, exist_ok=True)

    # Procura sessão salva pra auto-login. Se reset=True, pula (queremos login manual limpo).
    cdp_cookies: list[dict] = []
    if not reset:
        session_path = _find_session_file(safe)
        if session_path:
            cdp_cookies = _extract_cdp_cookies(session_path)
            if not cdp_cookies:
                print(f"[local-api] sessão {session_path.name} sem cookies utilizáveis (provavelmente vazia)")

    inject = bool(cdp_cookies)
    debug_port = _get_free_port() if inject else 0
    target_url = "https://www.instagram.com/"

    # UA Chrome desktop normal (não o UA Android do worker — Insta serviria layout mobile bugado)
    desktop_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    # === Flags anti-detect + anti-leak ===
    args = [
        chrome,
        f"--user-data-dir={profile_dir}",
        f"--user-agent={desktop_ua}",
        "--window-size=1280,800",
        "--no-first-run",
        "--no-default-browser-check",
        # Anti-detect: Instagram olha navigator.webdriver — sem isso, score bot 100%
        "--disable-blink-features=AutomationControlled",
        # Anti-leak: WebRTC vaza IP REAL mesmo com proxy. Esse flag força WebRTC
        # passar só por trás do proxy (ou desabilitar se não der)
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        # Translate prompt do Chrome em página estrangeira é signal estranho
        "--disable-features=Translate,AcceptCHFrame,PrivacySandboxSettings4",
        # Notificações falsas (browser sem notifs é signal)
        "--disable-notifications",
        # Locale pt-BR (coerente com worker BR)
        "--lang=pt-BR",
    ]

    # === Proxy COERENTE com o worker (CRÍTICO pra não flagar batch login) ===
    # Modo no_proxy: abre Chrome com IP RESIDENCIAL (sem proxy nenhum). Use SÓ
    # pra logar manualmente em conta nova/comprada — Chrome aceita auth do IG mais
    # facilmente quando o IP "bate" com o do celular onde a conta foi criada.
    # AVISO: depois de logar manual, vai criar inconsistência se worker tentar
    # logar via proxy depois — IG vê IP diferente. Solução: logar tudo via proxy
    # OU desativar proxy permanentemente nessa conta.
    proxy_user_auth = None
    proxy_pass_auth = None
    ext_dir = None
    if no_proxy:
        proxy = None  # força sem proxy nesse launch
        print(f"[local-api] ⚠️ Chrome SEM PROXY (modo manual login) — IP residencial vai aparecer pro Insta")
    if proxy:
        # Sticky session por conta (mesmo IP entre requests do Chrome + worker)
        try:
            from core.proxy_sticky import make_sticky
            sticky = make_sticky(proxy, safe)
            if sticky != proxy:
                print(f"[local-api] 🔒 sticky session aplicado pro Chrome")
                proxy = sticky
        except Exception:
            pass
        chrome_proxy, proxy_user_auth, proxy_pass_auth = _parse_proxy_for_chrome(proxy)
        if chrome_proxy:
            args.append(f"--proxy-server={chrome_proxy}")
            # NOTA: removido --host-resolver-rules=MAP * ~NOTFOUND. Esse flag
            # bloqueava DNS de TUDO (incluindo o próprio hostname do proxy),
            # resultando em "sem internet" no Chrome. Pra HTTP proxy, o proxy
            # resolve target DNS sozinho — Chrome só precisa resolver o hostname
            # do proxy via DNS normal (do sistema). Sem leak relevante.
            # Se proxy tem auth, monta uma extensão Chrome que responde ao auth dialog
            if proxy_user_auth and proxy_pass_auth:
                ext_dir = profile_dir / "_proxy_auth_ext"
                try:
                    _build_proxy_auth_extension(ext_dir, proxy_user_auth, proxy_pass_auth)
                    args.append(f"--load-extension={ext_dir}")
                except Exception as e:
                    print(f"[local-api] ⚠️ falha criando extensão proxy auth: {e}")
            print(f"[local-api] 🌐 Chrome via proxy {chrome_proxy} (auth: {'sim' if proxy_user_auth else 'não'})")

    # SEMPRE habilita debug port pra feature "Salvar Sessão" funcionar mesmo
    # quando não estamos injetando cookies (ex: login manual em conta nova).
    # Chrome escreve em DevToolsActivePort dentro do profile, _save_session_from_chrome lê.
    if debug_port == 0:
        debug_port = _get_free_port()
    args += [
        f"--remote-debugging-port={debug_port}",
        "--remote-allow-origins=*",
    ]
    if inject:
        # Sobe Chrome em about:blank — injetamos cookies via CDP ANTES de navegar
        args.append("about:blank")
    else:
        args.append(target_url)

    try:
        creationflags = 0
        if platform.system() == "Windows":
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(args, close_fds=True, creationflags=creationflags)
    except Exception as e:
        return False, f"Popen falhou: {e}"

    proxy_desc = " + proxy" if proxy else " (SEM proxy)"
    desc = f"Chrome desktop{proxy_desc}"
    if reset:
        return True, f"{desc} (profile RESETADO — login manual)"
    if inject:
        injected = _inject_cookies_via_cdp(debug_port, cdp_cookies, target_url)
        if injected:
            return True, f"{desc} + auto-login ({len(cdp_cookies)} cookies)"
        else:
            return True, f"{desc} (sem auto-login — login manual)"
    return True, f"{desc} (sem sessão salva — login manual)"


def _save_session_from_chrome(username: str) -> tuple[bool, str]:
    """Extrai cookies do Chrome aberto e monta session.json válida pro instagrapi.

    Workflow esperado:
    1. User clica Smartphone → Chrome abre com debug port
    2. User loga manual no Insta (IG aceita por ser browser real)
    3. User chama esse endpoint → lê cookies → gera session.json
    4. Worker passa a usar essa sessão SEM precisar de API login

    Resolve o problema 'celular loga normal mas worker dá challenge'
    porque a sessão SAIU de um login real validado.
    """
    safe = "".join(c for c in username if c.isalnum() or c in "._-")
    if not safe:
        return False, "username inválido"

    profile_dir = Path.home() / "InstaposterProfiles" / safe
    if not profile_dir.exists():
        return False, "Profile não existe. Clica Smartphone primeiro, loga, depois Salvar Sessão."

    # Lê DevToolsActivePort que o Chrome escreve com a porta debug ativa
    port_file = profile_dir / "DevToolsActivePort"
    if not port_file.exists():
        return False, "Chrome não tá aberto OU não foi iniciado com debug port. Clica Smartphone (sem reset) e tenta de novo."

    try:
        lines = port_file.read_text(encoding="utf-8").strip().split("\n")
        port = int(lines[0])
    except Exception as e:
        return False, f"erro lendo DevToolsActivePort: {e}"

    # Conecta via CDP, lê cookies
    try:
        import websocket as _ws
        import urllib.request as _urllib
        # Lista targets pra achar uma page do Insta
        r = _urllib.urlopen(f"http://127.0.0.1:{port}/json", timeout=5)
        targets = json.loads(r.read().decode("utf-8"))
        # Procura page do instagram primeiro, senão qualquer page
        page = next((t for t in targets if t.get("type") == "page" and "instagram" in (t.get("url", "").lower())), None)
        if not page:
            page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            return False, "Nenhuma aba aberta no Chrome"
        ws_url = page.get("webSocketDebuggerUrl")
        if not ws_url:
            return False, "websocket URL não disponível"
        ws_conn = _ws.create_connection(ws_url, timeout=8)
    except Exception as e:
        return False, f"erro conectando CDP: {e}"

    try:
        ws_conn.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        # Pode receber events antes da resposta — drena até achar id=1
        cookies_list = None
        for _ in range(30):
            try:
                response = json.loads(ws_conn.recv())
            except Exception:
                break
            if response.get("id") == 1:
                cookies_list = response.get("result", {}).get("cookies", [])
                break
        if cookies_list is None:
            return False, "CDP não retornou cookies"

        # Filtra cookies do Instagram (qualquer domínio .instagram.com / instagram.com / www.instagram.com)
        ig_cookies = {}
        for c in cookies_list:
            dom = (c.get("domain") or "").lower()
            if "instagram.com" not in dom:
                continue
            ig_cookies[c["name"]] = c["value"]

        sessionid = ig_cookies.get("sessionid")
        ds_user_id = ig_cookies.get("ds_user_id")
        if not sessionid or not ds_user_id:
            return False, f"Não tá logado no Insta. Faltam sessionid ou ds_user_id. Cookies encontrados: {list(ig_cookies.keys())[:10]}"

        # Monta session.json no formato esperado pelo instagrapi
        import secrets as _secrets
        import uuid as _uuid_mod
        def _uuid():
            return str(_uuid_mod.uuid4())

        session_data = {
            "uuids": {
                "phone_id": _uuid(),
                "uuid": _uuid(),
                "client_session_id": _uuid(),
                "advertising_id": _uuid(),
                "android_device_id": "android-" + _secrets.token_hex(8),
                "request_id": _uuid(),
                "tray_session_id": _uuid(),
            },
            "mid": ig_cookies.get("mid", ""),
            "ig_u_rur": ig_cookies.get("rur"),
            "ig_www_claim": "",
            "authorization_data": {
                "ds_user_id": ds_user_id,
                "sessionid": sessionid,
            },
            "cookies": ig_cookies,
            "last_login": time.time(),
            # Device padrão Android (Pixel 8 Pro) — instagrapi usa pra montar headers.
            # Worker vai usar device_continuity em fresh login, mas como temos sessão
            # válida, fresh login provavelmente nem acontece.
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
        }

        # Acha sessions dir do workspace (mesmo lookup que _find_session_file faz, mas inverso)
        project_root = Path(__file__).resolve().parent
        data_dir = Path(os.environ.get("DATA_DIR", str(project_root)))
        ws_root = data_dir / "workspaces"
        # Tenta achar workspace existente que tem essa conta. Senão usa "default".
        target_dir = None
        if ws_root.exists():
            for ws_dir in ws_root.iterdir():
                if not ws_dir.is_dir():
                    continue
                # Procura accounts.json com essa @
                acc_file = ws_dir / "accounts.json"
                if acc_file.exists():
                    try:
                        accs = json.loads(acc_file.read_text(encoding="utf-8"))
                        if any(a.get("username") == safe for a in accs):
                            target_dir = ws_dir / "sessions"
                            break
                    except Exception:
                        pass
        if not target_dir:
            target_dir = ws_root / "default" / "sessions"

        target_dir.mkdir(parents=True, exist_ok=True)
        session_file = target_dir / f"{safe}.json"
        # Backup da sessão anterior se houver
        if session_file.exists():
            backup = session_file.with_suffix(".json.bak")
            try:
                backup.write_bytes(session_file.read_bytes())
            except Exception:
                pass
        session_file.write_text(json.dumps(session_data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True, f"Sessão salva em {session_file.name} ({len(ig_cookies)} cookies, sessionid: ...{sessionid[-12:]})"
    finally:
        try:
            ws_conn.close()
        except Exception:
            pass


class _LocalAPIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silencia o log default do http.server (polui o terminal do worker)
        pass

    def _send(self, status: int, body: bytes = b"", content_type: str = "application/json"):
        self.send_response(status)
        # CORS liberal: o server só escuta 127.0.0.1 e só faz UMA ação (abrir Chrome).
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With")
        # Private Network Access: Chrome novos exigem isso pra HTTPS chamar HTTP localhost
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        from urllib.parse import urlparse
        import json as _json
        path = urlparse(self.path).path
        if path == "/health":
            body = _json.dumps({"ok": True, "worker_name": WORKER_NAME}).encode("utf-8")
            self._send(200, body)
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        import json as _json
        parsed = urlparse(self.path)

        # /save-session: extrai cookies do Chrome aberto e gera session.json
        if parsed.path == "/save-session":
            qs = parse_qs(parsed.query)
            username = (qs.get("username") or [""])[0].strip()
            if not username:
                self._send(400, b'{"error":"username obrigatorio"}')
                return
            ok, info = _save_session_from_chrome(username)
            if ok:
                body = _json.dumps({"ok": True, "message": info}).encode("utf-8")
                self._send(200, body)
                print(f"[local-api] 💾 sessão SALVA pra @{username}: {info}")
            else:
                body = _json.dumps({"ok": False, "error": info}).encode("utf-8")
                self._send(400, body)
                print(f"[local-api] ⚠️ save-session @{username} falhou: {info}")
            return

        if parsed.path != "/open-browser":
            self._send(404, b'{"error":"not found"}')
            return
        qs = parse_qs(parsed.query)
        username = (qs.get("username") or [""])[0].strip()
        reset = (qs.get("reset") or ["0"])[0] in ("1", "true", "yes")
        no_proxy = (qs.get("no_proxy") or ["0"])[0] in ("1", "true", "yes")
        if not username:
            self._send(400, b'{"error":"username obrigatorio"}')
            return
        # Proxy: aceita via query (?proxy=...) OU body JSON {"proxy":"..."}
        proxy = (qs.get("proxy") or [""])[0].strip() or None
        if not proxy:
            try:
                content_len = int(self.headers.get("Content-Length", "0"))
                if content_len > 0:
                    raw = self.rfile.read(content_len)
                    body_data = _json.loads(raw.decode("utf-8"))
                    proxy = (body_data.get("proxy") or "").strip() or None
            except Exception:
                pass
        ok, info = _open_chrome_for_account(username, reset=reset, proxy=proxy, no_proxy=no_proxy)
        if ok:
            body = _json.dumps({"ok": True, "device": info, "reset": reset, "proxy_used": bool(proxy) and not no_proxy, "no_proxy": no_proxy}).encode("utf-8")
            self._send(200, body)
            emoji = "⚠️" if no_proxy else ("🧹" if reset else "📱")
            print(f"[local-api] {emoji} abrindo Chrome pra @{username} ({info})")
        else:
            body = _json.dumps({"ok": False, "error": info}).encode("utf-8")
            # 429 (Too Many Requests) se foi throttle, 500 se foi outro erro
            status = 429 if "Espere" in info or "Aguarde" in info else 500
            self._send(status, body)
            print(f"[local-api] ⚠️ falha abrindo @{username}: {info}")


def _local_api_loop():
    """HTTP server local na porta 17777 pra UI abrir Chrome direto (sem download .bat)."""
    try:
        server = HTTPServer(("127.0.0.1", LOCAL_API_PORT), _LocalAPIHandler)
        print(f"[local-api] escutando em http://127.0.0.1:{LOCAL_API_PORT} (botão 'Smartphone' da UI)")
        server.serve_forever()
    except OSError as e:
        print(f"[local-api] ⚠️ porta {LOCAL_API_PORT} ocupada ({e}) — UI vai cair pro download .bat")
    except Exception as e:
        print(f"[local-api] crash: {e}")


# ----------- Loop principal -----------

def _periodic_heartbeat():
    """Roda em thread separada — heartbeat a cada HEARTBEAT_INTERVAL_SECONDS."""
    while not _stop_flag.is_set():
        heartbeat()
        # Acorda se stop foi pedido
        if _stop_flag.wait(HEARTBEAT_INTERVAL_SECONDS):
            return


# Watchdog: monitora cada thread, se passar de MAX_JOB_SECONDS sem terminar, MATA o worker.
# Wrapper-forever vai relançar em 10s. Melhor reiniciar tudo que ficar travado pra sempre.
MAX_JOB_SECONDS = 15 * 60  # 15min máximo por job (login+upload+post normal é < 2min)
LOCK_ACQUIRE_TIMEOUT = 5 * 60  # 5min esperando lock de outra thread = desiste

# Estado pra watchdog
_thread_job_started: dict[int, float] = {}  # thread_id -> timestamp inicio do job atual
_thread_state_lock = threading.Lock()


def _watchdog_loop():
    """Thread separada que monitora estado de cada worker thread.

    Se uma thread está num job há mais de MAX_JOB_SECONDS sem terminar,
    significa que o instagrapi/login travou infinitamente. Matamos o
    processo inteiro — wrapper-forever vai relançar limpo.

    Por que matar o processo? Threading.Thread no Python NÃO tem .kill().
    Não dá pra forçar abortar uma thread travada num socket bloqueado.
    Reiniciar tudo é a única forma confiável.
    """
    while not _stop_flag.is_set():
        _stop_flag.wait(30)
        now = time.time()
        with _thread_state_lock:
            for tid, started in list(_thread_job_started.items()):
                elapsed = now - started
                if elapsed > MAX_JOB_SECONDS:
                    print(f"\n[WATCHDOG] ⚠️ T{tid} travada há {elapsed/60:.1f}min num job!")
                    print(f"[WATCHDOG] Matando worker pra wrapper-forever reiniciar limpo.")
                    print(f"[WATCHDOG] (Threads Python não podem ser killed; reset total é a única opção)\n")
                    sys.stdout.flush()
                    # os._exit() é mais bruto que sys.exit() — pula finalizers, atomic
                    os._exit(99)


def _worker_loop(thread_id: int):
    """Loop independente: claim → execute → repeat. Roda N vezes em paralelo
    quando WORKER_CONCURRENCY > 1. Lock por conta garante que 2 threads nunca
    processam a mesma @ em paralelo."""
    while not _stop_flag.is_set():
        try:
            job = fetch_next_job()
            if not job:
                # Sem trabalho — dorme com jitter pra não bater todo polling juntos
                _stop_flag.wait(POLL_INTERVAL_SECONDS + random.uniform(0, 2))
                continue

            username = job.get("account_username", "")
            lock = get_account_lock(username)
            # Se outra thread já está nessa conta, espera serializar (COM TIMEOUT!)
            acquired_now = lock.acquire(blocking=False)
            if not acquired_now:
                if WORKER_CONCURRENCY > 1:
                    print(f"[T{thread_id}] @{username} ocupada por outra thread, serializando (timeout 5min)…")
                # Bug antigo: lock.acquire() sem timeout = trava infinito se outra thread morreu
                # Agora: se esperar > 5min, desiste e tenta outro job
                got_lock = lock.acquire(timeout=LOCK_ACQUIRE_TIMEOUT)
                if not got_lock:
                    print(f"[T{thread_id}] ⚠️ NÃO conseguiu lock de @{username} em {LOCK_ACQUIRE_TIMEOUT/60:.0f}min — desistindo desse job, vai pegar outro")
                    # Job volta pra fila no servidor automaticamente (claim expira)
                    continue
            try:
                # Marca início pro watchdog
                with _thread_state_lock:
                    _thread_job_started[thread_id] = time.time()

                # Stagger anti-detecção quando há paralelismo.
                # Exponencial (humanlike_delay) em vez de uniform — padrão long-tail
                # é mais natural que linear (random.randint), Insta tem detector
                # de "muito regular" que pega uniforme facilmente.
                if WORKER_CONCURRENCY > 1:
                    from core.retry import humanlike_delay
                    jitter = humanlike_delay(min_s=15, mean_s=30, max_s=90)
                    print(f"[T{thread_id}] aguarda {jitter}s antes de iniciar (stagger exp)")
                    if _stop_flag.wait(jitter):
                        return
                execute_job(job)
            finally:
                # Sempre limpa watchdog e libera lock
                with _thread_state_lock:
                    _thread_job_started.pop(thread_id, None)
                try:
                    lock.release()
                except Exception:
                    pass
        except Exception as e:
            print(f"[T{thread_id}] exceção no loop: {e}")
            traceback.print_exc()
            _stop_flag.wait(POLL_INTERVAL_SECONDS)


def main():
    # Força UTF-8 no stdout (Windows usa cp1252)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if not SERVER_URL or not WORKER_TOKEN:
        print("ERRO: SERVER_URL e WORKER_TOKEN são obrigatórios.")
        print("Crie um arquivo .env ao lado do worker.py com:")
        print("  SERVER_URL=https://seudominio.com")
        print("  WORKER_TOKEN=<gerado em /workers>")
        sys.exit(1)

    print(f"=" * 60)
    print(f"  Insta Poster Worker")
    print(f"=" * 60)
    print(f"  Server:      {SERVER_URL}")
    print(f"  Nome:        {WORKER_NAME}")
    print(f"  Platform:    {PLATFORM}")
    print(f"  Concurrency: {WORKER_CONCURRENCY} thread(s)")
    print(f"=" * 60)
    print()

    # Heartbeat inicial pra validar token
    info = heartbeat()
    if not info:
        print("Heartbeat inicial falhou. Verifica SERVER_URL e WORKER_TOKEN.")
        sys.exit(2)
    print(f"✓ Conectado (worker_id: {info.get('worker_id')})")
    print(f"  Aguardando jobs… ({WORKER_CONCURRENCY} thread(s), poll {POLL_INTERVAL_SECONDS}s)")
    print()

    # Heartbeat em thread dedicada (não bloqueia o claim)
    hb_thread = threading.Thread(target=_periodic_heartbeat, daemon=True, name="heartbeat")
    hb_thread.start()

    # Watchdog: mata o worker se uma thread fica > 15min num job (indica deadlock).
    # Wrapper-forever reinicia automático em 10s.
    wd_thread = threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog")
    wd_thread.start()

    # API local na 127.0.0.1:17777 — UI da web chama isso pra abrir Chrome direto.
    api_thread = threading.Thread(target=_local_api_loop, daemon=True, name="local-api")
    api_thread.start()

    # N threads de execução de jobs
    workers: list[threading.Thread] = []
    for i in range(WORKER_CONCURRENCY):
        t = threading.Thread(target=_worker_loop, args=(i,), daemon=True, name=f"worker-{i}")
        t.start()
        workers.append(t)
        # Stagger no startup também — não inicia 2 polls no mesmo instante
        if i < WORKER_CONCURRENCY - 1:
            time.sleep(random.uniform(3, 8))

    try:
        while not _stop_flag.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[worker] encerrando…")
        _stop_flag.set()
        # Dá 3s pras threads finalizarem polling
        for t in workers:
            t.join(timeout=3)
        sys.exit(0)


if __name__ == "__main__":
    main()
