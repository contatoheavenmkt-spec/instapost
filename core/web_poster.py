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

    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
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


def _upload_video(session: requests.Session, video_path: Path, upload_id: str) -> dict:
    """Upload video via rupload_igvideo."""
    video_size = video_path.stat().st_size
    entity = f"{upload_id}_0_{uuid.uuid4().hex}"

    with open(video_path, "rb") as f:
        r = session.post(
            f"https://www.instagram.com/rupload_igvideo/{entity}",
            data=f.read(),
            headers={
                "X-Entity-Name": entity,
                "X-Entity-Length": str(video_size),
                "X-Entity-Type": "video/mp4",
                "X-Instagram-Rupload-Params": json.dumps({
                    "media_type": 2,
                    "upload_id": upload_id,
                    "upload_media_height": 1920,
                    "upload_media_width": 1080,
                }),
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


def _upload_photo(session: requests.Session, photo_path: Path, upload_id: str) -> dict:
    """Upload foto via rupload_igphoto (pra story de foto)."""
    photo_size = photo_path.stat().st_size
    entity = f"{upload_id}_0_{uuid.uuid4().hex}"

    with open(photo_path, "rb") as f:
        r = session.post(
            f"https://www.instagram.com/rupload_igphoto/{entity}",
            data=f.read(),
            headers={
                "X-Entity-Name": entity,
                "X-Entity-Length": str(photo_size),
                "X-Entity-Type": "image/jpeg",
                "X-Instagram-Rupload-Params": json.dumps({
                    "upload_id": upload_id,
                    "media_type": 1,
                    "waterfall_id": str(uuid.uuid4()),
                    "image_compression": json.dumps({"lib_name": "moz", "lib_version": "3.1.m", "quality": "80"}),
                }),
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
            return {"success": False, "media_id": None, "error": f"Upload video falhou: {v_result}"}
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

        # 3. Publish as Reel
        print(f"[web-poster] publishing reel...")
        r = s.post(
            "https://www.instagram.com/api/v1/media/configure_to_clips/",
            data={
                "source_type": "library",
                "caption": caption or "",
                "upload_id": upload_id,
                "disable_comments": "0",
                "like_and_view_counts_disabled": "0",
                "igtv_share_preview_to_feed": "1",
                "is_unified_video": "1",
                "video_subtitles_enabled": "0",
            },
            timeout=60,
        )

        if r.status_code != 200:
            return {"success": False, "media_id": None, "error": f"Configure falhou: HTTP {r.status_code} - {r.text[:300]}"}

        rj = r.json()
        media = rj.get("media")
        if media:
            pk = media.get("pk") or media.get("id")
            print(f"[web-poster] REEL POSTADO! pk={pk}")
            return {"success": True, "media_id": str(pk), "error": None}

        msg = rj.get("message", "")
        if msg == "media_needs_reupload":
            return {"success": False, "media_id": None, "error": "Instagram pediu reupload (thumbnail inválido)"}

        return {"success": False, "media_id": None, "error": f"Resposta inesperada: {json.dumps(rj)[:300]}"}

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

        # 1. Upload video
        print(f"[web-poster] uploading story video ({video.stat().st_size // 1024} KB)...")
        v_result = _upload_video(s, video, upload_id)
        if v_result["status_code"] != 200:
            return {"success": False, "media_id": None, "error": f"Upload video falhou: {v_result}"}

        # 2. Thumbnail
        thumb = _generate_thumbnail(video)
        if thumb:
            _upload_thumbnail(s, thumb, upload_id)

        # 3. Configure as Story
        print(f"[web-poster] publishing story...")
        configure_data = {
            "source_type": "library",
            "upload_id": upload_id,
            "caption": caption or "",
        }

        # Link sticker (se fornecido)
        if link_url:
            configure_data["story_sticker_ids"] = "link_sticker_default"
            configure_data["story_links"] = json.dumps([{
                "webUri": link_url,
                "linkTitle": link_text or "Clique aqui",
                "x": 0.5, "y": 0.5, "z": 0,
                "width": 1.0, "height": 1.0,
                "rotation": 0.0,
            }])

        r = s.post(
            "https://www.instagram.com/api/v1/media/configure_to_story/",
            data=configure_data,
            timeout=60,
        )

        if r.status_code != 200:
            return {"success": False, "media_id": None, "error": f"Configure story falhou: HTTP {r.status_code} - {r.text[:300]}"}

        rj = r.json()
        media = rj.get("media")
        if media:
            pk = media.get("pk") or media.get("id")
            print(f"[web-poster] STORY POSTADO! pk={pk}")
            return {"success": True, "media_id": str(pk), "error": None}

        return {"success": False, "media_id": None, "error": f"Resposta: {json.dumps(rj)[:300]}"}

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

        # 1. Upload foto
        print(f"[web-poster] uploading story photo ({photo.stat().st_size // 1024} KB)...")
        p_result = _upload_photo(s, photo, upload_id)
        if p_result["status_code"] != 200:
            return {"success": False, "media_id": None, "error": f"Upload foto falhou: {p_result}"}

        # 2. Configure as Story
        print(f"[web-poster] publishing photo story...")
        configure_data = {
            "source_type": "library",
            "upload_id": upload_id,
            "caption": caption or "",
        }

        if link_url:
            configure_data["story_sticker_ids"] = "link_sticker_default"
            configure_data["story_links"] = json.dumps([{
                "webUri": link_url,
                "linkTitle": link_text or "Clique aqui",
                "x": 0.5, "y": 0.5, "z": 0,
                "width": 1.0, "height": 1.0,
                "rotation": 0.0,
            }])

        r = s.post(
            "https://www.instagram.com/api/v1/media/configure_to_story/",
            data=configure_data,
            timeout=60,
        )

        if r.status_code != 200:
            return {"success": False, "media_id": None, "error": f"Configure falhou: HTTP {r.status_code} - {r.text[:300]}"}

        rj = r.json()
        media = rj.get("media")
        if media:
            pk = media.get("pk") or media.get("id")
            print(f"[web-poster] STORY PHOTO POSTADO! pk={pk}")
            return {"success": True, "media_id": str(pk), "error": None}

        return {"success": False, "media_id": None, "error": f"Resposta: {json.dumps(rj)[:300]}"}

    except Exception as e:
        return {"success": False, "media_id": None, "error": str(e)}
