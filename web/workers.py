"""
Gerencia workers remotos — PCs da equipe que executam disparos com IP residencial.

Cada worker tem:
  - id (gerado pelo servidor)
  - name (nome amigável, ex: "PC do Edson")
  - token (chave de auth, longa e revogável)
  - last_seen (último heartbeat)
  - created_at, created_by

Storage: workers.json no DATA_DIR.

Worker registra usando token gerado pelo owner. Heartbeat a cada N segundos
mantém status "online". Sem heartbeat por > 60s = "offline".
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from typing import Optional

from core.paths import data_path

WORKERS_FILE = data_path("workers.json")
ONLINE_THRESHOLD_SECONDS = 60
TOKEN_LEN = 40


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


class Worker:
    def __init__(self, data: dict):
        self.id: str = data["id"]
        self.name: str = data["name"]
        self.token: str = data["token"]
        self.last_seen: Optional[str] = data.get("last_seen")
        self.created_at: str = data.get("created_at") or now_iso()
        self.created_by: Optional[str] = data.get("created_by")
        self.last_ip: Optional[str] = data.get("last_ip")
        self.platform: Optional[str] = data.get("platform")  # ex: "Windows 11"

    def to_dict(self, hide_token: bool = False) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "last_seen": self.last_seen,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "last_ip": self.last_ip,
            "platform": self.platform,
            "online": self.is_online(),
        }
        if not hide_token:
            d["token"] = self.token
        return d

    def is_online(self) -> bool:
        ts = parse_iso(self.last_seen)
        if not ts:
            return False
        delta = (datetime.now(timezone.utc) - ts).total_seconds()
        return delta < ONLINE_THRESHOLD_SECONDS


class WorkerManager:
    def __init__(self):
        self._items: dict[str, Worker] = {}
        self._token_index: dict[str, str] = {}  # token -> worker_id
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if not WORKERS_FILE.exists():
            return
        try:
            raw = json.loads(WORKERS_FILE.read_text(encoding="utf-8"))
            for entry in raw:
                w = Worker(entry)
                self._items[w.id] = w
                self._token_index[w.token] = w.id
        except Exception as e:
            print(f"[workers] failed to load: {e}")

    def _save(self):
        try:
            data = [w.to_dict(hide_token=False) for w in self._items.values()]
            # to_dict adiciona "online" computado — remove pra não persistir
            for d in data:
                d.pop("online", None)
                # Re-inclui token (to_dict tem hide_token=False mas pra garantir)
                d["token"] = self._items[d["id"]].token
            WORKERS_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[workers] failed to save: {e}")

    # ---- queries ----

    def list(self, hide_token: bool = True) -> list[dict]:
        with self._lock:
            items = sorted(self._items.values(), key=lambda w: w.created_at)
            return [w.to_dict(hide_token=hide_token) for w in items]

    def get(self, worker_id: str) -> Optional[Worker]:
        return self._items.get(worker_id)

    def by_token(self, token: str) -> Optional[Worker]:
        wid = self._token_index.get(token)
        if not wid:
            return None
        return self._items.get(wid)

    def online_workers(self) -> list[Worker]:
        return [w for w in self._items.values() if w.is_online()]

    # ---- mutations ----

    def create(self, name: str, created_by: Optional[str] = None) -> Worker:
        name = (name or "").strip() or "Worker sem nome"
        with self._lock:
            worker = Worker({
                "id": "wk_" + secrets.token_hex(8),
                "name": name,
                "token": secrets.token_urlsafe(TOKEN_LEN),
                "last_seen": None,
                "created_at": now_iso(),
                "created_by": created_by,
                "last_ip": None,
                "platform": None,
            })
            self._items[worker.id] = worker
            self._token_index[worker.token] = worker.id
            self._save()
            return worker

    def revoke(self, worker_id: str) -> bool:
        with self._lock:
            worker = self._items.pop(worker_id, None)
            if not worker:
                return False
            self._token_index.pop(worker.token, None)
            self._save()
            return True

    def heartbeat(self, token: str, ip: str = "", platform: str = "") -> Optional[Worker]:
        worker = self.by_token(token)
        if not worker:
            return None
        with self._lock:
            worker.last_seen = now_iso()
            if ip:
                worker.last_ip = ip
            if platform:
                worker.platform = platform
            self._save()
        return worker


manager = WorkerManager()
