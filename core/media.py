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


# ----- Normalização de imagem pra Story do Instagram -----

STORY_SIZE = (1080, 1920)  # 9:16 padrão do Instagram Story


def normalize_image_for_story(input_path: Path, output_path: Optional[Path] = None) -> Path:
    """Prepara uma imagem pra ser postada como Story do Instagram.

    Resolve o erro 'Photo story upload configure succeeded without media payload'
    que acontece com:
      - Fotos do WhatsApp (metadados JFIF estranhos)
      - Proporções diferentes de 9:16
      - JPEGs mal-formados ou com EXIF corrompido

    Faz:
      1. Aplica rotação EXIF (se a foto era do celular)
      2. Remove TODOS os metadados
      3. Converte pra RGB (sRGB)
      4. Resize/padding pra 1080x1920 mantendo o aspecto original (fundo preto)
      5. Salva como JPEG limpo, qualidade 92

    Retorna o path do output. Se output_path não for passado, sobrescreve o input.
    """
    output_path = output_path or input_path
    try:
        from PIL import Image, ImageOps
    except Exception as e:
        print(f"[normalize_story] PIL faltando: {e}")
        return input_path

    try:
        with Image.open(input_path) as img:
            # 1. Rotação EXIF (foto do celular vem rotacionada)
            img = ImageOps.exif_transpose(img)
            # 2. Converte pra RGB
            if img.mode != "RGB":
                img = img.convert("RGB")

            # 3. Resize/padding pra 1080x1920
            img_ratio = img.width / img.height
            target_ratio = STORY_SIZE[0] / STORY_SIZE[1]

            if abs(img_ratio - target_ratio) < 0.01:
                # Já está em 9:16 (ou muito próximo) — só redimensiona
                final = img.resize(STORY_SIZE, Image.LANCZOS)
            else:
                # Padding com fundo preto pra manter aspecto
                canvas = Image.new("RGB", STORY_SIZE, (0, 0, 0))
                if img_ratio > target_ratio:
                    # Imagem é mais larga (paisagem ou quadrada) — caber pela largura
                    new_w = STORY_SIZE[0]
                    new_h = int(new_w / img_ratio)
                    resized = img.resize((new_w, new_h), Image.LANCZOS)
                    canvas.paste(resized, (0, (STORY_SIZE[1] - new_h) // 2))
                else:
                    # Imagem é mais alta — caber pela altura
                    new_h = STORY_SIZE[1]
                    new_w = int(new_h * img_ratio)
                    resized = img.resize((new_w, new_h), Image.LANCZOS)
                    canvas.paste(resized, ((STORY_SIZE[0] - new_w) // 2, 0))
                final = canvas

            # 4. Salva JPEG limpo (sem metadados)
            final.save(output_path, "JPEG", quality=92, optimize=False)
            return output_path
    except Exception as e:
        print(f"[normalize_story] falhou em {input_path}: {e} — usando original")
        return input_path
