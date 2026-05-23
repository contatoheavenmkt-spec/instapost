"""
Lock de arquivo cross-platform (Windows + Linux/Mac).

Usado pra evitar race condition em session.json quando 2 jobs paralelos
da mesma conta tentam load/modify/dump ao mesmo tempo. Threading.Lock
do worker resolve dentro de UM processo, mas se rodar 2 workers
no mesmo PC (ou 2 instâncias de qualquer coisa que toca session.json),
threading não basta — precisa de lock no nível do FS.

Use:
    with file_lock(session_path):
        cl.load_settings(session_path)
        ... mexe ...
        cl.dump_settings(session_path)
"""
from __future__ import annotations

import os
import platform
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def file_lock(path, timeout: float = 30.0):
    """Lock exclusivo em arquivo. Bloqueia até adquirir ou timeout.

    Cria um sidecar `.lock` ao lado do arquivo original. O lock fica nesse
    sidecar pra evitar abrir o arquivo principal em modo exclusivo (que
    quebraria leituras normais)."""
    p = Path(str(path))
    lock_path = p.with_suffix(p.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+")
    try:
        _acquire(f, timeout)
        try:
            yield
        finally:
            _release(f)
    finally:
        try:
            f.close()
        except Exception:
            pass
        # Best-effort: remove o .lock se ninguém mais tá usando.
        # Pode falhar (outro processo segurando) — ignora.
        try:
            lock_path.unlink()
        except Exception:
            pass


def _acquire(f, timeout: float) -> None:
    if platform.system() == "Windows":
        import msvcrt
        deadline = time.time() + timeout
        while True:
            try:
                # LK_NBLCK = non-blocking exclusive lock
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(f"file lock timeout ({timeout}s): {f.name}")
                time.sleep(0.15)
    else:
        import fcntl
        # LOCK_EX bloqueia; setamos timeout via signal não é portável.
        # Implementamos loop manual com LOCK_NB.
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.time() >= deadline:
                    raise TimeoutError(f"file lock timeout ({timeout}s): {f.name}")
                time.sleep(0.15)


def _release(f) -> None:
    if platform.system() == "Windows":
        import msvcrt
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
    else:
        import fcntl
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
