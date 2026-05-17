"""
Helpers de mídia: gera thumbnail de vídeo, valida arquivos.
Usa moviepy 2.x + Pillow.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


THUMB_SIZE = (640, 360)  # 16:9, leve pra grid


def generate_thumbnail(video_path: Path, thumb_path: Optional[Path] = None) -> Path:
    """Gera um JPG do primeiro frame relevante do vídeo.
    Salva ao lado do vídeo com mesmo nome + .jpg, ou no path passado.
    Retorna o path do thumb gerado.

    Falha silenciosa: se moviepy não conseguir ler o vídeo, cria placeholder.
    """
    thumb_path = thumb_path or video_path.with_suffix(".jpg")

    try:
        # Lazy import — moviepy pesa ~200ms
        from moviepy import VideoFileClip
        from PIL import Image

        with VideoFileClip(str(video_path)) as clip:
            # Pega frame em 1s (ou no meio se o vídeo for < 2s)
            t = min(1.0, max(0.0, clip.duration / 2)) if clip.duration else 0.0
            clip.save_frame(str(thumb_path), t=t)

        # Resize pra padronizar (Reels original geralmente é 1080x1920 → fica pesado)
        img = Image.open(thumb_path)
        # Mantém proporção, encaixa em THUMB_SIZE
        img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
        img.convert("RGB").save(thumb_path, "JPEG", quality=82, optimize=True)
        return thumb_path

    except Exception as e:
        # Cria placeholder cinza com texto pra UI não quebrar
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGB", THUMB_SIZE, color=(30, 32, 38))
            d = ImageDraw.Draw(img)
            d.text((20, 20), f"Sem prévia\n{video_path.name}", fill=(150, 150, 160))
            img.save(thumb_path, "JPEG", quality=70)
        except Exception:
            pass
        # Log mas não levanta — upload deve continuar
        print(f"[thumb] falhou em {video_path.name}: {e}")
        return thumb_path
