from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from app.schemas import MerchantEnrichment

_BASE_DIR = Path(__file__).resolve().parents[1]
_DICT_PATH = _BASE_DIR / "data" / "india_merchants.json"
_CACHE_PATH = _BASE_DIR / "data" / "enrichment_cache.db"

_CLEAN_RE = re.compile(r"[^\w\s]")
_SUFFIX_RE = re.compile(
    r"\b(PVT|LTD|LIMITED|INC|CORP|LLP|SOLUTIONS|TECHNOLOGIES|SERVICES|RECHARGE|PAY|PAYMENTS|INDIA|ONLINE|WEB|STORES)\b",
    re.IGNORECASE,
)

def normalize_name(name: str) -> str:
    if not name:
        return ""
    text = _CLEAN_RE.sub(" ", name)
    text = _SUFFIX_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().lower()

class MerchantServerResolver:
    def __init__(self) -> None:
        self._dictionary: list[dict[str, Any]] = []
        self._alias_map: dict[str, dict[str, Any]] = {}
        self._load_dictionary()
        self._init_cache()

    def _load_dictionary(self) -> None:
        if not _DICT_PATH.exists():
            return
        data = json.loads(_DICT_PATH.read_text(encoding="utf-8"))
        self._dictionary = data.get("merchants", [])
        for entry in self._dictionary:
            for alias in entry.get("aliases", []):
                norm = normalize_name(alias)
                if norm:
                    self._alias_map[norm] = entry

    def _init_cache(self) -> None:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(_CACHE_PATH) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS merchant_cache (
                    normalized_name TEXT PRIMARY KEY,
                    enrichment_json TEXT NOT NULL
                )
                """
            )

    def _get_from_cache(self, norm_name: str) -> MerchantEnrichment | None:
        try:
            with sqlite3.connect(_CACHE_PATH) as conn:
                cur = conn.execute("SELECT enrichment_json FROM merchant_cache WHERE normalized_name = ?", (norm_name,))
                row = cur.fetchone()
                if row:
                    return MerchantEnrichment(**json.loads(row[0]))
        except Exception:
            pass
        return None

    def _save_to_cache(self, norm_name: str, enrichment: MerchantEnrichment) -> None:
        try:
            with sqlite3.connect(_CACHE_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO merchant_cache (normalized_name, enrichment_json) VALUES (?, ?)",
                    (norm_name, enrichment.model_dump_json()),
                )
        except Exception:
            pass

    def lookup_dict(self, norm_name: str, raw_name: str) -> MerchantEnrichment | None:
        if norm_name in self._alias_map:
            e = self._alias_map[norm_name]
            return MerchantEnrichment(
                canonical_name=e["canonical_name"],
                category=e["category"],
                is_essential=e["is_essential"],
                risk_flag=e.get("risk_flag"),
                lifestyle_dim=e["lifestyle_dim"],
                recurring_type=e["recurring_type"],
            )

        for alias, e in self._alias_map.items():
            if len(alias) >= 4 and (norm_name.startswith(alias) or alias.startswith(norm_name)):
                return MerchantEnrichment(
                    canonical_name=e["canonical_name"],
                    category=e["category"],
                    is_essential=e["is_essential"],
                    risk_flag=e.get("risk_flag"),
                    lifestyle_dim=e["lifestyle_dim"],
                    recurring_type=e["recurring_type"],
                )
        return None

    def resolve(self, raw_name: str) -> MerchantEnrichment:
        norm = normalize_name(raw_name)
        if not norm:
            return MerchantEnrichment.unknown(raw_name)

        cached = self._get_from_cache(norm)
        if cached:
            return cached

        hit = self.lookup_dict(norm, raw_name)
        if hit:
            self._save_to_cache(norm, hit)
            return hit

        fallback = MerchantEnrichment.unknown(raw_name)
        self._save_to_cache(norm, fallback)
        return fallback

_resolver_instance: MerchantServerResolver | None = None

def get_server_resolver() -> MerchantServerResolver:
    global _resolver_instance
    if _resolver_instance is None:
        _resolver_instance = MerchantServerResolver()
    return _resolver_instance
