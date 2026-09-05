# -*- coding: utf-8 -*-
"""战舰世界战绩结算自动推送插件（MaiBot）

模块结构：
  constants.py   — 常量定义
  config.py      — 配置模型
  api.py         — Vortex API 客户端
  stats.py       — 统计快照、差值检测、播报格式化
  cards.py       — help/status/list 图片卡片生成
  leaderboard.py — 榜单注册表 + 排行榜实现（命令/帮助/状态由注册表自动生成）
  battle_log.py  — 战斗日志存储与查询
  utils.py       — HTML渲染、背景图、图片发送工具

榜单扩展：在 leaderboard.py 用 register_board() 注册新榜，命令、排行榜帮助、
管理员帮助开关、status 状态、跨天推送均自动生成，无需修改其他文件。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# 确保插件目录在 sys.path 中，使同目录模块可导入
sys.path.insert(0, str(Path(__file__).parent))

import asyncio
import time
from typing import Any

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Command, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from api import WowsApi
from battle_log import BattleLogStore, extract_log_fields
from config import PluginConfig
from constants import (
    EXTRA_ITEMS,
    PUBLIC_COMMANDS,
    SERVER_VORTEX,
    SHIP_MAP_REFRESH_SECONDS,
    VALID_BATTLE_TYPES,
    normalize_server,
)
from nl_query import (
    DEFAULT_ENABLED as NL_DEFAULT_ENABLED,
    TOOL_DESCRIPTION as NL_TOOL_DESCRIPTION,
    run_query,
)
from ship_db import ShipDb
from state_store import StateStore
from cards import (
    HELP_TEXT,
    ADMIN_HELP_TEXT,
    build_help_html,
    build_admin_help_html,
    build_status_html,
    build_list_html_pages,
)
import leaderboard
from leaderboard import (
    PERIOD_DAILY,
    build_lbhelp_html,
    cmd_specs,
    get_history,
    get_stream_data as get_king_data,
    lbhelp_text,
    month_str,
    parse_date,
)
from stats import (
    check_career_records,
    default_extra,
    detect_new_battles,
    format_battle,
    format_record_break,
    normalize_extra,
    should_broadcast_damage,
    should_broadcast_type,
    summarize,
)
from utils import send_html_image

MODE_TEXT = {1: "单野", 2: "单野/组排", 3: "ALL"}


class WowsBattlePushPlugin(MaiBotPlugin):
    config_model = PluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._api = WowsApi()
        self._state: dict[str, Any] = {"version": 1, "bindings": {}, "snapshots": {}, "daily_king": {}}
        self._battle_log: BattleLogStore | None = None
        self._store: StateStore | None = None
        self._poller_task: asyncio.Task | None = None
        self._stopped = False
        self._ship_map: dict[int, str] = {}
        self._ship_map_ts: float = 0.0
        self._ship_db = ShipDb(Path(__file__).parent / "data" / "ship_db.json")

    # ---------- 权限 ----------
    def _get_sender_id(self, kwargs: dict[str, Any]) -> str:
        for key in ("sender_id", "user_id", "qq", "from_user_id", "from_user", "sender", "user"):
            v = kwargs.get(key)
            if v:
                return str(v)
        sender = kwargs.get("sender") or kwargs.get("user")
        if isinstance(sender, dict):
            for key in ("id", "qq", "user_id", "userid"):
                v = sender.get(key)
                if v:
                    return str(v)
        return ""

    def _is_admin(self, sender_id: str) -> bool:
        return bool(sender_id) and sender_id in [str(a) for a in (self.config.plugin.admin_qq or [])]

    async def _check_permission(self, command_name: str, kwargs: dict[str, Any]) -> bool:
        if command_name in PUBLIC_COMMANDS or self._is_admin(self._get_sender_id(kwargs)):
            return True
        stream_id = kwargs.get("stream_id", "")
        await self.ctx.send.text(f"权限不足：该命令仅管理员可用。（你的QQ：{self._get_sender_id(kwargs) or '未知'}）", stream_id)
        return False

    # ---------- 绑定辅助 ----------
    def _get_binding(self, stream_id: str) -> dict[str, Any] | None:
        return self._state.get("bindings", {}).get(stream_id)

    def _find_account(self, stream_id: str, server: str, account_id: int) -> dict | None:
        binding = self._get_binding(stream_id)
        if not binding:
            return None
        for acc in binding.get("accounts", []):
            if str(acc.get("server", "")).upper() == server.upper() and int(acc.get("account_id", 0)) == account_id:
                return acc
        return None

    def _get_group_nickname(self, stream_id: str, server: str, account_id: int) -> str | None:
        acc = self._find_account(stream_id, server, account_id)
        nick = str(acc.get("nickname") or "") if acc else ""
        return nick if nick else None

    def _get_display_name(self, stream_id: str, server: str, account_id: int, game_name: str) -> str:
        return self._get_group_nickname(stream_id, server, account_id) or game_name

    def _get_game_name(self, server: str, account_id: int) -> str:
        return self._state.get("snapshots", {}).get(f"{server.upper()}:{account_id}", {}).get("name") or "未拉取"

    def _get_display_mode(self, stream_id: str) -> int:
        binding = self._get_binding(stream_id)
        try:
            mode = int(binding.get("display_mode", 3)) if binding else 3
        except (TypeError, ValueError):
            mode = 3
        return mode if mode in (1, 2, 3) else 3

    def _get_damage_range(self, stream_id: str) -> tuple[int, int]:
        binding = self._get_binding(stream_id)
        if not binding:
            return 0, 0
        try:
            low = max(int(binding.get("damage_low", 0)), 0)
            high = max(int(binding.get("damage_high", 0)), 0)
        except (TypeError, ValueError):
            low, high = 0, 0
        return low, high

    def _get_extra(self, stream_id: str) -> dict[str, bool]:
        binding = self._get_binding(stream_id)
        return normalize_extra(binding.get("extra")) if binding else default_extra()

    def _refresh_ranked_nicknames(self, stream_id: str, ranked: list[dict]) -> list[dict]:
        """用最新绑定数据更新排行榜中的群昵称"""
        for acc in ranked:
            server, account_id = acc.get("server"), acc.get("account_id")
            if server and account_id:
                nick = self._get_group_nickname(stream_id, server, account_id)
                if nick:
                    acc["group_nickname"] = nick
        return ranked

    def _is_account_monitored(self, stream_id: str, server: str, account_id: int) -> bool:
        return self._find_account(stream_id, server, account_id) is not None

    def _monitored_keys(self, stream_id: str) -> set[str]:
        binding = self._get_binding(stream_id)
        if not binding:
            return set()
        return {
            f"{str(a.get('server', '')).upper()}:{int(a.get('account_id', 0))}"
            for a in binding.get("accounts", [])
        }

    # ---------- 生命周期 ----------
    async def on_load(self) -> None:
        self._load_state()
        self._battle_log = BattleLogStore(Path(__file__).parent / "data")
        self.ctx.logger.info("插件已加载，绑定群数=%d，榜单=%s",
                             len(self._state["bindings"]), list(leaderboard.BOARDS.keys()))
        self._poller_task = asyncio.create_task(self._poller_loop())

    async def on_unload(self) -> None:
        self._stopped = True
        if self._poller_task:
            self._poller_task.cancel()
            try:
                await self._poller_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._save_state()
        self.ctx.logger.info("插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info("配置已更新: version=%s", version)

    # ---------- 自然语言查询 ----------

    @Tool(
        "wows_query",
        brief_description="查询战舰世界战绩统计（本群绑定玩家）",
        detailed_description=NL_TOOL_DESCRIPTION,
        parameters=[
            ToolParameterInfo(
                name="player", param_type=ToolParamType.STRING,
                description="玩家群昵称或游戏ID（如'XXX'），不填表示统计本群全部绑定玩家",
                required=False,
            ),
            ToolParameterInfo(
                name="date", param_type=ToolParamType.STRING,
                description="日期或时间段：今天/昨天/这个星期/上周/这个月/上月/最近N天/YYYY-MM-DD，不填默认今天",
                required=False,
            ),
            ToolParameterInfo(
                name="ship_type", param_type=ToolParamType.STRING,
                description="舰种中文名：航母/战列舰/巡洋舰/驱逐舰/潜艇（俗称'小人'），不填表示全部",
                required=False,
            ),
            ToolParameterInfo(
                name="metric", param_type=ToolParamType.STRING,
                description="统计指标：最高伤害/最高击杀/场均伤害/场次/胜场/玩家列表",
                required=True,
            ),
        ],
    )
    async def handle_wows_query(self, player: str = "", date: str = "", ship_type: str = "", metric: str = "", **kwargs: Any):
        """自然语言战绩查询：读本群战斗日志，按玩家/舰种/指标汇总。"""
        stream_id = kwargs.get("stream_id", "")
        binding = self._get_binding(stream_id)
        if not binding or not binding.get("accounts"):
            return {"success": False, "content": "当前群尚未绑定任何账号，无法查询。"}
        if not binding.get("nl_enabled", NL_DEFAULT_ENABLED):
            return {"success": False, "content": "本群已关闭自然语言查询，管理员可用 /开启自然语言查询 开启。"}
        try:
            start, end = self._parse_date_range(date)
            if not self._ship_db.loaded:
                try:
                    await self._ship_db.refresh(self._api)
                except Exception:  # noqa: BLE001
                    self.ctx.logger.warning("舰种库刷新失败，舰种过滤可能不完整")
            records = self._battle_log.get_by_date_range(start, end) if self._battle_log else []
            result = run_query(
                records, self._ship_db, binding.get("accounts", []),
                player=player, ship_type=ship_type, metric=metric,
            )
            return {"success": True, "content": result}
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("自然语言查询失败")
            return {"success": False, "content": "查询失败，请稍后再试。"}

    @staticmethod
    def _parse_date_range(date_text: str) -> tuple[str, str]:
        """把日期说法解析为 (起始日期, 结束日期) %Y-%m-%d；单日则 start==end"""
        today = date.today()
        t = (date_text or "").strip()
        if not t or t in ("今天", "今日"):
            return today.isoformat(), today.isoformat()
        if t in ("昨天", "昨日"):
            d = today - timedelta(days=1)
            return d.isoformat(), d.isoformat()
        if t in ("这个星期", "本周", "这周"):
            start = today - timedelta(days=today.weekday())  # 本周一
            return start.isoformat(), today.isoformat()
        if t in ("上星期", "上周"):
            this_monday = today - timedelta(days=today.weekday())
            last_sunday = this_monday - timedelta(days=1)
            last_monday = this_monday - timedelta(days=7)
            return last_monday.isoformat(), last_sunday.isoformat()
        if t in ("这个月", "本月"):
            return today.replace(day=1).isoformat(), today.isoformat()
        if t in ("上个月", "上月"):
            last_month_end = today.replace(day=1) - timedelta(days=1)
            return last_month_end.replace(day=1).isoformat(), last_month_end.isoformat()
        m = __import__("re").fullmatch(r"最近(\d+)天", t)
        if m:
            n = int(m.group(1))
            return (today - timedelta(days=n - 1)).isoformat(), today.isoformat()
        m = __import__("re").fullmatch(r"(\d{4})[-/]?(\d{1,2})[-/]?(\d{1,2})", t)
        if m:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d.isoformat(), d.isoformat()
        return today.isoformat(), today.isoformat()

    # ---------- 状态存取 ----------
    def _load_state(self) -> None:
        try:
            self._store = StateStore(Path(__file__).parent)
            self._state["bindings"] = self._store.groups.get("bindings", {})
            self._state["snapshots"] = self._store.snapshots
            self._state["daily_king"] = self._store.leaderboard
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("读取状态文件失败，使用空状态")

    def _save_state(self) -> None:
        """退出时全量保存三个数据文件"""
        try:
            self._sync_store()
            self._store.save_groups()
            self._store.save_snapshots()
            self._store.save_leaderboard()
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("保存状态文件失败")

    def _sync_store(self) -> None:
        """把内存 state 同步到 store（不写盘）"""
        self._store.groups["bindings"] = self._state["bindings"]
        self._store.snapshots = self._state["snapshots"]
        self._store.leaderboard = self._state["daily_king"]

    def _save_bindings(self) -> None:
        try:
            self._store.groups["bindings"] = self._state["bindings"]
            self._store.save_groups()
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("保存群配置失败")

    def _save_snapshots(self) -> None:
        try:
            self._store.snapshots = self._state["snapshots"]
            self._store.save_snapshots()
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("保存快照失败")

    def _save_leaderboard(self) -> None:
        try:
            self._store.leaderboard = self._state["daily_king"]
            self._store.save_leaderboard()
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("保存榜单数据失败")

    # ---------- 轮询 ----------
    async def _ensure_ship_map(self) -> None:
        if time.time() - self._ship_map_ts < SHIP_MAP_REFRESH_SECONDS and self._ship_map:
            return
        try:
            self._ship_map = await self._api.fetch_encyclopedia()
            self._ship_map_ts = time.time()
            self.ctx.logger.info("战舰图鉴已刷新: %d 艘", len(self._ship_map))
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("刷新战舰图鉴失败，沿用旧缓存")

    async def _poller_loop(self) -> None:
        await asyncio.sleep(5)
        while not self._stopped:
            try:
                await self._poll_once()
            except Exception:  # noqa: BLE001
                self.ctx.logger.exception("轮询异常")
            await asyncio.sleep(self.config.plugin.poll_interval_minutes * 60)

    def _unique_accounts(self) -> list[tuple[str, int]]:
        seen: set[tuple[str, int]] = set()
        for binding in self._state["bindings"].values():
            for acc in binding.get("accounts", []):
                key = (str(acc.get("server", "")).upper(), int(acc.get("account_id", 0)))
                if key[1] > 0:
                    seen.add(key)
        return sorted(seen)

    def _streams_for_account(self, server: str, account_id: int) -> list[str]:
        return [
            stream_id
            for stream_id, binding in self._state["bindings"].items()
            if not binding.get("paused") and self._find_account(stream_id, server, account_id)
        ]

    def _enabled_types(self) -> list[str]:
        types = [bt for bt in self.config.plugin.enabled_battle_types if bt in VALID_BATTLE_TYPES]
        return types or ["pvp_solo"]

    async def _poll_once(self) -> None:
        if not self.config.plugin.push_enabled:
            return
        if await leaderboard.check_daily_reset(
            self._state, self.ctx, send_html_image, self.ctx.logger,
            get_records_fn=lambda sid, d: self._battle_log.get_by_date(d) if self._battle_log else [],
            is_monitored_fn=self._is_account_monitored,
            get_nickname_fn=self._get_group_nickname,
        ):
            self._save_leaderboard()
        await self._ensure_ship_map()
        accounts = self._unique_accounts()
        if not accounts:
            return
        enabled_types = self._enabled_types()
        for server, account_id in accounts:
            try:
                await self._check_account(server, account_id, enabled_types, push=True)
            except Exception:  # noqa: BLE001
                self.ctx.logger.exception("检查账号 %s:%s 失败", server, account_id)
        # battle 日志暂定长久保存；如需定期清理，恢复此处 cleanup_old 调用
        # if self._battle_log:
        #     removed = self._battle_log.cleanup_old(self.config.plugin.log_retention_days)
        #     if removed > 0:
        #         self.ctx.logger.info("清理过期战斗日志 %d 条", removed)

    async def _check_account(
        self, server: str, account_id: int, battle_types: list[str], push: bool,
        stream_ids: set[str] | None = None,
    ) -> dict[str, list]:
        """拉取账号各对局类型快照，检测新对局、存战斗日志、按需推送"""
        snap_key = f"{server.upper()}:{account_id}"
        old_snap = self._state["snapshots"].get(snap_key) or {}
        old_types = old_snap.get("battle_types") or {}
        new_types: dict[str, dict[int, dict[str, int]]] = {}
        name = old_snap.get("name") or f"Account{account_id}"
        results: dict[str, list] = {}

        for bt in battle_types:
            try:
                name, stats = await self._api.fetch_user_ships(server, account_id, bt)
            except Exception:  # noqa: BLE001
                self.ctx.logger.warning("拉取 %s:%s %s 失败，跳过", server, account_id, bt)
                continue
            new_snap = summarize(stats)
            new_types[bt] = new_snap
            old = old_types.get(bt) or {}
            if not old:
                continue
            diffs = detect_new_battles(old, new_snap)
            if not diffs:
                continue
            results[bt] = list(diffs.values())
            if self._battle_log:
                for ship_id, d in diffs.items():
                    self._battle_log.add_record({
                        "timestamp": time.time(),
                        "date": time.strftime("%Y-%m-%d"),
                        "server": server.upper(),
                        "account_id": account_id,
                        "account_name": name,
                        "ship_id": ship_id,
                        "ship_name": self._ship_map.get(ship_id, f"Ship{ship_id}"),
                        "battle_type": bt,
                        "battles": d.get("battles", 0),
                        "wins": d.get("wins", 0),
                        "losses": d.get("losses", 0),
                        "damage": d.get("damage", 0),
                        "kills": d.get("kills", 0),
                        "xp": d.get("xp", 0),
                        "potential": d.get("potential", 0),
                        "scouting": d.get("scouting", 0),
                        **extract_log_fields(d),
                    })
            if push:
                for ship_id, d in diffs.items():
                    ship_name = self._ship_map.get(ship_id, f"Ship{ship_id}")
                    streams = stream_ids if stream_ids is not None else self._streams_for_account(server, account_id)
                    for stream_id in streams:
                        display_mode = self._get_display_mode(stream_id)
                        if not should_broadcast_type(bt, display_mode):
                            continue
                        low, high = self._get_damage_range(stream_id)
                        if not should_broadcast_damage(d["damage"], low, high):
                            continue
                        extra = self._get_extra(stream_id)
                        display_name = self._get_display_name(stream_id, server, account_id, name)
                        text = format_battle(display_name, ship_name, d, bt, display_mode, extra)
                        if extra.get("record"):
                            broken = check_career_records(old.get(ship_id) or {}, new_snap.get(ship_id) or {})
                            if broken:
                                text = text + "\n" + format_record_break(broken)
                        await self._push_to_stream(text, stream_id)

        if new_types:
            self._state["snapshots"][snap_key] = {"name": name, "battle_types": new_types, "updated": time.time()}
            self._save_snapshots()
        return results

    async def _refresh_snapshots_for_stream(self, stream_id: str) -> None:
        """静默刷新该群所有账号的快照（暂停恢复时跳过历史战绩）"""
        binding = self._get_binding(stream_id)
        if not binding or not binding.get("accounts"):
            return
        await self._ensure_ship_map()
        enabled_types = self._enabled_types()
        for acc in binding["accounts"]:
            server = str(acc.get("server", "")).upper()
            account_id = int(acc.get("account_id", 0))
            if account_id <= 0:
                continue
            snap_key = f"{server}:{account_id}"
            new_types: dict[str, dict[int, dict[str, int]]] = {}
            name = self._state.get("snapshots", {}).get(snap_key, {}).get("name") or f"Account{account_id}"
            for bt in enabled_types:
                try:
                    name, stats = await self._api.fetch_user_ships(server, account_id, bt)
                    new_types[bt] = summarize(stats)
                except Exception:  # noqa: BLE001
                    self.ctx.logger.warning("恢复刷新快照失败 %s:%s %s", server, account_id, bt)
                    continue
            if new_types:
                self._state.setdefault("snapshots", {})[snap_key] = {
                    "name": name, "battle_types": new_types, "updated": time.time()
                }
        self._save_snapshots()

    async def _push_to_stream(self, text: str, stream_id: str) -> None:
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("推送失败 stream=%s", stream_id)

    # ---------- 命令 ----------
    async def _reply(self, stream_id: str, text: str) -> tuple[bool, str, int]:
        await self.ctx.send.text(text, stream_id)
        return True, text, 1

    async def _reply_image(self, stream_id: str, html: str, fallback_text: str) -> tuple[bool, str, int]:
        return await send_html_image(self.ctx, stream_id, html, fallback_text, self.ctx.logger)

    async def _denied(self) -> tuple[bool, str, int]:
        return True, "", 0

    @Command("wows_help", pattern=r"^/wows(\s+help)?$")
    async def cmd_help(self, **kwargs):
        stream_id = kwargs["stream_id"]
        return await self._reply_image(stream_id, build_help_html(), HELP_TEXT)

    @Command("wows_admin_help", pattern=r"^/wows\s+adminhelp$")
    async def cmd_admin_help(self, **kwargs):
        if not await self._check_permission("wows_admin_help", kwargs):
            return await self._denied()
        stream_id = kwargs["stream_id"]
        return await self._reply_image(stream_id, build_admin_help_html(), ADMIN_HELP_TEXT)

    @Command("wows_lbhelp", pattern=r"^/wows\s+lbhelp$")
    async def cmd_lbhelp(self, **kwargs):
        stream_id = kwargs["stream_id"]
        return await self._reply_image(stream_id, build_lbhelp_html(), lbhelp_text())

    @Command("cn_lbhelp", pattern=r"^/排行榜帮助$")
    async def cn_lbhelp(self, **kwargs):
        return await self.cmd_lbhelp(**kwargs)

    @Command("wows_on", pattern=r"^/wows\s+on$")
    async def cmd_on(self, **kwargs):
        if not await self._check_permission("wows_on", kwargs):
            return await self._denied()
        stream_id = kwargs["stream_id"]
        bindings = self._state.setdefault("bindings", {})
        if stream_id not in bindings:
            bindings[stream_id] = {
                "accounts": [], "display_mode": 3,
                "damage_low": 0, "damage_high": 0,
                "extra": default_extra(), "paused": False,
            }
            self._save_bindings()
            text = "本群开启战绩推送，可用 /wows add <服务器> <账号ID> 添加播报账号"
        else:
            binding = bindings[stream_id]
            if binding.get("paused"):
                binding["paused"] = False
                self._save_bindings()
                await self._refresh_snapshots_for_stream(stream_id)
                text = "已恢复战绩推送"
            else:
                text = "本群已开启战绩推送"
        return await self._reply(stream_id, text)

    @Command("wows_off", pattern=r"^/wows\s+off$")
    async def cmd_off(self, **kwargs):
        if not await self._check_permission("wows_off", kwargs):
            return await self._denied()
        stream_id = kwargs["stream_id"]
        binding = self._get_binding(stream_id)
        if not binding:
            return await self._reply(stream_id, "本群尚未开启推送")
        if binding.get("paused"):
            return await self._reply(stream_id, "服务已暂停")
        binding["paused"] = True
        self._save_bindings()
        return await self._reply(stream_id, "已暂停战绩推送，/wows on 可恢复")

    @Command("wows_pause", pattern=r"^/wows\s+pause$")
    async def cmd_pause(self, **kwargs):
        return await self.cmd_off(**kwargs)

    @Command("wows_add", pattern=r"^/wows\s+add\s+(?P<server>\S+)\s+(?P<account>\S+)$")
    async def cmd_add(self, **kwargs):
        stream_id = kwargs["stream_id"]
        groups = kwargs.get("matched_groups") or {}
        server = normalize_server(groups.get("server", ""))
        account = groups.get("account", "").strip()

        if server not in SERVER_VORTEX:
            return await self._reply(stream_id, f"服务器参数无效：{server}（可用 CN/ASIA/EU/NA/RU 或 国服/亚服/欧服/美服/俄服）")

        # 数字直接当 UID，非数字调用昵称搜索
        if account.isdigit():
            account_id, game_name = int(account), account
        else:
            result = await self._api.search_player(server, account)
            if result is None:
                return await self._reply(stream_id, f"未找到玩家：{account}（请检查昵称和服务器）")
            account_id, game_name = result

        if account_id <= 0:
            return await self._reply(stream_id, "账号ID格式错误")

        binding = self._get_binding(stream_id)
        if binding is None:
            return await self._reply(stream_id, "请先由管理员发送 /wows on 开启功能，再添加账号")
        accounts = binding.setdefault("accounts", [])
        for acc in accounts:
            if str(acc.get("server", "")).upper() == server and int(acc.get("account_id", 0)) == account_id:
                return await self._reply(stream_id, f"账号 {server} {account_id} 已在监控列表中")
        accounts.append({"server": server, "account_id": account_id, "nickname": ""})
        self._save_bindings()
        text = f"已添加监控账号 {server} {account_id}" if account.isdigit() else f"已添加监控账号 {server} {game_name}（UID: {account_id}）"
        return await self._reply(stream_id, text)

    @Command("wows_remove", pattern=r"^/wows\s+remove\s+(?P<server>\S+)\s+(?P<account_id>\d+)$")
    async def cmd_remove(self, **kwargs):
        stream_id = kwargs["stream_id"]
        groups = kwargs.get("matched_groups") or {}
        server = normalize_server(groups.get("server", ""))
        account_id = int(groups.get("account_id", "0") or 0)
        binding = self._get_binding(stream_id)
        if not binding:
            return await self._reply(stream_id, "尚未绑定，无需移除")
        accounts = binding.get("accounts", [])
        new_accounts = [
            a for a in accounts
            if not (str(a.get("server", "")).upper() == server and int(a.get("account_id", 0)) == account_id)
        ]
        if len(new_accounts) == len(accounts):
            return await self._reply(stream_id, f"未找到账号 {server} {account_id}")
        binding["accounts"] = new_accounts
        self._save_bindings()
        return await self._reply(stream_id, f"已移除账号 {server} {account_id}")

    @Command("wows_list", pattern=r"^/wows\s+list$")
    async def cmd_list(self, **kwargs):
        stream_id = kwargs["stream_id"]
        binding = self._get_binding(stream_id)
        fallback = "本群暂无监控账号。先 /wows on 开启功能再添加"
        if binding and binding.get("accounts"):
            lines = ["本群监控账号："]
            for acc in binding["accounts"]:
                server = str(acc.get("server", "")).upper()
                aid = int(acc.get("account_id", 0))
                game_name = self._get_game_name(server, aid)
                nick = str(acc.get("nickname") or "")
                lines.append(f"· {server} | UID:{aid} | 游戏ID:{game_name} | 群昵称:{nick if nick else '无'}")
            fallback = "\n".join(lines)
        pages = build_list_html_pages(binding, self._state.get("snapshots", {}))
        for idx, html in enumerate(pages):
            await self._reply_image(stream_id, html, fallback if idx == 0 else "")
        return True, "", len(pages)

    @Command("wows_status", pattern=r"^/wows\s+status$")
    async def cmd_status(self, **kwargs):
        if not await self._check_permission("wows_status", kwargs):
            return await self._denied()
        stream_id = kwargs["stream_id"]
        binding = self._get_binding(stream_id)
        if not binding:
            return await self._reply(stream_id, "本群尚未开启，请先 /wows on")
        mode = self._get_display_mode(stream_id)
        mode_text = MODE_TEXT.get(mode, str(mode))
        dmg_low, dmg_high = self._get_damage_range(stream_id)
        extra = binding.get("extra", {})
        kd = get_king_data(self._state, stream_id)
        board_enabled = kd["enabled"]
        extra_text = " ".join(f"{k}={'开' if v else '关'}" for k, v in extra.items())
        board_text = " ".join(f"{leaderboard.BOARDS[key]['title_cn']}={'开' if v else '关'}" for key, v in board_enabled.items())
        fallback = (
            f"推送状态：{'暂停' if binding.get('paused') else '运行中'}\n"
            f"自然语言查询：{'开启' if binding.get('nl_enabled', NL_DEFAULT_ENABLED) else '关闭'}\n"
            f"榜单开关：{board_text}\n"
            f"显示模式：{mode}（{mode_text}）\n"
            f"伤害范围：≤{dmg_low} 或 ≥{dmg_high}\n"
            f"额外播报：{extra_text}\n"
            f"监控账号：{len(binding.get('accounts', []))} 个"
        )
        return await self._reply_image(
            stream_id,
            build_status_html(binding, board_enabled, bool(binding.get("nl_enabled", NL_DEFAULT_ENABLED))),
            fallback,
        )

    @Command("wows_check", pattern=r"^/wows\s+check$")
    async def cmd_check(self, **kwargs):
        stream_id = kwargs["stream_id"]
        binding = self._get_binding(stream_id)
        if not binding or not binding.get("accounts"):
            return await self._reply(stream_id, "本群暂无监控账号")
        await self._ensure_ship_map()
        enabled_types = self._enabled_types()
        has_new = False
        for acc in binding["accounts"]:
            server = str(acc.get("server", "")).upper()
            account_id = int(acc.get("account_id", 0))
            try:
                results = await self._check_account(
                    server, account_id, enabled_types, push=True, stream_ids={stream_id}
                )
                if any(results.values()):
                    has_new = True
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.exception("手动检查失败 %s:%s", server, account_id)
        if not has_new:
            return await self._reply(stream_id, "检查完成：当前无新对局")

    @Command("wows_nick", pattern=r"^/wows\s+nick\s+(?P<server>\S+)\s+(?P<account_id>\d+)(?:\s+(?P<nickname>.+))?$")
    async def cmd_nick(self, **kwargs):
        if not await self._check_permission("wows_nick", kwargs):
            return await self._denied()
        stream_id = kwargs["stream_id"]
        groups = kwargs.get("matched_groups") or {}
        server = normalize_server(groups.get("server", ""))
        try:
            account_id = int(groups.get("account_id", "0"))
        except ValueError:
            account_id = 0
        nickname = (groups.get("nickname") or "").strip()
        if len(nickname) > 16:
            return await self._reply(stream_id, "昵称限制16字符")
        if server not in SERVER_VORTEX:
            return await self._reply(stream_id, f"服务器参数无效：{server}（可用 {', '.join(SERVER_VORTEX)}）")
        if account_id <= 0:
            return await self._reply(stream_id, "账号ID格式错误")
        acc = self._find_account(stream_id, server, account_id)
        if acc is None:
            return await self._reply(stream_id, f"未找到账号 {server} {account_id}，请先 /wows add 添加")
        if nickname:
            acc["nickname"] = nickname
            text = f"已设置 {server} {account_id} 的群内播报昵称为：{nickname}"
        else:
            acc["nickname"] = ""
            text = f"已清除 {server} {account_id} 的群内播报昵称，将使用游戏ID"
        self._save_bindings()
        return await self._reply(stream_id, text)

    @Command("wows_mode", pattern=r"^/wows\s+mode\s+(?P<mode>[123])$")
    async def cmd_mode(self, **kwargs):
        if not await self._check_permission("wows_mode", kwargs):
            return await self._denied()
        stream_id = kwargs["stream_id"]
        groups = kwargs.get("matched_groups") or {}
        mode = int(groups.get("mode", "3"))
        if mode not in (1, 2, 3):
            return await self._reply(stream_id, "模式参数无效，仅支持 1/2/3")
        binding = self._get_binding(stream_id)
        if not binding:
            return await self._reply(stream_id, "本群尚未开启，请先 /wows on")
        binding["display_mode"] = mode
        self._save_bindings()
        desc = {
            1: "只播报单野/排位，不显示类型标签",
            2: "播报单野/双排/三排/排位，显示具体类型",
            3: "播报所有类型（含人机），显示详细类型",
        }
        return await self._reply(stream_id, f"已设置本群播报显示模式为 {mode}：{desc[mode]}")

    @Command("wows_range", pattern=r"^/wows\s+range\s+(?P<low>\d+)\s+(?P<high>\d+)$")
    async def cmd_range(self, **kwargs):
        if not await self._check_permission("wows_range", kwargs):
            return await self._denied()
        stream_id = kwargs["stream_id"]
        groups = kwargs.get("matched_groups") or {}
        low = int(groups.get("low", "0"))
        high = int(groups.get("high", "0"))
        if low < 0 or high < 0:
            return await self._reply(stream_id, "伤害数值不能为负数")
        if low > 0 and high > 0 and low >= high:
            return await self._reply(stream_id, "低阈值必须小于高阈值")
        binding = self._get_binding(stream_id)
        if not binding:
            return await self._reply(stream_id, "本群尚未开启，请先 /wows on")
        binding["damage_low"] = low
        binding["damage_high"] = high
        self._save_bindings()
        if low == 0 and high == 0:
            text = "已清除伤害范围过滤，所有伤害均播报"
        else:
            parts = []
            if low > 0:
                parts.append(f"低于{low}播报")
            if high > 0:
                parts.append(f"高于{high}播报")
            text = f"已设置伤害范围：{'，'.join(parts)}"
        return await self._reply(stream_id, text)

    @Command("wows_extra", pattern=r"^/wows\s+extra\s+(?P<args>.+)$")
    async def cmd_extra(self, **kwargs):
        if not await self._check_permission("wows_extra", kwargs):
            return await self._denied()
        stream_id = kwargs["stream_id"]
        groups = kwargs.get("matched_groups") or {}
        args = (groups.get("args") or "").strip().split()
        if len(args) < 2:
            return await self._reply(stream_id, "用法：/wows extra <项1> [项2...] <on|off>")
        action = args[-1].lower()
        if action not in ("on", "off"):
            return await self._reply(stream_id, "最后一个参数必须是 on 或 off")
        items = [a.lower() for a in args[:-1]]
        invalid = [it for it in items if it not in EXTRA_ITEMS]
        if invalid:
            return await self._reply(stream_id, f"无效项：{', '.join(invalid)}（可用：{', '.join(EXTRA_ITEMS)}）")
        binding = self._get_binding(stream_id)
        if not binding:
            return await self._reply(stream_id, "本群尚未开启，请先 /wows on")
        extra = binding.setdefault("extra", default_extra())
        for it in items:
            extra[it] = action == "on"
        self._save_bindings()
        labels = [EXTRA_ITEMS[it] for it in items]
        return await self._reply(stream_id, f"已{'开启' if action == 'on' else '关闭'}额外播报：{'、'.join(labels)}")

    async def _cmd_nl_toggle(self, stream_id: str, value: bool) -> tuple[bool, str, int]:
        binding = self._get_binding(stream_id)
        if not binding:
            return await self._reply(stream_id, "本群尚未开启，请先 /wows on")
        binding["nl_enabled"] = value
        self._save_bindings()
        return await self._reply(stream_id, f"已{'开启' if value else '关闭'}自然语言查询(beta)")

    @Command("wows_nl", pattern=r"^/wows\s+nl\s+(?P<action>on|off)$")
    async def cmd_nl(self, **kwargs):
        if not await self._check_permission("wows_nl", kwargs):
            return await self._denied()
        groups = kwargs.get("matched_groups") or {}
        value = (groups.get("action") or "").lower() == "on"
        return await self._cmd_nl_toggle(kwargs["stream_id"], value)

    @Command("cn_nl_on", pattern=r"^/开启自然语言查询$")
    async def cn_nl_on(self, **kwargs):
        if not await self._check_permission("cn_nl_on", kwargs):
            return await self._denied()
        return await self._cmd_nl_toggle(kwargs["stream_id"], True)

    @Command("cn_nl_off", pattern=r"^/关闭自然语言查询$")
    async def cn_nl_off(self, **kwargs):
        if not await self._check_permission("cn_nl_off", kwargs):
            return await self._denied()
        return await self._cmd_nl_toggle(kwargs["stream_id"], False)

    # ---------- 榜单命令（由注册表动态注册，此处提供通用分发） ----------

    async def _dispatch_board(self, board_key: str, spec: dict, kwargs: dict) -> tuple[bool, str, int]:
        """榜单命令统一分发：查看/历史/月榜/开关"""
        stream_id = kwargs["stream_id"]
        if not spec["public"] and not await self._check_permission(spec["name"], kwargs):
            return await self._denied()
        action = spec["action"]
        if action == "view":
            return await self._cmd_board_view(board_key, stream_id)
        if action == "history":
            date_iso = parse_date((kwargs.get("matched_groups") or {}).get("date", ""))
            return await self._cmd_board_history(board_key, stream_id, date_iso)
        if action == "month":
            return await self._cmd_board_month(board_key, stream_id)
        if action == "toggle":
            value = spec.get("toggle_value")
            if value is None:
                value = (kwargs.get("matched_groups") or {}).get("action", "") == "on"
            return await self._cmd_board_toggle(board_key, stream_id, bool(value))
        return await self._denied()

    async def _cmd_board_view(self, board_key: str, stream_id: str) -> tuple[bool, str, int]:
        board = leaderboard.BOARDS[board_key]
        kd = get_king_data(self._state, stream_id)
        if not kd["enabled"].get(board_key, False):
            toggle = [s for s in cmd_specs(board_key, board) if s["action"] == "toggle"]
            cmd = toggle[0]["cmd"] if toggle else f"/wows {board_key} on"
            return await self._reply(stream_id, f"本群未开启{board['title_cn']}榜，管理员可使用 {cmd} 开启")
        today = leaderboard.today_str()
        day_records = self._battle_log.get_by_date(today) if self._battle_log else []
        ranked = board["rank_fn"](day_records, self._monitored_keys(stream_id), self._get_group_nickname, stream_id)
        self._refresh_ranked_nicknames(stream_id, ranked)
        html = board["build_html_fn"](ranked, today, kd.get("last", {}).get(board_key), PERIOD_DAILY)
        return await self._reply_image(stream_id, html, "今日暂无有效战绩")

    async def _cmd_board_history(self, board_key: str, stream_id: str, date_iso: str | None) -> tuple[bool, str, int]:
        board = leaderboard.BOARDS[board_key]
        if not date_iso:
            return await self._reply(stream_id, "日期格式错误，如 /wows king 20260831")
        kd = get_king_data(self._state, stream_id)
        ranked = get_history(kd, board_key, date_iso)
        if not ranked and self._battle_log:
            # 兜底检查器：history 无该日固定榜时，从战斗日志实时计算
            day_records = self._battle_log.get_by_date(date_iso)
            ranked = board["rank_fn"](day_records, self._monitored_keys(stream_id), self._get_group_nickname, stream_id)
        if not ranked:
            return await self._reply(stream_id, f"{date_iso} 暂无{board['title_cn']}榜数据")
        self._refresh_ranked_nicknames(stream_id, ranked)
        html = board["build_html_fn"](ranked, date_iso, None, PERIOD_DAILY)
        return await self._reply_image(stream_id, html, f"{date_iso} {board['title_cn']}榜")

    async def _cmd_board_month(self, board_key: str, stream_id: str) -> tuple[bool, str, int]:
        board = leaderboard.BOARDS[board_key]
        if not board.get("supports_month") or not board.get("build_month_fn"):
            return await self._reply(stream_id, f"{board['title_cn']}榜不支持月榜")
        kd = get_king_data(self._state, stream_id)
        monthly = kd.get("monthly", {}).get(month_str(), {}).get(board_key, {})
        ranked = board["month_rank_fn"](monthly)
        if not ranked:
            return await self._reply(stream_id, f"本月暂无{board['title_cn']}榜数据")
        self._refresh_ranked_nicknames(stream_id, ranked)
        html = board["build_month_fn"](ranked, month_str())
        return await self._reply_image(stream_id, html, "本月暂无数据")

    async def _cmd_board_toggle(self, board_key: str, stream_id: str, value: bool) -> tuple[bool, str, int]:
        board = leaderboard.BOARDS[board_key]
        kd = get_king_data(self._state, stream_id)
        kd["enabled"][board_key] = value
        self._save_leaderboard()
        return await self._reply(stream_id, f"已{'开启' if value else '关闭'}本群{board['title_cn']}榜")

    # ---------- 中文命令别名 ----------
    @Command("cn_help", pattern=r"^/帮助$")
    async def cn_help(self, **kwargs): return await self.cmd_help(**kwargs)

    @Command("cn_admin_help", pattern=r"^/管理员帮助$")
    async def cn_admin_help(self, **kwargs): return await self.cmd_admin_help(**kwargs)

    @Command("cn_add", pattern=r"^/添加\s+(?P<server>\S+)\s+(?P<account>\S+)$")
    async def cn_add(self, **kwargs): return await self.cmd_add(**kwargs)

    @Command("cn_remove", pattern=r"^/移除\s+(?P<server>\S+)\s+(?P<account_id>\d+)$")
    async def cn_remove(self, **kwargs): return await self.cmd_remove(**kwargs)

    @Command("cn_list", pattern=r"^/列表$")
    async def cn_list(self, **kwargs): return await self.cmd_list(**kwargs)

    @Command("cn_check", pattern=r"^/检查$")
    async def cn_check(self, **kwargs): return await self.cmd_check(**kwargs)

    @Command("cn_on", pattern=r"^/开启$")
    async def cn_on(self, **kwargs): return await self.cmd_on(**kwargs)

    @Command("cn_off", pattern=r"^/暂停$")
    async def cn_off(self, **kwargs): return await self.cmd_off(**kwargs)

    @Command("cn_nick", pattern=r"^/昵称\s+(?P<server>\S+)\s+(?P<account_id>\d+)(?:\s+(?P<nickname>.+))?$")
    async def cn_nick(self, **kwargs): return await self.cmd_nick(**kwargs)

    @Command("cn_mode", pattern=r"^/模式\s+(?P<mode>[123])$")
    async def cn_mode(self, **kwargs): return await self.cmd_mode(**kwargs)

    @Command("cn_range", pattern=r"^/伤害范围\s+(?P<low>\d+)\s+(?P<high>\d+)$")
    async def cn_range(self, **kwargs): return await self.cmd_range(**kwargs)

    @Command("cn_extra", pattern=r"^/额外播报\s+(?P<args>.+)$")
    async def cn_extra(self, **kwargs): return await self.cmd_extra(**kwargs)

    @Command("cn_status", pattern=r"^/状态$")
    async def cn_status(self, **kwargs): return await self.cmd_status(**kwargs)


def _make_board_handler(key: str, spec: dict):
    """榜单命令处理器工厂：通过闭包捕获 key/spec，避免循环变量共享"""
    async def method(self, **kwargs):
        return await self._dispatch_board(key, spec, kwargs)
    return method


def _install_board_commands(cls) -> None:
    """遍历榜单注册表，动态挂载每个榜单的全部命令"""
    for key, board in leaderboard.BOARDS.items():
        for spec in cmd_specs(key, board):
            method_name = spec["method"]
            method = _make_board_handler(key, spec)
            method.__name__ = method_name
            method = Command(spec["name"], pattern=spec["pattern"])(method)
            setattr(cls, method_name, method)


# 合并榜单公开命令到 PUBLIC_COMMANDS
for _key, _board in leaderboard.BOARDS.items():
    for _spec in cmd_specs(_key, _board):
        if _spec["public"]:
            PUBLIC_COMMANDS.add(_spec["name"])

_install_board_commands(WowsBattlePushPlugin)


def create_plugin():
    return WowsBattlePushPlugin()
