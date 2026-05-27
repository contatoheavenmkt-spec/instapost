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

# Bots que acessam URLs pra gerar preview (Instagram, WhatsApp, etc).
# Quando posta link no story do Insta, o IG dispara facebookexternalhit pra
# pegar thumbnail — antes esse hit virava +1 "clique", inflando métrica.
# Agora: ainda registra o hit (com is_bot=True) mas NÃO incrementa click_count.
BOT_USER_AGENTS = (
    "facebookexternalhit", "facebookcatalog",
    "whatsapp",
    "telegrambot",
    "twitterbot",
    "discordbot",
    "linkedinbot", "linkedin",
    "slackbot",
    "googlebot", "bingbot", "duckduckbot", "yandexbot",
    "applebot",
    "skypeuripreview",
    "embedly",
    "redditbot",
    "pinterestbot",
    "tumblr",
    "vkshare",
    "headlesschrome",  # crawlers / preview headless
    "puppeteer", "playwright",
    "lighthouse",
    "ahrefsbot", "semrushbot", "mj12bot",  # SEO crawlers
    "spider", "crawler", "scraper",  # genérico
    "bot/",  # genérico "MyBot/1.0"
)
# Dedup: mesmo IP+UA dentro de 30s = 1 clique só (anti reload spam / prefetch)
DEDUP_WINDOW_SECONDS = 30


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
        # Workspace dono do link — isolamento na UI. Links antigos sem campo = "default".
        # Redirect via /r/SLUG continua funcionando sem filtro de ws (slug é global).
        self.workspace_slug: str = data.get("workspace_slug") or "default"
        self.active: bool = data.get("active", True)
        # click_count = HITS REAIS de usuario (excluido bot + dedupado)
        self.click_count: int = data.get("click_count", 0)
        # bot_count = hits de preview (facebookexternalhit, whatsapp, etc) — separados
        self.bot_count: int = data.get("bot_count", 0)
        self.clicks: list = data.get("clicks", [])

    def to_dict(self) -> dict:
        # unique_clicks = IPs distintos entre cliques reais (exclui bots)
        unique_ips = set()
        for c in self.clicks:
            if not c.get("is_bot") and c.get("ip_hash"):
                unique_ips.add(c["ip_hash"])
        return {
            "slug": self.slug,
            "target_url": self.target_url,
            "label": self.label,
            "account": self.account,
            "parent_slug": self.parent_slug,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "workspace_slug": self.workspace_slug,
            "active": self.active,
            "click_count": self.click_count,
            "unique_clicks": len(unique_ips),
            "bot_count": self.bot_count,
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
            # Backfill 1x: dados antigos não tinham is_bot/is_duplicate.
            # Reclassifica retroativamente baseado no UA guardado.
            self._backfill_bot_flags()
        except Exception as e:
            print(f"[shortener] failed to load: {e}")

    def _backfill_bot_flags(self):
        """Reclassifica cliques antigos pra ter is_bot/is_duplicate flag.
        Antes do bot-filter, cada hit virava clique — agora separamos.
        Roda 1x no boot. Idempotente: clicks que já têm is_bot ficam intactos."""
        from datetime import datetime as _dt
        changed = False
        for link in self._items.values():
            real_count = 0
            bot_count = 0
            # Pra dedup retroativo precisamos ordenar por timestamp e olhar pares
            sorted_clicks = sorted(link.clicks, key=lambda c: c.get("ts", ""))
            seen: dict[tuple[str, str], str] = {}  # (ip, ua) -> last_ts
            for c in sorted_clicks:
                # Já classificado? skip (idempotente)
                already_classified = "is_bot" in c
                if not already_classified:
                    ua = (c.get("ua") or "").lower()
                    c["is_bot"] = any(pat in ua for pat in BOT_USER_AGENTS)
                    # Dedup: mesmo (ip, ua) dentro de 30s do anterior = duplicate
                    c["is_duplicate"] = False
                    if not c["is_bot"]:
                        key = (c.get("ip_hash") or "", c.get("ua") or "")
                        prev_ts = seen.get(key)
                        if prev_ts:
                            try:
                                delta = (_dt.fromisoformat(c["ts"]) - _dt.fromisoformat(prev_ts)).total_seconds()
                                if delta < DEDUP_WINDOW_SECONDS:
                                    c["is_duplicate"] = True
                            except Exception:
                                pass
                        if not c["is_duplicate"]:
                            seen[key] = c["ts"]
                    changed = True
                # Recalcula contadores totais a partir das flags (single source of truth)
                if c.get("is_bot"):
                    bot_count += 1
                elif not c.get("is_duplicate"):
                    real_count += 1
            # Atualiza contadores cumulativos baseado nas flags atuais
            if link.click_count != real_count or link.bot_count != bot_count:
                link.click_count = real_count
                link.bot_count = bot_count
                changed = True
        if changed:
            try:
                data = [l.to_dict() for l in self._items.values()]
                LINKS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[shortener] backfill bot flags: reclassificou {len(self._items)} link(s)")
            except Exception as e:
                print(f"[shortener] backfill save falhou: {e}")

    def _save(self):
        try:
            data = [l.to_dict() for l in self._items.values()]
            LINKS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[shortener] failed to save: {e}")

    # ---- queries ----

    def list(self, workspace_slug: Optional[str] = None) -> list[dict]:
        """Lista links. Se workspace_slug for fornecido, filtra apenas dele.
        None = retorna todos. Redirect via /r/SLUG NÃO usa esse filtro (slugs são globais)."""
        with self._lock:
            items = sorted(self._items.values(), key=lambda l: l.created_at, reverse=True)
            if workspace_slug:
                items = [l for l in items if l.workspace_slug == workspace_slug]
            return [l.to_dict() for l in items]

    def get(self, slug: str) -> Optional[Link]:
        return self._items.get(slug)

    # ---- mutations ----

    def create(self, target_url: str, label: Optional[str] = None,
               account: Optional[str] = None, parent_slug: Optional[str] = None,
               created_by: Optional[str] = None, slug: Optional[str] = None,
               workspace_slug: Optional[str] = None) -> Link:
        target_url = (target_url or "").strip()
        if not target_url:
            raise ValueError("target_url obrigatório")
        if not (target_url.startswith("http://") or target_url.startswith("https://")):
            target_url = "https://" + target_url

        # Auto-popular workspace do contexto se caller não setou
        if not workspace_slug:
            try:
                from core.paths import get_workspace
                workspace_slug = get_workspace()
            except Exception:
                workspace_slug = "default"

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
                "workspace_slug": workspace_slug,
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

    def delete_by_account(self, username: str, workspace_slug: Optional[str] = None) -> int:
        """Remove TODOS os links de uma conta específica. Usado em cleanup."""
        if not username:
            return 0
        with self._lock:
            slugs_to_delete = [
                slug for slug, link in self._items.items()
                if link.account == username
                and (not workspace_slug or link.workspace_slug == workspace_slug)
            ]
            for slug in slugs_to_delete:
                del self._items[slug]
            if slugs_to_delete:
                self._save()
            return len(slugs_to_delete)

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
        """Registra clique e retorna target_url se link existir + estiver ativo.

        Filtros aplicados pra métricas REAIS:
        1. **Bot detection**: hits de preview (facebookexternalhit, WhatsApp,
           etc) ainda redirecionam mas NÃO contam como clique de usuário.
           Salvos em bot_count separado.
        2. **Dedup 30s**: mesmo IP+UA dentro de janela curta = 1 clique (anti
           reload spam, prefetch de browser, double-tap mobile).

        Resultado: click_count reflete usuários REAIS clicando, não previews.
        """
        ua_lower = (user_agent or "").lower()
        is_bot = any(pat in ua_lower for pat in BOT_USER_AGENTS)

        with self._lock:
            link = self._items.get(slug)
            if not link or not link.active:
                return None

            ip_h = hash_ip(ip)

            # Dedup: se MESMO ip+ua clicou nos últimos DEDUP_WINDOW_SECONDS, não
            # conta de novo (mas ainda registra o evento — útil pra debug).
            is_duplicate = False
            if not is_bot and ip_h:
                from datetime import datetime as _dt
                try:
                    now_dt = _dt.fromisoformat(now_iso())
                    for prev in reversed(link.clicks[-50:]):  # olha só últimos 50 pra ser rápido
                        if prev.get("ip_hash") != ip_h:
                            continue
                        if prev.get("ua") != (user_agent or "")[:140]:
                            continue
                        try:
                            prev_dt = _dt.fromisoformat(prev["ts"])
                            if (now_dt - prev_dt).total_seconds() < DEDUP_WINDOW_SECONDS:
                                is_duplicate = True
                                break
                        except Exception:
                            pass
                except Exception:
                    pass

            link.clicks.append({
                "ts": now_iso(),
                "ip_hash": ip_h,
                "ua": (user_agent or "")[:140],
                "ref": (referrer or "")[:140],
                "is_bot": is_bot,
                "is_duplicate": is_duplicate,
            })

            # Contador real: só incrementa se NÃO é bot E NÃO é duplicado
            if is_bot:
                link.bot_count += 1
            elif not is_duplicate:
                link.click_count += 1

            # Trim
            if len(link.clicks) > MAX_CLICKS_KEPT_PER_LINK:
                link.clicks = link.clicks[-MAX_CLICKS_KEPT_PER_LINK:]
            self._save()
            return link.target_url


manager = LinkManager()
