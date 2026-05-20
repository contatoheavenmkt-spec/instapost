"""
Encurtador de URLs próprio.

Storage em links.json. Slug aleatório alfanumérico. Cada slug pode ser usado
1 vez (link único) ou várias (link permanente). Tracking de cliques com
privacy: IP é hasheado, não armazenado em claro.

Quando integrado com Stories, o sistema gera 1 slug DIFERENTE por conta
apontando pro MESMO destino — anti-clusterização do Instagram.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import string
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.paths import LINKS_FILE

SLUG_ALPHABET = string.ascii_letters + string.digits  # 62 chars
SLUG_LEN = 7  # 62^7 = 3.5 trilhões — colisão é negligível
MAX_CLICKS_KEPT_PER_LINK = 1000  # aumentado pra permitir analytics por data


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def gen_slug(length: int = SLUG_LEN) -> str:
    return "".join(secrets.choice(SLUG_ALPHABET) for _ in range(length))


def hash_ip(ip: str) -> str:
    """Hash trunc do IP — distingue dispositivos sem armazenar IP real."""
    if not ip:
        return ""
    h = hashlib.sha256(ip.encode("utf-8")).hexdigest()
    return h[:12]


class Link:
    def __init__(self, data: dict):
        self.slug: str = data["slug"]
        self.target_url: str = data["target_url"]
        self.label: Optional[str] = data.get("label")
        self.account: Optional[str] = data.get("account")  # se foi gerado pra uma conta específica
        self.parent_slug: Optional[str] = data.get("parent_slug")  # grupo (multi-conta)
        self.created_at: str = data.get("created_at") or now_iso()
        self.created_by: Optional[str] = data.get("created_by")
        self.active: bool = data.get("active", True)
        self.click_count: int = data.get("click_count", 0)
        self.clicks: list = data.get("clicks", [])

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "target_url": self.target_url,
            "label": self.label,
            "account": self.account,
            "parent_slug": self.parent_slug,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "active": self.active,
            "click_count": self.click_count,
            "clicks": self.clicks[-MAX_CLICKS_KEPT_PER_LINK:],
        }


class LinkManager:
    def __init__(self):
        self._items: dict[str, Link] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if not LINKS_FILE.exists():
            return
        try:
            raw = json.loads(LINKS_FILE.read_text(encoding="utf-8"))
            self._items = {d["slug"]: Link(d) for d in raw}
        except Exception as e:
            print(f"[shortener] failed to load: {e}")

    def _save(self):
        try:
            data = [l.to_dict() for l in self._items.values()]
            LINKS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[shortener] failed to save: {e}")

    # ---- queries ----

    def list(self) -> list[dict]:
        with self._lock:
            items = sorted(self._items.values(), key=lambda l: l.created_at, reverse=True)
            return [l.to_dict() for l in items]

    def get(self, slug: str) -> Optional[Link]:
        return self._items.get(slug)

    # ---- mutations ----

    def create(self, target_url: str, label: Optional[str] = None,
               account: Optional[str] = None, parent_slug: Optional[str] = None,
               created_by: Optional[str] = None, slug: Optional[str] = None) -> Link:
        target_url = (target_url or "").strip()
        if not target_url:
            raise ValueError("target_url obrigatório")
        if not (target_url.startswith("http://") or target_url.startswith("https://")):
            target_url = "https://" + target_url

        with self._lock:
            # Slug customizado ou gera aleatório (com retry se colidir)
            if slug:
                slug = slug.strip()
                if slug in self._items:
                    raise ValueError(f"Slug '{slug}' já existe")
            else:
                for _ in range(8):
                    candidate = gen_slug()
                    if candidate not in self._items:
                        slug = candidate
                        break
                else:
                    slug = gen_slug(SLUG_LEN + 2)  # aumenta 2 chars se azar épico

            link = Link({
                "slug": slug,
                "target_url": target_url,
                "label": label,
                "account": account,
                "parent_slug": parent_slug,
                "created_at": now_iso(),
                "created_by": created_by,
                "active": True,
                "click_count": 0,
                "clicks": [],
            })
            self._items[slug] = link
            self._save()
            return link

    def create_batch_for_accounts(self, target_url: str, accounts: list[str],
                                  label: Optional[str] = None,
                                  created_by: Optional[str] = None) -> list[Link]:
        """Cria 1 link único por conta, apontando pro MESMO destino.
        Todos compartilham o mesmo parent_slug (gerado também como link aleatório
        usado apenas como identificador de grupo)."""
        parent = gen_slug(6)
        out = []
        for acc in accounts:
            link = self.create(
                target_url=target_url,
                label=f"{label or 'auto'} · @{acc}",
                account=acc,
                parent_slug=parent,
                created_by=created_by,
            )
            out.append(link)
        return out

    def delete(self, slug: str) -> bool:
        with self._lock:
            if slug not in self._items:
                return False
            del self._items[slug]
            self._save()
            return True

    def toggle_active(self, slug: str) -> bool:
        with self._lock:
            link = self._items.get(slug)
            if not link:
                return False
            link.active = not link.active
            self._save()
            return link.active

    def track_click(self, slug: str, ip: str = "", user_agent: str = "", referrer: str = "") -> Optional[str]:
        """Registra clique e retorna target_url se link existir + estiver ativo."""
        with self._lock:
            link = self._items.get(slug)
            if not link or not link.active:
                return None
            link.click_count += 1
            link.clicks.append({
                "ts": now_iso(),
                "ip_hash": hash_ip(ip),
                "ua": (user_agent or "")[:140],
                "ref": (referrer or "")[:140],
            })
            # Trim
            if len(link.clicks) > MAX_CLICKS_KEPT_PER_LINK:
                link.clicks = link.clicks[-MAX_CLICKS_KEPT_PER_LINK:]
            self._save()
            return link.target_url


manager = LinkManager()
