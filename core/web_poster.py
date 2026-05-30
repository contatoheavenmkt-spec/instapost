"""
Poster via Web API do Instagram — usa cookies do Chrome (Save Session).

Não depende do instagrapi pra postar. Usa os mesmos endpoints que o
browser real (www.instagram.com/rupload_igvideo, configure_to_clips, etc).

Fluxo:
1. Upload video via rupload_igvideo
2. Upload thumbnail via rupload_igphoto
3. Configure (publish) via configure_to_clips (Reel) ou configure_to_story (Story)
"""
import json
import time
import uuid
import subprocess
from pathlib import Path
from typing import Optional

import requests


def _classify_error(response_json: dict = None, status_code: int = 0, raw_text: str = "") -> dict:
    """Classifica erros do Instagram pra que o worker tome ação adequada.

    Retorna dict com {success, media_id, error, error_type} onde error_type é:
      - "auth_expired": sessão expirou, precisa re-login
      - "rate_limited": muitas requisições, esperar
      - "blocked": conta bloqueada/checkpoint
      - "generic": outro erro
    """
    msg = ""
    if response_json:
        msg = response_json.get("message", "")

    error_type = "generic"
    if status_code in (401, 403) or "login_required" in msg:
        error_type = "auth_expired"
    elif status_code == 429 or "rate" in msg.lower() or "too many" in msg.lower() or "wait" in msg.lower():
        error_type = "rate_limited"
    elif "checkpoint" in msg or "suspended" in msg or "challenge" in msg:
        error_type = "blocked"

    detail = msg or raw_text or f"HTTP {status_code}"
    return {
        "success": False,
        "media_id": None,
        "error": f"[{error_type}] {detail[:300]}",
        "error_type": error_type,
    }


