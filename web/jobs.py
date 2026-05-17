"""
Gerenciador de jobs em background.
Executa post.py / test_login.py como subprocess, captura stdout/stderr em tempo real,
mantém estado em memória + snapshot em disco (logs/jobs.json).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.paths import CODE_ROOT, JOBS_FILE

ROOT = CODE_ROOT  # processo subprocess roda do code root (onde estão post.py, test_login.py)
JOBS_FILE.parent.mkdir(exist_ok=True)

MAX_LINES_PER_JOB = 2000
MAX_JOBS_KEPT = 50


class Job:
    def __init__(self, kind: str, args: list[str], label: str):
        self.id: str = uuid.uuid4().hex[:12]
        self.kind: str = kind  # "post" | "test_login"
        self.label: str = label
        self.args: list[str] = args
        self.status: str = "queued"  # queued | running | finished | error | cancelled
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.exit_code: Optional[int] = None
        self.lines: deque[str] = deque(maxlen=MAX_LINES_PER_JOB)
        self.awaiting: Optional[dict] = None  # {prompt: str} quando script espera input
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    def to_dict(self, include_lines: bool = True) -> dict:
        d = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "args": self.args,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "line_count": len(self.lines),
            "awaiting": self.awaiting,
        }
        if include_lines:
            d["lines"] = list(self.lines)
        return d

    def send_input(self, value: str) -> bool:
        """Escreve no stdin do subprocess (quando o script tá esperando input via input())."""
        with self._lock:
            if not self._proc or self._proc.poll() is not None:
                return False
            if not self._proc.stdin:
                return False
            try:
                self._proc.stdin.write(value.rstrip("\n") + "\n")
                self._proc.stdin.flush()
                self.awaiting = None
                self.lines.append(f"[input enviado] {value[:20]}{'…' if len(value) > 20 else ''}")
                return True
            except Exception:
                return False


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._order: deque[str] = deque(maxlen=MAX_JOBS_KEPT)
        self._lock = threading.Lock()
        self._load_snapshot()

    def _load_snapshot(self):
        if not JOBS_FILE.exists():
            return
        try:
            data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
            for entry in data:
                j = Job(entry["kind"], entry.get("args", []), entry.get("label", ""))
                j.id = entry["id"]
                j.status = entry["status"]
                if j.status == "running":
                    j.status = "error"
                    j.lines.append("[restored] processo perdido após restart do servidor")
                j.started_at = entry.get("started_at")
                j.finished_at = entry.get("finished_at")
                j.exit_code = entry.get("exit_code")
                for ln in entry.get("lines", []):
                    j.lines.append(ln)
                self._jobs[j.id] = j
                self._order.append(j.id)
        except Exception:
            pass

    def _save_snapshot(self):
        with self._lock:
            data = [self._jobs[jid].to_dict() for jid in self._order if jid in self._jobs]
        try:
            JOBS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def list(self) -> list[dict]:
        with self._lock:
            return [self._jobs[jid].to_dict(include_lines=False) for jid in reversed(self._order) if jid in self._jobs]

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def start(self, kind: str, args: list[str], label: str, env: Optional[dict] = None) -> Job:
        job = Job(kind=kind, args=args, label=label)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        thread = threading.Thread(target=self._run, args=(job, env), daemon=True)
        thread.start()
        return job

    def _run(self, job: Job, env: Optional[dict]):
        job.status = "running"
        job.started_at = datetime.now().isoformat(timespec="seconds")
        job.lines.append(f"[start] {job.label}")
        job.lines.append(f"[cmd] python {' '.join(job.args)}")
        self._save_snapshot()

        py_exe = ROOT / "venv" / "Scripts" / "python.exe"
        if not py_exe.exists():
            py_exe = Path(sys.executable)

        proc_env = os.environ.copy()
        proc_env["PYTHONIOENCODING"] = "utf-8"
        proc_env["PYTHONUTF8"] = "1"
        if env:
            proc_env.update(env)

        try:
            with job._lock:
                job._proc = subprocess.Popen(
                    [str(py_exe), "-u", *job.args],
                    cwd=str(ROOT),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=proc_env,
                    bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )

            assert job._proc.stdout is not None
            last_save = datetime.now()
            awaiting_re = __import__("re").compile(r"^\[AWAITING:(.+?)\]\s*$")
            for raw in job._proc.stdout:
                line = raw.rstrip("\n")
                m = awaiting_re.match(line)
                if m:
                    job.awaiting = {"prompt": m.group(1).strip()}
                    job.lines.append(line)
                else:
                    job.lines.append(line)
                if (datetime.now() - last_save).total_seconds() > 2:
                    self._save_snapshot()
                    last_save = datetime.now()

            job.exit_code = job._proc.wait()
            job.status = "finished" if job.exit_code == 0 else "error"
            job.lines.append(f"[exit] code={job.exit_code}")
        except Exception as e:
            job.status = "error"
            job.lines.append(f"[exception] {e}")
        finally:
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            self._save_snapshot()

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status != "running":
            return False
        with job._lock:
            if job._proc and job._proc.poll() is None:
                try:
                    job._proc.terminate()
                except Exception:
                    return False
        job.lines.append("[cancel] terminate enviado")
        job.status = "cancelled"
        self._save_snapshot()
        return True


manager = JobManager()
