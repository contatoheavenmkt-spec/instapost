"""
Wrappers de postagem via instagrapi.

Suporta Reels (vídeo permanente no perfil) e Stories (foto/vídeo que dura 24h,
opcionalmente com link sticker).
"""
import json
from pathlib import Path
from typing import Optional

from instagrapi import Client


# ----------- REELS -----------

def post_reel(cl: Client, video_path: str, caption: str) -> dict:
    """
    Posta um Reel.
    Retorna dict com {success: bool, media_id: str|None, error: str|None}
    """
    try:
        video = Path(video_path)
        if not video.exists():
            return {"success": False, "media_id": None, "error": f"Arquivo não encontrado: {video_path}"}

        media = cl.clip_upload(
            path=video,
            caption=caption,
        )
        return {"success": True, "media_id": str(media.pk), "error": None}
    except Exception as e:
        return {"success": False, "media_id": None, "error": str(e)}


# ----------- STORIES -----------

def _build_story_links(link_url: Optional[str], link_text: Optional[str] = None) -> list:
    """Cria o sticker de link pro story se URL foi passada.
    link_text é o texto que aparece no sticker (ex: 'Clique aqui').
    Default do Instagram quando link_text vazio é mostrar a URL."""
    if not link_url:
        return []
    try:
        from instagrapi.types import StoryLink
        text = (link_text or "Clique aqui").strip()
        return [StoryLink(webUri=link_url, linkText=text)]
    except Exception:
        # Fallback: tenta sem linkText caso versão do instagrapi não suporte
        try:
            from instagrapi.types import StoryLink
            return [StoryLink(webUri=link_url)]
        except Exception:
            return []


def post_story_video(cl: Client, video_path: str, caption: str = "",
                     link_url: Optional[str] = None, link_text: Optional[str] = None) -> dict:
    """Posta vídeo no Story (até 60s, vertical 9:16). Se link_url for passada,
    adiciona como link sticker com texto custom (default: 'Clique aqui')."""
    try:
        video = Path(video_path)
        if not video.exists():
            return {"success": False, "media_id": None, "error": f"Arquivo não encontrado: {video_path}"}

        links = _build_story_links(link_url, link_text)
        kwargs = {"path": video, "caption": caption or ""}
        if links:
            kwargs["links"] = links

        media = cl.video_upload_to_story(**kwargs)
        return {"success": True, "media_id": str(media.pk), "error": None}
    except Exception as e:
        return {"success": False, "media_id": None, "error": str(e)}


def post_story_photo(cl: Client, photo_path: str, caption: str = "",
                     link_url: Optional[str] = None, link_text: Optional[str] = None) -> dict:
    """Posta foto no Story com link sticker opcional (texto custom)."""
    try:
        photo = Path(photo_path)
        if not photo.exists():
            return {"success": False, "media_id": None, "error": f"Arquivo não encontrado: {photo_path}"}

        links = _build_story_links(link_url, link_text)
        kwargs = {"path": photo, "caption": caption or ""}
        if links:
            kwargs["links"] = links

        media = cl.photo_upload_to_story(**kwargs)
        return {"success": True, "media_id": str(media.pk), "error": None}
    except Exception as e:
        return {"success": False, "media_id": None, "error": str(e)}


# ----------- HELPERS -----------

def detect_media_kind(path: str) -> str:
    """Retorna 'photo' ou 'video' baseado na extensão."""
    ext = Path(path).suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return "photo"
    return "video"


def load_caption(media_path: str) -> str:
    """
    Carrega legenda do .txt com mesmo nome do arquivo.
    Ex: video1.mp4 → video1.txt
    """
    txt_path = Path(media_path).with_suffix(".txt")
    if txt_path.exists():
        return txt_path.read_text(encoding="utf-8").strip()
    return ""


def load_meta(media_path: str) -> dict:
    """
    Carrega metadata do arquivo (.meta.json).
    Esperado: {"kind": "reel"|"story", "link_url": str|None, "link_text": str|None}
    Default: kind=reel pra .mp4, story pra .jpg/.png. link_text default "Clique aqui".
    """
    p = Path(media_path)
    # .meta.json é "video.mp4.meta.json" (sem trocar suffix, pra coexistir com .txt e .jpg)
    meta_path = p.with_name(p.name + ".meta.json")
    default_kind = "story" if detect_media_kind(media_path) == "photo" else "reel"
    default = {"kind": default_kind, "link_url": None, "link_text": "Clique aqui"}
    if not meta_path.exists():
        return default
    try:
        loaded = json.loads(meta_path.read_text(encoding="utf-8"))
        merged = {**default, **loaded}
        # Garante que link_text sempre tem valor (sticker fica feio sem texto)
        if not (merged.get("link_text") or "").strip():
            merged["link_text"] = "Clique aqui"
        return merged
    except Exception:
        return default


def save_meta(media_path: str, meta: dict) -> None:
    p = Path(media_path)
    meta_path = p.with_name(p.name + ".meta.json")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
