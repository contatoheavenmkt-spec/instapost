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

import os
import platform
import random
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

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


# ----------- Loop principal -----------

def _periodic_heartbeat():
    """Roda em thread separada — heartbeat a cada HEARTBEAT_INTERVAL_SECONDS."""
    while not _stop_flag.is_set():
        heartbeat()
        # Acorda se stop foi pedido
        if _stop_flag.wait(HEARTBEAT_INTERVAL_SECONDS):
            return


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
            # Se outra thread já está nessa conta, espera serializar
            acquired_now = lock.acquire(blocking=False)
            if not acquired_now:
                if WORKER_CONCURRENCY > 1:
                    print(f"[T{thread_id}] @{username} ocupada por outra thread, serializando…")
                lock.acquire()
            try:
                # Stagger anti-detecção quando há paralelismo: cada thread espera
                # 15-45s antes de começar (evita 2 logins exatos no mesmo segundo).
                if WORKER_CONCURRENCY > 1:
                    jitter = random.uniform(15, 45)
                    print(f"[T{thread_id}] aguarda {jitter:.0f}s antes de iniciar (stagger)")
                    if _stop_flag.wait(jitter):
                        return
                execute_job(job)
            finally:
                lock.release()
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
