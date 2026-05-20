"""
Tracker de saúde por conta — detector passivo de shadow ban / queda anormal.

Estratégia:
- 1x/dia, worker pega métricas (views médios dos últimos 10 posts, follower count).
- Sistema salva snapshot em data/workspaces/<slug>/health/<username>.json
- Analyzer detecta queda > 70% comparando últimos 3 vs anteriores 7 (default).
- Se queda detectada → marca conta com shadowban_suspected=true (em accounts.json)
- Opcional follow-up: teste de hashtag pra confirmar (job hashtag_check).

NÃO promete certeza absoluta. Insta não tem API "está shadowbanned?". Só
detecta padrões estatísticos de queda.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Optional

from core import paths


# Threshold default: queda > 70% em 7 dias (3 posts recentes vs 7 anteriores).
DEFAULT_DROP_PCT = 0.70

# Quantos snapshots manter por conta (histórico)
MAX_HISTORY = 60  # ~2 meses

_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _health_dir(slug: Optional[str] = None):
    d = paths.workspace_root(slug) / "health"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _health_file(username: str, slug: Optional[str] = None):
    safe = "".join(c for c in username if c.isalnum() or c in "._-")
    return _health_dir(slug) / f"{safe}.json"


def load_history(username: str, slug: Optional[str] = None) -> list[dict]:
    p = _health_file(username, slug)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def record(username: str, snapshot: dict, slug: Optional[str] = None) -> dict:
    """Adiciona um snapshot ao histórico e retorna análise atualizada.

    snapshot esperado:
    {
        "collected_at": ISO,
        "follower_count": int,
        "media_count": int,
        "recent_posts": [
            {"pk": str, "views": int, "likes": int, "taken_at": ISO},
            ...
        ],
        "avg_views_last_3": float,
        "avg_views_baseline": float,  # média dos posts 4-10
    }
    """
    with _lock:
        history = load_history(username, slug)
        history.append(snapshot)
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        p = _health_file(username, slug)
        try:
            p.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[health] erro salvando {username}: {e}")
    return analyze(username, slug)


def analyze(username: str, slug: Optional[str] = None, drop_pct: float = DEFAULT_DROP_PCT) -> dict:
    """Olha o histórico e calcula score + se está suspeito.

    Retorna:
    {
        "username": str,
        "snapshots_count": int,
        "latest_avg_recent": float | None,
        "latest_avg_baseline": float | None,
        "drop_pct": float | None,  # % de queda recent vs baseline
        "suspected": bool,
        "reason": str | None,
        "health_score": int (0-100),
        "follower_delta_7d": int | None,
    }
    """
    history = load_history(username, slug)
    if not history:
        return {
            "username": username,
            "snapshots_count": 0,
            "latest_avg_recent": None,
            "latest_avg_baseline": None,
            "drop_pct": None,
            "suspected": False,
            "reason": None,
            "health_score": 50,
            "follower_delta_7d": None,
        }

    latest = history[-1]
    recent = float(latest.get("avg_views_last_3") or 0)
    baseline = float(latest.get("avg_views_baseline") or 0)

    drop = None
    suspected = False
    reason = None

    if baseline > 0 and recent < baseline:
        drop = (baseline - recent) / baseline
        if drop > drop_pct:
            suspected = True
            reason = f"queda {int(drop*100)}% em views recentes (média últimos 3: {int(recent)} vs baseline: {int(baseline)})"

    # Health score: 100 = saudável, 0 = ruim
    if baseline > 0:
        ratio = min(recent / baseline, 1.0) if baseline > 0 else 1.0
        score = int(ratio * 100)
    else:
        score = 50

    # Delta de seguidores nos últimos 7 dias (se houver dados)
    follower_delta = None
    if len(history) >= 7:
        old_followers = history[-7].get("follower_count")
        new_followers = latest.get("follower_count")
        if old_followers is not None and new_followers is not None:
            follower_delta = new_followers - old_followers

    return {
        "username": username,
        "snapshots_count": len(history),
        "latest_avg_recent": recent,
        "latest_avg_baseline": baseline,
        "drop_pct": drop,
        "suspected": suspected,
        "reason": reason,
        "health_score": max(0, min(100, score)),
        "follower_delta_7d": follower_delta,
    }
