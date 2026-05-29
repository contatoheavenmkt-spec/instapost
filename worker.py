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
from urllib.parse import urlparse

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
_client_cache: dict[str, tuple[object, float, Optional[str]]] = {}  # username -> (Client, last_used_ts, proxy_signature)
_client_cache_lock = threading.Lock()


def _proxy_signature(proxy: Optional[str]) -> Optional[str]:
    """Normaliza proxy pra comparação de igualdade entre jobs.
    Reusa _normalize_proxy do session.py pra que formatos diferentes do mesmo
    proxy (com/sem http://, user:pass:host:port vs URL) batam como iguais."""
    if not proxy:
        return None
    try:
        from core.session import _normalize_proxy
        return _normalize_proxy(proxy)
    except Exception:
        return proxy.strip()


def get_account_lock(username: str) -> threading.Lock:
    """Lock por conta — garante que 2 threads do worker nunca postam na mesma @ em paralelo."""
    with _account_locks_meta_lock:
        lock = _account_locks.get(username)
        if lock is None:
            lock = threading.Lock()
            _account_locks[username] = lock
        return lock


def get_cached_client(username: str, expected_proxy: Optional[str] = None):
    """Devolve Client cacheado se ainda fresco E proxy igual ao esperado.
    Se proxy do job mudou (user trocou de provedor), invalida — senão o Client
    velho sai pelo proxy antigo (esgotado/morto) mesmo com proxy novo configurado."""
    now = time.time()
    with _client_cache_lock:
        entry = _client_cache.get(username)
        if not entry:
            return None
        cl, last_used, cached_proxy_sig = entry
        if now - last_used >= CLIENT_CACHE_TTL_SECONDS:
            _client_cache.pop(username, None)
            return None
        # Proxy mismatch = invalida (user trocou proxy enquanto cache tava quente)
        expected_sig = _proxy_signature(expected_proxy)
        if expected_sig != cached_proxy_sig:
            print(f"[cache] invalidando @{username}: proxy mudou (cached={cached_proxy_sig[:30] if cached_proxy_sig else None}... -> novo={expected_sig[:30] if expected_sig else None}...)")
            _client_cache.pop(username, None)
            return None
        # Bump last_used
        _client_cache[username] = (cl, now, cached_proxy_sig)
        return cl


def store_client(username: str, cl, proxy: Optional[str] = None) -> None:
    with _client_cache_lock:
        _client_cache[username] = (cl, time.time(), _proxy_signature(proxy))


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


# ----------- Session sync (worker <-> servidor central) -----------

def _upload_session_to_server(username: str, session_data: dict):
    """Best-effort: envia sessão pro servidor central pra sincronizar entre workers.
    Se falhar, não impede nada — sessão local continua funcionando."""
    try:
        r = post("/api/worker/sessions/upload", {"username": username, "session_data": session_data})
        if r.status_code == 200:
            print(f"[sync] ✓ sessão de @{username} enviada pro servidor")
        else:
            print(f"[sync] ⚠️ upload falhou HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[sync] ⚠️ upload falhou: {e}")


