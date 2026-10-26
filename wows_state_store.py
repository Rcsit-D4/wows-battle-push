"""多文件状态存储：groups / snapshots / leaderboard / pending 独立文件 + 旧 data.json 自动迁移

文件均位于插件目录下的 data/ 文件夹：
- groups.json      群配置（良好可读缩进）
- snapshots.json   账号快照（紧凑）
- leaderboard.json 榜单数据（紧凑）
- pending.json     待补发事件（紧凑，手动检查/发送失败时使用）
- battle.json      战斗日志（紧凑，由 BattleLogStore 管理）
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from wows_utils import atomic_write_json

logger = logging.getLogger(__name__)

DATA_DIR = "data"
GROUPS_FILE = "groups.json"
SNAPSHOTS_FILE = "snapshots.json"
LEADERBOARD_FILE = "leaderboard.json"
PENDING_FILE = "pending.json"
LEGACY_FILE = "data.json"
BATTLE_LOG_FILE = "battle.json"


class StateStore:
    """按用途分文件存储，支持旧版单文件 data.json 的一次性迁移"""

    def __init__(self, plugin_dir: Path) -> None:
        self._dir = Path(plugin_dir)
        self._data_dir = self._dir / DATA_DIR
        self.groups: dict[str, Any] = {}
        self.snapshots: dict[str, Any] = {}
        self.leaderboard: dict[str, Any] = {}
        self.pending: list[Any] = []
        self._migrate_legacy()
        self.load_all()

    # ---------- 迁移 ----------
    def _migrate_legacy(self) -> None:
        """旧 data.json / battle.json 拆分或移动到 data/ 目录，幂等。

        逐文件判断，只补缺失文件，避免迁移中断后旧 data.json 覆盖新文件。
        """
        legacy = self._dir / LEGACY_FILE
        new_files = {
            self._data_dir / GROUPS_FILE: (
                lambda data: {"version": 1, "bindings": data.get("bindings", {})},
                True,
            ),
            self._data_dir / SNAPSHOTS_FILE: (lambda data: data.get("snapshots", {}), False),
            self._data_dir / LEADERBOARD_FILE: (lambda data: data.get("daily_king", {}), True),
        }
        if legacy.exists() and not all(path.exists() for path in new_files):
            try:
                data = json.loads(legacy.read_text(encoding="utf-8"))
                self._data_dir.mkdir(parents=True, exist_ok=True)
                for path, (payload_fn, indent) in new_files.items():
                    if path.exists():
                        continue
                    if not self._write(path, payload_fn(data), indent=indent):
                        raise RuntimeError(f"迁移写入失败: {path}")
                legacy.replace(self._data_dir / (LEGACY_FILE + ".bak"))
            except Exception:  # noqa: BLE001
                logger.exception("旧 data.json 迁移失败，保留原文件: %s", legacy)

        old_battle = self._dir / BATTLE_LOG_FILE
        new_battle = self._data_dir / BATTLE_LOG_FILE
        if old_battle.exists() and not new_battle.exists():
            try:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_battle), str(new_battle))
            except Exception:  # noqa: BLE001
                logger.exception("旧 battle.json 移动失败: %s", old_battle)

    # ---------- 加载 ----------
    def load_all(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self.groups = self._read(self._data_dir / GROUPS_FILE, {"version": 1, "bindings": {}})
        self.snapshots = self._read(self._data_dir / SNAPSHOTS_FILE, {})
        self.leaderboard = self._read(self._data_dir / LEADERBOARD_FILE, {})
        self.pending = self._read(self._data_dir / PENDING_FILE, [])
        if not isinstance(self.groups, dict) or "bindings" not in self.groups:
            self.groups = {"version": 1, "bindings": {}}
        if not isinstance(self.snapshots, dict):
            self.snapshots = {}
        if not isinstance(self.leaderboard, dict):
            self.leaderboard = {}
        if not isinstance(self.pending, list):
            self.pending = []

    # ---------- 保存 ----------
    def save_groups(self) -> bool:
        return self._write(self._data_dir / GROUPS_FILE, self.groups, indent=True)

    def save_snapshots(self) -> bool:
        return self._write(self._data_dir / SNAPSHOTS_FILE, self.snapshots)

    def save_leaderboard(self) -> bool:
        return self._write(self._data_dir / LEADERBOARD_FILE, self.leaderboard, indent=True)

    def save_pending(self) -> bool:
        return self._write(self._data_dir / PENDING_FILE, self.pending)

    # ---------- 内部 ----------
    @staticmethod
    def _read(path: Path, default: Any) -> Any:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            logger.exception("读取状态文件失败，使用默认值: %s", path)
        return default

    @staticmethod
    def _write(path: Path, data: Any, indent: bool = False) -> bool:
        return atomic_write_json(path, data, indent)
