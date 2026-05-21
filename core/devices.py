"""
Device fingerprint random por conta.

Cada @conta recebe um modelo Android diferente (deterministico baseado em
hash do username — mesmo username = sempre mesmo device, sem precisar
persistir nada).

Resultado: Instagram vê 300 "celulares" diferentes (Xiaomi, Samsung, Moto,
Realme, etc) em vez de 300 sessões do mesmo Python rodando no mesmo PC.

Reduz cluster detection significativamente.

Os devices abaixo são MODELOS REAIS populares no Brasil em 2024-2025,
com specs corretos (manufacturer, model code, device codename, CPU).
"""
from __future__ import annotations

import hashlib
import random
from typing import Dict, Optional


# Pool de devices Android REAIS (Brasil 2024-2025)
# Cada entry tem os campos que o instagrapi precisa pra set_device()
DEVICES = [
    # ===== Xiaomi (mais comum no Brasil) =====
    {
        "manufacturer": "Xiaomi",
        "model": "Redmi Note 11",
        "device": "spes",
        "cpu": "qcom",
        "android_version": 33,
        "android_release": "13",
        "dpi": "440dpi",
        "resolution": "1080x2400",
    },
    {
        "manufacturer": "Xiaomi",
        "model": "Redmi Note 12",
        "device": "topaz",
        "cpu": "qcom",
        "android_version": 33,
        "android_release": "13",
        "dpi": "395dpi",
        "resolution": "1080x2400",
    },
    {
        "manufacturer": "Xiaomi",
        "model": "Redmi Note 12 Pro",
        "device": "ruby",
        "cpu": "mt6855",
        "android_version": 34,
        "android_release": "14",
        "dpi": "395dpi",
        "resolution": "1080x2400",
    },
    {
        "manufacturer": "Xiaomi",
        "model": "POCO X5 Pro",
        "device": "redwood",
        "cpu": "qcom",
        "android_version": 33,
        "android_release": "13",
        "dpi": "395dpi",
        "resolution": "1080x2400",
    },
    {
        "manufacturer": "Xiaomi",
        "model": "Redmi 12C",
        "device": "earth",
        "cpu": "mt6769z",
        "android_version": 32,
        "android_release": "12",
        "dpi": "270dpi",
        "resolution": "720x1600",
    },
    # ===== Samsung =====
    {
        "manufacturer": "samsung",
        "model": "SM-A536E",
        "device": "a53x",
        "cpu": "s5e8825",
        "android_version": 33,
        "android_release": "13",
        "dpi": "450dpi",
        "resolution": "1080x2400",
    },
    {
        "manufacturer": "samsung",
        "model": "SM-A546E",
        "device": "a54x",
        "cpu": "s5e8835",
        "android_version": 34,
        "android_release": "14",
        "dpi": "450dpi",
        "resolution": "1080x2340",
    },
    {
        "manufacturer": "samsung",
        "model": "SM-A047M",
        "device": "a04",
        "cpu": "mt6765",
        "android_version": 32,
        "android_release": "12",
        "dpi": "280dpi",
        "resolution": "720x1600",
    },
    {
        "manufacturer": "samsung",
        "model": "SM-G991B",
        "device": "o1s",
        "cpu": "exynos2100",
        "android_version": 33,
        "android_release": "13",
        "dpi": "450dpi",
        "resolution": "1080x2400",
    },
    {
        "manufacturer": "samsung",
        "model": "SM-A136M",
        "device": "a13x",
        "cpu": "s5e3830",
        "android_version": 33,
        "android_release": "13",
        "dpi": "420dpi",
        "resolution": "1080x2408",
    },
    # ===== Motorola =====
    {
        "manufacturer": "motorola",
        "model": "moto g(60)s",
        "device": "rhode",
        "cpu": "mt6781",
        "android_version": 31,
        "android_release": "12",
        "dpi": "395dpi",
        "resolution": "1080x2460",
    },
    {
        "manufacturer": "motorola",
        "model": "moto g52",
        "device": "rhode",
        "cpu": "qcom",
        "android_version": 33,
        "android_release": "13",
        "dpi": "395dpi",
        "resolution": "1080x2400",
    },
    {
        "manufacturer": "motorola",
        "model": "moto edge 30",
        "device": "dubai",
        "cpu": "qcom",
        "android_version": 33,
        "android_release": "13",
        "dpi": "395dpi",
        "resolution": "1080x2400",
    },
    # ===== Realme =====
    {
        "manufacturer": "Realme",
        "model": "RMX3501",
        "device": "RE54AAL1",
        "cpu": "mt6877v",
        "android_version": 32,
        "android_release": "12",
        "dpi": "395dpi",
        "resolution": "1080x2400",
    },
    {
        "manufacturer": "Realme",
        "model": "RMX3771",
        "device": "RE5C77L1",
        "cpu": "mt6877v",
        "android_version": 33,
        "android_release": "13",
        "dpi": "395dpi",
        "resolution": "1080x2400",
    },
]

