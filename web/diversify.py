"""
Auto-loop de disparo diversificado por workspace.

Settings persistidos em data/workspaces/<slug>/diversify_settings.json:
{
    "enabled": bool,
    "interval_hours": int (1-72),
    "max_per_account": int (1-20),
    "last_run_at": iso | null,
    "completed_at": iso | null,
}
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from typing import Optional

from core import paths


def _settings_file(slug: Optional[str] = None):
    return paths.workspace_root(slug) / "diversify_settings.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


_lock = threading.Lock()


DEFAULTS = {
    "enabled": False,
    "interval_hours": 1,                # ritmo veterana (entre rodadas)
    "max_per_account": 1,
    "last_run_at": None,
    "completed_at": None,
    "kind_filter": "all",               # "all" | "reel" | "story"
    "repetitions_per_video": 3,         # quantas vezes mesmo video por conta antes de avançar
    "new_account_threshold_hours": 24,  # quanto tempo após 1º post conta é "nova"
    "new_account_interval_hours": 6,    # ritmo de aquecimento da conta nova
}


def load(slug: Optional[str] = None) -> dict:
    """Carrega settings do workspace ativo (ou específico). Mescla com defaults."""
    p = _settings_file(slug)
    if not p.exists():
        return dict(DEFAULTS)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {**DEFAULTS, **raw}
    except Exception as e:
        print(f"[diversify] erro lendo settings: {e}")
        return dict(DEFAULTS)


def save(data: dict, slug: Optional[str] = None):
    """Salva settings (mescla com existente)."""
    p = _settings_file(slug)
    with _lock:
        current = load(slug)
        current.update(data)
        # Sanitiza
        current["interval_hours"] = max(1, min(72, int(current.get("interval_hours", 6))))
        current["max_per_account"] = max(1, min(20, int(current.get("max_per_account", 1))))
        current["enabled"] = bool(current.get("enabled"))
        kf = (current.get("kind_filter") or "all").lower()
        current["kind_filter"] = kf if kf in ("all", "reel", "story") else "all"
        current["repetitions_per_video"] = max(1, min(10, int(current.get("repetitions_per_video", 3))))
        current["new_account_threshold_hours"] = max(1, min(168, int(current.get("new_account_threshold_hours", 24))))
        current["new_account_interval_hours"] = max(1, min(72, int(current.get("new_account_interval_hours", 6))))
        try:
            p.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[diversify] erro salvando settings: {e}")
        return current


def mark_run(slug: Optional[str] = None):
    save({"last_run_at": now_iso()}, slug=slug)


def mark_completed(slug: Optional[str] = None):
    save({"enabled": False, "completed_at": now_iso()}, slug=slug)
