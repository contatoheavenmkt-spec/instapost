"""
Resolve caminhos do projeto.

Dev local: DATA_DIR é a raiz do projeto (compatibilidade total).
Produção (Docker): export DATA_DIR=/app/data — todos os arquivos de estado vão pra lá.
"""
from __future__ import annotations

import os
from pathlib import Path

# Raiz do código (onde fica web/, core/, post.py, etc)
CODE_ROOT = Path(__file__).resolve().parent.parent

# Onde ficam os dados (state). Pode ser sobrescrito por env var.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(CODE_ROOT)))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def data_path(*parts) -> Path:
    """Resolve um path dentro do diretório de dados, criando subdirs se preciso."""
    return DATA_DIR.joinpath(*parts)


# Atalhos comuns
SESSIONS_DIR = data_path("sessions")
LOGS_DIR = data_path("logs")
PENDING_DIR = data_path("content", "pending")
POSTED_DIR = data_path("content", "posted")
ACCOUNTS_FILE = data_path("accounts.json")
USERS_FILE = data_path("users.json")
INVITES_FILE = data_path("invites.json")
SCHEDULES_FILE = data_path("schedules.json")
LINKS_FILE = data_path("links.json")
JOBS_FILE = data_path("logs", "jobs.json")
SECRET_FILE = data_path(".secret.key")

# Garante estrutura mínima
for d in (SESSIONS_DIR, LOGS_DIR, PENDING_DIR, POSTED_DIR):
    d.mkdir(parents=True, exist_ok=True)
