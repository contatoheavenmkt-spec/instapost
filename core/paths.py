"""
Resolve caminhos do projeto.

Dev local: DATA_DIR é a raiz do projeto (compatibilidade total).
Produção (Docker): export DATA_DIR=/app/data — todos os arquivos de estado vão pra lá.

**Workspaces:** cada workspace tem sua própria pasta isolada dentro de
DATA_DIR/workspaces/<slug>/ contendo accounts.json, content/, sessions/ e
profile_pics/. Globais (compartilhados): users, invites, workers, logs,
schedules, links, finance, remote_jobs, .secret.key.

Uso:
    from core import paths
    paths.accounts_file()       # workspace ativo (via contextvar)
    paths.pending_dir("modelo-2")  # workspace específico
"""
from __future__ import annotations

import contextvars
import os
from pathlib import Path

# Raiz do código (onde fica web/, core/, post.py, etc)
CODE_ROOT = Path(__file__).resolve().parent.parent

# Onde ficam os dados (state). Pode ser sobrescrito por env var.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(CODE_ROOT)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_WORKSPACE = "default"

# Contexto do workspace ativo. Setado por:
#   - Middleware HTTP (a cada request, baseado em request.session['workspace'])
#   - Background threads (explicitamente, iterando por workspace)
_current_ws: contextvars.ContextVar[str] = contextvars.ContextVar(
    "workspace", default=DEFAULT_WORKSPACE
)


def data_path(*parts) -> Path:
    """Resolve um path dentro do diretório de dados, criando subdirs se preciso."""
    return DATA_DIR.joinpath(*parts)


# ---- Workspace context API ----

def get_workspace() -> str:
    """Workspace ativo no contexto atual."""
    return _current_ws.get() or DEFAULT_WORKSPACE


def set_workspace(slug: str | None) -> None:
    """Define o workspace ativo no contexto atual (thread/task local)."""
    _current_ws.set((slug or DEFAULT_WORKSPACE).strip() or DEFAULT_WORKSPACE)


def workspace_root(slug: str | None = None) -> Path:
    """Pasta raiz de um workspace. Cria se não existir."""
    slug = (slug or get_workspace()).strip() or DEFAULT_WORKSPACE
    p = DATA_DIR / "workspaces" / slug
    p.mkdir(parents=True, exist_ok=True)
    return p


def list_workspace_slugs() -> list[str]:
    """Lista todos os slugs de workspaces existentes em disco."""
    root = DATA_DIR / "workspaces"
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


# ---- Workspace-aware paths (mudam conforme o workspace ativo) ----

def accounts_file(slug: str | None = None) -> Path:
    return workspace_root(slug) / "accounts.json"


def sessions_dir(slug: str | None = None) -> Path:
    d = workspace_root(slug) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pending_dir(slug: str | None = None) -> Path:
    d = workspace_root(slug) / "content" / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def posted_dir(slug: str | None = None) -> Path:
    d = workspace_root(slug) / "content" / "posted"
    d.mkdir(parents=True, exist_ok=True)
    return d


def variants_dir(slug: str | None = None) -> Path:
    d = workspace_root(slug) / "content" / "variants"
    d.mkdir(parents=True, exist_ok=True)
    return d


def profile_pics_dir(slug: str | None = None) -> Path:
    d = workspace_root(slug) / "profile_pics"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---- Globais (não mudam por workspace) ----

LOGS_DIR = data_path("logs")
LOGS_DIR.mkdir(parents=True, exist_ok=True)
USERS_FILE = data_path("users.json")
INVITES_FILE = data_path("invites.json")
WORKSPACES_META_FILE = data_path("workspaces_meta.json")
SCHEDULES_FILE = data_path("schedules.json")  # global por ora
LINKS_FILE = data_path("links.json")          # global por ora
JOBS_FILE = data_path("logs", "jobs.json")
SECRET_FILE = data_path(".secret.key")


# ---- Migração: estado pré-workspace (DATA_DIR/accounts.json) -> default ws ----

_MIGRATION_DONE = False


def migrate_legacy_to_default_workspace() -> bool:
    """Se há dados no estilo pré-workspace (accounts.json na raiz), move pro
    workspace 'default'. Idempotente: roda 1x na vida da pasta.

    Retorna True se migrou alguma coisa.
    """
    global _MIGRATION_DONE
    if _MIGRATION_DONE:
        return False
    _MIGRATION_DONE = True

    legacy_accounts = DATA_DIR / "accounts.json"
    default_root = DATA_DIR / "workspaces" / DEFAULT_WORKSPACE
    moved_anything = False

    # Só migra se:
    #  - legacy accounts.json existe
    #  - workspaces/default ainda não tem accounts.json (não duplica)
    if legacy_accounts.exists() and not (default_root / "accounts.json").exists():
        default_root.mkdir(parents=True, exist_ok=True)
        legacy_accounts.rename(default_root / "accounts.json")
        moved_anything = True

    # Mover content/, sessions/, profile_pics/ se existirem na raiz
    for sub in ("content", "sessions", "profile_pics"):
        src = DATA_DIR / sub
        dst = default_root / sub
        if src.exists() and not dst.exists():
            default_root.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            moved_anything = True

    return moved_anything


# Tenta migrar no import (idempotente, barato se não precisa). Mesmo sem chamar
# explicitamente, qualquer importação do módulo já move o estado legado.
try:
    migrate_legacy_to_default_workspace()
except Exception as _e:
    # Não bloqueia o boot se migração falhar — só loga.
    print(f"[paths] migração legacy->workspace falhou: {_e}")

# Garante que workspace default existe
(DATA_DIR / "workspaces" / DEFAULT_WORKSPACE).mkdir(parents=True, exist_ok=True)


# ---- Shims de retrocompatibilidade ----
# Proxies que resolvem o path dinamicamente baseado no workspace ATIVO no
# contextvar. Permitem que código antigo (ACCOUNTS_FILE, PENDING_DIR, etc)
# continue funcionando sem reescrever 60+ usos.

class _WorkspacePathProxy:
    """Path-like que sempre resolve no momento do acesso (ws-aware)."""

    __slots__ = ("_resolver",)

    def __init__(self, resolver_fn):
        object.__setattr__(self, "_resolver", resolver_fn)

    def _resolve(self) -> Path:
        return self._resolver()

    # Operações de Path mais comuns
    def __truediv__(self, other):
        return self._resolve() / other

    def __rtruediv__(self, other):
        return other / self._resolve()

    def __fspath__(self):
        return str(self._resolve())

    def __str__(self):
        return str(self._resolve())

    def __repr__(self):
        return f"<_WorkspacePathProxy {self._resolve()}>"

    # Delega qualquer outro atributo pro Path concreto
    def __getattr__(self, attr):
        return getattr(self._resolve(), attr)


ACCOUNTS_FILE = _WorkspacePathProxy(accounts_file)
PENDING_DIR = _WorkspacePathProxy(pending_dir)
POSTED_DIR = _WorkspacePathProxy(posted_dir)
SESSIONS_DIR = _WorkspacePathProxy(sessions_dir)
VARIANTS_DIR = _WorkspacePathProxy(variants_dir)
PROFILE_PICS_DIR = _WorkspacePathProxy(profile_pics_dir)
