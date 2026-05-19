"""
Área financeira — lançamentos de custos e vendas.

Storage: data/finance.json (lista de dicts).

Schema de cada lançamento:
{
    "id": "fin_<hex>",
    "type": "custo" | "venda",
    "category": str (preset, ver CATEGORIES),
    "amount": float (R$),
    "description": str,
    "date": "YYYY-MM-DD",
    "created_at": ISO,
    "created_by": email,
    "notes": str | None
}
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from typing import Optional

from core.paths import data_path

FINANCE_FILE = data_path("finance.json")

CATEGORIES = {
    "custo": ["compra-conta", "proxy", "ferramenta", "anuncio", "outro"],
    "venda": ["cliente-direto", "indicacao", "outro"],
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class FinanceEntry:
    def __init__(self, data: dict):
        self.id: str = data["id"]
        self.type: str = data["type"]
        self.category: str = data.get("category") or "outro"
        self.amount: float = float(data["amount"])
        self.description: str = data.get("description", "")
        self.date: str = data["date"]
        self.created_at: str = data.get("created_at") or now_iso()
        self.created_by: Optional[str] = data.get("created_by")
        self.notes: Optional[str] = data.get("notes")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "category": self.category,
            "amount": self.amount,
            "description": self.description,
            "date": self.date,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "notes": self.notes,
        }


class FinanceManager:
    def __init__(self):
        self._items: list[FinanceEntry] = []
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if not FINANCE_FILE.exists():
            return
        try:
            raw = json.loads(FINANCE_FILE.read_text(encoding="utf-8"))
            self._items = [FinanceEntry(d) for d in raw]
        except Exception as e:
            print(f"[finance] failed to load: {e}")

    def _save(self):
        try:
            data = [e.to_dict() for e in self._items]
            FINANCE_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[finance] failed to save: {e}")

    def list(self, type_filter: Optional[str] = None, month: Optional[str] = None) -> list[dict]:
        """
        Lista lançamentos ordenados por data desc.
        type_filter: 'custo' | 'venda' | None
        month: 'YYYY-MM' | None
        """
        with self._lock:
            items = self._items[:]
        if type_filter:
            items = [e for e in items if e.type == type_filter]
        if month:
            items = [e for e in items if (e.date or "").startswith(month)]
        items.sort(key=lambda e: (e.date, e.created_at), reverse=True)
        return [e.to_dict() for e in items]

    def summary(self, month: Optional[str] = None) -> dict:
        """Totais agregados (opcionalmente filtrado por mês YYYY-MM)."""
        with self._lock:
            items = self._items[:]
        if month:
            items = [e for e in items if (e.date or "").startswith(month)]

        total_custos = sum(e.amount for e in items if e.type == "custo")
        total_vendas = sum(e.amount for e in items if e.type == "venda")
        saldo = total_vendas - total_custos

        by_cat_custo: dict[str, float] = {}
        by_cat_venda: dict[str, float] = {}
        for e in items:
            target = by_cat_custo if e.type == "custo" else by_cat_venda
            target[e.category] = target.get(e.category, 0.0) + e.amount

        return {
            "month": month,
            "total_custos": round(total_custos, 2),
            "total_vendas": round(total_vendas, 2),
            "saldo": round(saldo, 2),
            "count_custos": sum(1 for e in items if e.type == "custo"),
            "count_vendas": sum(1 for e in items if e.type == "venda"),
            "by_category": {
                "custo": {k: round(v, 2) for k, v in by_cat_custo.items()},
                "venda": {k: round(v, 2) for k, v in by_cat_venda.items()},
            },
        }

    def create(self, payload: dict, created_by: Optional[str] = None) -> FinanceEntry:
        if payload.get("type") not in ("custo", "venda"):
            raise ValueError("type deve ser 'custo' ou 'venda'")
        try:
            amount = float(payload["amount"])
        except (KeyError, ValueError, TypeError):
            raise ValueError("amount inválido")
        if amount < 0:
            raise ValueError("amount não pode ser negativo")
        category = (payload.get("category") or "outro").strip().lower()
        if category not in CATEGORIES[payload["type"]]:
            category = "outro"
        date = (payload.get("date") or "").strip()
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("date deve ser YYYY-MM-DD")

        entry = FinanceEntry({
            "id": "fin_" + uuid.uuid4().hex[:10],
            "type": payload["type"],
            "category": category,
            "amount": round(amount, 2),
            "description": (payload.get("description") or "").strip()[:200],
            "date": date,
            "created_at": now_iso(),
            "created_by": created_by,
            "notes": (payload.get("notes") or "").strip()[:500] or None,
        })
        with self._lock:
            self._items.append(entry)
            self._save()
        return entry

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [e for e in self._items if e.id != entry_id]
            if len(self._items) == before:
                return False
            self._save()
            return True


manager = FinanceManager()