def _get_ffmpeg():
    """Retorna path do ffmpeg (imageio_ffmpeg ou sistema)."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _generate_thumbnail(video_path: Path) -> Optional[Path]:
    """Gera thumbnail 1080x1920 do video."""
    thumb = video_path.with_suffix(".jpg")
    try:
        ffmpeg = _get_ffmpeg()
        subprocess.run(
            [ffmpeg, "-y", "-i", str(video_path),
             "-ss", "00:00:01", "-vframes", "1",
             "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
             "-q:v", "2", str(thumb)],
            capture_output=True, timeout=30,
        )
        if thumb.exists() and thumb.stat().st_size > 0:
            return thumb
    except Exception as e:
        print(f"[web-poster] ffmpeg thumbnail falhou: {e}")
    return None


def _build_session(session_data: dict) -> requests.Session:
    """Cria requests.Session com cookies do Chrome."""
    s = requests.Session()
    cookies = session_data.get("cookies", {})
    auth = session_data.get("authorization_data", {})

    # Usa UA da sessão (deve ser o mesmo UA do Chrome que fez login).
    # Só substitui se estiver vazio — manter o UA original evita mismatch
    # de fingerprint que o IG detecta como bot.
    ua = session_data.get("user_agent", "")
    if not ua:
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"

    s.headers.update({
        "User-Agent": ua,
        "X-CSRFToken": cookies.get("csrftoken", ""),
        "X-IG-App-ID": "936619743392459",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
    })

    for name in ("sessionid", "ds_user_id", "csrftoken", "mid", "ig_did", "datr", "rur"):
        val = cookies.get(name) or auth.get(name) or ""
        if val:
            s.cookies.set(name, val, domain=".instagram.com")

    return s


def _upload_video(session: requests.Session, video_path: Path, upload_id: str,
                   for_story: bool = False) -> dict:
    """Upload video via rupload_igvideo. Usa streaming pra não carregar
    o arquivo inteiro em memória (vídeos podem ter 100MB+).

    for_story: se True, marca upload como story (obrigatório pra configure_to_story
    aceitar o upload_id — sem isso retorna 'incorrect upload id').
    """
    video_size = video_path.stat().st_size
    entity = f"{upload_id}_0_{uuid.uuid4().hex}"

    rupload_params = {
        "media_type": 2,
        "upload_id": upload_id,
        "upload_media_height": 1920,
        "upload_media_width": 1080,
    }
    if for_story:
        rupload_params["for_direct_story"] = "1"

    with open(video_path, "rb") as f:
        r = session.post(
            f"https://www.instagram.com/rupload_igvideo/{entity}",
            data=f,
            headers={
                "X-Entity-Name": entity,
                "X-Entity-Length": str(video_size),
                "X-Entity-Type": "video/mp4",
                "X-Instagram-Rupload-Params": json.dumps(rupload_params),
                "Offset": "0",
                "Content-Type": "video/mp4",
            },
            timeout=300,
        )

    return {"status_code": r.status_code, "response": r.json() if r.status_code == 200 else {"error": r.text[:300]}}


def _upload_thumbnail(session: requests.Session, thumb_path: Path, upload_id: str) -> dict:
    """Upload thumbnail via rupload_igphoto."""
    thumb_size = thumb_path.stat().st_size
    entity = f"{upload_id}_cover_0_{uuid.uuid4().hex}"

    with open(thumb_path, "rb") as f:
        r = session.post(
            f"https://www.instagram.com/rupload_igphoto/{entity}",
            data=f.read(),
            headers={
                "X-Entity-Name": entity,
                "X-Entity-Length": str(thumb_size),
                "X-Entity-Type": "image/jpeg",
                "X-Instagram-Rupload-Params": json.dumps({
                    "upload_id": upload_id,
                    "media_type": 2,
                    "waterfall_id": str(uuid.uuid4()),
                    "image_compression": json.dumps({"lib_name": "moz", "lib_version": "3.1.m", "quality": "80"}),
                    "is_sidecar": "0",
                }),
                "Offset": "0",
                "Content-Type": "image/jpeg",
            },
            timeout=60,
        )

    return {"status_code": r.status_code, "response": r.json() if r.status_code == 200 else {"error": r.text[:300]}}


def _upload_photo(session: requests.Session, photo_path: Path, upload_id: str,
                   for_story: bool = False) -> dict:
    """Upload foto via rupload_igphoto (pra story de foto)."""
    photo_size = photo_path.stat().st_size
    entity = f"{upload_id}_0_{uuid.uuid4().hex}"

    rupload_params = {
        "upload_id": upload_id,
        "media_type": 1,
        "waterfall_id": str(uuid.uuid4()),
        "image_compression": json.dumps({"lib_name": "moz", "lib_version": "3.1.m", "quality": "80"}),
    }
    if for_story:
        rupload_params["for_direct_story"] = "1"

    with open(photo_path, "rb") as f:
        r = session.post(
            f"https://www.instagram.com/rupload_igphoto/{entity}",
            data=f.read(),
            headers={
                "X-Entity-Name": entity,
                "X-Entity-Length": str(photo_size),
                "X-Entity-Type": "image/jpeg",
                "X-Instagram-Rupload-Params": json.dumps(rupload_params),
                "Offset": "0",
                "Content-Type": "image/jpeg",
            },
            timeout=60,
        )

    return {"status_code": r.status_code, "response": r.json() if r.status_code == 200 else {"error": r.text[:300]}}


def web_post_reel(session_data: dict, video_path: str, caption: str = "") -> dict:
    """
    Posta Reel via Web API usando cookies do Chrome.

    Returns:
        dict com {success, media_id, error}
    """
    video = Path(video_path)
    if not video.exists():
        return {"success": False, "media_id": None, "error": f"Arquivo não encontrado: {video_path}"}

    try:
        s = _build_session(session_data)
        upload_id = str(int(time.time() * 1000))

        # 1. Upload video
        print(f"[web-poster] uploading video ({video.stat().st_size // 1024} KB)...")
        v_result = _upload_video(s, video, upload_id)
        if v_result["status_code"] != 200:
            return _classify_error(v_result.get("response"), v_result["status_code"],
                                   str(v_result.get("response", {}).get("error", "")))
        print(f"[web-poster] video uploaded OK")

        # 2. Upload thumbnail
        thumb = _generate_thumbnail(video)
        if thumb:
            print(f"[web-poster] uploading thumbnail...")
            t_result = _upload_thumbnail(s, thumb, upload_id)
            if t_result["status_code"] == 200:
                print(f"[web-poster] thumbnail OK")
            else:
                print(f"[web-poster] thumbnail falhou (continuando sem): {t_result}")
        else:
            print(f"[web-poster] sem thumbnail (ffmpeg indisponível)")

        # 3. Publish as Reel (com retry pra transcode)
        configure_data = {
            "source_type": "library",
            "caption": caption or "",
            "upload_id": upload_id,
            "disable_comments": "0",
            "like_and_view_counts_disabled": "0",
            "igtv_share_preview_to_feed": "1",
            "is_unified_video": "1",
            "video_subtitles_enabled": "0",
        }

        # Instagram precisa de tempo pra transcodificar o video.
        # Retry até 5x com intervalo crescente.
        rj = None
        for attempt in range(6):
            if attempt > 0:
                wait = 5 + attempt * 5  # 10s, 15s, 20s, 25s, 30s
                print(f"[web-poster] aguardando transcode ({wait}s)...")
                time.sleep(wait)

            print(f"[web-poster] publishing reel (tentativa {attempt + 1})...")
            r = s.post(
                "https://www.instagram.com/api/v1/media/configure_to_clips/",
                data=configure_data,
                timeout=60,
            )

            if r.status_code == 200:
                rj = r.json()
                media = rj.get("media")
                if media:
                    pk = media.get("pk") or media.get("id")
                    print(f"[web-poster] REEL POSTADO! pk={pk}")
                    return {"success": True, "media_id": str(pk), "error": None}

                msg = rj.get("message", "")
                if "transcode" in msg.lower() or "not finished" in msg.lower():
                    print(f"[web-poster] transcode em andamento...")
                    continue  # retry
                if "media_needs_reupload" in msg:
                    return {"success": False, "media_id": None, "error": "Instagram pediu reupload (thumbnail inválido)"}
                # Classifica erros de auth/rate-limit
                return _classify_error(rj, r.status_code)
            elif r.status_code == 202:
                # 202 = accepted but not done yet (transcode)
                print(f"[web-poster] transcode em andamento (202)...")
                continue
            else:
                return _classify_error(None, r.status_code, r.text[:300])

        # Loop esgotado sem sucesso
        if rj is not None:
            msg = rj.get("message", "")
            if msg == "media_needs_reupload":
                return {"success": False, "media_id": None, "error": "Instagram pediu reupload (thumbnail inválido)"}
            return {"success": False, "media_id": None, "error": f"Resposta inesperada: {json.dumps(rj)[:300]}"}

        return {"success": False, "media_id": None, "error": "Transcode não completou após 6 tentativas"}

    except Exception as e:
        return {"success": False, "media_id": None, "error": str(e)}


def web_post_story_video(session_data: dict, video_path: str, caption: str = "",
                         link_url: str = None, link_text: str = None) -> dict:
    """
    Posta Story (video) via Web API usando cookies do Chrome.

    Returns:
        dict com {success, media_id, error}
    """
    video = Path(video_path)
    if not video.exists():
        return {"success": False, "media_id": None, "error": f"Arquivo não encontrado: {video_path}"}

    try:
        s = _build_session(session_data)
        upload_id = str(int(time.time() * 1000))

        # 1. Upload video (marcado como story — sem isso, configure_to_story rejeita)
        print(f"[web-poster] uploading story video ({video.stat().st_size // 1024} KB)...")
        v_result = _upload_video(s, video, upload_id, for_story=True)
        if v_result["status_code"] != 200:
            return {"success": False, "media_id": None, "error": f"Upload video falhou: {v_result}"}

        # 2. Thumbnail (não precisa de for_story — é só cover image)
        thumb = _generate_thumbnail(video)
        if thumb:
            _upload_thumbnail(s, thumb, upload_id)

        # 3. Configure as Story
        print(f"[web-poster] publishing story...")
        configure_data = {
            "source_type": "library",
            "upload_id": upload_id,
            "caption": caption or "",
            "configure_mode": "1",
            "client_shared_at": str(int(time.time())),
            "audience": "default",
        }

        # Link sticker (se fornecido)
        # Web API usa tap_models (não story_links que é do mobile API).
        # story_links é silenciosamente ignorado em sessões web/cookies.
        if link_url:
            configure_data["story_sticker_ids"] = "link_sticker_default"
            configure_data["tap_models"] = json.dumps([{
                "x": 0.4976,
                "y": 0.8,
                "z": 0,
                "width": 0.5,
                "height": 0.0856,
                "rotation": 0.0,
                "type": "story_link",
                "link_type": "web_link",
                "url": link_url,
                "display_text": link_text or "Clique aqui",
                "custom_cta": link_text or "Clique aqui",
            }])
            # Mantém story_links como fallback (algumas sessões hybrid aceitam)
            configure_data["story_links"] = json.dumps([{
                "webUri": link_url,
                "linkTitle": link_text or "Clique aqui",
                "linkType": 1,
                "x": 0.4976, "y": 0.8, "z": 0,
                "width": 0.5, "height": 0.0856,
                "rotation": 0.0,
            }])
            print(f"[web-poster] link sticker: {link_url} [{link_text}]")

        # Retry pra transcode
        rj = None
        for attempt in range(6):
            if attempt > 0:
                wait = 5 + attempt * 5
                print(f"[web-poster] aguardando transcode story ({wait}s)...")
                time.sleep(wait)

            r = s.post(
                "https://www.instagram.com/api/v1/media/configure_to_story/",
                data=configure_data,
                timeout=60,
            )

            if r.status_code == 202:
                print(f"[web-poster] transcode story em andamento (202)...")
                continue

            if r.status_code == 200:
                try:
                    rj = r.json()
                except Exception:
                    rj = {}
                msg = rj.get("message", "")
                if "transcode" in msg.lower() or "not finished" in msg.lower():
                    print(f"[web-poster] transcode story em andamento...")
                    continue
            break

        if r.status_code != 200:
            return _classify_error(None, r.status_code, r.text[:300])

        if rj is None:
            try:
                rj = r.json()
            except Exception:
                rj = {}
        media = rj.get("media")
        if media:
            pk = media.get("pk") or media.get("id")
            print(f"[web-poster] STORY POSTADO! pk={pk}")
            return {"success": True, "media_id": str(pk), "error": None}

        return _classify_error(rj, r.status_code)

    except Exception as e:
        return {"success": False, "media_id": None, "error": str(e)}


def web_post_story_photo(session_data: dict, photo_path: str, caption: str = "",
                         link_url: str = None, link_text: str = None) -> dict:
    """
    Posta Story (foto) via Web API usando cookies do Chrome.

    Returns:
        dict com {success, media_id, error}
    """
    photo = Path(photo_path)
    if not photo.exists():
        return {"success": False, "media_id": None, "error": f"Arquivo não encontrado: {photo_path}"}

    try:
        s = _build_session(session_data)
        upload_id = str(int(time.time() * 1000))

        # 1. Upload foto (marcado como story — sem isso, configure_to_story rejeita)
        print(f"[web-poster] uploading story photo ({photo.stat().st_size // 1024} KB)...")
        p_result = _upload_photo(s, photo, upload_id, for_story=True)
        if p_result["status_code"] != 200:
            return {"success": False, "media_id": None, "error": f"Upload foto falhou: {p_result}"}

        # 2. Configure as Story
        print(f"[web-poster] publishing photo story...")
        configure_data = {
            "source_type": "library",
            "upload_id": upload_id,
            "caption": caption or "",
            "configure_mode": "1",
            "client_shared_at": str(int(time.time())),
            "audience": "default",
        }

        if link_url:
            configure_data["story_sticker_ids"] = "link_sticker_default"
            configure_data["tap_models"] = json.dumps([{
                "x": 0.4976,
                "y": 0.8,
                "z": 0,
                "width": 0.5,
                "height": 0.0856,
                "rotation": 0.0,
                "type": "story_link",
                "link_type": "web_link",
                "url": link_url,
                "display_text": link_text or "Clique aqui",
                "custom_cta": link_text or "Clique aqui",
            }])
            configure_data["story_links"] = json.dumps([{
                "webUri": link_url,
                "linkTitle": link_text or "Clique aqui",
                "linkType": 1,
                "x": 0.4976, "y": 0.8, "z": 0,
                "width": 0.5, "height": 0.0856,
                "rotation": 0.0,
            }])
            print(f"[web-poster] link sticker: {link_url} [{link_text}]")

        r = s.post(
            "https://www.instagram.com/api/v1/media/configure_to_story/",
            data=configure_data,
            timeout=60,
        )

        if r.status_code != 200:
            return _classify_error(None, r.status_code, r.text[:300])

        try:
            rj = r.json()
        except Exception:
            rj = {}
        media = rj.get("media")
        if media:
            pk = media.get("pk") or media.get("id")
            print(f"[web-poster] STORY PHOTO POSTADO! pk={pk}")
            return {"success": True, "media_id": str(pk), "error": None}

        return _classify_error(rj, r.status_code)

    except Exception as e:
        return {"success": False, "media_id": None, "error": str(e)}
