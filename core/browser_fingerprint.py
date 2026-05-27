"""
Gera fingerprint de browser (UA + screen + lang + timezone + etc) por conta.

Por que: cada conta precisa parecer um device diferente quando o usuário abre
Instagram via extensão Chrome pra Save Sessão. Sem isso, IG vê o mesmo fingerprint
(mesmo Chrome) em todas as contas — sinal de "farm".

LIMITAÇÃO: extensão Chrome NÃO consegue spoofar Canvas/WebGL/AudioContext (precisa
patched Chromium tipo Dolphin Anty). Mas spoofar UA + screen + lang + timezone já
quebra a maioria dos clustering por device fingerprint.

Fingerprint é determinístico por (username, workspace) — mesma conta gera mesmo
fingerprint sempre (continuidade pro IG, igual `core/devices.py` faz pro instagrapi).
"""
from __future__ import annotations

import hashlib
import random
from typing import Optional


# Pool de User-Agents desktop reais (Chrome 131-133 em diferentes OS)
DESKTOP_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
]

# Pool de resoluções de tela comuns (com aspect ratio realista)
SCREEN_RESOLUTIONS = [
    (1920, 1080), (1366, 768), (1536, 864), (1440, 900),
    (1600, 900), (1280, 720), (2560, 1440), (1920, 1200),
    (1680, 1050), (1280, 800), (3840, 2160), (2880, 1800),
]

# Timezones do Brasil (caso seja conta BR) + alguns globais
TIMEZONES_BR = [
    {"name": "America/Sao_Paulo", "offset": -180},
    {"name": "America/Bahia", "offset": -180},
    {"name": "America/Fortaleza", "offset": -180},
    {"name": "America/Recife", "offset": -180},
    {"name": "America/Maceio", "offset": -180},
    {"name": "America/Manaus", "offset": -240},
    {"name": "America/Belem", "offset": -180},
    {"name": "America/Cuiaba", "offset": -240},
]

# Línguas com preferência BR
LANGUAGE_POOLS = [
    {"primary": "pt-BR", "list": ["pt-BR", "pt", "en-US", "en"]},
    {"primary": "pt-BR", "list": ["pt-BR", "pt"]},
    {"primary": "pt", "list": ["pt", "pt-BR", "en"]},
]

HARDWARE_CONCURRENCY_POOL = [4, 6, 8, 8, 8, 12, 16]  # 8 mais comum
DEVICE_MEMORY_POOL = [4, 8, 8, 8, 16]  # 8GB mais comum


def _platform_from_ua(ua: str) -> str:
    """Extrai platform string consistente com o UA."""
    if "Windows" in ua:
        return "Win32"
    if "Macintosh" in ua or "Mac OS X" in ua:
        return "MacIntel"
    if "Linux" in ua:
        return "Linux x86_64"
    return "Win32"


def _color_depth_from_random(rng: random.Random) -> int:
    # 24 mais comum em monitores modernos, 32 em alguns
    return rng.choice([24, 24, 24, 30, 32])


def _device_pixel_ratio(rng: random.Random) -> float:
    # 1.0 standard, 2.0 retina, 1.25/1.5 windows scaling
    return rng.choice([1.0, 1.0, 1.0, 1.25, 1.5, 2.0])


def generate_fingerprint(username: str, workspace_slug: str = "default") -> dict:
    """Gera fingerprint determinístico por (username, workspace_slug).

    Mesma conta = mesmo fingerprint sempre (continuidade). Conta diferente =
    fingerprint diferente.

    Returns dict pronto pra serializar em session.json.browser_fingerprint.
    """
    seed_str = f"{(workspace_slug or 'default').lower()}|{(username or '').lower()}"
    seed = int(hashlib.sha256(seed_str.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)

    ua = rng.choice(DESKTOP_UAS)
    platform = _platform_from_ua(ua)
    w, h = rng.choice(SCREEN_RESOLUTIONS)
    tz = rng.choice(TIMEZONES_BR)
    lang = rng.choice(LANGUAGE_POOLS)
    color_depth = _color_depth_from_random(rng)
    dpr = _device_pixel_ratio(rng)

    return {
        "user_agent": ua,
        "platform": platform,
        "language": lang["primary"],
        "languages": lang["list"],
        "screen": {
            "width": w,
            "height": h,
            "avail_width": w,
            "avail_height": h - rng.choice([40, 60, 80]),  # taskbar
            "color_depth": color_depth,
            "pixel_depth": color_depth,
        },
        "device_pixel_ratio": dpr,
        "timezone": tz["name"],
        "timezone_offset_minutes": tz["offset"],
        "hardware_concurrency": rng.choice(HARDWARE_CONCURRENCY_POOL),
        "device_memory_gb": rng.choice(DEVICE_MEMORY_POOL),
        "vendor": "Google Inc.",
        "version": "v1",  # schema version pra futuras migracoes
    }
