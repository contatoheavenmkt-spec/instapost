"""
Bootstrap retroativo do `posted_media` por conta.

Olha os arquivos em data/posted/ (mídias já postadas antes do feature de sync
existir), lê metadata + mtime, e popula a chave `posted_media` no accounts.json
pra cada conta que NÃO está em --exclude.

Uso na VPS:
    docker compose -f docker-compose.nginx.yml exec app \\
        python tools/bootstrap_posted_media.py --exclude conta_nova1,conta_nova2 --dry-run

Quando estiver feliz com o preview, remove --dry-run.

Rodar com --dry-run primeiro é OBRIGATÓRIO antes de gravar.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Garante root no sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.paths import ACCOUNTS_FILE, POSTED_DIR  # noqa: E402
from core.poster import load_meta  # noqa: E402

MEDIA_EXTS = {".mp4", ".jpg", ".jpeg", ".png", ".webp"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def is_video_thumb(p: Path) -> bool:
    """Detecta .jpg que é thumb de um .mp4 (sibling com mesmo stem ou nome 'video.mp4.jpg')."""
    suffix = p.suffix.lower()
    if suffix not in (".jpg", ".jpeg"):
        return False
    if p.stem.lower().endswith(".mp4"):
        return True
    if p.with_suffix(".mp4").exists():
        return True
    return False


def collect_posted_files() -> list[dict]:
    """Lista os arquivos de mídia REAIS em data/posted/ com mtime e kind."""
    if not POSTED_DIR.exists():
        return []
    items = []
    for p in POSTED_DIR.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in MEDIA_EXTS:
            continue
        if p.name.endswith(".meta.json"):
            continue
        if is_video_thumb(p):
            continue
        try:
            meta = load_meta(str(p))
        except Exception:
            meta = {}
        kind = meta.get("kind") or ("story" if p.suffix.lower() in PHOTO_EXTS else "reel")
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).astimezone()
        items.append({
            "name": p.name,
            "kind": kind,
            "posted_at": mtime.isoformat(timespec="seconds"),
            "media_id": None,  # bootstrap não sabe o media_id real
            "mtime": p.stat().st_mtime,
        })
    # Ordena cronologicamente (mais antigo primeiro)
    items.sort(key=lambda x: x["mtime"])
    return items


def main():
    ap = argparse.ArgumentParser(description="Bootstrap retroativo de posted_media")
    ap.add_argument("--exclude", default="", help="Lista CSV de usernames de contas NOVAS (que NÃO devem receber o histórico). Ex: --exclude nova1,nova2")
    ap.add_argument("--dry-run", action="store_true", help="Só mostra o que faria, não grava")
    args = ap.parse_args()

    exclude_set = {u.strip().lower() for u in args.exclude.split(",") if u.strip()}

    if not ACCOUNTS_FILE.exists():
        print(f"ERRO: {ACCOUNTS_FILE} não existe", file=sys.stderr)
        sys.exit(1)

    accounts = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    files = collect_posted_files()

    print(f"[posted/] {len(files)} midia(s) reais encontradas")
    if not files:
        print("Nada pra fazer. Sai.")
        return

    print(f"\nOrdem cronológica (mtime):")
    for i, f in enumerate(files, 1):
        print(f"  {i:>2}. [{f['kind']:>5}] {f['name']}  ({f['posted_at']})")

    print(f"\n[contas] {len(accounts)} no accounts.json")
    targets = []
    skipped = []
    for a in accounts:
        if a["username"].lower() in exclude_set:
            skipped.append(a["username"])
        else:
            targets.append(a)

    print(f"   -> Vao receber historico: {len(targets)}")
    for a in targets:
        existing = len(a.get("posted_media") or [])
        print(f"      - @{a['username']}  (ja tem {existing} no posted_media)")
    if skipped:
        print(f"   -> Pulando (--exclude): {len(skipped)}")
        for u in skipped:
            print(f"      - @{u}")

    # Aplica
    total_added = 0
    for a in targets:
        existing_names = {p.get("name") for p in (a.get("posted_media") or []) if p.get("name")}
        new_items = []
        for f in files:
            if f["name"] in existing_names:
                continue
            new_items.append({
                "name": f["name"],
                "kind": f["kind"],
                "posted_at": f["posted_at"],
                "media_id": None,
                "source": "bootstrap",
            })
        if new_items:
            a["posted_media"] = (a.get("posted_media") or []) + new_items
            total_added += len(new_items)
            print(f"\n@{a['username']}: +{len(new_items)} midias adicionadas (total agora: {len(a['posted_media'])})")

    print(f"\n{'='*60}")
    print(f"TOTAL: {total_added} registro(s) a adicionar em {len(targets)} conta(s)")
    print(f"{'='*60}")

    if args.dry_run:
        print("\n[DRY-RUN] nada foi gravado. Roda sem --dry-run pra aplicar.")
        return

    # Backup do accounts.json antes
    backup_path = ACCOUNTS_FILE.with_suffix(f".json.bak-{int(datetime.now().timestamp())}")
    backup_path.write_text(ACCOUNTS_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"\n[backup] salvo em {backup_path}")

    ACCOUNTS_FILE.write_text(
        json.dumps(accounts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[ok] accounts.json atualizado")


if __name__ == "__main__":
    main()
