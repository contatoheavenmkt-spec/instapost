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
        # Legacy: job_id era único quando disparava local. Agora usamos remote_job_ids (N jobs).
        self.job_id: Optional[str] = data.get("job_id")
        self.remote_job_ids: list[str] = data.get("remote_job_ids", []) or []
        # via: "worker" (default novo) ou "server" (legacy)
        self.via: str = data.get("via", "worker")
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
            "remote_job_ids": self.remote_job_ids,
            "via": self.via,
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
        # Thread separada pra automações (auto-like, auto-follow-back)
        self._automation_thread = threading.Thread(
            target=self._automation_loop, daemon=True, name="automation_scheduler"
        )
        self._automation_thread.start()
        # Thread separada pra sync/backfill (contas novas pegando feed antigo)
        self._sync_thread = threading.Thread(
            target=self._sync_loop, daemon=True, name="sync_scheduler"
        )
        self._sync_thread.start()
        print(f"[scheduler] started (tick {TICK_SECONDS}s) + automations + sync loops")

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
            # ----- Updates de schedules em running -----
            if s.status == "running":
                # Modo worker: checa status de TODOS os remote_jobs
                if s.via == "worker" and s.remote_job_ids:
                    final, err = self._aggregate_remote_status(s)
                    if final:
                        s.status = final
                        if err:
                            s.note = err
                        changed = True
                # Modo server legacy
                elif s.job_id:
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

    def _aggregate_remote_status(self, s: Schedule):
        """Verifica todos os remote_jobs do schedule e agrega num status final.
        Retorna (status, note) ou (None, None) se ainda tem job rodando."""
        try:
            from web.remote_jobs import manager as rjob_manager
        except Exception:
            return None, None

        statuses = []
        errors = []
        for rjid in s.remote_job_ids:
            rj = rjob_manager.get(rjid)
            if rj is None:
                continue
            statuses.append(rj.status)
            if rj.error_msg:
                errors.append(f"{rj.account_username}: {rj.error_msg}")

        if not statuses:
            return None, None

        # Se algum ainda tá pending/claimed/running, não fecha
        active = {"pending", "claimed", "running"}
        if any(st in active for st in statuses):
            return None, None

        # Todos terminaram
        oks = sum(1 for st in statuses if st == "done")
        total = len(statuses)
        if oks == total:
            return "done", f"{oks}/{total} contas postadas"
        if oks == 0:
            return "error", "; ".join(errors[:3]) or f"0/{total} OK"
        return "done", f"{oks}/{total} contas postadas (parcial)"

    def _dispatch(self, s: Schedule):
        """Inicia o job(s) correspondente(s) ao schedule e marca como running.
        Padrão novo: dispara via worker (remote_jobs). Fallback: server (post.py local)."""
        try:
            if s.via == "worker":
                self._dispatch_via_worker(s)
            else:
                self._dispatch_via_server(s)
        except Exception as e:
            s.status = "error"
            s.note = f"erro ao disparar: {e}"
            print(f"[scheduler] dispatch error {s.id}: {e}")

    def _dispatch_via_worker(self, s: Schedule):
        """Cria 1 remote_job por conta conectada via worker.
        - account específica → 1 remote_job
        - account=None       → N remote_jobs (todas conectadas via worker)
        """
        # Importações lazy pra evitar ciclo
        from web.remote_jobs import manager as rjob_manager
        from core.paths import PENDING_DIR, ACCOUNTS_FILE
        from core.poster import load_meta, detect_media_kind, load_caption
        from web.shortener import manager as link_manager
        import os as _os, json as _json

        # Lê accounts.json
        try:
            with open(ACCOUNTS_FILE, encoding="utf-8") as f:
                all_accounts = _json.load(f)
        except Exception as e:
            raise RuntimeError(f"accounts.json: {e}")

        # Filtra contas alvo
        if s.account:
            targets = [a for a in all_accounts if a["username"] == s.account]
            if not targets:
                raise RuntimeError(f"Conta @{s.account} não existe")
        else:
            # "todas conectadas via worker"
            targets = [
                a for a in all_accounts
                if a.get("active", True) and a.get("connected_via_worker_id")
            ]
            if not targets:
                raise RuntimeError("Nenhuma conta conectada via worker")

        # Lê info da mídia
        media_path = PENDING_DIR / s.video
        if not media_path.exists():
            raise RuntimeError(f"Mídia {s.video} não está em pending")
        meta = load_meta(str(media_path))
        media_type = detect_media_kind(str(media_path))
        caption = load_caption(str(media_path))
        link_url = meta.get("link_url")
        link_text = meta.get("link_text") or "Clique aqui"

        # URL pública pro worker baixar
        base = _os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or "http://127.0.0.1:8000"
        media_url = f"{base}/api/worker/media/{s.video}"

        created_ids = []
        for acc in targets:
            # Encurta link se for story
            this_link = link_url
            if meta.get("kind") == "story" and link_url:
                try:
                    short = link_manager.create(
                        target_url=link_url,
                        label=f"agendado · @{acc['username']}",
                        account=acc["username"],
                        created_by="scheduler",
                    )
                    this_link = f"{base}/r/{short.slug}"
                except Exception as e:
                    print(f"[scheduler] shortener falhou pra {acc['username']}: {e}")

            highlight_title = None
            if meta.get("kind") == "story" and acc.get("auto_highlight_enabled") and acc.get("auto_highlight_title"):
                highlight_title = acc["auto_highlight_title"]

            rj = rjob_manager.create({
                "operation": "post",
                "params": {"auto_highlight_title": highlight_title} if highlight_title else {},
                "account_username": acc["username"],
                "account_password": acc["password"],
                "account_totp_secret": acc.get("totp_secret"),
                "account_proxy": acc.get("proxy"),
                "video_name": s.video,
                "media_type": media_type,
                "kind": meta.get("kind", "reel"),
                "caption": caption,
                "link_url": this_link,
                "link_text": link_text,
                "media_url": media_url,
                "created_by": f"schedule:{s.id}",
            })
            created_ids.append(rj.id)

        s.status = "running"
        s.remote_job_ids = created_ids
        s.note = f"{len(created_ids)} remote_job(s) criados"
        print(f"[scheduler] dispatched {s.id} via worker -> {len(created_ids)} jobs")

    # ===================== AUTOMAÇÕES =====================

    def _automation_loop(self):
        """Loop separado pra criar jobs de auto-like e auto-follow-back ao
        longo do dia, espaçados aleatoriamente respeitando o limite diário."""
        import time as _t, random as _r
        # Espera 60s pra app subir
        _t.sleep(60)
        while not self._stop_event.is_set():
            try:
                self._automation_tick()
            except Exception as e:
                print(f"[automation] tick falhou: {e}")
            # Acorda a cada 5-12 min (aleatório pra não criar padrão)
            interval = _r.randint(5 * 60, 12 * 60)
            self._stop_event.wait(interval)

    def _automation_tick(self):
        """Pra cada conta com automação ligada e que ainda não bateu o limite
        do dia, com chance aleatória, cria 1 remote_job. Só age durante 'janela
        ativa' (7h-22h horário do servidor) pra simular humano."""
        import json as _json, random as _r
        from datetime import datetime as _dt
        from core.paths import ACCOUNTS_FILE

        # Janela de atividade 07:00 - 22:00
        hour = _dt.now().hour
        if hour < 7 or hour >= 22:
            return

        try:
            with open(ACCOUNTS_FILE, encoding="utf-8") as f:
                accounts = _json.load(f)
        except Exception:
            return

        try:
            from web.remote_jobs import manager as rjob_manager
        except Exception:
            return

        today_str = _dt.now().strftime("%Y-%m-%d")
        changed = False

        for acc in accounts:
            # Pula contas inativas ou não conectadas via worker
            if not acc.get("active", True):
                continue
            if not acc.get("connected_via_worker_id"):
                continue

            # Reseta contadores diários se o dia mudou
            if acc.get("auto_like_today_date") != today_str:
                acc["auto_like_today_date"] = today_str
                acc["auto_like_today_count"] = 0
                changed = True
            if acc.get("auto_follow_back_today_date") != today_str:
                acc["auto_follow_back_today_date"] = today_str
                acc["auto_follow_back_today_count"] = 0
                changed = True

            # ------ AUTO LIKE ------
            if acc.get("auto_like_enabled"):
                limit = int(acc.get("auto_like_max_per_day", 0))
                done = int(acc.get("auto_like_today_count", 0))
                if done < limit:
                    # Chance proporcional ao tempo do dia restante
                    # Janela útil = ~15h (7h-22h). Por tick ~10min, são ~90 ticks.
                    # Pra distribuir N likes em 90 ticks de forma uniforme, chance = N/90
                    remaining = limit - done
                    chance = remaining / 90.0
                    if _r.random() < chance:
                        # Cria job: o worker faz 1-3 likes nessa execução
                        per_run = _r.randint(1, min(3, remaining))
                        rj = rjob_manager.create({
                            "operation": "auto_like_own",
                            "params": {"max_likes": per_run},
                            "account_username": acc["username"],
                            "account_password": acc["password"],
                            "account_totp_secret": acc.get("totp_secret"),
                            "account_proxy": acc.get("proxy"),
                            "created_by": "automation:like",
                        })
                        acc["auto_like_today_count"] = done + per_run
                        changed = True
                        print(f"[automation] auto_like_own @{acc['username']} +{per_run} (total dia: {acc['auto_like_today_count']}/{limit})")

            # ------ AUTO FOLLOW BACK ------
            if acc.get("auto_follow_back_enabled"):
                limit_f = int(acc.get("auto_follow_back_max_per_day", 0))
                done_f = int(acc.get("auto_follow_back_today_count", 0))
                if done_f < limit_f:
                    remaining_f = limit_f - done_f
                    chance_f = remaining_f / 90.0
                    if _r.random() < chance_f:
                        per_run = _r.randint(1, min(2, remaining_f))
                        rj = rjob_manager.create({
                            "operation": "auto_follow_back",
                            "params": {
                                "max_follows": per_run,
                                "seen_followers": acc.get("auto_follow_back_seen_followers", []),
                            },
                            "account_username": acc["username"],
                            "account_password": acc["password"],
                            "account_totp_secret": acc.get("totp_secret"),
                            "account_proxy": acc.get("proxy"),
                            "created_by": "automation:follow_back",
                        })
                        # Reserva os slots (worker vai atualizar count real depois)
                        acc["auto_follow_back_today_count"] = done_f + per_run
                        changed = True
                        print(f"[automation] auto_follow_back @{acc['username']} +{per_run} (total dia: {acc['auto_follow_back_today_count']}/{limit_f})")

        # Salva accounts.json se mudou
        if changed:
            try:
                with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                    _json.dump(accounts, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[automation] erro salvando accounts: {e}")

    # ===================== SYNC / BACKFILL =====================

    def _sync_loop(self):
        """Loop que vê contas com sync_enabled e cria 1 remote_job de backfill
        pra próxima mídia do pool central que essa conta ainda não postou,
        respeitando sync_interval_hours por conta. Auto-desliga quando termina."""
        import time as _t
        _t.sleep(90)  # espera app subir
        while not self._stop_event.is_set():
            try:
                self._sync_tick()
            except Exception as e:
                print(f"[sync] tick falhou: {e}")
            # Tick a cada 10 min — granularidade fina o bastante pra intervalos curtos
            self._stop_event.wait(10 * 60)

    def _sync_tick(self):
        import json as _json, os as _os
        from datetime import datetime as _dt
        from core.paths import ACCOUNTS_FILE, PENDING_DIR, POSTED_DIR
        from core.poster import load_meta, detect_media_kind, load_caption
        from web.remote_jobs import manager as rjob_manager
        from web.shortener import manager as link_manager

        # Janela de atividade 07:00 - 22:00 (mesmo da automation_loop)
        hour = _dt.now().hour
        if hour < 7 or hour >= 22:
            return

        try:
            with open(ACCOUNTS_FILE, encoding="utf-8") as f:
                accounts = _json.load(f)
        except Exception:
            return

        # Constrói o pool central (união das posted_media de todas as contas)
        pool: dict[str, dict] = {}
        for a in accounts:
            for item in (a.get("posted_media") or []):
                name = item.get("name")
                if not name:
                    continue
                posted_at = item.get("posted_at") or ""
                if name not in pool:
                    pool[name] = {
                        "name": name,
                        "kind": item.get("kind", "reel"),
                        "first_posted_at": posted_at,
                    }
                elif posted_at and (not pool[name]["first_posted_at"] or posted_at < pool[name]["first_posted_at"]):
                    pool[name]["first_posted_at"] = posted_at

        pool_sorted = sorted(pool.values(), key=lambda x: x["first_posted_at"] or "")
        if not pool_sorted:
            return  # nada postado no sistema ainda

        now = now_local()
        base = _os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or "http://127.0.0.1:8000"
        changed = False

        for acc in accounts:
            if not acc.get("active", True):
                continue
            if not acc.get("sync_enabled"):
                continue
            if not acc.get("connected_via_worker_id"):
                continue

            already = {p.get("name") for p in (acc.get("posted_media") or [])}
            pending_pool = [m for m in pool_sorted if m["name"] not in already]
            # Filtra só mídias que ainda existem em disco
            pending_pool = [
                m for m in pending_pool
                if (PENDING_DIR / m["name"]).exists() or (POSTED_DIR / m["name"]).exists()
            ]

            if not pending_pool:
                # Conta alcançou o feed — auto-desliga
                if not acc.get("sync_completed"):
                    acc["sync_completed"] = True
                    acc["sync_enabled"] = False
                    changed = True
                    print(f"[sync] @{acc['username']} alcançou feed central — sync desligado")
                continue

            # Já tem job de sync pendente/rodando pra essa conta? Pula.
            has_active = any(
                j.account_username == acc["username"]
                and j.operation == "post"
                and (j.created_by or "").startswith("sync:")
                and j.status in ("pending", "claimed", "running")
                for j in rjob_manager._items.values()
            )
            if has_active:
                continue

            # Respeita intervalo
            interval_h = int(acc.get("sync_interval_hours", 8))
            last_iso = acc.get("sync_last_post_at")
            if last_iso:
                try:
                    last_dt = datetime.fromisoformat(last_iso)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.astimezone()
                    elapsed_h = (now - last_dt).total_seconds() / 3600.0
                    if elapsed_h < interval_h:
                        continue
                except Exception:
                    pass

            # Pega a PRÓXIMA mídia da fila (cronológica)
            next_media = pending_pool[0]
            media_name = next_media["name"]
            # Pega o arquivo onde estiver (pending OU posted)
            media_path = PENDING_DIR / media_name
            if not media_path.exists():
                media_path = POSTED_DIR / media_name
            if not media_path.exists():
                continue

            try:
                meta = load_meta(str(media_path))
                media_type = detect_media_kind(str(media_path))
                caption = load_caption(str(media_path))
            except Exception as e:
                print(f"[sync] erro lendo meta de {media_name}: {e}")
                continue

            media_url = f"{base}/api/worker/media/{media_name}"
            link_url = meta.get("link_url")
            link_text = meta.get("link_text") or "Clique aqui"

            # Story+link: encurta por conta (anti-cluster)
            if meta.get("kind") == "story" and link_url:
                try:
                    short = link_manager.create(
                        target_url=link_url,
                        label=f"sync · @{acc['username']}",
                        account=acc["username"],
                        created_by="sync",
                    )
                    link_url = f"{base}/r/{short.slug}"
                except Exception as e:
                    print(f"[sync] shortener falhou pra {acc['username']}: {e}")

            highlight_title = None
            if meta.get("kind") == "story" and acc.get("auto_highlight_enabled") and acc.get("auto_highlight_title"):
                highlight_title = acc["auto_highlight_title"]

            rjob_manager.create({
                "operation": "post",
                "params": {"auto_highlight_title": highlight_title} if highlight_title else {},
                "account_username": acc["username"],
                "account_password": acc["password"],
                "account_totp_secret": acc.get("totp_secret"),
                "account_proxy": acc.get("proxy"),
                "video_name": media_name,
                "media_type": media_type,
                "kind": meta.get("kind", "reel"),
                "caption": caption,
                "link_url": link_url,
                "link_text": link_text,
                "media_url": media_url,
                "created_by": f"sync:{acc['username']}",
            })
            # Marca tentativa (mesmo que falhe, evita spamar; sync_last_post_at real
            # vai ser sobrescrito quando worker reportar success)
            acc["sync_last_post_at"] = now.isoformat(timespec="seconds")
            changed = True
            print(f"[sync] @{acc['username']} backfill -> {media_name} ({len(already)+1}/{len(pool_sorted)})")

        if changed:
            try:
                with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                    _json.dump(accounts, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[sync] erro salvando accounts: {e}")

    def _dispatch_via_server(self, s: Schedule):
        """Caminho LEGACY — dispara post.py local na VPS. Usa IP da VPS (vai falhar)."""
        args = ["post.py", "--video", s.video]
        if s.account:
            args += ["--conta", s.account]
        job = self._jobs.start(
            kind="scheduled",
            args=args,
            label=f"agendado · server · {s.video}",
        )
        s.status = "running"
        s.job_id = job.id
        print(f"[scheduler] dispatched {s.id} via server -> job {job.id}")


# Instância singleton (similar ao job_manager)
manager: Optional[ScheduleManager] = None


def init(job_manager) -> ScheduleManager:
    """Cria + inicia o manager. Idempotente."""
    global manager
    if manager is None:
        manager = ScheduleManager(job_manager)
        manager.start()
    return manager
