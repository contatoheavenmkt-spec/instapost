"""
Anti-cluster: gera uma variante única de cada mídia por conta antes de postar.

Por que: postar o mesmo .mp4 (mesmo hash, mesmo audio fingerprint, mesmos
metadados) em N contas é flag forte. Instagram cruza fingerprint visual+audio
e marca como spam coordenado.

Solução: cada conta recebe um arquivo levemente diferente (visualmente igual ao
olho humano, mas com hash, fingerprint e metadados todos diferentes).

Estratégia (determinística por conta):
  - Seed = hash(username + filename) — mesma conta+mídia = mesma variante sempre
  - Vídeo (.mp4):
      * Remove TODOS os metadados (-map_metadata -1)
      * Crop 1-3px aleatório em uma borda
      * CRF variando (23-25) → bitrate diferente
      * Filtro eq() pra saturação +/- 2%
      * Audio gain de -1 a +1 dB
  - Foto (.jpg/.png/.webp):
      * Já há normalização básica em core/media.normalize_image_for_story
      * Aqui aplica crop 1-2px + saturação +/-2% por conta

Cache: variantes ficam em data/content/variants/{stem}/{username}.{ext}
       Geradas on-demand quando o worker pede. Cache permanente (regenera só
       se o arquivo original mudar de mtime).
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from core.paths import VARIANTS_DIR

# Liga/desliga global (env var ANTICLUSTER=0 desliga e devolve original)
import os
ENABLED = os.environ.get("ANTICLUSTER", "1") not in ("0", "false", "no")

VIDEO_EXTS = {".mp4"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# Cache em RAM pra evitar stat() em todo request
_cache: dict[str, Path] = {}


def _seed(username: str, filename: str) -> int:
    """Seed determinístico (32-bit) a partir do par (conta, arquivo)."""
    h = hashlib.sha256(f"{username.lower()}::{filename}".encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def _params_for(username: str, filename: str) -> dict:
    """Calcula params imperceptíveis mas únicos por conta."""
    seed = _seed(username, filename)
    # Distribui escolhas usando bits diferentes do seed
    crop_px = 1 + (seed % 3)                          # 1, 2 ou 3 px
    crop_edge = (seed >> 2) % 4                       # 0=top, 1=right, 2=bottom, 3=left
    crf = 23 + ((seed >> 4) % 3)                      # 23, 24 ou 25
    sat = 0.98 + (((seed >> 6) % 5) * 0.01)           # 0.98 - 1.02
    bright = -0.01 + (((seed >> 8) % 3) * 0.01)       # -0.01, 0.0, +0.01
    audio_gain = -1.0 + (((seed >> 10) % 5) * 0.5)    # -1.0 a +1.0 dB
    return {
        "crop_px": crop_px,
        "crop_edge": crop_edge,
        "crf": crf,
        "sat": round(sat, 3),
        "bright": round(bright, 3),
        "audio_gain": round(audio_gain, 2),
    }


def _ffmpeg_vf_for(p: dict) -> str:
    """Constrói o filtro -vf do ffmpeg pro vídeo."""
    # Crop: tira 'crop_px' de uma borda específica.
    # ffmpeg crop=W:H:X:Y onde W,H são tamanhos finais e X,Y o offset.
    # Pra cortar 'n' pixels da borda 'edge', usamos in_w / in_h:
    edge = p["crop_edge"]
    px = p["crop_px"]
    if edge == 0:    # top
        crop = f"crop=in_w:in_h-{px}:0:{px}"
    elif edge == 1:  # right
        crop = f"crop=in_w-{px}:in_h:0:0"
    elif edge == 2:  # bottom
        crop = f"crop=in_w:in_h-{px}:0:0"
    else:            # left
        crop = f"crop=in_w-{px}:in_h:{px}:0"
    eq = f"eq=saturation={p['sat']}:brightness={p['bright']}"
    return f"{crop},{eq}"


def _variant_path(original: Path, username: str) -> Path:
    """Onde a variante daquela conta vai ser guardada."""
    stem = original.stem
    return VARIANTS_DIR / stem / f"{username}{original.suffix.lower()}"


def variant_for_account(original: Path, username: str, timeout: int = 120) -> Path:
    """Retorna o path da variante única pra essa conta. Gera se não existir.

    Se ANTICLUSTER desligado OU se ffmpeg falhar → retorna original.
    Cache permanente: regenera só se mtime do original mudou.
    """
    if not ENABLED or not original.exists():
        return original
    ext = original.suffix.lower()
    if ext not in VIDEO_EXTS and ext not in PHOTO_EXTS:
        return original

    cache_key = f"{username}::{original.name}"
    if cache_key in _cache:
        cached = _cache[cache_key]
        if cached.exists():
            # Confere se original não mudou
            if cached.stat().st_mtime >= original.stat().st_mtime:
                return cached

    target = _variant_path(original, username)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Já existe e está fresco?
    if target.exists() and target.stat().st_mtime >= original.stat().st_mtime:
        _cache[cache_key] = target
        return target

    p = _params_for(username, original.name)
    tmp = target.with_suffix(target.suffix + ".tmp")

    # Mapeia extensão -> formato ffmpeg explícito.
    # CRÍTICO: o tmp termina em ".mp4.tmp" / ".jpg.tmp" etc — ffmpeg infere o
    # muxer pela última extensão, vê ".tmp", não conhece, e falha com
    # "Error initializing the muxer ... Invalid argument". Sempre forçamos -f.
    fmt_map = {
        ".mp4": "mp4",
        ".jpg": "image2", ".jpeg": "image2",
        ".png": "image2", ".webp": "image2",
    }
    output_format = fmt_map.get(ext, "mp4" if ext in VIDEO_EXTS else "image2")

    try:
        if ext in VIDEO_EXTS:
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(original),
                "-map_metadata", "-1",
                "-vf", _ffmpeg_vf_for(p),
                "-c:v", "libx264", "-crf", str(p["crf"]), "-preset", "veryfast",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-af", f"volume={p['audio_gain']}dB",
                "-movflags", "+faststart",
                "-f", output_format,
                str(tmp),
            ]
        else:  # foto
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(original),
                "-map_metadata", "-1",
                "-vf", _ffmpeg_vf_for(p),
                "-q:v", "2",
                "-f", output_format,
                str(tmp),
            ]
        subprocess.run(cmd, timeout=timeout, check=True)
        # Move atomicamente
        shutil.move(str(tmp), str(target))
        _cache[cache_key] = target
        print(f"[anticluster] gerou variante {target.name} para @{username} "
              f"(crf={p.get('crf')}, sat={p['sat']}, edge={p['crop_edge']}, audio={p['audio_gain']}dB)")
        return target
    except subprocess.TimeoutExpired:
        print(f"[anticluster] timeout gerando variante de {original.name} pra @{username} — devolvendo original")
    except subprocess.CalledProcessError as e:
        print(f"[anticluster] ffmpeg falhou ({e.returncode}) pra @{username}/{original.name} — devolvendo original")
    except Exception as e:
        print(f"[anticluster] erro gerando variante: {e} — devolvendo original")
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass

    return original


def cleanup_variants_for(filename: str):
    """Remove a pasta de variantes de uma mídia (chame ao deletar o original)."""
    stem = Path(filename).stem
    folder = VARIANTS_DIR / stem
    if folder.exists():
        try:
            shutil.rmtree(folder)
            # Limpa cache em RAM
            for k in list(_cache.keys()):
                if k.endswith(f"::{filename}"):
                    del _cache[k]
        except Exception as e:
            print(f"[anticluster] erro limpando variantes de {filename}: {e}")