def _download_session_from_server(username: str) -> bool:
    """Tenta baixar sessão do servidor central se não existir localmente.
    Permite que worker novo use sessão criada em outro PC.
    Retorna True se baixou com sucesso."""
    try:
        r = get(f"/api/worker/sessions/download?username={username}")
        if r.status_code != 200:
            return False
        data = r.json()
        session_data = data.get("session_data")
        if not session_data:
            return False
        # Salva localmente na estrutura de workspaces
        project_root = Path(__file__).resolve().parent
        data_dir = Path(os.environ.get("DATA_DIR", str(project_root)))
        ws_slug = data.get("workspace_slug", "default")
        target_dir = data_dir / "workspaces" / ws_slug / "sessions"
        target_dir.mkdir(parents=True, exist_ok=True)
        session_file = target_dir / f"{username}.json"
        session_file.write_text(
            json.dumps(session_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[sync] ✓ sessão de @{username} baixada do servidor (ws={ws_slug})")
        return True
    except Exception as e:
        print(f"[sync] download falhou ({e}) — seguindo sem sessão remota")
        return False


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

    # CRÍTICO: seta workspace do job ANTES de qualquer operação.
    # Sem isso, SESSIONS_DIR aponta pro workspace default e não encontra
    # sessões de contas em outros workspaces.
    ws_slug = job.get("workspace_slug", "default")
    try:
        from core import paths as _paths_job
        _paths_job.set_workspace(ws_slug)
    except Exception:
        pass

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
    # IMPORTANTE: só test_login (Botão Conectar manual) pode disparar fresh login API.
    # Demais operations (post, auto_like, sync, collect_insights, etc) usam SÓ a sessão
    # salva — se expirou, falham suave sem disparar challenge automático no Insta.
    def do_login():
        job_proxy = job.get("account_proxy")
        cached = get_cached_client(username, expected_proxy=job_proxy)
        if cached is not None:
            log(f"♻️ sessão Insta em cache (sem relogin)")
            return cached
        # Sync: se não tem sessão local, tenta baixar do servidor central
        if not _find_session_file(username):
            if _download_session_from_server(username):
                log("📥 sessão sincronizada do servidor")
        report_step(job_id, "logging")
        cl = get_client(
            username=username,
            password=job["account_password"],
            proxy=job_proxy,
            totp_secret=job.get("account_totp_secret"),
            allow_fresh_login=(operation == "test_login"),
        )
        store_client(username, cl, proxy=job_proxy)
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
            # Sync: envia sessão pro servidor pra outros workers usarem
            try:
                _sess_path = _find_session_file(username)
                if _sess_path:
                    _sess_data = json.loads(_sess_path.read_text(encoding="utf-8"))
                    _upload_session_to_server(username, _sess_data)
            except Exception:
                pass
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

    # Carrega sessão (cookies do Chrome) pra usar na Web API
    # Sync: se não tem sessão local, tenta baixar do servidor central
    if not _find_session_file(username):
        if _download_session_from_server(username):
            log("📥 sessão sincronizada do servidor")

    report_step(job_id, "logging")
    session_file = _find_session_file(username)
    if not session_file:
        log(f"❌ sem sessão pra @{username} — faça Save Sessão via Chrome")
        report_result(job_id, False, error_msg=f"login: Conta @{username} sem sessão salva. Abra Chrome via Smartphone, loga manual e clica Salvar Sessão.")
        return

    try:
        session_data = json.loads(session_file.read_text(encoding="utf-8"))
        log(f"✓ sessão carregada de @{username}")
    except Exception as e:
        log(f"❌ erro lendo sessão: {e}")
        report_result(job_id, False, error_msg=f"login: erro lendo sessão: {e}")
        return

    # Posta via Web API (usa cookies do Chrome, sem instagrapi)
    report_step(job_id, "posting")
    try:
        from core.web_poster import web_post_reel, web_post_story_video, web_post_story_photo

        kind = job.get("kind", "reel")
        media_type = job.get("media_type", "video")
        caption = job.get("caption", "")
        link_url = job.get("link_url")
        link_text = job.get("link_text") or "Clique aqui"

        log(f"postando ({kind}, {media_type}){' + link [' + link_text + ']' if link_url else ''}")

        if kind == "story":
            if media_type == "photo":
                try:
                    from core.media import normalize_image_for_story
                    log("normalizando foto pra story (1080x1920)")
                    normalize_image_for_story(media_path)
                except Exception as ne:
                    log(f"⚠️ normalização falhou ({ne}) — tentando com original")
                result = web_post_story_photo(session_data, str(media_path), caption, link_url, link_text)
            else:
                result = web_post_story_video(session_data, str(media_path), caption, link_url, link_text)
        else:
            if media_type == "photo":
                result = {"success": False, "media_id": None, "error": "Foto não pode virar Reel"}
            else:
                result = web_post_reel(session_data, str(media_path), caption)

        if result.get("success"):
            mid = result.get("media_id")
            log(f"✅ postado! media_id={mid}")
            report_result(job_id, True, media_id=mid)
        else:
            log(f"❌ post falhou: {result.get('error')}")
            report_result(job_id, False, error_msg=result.get("error"))

    except Exception as e:
        log(f"❌ exceção: {e}")
        report_result(job_id, False, error_msg=str(e))

    finally:
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


def _kill_chrome_for_profile_any(username_safe: str) -> None:
    """Mata QUALQUER Chrome rodando com profile dessa @ (qualquer timestamp).
    Usado pela nova estratégia de profile dirs únicos por launch — antes de
    abrir Chrome novo, mata todos os Chromes de launches anteriores dessa @."""
    if platform.system() != "Windows":
        return
    try:
        for attempt in range(3):
            # Match: chrome.exe com InstaposterProfiles\<username>(_timestamp ou sem) no cmdline
            ps_get = (
                f"Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*InstaposterProfiles*{username_safe}*' }} | "
                f"Select-Object -ExpandProperty ProcessId"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps_get],
                capture_output=True, text=True, timeout=5,
            )
            pids = [p.strip() for p in (result.stdout or "").splitlines() if p.strip().isdigit()]
            if not pids:
                break
            if attempt == 0:
                print(f"[local-api] matando {len(pids)} Chrome(s) anterior(es) de @{username_safe}")
            for pid in pids:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", pid],
                        capture_output=True, timeout=3,
                    )
                except Exception:
                    pass
            time.sleep(1.0)
        time.sleep(0.3)
    except Exception as e:
        print(f"[local-api] kill Chrome anterior falhou (ignorando): {e}")