# Versões recentes do app Instagram (Android). Atualizar conforme novas saem.
APP_VERSIONS = [
    "320.0.0.42.101",
    "325.0.0.36.101",
    "330.0.0.40.93",
    "335.0.0.40.93",
    "340.0.0.40.93",
]


def _seed_from_username(username: str) -> int:
    """Hash determinístico do username pra seed (32-bit)."""
    h = hashlib.sha256(username.lower().encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def device_for_account(username: str) -> Dict:
    """Retorna device fingerprint determinístico pra essa conta.

    Mesmo username → sempre o mesmo device. Não precisa persistir.
    Inclui device_id, phone_id, uuid próprios (também determinísticos).
    """
    if not username:
        return DEVICES[0]

    seed = _seed_from_username(username)
    rng = random.Random(seed)

    device = rng.choice(DEVICES)
    app_version = rng.choice(APP_VERSIONS)

    # IDs únicos determinísticos por username
    # Gera UUIDs estilo instagrapi (16 hex chars com hifens)
    def _det_uuid():
        s = rng.getrandbits(128).to_bytes(16, "big").hex()
        return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"

    return {
        **device,
        "app_version": app_version,
        "version_code": f"{rng.randint(300000000, 360000000)}",
        # device_id: começa com "android-" + 16 hex
        "device_id": "android-" + format(rng.getrandbits(64), "016x"),
        "phone_id": _det_uuid(),
        "uuid": _det_uuid(),
        "advertising_id": _det_uuid(),
        # User agent montado dinâmicamente baseado no device
        # Pattern: Instagram <app_ver> Android (<sdk>/<release>; <dpi>; <resolution>; <manufacturer>; <model>; <device>; <cpu>; <locale>; <version_code>)
        "user_agent": (
            f"Instagram {app_version} Android ({device['android_version']}/{device['android_release']}; "
            f"{device['dpi']}; {device['resolution']}; "
            f"{device['manufacturer']}; {device['model']}; {device['device']}; {device['cpu']}; "
            f"pt_BR; {rng.randint(300000000, 360000000)})"
        ),
        "locale": "pt_BR",
        "timezone": "America/Sao_Paulo",
    }


def apply_device_to_client(cl, username: str) -> Optional[Dict]:
    """Aplica device fingerprint ao Client do instagrapi ANTES do login.

    Returns o device dict aplicado pra logging/debug.
    """
    try:
        device = device_for_account(username)
        # set_device aceita um dict com chaves esperadas pelo instagrapi
        cl.set_device({
            "app_version": device["app_version"],
            "android_version": device["android_version"],
            "android_release": device["android_release"],
            "dpi": device["dpi"],
            "resolution": device["resolution"],
            "manufacturer": device["manufacturer"],
            "device": device["device"],
            "model": device["model"],
            "cpu": device["cpu"],
            "version_code": device["version_code"],
        })
        cl.set_user_agent(device["user_agent"])
        cl.set_locale(device["locale"])
        cl.set_timezone_offset(-3 * 60 * 60)  # BRT (UTC-3) em segundos
        # IDs únicos por conta (não compartilhados entre Clients)
        cl.set_device_id(device["device_id"], device["uuid"], device["phone_id"])
        return device
    except Exception as e:
        print(f"[device] erro aplicando device pra @{username}: {e}")
        return None
