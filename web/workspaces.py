"""
Gerencia workspaces — pastas 100% isoladas pra modelos/clientes diferentes.

Cada workspace tem suas próprias contas, mídias, sessões e fotos de perfil.
Compartilhados entre workspaces: equipe (users/invites), workers, logs, finance,
schedules, links, remote_jobs (a fila atravessa workspaces porque o worker é
único).

Storage: data/workspaces_meta.json + pastas data/workspaces/<slug>/

Owner pode criar/renomear/deletar workspaces; membros só trocam o ativo.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
from datetime import datetime, timezone
from typing import Optional

from core import paths


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str) -> str:
    """Transforma 'Modelo 1' em 'modelo-1'."""
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:30] or "ws"


class Workspace:
    def __init__(self, data: dict):
        self.slug: str = data["slug"]
        self.name: str = data.get("name") or data["slug"]
        self.created_at: str = data.get("created_at") or now_iso()
        self.created_by: Optional[str] = data.get("created_by")

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }


class WorkspaceManager:
    def __init__(self):
        self._items: dict[str, Workspace] = {}
        self._lock = threading.Lock()
        self._load()
        self._ensure_default()

    def _load(self):
        if not paths.WORKSPACES_META_FILE.exists():
            return
        try:
            raw = json.loads(paths.WORKSPACES_META_FILE.read_text(encoding="utf-8"))
            for entry in raw:
                w = Workspace(entry)
                self._items[w.slug] = w
        except Exception as e:
            print(f"[workspaces] failed to load meta: {e}")

    def _save(self):
        try:
            data = [w.to_dict() for w in self._items.values()]
            paths.WORKSPACES_META_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[workspaces] failed to save: {e}")

    def _ensure_default(self):
        """Garante que workspace 'default' sempre existe (e está registrado).
        Também registra workspaces existentes em disco que não estão no meta."""
        with self._lock:
            disk_slugs = set(paths.list_workspace_slugs())
            disk_slugs.add(paths.DEFAULT_WORKSPACE)

            changed = False
            for slug in disk_slugs:
                if slug not in self._items:
                    # Workspace existe em disco mas não tem meta — registra
                    self._items[slug] = Workspace({
                        "slug": slug,
                        "name": "Padrão" if slug == paths.DEFAULT_WORKSPACE else slug,
                        "created_at": now_iso(),
                        "created_by": None,
                    })
                    changed = True

            if changed:
                self._save()

    def list(self) -> list[dict]:
        with self._lock:
            items = sorted(self._items.values(), key=lambda w: (w.slug != paths.DEFAULT_WORKSPACE, w.name.lower()))
            return [w.to_dict() for w in items]

    def get(self, slug: str) -> Optional[Workspace]:
        return self._items.get(slug)

    def exists(self, slug: str) -> bool:
        return slug in self._items

    def create(self, name: str, slug: Optional[str] = None, created_by: Optional[str] = None) -> Workspace:
        name = (name or "").strip()
        if not name:
            raise ValueError("Nome obrigatório")
        chosen = slug.strip().lower() if slug else slugify(name)
        if not SLUG_RE.match(chosen):
            raise ValueError(f"Slug inválido: '{chosen}'. Use só letras minúsculas, números e hífen.")
        with self._lock:
            if chosen in self._items:
                raise ValueError(f"Workspace '{chosen}' já existe")
            ws = Workspace({
                "slug": chosen,
                "name": name,
                "created_at": now_iso(),
                "created_by": created_by,
            })
            self._items[ws.slug] = ws
            # Cria pasta em disco
            paths.workspace_root(ws.slug)
            self._save()
            return ws

    def rename(self, slug: str, new_name: str) -> Workspace:
        new_name = (new_name or "").strip()
        if not new_name:
            raise ValueError("Nome obrigatório")
        with self._lock:
            ws = self._items.get(slug)
            if not ws:
                raise ValueError(f"Workspace '{slug}' não encontrado")
            ws.name = new_name
            self._save()
            return ws

    def delete(self, slug: str) -> bool:
        """Remove workspace e TODOS os dados dele. Não pode deletar 'default'."""
        if slug == paths.DEFAULT_WORKSPACE:
            raise ValueError("Workspace 'default' não pode ser deletado")
        with self._lock:
            if slug not in self._items:
                return False
            # Remove pasta em disco
            ws_path = paths.DATA_DIR / "workspaces" / slug
            if ws_path.exists():
                try:
                    shutil.rmtree(ws_path)
                except Exception as e:
                    print(f"[workspaces] erro removendo {ws_path}: {e}")
            del self._items[slug]
            self._save()
            return True


manager = WorkspaceManager()
