"""
Agendador de disparos.

Mantém uma lista persistida em schedules.json. Uma thread daemon roda em loop
verificando agendamentos pendentes — quando chega a hora, dispara o job
via web.jobs.manager e marca o schedule como "running" / "done" / "error".

Decisões:
  - Timezone: usa o local do servidor (Brasília no nosso caso). Datas são
    salvas em ISO 8601 com offset, exibidas como o cliente quiser.
  - Atrasados: se servidor estava off na hora, schedules > 5 min atrasados
    viram "missed" em vez de rodar (postar 4h atrasado é pior que pular).
  - Conflito: bloqueia 2 schedules pra MESMA conta específica com < 5 min de
    diferença. Quando o schedule é "todas as contas", deixa passar mas avisa.
  - One-shot: sem recorrência por enquanto.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from core.paths import SCHEDULES_FILE

# Quanto tempo depois da hora marcada o schedule é considerado "perdido"
MISS_GRACE_SECONDS = 5 * 60
# Janela de conflito (não permite 2 schedules pra mesma conta dentro disso)
CONFLICT_WINDOW_SECONDS = 5 * 60
# Intervalo do loop do scheduler
TICK_SECONDS = 30
# Limite de schedules guardados
MAX_SCHEDULES_KEPT = 200


def now_local() -> datetime:
    """Datetime com tzinfo local do servidor."""
    return datetime.now().astimezone()


def parse_iso(s: str) -> datetime:
    """Aceita ISO 8601 com ou sem timezone — se vier sem, assume local."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


class Schedule:
    def __init__(self, data: dict):
        self.id: str = data["id"]
        self.video: str = data["video"]
        self.account: Optional[str] = data.get("account") or None
        self.scheduled_at: str = data["scheduled_at"]  # ISO local
        self.status: str = data.get("status", "pending")  # pending | running | done | error | cancelled | missed
        self.created_at: str = data.get("created_at") or now_local().isoformat(timespec="seconds")
        self.created_by: Optional[str] = data.get("created_by")
        self.job_id: Optional[str] = data.get("job_id")
        self.note: Optional[str] = data.get("note")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "video": self.video,
            "account": self.account,
            "scheduled_at": self.scheduled_at,
            "status": self.status,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "job_id": self.job_id,
            "note": self.note,
        }

    @property
    def scheduled_dt(self) -> datetime:
        return parse_iso(self.scheduled_at)


class ScheduleManager:
    def __init__(self, job_manager):
        self._jobs = job_manager
        self._items: list[Schedule] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._load()

    # ---- persistence ----

    def _load(self):
        if not SCHEDULES_FILE.exists():
            return
        try:
            raw = json.loads(SCHEDULES_FILE.read_text(encoding="utf-8"))
            self._items = [Schedule(d) for d in raw]
        except Exception as e:
            print(f"[scheduler] failed to load schedules.json: {e}")

    def _save(self):
        try:
            data = [s.to_dict() for s in self._items]
            SCHEDULES_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[scheduler] failed to save: {e}")

    # ---- queries ----

    def list(self) -> list[dict]:
        with self._lock:
            items = sorted(self._items, key=lambda s: s.scheduled_at)
            return [s.to_dict() for s in items]

    def get(self, schedule_id: str) -> Optional[Schedule]:
        with self._lock:
            for s in self._items:
                if s.id == schedule_id:
                    return s
        return None

    def conflicts(self, account: Optional[str], when: datetime, ignore_id: Optional[str] = None) -> list[Schedule]:
        """Retorna schedules pendentes pra MESMA conta específica dentro da janela.
        Schedules 'todas as contas' (account=None) sempre são compatíveis (mas avisamos)."""
        if not account:
            return []
        out = []
        for s in self._items:
            if s.id == ignore_id:
                continue
            if s.status not in ("pending", "running"):
                continue
            if s.account != account:
                continue
            delta = abs((s.scheduled_dt - when).total_seconds())
            if delta < CONFLICT_WINDOW_SECONDS:
                out.append(s)
        return out

    # ---- mutations ----

    def create(self, video: str, account: Optional[str], scheduled_at: datetime, created_by: Optional[str] = None) -> Schedule:
        sched = Schedule({
            "id": uuid.uuid4().hex[:12],
            "video": video,
            "account": account,
            "scheduled_at": scheduled_at.isoformat(timespec="seconds"),
            "status": "pending",
            "created_at": now_local().isoformat(timespec="seconds"),
            "created_by": created_by,
        })
        with self._lock:
            self._items.append(sched)
            # Não deixa crescer sem controle
            if len(self._items) > MAX_SCHEDULES_KEPT:
                # Mantém só os mais recentes por created_at
                self._items = sorted(self._items, key=lambda s: s.created_at)[-MAX_SCHEDULES_KEPT:]
            self._save()
        return sched

    def cancel(self, schedule_id: str) -> bool:
        with self._lock:
            for s in self._items:
                if s.id == schedule_id:
                    if s.status != "pending":
                        return False
                    s.status = "cancelled"
                    self._save()
                    return True
        return False

    def delete(self, schedule_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [s for s in self._items if s.id != schedule_id]
            if len(self._items) == before:
                return False
            self._save()
            return True

    # ---- loop ----

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()
        print(f"[scheduler] started (tick {TICK_SECONDS}s)")

    def stop(self):
        self._stop_event.set()

    def _loop(self):
        # Primeira passada rápida pra processar pendentes antigos
        time.sleep(2)
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                print(f"[scheduler] tick falhou: {e}")
            # Espera, mas acorda se stop foi pedido
            self._stop_event.wait(TICK_SECONDS)

    def _tick(self):
        now = now_local()
        changed = False
        with self._lock:
            snapshot = list(self._items)

        for s in snapshot:
            # Update de jobs já disparados
            if s.status == "running" and s.job_id:
                job = self._jobs.get(s.job_id)
                if job and job.status in ("finished", "error", "cancelled"):
                    s.status = "done" if job.status == "finished" else job.status
                    changed = True
                continue

            if s.status != "pending":
                continue

            try:
                when = s.scheduled_dt
            except Exception:
                continue

            # Atrasado demais → missed
            if (now - when).total_seconds() > MISS_GRACE_SECONDS:
                s.status = "missed"
                s.note = f"servidor offline ou tick perdido (atrasado {int((now - when).total_seconds() // 60)}min)"
                changed = True
                continue

            # Chegou a hora → dispara
            if when <= now:
                self._dispatch(s)
                changed = True

        if changed:
            with self._lock:
                self._save()

    def _dispatch(self, s: Schedule):
        """Inicia o job correspondente ao schedule e marca como running."""
        args = ["post.py", "--video", s.video]
        label_bits = [s.video]
        if s.account:
            args += ["--conta", s.account]
            label_bits.append(f"@{s.account}")
        else:
            label_bits.append("todas conectadas")
        label = "agendado · " + " · ".join(label_bits)

        try:
            job = self._jobs.start(kind="scheduled", args=args, label=label)
            s.status = "running"
            s.job_id = job.id
            print(f"[scheduler] dispatched {s.id} -> job {job.id}")
        except Exception as e:
            s.status = "error"
            s.note = f"erro ao disparar: {e}"
            print(f"[scheduler] dispatch error {s.id}: {e}")


# Instância singleton (similar ao job_manager)
manager: Optional[ScheduleManager] = None


def init(job_manager) -> ScheduleManager:
    """Cria + inicia o manager. Idempotente."""
    global manager
    if manager is None:
        manager = ScheduleManager(job_manager)
        manager.start()
    return manager
