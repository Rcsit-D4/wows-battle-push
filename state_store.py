"""多文件状态存储：groups / snapshots / leaderboard 独立文件 + 旧 data.json 自动迁移

文件均位于插件目录下的 data/ 文件夹：
- groups.json      群配置（良好可读缩进）
- snapshots.json   账号快照（紧凑）
- leaderboard.json 榜单数据（紧凑）
- battle.json      战斗日志（紧凑，由 BattleLogStore 管理）
"""

import json
import shutil
from pathlib import Path
from typing import Any

DATA_DIR = "data"
GROUPS_FILE = "groups.json"
SNAPSHOTS_FILE = "snapshots.json"
LEADERBOARD_FILE = "leaderboard.json"
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
        self._migrate_legacy()
        self.load_all()

    # ---------- 迁移 ----------
    def _migrate_legacy(self) -> None:
        """旧 data.json / battle.json 拆分或移动到 data/ 目录，幂等"""
        legacy = self._dir / LEGACY_FILE
        new_files = (
            self._data_dir / GROUPS_FILE,
            self._data_dir / SNAPSHOTS_FILE,
            self._data_dir / LEADERBOARD_FILE,
        )
        if legacy.exists() and not all(f.exists() for f in new_files):
            try:
                data = json.loads(legacy.read_text(encoding="utf-8"))
                self._data_dir.mkdir(parents=True, exist_ok=True)
                self._write(self._data_dir / GROUPS_FILE,
                            {"version": 1, "bindings": data.get("bindings", {})}, indent=True)
                self._write(self._data_dir / SNAPSHOTS_FILE, data.get("snapshots", {}))
                self._write(self._data_dir / LEADERBOARD_FILE, data.get("daily_king", {}), indent=True)
                legacy.replace(self._data_dir / (LEGACY_FILE + ".bak"))
            except Exception:  # noqa: BLE001
                pass
        old_battle = self._dir / BATTLE_LOG_FILE
        new_battle = self._data_dir / BATTLE_LOG_FILE
        if old_battle.exists() and not new_battle.exists():
            try:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_battle), str(new_battle))
            except Exception:  # noqa: BLE001
                pass

    # ---------- 加载 ----------
    def load_all(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self.groups = self._read(self._data_dir / GROUPS_FILE, {"version": 1, "bindings": {}})
        self.snapshots = self._read(self._data_dir / SNAPSHOTS_FILE, {})
        self.leaderboard = self._read(self._data_dir / LEADERBOARD_FILE, {})
        if not isinstance(self.groups, dict) or "bindings" not in self.groups:
            self.groups = {"version": 1, "bindings": {}}
        if not isinstance(self.snapshots, dict):
            self.snapshots = {}
        if not isinstance(self.leaderboard, dict):
            self.leaderboard = {}

    # ---------- 保存 ----------
    def save_groups(self) -> None:
        self._write(self._data_dir / GROUPS_FILE, self.groups, indent=True)

    def save_snapshots(self) -> None:
        self._write(self._data_dir / SNAPSHOTS_FILE, self.snapshots)

    def save_leaderboard(self) -> None:
        self._write(self._data_dir / LEADERBOARD_FILE, self.leaderboard, indent=True)

    # ---------- 内部 ----------
    @staticmethod
    def _read(path: Path, default: Any) -> Any:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return default

    @staticmethod
    def _write(path: Path, data: Any, indent: bool = False) -> None:
        """原子写入：先写临时文件再重命名，防止写入中断损坏"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            if indent:
                text = json.dumps(data, ensure_ascii=False, indent=2)
            else:
                text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except Exception:  # noqa: BLE001
            pass
