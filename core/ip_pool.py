"""
Banco de IPs — memória persistente do que cada IP já fez no Instagram.

Por que: proxy DataImpulse rotativo tem pool de milhares de IPs, mas
alguns estão QUEIMADOS pelo IG (já usados por bots, marcados como
suspeitos). Sem memória, o worker pode pegar o mesmo IP queimado
várias vezes seguidas e nunca conseguir logar.

Com este pool, o sistema aprende:
1. **Quais IPs já queimaram** (login falhou com IP_BLACKLISTED ou challenge)
   → próximas tentativas rotacionam sid pra evitar
2. **Qual conta usa qual IP** (1 IP = 1 conta, evita cluster)
   → impede 2 contas usarem o mesmo IP simultaneamente
3. **Quais IPs já provaram funcionar** (login OK)
   → reusa preferencialmente

Storage: JSON em DATA_DIR/ip_pool.json. Lock + atomic write.

Auto-cleanup: IPs queimados > 7d podem ser "perdoados" (DataImpulse
recicla IPs e o que tava queimado pode ter sido limpo).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from core.paths import data_path

POOL_FILE = data_path("ip_pool.json")

# Quantas falhas com challenge pra considerar IP burnt
CHALLENGE_BURN_THRESHOLD = 2
# Quanto tempo IP fica burnt antes de ser "perdoado" (h)
BURN_TTL_HOURS = 72
# Cache do IP detectado por (proxy_url) — evita ipify excessivo
_ip_cache: dict[str, tuple[str, float]] = {}
IP_CACHE_TTL = 10 * 60  # 10min

_lock = threading.RLock()
_pool_cache: Optional[dict] = None
_pool_loaded_at: float = 0


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    """Carrega pool do disco. Cacheia em memória pra evitar I/O excessivo."""
    global _pool_cache, _pool_loaded_at
    with _lock:
        # Reload se mais de 60s desde último load (outras instâncias podem ter mudado)
        if _pool_cache is not None and (time.time() - _pool_loaded_at) < 60:
            return _pool_cache
        p = Path(str(POOL_FILE))
        if not p.exists():
            _pool_cache = {"version": 1, "ips": {}}
            _pool_loaded_at = time.time()
            return _pool_cache
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if "ips" not in data:
                data["ips"] = {}
            _pool_cache = data
            _pool_loaded_at = time.time()
            return data
        except Exception as e:
            print(f"[ip_pool] erro carregando: {e}")
            _pool_cache = {"version": 1, "ips": {}}
            _pool_loaded_at = time.time()
            return _pool_cache


def _save(data: dict) -> None:
    """Atomic write: tmp + rename."""
    global _pool_cache
    p = Path(str(POOL_FILE))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(p)
        _pool_cache = data  # mantém cache atualizado
    except Exception as e:
        print(f"[ip_pool] erro salvando: {e}")


def get_current_ip_via_proxy(proxy_url: str, timeout: float = 6.0) -> Optional[str]:
    """Descobre o IP público REAL que sai pelo proxy (hit ipify).
    Cacheia por proxy_url + 10min — evita ipify excessivo.

    Retorna None se proxy falhar / timeout / erro."""
    if not proxy_url:
        return None
    now = time.time()
    cached = _ip_cache.get(proxy_url)
    if cached and (now - cached[1]) < IP_CACHE_TTL:
        return cached[0]
    try:
        import requests as _rq
        r = _rq.get(
            "https://api.ipify.org?format=json",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=timeout,
        )
        if r.status_code == 200:
            ip = r.json().get("ip")
            if ip:
                _ip_cache[proxy_url] = (ip, now)
                return ip
    except Exception:
        pass
    return None


def is_burnt(ip: str) -> bool:
    """True se o IP tá marcado como queimado E ainda dentro do TTL de burn."""
    if not ip:
        return False
    data = _load()
    entry = data["ips"].get(ip)
    if not entry or not entry.get("is_burnt"):
        return False
    burnt_at = entry.get("burnt_at")
    if not burnt_at:
        return True  # marcado burnt sem timestamp = considera burnt
    try:
        bd = datetime.fromisoformat(burnt_at)
        if bd.tzinfo is None:
            bd = bd.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - bd).total_seconds() / 3600
        if age_h >= BURN_TTL_HOURS:
            # Perdoa IP — pode ter sido reciclado
            with _lock:
                entry["is_burnt"] = False
                entry["forgiven_at"] = _now_utc_iso()
                _save(data)
            return False
        return True
    except Exception:
        return True


def owner_of(ip: str) -> Optional[str]:
    """Retorna o username da conta dona desse IP (se exclusivo)."""
    if not ip:
        return None
    data = _load()
    entry = data["ips"].get(ip)
    if not entry:
        return None
    owners = entry.get("owner_accounts") or []
    return owners[-1] if owners else None  # último dono que confirmou


def is_owned_by_other(ip: str, my_username: str) -> bool:
    """True se OUTRA conta (não eu) tá usando esse IP atualmente."""
    owner = owner_of(ip)
    if not owner or owner == my_username:
        return False
    return True


def mark_outcome(
    ip: str,
    account: str,
    outcome: str,  # 'ok' | 'blacklisted' | 'challenge' | 'unknown'
    error_msg: Optional[str] = None,
) -> None:
    """Registra resultado de tentativa com esse IP. Atualiza burnt status."""
    if not ip or not account:
        return
    with _lock:
        data = _load()
        entry = data["ips"].setdefault(ip, {
            "first_seen": _now_utc_iso(),
            "outcomes": {"ok": 0, "blacklisted": 0, "challenge": 0, "unknown": 0},
            "owner_accounts": [],
            "is_burnt": False,
        })
        entry["last_seen"] = _now_utc_iso()
        outcomes = entry.setdefault("outcomes", {"ok": 0, "blacklisted": 0, "challenge": 0, "unknown": 0})
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if error_msg:
            entry["last_error"] = error_msg[:200]
        # Atualiza burnt status:
        # - blacklisted = burnt imediato
        # - 2+ challenges = burnt
        # - ok = NÃO desburnt (se já é burnt fica burnt — pode ter sido sorte)
        if outcome == "blacklisted":
            entry["is_burnt"] = True
            entry["burnt_at"] = _now_utc_iso()
            entry["burnt_reason"] = "IP_BLACKLISTED"
        elif outcome == "challenge" and outcomes["challenge"] >= CHALLENGE_BURN_THRESHOLD:
            entry["is_burnt"] = True
            entry["burnt_at"] = _now_utc_iso()
            entry["burnt_reason"] = f"{outcomes['challenge']}x challenge"
        # Owner tracking
        if outcome == "ok":
            owners = entry.setdefault("owner_accounts", [])
            if account not in owners:
                owners.append(account)
                # Limita histórico aos últimos 5 donos
                entry["owner_accounts"] = owners[-5:]
        _save(data)


def release_account(account: str) -> None:
    """Remove account de TODOS os owners (use quando trocar proxy ou apagar conta)."""
    if not account:
        return
    with _lock:
        data = _load()
        changed = False
        for ip, entry in data["ips"].items():
            owners = entry.get("owner_accounts") or []
            if account in owners:
                entry["owner_accounts"] = [o for o in owners if o != account]
                changed = True
        if changed:
            _save(data)


def stats() -> dict:
    """Retorna estatísticas do pool pra dashboard."""
    data = _load()
    ips = data["ips"]
    total = len(ips)
    burnt = sum(1 for e in ips.values() if e.get("is_burnt"))
    active = sum(1 for e in ips.values() if e.get("owner_accounts"))
    ok_count = sum(e.get("outcomes", {}).get("ok", 0) for e in ips.values())
    fail_count = sum(
        e.get("outcomes", {}).get("blacklisted", 0) + e.get("outcomes", {}).get("challenge", 0)
        for e in ips.values()
    )
    return {
        "total_ips": total,
        "burnt_ips": burnt,
        "active_ips": active,
        "clean_ips": max(0, total - burnt),
        "total_logins_ok": ok_count,
        "total_logins_fail": fail_count,
    }