def _kill_chrome_for_profile(profile_dir: Path) -> None:
    """Mata chrome.exe que está usando esse profile dir + remove lock files.

    Necessário porque se já tem Chrome aberto nessa mesma --user-data-dir, o
    novo launch vira IPC client (ignora --remote-debugging-port). Sem
    debug port, Save Sessão não funciona.

    Estratégia robusta:
    1. Lista TODAS as PIDs do Chrome com essa profile (filtro cmdline)
    2. Mata cada uma com taskkill /F /T (force + tree — mata renderers/GPU/network)
    3. Repete até 3x se Chrome auto-restart
    4. Remove SingletonLock/Cookie/Socket pro novo Chrome iniciar limpo
    """
    if platform.system() != "Windows":
        return
    marker = profile_dir.name
    try:
        for attempt in range(3):
            # Pega PIDs ativas
            ps_get = (
                f"Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*InstaposterProfiles*{marker}*' }} | "
                f"Select-Object -ExpandProperty ProcessId"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps_get],
                capture_output=True, text=True, timeout=5,
            )
            pids = [p.strip() for p in (result.stdout or "").splitlines() if p.strip().isdigit()]
            if not pids:
                break  # nada pra matar
            if attempt == 0:
                print(f"[local-api] matando {len(pids)} Chrome(s) anterior(es) de @{marker}")
            # taskkill /F /T = force + tree (mata processo + filhos)
            for pid in pids:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", pid],
                        capture_output=True, timeout=3,
                    )
                except Exception:
                    pass
            time.sleep(1.0)
        # Remove SingletonLock e amigos — Chrome usa pra detectar "outra instância"
        # Se sobrou (Chrome morreu sem cleanup), bloqueia novo Chrome de iniciar.
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                lock_file = profile_dir / lock_name
                if lock_file.exists() or lock_file.is_symlink():
                    lock_file.unlink()
            except Exception:
                pass
        time.sleep(0.3)  # margem pra FS liberar
    except Exception as e:
        print(f"[local-api] kill Chrome anterior falhou (ignorando): {e}")


