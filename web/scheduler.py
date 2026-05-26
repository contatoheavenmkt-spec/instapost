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


def is_worker_paused(acc: dict) -> bool:
    """True se acc.worker_paused_until ainda não expirou. Use pra pular
    contas onde o user tá mexendo manualmente no browser (evita 2 devices
    logando ao mesmo tempo, que dispara challenge no Instagram)."""
    iso = acc.get("worker_paused_until")
    if not iso:
        return False
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt > now_local()
    except Exception:
        return False


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
        # Workspace do schedule — usado pra isolamento na UI. Vazio = "default".
        self.workspace_slug: str = data.get("workspace_slug") or "default"
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
            "workspace_slug": self.workspace_slug,
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

    def list(self, workspace_slug: Optional[str] = None) -> list[dict]:
        """Lista schedules. Se workspace_slug for fornecido, filtra apenas dele.
        None = retorna todos (admin/internal)."""
        with self._lock:
            items = sorted(self._items, key=lambda s: s.scheduled_at)
            if workspace_slug:
                items = [s for s in items if s.workspace_slug == workspace_slug]
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
        # Lock obrigatório: o _loop pode mutar _items enquanto iteramos.
        # Sem isso, RuntimeError "list changed during iteration" eventualmente.
        with self._lock:
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

    def create(self, video: str, account: Optional[str], scheduled_at: datetime, created_by: Optional[str] = None, workspace_slug: Optional[str] = None) -> Schedule:
        # Auto-popular workspace do contexto se não veio explícito
        if not workspace_slug:
            try:
                from core.paths import get_workspace
                workspace_slug = get_workspace()
            except Exception:
                workspace_slug = "default"
        sched = Schedule({
            "id": uuid.uuid4().hex[:12],
            "video": video,
            "account": account,
            "scheduled_at": scheduled_at.isoformat(timespec="seconds"),
            "status": "pending",
            "created_at": now_local().isoformat(timespec="seconds"),
            "created_by": created_by,
            "workspace_slug": workspace_slug,
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
        # Thread separada pro auto-loop de disparo diversificado (por workspace)
        self._diversify_thread = threading.Thread(
            target=self._diversify_loop, daemon=True, name="diversify_scheduler"
        )
        self._diversify_thread.start()
        # Thread separada pro health tracker (shadow ban detector passivo)
        self._health_thread = threading.Thread(
            target=self._health_loop, daemon=True, name="health_scheduler"
        )
        self._health_thread.start()
        # Thread separada pra limpar jobs zumbis (claimed/running travados)
        self._zombie_thread = threading.Thread(
            target=self._zombie_loop, daemon=True, name="zombie_cleanup"
        )
        self._zombie_thread.start()
        print(f"[scheduler] started (tick {TICK_SECONDS}s) + automations + sync + diversify + health + zombie loops")

    def stop(self):
        self._stop_event.set()

    def _loop(self):
        # Primeira passada rápida pra processar pendentes antigos
        time.sleep(2)
        # Auto-rescue: na partida do servidor, reagenda missed + pending atrasados
        # mantendo intervalo entre eles, começando a partir de agora+5min.
        try:
            self._rescue_late_schedules()
        except Exception as e:
            print(f"[scheduler] rescue falhou: {e}")
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                print(f"[scheduler] tick falhou: {e}")
            # Espera, mas acorda se stop foi pedido
            self._stop_event.wait(TICK_SECONDS)

    def _rescue_late_schedules(self, buffer_minutes: int = 5):
        """Reagenda schedules 'missed' e 'pending no passado' mantendo o intervalo
        original entre eles, começando em now + buffer_minutes.

        Roda 1x no startup do scheduler (servidor caiu/voltou).
        """
        now = now_local()
        with self._lock:
            late = []
            for s in self._items:
                if s.status == "missed":
                    late.append(s)
                elif s.status == "pending":
                    try:
                        if s.scheduled_dt < now:
                            late.append(s)
                    except Exception:
                        continue
            if not late:
                return

            late.sort(key=lambda s: s.scheduled_at)

            # Calcula deltas relativos
            origins = [s.scheduled_dt for s in late]
            deltas = [0.0]
            for i in range(1, len(origins)):
                deltas.append((origins[i] - origins[i-1]).total_seconds())

            base = now + timedelta(minutes=buffer_minutes)
            offset = 0.0
            for i, s in enumerate(late):
                offset += deltas[i]
                new_dt = base + timedelta(seconds=offset)
                old_iso = s.scheduled_at
                s.scheduled_at = new_dt.isoformat(timespec="seconds")
                s.status = "pending"
                s.note = f"auto-reagendado (era {old_iso[:16]})"
                s.remote_job_ids = []
                s.job_id = None

            self._save()
            print(f"[scheduler] rescue: {len(late)} schedules reagendados a partir de {base.isoformat(timespec='seconds')}")

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
        from core import paths as _paths
        from core.paths import PENDING_DIR, ACCOUNTS_FILE
        from core.poster import load_meta, detect_media_kind, load_caption
        from web.shortener import manager as link_manager
        import os as _os, json as _json

        # Setta contexto do workspace ANTES de qualquer leitura/criação — assim
        # ACCOUNTS_FILE, PENDING_DIR, e create() do rjob herdam o ws certo.
        _paths.set_workspace(s.workspace_slug or "default")

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

        created_ids = []
        for acc in targets:
            # media_url COM ?account= pra disparar variante anti-cluster
            media_url = f"{base}/api/worker/media/{s.video}?account={acc['username']}"
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
                "workspace_slug": s.workspace_slug or "default",
                "params": {"auto_highlight_title": highlight_title} if highlight_title else {},
                "account_username": acc["username"],
                "account_password": acc["password"],
                "account_totp_secret": acc.get("totp_secret"),
                "account_proxy": acc.get("proxy"),
                "video_name": s.video,
                "media_type": media_type,
                "kind": "story" if media_type == "photo" else (meta.get("kind") or "reel"),  # FIX: foto SEMPRE story (Insta nao aceita foto como reel)
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
        """Pra cada workspace, pra cada conta com automação ligada e que ainda
        não bateu o limite do dia, com chance aleatória, cria 1 remote_job.
        Só age durante 'janela ativa' (7h-22h horário do servidor) pra simular humano."""
        from datetime import datetime as _dt
        from core import paths as _paths

        # Janela de atividade 07:00 - 22:00
        hour = _dt.now().hour
        if hour < 7 or hour >= 22:
            return

        # Itera por todos os workspaces — cada um tem seu próprio accounts.json
        for slug in _paths.list_workspace_slugs():
            try:
                self._automation_tick_for_workspace(slug)
            except Exception as e:
                print(f"[automation] erro no ws '{slug}': {e}")

    def _automation_tick_for_workspace(self, slug: str):
        """Roda 1 rodada do automation tick em 1 workspace específico."""
        import json as _json, random as _r
        from datetime import datetime as _dt
        from core import paths as _paths

        # CRÍTICO: setta contexto antes de ACCOUNTS_FILE/create() pegarem o ws
        _paths.set_workspace(slug)
        from core.paths import ACCOUNTS_FILE

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
            if is_worker_paused(acc):
                continue  # user tá mexendo no browser, evita conflito

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
                            "workspace_slug": slug,
                            "params": {"max_likes": per_run},
                            "account_username": acc["username"],
                            "account_password": acc["password"],
                            "account_totp_secret": acc.get("totp_secret"),
                            "account_proxy": acc.get("proxy"),
                            "created_by": "automation:like",
                        })
                        acc["auto_like_today_count"] = done + per_run
                        changed = True
                        print(f"[automation] auto_like_own @{acc['username']} +{per_run} (ws={slug}, total dia: {acc['auto_like_today_count']}/{limit})")

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
                            "workspace_slug": slug,
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
                        print(f"[automation] auto_follow_back @{acc['username']} +{per_run} (ws={slug}, total dia: {acc['auto_follow_back_today_count']}/{limit_f})")

        # Salva accounts.json se mudou
        if changed:
            try:
                with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                    _json.dump(accounts, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[automation] erro salvando accounts (ws={slug}): {e}")

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
        """SYNC MODE = 'conta nova começa do zero da biblioteca, isoladamente'.

        Diferente do auto-loop diversificado (que distribui vídeos DIFERENTES entre
        várias contas), o sync pega a biblioteca pending/ INTEIRA e posta na ordem
        cronológica em UMA conta específica (a que tem sync_enabled), respeitando
        seu próprio sync_interval_hours.

        Contas com sync_enabled são EXCLUÍDAS do auto-loop diversificado (no
        _do_dispatch_diversified). Não acontecem 2 fluxos simultâneos na mesma conta.

        Auto-desliga quando a conta postou TODO o pool de pending.
        """
        import json as _json, os as _os
        from datetime import datetime as _dt
        from core import paths as _paths
        from core.poster import load_meta, detect_media_kind, load_caption
        from web.remote_jobs import manager as rjob_manager
        from web.shortener import manager as link_manager
        from web.workers import manager as worker_manager

        # Janela de atividade 07:00 - 22:00
        hour = _dt.now().hour
        if hour < 7 or hour >= 22:
            return

        # Mesma guarda do auto-loop: se NÃO tem worker online, pausa o sync
        if not worker_manager.online_workers():
            return

        # Itera por todos os workspaces
        for slug in _paths.list_workspace_slugs():
            try:
                self._sync_tick_for_workspace(slug)
            except Exception as e:
                print(f"[sync] erro no ws '{slug}': {e}")

    def _sync_tick_for_workspace(self, slug: str):
        """Sync tick pra 1 workspace específico."""
        import json as _json, os as _os
        from core import paths as _paths
        from core.poster import load_meta, detect_media_kind, load_caption
        from web.remote_jobs import manager as rjob_manager
        from web.shortener import manager as link_manager

        _paths.set_workspace(slug)
        accounts_file = _paths.accounts_file(slug)
        pending_dir = _paths.pending_dir(slug)
        posted_dir = _paths.posted_dir(slug)

        if not accounts_file.exists():
            return
        try:
            accounts = _json.loads(accounts_file.read_text(encoding="utf-8"))
        except Exception:
            return

        # Pool = vídeos em pending/ ordenados cronologicamente (mais antigo primeiro)
        MEDIA_EXTS = {".mp4", ".jpg", ".jpeg", ".png", ".webp"}
        pool_items = []
        for p in pending_dir.iterdir():
            if not p.is_file() or p.suffix.lower() not in MEDIA_EXTS:
                continue
            if p.name.endswith(".meta.json"):
                continue
            if p.suffix.lower() in (".jpg", ".jpeg"):
                if p.stem.lower().endswith(".mp4") or p.with_suffix(".mp4").exists():
                    continue
            pool_items.append((p.name, p.stat().st_mtime))
        pool_items.sort(key=lambda x: x[1])
        pool_sorted = [{"name": n, "mtime": t} for n, t in pool_items]

        if not pool_sorted:
            return  # biblioteca vazia

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
            if is_worker_paused(acc):
                continue  # user tá mexendo no browser

            already = {p.get("name") for p in (acc.get("posted_media") or [])}
            pending_pool = [m for m in pool_sorted if m["name"] not in already]

            if not pending_pool:
                # Conta postou TODA a biblioteca — auto-desliga
                if not acc.get("sync_completed"):
                    acc["sync_completed"] = True
                    acc["sync_enabled"] = False
                    changed = True
                    print(f"[sync] @{acc['username']} completou biblioteca ({len(already)} vídeos) — sync desligado")
                continue

            # Já tem job de sync pendente/rodando pra essa conta? Pula.
            # Filtra por ws pra não cruzar contas iguais entre workspaces.
            has_active = any(
                j.account_username == acc["username"]
                and j.operation == "post"
                and (j.created_by or "").startswith("sync:")
                and j.status in ("pending", "claimed", "running")
                and j.workspace_slug == slug
                for j in rjob_manager.snapshot_values()
            )
            if has_active:
                continue

            # Respeita intervalo (com tolerância 1min)
            interval_h = float(acc.get("sync_interval_hours", 8))
            last_iso = acc.get("sync_last_post_at")
            if last_iso:
                try:
                    last_dt = datetime.fromisoformat(last_iso)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.astimezone()
                    elapsed_h = (now - last_dt).total_seconds() / 3600.0
                    if elapsed_h < interval_h - (1.0 / 60.0):
                        continue
                except Exception:
                    pass

            # Pega a PRÓXIMA mídia da fila (cronológica = mais antiga primeiro)
            next_media = pending_pool[0]
            media_name = next_media["name"]
            media_path = pending_dir / media_name
            if not media_path.exists():
                media_path = posted_dir / media_name
            if not media_path.exists():
                continue

            try:
                meta = load_meta(str(media_path))
                media_type = detect_media_kind(str(media_path))
                caption = load_caption(str(media_path))
            except Exception as e:
                print(f"[sync] erro lendo meta de {media_name}: {e}")
                continue

            # media_url COM ?account= pra disparar variante anti-cluster
            media_url = f"{base}/api/worker/media/{media_name}?account={acc['username']}"
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
                "workspace_slug": slug,
                "params": {"auto_highlight_title": highlight_title} if highlight_title else {},
                "account_username": acc["username"],
                "account_password": acc["password"],
                "account_totp_secret": acc.get("totp_secret"),
                "account_proxy": acc.get("proxy"),
                "video_name": media_name,
                "media_type": media_type,
                "kind": "story" if media_type == "photo" else (meta.get("kind") or "reel"),  # FIX: foto SEMPRE story (Insta nao aceita foto como reel)
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
                accounts_file.write_text(
                    _json.dumps(accounts, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                print(f"[sync] erro salvando accounts: {e}")

    # ===================== AUTO-LOOP DE DISPARO DIVERSIFICADO =====================

    def _diversify_loop(self):
        """Itera por todos os workspaces que têm auto-loop ligado. Em cada,
        cria N jobs diversificados respeitando interval_hours.
        Tick a cada 5min (era 15min), janela 7h-22h."""
        import time as _t
        _t.sleep(30)  # 30s de espera no startup (antes era 120s)
        while not self._stop_event.is_set():
            try:
                self._diversify_tick()
            except Exception as e:
                print(f"[diversify] tick falhou: {e}")
            # 5min tick — mais responsivo (era 15min)
            self._stop_event.wait(5 * 60)

    def _diversify_tick(self):
        from datetime import datetime as _dt
        from core import paths as _paths
        from web.workers import manager as worker_manager

        # GUARDA CRÍTICA: se NÃO tem worker online, NÃO cria jobs novos
        # (anti-inchar fila quando worker caiu de madrugada).
        # Worker quando volta, vai pegar os pending antigos + auto-loop volta a criar novos.
        online_workers = worker_manager.online_workers()
        if not online_workers:
            # Log a cada ~30min pra dar visibilidade
            if _dt.now().minute % 30 < 5:
                print(f"[diversify] PAUSADO: nenhum worker online (auto-loop em standby até worker reconectar)")
            return

        # Itera por todos os workspaces existentes em disco
        # Janela de horário agora é POR WORKSPACE (lida em _diversify_tick_for_workspace)
        for slug in _paths.list_workspace_slugs():
            try:
                self._diversify_tick_for_workspace(slug)
            except Exception as e:
                print(f"[diversify] erro no ws '{slug}': {e}")

    def _diversify_tick_for_workspace(self, slug: str):
        """Roda 1 rodada de dispatch-diversified pra esse workspace, se devido."""
        from datetime import datetime as _dt
        from core import paths as _paths
        from web import diversify as _diversify

        # Setta workspace ativo no contextvar (thread-local!) pra que
        # paths.ACCOUNTS_FILE etc apontem pra esse ws
        _paths.set_workspace(slug)

        settings = _diversify.load(slug)
        if not settings.get("enabled"):
            return

        # Janela de horário por workspace (default 6h-24h, antes era hardcoded 7-22)
        # Bug anterior: 7-22 bloqueava hour=22 (22 >= 22 True), então qualquer
        # próximo run agendado entre 22:00-22:59 nunca disparava.
        win_start = int(settings.get("window_start_hour", 6))
        win_end = int(settings.get("window_end_hour", 24))
        hour = _dt.now().hour
        # Janela é [start, end) — end EXCLUSIVO. 24 = inclui 23h.
        in_window = (win_start <= hour < win_end) if win_end <= 24 else (hour >= win_start or hour < (win_end - 24))
        if not in_window:
            if _dt.now().minute < 5:
                print(f"[diversify] ws='{slug}' fora da janela {win_start}h-{win_end}h (atual: {hour}h), aguardando")
            return

        interval_h = float(settings.get("interval_hours", 6))
        last_iso = settings.get("last_run_at")
        now = now_local()
        if last_iso:
            try:
                last = _dt.fromisoformat(last_iso)
                if last.tzinfo is None:
                    last = last.astimezone()
                elapsed_h = (now - last).total_seconds() / 3600.0
                # Tolerância de 1min pra evitar perder rodada por drift de relógio
                if elapsed_h < interval_h - (1.0 / 60.0):
                    # Log a cada ~30min pra dar visibilidade
                    if int(elapsed_h * 60) % 30 == 0:
                        print(f"[diversify] ws='{slug}' aguardando: {elapsed_h:.2f}h/{interval_h}h ({int((interval_h - elapsed_h) * 60)}min restantes)")
                    return
                print(f"[diversify] ws='{slug}' ATINGIU intervalo: {elapsed_h:.2f}h >= {interval_h}h — disparando")
            except Exception as e:
                print(f"[diversify] erro parseando last_run_at: {e} — vai rodar mesmo assim")
        else:
            print(f"[diversify] ws='{slug}' primeira execução (sem last_run_at)")

        # Roda 1 rodada: replica logica do api_dispatch_diversified
        max_per_acc = int(settings.get("max_per_account", 1))
        kind_filter = settings.get("kind_filter", "all")
        reps_per_video = int(settings.get("repetitions_per_video", 3))
        new_thresh_h = float(settings.get("new_account_threshold_hours", 24))
        new_interval_h = float(settings.get("new_account_interval_hours", 6))
        result = self._do_dispatch_diversified(
            slug,
            max_per_account=max_per_acc,
            kind_filter=kind_filter,
            reps_per_video=reps_per_video,
            interval_hours=float(settings.get("interval_hours", 1)),
            new_account_threshold_hours=new_thresh_h,
            new_account_interval_hours=new_interval_h,
        )

        if result.get("all_completed"):
            print(f"[diversify] ws='{slug}' completou TODOS os videos. Auto-desligando.")
            _diversify.mark_completed(slug)
        elif result.get("count", 0) > 0:
            print(f"[diversify] ws='{slug}' criou {result['count']} job(s) (pool={result['pool_size']}, contas={result['accounts_count']})")
            _diversify.mark_run(slug)
        else:
            # nada criado mas nao completou — pula sem marcar
            print(f"[diversify] ws='{slug}' nada criado (sem contas ou pool vazio)")

    def _do_dispatch_diversified(
        self,
        slug: str,
        max_per_account: int = 1,
        kind_filter: str = "all",
        reps_per_video: int = 1,
        interval_hours: float = 1.0,
        new_account_threshold_hours: float = 24.0,
        new_account_interval_hours: float = 6.0,
    ) -> dict:
        """Logica core do disparo diversificado, sem HTTP. Usado pelo auto-loop.

        Args:
            kind_filter: 'all' | 'reel' | 'story'
            reps_per_video: quantas vezes o mesmo video é postado por conta antes
                de avançar pro próximo (descoberta empírica: 3x viraliza)
            interval_hours: ritmo veterana (entre rodadas da mesma conta)
            new_account_threshold_hours: contas com 1ª atividade < N horas atrás
                são tratadas como "novas" (modo aquecimento)
            new_account_interval_hours: ritmo conta nova (geralmente mais espaçado)
        """
        import os as _os
        import json as _json
        from core import paths as _paths
        from core.poster import load_meta, detect_media_kind, load_caption
        from web.remote_jobs import manager as rjob_manager
        from web.shortener import manager as link_manager

        # Lê accounts do workspace
        try:
            accounts_file = _paths.accounts_file(slug)
            if not accounts_file.exists():
                return {"count": 0, "all_completed": False, "pool_size": 0, "accounts_count": 0}
            accounts = _json.loads(accounts_file.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[diversify] erro lendo accounts ws='{slug}': {e}")
            return {"count": 0, "all_completed": False, "pool_size": 0, "accounts_count": 0}

        # Contas elegíveis ao auto-loop diversificado:
        # - active=true
        # - connected_via_worker_id (logada no worker)
        # - sync_enabled=false (contas em sync rodam isoladas no _sync_loop)
        targets = [
            a for a in accounts
            if a.get("active", True)
            and a.get("connected_via_worker_id")
            and not a.get("sync_enabled")
        ]
        if not targets:
            return {"count": 0, "all_completed": False, "pool_size": 0, "accounts_count": 0}

        # Lista pending videos cronologicamente
        pending_dir = _paths.pending_dir(slug)
        if not pending_dir.exists():
            return {"count": 0, "all_completed": False, "pool_size": 0, "accounts_count": len(targets)}

        MEDIA_EXTS = {".mp4", ".jpg", ".jpeg", ".png", ".webp"}
        pool_items = []
        for p in pending_dir.iterdir():
            if not p.is_file() or p.suffix.lower() not in MEDIA_EXTS:
                continue
            if p.name.endswith(".meta.json"):
                continue
            # Pula thumbs (jpg sibling de mp4)
            if p.suffix.lower() in (".jpg", ".jpeg"):
                if p.stem.lower().endswith(".mp4") or p.with_suffix(".mp4").exists():
                    continue
            # Filtro de kind (lê meta pra saber se é reel/story)
            if kind_filter and kind_filter != "all":
                try:
                    item_meta = load_meta(str(p))
                    item_kind = item_meta.get("kind") or ("story" if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") else "reel")
                    if item_kind != kind_filter:
                        continue
                except Exception:
                    pass
            pool_items.append((p.name, p.stat().st_mtime))
        pool_items.sort(key=lambda x: x[1])
        pool = [n for n, _ in pool_items]

        if not pool:
            return {"count": 0, "all_completed": False, "pool_size": 0, "accounts_count": len(targets)}

        # Distribui (mesmo algoritmo da função HTTP)
        used_in_round: set[str] = set()
        assignments: list[tuple[dict, str]] = []
        accounts_completed: list[str] = []

        # ANTI-DUPLICATE: pra cada conta, considera "já pego" tudo que está em
        # pending/claimed/running ATUALMENTE (jobs de rodadas anteriores ainda
        # não processados). Antes, o sistema só olhava posted_media e criava
        # múltiplos jobs do mesmo vídeo enquanto worker não terminava o 1º.
        # Filtra por ws pra isolamento.
        pending_per_acc: dict[str, set[str]] = {}
        for j in rjob_manager.snapshot_values():
            if (
                j.operation == "post"
                and j.status in ("pending", "claimed", "running")
                and j.video_name
                and j.workspace_slug == slug
            ):
                pending_per_acc.setdefault(j.account_username, set()).add(j.video_name)

        # ROTAÇÃO REAL: cada conta começa numa posição DIFERENTE do pool, baseado
        # em quantos vídeos ela já postou (acumulado) + sua posição na lista.
        # Resultado: rodada 1 conta_A pega v1, conta_B pega v2, conta_C pega v3.
        # Rodada 2 (após posted_media incrementar): conta_A pega v2, conta_B pega v3, etc.
        # Pool é tratado como LISTA CIRCULAR.
        targets_sorted = sorted(targets, key=lambda a: a.get("username", ""))
        pool_len = len(pool)

        from datetime import datetime as _dt_warm, timezone as _tz_warm
        _now_warm = _dt_warm.now(_tz_warm.utc)

        for idx, acc in enumerate(targets_sorted):
            # PAUSA MANUAL: user tá mexendo no browser, evita 2 devices ao mesmo tempo
            if is_worker_paused(acc):
                print(f"[diversify] @{acc['username']} PAUSADA pelo user — skip")
                continue
            posted_count = len(acc.get("posted_media") or [])
            already = {p.get("name") for p in (acc.get("posted_media") or []) if p.get("name")}
            in_flight = pending_per_acc.get(acc["username"], set())
            forbidden = already | in_flight  # vídeos que essa conta NÃO pode receber

            # ===== MODO AQUECIMENTO (conta nova) =====
            # Conta sem posted_media OU com 1ª atividade < N horas é tratada como nova.
            # Default: máximo 1 post / 24h pra ela. Resto da config é ignorado.
            # Override: se acc.skip_warmup=True, pula direto pro modo veterana.
            is_new_acc = False
            posted_media = acc.get("posted_media") or []
            skip_warm = bool(acc.get("skip_warmup", False))
            if not skip_warm:
                try:
                    first_post_iso = None
                    if posted_media:
                        first_post_iso = min(p.get("posted_at", "") for p in posted_media if p.get("posted_at"))
                    if not first_post_iso:
                        is_new_acc = True  # sem histórico = MUITO nova
                    else:
                        first_dt = _dt_warm.fromisoformat(first_post_iso)
                        if first_dt.tzinfo is None:
                            first_dt = first_dt.astimezone()
                        hours_active = (_now_warm.astimezone(first_dt.tzinfo) - first_dt).total_seconds() / 3600.0
                        if hours_active < new_account_threshold_hours:
                            is_new_acc = True
                except Exception:
                    pass

            # Aplica restrições de aquecimento
            effective_max_per_acc = max_per_account
            if is_new_acc:
                effective_max_per_acc = 1  # conta nova só ganha 1 post por rodada
                # Checa última atividade — se postou nas últimas N horas, PULA essa rodada
                if posted_media:
                    try:
                        last_post_iso = max(p.get("posted_at", "") for p in posted_media if p.get("posted_at"))
                        last_dt = _dt_warm.fromisoformat(last_post_iso)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.astimezone()
                        hours_since = (_now_warm.astimezone(last_dt.tzinfo) - last_dt).total_seconds() / 3600.0
                        if hours_since < new_account_interval_hours - 0.05:
                            # Conta nova ainda em cooldown da última postagem — pula
                            print(f"[diversify] @{acc['username']} em AQUECIMENTO ({hours_since:.1f}h/{new_account_interval_hours}h desde último post) — skip")
                            continue
                    except Exception:
                        pass
                print(f"[diversify] @{acc['username']} em AQUECIMENTO (conta < {new_account_threshold_hours}h) — limitado a 1 post nessa rodada")

            # Offset rotacional inicial (acc-específico)
            start_offset = (posted_count + idx) % max(1, pool_len)

            for slot in range(max(1, min(20, effective_max_per_acc))):
                # Procura no pool a partir do offset, em ordem circular
                chosen = None
                for step in range(pool_len):
                    candidate = pool[(start_offset + step) % pool_len]
                    if candidate in forbidden:
                        continue
                    # Anti cross-account: preferir candidato não usado nesta rodada,
                    # mas se TODOS os disponíveis pra essa conta já foram usados, recicla
                    if candidate not in used_in_round:
                        chosen = candidate
                        break
                # Se nada novo foi encontrado, tenta de novo aceitando reciclar
                if chosen is None:
                    for step in range(pool_len):
                        candidate = pool[(start_offset + step) % pool_len]
                        if candidate not in forbidden:
                            chosen = candidate
                            break

                if chosen is None:
                    # Conta já tem tudo que o pool oferece (em forbidden) — completed
                    if acc["username"] not in accounts_completed:
                        accounts_completed.append(acc["username"])
                    break

                used_in_round.add(chosen)
                forbidden.add(chosen)
                assignments.append((acc, chosen))
                # Avança o offset pra próximo slot da MESMA conta pegar vídeo diferente
                start_offset = (start_offset + 1) % max(1, pool_len)

        if not assignments:
            return {
                "count": 0, "all_completed": True,
                "pool_size": len(pool), "accounts_count": len(targets),
            }

        base = _os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or "http://127.0.0.1:8000"
        # Stagger anti-flag: distribui jobs com distribuição exponencial.
        # Antes: linear (i * 60s) + jitter uniforme — Insta detecta o padrão regular.
        # Agora: exponencial (humanlike_delay) — long-tail mimics real users.
        from core.retry import humanlike_delay
        from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
        _now_utc = _dt2.now(_tz2.utc)
        stagger_times = []
        cumulative_s = 0
        for i in range(len(assignments)):
            # 1º job: 5s. Próximos: incremento exponencial (mean 60s, max 180s).
            if i == 0:
                cumulative_s = 5
            else:
                cumulative_s += humanlike_delay(min_s=20, mean_s=60, max_s=180)
            stagger_times.append((_now_utc + _td2(seconds=cumulative_s)).isoformat(timespec="seconds"))
        stagger_times.sort()

        created = []
        for idx_assign, (acc, video_name) in enumerate(assignments):
            media_path = pending_dir / video_name
            try:
                meta = load_meta(str(media_path))
                media_type = detect_media_kind(str(media_path))
                caption = load_caption(str(media_path))
            except Exception as e:
                print(f"[diversify] erro lendo meta de {video_name}: {e}")
                continue

            link_url = meta.get("link_url")
            link_text = meta.get("link_text") or "Clique aqui"
            if meta.get("kind") == "story" and link_url:
                try:
                    short = link_manager.create(
                        target_url=link_url,
                        label=f"diversify-auto · @{acc['username']}",
                        account=acc["username"],
                        created_by=f"diversify-auto:{slug}",
                    )
                    link_url = f"{base}/r/{short.slug}"
                except Exception as e:
                    print(f"[diversify] shortener falhou: {e}")

            highlight_title = None
            if meta.get("kind") == "story" and acc.get("auto_highlight_enabled") and acc.get("auto_highlight_title"):
                highlight_title = acc["auto_highlight_title"]

            media_url = f"{base}/api/worker/media/{video_name}?account={acc['username']}"
            try:
                job = rjob_manager.create({
                    "operation": "post",
                    "workspace_slug": slug,
                    "params": {"auto_highlight_title": highlight_title} if highlight_title else {},
                    "account_username": acc["username"],
                    "account_password": acc["password"],
                    "account_totp_secret": acc.get("totp_secret"),
                    "account_proxy": acc.get("proxy"),
                    "video_name": video_name,
                    "media_type": media_type,
                    "kind": "story" if media_type == "photo" else (meta.get("kind") or "reel"),  # FIX: foto SEMPRE story (Insta nao aceita foto como reel)
                    "caption": caption,
                    "link_url": link_url,
                    "link_text": link_text,
                    "media_url": media_url,
                    "created_by": f"diversify-auto:{slug}",
                    "scheduled_for": stagger_times[idx_assign] if idx_assign < len(stagger_times) else None,
                })
                created.append(job.id)
            except Exception as e:
                print(f"[diversify] erro criando job pra @{acc['username']}/{video_name}: {e}")

        return {
            "count": len(created),
            "all_completed": False,
            "pool_size": len(pool),
            "accounts_count": len(targets),
            "accounts_completed": accounts_completed,
        }

    # ===================== ZOMBIE CLEANUP =====================

    def _zombie_loop(self):
        """Periodicamente libera jobs zumbis (claimed > 2min, running > 10min sem update).

        Por que separado: _expire_stale_claims original só rodava no claim_next.
        Se worker tá offline, claim_next NÃO é chamado e zumbis ficam parados pra
        sempre. Esta thread garante limpeza independente.
        """
        import time as _t
        _t.sleep(60)  # aguarda 1min apos startup
        while not self._stop_event.is_set():
            try:
                from web.remote_jobs import manager as rjob_manager
                freed = rjob_manager.cleanup_zombies()
                if freed > 0:
                    print(f"[zombie] liberou {freed} job(s) zumbi (worker travou ou desconectou)")
            except Exception as e:
                print(f"[zombie] erro: {e}")
            # Tick a cada 2min
            self._stop_event.wait(2 * 60)

    # ===================== HEALTH TRACKER (SHADOW BAN DETECTOR) =====================

    def _health_loop(self):
        """1x/dia (qualquer hora) dispara collect_insights pra cada conta conectada
        em cada workspace. Resultados sao salvos via api_worker_job_result
        side-effect (em main.py)."""
        import time as _t
        _t.sleep(180)  # 3min apos startup
        while not self._stop_event.is_set():
            try:
                self._health_tick()
            except Exception as e:
                print(f"[health] tick falhou: {e}")
            # 1 hora entre verificacoes (so dispara collect 1x/dia por conta)
            self._stop_event.wait(60 * 60)

    def _health_tick(self):
        """Pra cada workspace, pra cada conta conectada, cria 1 job collect_insights
        se nao houve coleta nas ultimas 22h."""
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from core import paths as _paths
        from web import health as _health
        from web.remote_jobs import manager as rjob_manager
        import json as _json

        now = _dt.now(_tz.utc)

        for slug in _paths.list_workspace_slugs():
            _paths.set_workspace(slug)
            accounts_file = _paths.accounts_file(slug)
            if not accounts_file.exists():
                continue
            try:
                accounts = _json.loads(accounts_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            for acc in accounts:
                if not acc.get("active", True):
                    continue
                if not acc.get("connected_via_worker_id"):
                    continue
                if is_worker_paused(acc):
                    continue  # user tá mexendo no browser
                # Ultima coleta foi a menos de 22h? Pula.
                history = _health.load_history(acc["username"], slug)
                if history:
                    try:
                        last = _dt.fromisoformat(history[-1]["collected_at"])
                        if (now - last).total_seconds() < 22 * 3600:
                            continue
                    except Exception:
                        pass
                # Cria job collect_insights
                try:
                    rjob_manager.create({
                        "operation": "collect_insights",
                        "workspace_slug": slug,
                        "account_username": acc["username"],
                        "account_password": acc["password"],
                        "account_totp_secret": acc.get("totp_secret"),
                        "account_proxy": acc.get("proxy"),
                        "created_by": f"health:{slug}",
                    })
                    print(f"[health] criou collect_insights pra @{acc['username']} (ws={slug})")
                except Exception as e:
                    print(f"[health] erro criando job pra {acc['username']}: {e}")

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
