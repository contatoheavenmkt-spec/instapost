"""
Fila de jobs remotos — disparos que workers da equipe executam no PC delas
em vez do servidor.

Cada job é um pacote completo: credenciais da conta, URL da mídia, legenda,
tipo (reel/story), link sticker. Worker pega o próximo, baixa mídia, faz login
Instagram, posta, reporta resultado.

Storage: remote_jobs.json no DATA_DIR.

Estados:
  - pending: aguardando worker pegar
  - claimed: worker pegou (claim em até CLAIM_TIMEOUT_SECONDS senão volta a pending)
  - running: worker tá executando
  - done: postou com sucesso
  - error: deu erro (mensagem em error_msg)
  - cancelled: cancelado pelo usuário antes de ser pego
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from core.paths import data_path

REMOTE_JOBS_FILE = data_path("remote_jobs.json")
MAX_KEPT = 200
CLAIM_TIMEOUT_SECONDS = 300  # 5min: tempo seguro pra worker baixar vídeo grande + começar a postar
MAX_LOG_LINES = 300


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


class RemoteJob:
    def __init__(self, data: dict):
        self.id: str = data["id"]
        # Operação: "post" | "test_login" | "edit_profile" | "change_picture"
        # | "auto_like_own" | "auto_follow_back" | "get_profile_info"
        self.operation: str = data.get("operation", "post")
        # Workspace do job — usado pra ISOLAMENTO de dados na UI. Jobs antigos
        # sem campo viram "default" pra retrocompat. Worker ignora (claim global).
        self.workspace_slug: str = data.get("workspace_slug") or "default"
        # Params específicos por operation (dict livre)
        self.params: dict = data.get("params", {}) or {}
        # Auth da conta (sempre obrigatório)
        self.account_username: str = data["account_username"]
        self.account_password: str = data["account_password"]
        self.account_totp_secret: Optional[str] = data.get("account_totp_secret")
        self.account_proxy: Optional[str] = data.get("account_proxy")
        self.video_name: str = data.get("video_name", "")
        self.media_type: str = data.get("media_type", "video")  # "video" | "photo"
        self.kind: str = data.get("kind", "reel")  # "reel" | "story"
        self.caption: str = data.get("caption", "")
        self.link_url: Optional[str] = data.get("link_url")
        self.link_text: Optional[str] = data.get("link_text", "Clique aqui")
        self.media_url: str = data.get("media_url", "")
        # Estado
        self.status: str = data.get("status", "pending")
        # Stagger anti-flag: worker só pega quando scheduled_for <= now (UTC ISO).
        # None/"" = pode pegar imediatamente.
        self.scheduled_for: Optional[str] = data.get("scheduled_for") or None
        # Etapa fina dentro de claimed/running pro Kanban:
        # queued | downloading | logging | posting | finishing | done
        self.step: str = data.get("step", "queued")
        self.step_at: Optional[str] = data.get("step_at")
        self.worker_id: Optional[str] = data.get("worker_id")
        self.created_at: str = data.get("created_at") or now_iso()
        self.created_by: Optional[str] = data.get("created_by")
        self.claimed_at: Optional[str] = data.get("claimed_at")
        self.finished_at: Optional[str] = data.get("finished_at")
        self.media_id: Optional[str] = data.get("media_id")
        self.error_msg: Optional[str] = data.get("error_msg")
        self.result_data: dict = data.get("result_data") or {}
        self.logs: list[str] = data.get("logs", [])

    def to_dict(self, include_secrets: bool = False, include_logs: bool = True) -> dict:
        d = {
            "id": self.id,
            "operation": self.operation,
            "workspace_slug": self.workspace_slug,
            "params": self.params,
            "account_username": self.account_username,
            "video_name": self.video_name,
            "media_type": self.media_type,
            "kind": self.kind,
            "caption": self.caption,
            "link_url": self.link_url,
            "link_text": self.link_text,
            "media_url": self.media_url,
            "status": self.status,
            "scheduled_for": self.scheduled_for,
            "step": self.step,
            "step_at": self.step_at,
            "worker_id": self.worker_id,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "claimed_at": self.claimed_at,
            "finished_at": self.finished_at,
            "media_id": self.media_id,
            "error_msg": self.error_msg,
            "result_data": self.result_data,
        }
        if include_secrets:
            d["account_password"] = self.account_password
            d["account_totp_secret"] = self.account_totp_secret
            d["account_proxy"] = self.account_proxy
        if include_logs:
            d["logs"] = self.logs
        return d


class RemoteJobManager:
    def __init__(self):
        self._items: dict[str, RemoteJob] = {}
        self._lock = threading.Lock()
        self._load()

    def snapshot_values(self) -> list:
        """Cópia thread-safe dos jobs pra iteração externa sem lock.
        Use em vez de iterar `_items.values()` direto, senão RuntimeError
        quando outra thread muta o dict no meio."""
        with self._lock:
            return list(self._items.values())

    def _load(self):
        if not REMOTE_JOBS_FILE.exists():
            return
        try:
            raw = json.loads(REMOTE_JOBS_FILE.read_text(encoding="utf-8"))
            for entry in raw:
                j = RemoteJob(entry)
                self._items[j.id] = j
        except Exception as e:
            print(f"[remote_jobs] failed to load: {e}")

    def _save(self):
        try:
            # Mantém últimos MAX_KEPT por created_at
            items = sorted(self._items.values(), key=lambda j: j.created_at)[-MAX_KEPT:]
            data = [j.to_dict(include_secrets=True, include_logs=True) for j in items]
            REMOTE_JOBS_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # Re-sincroniza dict com items mantidos
            self._items = {j.id: j for j in items}
        except Exception as e:
            print(f"[remote_jobs] failed to save: {e}")

    def _expire_stale_claims(self):
        """Libera jobs zumbis (claimed > 2min ou running > 10min sem report).

        Casos cobertos:
        - claimed: worker pegou mas não começou (timeout curto = 2min)
        - running: worker tava processando mas travou (timeout maior = 10min,
          tempo suficiente pra um post completo terminar normalmente)
        """
        now = datetime.now(timezone.utc)
        changed = False
        for j in self._items.values():
            if j.status == "claimed":
                ts = parse_iso(j.claimed_at)
                if ts and (now - ts).total_seconds() > CLAIM_TIMEOUT_SECONDS:
                    j.status = "pending"
                    j.worker_id = None
                    j.claimed_at = None
                    j.step = "queued"
                    j.step_at = None
                    changed = True
            elif j.status == "running":
                # Usa step_at (última atividade reportada) ou claimed_at como fallback
                last_activity = parse_iso(j.step_at) or parse_iso(j.claimed_at)
                if last_activity and (now - last_activity).total_seconds() > 600:  # 10min
                    print(f"[remote_jobs] zumbi liberado: {j.id} (running > 10min sem update)")
                    j.status = "pending"
                    j.worker_id = None
                    j.claimed_at = None
                    j.step = "queued"
                    j.step_at = None
                    changed = True
        return changed

    def cleanup_zombies(self) -> int:
        """Roda _expire_stale_claims fora do claim_next (chamado por thread periódica).
        Retorna quantos foram liberados."""
        with self._lock:
            before = sum(1 for j in self._items.values() if j.status in ("claimed", "running"))
            self._expire_stale_claims()
            after = sum(1 for j in self._items.values() if j.status in ("claimed", "running"))
            freed = before - after
            if freed > 0:
                self._save()
            return freed

    # ---- queries ----

    def list(self, workspace_slug: Optional[str] = None) -> list[dict]:
        """Lista jobs. Se workspace_slug for fornecido, filtra apenas dele.
        None = retorna todos (usado por worker/admin)."""
        with self._lock:
            items = sorted(self._items.values(), key=lambda j: j.created_at, reverse=True)
            if workspace_slug:
                items = [j for j in items if j.workspace_slug == workspace_slug]
            return [j.to_dict(include_secrets=False) for j in items]

    def get(self, job_id: str) -> Optional[RemoteJob]:
        return self._items.get(job_id)

    def pending_count(self, workspace_slug: Optional[str] = None) -> int:
        if workspace_slug:
            return sum(
                1 for j in self._items.values()
                if j.status == "pending" and j.workspace_slug == workspace_slug
            )
        return sum(1 for j in self._items.values() if j.status == "pending")

    # ---- mutations ----

    def create(self, payload: dict) -> RemoteJob:
        """Cria novo job. Pra operation='post' com video_name, retorna job existente
        se já houver um ATIVO (pending/claimed/running) pra mesma (account, video).
        Isso evita race condition de 2 callers criarem o mesmo job simultaneamente.

        Side effects automáticos pra post jobs:
        1. Caption passa por humanize_caption() — spinner {a|b|c} + hashtag shuffle
           por conta. Cada @ recebe variação ÚNICA do mesmo template.
        2. Anti-duplicate: dentro de 24h, se a mesma @ já postou o mesmo vídeo,
           bloqueia (evita ban acidental por duplo-post).
        """
        with self._lock:
            # Resolve workspace_slug ANTES do dedupe — dedupe deve respeitar isolamento
            # (mesma @ em workspaces diferentes = jobs diferentes, não duplicata).
            if not payload.get("workspace_slug"):
                try:
                    from core.paths import get_workspace
                    payload["workspace_slug"] = get_workspace()
                except Exception:
                    payload["workspace_slug"] = "default"
            ws_slug = payload["workspace_slug"]

            # Humaniza caption ANTES de qualquer check (caption final = source of truth)
            if payload.get("operation", "post") == "post":
                acc = payload.get("account_username")
                video = payload.get("video_name")
                caption = payload.get("caption") or ""
                if acc and caption:
                    try:
                        from core.spinner import humanize_caption
                        payload["caption"] = humanize_caption(caption, acc)
                    except Exception as _e:
                        pass  # se spinner falhar, usa caption raw

                # Anti-duplicate ATIVO (job ainda na fila) — scoped por workspace
                if acc and video:
                    for j in self._items.values():
                        if (
                            j.operation == "post"
                            and j.account_username == acc
                            and j.video_name == video
                            and j.workspace_slug == ws_slug
                            and j.status in ("pending", "claimed", "running")
                        ):
                            return j

                # Anti-duplicate HISTÓRICO (já postou esse vídeo nessa @ nas últimas 24h)
                # Evita: usuário acidentalmente coloca mesmo vídeo na fila 2x =
                # 2x posts seguidos na mesma @ = flag automático de IG por spam.
                if acc and video:
                    try:
                        from web.main import load_accounts
                        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                        accounts = load_accounts()
                        now_utc = _dt.now(_tz.utc)
                        for a in accounts:
                            if a.get("username") != acc:
                                continue
                            for posted in (a.get("posted_media") or []):
                                if posted.get("name") != video:
                                    continue
                                posted_at = posted.get("posted_at")
                                if not posted_at:
                                    continue
                                try:
                                    pd = _dt.fromisoformat(posted_at)
                                    if pd.tzinfo is None:
                                        pd = pd.astimezone()
                                    elapsed_h = (now_utc - pd.astimezone(_tz.utc)).total_seconds() / 3600
                                    if elapsed_h < 24:
                                        # Cria job "fake" que retorna erro imediato (não cria de verdade)
                                        # Pra UI mostrar feedback claro em vez de silenciar
                                        print(f"[remote_jobs] 🚫 DUPLICATE BLOQUEADO: @{acc} já postou {video} há {elapsed_h:.1f}h")
                                        raise ValueError(
                                            f"@{acc} já postou '{video}' há {elapsed_h:.1f}h "
                                            f"(< 24h). Bloqueado pra evitar ban por spam."
                                        )
                                except ValueError:
                                    raise
                                except Exception:
                                    pass
                    except ValueError:
                        raise
                    except Exception as _e:
                        pass  # se check falhar, não bloqueia criação

            payload["id"] = "rj_" + uuid.uuid4().hex[:12]
            payload["status"] = "pending"
            payload["created_at"] = now_iso()
            # workspace_slug já foi resolvido no topo (antes do dedupe)
            job = RemoteJob(payload)
            self._items[job.id] = job
            self._save()
            return job

    def claim_next(self, worker_id: str) -> Optional[RemoteJob]:
        """Worker pede o próximo job. Marca como claimed pra esse worker.

        Respeita scheduled_for (stagger): só retorna jobs cuja hora agendada já
        chegou. Jobs sem scheduled_for podem ser pegos imediatamente.
        """
        with self._lock:
            self._expire_stale_claims()
            now = now_iso()
            # FIFO por created_at, mas SÓ jobs disponíveis (scheduled_for vazio ou no passado)
            candidates = sorted(
                [
                    j for j in self._items.values()
                    if j.status == "pending"
                    and (not j.scheduled_for or j.scheduled_for <= now)
                ],
                key=lambda j: j.created_at,
            )
            if not candidates:
                return None
            job = candidates[0]
            job.status = "claimed"
            job.worker_id = worker_id
            job.claimed_at = now_iso()
            self._save()
            return job

    def mark_running(self, job_id: str, worker_id: str) -> bool:
        with self._lock:
            job = self._items.get(job_id)
            if not job or job.worker_id != worker_id:
                return False
            job.status = "running"
            # Se worker ainda não reportou step, assume downloading (1ª etapa do flow)
            if job.step == "queued":
                job.step = "downloading"
                job.step_at = now_iso()
            self._save()
            return True

    VALID_STEPS = ("queued", "downloading", "logging", "posting", "finishing", "done")

    def set_step(self, job_id: str, worker_id: str, step: str) -> bool:
        if step not in self.VALID_STEPS:
            return False
        with self._lock:
            job = self._items.get(job_id)
            if not job or job.worker_id != worker_id:
                return False
            job.step = step
            job.step_at = now_iso()
            self._save()
            return True

    def append_log(self, job_id: str, worker_id: str, line: str) -> bool:
        with self._lock:
            job = self._items.get(job_id)
            if not job or job.worker_id != worker_id:
                return False
            job.logs.append(line[:500])  # limita tamanho da linha
            if len(job.logs) > MAX_LOG_LINES:
                job.logs = job.logs[-MAX_LOG_LINES:]
            self._save()
            return True

    def report_result(self, job_id: str, worker_id: str, success: bool,
                      media_id: Optional[str] = None, error_msg: Optional[str] = None,
                      result_data: Optional[dict] = None) -> bool:
        with self._lock:
            job = self._items.get(job_id)
            if not job or job.worker_id != worker_id:
                return False
            job.status = "done" if success else "error"
            job.step = "done"
            job.step_at = now_iso()
            job.media_id = media_id
            job.error_msg = error_msg
            if result_data:
                job.result_data = result_data
            job.finished_at = now_iso()
            self._save()
            return True

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._items.get(job_id)
            if not job or job.status not in ("pending", "claimed"):
                return False
            job.status = "cancelled"
            job.finished_at = now_iso()
            self._save()
            return True

    def delete(self, job_id: str) -> bool:
        with self._lock:
            if job_id not in self._items:
                return False
            del self._items[job_id]
            self._save()
            return True

    def requeue(self, job_id: str) -> bool:
        """Forca um job de volta pra 'pending' (limpa scheduled_for + worker_id).
        Util quando um job ficou travado em pending com scheduled_for futuro ou
        em claimed/running zumbi."""
        with self._lock:
            job = self._items.get(job_id)
            if not job:
                return False
            # Permite re-enfileirar de qualquer estado exceto done bem-sucedido recente
            job.status = "pending"
            job.scheduled_for = None
            job.worker_id = None
            job.claimed_at = None
            job.step = "queued"
            job.step_at = None
            job.error_msg = None
            self._save()
            return True

    def delete_by_account(self, username: str, workspace_slug: Optional[str] = None) -> int:
        """Remove TODOS os jobs (qualquer status) de uma conta específica.
        Usado quando user deleta uma conta — limpa histórico órfão.
        Se workspace_slug for fornecido, scoped nele. Retorna número removido."""
        if not username:
            return 0
        with self._lock:
            ids_to_delete = [
                jid for jid, j in self._items.items()
                if j.account_username == username
                and (not workspace_slug or j.workspace_slug == workspace_slug)
            ]
            for jid in ids_to_delete:
                self._items.pop(jid, None)
            if ids_to_delete:
                self._save()
            return len(ids_to_delete)

    def purge_orphans(self, valid_accounts_by_ws: dict[str, set[str]]) -> int:
        """Remove jobs cujo (account_username, workspace_slug) não existe mais.
        valid_accounts_by_ws: { workspace_slug: {set de usernames ativos} }.
        Útil pra sweep periódico — pega jobs órfãos que vieram de delete sem cleanup."""
        with self._lock:
            ids_to_delete = []
            for jid, j in self._items.items():
                valid_set = valid_accounts_by_ws.get(j.workspace_slug, set())
                if j.account_username not in valid_set:
                    ids_to_delete.append(jid)
            for jid in ids_to_delete:
                self._items.pop(jid, None)
            if ids_to_delete:
                self._save()
            return len(ids_to_delete)

    def dedupe_pending(self, workspace_slug: Optional[str] = None) -> int:
        """Remove jobs pending/claimed duplicados: pra cada par (account, video_name)
        mantém apenas 1 job ativo, deleta os outros.

        Útil pra limpar fila quando algum bug criou múltiplos jobs do mesmo
        vídeo pra mesma conta (ex: race condition antes do anti-duplicate).

        Se workspace_slug for fornecido, age apenas em jobs desse ws.
        """
        with self._lock:
            seen: dict[tuple[str, str, str], str] = {}  # (ws, acc, video) -> job_id mantém
            to_delete: list[str] = []
            for j in self._items.values():
                if j.status not in ("pending", "claimed"):
                    continue
                if j.operation != "post" or not j.video_name:
                    continue
                if workspace_slug and j.workspace_slug != workspace_slug:
                    continue
                key = (j.workspace_slug, j.account_username, j.video_name)
                if key in seen:
                    # Já tem outro job ativo pra esse par — marca pra deletar
                    to_delete.append(j.id)
                else:
                    seen[key] = j.id
            for jid in to_delete:
                self._items.pop(jid, None)
            if to_delete:
                self._save()
            return len(to_delete)

    def requeue_stuck(self, workspace_slug: Optional[str] = None) -> int:
        """Re-enfileira TODOS os jobs que estão presos: pending com scheduled_for
        no futuro, ou claimed/running sem update recente, ou pending sem worker
        ha mais de 10min.

        Se workspace_slug for fornecido, age apenas em jobs desse ws.
        Retorna quantos foram re-enfileirados.
        """
        now = now_iso()
        count = 0
        with self._lock:
            for job in self._items.values():
                if workspace_slug and job.workspace_slug != workspace_slug:
                    continue
                stuck = False
                # Pending com scheduled_for ainda no futuro: forca pra agora
                if job.status == "pending" and job.scheduled_for and job.scheduled_for > now:
                    stuck = True
                # Claimed/running zumbi (worker pegou e não terminou)
                elif job.status in ("claimed", "running"):
                    ts = parse_iso(job.claimed_at)
                    if ts:
                        from datetime import datetime as _dt, timezone as _tz
                        nowdt = _dt.now(_tz.utc)
                        if (nowdt - ts).total_seconds() > 300:  # 5min sem update
                            stuck = True
                # Pending velho (criado ha mais de 1h e nao foi pego)
                elif job.status == "pending":
                    ts = parse_iso(job.created_at)
                    if ts:
                        from datetime import datetime as _dt, timezone as _tz
                        nowdt = _dt.now(_tz.utc)
                        if (nowdt - ts).total_seconds() > 3600:
                            stuck = True

                if stuck:
                    job.status = "pending"
                    job.scheduled_for = None
                    job.worker_id = None
                    job.claimed_at = None
                    job.step = "queued"
                    job.step_at = None
                    count += 1
            if count > 0:
                self._save()
        return count


manager = RemoteJobManager()
