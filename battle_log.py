# -*- coding: utf-8 -*-
"""战斗日志存储：独立于 battle.json，按日期归档，为排行榜提供数据"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

BATTLE_LOG_FILE = "battle.json"

# 保存到战斗日志的固定字段；*_agro 潜在字段由 extract_log_fields 动态提取
LOG_FIELDS = [
    "survived", "win_and_survived",
    "exp",
    "planes_killed", "ships_spotted",
    "capture_points", "dropped_capture_points",
    "shots_by_main", "hits_by_main",
    "shots_by_tpd", "hits_by_tpd",
]


class BattleLogStore:
    """战斗日志存储，独立文件读写"""

    def __init__(self, plugin_dir: Path):
        self._path = plugin_dir / BATTLE_LOG_FILE
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}

    def _save(self) -> None:
        """原子写入，防止写入中断导致文件损坏"""
        try:
            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(self._data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp_path.replace(self._path)
        except Exception:
            pass

    def add_record(self, record: dict[str, Any]) -> None:
        """添加一条战斗记录，按日期归档"""
        date_key = record.get("date") or datetime.now().strftime("%Y-%m-%d")
        self._data.setdefault("logs", {}).setdefault(date_key, []).append(record)
        self._save()

    def get_by_date(self, date_str: str) -> list[dict]:
        return self._data.get("logs", {}).get(date_str, [])

    def get_by_date_range(self, start_date: str, end_date: str) -> list[dict]:
        """返回日期区间 [start, end] 内的全部记录（含首尾），按日期升序"""
        logs = self._data.get("logs", {})
        result: list[dict] = []
        for date_key in sorted(logs.keys()):
            if start_date <= date_key <= end_date:
                result.extend(logs[date_key])
        return result

    def cleanup_old(self, retention_days: int = 30) -> int:
        """清理保留期前的日志，返回删除的记录数"""
        logs = self._data.get("logs", {})
        cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
        removed = 0
        for date_key in list(logs.keys()):
            if date_key < cutoff:
                removed += len(logs[date_key])
                del logs[date_key]
        if removed > 0:
            self._save()
        return removed


def extract_log_fields(diff: dict[str, int]) -> dict[str, int]:
    """从差值中提取日志字段：固定字段 + 所有非max的*_agro潜在字段"""
    result = {k: diff.get(k, 0) for k in LOG_FIELDS}
    for k, v in diff.items():
        if k.endswith("_agro") and not k.startswith("max_"):
            result[k] = v
    return result
