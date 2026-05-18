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
import socket
import sys
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

# Diretório temp pra baixar mídia
TMP_DIR = Path(__file__).resolve().parent / ".worker_tmp"
TMP_DIR.mkdir(exist_ok=True)


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
            add_story_to_highlight,
        )
    except ImportError as e:
        log(f"❌ módulos core não encontrados: {e}")
        log("Rode o worker dentro da pasta do projeto: python worker.py")
        report_result(job_id, False, error_msg=f"import: {e}")
        return

    # Helper: faz login do Instagram (compartilhado entre operations)
    def do_login():
        return get_client(
            username=username,
            password=job["account_password"],
            proxy=job.get("account_proxy"),
            totp_secret=job.get("account_totp_secret"),
        )

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
    # OPERAÇÃO: post (default)
    # =====================================================
    log(f"iniciando job: {job['video_name']} ({job['kind']}) -> @{username}")

    # Baixa mídia
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

    # Login Instagram
    try:
        log(f"fazendo login no Instagram (sessão local, se existir)")
        cl = get_client(
            username=username,
            password=job["account_password"],
            proxy=job.get("account_proxy"),
            totp_secret=job.get("account_totp_secret"),
        )
        log(f"✓ logado como @{username}")
    except Exception as e:
        log(f"❌ falha login: {e}")
        report_result(job_id, False, error_msg=f"login: {e}")
        return

    # Posta
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
            log(f"✅ postado! media_id={result.get('media_id')}")

            # Se foi Story + tem destaque configurado, adiciona ao destaque
            highlight_info = None
            if kind == "story":
                highlight_title = params.get("auto_highlight_title") or job.get("auto_highlight_title")
                if highlight_title and result.get("media_id"):
                    log(f"📌 adicionando ao destaque '{highlight_title}'")
                    try:
                        hr = add_story_to_highlight(cl, result["media_id"], highlight_title)
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
                media_id=result.get("media_id"),
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
    print(f"  Server:   {SERVER_URL}")
    print(f"  Nome:     {WORKER_NAME}")
    print(f"  Platform: {PLATFORM}")
    print(f"=" * 60)
    print()

    # Heartbeat inicial pra validar token
    info = heartbeat()
    if not info:
        print("Heartbeat inicial falhou. Verifica SERVER_URL e WORKER_TOKEN.")
        sys.exit(2)
    print(f"✓ Conectado (worker_id: {info.get('worker_id')})")
    print(f"  Aguardando jobs... (poll {POLL_INTERVAL_SECONDS}s)")
    print()

    last_heartbeat = time.time()

    while True:
        try:
            # Heartbeat periódico
            if time.time() - last_heartbeat > HEARTBEAT_INTERVAL_SECONDS:
                heartbeat()
                last_heartbeat = time.time()

            # Pega próximo job (claim)
            job = fetch_next_job()
            if job:
                execute_job(job)
                # Após executar, manda heartbeat imediato
                heartbeat()
                last_heartbeat = time.time()
            else:
                time.sleep(POLL_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            print("\n[worker] encerrando…")
            sys.exit(0)
        except Exception as e:
            print(f"[loop] exceção: {e}")
            traceback.print_exc()
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