# ===== Throttle de abertura do launcher (anti-batch-detection) =====
# Antes: 5min/@ + 60s global — agressivo demais agora que cada conta usa
# proxy sticky DIFERENTE (sem batch pattern detectável pelo IG).
# Agora: 30s/@ (anti double-click acidental) + 5s global (anti flood).
_last_open_per_user: dict[str, float] = {}
_last_open_global: float = 0.0
_throttle_lock = threading.Lock()
THROTTLE_PER_USER_SECONDS = 30
THROTTLE_GLOBAL_SECONDS = 5


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
    """Gera extensão Chrome temporária pra auth automática de proxy.

    Usa Manifest V3 (MV2 tá deprecated em Chrome 127+ e estava deixando o
    browser em branco). MV3 pra onAuthRequired precisa:
    - permission 'webRequest' + 'webRequestAuthProvider'
    - listener com modo 'asyncBlocking'

    Sem essa extensão, Chrome popa modal pedindo user/senha do proxy
    em CADA request (chato e quebra carregamento de página)."""
    extension_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": 3,
        "name": "Insta Poster Proxy Auth",
        "version": "1.0",
        "permissions": ["webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "bg.js"},
    }
    bg_js = (
        "// Auto-responde ao proxy auth challenge.\n"
        "// asyncBlocking permite resolver o callback async com credenciais.\n"
        "chrome.webRequest.onAuthRequired.addListener(\n"
        "  function(details, callback) {\n"
        "    // Só responde se for auth do PROXY (não auth de sites)\n"
        "    if (details.isProxy) {\n"
        "      callback({authCredentials: {\n"
        f"        username: {json.dumps(proxy_user)},\n"
        f"        password: {json.dumps(proxy_pass)}\n"
        "      }});\n"
        "    } else {\n"
        "      callback();\n"
        "    }\n"
        "  },\n"
        "  {urls: ['<all_urls>']},\n"
        "  ['asyncBlocking']\n"
        ");\n"
        "console.log('[InstaPosterProxyAuth] MV3 listener registrado');\n"
    )
    (extension_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
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
    clean_mobile: bool = False,
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

    # ESTRATÉGIA: profile dir PERSISTENTE por conta (sem timestamp).
    # Vantagem: cookies do Chrome ficam entre launches → Smartphone abre
    # JÁ LOGADO depois da 1ª vez. Requer kill robusto de Chromes anteriores
    # antes de relançar pra evitar conflito singleton.
    profile_base = Path.home() / "InstaposterProfiles"
    profile_base.mkdir(parents=True, exist_ok=True)

    # Migration cleanup: remove profiles timestamped antigos (legado do experimento
    # com timestamp). Move cookies relevantes pro persistente se for o caso.
    try:
        import shutil as _sh_migrate
        for old in profile_base.glob(f"{safe}_*"):
            try:
                if old.is_dir() and old.name != safe:
                    _sh_migrate.rmtree(old, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass

    # Mata QUALQUER Chrome rodando com profile dessa @ (com ou sem timestamp).
    # Sem isso, novo launch vira IPC client da instância antiga (sem debug port).
    _kill_chrome_for_profile_any(safe)

    # Profile dir PERSISTENTE: <username> (sem timestamp)
    profile_dir = profile_base / safe

    if reset:
        # Reset apaga TUDO da conta
        try:
            import shutil
            if profile_dir.exists():
                shutil.rmtree(profile_dir, ignore_errors=True)
            for old in profile_base.glob(f"{safe}*"):
                shutil.rmtree(old, ignore_errors=True)
            print(f"[local-api] 🧹 profile de @{safe} resetado")
        except Exception as e:
            print(f"[local-api] ⚠️ falha resetando profile: {e}")

    profile_dir.mkdir(parents=True, exist_ok=True)

    # Remove SingletonLock e DevToolsActivePort antigo (sobra quando Chrome morre sem cleanup)
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort"):
        try:
            lf = profile_dir / lock_name
            if lf.exists() or lf.is_symlink():
                lf.unlink()
        except Exception:
            pass

    # === MODO LOGIN MANUAL (clean_mobile=True) ===
    # Originalmente era mobile UA estilo Dolphin Anty, MAS Instagram mobile web
    # NÃO PERMITE login confiável — redireciona pra página de marketing pedindo
    # baixar o app, mesmo digitando credenciais corretas. Pra login efetivo,
    # precisa do desktop web do Insta.
    #
    # Configuração agora:
    # - Desktop UA Chrome 131 Windows (Insta aceita login normal)
    # - Window 1280x800 (desktop layout completo)
    # - SEM cookie inject (login fresh manual)
    # - Proxy da conta SE configurado (via forwarder local que resolve auth)
    cdp_cookies: list[dict] = []
    if clean_mobile:
        # Desktop UA — Instagram desktop web aceita login manual confiavelmente.
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
        window_size = "1280,800"
        target_url = "https://www.instagram.com/accounts/login/"
        print(f"[local-api] 🖥️ modo login manual: desktop UA + 1280x800 + proxy={'sim' if proxy else 'não'}")
    else:
        # Modo antigo: tenta auto-login com cookies + proxy
        if not reset:
            session_path = _find_session_file(safe)
            if session_path:
                cdp_cookies = _extract_cdp_cookies(session_path)
                if not cdp_cookies:
                    print(f"[local-api] sessão {session_path.name} sem cookies utilizáveis (provavelmente vazia)")
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
        window_size = "1280,800"
        target_url = "https://www.instagram.com/"

    inject = bool(cdp_cookies) and not clean_mobile
    debug_port = _get_free_port()  # SEMPRE habilita debug port pra Save Session funcionar

    # === Flags anti-detect + anti-leak ===
    args = [
        chrome,
        f"--user-data-dir={profile_dir}",
        f"--user-agent={user_agent}",
        f"--window-size={window_size}",
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
    if clean_mobile:
        # Touch events emulation pra IG servir layout mobile
        args.append("--touch-events=enabled")

    # === Proxy COERENTE com o worker (CRÍTICO pra não flagar batch login) ===
    # Modo no_proxy: abre Chrome com IP RESIDENCIAL (sem proxy nenhum). Use SÓ
    # pra logar manualmente em conta nova/comprada — Chrome aceita auth do IG mais
    # facilmente quando o IP "bate" com o do celular onde a conta foi criada.
    # AVISO: depois de logar manual, vai criar inconsistência se worker tentar
    # logar via proxy depois — IG vê IP diferente. Solução: logar tudo via proxy
    # OU desativar proxy permanentemente nessa conta.
    if no_proxy:
        proxy = None
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
        # NOVA ESTRATÉGIA: Local Forwarder em vez de extensão Chrome.
        # Chrome conecta em 127.0.0.1:PORT_LOCAL (sem auth dialog), forwarder
        # em Python autentica com o upstream (DataImpulse). Bypassa o bug do
        # auth dialog repetitivo que MV2/MV3 extension não resolvia.
        try:
            from core.proxy_forwarder import start_forwarder
            fwd_server, fwd_port = start_forwarder(proxy)
            args.append(f"--proxy-server=http://127.0.0.1:{fwd_port}")
            print(f"[local-api] 🌐 Chrome via forwarder local 127.0.0.1:{fwd_port} → upstream {urlparse(proxy).hostname}")
            # NOTA: forwarder roda em daemon thread, morre com o worker.
            # Pra fechar antes (quando Chrome fecha), seria preciso watcher
            # do processo Chrome — não implementado por enquanto, OK pra MVP.
        except Exception as e:
            print(f"[local-api] ⚠️ falha iniciando forwarder ({e}) — tentando direto sem auth")
            # Fallback: passa proxy direto pro Chrome (vai falhar com auth dialog
            # mas pelo menos algo tenta)
            chrome_proxy, _, _ = _parse_proxy_for_chrome(proxy)
            if chrome_proxy:
                args.append(f"--proxy-server={chrome_proxy}")

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
        # Debug: imprime args (ofusca paths/tokens longos)
        debug_args = [a if len(a) < 100 else a[:80] + "..." for a in args]
        print(f"[local-api] launching Chrome PID-novo args: {' '.join(debug_args)}", flush=True)
        proc_chrome = subprocess.Popen(args, close_fds=True, creationflags=creationflags)
        print(f"[local-api] Chrome lançado PID={proc_chrome.pid} profile={profile_dir}", flush=True)
    except Exception as e:
        return False, f"Popen falhou: {e}"

    # Aguarda Chrome escrever DevToolsActivePort (até 12s)
    # Se não aparecer, Chrome possivelmente atachou a outro Chrome OU falhou.
    port_file = profile_dir / "DevToolsActivePort"
    import time as _t_wait
    deadline = _t_wait.time() + 12
    while _t_wait.time() < deadline:
        if port_file.exists():
            try:
                lines = port_file.read_text(encoding="utf-8").strip().split("\n")
                actual_port = int(lines[0])
                print(f"[local-api] ✓ DevToolsActivePort criado: porta={actual_port} em {profile_dir.name}", flush=True)
                # Atualiza debug_port pra usar a porta REAL que Chrome bindou
                debug_port = actual_port
                break
            except Exception:
                pass
        _t_wait.sleep(0.4)
    else:
        print(f"[local-api] ⚠️ DevToolsActivePort NÃO criado em 12s em {profile_dir}", flush=True)
        # Chrome 148+ pode não criar o arquivo. Verifica se a porta responde e cria manualmente.
        try:
            import urllib.request as _urlr_check
            _urlr_check.urlopen(f"http://127.0.0.1:{debug_port}/json/version", timeout=3).read()
            port_file.write_text(f"{debug_port}\n", encoding="utf-8")
            print(f"[local-api] ✓ DevToolsActivePort criado manualmente (Chrome 148+): porta={debug_port}", flush=True)
        except Exception:
            print(f"[local-api]    → Chrome atachou a outra instância OU falhou ao bindar porta", flush=True)
            # Lista processos chrome pra diagnóstico
            try:
                ps_get = (
                    f"Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                    f"Where-Object {{ $_.CommandLine -like '*{safe}*' }} | "
                    f"Select-Object ProcessId,CommandLine | Format-List"
                )
                r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps_get],
                                  capture_output=True, text=True, timeout=5)
                print(f"[local-api]    Chromes com '{safe}' no cmdline:\n{r.stdout[:1500]}")
            except Exception:
                pass

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

    # Estratégia: profiles têm timestamp suffix (<user>_<ts>).
    profile_base = Path.home() / "InstaposterProfiles"
    candidates: list[Path] = []
    if (profile_base / safe).exists():
        candidates.append(profile_base / safe)
    if profile_base.exists():
        candidates.extend(sorted(
            profile_base.glob(f"{safe}_*"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        ))

    # Etapa 1: tenta achar Chrome rodando E debug port ativo.
    # Método A: DevToolsActivePort file no profile dir (preferido)
    # Método B: Extrair --remote-debugging-port do cmdline + verificar se debug
    #           server responde (fallback quando DevToolsActivePort não foi escrito)
    profile_dir = None
    debug_port_found = None

    # Método A: scan profile dirs por DevToolsActivePort
    for cand in candidates:
        port_file_check = cand / "DevToolsActivePort"
        if port_file_check.exists():
            try:
                lines = port_file_check.read_text(encoding="utf-8").strip().split("\n")
                debug_port_found = int(lines[0])
                profile_dir = cand
                break
            except Exception:
                pass

    # Método B: se A falhou, scan Chrome processes pra achar debug port no cmdline
    if profile_dir is None and platform.system() == "Windows":
        try:
            import re as _re_cmd
            ps_get = (
                f"Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*InstaposterProfiles*{safe}*' "
                f"-and $_.CommandLine -like '*remote-debugging-port*' "
                f"-and $_.CommandLine -notlike '*--type=*' }} | "
                f"Select-Object -ExpandProperty CommandLine"
            )
            result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps_get],
                                  capture_output=True, text=True, timeout=5)
            for line in (result.stdout or "").splitlines():
                m_port = _re_cmd.search(r"remote-debugging-port=(\d+)", line)
                # Regex robusta: suporta paths com espaço (ex: "C:\Users\Edson Juan\...")
                m_dir = _re_cmd.search(r'--user-data-dir="?([^"]+?)"?\s', line) or \
                        _re_cmd.search(r"--user-data-dir=([^\"\s]+)", line) or \
                        _re_cmd.search(r"InstaposterProfiles[\\\\/](\w[\w.-]*)", line)
                if m_port and m_dir:
                    port_candidate = int(m_port.group(1))
                    # Verifica se debug server REALMENTE responde
                    try:
                        import urllib.request as _urlr
                        _urlr.urlopen(f"http://127.0.0.1:{port_candidate}/json/version", timeout=2).read()
                        # Sucesso: porta tá listening
                        dir_name = m_dir.group(1).split("\\")[-1].split("/")[-1] if m_dir.group(1).startswith("--") else m_dir.group(1)
                        # Reconstrói path do profile dir
                        possible_profile = profile_base / dir_name
                        if possible_profile.exists():
                            profile_dir = possible_profile
                            debug_port_found = port_candidate
                            print(f"[local-api] 💡 achou Chrome debug via cmdline scan: porta={port_candidate} profile={dir_name}")
                            break
                    except Exception:
                        # Porta no cmdline mas não responde — Chrome bindou em outra OU não bindou
                        continue
        except Exception as e:
            print(f"[local-api] cmdline scan falhou: {e}")

    if profile_dir is None:
        # Diagnóstico detalhado
        candidates_exist = any(c.exists() for c in candidates)
        chrome_running = False
        running_profiles: list[str] = []
        if platform.system() == "Windows":
            try:
                ps_cmd = (
                    "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                    "Where-Object { $_.CommandLine -like '*InstaposterProfiles*' } | "
                    "Select-Object -ExpandProperty CommandLine"
                )
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True, text=True, timeout=5,
                )
                lines = (result.stdout or "").strip().splitlines()
                for line in lines:
                    # Extrai o nome da profile (após InstaposterProfiles\), com ou sem _timestamp
                    import re as _re
                    m = _re.search(r"InstaposterProfiles[\\/](\w[\w.-]*)", line)
                    if m:
                        # Remove timestamp suffix (_NNN) se houver
                        name = _re.sub(r"_\d+$", "", m.group(1))
                        running_profiles.append(name)
                chrome_running = bool(running_profiles)
            except Exception:
                pass

        if chrome_running and safe in running_profiles:
            msg = (
                f"Chrome tá aberto pra @{safe} MAS sem debug port (DevToolsActivePort não existe). "
                f"Provavel: Chrome foi aberto sem o --remote-debugging-port. "
                f"FECHE essa janela Chrome e clique Smartphone DE NOVO."
            )
        elif chrome_running:
            msg = (
                f"Você tem Chrome aberto pra: {', '.join('@' + p for p in running_profiles)}. "
                f"Mas NÃO pra @{safe}. Você quer Salvar Sessão de qual conta? "
                f"Clique 'Salvar Sessão' na linha da conta CORRETA OU abra @{safe} via Smartphone primeiro."
            )
        else:
            msg = (
                f"Nenhum Chrome aberto pra essa conta. "
                f"Profiles candidatos: {[str(c.name) for c in candidates] or 'nenhum'} (existe: {candidates_exist}). "
                f"Clica Smartphone na @{safe} primeiro, loga, mantem Chrome aberto e DAÍ clica Salvar Sessão."
            )
        return False, msg

    # Porta debug já descoberta acima (Método A: DevToolsActivePort, ou B: cmdline scan)
    port = debug_port_found
    if not port:
        return False, "Não consegui descobrir a porta debug do Chrome"

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
            # UA deve bater com o Chrome que fez o login (desktop, não mobile)
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "user_agent_mobile": "Instagram 428.0.0.47.67 Android (34/14; 480dpi; 1344x2992; Google/google; Pixel 8 Pro; husky; husky; pt_BR; 961145276)",
            "country": "BR",
            "country_code": 55,
            "locale": "pt_BR",
            "timezone_offset": -10800,
            # Marker: sessão salva manualmente via Chrome.
            # session.py respeita esse flag e PULA o teste leve (get_timeline_feed)
            # que poderia falhar por rate limit temporário e disparar fresh login
            # API = challenge = conta morre. Sessão manual é tratada como
            # confiável até dar 401 real.
            "manually_saved": True,
            "from_chrome": True,
            "saved_at": int(time.time()),
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
        # Sync: envia sessão pro servidor central pra outros workers usarem
        try:
            _upload_session_to_server(safe, session_data)
        except Exception as _sync_err:
            print(f"[sync] ⚠️ upload pro servidor falhou ({_sync_err}) — sessão local OK")
        return True, f"Sessão salva em {session_file.name} ({len(ig_cookies)} cookies, sessionid: ...{sessionid[-12:]})"
    finally:
        try:
            ws_conn.close()
        except Exception:
            pass


