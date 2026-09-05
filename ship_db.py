# -*- coding: utf-8 -*-
"""舰种数据库：从图鉴拉取并本地缓存 ship_id -> {name, type, tier}"""

import json
from pathlib import Path
from typing import Any

SHIP_TYPE_CN: dict[str, str] = {
    "AirCarrier": "航母",
    "Battleship": "战列舰",
    "Cruiser": "巡洋舰",
    "Destroyer": "驱逐舰",
    "Submarine": "潜艇",
}
SHIP_TYPE_EN: dict[str, str] = {v: k for k, v in SHIP_TYPE_CN.items()}

# 图鉴未收录的船（B站联动等），手动补充
MANUAL_OVERRIDES: dict[str, dict] = {
    "4274657136": {"name": "雷神", "type": "Battleship", "tier": 10},
}


class ShipDb:
    """舰种本地缓存，缺缓存时由外部调用 refresh 从图鉴拉取"""

    def __init__(self, cache_path: Path):
        self._path = cache_path
        self._db: dict[str, dict[str, Any]] = {}
        self._overrides: dict[str, dict[str, Any]] = dict(MANUAL_OVERRIDES)
        self._load()

    def _load(self) -> None:
        """从本地缓存加载；图鉴补录（MANUAL_OVERRIDES）不计入 loaded"""
        self._db = {}
        try:
            if self._path.exists():
                self._db = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            self._db = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._db, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    @property
    def loaded(self) -> bool:
        """是否有完整图鉴缓存（不含补录）；无缓存时由调用方触发 refresh"""
        return bool(self._db)

    async def refresh(self, api: Any) -> int:
        """从图鉴接口拉取全量并落盘，返回拉取条数"""
        raw = await api.fetch_ship_catalog()
        if raw:
            self._db = raw
            self._save()
        return len(self._db)

    def _entry(self, ship_id: Any) -> dict:
        key = str(ship_id)
        return self._db.get(key) or self._overrides.get(key) or {}

    def ship_name(self, ship_id: Any) -> str:
        return self._entry(ship_id).get("name", "")

    def ship_type_cn(self, ship_id: Any) -> str:
        """返回中文舰种名；未知舰种返回空串"""
        return SHIP_TYPE_CN.get(self._entry(ship_id).get("type", ""), "")

    def ship_type_en(self, ship_id: Any) -> str:
        """返回英文舰种代码（AirCarrier等）；未知返回空串"""
        return self._entry(ship_id).get("type", "")