def _auto_login_flow(username: str, password: str, email: str = None, proxy: str = None):
    """Abre Chrome, preenche login/senha via CDP, busca código no tempmail."""
    import websocket as _ws_auto

    print(f"[auto-login] iniciando pra @{username}")

    ok, info = _open_chrome_for_account(username, reset=True, proxy=proxy, clean_mobile=True)
    if not ok:
        print(f"[auto-login] falha abrindo Chrome: {info}")
        return

    safe = "".join(c for c in username if c.isalnum() or c in "._-")
    profile_dir = Path.home() / "InstaposterProfiles" / safe
    port_file = profile_dir / "DevToolsActivePort"

    deadline = time.time() + 20
    debug_port = None
    while time.time() < deadline:
        if port_file.exists():
            try:
                debug_port = int(port_file.read_text("utf-8").strip().split("\n")[0])
                break
            except Exception:
                pass
        time.sleep(0.5)

    if not debug_port:
        print(f"[auto-login] DevToolsActivePort não encontrado")
        return

    time.sleep(2)

    try:
        import urllib.request as _urlr_auto
        r = _urlr_auto.urlopen(f"http://127.0.0.1:{debug_port}/json", timeout=5)
        targets = json.loads(r.read().decode("utf-8"))
        page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            return
        ws = _ws_auto.create_connection(page["webSocketDebuggerUrl"], timeout=10)
    except Exception as e:
        print(f"[auto-login] falha CDP: {e}")
        return

    msg_id = [0]

    def cdp_send(method, params=None):
        msg_id[0] += 1
        ws.send(json.dumps({"id": msg_id[0], "method": method, "params": params or {}}))
        for _ in range(5):
            try:
                resp = json.loads(ws.recv())
                if resp.get("id") == msg_id[0]:
                    return resp.get("result", {})
            except Exception:
                break
        return {}

    def cdp_eval(expression):
        return cdp_send("Runtime.evaluate", {"expression": expression})

    try:
        print(f"[auto-login] preenchendo login @{username}")
        cdp_eval(f'''
            (function() {{
                var el = document.querySelector('input[name="email"]') ||
                         document.querySelector('input[name="username"]') ||
                         document.querySelector('input[autocomplete*="username"]');
                if (!el) return "no_field";
                var nativeSet = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                nativeSet.call(el, "{username}");
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                return "ok";
            }})()
        ''')
        time.sleep(0.3)

        escaped_pw = password.replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'")
        cdp_eval(f'''
            (function() {{
                var el = document.querySelector('input[name="pass"]') ||
                         document.querySelector('input[name="password"]') ||
                         document.querySelector('input[type="password"]');
                if (!el) return "no_field";
                var nativeSet = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                nativeSet.call(el, "{escaped_pw}");
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                return "ok";
            }})()
        ''')
        time.sleep(0.3)

        print(f"[auto-login] clicando login")
        cdp_eval('(function() { var btn = document.querySelector("button[type=\\"submit\\"]"); if (btn) btn.click(); return "ok"; })()')

        print(f"[auto-login] aguardando resposta (5s)...")
        time.sleep(5)

        if email:
            # Reconecta CDP (página pode ter mudado)
            try:
                ws.close()
            except Exception:
                pass
            try:
                r2 = _urlr_auto.urlopen(f"http://127.0.0.1:{debug_port}/json", timeout=5)
                targets2 = json.loads(r2.read().decode("utf-8"))
                page2 = next((t for t in targets2 if t.get("type") == "page"), None)
                if page2:
                    ws2 = _ws_auto.create_connection(page2["webSocketDebuggerUrl"], timeout=10)
                    # Reatribui pra usar nas funções
                    msg_id[0] = 0
                    def cdp_eval2(expr):
                        msg_id[0] += 1
                        ws2.send(json.dumps({"id": msg_id[0], "method": "Runtime.evaluate", "params": {"expression": expr}}))
                        for _ in range(5):
                            try:
                                resp = json.loads(ws2.recv())
                                if resp.get("id") == msg_id[0]:
                                    return resp.get("result", {})
                            except Exception:
                                break
                        return {}
            except Exception:
                pass

            print(f"[auto-login] buscando código em {email}...")
            try:
                from core.tempmail import fetch_instagram_code
                code = fetch_instagram_code(email, timeout=120, poll_interval=4)
                if code:
                    print(f"[auto-login] CÓDIGO: {code}")
                    # Mostra no título do Chrome
                    try:
                        cdp_eval2(f'document.title = "CÓDIGO: {code}"')
                    except Exception:
                        pass
                    # Preenche código
                    try:
                        cdp_eval2(f'''
                            (function() {{
                                var inputs = document.querySelectorAll('input[name="security_code"], input[name="verificationCode"], input[type="text"], input[type="number"]');
                                for (var i = 0; i < inputs.length; i++) {{
                                    var el = inputs[i];
                                    if (el.offsetParent !== null) {{
                                        var nativeSet = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                                        nativeSet.call(el, "{code}");
                                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                                        return "filled";
                                    }}
                                }}
                                return "no_input";
                            }})()
                        ''')
                        time.sleep(0.5)
                        cdp_eval2('''
                            (function() {
                                var btns = document.querySelectorAll('button[type="button"], button[type="submit"]');
                                for (var i = 0; i < btns.length; i++) {
                                    var txt = btns[i].textContent.toLowerCase();
                                    if (txt.includes("confirm") || txt.includes("enviar") || txt.includes("submit") || txt.includes("next") || txt.includes("continuar")) {
                                        btns[i].click();
                                        return "clicked";
                                    }
                                }
                                var last = btns[btns.length - 1];
                                if (last) last.click();
                                return "fallback";
                            })()
                        ''')
                        print(f"[auto-login] código preenchido e confirmado!")
                    except Exception as e:
                        print(f"[auto-login] erro preenchendo código: {e}")
                else:
                    print(f"[auto-login] código não chegou — preencha manualmente")
            except Exception as e:
                print(f"[auto-login] erro buscando código: {e}")
        else:
            print(f"[auto-login] sem email — preencha código manualmente se pedir")

        print(f"[auto-login] concluído pra @{username} — clique 'Salvar Sessão'")

    except Exception as e:
        print(f"[auto-login] erro: {e}")
    finally:
        try:
            ws.close()
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

        # /auto-login: abre Chrome, preenche login/senha, busca código email
        if parsed.path == "/auto-login":
            qs = parse_qs(parsed.query)
            username = (qs.get("username") or [""])[0].strip()
            if not username:
                self._send(400, b'{"error":"username obrigatorio"}')
                return
            password = None
            email = None
            proxy = None
            try:
                content_len = int(self.headers.get("Content-Length", "0"))
                if content_len > 0:
                    raw = self.rfile.read(content_len)
                    body_data = _json.loads(raw.decode("utf-8"))
                    password = (body_data.get("password") or "").strip() or None
                    email = (body_data.get("email") or "").strip() or None
                    proxy = (body_data.get("proxy") or "").strip() or None
            except Exception:
                pass
            if not password:
                self._send(400, b'{"error":"password obrigatorio no body JSON"}')
                return
            threading.Thread(
                target=_auto_login_flow,
                args=(username, password, email, proxy),
                daemon=True,
            ).start()
            body = _json.dumps({"ok": True, "message": f"Auto-login iniciado pra @{username}"}).encode("utf-8")
            self._send(200, body)
            return

        if parsed.path != "/open-browser":
            self._send(404, b'{"error":"not found"}')
            return
        qs = parse_qs(parsed.query)
        username = (qs.get("username") or [""])[0].strip()
        reset = (qs.get("reset") or ["0"])[0] in ("1", "true", "yes")
        no_proxy = (qs.get("no_proxy") or ["0"])[0] in ("1", "true", "yes")
        clean_mobile = (qs.get("clean_mobile") or ["0"])[0] in ("1", "true", "yes")
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
        ok, info = _open_chrome_for_account(username, reset=reset, proxy=proxy, no_proxy=no_proxy, clean_mobile=clean_mobile)
        if ok:
            body = _json.dumps({"ok": True, "device": info, "reset": reset, "clean_mobile": clean_mobile, "proxy_used": bool(proxy) and not no_proxy and not clean_mobile}).encode("utf-8")
            self._send(200, body)
            emoji = "📲" if clean_mobile else ("⚠️" if no_proxy else ("🧹" if reset else "📱"))
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

    # Heartbeat inicial com retry (até 60s) — cobre race condition em docker compose:
    # worker pode iniciar antes do app terminar de subir (FastAPI demora ~5-10s).
    # Sem retry, worker morre com exit(2) e Docker fica recriando container.
    info = None
    MAX_BOOTSTRAP_ATTEMPTS = 30  # 30 * 2s = 60s de tolerância
    for attempt in range(MAX_BOOTSTRAP_ATTEMPTS):
        info = heartbeat()
        if info:
            break
        if attempt == 0:
            print(f"Aguardando servidor responder... (até {MAX_BOOTSTRAP_ATTEMPTS * 2}s)")
        elif (attempt + 1) % 5 == 0:
            print(f"  ainda tentando ({attempt + 1}/{MAX_BOOTSTRAP_ATTEMPTS})...")
        time.sleep(2)

    if not info:
        print(f"Heartbeat inicial falhou após {MAX_BOOTSTRAP_ATTEMPTS * 2}s.")
        print("Verifica:")
        print(f"  - SERVER_URL acessível: {SERVER_URL}")
        print(f"  - WORKER_TOKEN válido (gerado em /workers do painel)")
        print(f"  - App docker container está rodando")
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
