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
from datetime import date, timedelta
from pathlib import Path

# 确保插件目录在 sys.path 中，使同目录模块可导入
sys.path.insert(0, str(Path(__file__).parent))

import asyncio
import re
import time
from typing import Any

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Command, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from wows_api import WowsApi
from wows_battle_log import BattleLogStore, extract_log_fields
from wows_config import PluginConfig
from wows_constants import (
    EXTRA_ITEMS,
    MODE_TEXT,
    PUBLIC_COMMANDS,
    SERVER_VORTEX,
    SHIP_MAP_REFRESH_SECONDS,
    VALID_BATTLE_TYPES,
    normalize_server,
)
from wows_nl_query import (
    DEFAULT_ENABLED as NL_DEFAULT_ENABLED,
    TOOL_DESCRIPTION as NL_TOOL_DESCRIPTION,
    run_query,
)
from wows_ship_db import ShipDb
from wows_state_store import StateStore
from wows_cards import (
    HELP_TEXT,
    ADMIN_HELP_TEXT,
    build_help_html,
    build_admin_help_html,
    build_status_html,
    build_list_html_pages,
)
import wows_leaderboard as leaderboard
from wows_leaderboard import (
    PERIOD_DAILY,
    build_lbhelp_html,
    cmd_specs,
    get_history,
    get_stream_data as get_king_data,
    lbhelp_text,
    month_str,
    parse_date,
)
from wows_stats import (
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
from wows_utils import send_html_image

# 手动检查只推当前群时，其他群的待补发事件保留时间与数量上限
PENDING_TTL_SECONDS = 7 * 24 * 3600
MAX_PENDING_EVENTS = 500


class WowsBattlePushPlugin(MaiBotPlugin):
    config_model = PluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._api = WowsApi()
        self._state: dict[str, Any] = {
            "version": 1, "bindings": {}, "snapshots": {}, "daily_king": {}, "pending": [],
        }
        self._battle_log: BattleLogStore | None = None
        self._store: StateStore | None = None
        self._poller_task: asyncio.Task | None = None
        self._ship_db_task: asyncio.Task | None = None
        self._stopped = False
        self._last_snap_save: float = 0.0
        self._ship_db = ShipDb(Path(__file__).parent / "data" / "ship_db.json")
        self._ship_db_ts: float = 0.0
        # 每账号一把锁：保证"读基线 → 差分 → 落日志 → 推播报 → 更新基线"只跑一次。
        # 手动检查与后台轮询共用同一把锁，避免同一次差分被写两遍日志/推两遍。
        self._account_locks: dict[str, asyncio.Lock] = {}

    # ---------- 账号级互斥 ----------
    def _account_lock(self, server: str, account_id: int) -> asyncio.Lock:
        key = f"{server.upper()}:{account_id}"
        lock = self._account_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._account_locks[key] = lock
        return lock

    def _is_account_busy(self, server: str, account_id: int) -> bool:
        lock = self._account_locks.get(f"{server.upper()}:{account_id}")
        return bool(lock and lock.locked())

    # ---------- 权限 ----------
    def _get_sender_id(self, kwargs: dict[str, Any]) -> str:
        """从 Host 注入参数中提取发送者 QQ。

        当前 Host 会直接传 ``user_id``；同时兼容文档中的 ``message`` 消息对象
        以及旧版本可能传入的 ``sender`` / ``from_user`` 等字段。
        """
        candidates: list[Any] = [
            kwargs.get("sender_id"),
            kwargs.get("user_id"),
            kwargs.get("qq"),
            kwargs.get("from_user_id"),
            kwargs.get("from_user"),
            kwargs.get("sender"),
            kwargs.get("user"),
        ]

        message = kwargs.get("message")
        if isinstance(message, dict):
            message_info = message.get("message_info")
            if isinstance(message_info, dict):
                user_info = message_info.get("user_info")
                if isinstance(user_info, dict):
                    candidates.extend([user_info.get("user_id"), user_info.get("id"), user_info.get("qq")])
            candidates.extend([message.get("user_id"), message.get("sender_id")])
            sender = message.get("sender") or message.get("user")
            if isinstance(sender, dict):
                candidates.extend([sender.get("id"), sender.get("qq"), sender.get("user_id")])

        raw_message = kwargs.get("raw_message")
        if isinstance(raw_message, dict):
            candidates.extend([raw_message.get("user_id"), raw_message.get("sender_id")])

        for value in candidates:
            if isinstance(value, dict):
                for key in ("user_id", "id", "qq", "userid"):
                    nested = value.get(key)
                    if nested:
                        return str(nested)
            elif value:
                return str(value)
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

    def _is_stream_paused(self, stream_id: str) -> bool:
        """未知聊天流按暂停处理；暂停群只停推送，不停结算。"""
        binding = self._get_binding(stream_id)
        return not binding or bool(binding.get("paused"))

    def _is_stream_active_for_account(self, stream_id: str, server: str, account_id: int) -> bool:
        """该 stream 当前是否仍应接收此账号的普通播报。"""
        return not self._is_stream_paused(stream_id) and self._is_account_monitored(stream_id, server, account_id)

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
        leaderboard.set_ship_type_resolver(self._ship_db.ship_type_en)
        self.ctx.logger.info("插件已加载，绑定群数=%d，榜单=%s",
                             len(self._state["bindings"]), list(leaderboard.BOARDS.keys()))
        self._last_snap_save = time.time()
        self._ship_db_task = asyncio.create_task(self._ensure_ship_db())
        self._poller_task = asyncio.create_task(self._poller_loop())

    async def on_unload(self) -> None:
        self._stopped = True
        for task in (self._poller_task, self._ship_db_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        if self._battle_log:
            self._battle_log.flush()
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
        core_tool=True,
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
        # "我/自己/本人"指代发送者：经 add ... me 的 QQ 绑定解析到 UID
        player = (player or "").strip()
        if player in ("我", "自己", "本人", "我自己"):
            qq = self._get_sender_id(kwargs)
            qb = binding.get("qq_bindings") or {}
            mine = qb.get(qq) if qq else None
            if not mine:
                return {"success": False,
                        "content": "尚未找到你的绑定：请先用 /add <服务器> <UID> me 完成 QQ 绑定，或用群昵称/游戏ID查询。"}
            player = str(mine.get("account_id"))
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
                scope=(date or "今天"),
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
        m = re.fullmatch(r"最近(\d+)天", t)
        if m:
            n = int(m.group(1))
            return (today - timedelta(days=n - 1)).isoformat(), today.isoformat()
        m = re.fullmatch(r"(\d{4})[-/]?(\d{1,2})[-/]?(\d{1,2})", t)
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
            self._state["pending"] = self._store.pending
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("读取状态文件失败，使用空状态")

    def _save_state(self) -> None:
        """退出时全量保存数据文件"""
        try:
            self._sync_store()
            for name, ok in (
                ("群配置", self._store.save_groups()),
                ("快照", self._store.save_snapshots()),
                ("榜单", self._store.save_leaderboard()),
                ("待补发事件", self._store.save_pending()),
            ):
                if not ok:
                    self.ctx.logger.error("保存%s失败", name)
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("保存状态文件失败")

    def _sync_store(self) -> None:
        """把内存 state 同步到 store（不写盘）"""
        self._store.groups["bindings"] = self._state["bindings"]
        self._store.snapshots = self._state["snapshots"]
        self._store.leaderboard = self._state["daily_king"]
        self._store.pending = self._state["pending"]

    def _save_pending(self) -> None:
        try:
            self._store.pending = self._state["pending"]
            if not self._store.save_pending():
                self.ctx.logger.error("保存待补发事件失败")
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("保存待补发事件失败")

    def _save_bindings(self) -> None:
        try:
            self._store.groups["bindings"] = self._state["bindings"]
            if not self._store.save_groups():
                self.ctx.logger.error("保存群配置失败")
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("保存群配置失败")

    def _save_snapshots(self) -> None:
        try:
            self._store.snapshots = self._state["snapshots"]
            if not self._store.save_snapshots():
                self.ctx.logger.error("保存快照失败")
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("保存快照失败")

    def _save_leaderboard(self) -> None:
        try:
            self._store.leaderboard = self._state["daily_king"]
            if not self._store.save_leaderboard():
                self.ctx.logger.error("保存榜单数据失败")
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("保存榜单数据失败")

    # ---------- 轮询 ----------
    async def _ensure_ship_db(self) -> None:
        """确保舰种库已加载（6小时刷新一次）"""
        if self._ship_db.loaded and time.time() - self._ship_db_ts < SHIP_MAP_REFRESH_SECONDS:
            return
        try:
            await self._ship_db.refresh(self._api)
            self._ship_db_ts = time.time()
            self.ctx.logger.info("舰种库已刷新: %d 艘", self._ship_db.size)
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("刷新舰种库失败，沿用旧缓存")

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
        try:
            # 跨天结算与推送开关解耦：暂停/关闭推送时仍要补 history 与月度
            if await leaderboard.check_daily_reset(
                self._state, self.ctx, send_html_image, self.ctx.logger,
                get_records_fn=lambda sid, d: self._battle_log.get_by_date(d) if self._battle_log else [],
                is_monitored_fn=self._is_account_monitored,
                get_nickname_fn=self._get_group_nickname,
                is_paused_fn=self._is_stream_paused,
                send_push=self.config.plugin.push_enabled,
            ):
                self._save_leaderboard()

            if not self.config.plugin.push_enabled:
                # 关闭推送期间不保留待补发，避免重新开启时补发旧战绩
                if self._pending_events():
                    self._state["pending"] = []
                    self._save_pending()
                return

            await self._drain_pending()
            await self._ensure_ship_db()
            accounts = self._unique_accounts()
            if not accounts:
                return
            enabled_types = self._enabled_types()
            # 账号间并发拉取，信号量限流避免打爆 Vortex；账号内对局类型仍串行
            sem = asyncio.Semaphore(self.config.plugin.poll_concurrency)

            async def _check_guarded(server: str, account_id: int) -> None:
                async with sem:
                    try:
                        await self._check_account(server, account_id, enabled_types, push=True)
                    except Exception:  # noqa: BLE001
                        self.ctx.logger.exception("检查账号 %s:%s 失败", server, account_id)

            await asyncio.gather(*(_check_guarded(s, a) for s, a in accounts))
            # battle 日志暂定长久保存；如需定期清理，恢复此处 cleanup_old 调用
            # if self._battle_log:
            #     removed = self._battle_log.cleanup_old(self.config.plugin.log_retention_days)
            #     if removed > 0:
            #         self.ctx.logger.info("清理过期战斗日志 %d 条", removed)
        finally:
            # 每轮结束统一写盘战斗日志；快照每小时兜底写盘一次
            if self._battle_log:
                self._battle_log.flush()
            if time.time() - self._last_snap_save > 3600:
                self._save_snapshots()
                self._last_snap_save = time.time()

    # ---------- 待补发事件（手动检查只推当前群时，其他群稍后补收） ----------
    def _pending_events(self) -> list[dict[str, Any]]:
        pending = self._state.get("pending")
        if not isinstance(pending, list):
            pending = []
            self._state["pending"] = pending
        return pending

    def _enqueue_pending(self, event: dict[str, Any], stream_ids: set[str] | list[str]) -> None:
        streams = {str(stream_id) for stream_id in stream_ids if stream_id}
        if not streams:
            return
        pending = self._pending_events()
        event_id = str(event.get("id") or "")
        for existing in pending:
            if not isinstance(existing, dict):
                continue
            if event_id and existing.get("id") == event_id:
                existing_streams = {str(s) for s in (existing.get("pending_streams") or []) if s}
                existing_streams.update(streams)
                existing["pending_streams"] = sorted(existing_streams)
                self._save_pending()
                return
        payload = dict(event)
        payload["pending_streams"] = sorted(streams)
        payload.setdefault("created", time.time())
        pending.append(payload)
        if len(pending) > MAX_PENDING_EVENTS:
            del pending[:-MAX_PENDING_EVENTS]
        self._save_pending()

    async def _drain_pending(self) -> None:
        """把待补发事件发送给后来恢复/仍在监控的群；暂停或移除的群直接丢弃。"""
        pending = self._pending_events()
        if not pending:
            return
        now = time.time()
        kept: list[dict[str, Any]] = []
        changed = False
        for event in pending:
            if not isinstance(event, dict):
                changed = True
                continue
            try:
                created = float(event.get("created", 0) or 0)
                server = str(event.get("server", "")).upper()
                account_id = int(event.get("account_id", 0))
            except (TypeError, ValueError):
                changed = True
                continue
            if now - created > PENDING_TTL_SECONDS:
                changed = True
                continue

            streams = {str(s) for s in (event.get("pending_streams") or []) if s}
            remaining: list[str] = []
            for stream_id in sorted(streams):
                if not self._is_stream_active_for_account(stream_id, server, account_id):
                    changed = True
                    continue
                ok = await self._push_event_to_stream(event, stream_id)
                changed = True
                if not ok:
                    remaining.append(stream_id)
            if remaining:
                event["pending_streams"] = sorted(set(remaining))
                kept.append(event)
            else:
                changed = True

        # 原地替换：drain 过程中可能有新事件入队，直接赋值会把这些新事件丢掉
        pending[:] = kept
        if changed:
            self._save_pending()

    def _drop_pending_for_stream(self, stream_id: str) -> None:
        """暂停/移除群时丢弃其待补发事件，恢复后不补旧战绩。"""
        pending = self._pending_events()
        if not pending:
            return
        kept: list[dict[str, Any]] = []
        changed = False
        for event in pending:
            if not isinstance(event, dict):
                changed = True
                continue
            streams = {str(s) for s in (event.get("pending_streams") or []) if s}
            if stream_id in streams:
                streams.discard(stream_id)
                changed = True
            if streams:
                event["pending_streams"] = sorted(streams)
                kept.append(event)
            else:
                changed = True
        if changed:
            pending[:] = kept
            self._save_pending()

    def _append_battle_record(
        self, *, server: str, account_id: int, account_name: str,
        battle_type: str, ship_id: int, diff: dict[str, Any],
    ) -> None:
        """战斗日志的唯一写入点：一次快照差分 → 一条日志记录。

        手动检查、后台轮询、恢复刷新都只能经由这里落盘；调用方必须已持有该账号
        的锁，保证同一次差分不会被写两遍（重复记录会让窝批榜场次翻倍）。
        """
        if not self._battle_log:
            return
        self._battle_log.add_record({
            "timestamp": time.time(),
            "date": time.strftime("%Y-%m-%d"),
            "server": server.upper(),
            "account_id": account_id,
            "account_name": account_name,
            "ship_id": ship_id,
            "ship_name": self._ship_db.ship_name(ship_id) or f"Ship{ship_id}",
            "battle_type": battle_type,
            "battles": diff.get("battles", 0),
            "wins": diff.get("wins", 0),
            "losses": diff.get("losses", 0),
            "damage": diff.get("damage", 0),
            "kills": diff.get("kills", 0),
            "xp": diff.get("xp", 0),
            "potential": diff.get("potential", 0),
            "scouting": diff.get("scouting", 0),
            **extract_log_fields(diff),
        })

    async def _check_account(
        self, server: str, account_id: int, battle_types: list[str], push: bool,
        stream_ids: set[str] | None = None, queue_others: bool = False,
    ) -> dict[str, list]:
        """账号级检测入口：加锁后执行一次"拉取 → 差分 → 落日志 → 推送"。

        战斗日志只有 ``_check_account_locked`` 里这一个写入点，一次差分只落一次。
        后台轮询与手动检查都走这里，因此不会各写一份。
        """
        async with self._account_lock(server, account_id):
            return await self._check_account_locked(
                server, account_id, battle_types, push,
                stream_ids=stream_ids, queue_others=queue_others,
            )

    async def _check_account_locked(
        self, server: str, account_id: int, battle_types: list[str], push: bool,
        stream_ids: set[str] | None = None, queue_others: bool = False,
    ) -> dict[str, list]:
        """拉取账号各对局类型快照，检测新对局、存战斗日志、按需推送。

        ``queue_others=True`` 用于手动检查：只立即推当前群，其他仍在监控该账号
        的群进入待补发队列，避免全局基线被提前消费导致它们永久漏播。

        调用方必须已持有该账号的锁（见 ``_check_account``）。
        """
        snap_key = f"{server.upper()}:{account_id}"
        old_snap = self._state["snapshots"].get(snap_key) or {}

        # 旧快照从 JSON 加载后舰船 key 是字符串，统一转 int，保证 record 检测可用
        old_types: dict[str, dict[int, dict[str, int]]] = {}
        for bt, ships in (old_snap.get("battle_types") or {}).items():
            if not isinstance(ships, dict):
                continue
            old_types[bt] = {
                int(ship_id): stats for ship_id, stats in ships.items() if str(ship_id).isdigit()
            }

        # 以旧快照为基底：某个类型拉取失败/返回空时保留旧基线，不能整体覆盖
        new_types: dict[str, dict[int, dict[str, int]]] = {
            bt: dict(ships) for bt, ships in old_types.items()
        }
        name = old_snap.get("name") or f"Account{account_id}"
        results: dict[str, list] = {}
        failed_types: list[str] = []

        for bt in battle_types:
            try:
                fetched_name, stats = await self._api.fetch_user_ships(server, account_id, bt)
            except Exception:  # noqa: BLE001
                failed_types.append(bt)
                self.ctx.logger.warning("拉取 %s:%s %s 失败，保留旧基线", server, account_id, bt)
                continue
            new_snap = summarize(stats)
            if not new_snap:
                failed_types.append(bt)
                self.ctx.logger.debug("拉取 %s:%s %s 返回空，保留旧基线", server, account_id, bt)
                continue
            if fetched_name and fetched_name != f"Account{account_id}":
                name = fetched_name
            new_types[bt] = new_snap
            old = old_types.get(bt) or {}
            if not old:
                # 首次建立该类型基线：不播报历史战绩
                continue
            all_diffs = detect_new_battles(old, new_snap, max_battles=None)
            if not all_diffs:
                continue
            diffs = {k: v for k, v in all_diffs.items()
                     if 0 < v.get("battles", 0) <= 5 and not v.get("_skip_broadcast")}
            results[bt] = list(diffs.values())

            if self._battle_log:
                for ship_id, d in all_diffs.items():
                    if d.get("_skip_broadcast"):
                        continue  # 历史遗留船（新船 battles>1）不落盘，避免污染榜单和 nl 查询
                    self._append_battle_record(
                        server=server, account_id=account_id, account_name=name,
                        battle_type=bt, ship_id=ship_id, diff=d,
                    )

            if not push or not diffs:
                continue

            streams = set(stream_ids) if stream_ids is not None else set(self._streams_for_account(server, account_id))
            for ship_id, d in diffs.items():
                old_ship = old.get(ship_id) or {}
                broken = check_career_records(old_ship, new_snap.get(ship_id) or {}) if old_ship else []
                event = {
                    "id": f"{time.time_ns()}:{server.upper()}:{account_id}:{bt}:{ship_id}",
                    "created": time.time(),
                    "server": server.upper(),
                    "account_id": account_id,
                    "account_name": name,
                    "ship_id": ship_id,
                    "ship_name": self._ship_db.ship_name(ship_id) or f"Ship{ship_id}",
                    "battle_type": bt,
                    "diff": d,
                    "broken": broken,
                }
                sent = await self._broadcast_event(event, streams)
                failed = {stream_id for stream_id, ok in sent.items() if not ok}
                if failed:
                    self._enqueue_pending(event, failed)
                if queue_others and stream_ids is not None:
                    others = set(self._streams_for_account(server, account_id)) - streams
                    if others:
                        self._enqueue_pending(event, others)

        if failed_types:
            self.ctx.logger.debug("账号 %s:%s 以下类型未更新基线: %s",
                                  server, account_id, ", ".join(failed_types))

        if new_types:
            # 内存基线始终更新（保持最新）；仅在有新对局或改名时写盘，避免每次轮询全量写入大快照
            self._state["snapshots"][snap_key] = {"name": name, "battle_types": new_types, "updated": time.time()}
            if results or name != old_snap.get("name"):
                self._save_snapshots()
        return results

    async def _refresh_snapshots_for_stream(self, stream_id: str) -> None:
        """静默刷新该群所有账号的快照（暂停恢复时跳过历史战绩）"""
        binding = self._get_binding(stream_id)
        if not binding or not binding.get("accounts"):
            return
        await self._ensure_ship_db()
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
                    fetched_name, stats = await self._api.fetch_user_ships(server, account_id, bt)
                except Exception:  # noqa: BLE001
                    self.ctx.logger.warning("恢复刷新快照失败 %s:%s %s", server, account_id, bt)
                    continue
                snap = summarize(stats)
                if not snap:
                    # 空结果保留旧基线，避免清空导致首轮漏播
                    continue
                if fetched_name and fetched_name != f"Account{account_id}":
                    name = fetched_name
                new_types[bt] = snap
            if new_types:
                old_types: dict[str, dict[int, dict[str, int]]] = {}
                for bt, ships in (self._state.get("snapshots", {}).get(snap_key, {}).get("battle_types") or {}).items():
                    if isinstance(ships, dict):
                        old_types[bt] = {
                            int(ship_id): stats for ship_id, stats in ships.items() if str(ship_id).isdigit()
                        }
                old_types.update(new_types)
                self._state["snapshots"][snap_key] = {
                    "name": name, "battle_types": old_types, "updated": time.time()
                }
        self._save_snapshots()

    async def _broadcast_event(self, event: dict[str, Any], stream_ids: set[str] | list[str]) -> dict[str, bool]:
        """按 stream 逐个应用显示模式/伤害范围/额外项并发送，返回每个 stream 是否成功。"""
        results: dict[str, bool] = {}
        for stream_id in stream_ids:
            results[stream_id] = await self._push_event_to_stream(event, stream_id)
        return results

    async def _push_event_to_stream(self, event: dict[str, Any], stream_id: str) -> bool:
        battle_type = str(event.get("battle_type", ""))
        diff = event.get("diff") or {}
        if not isinstance(diff, dict):
            return True
        display_mode = self._get_display_mode(stream_id)
        if not should_broadcast_type(battle_type, display_mode):
            return True
        low, high = self._get_damage_range(stream_id)
        if not should_broadcast_damage(int(diff.get("damage", 0)), low, high):
            return True
        extra = self._get_extra(stream_id)
        display_name = self._get_display_name(
            stream_id,
            str(event.get("server", "")),
            int(event.get("account_id", 0)),
            str(event.get("account_name") or ""),
        )
        text = format_battle(
            display_name,
            str(event.get("ship_name") or ""),
            diff,
            battle_type,
            display_mode,
            extra,
        )
        broken = event.get("broken") or []
        if extra.get("record") and isinstance(broken, list):
            broken_items: list[tuple[str, int, int]] = []
            for item in broken:
                try:
                    broken_items.append((str(item[0]), int(item[1]), int(item[2])))
                except (TypeError, ValueError, IndexError):
                    continue
            if broken_items:
                text = text + "\n" + format_record_break(broken_items)
        return await self._push_to_stream(text, stream_id)

    async def _push_to_stream(self, text: str, stream_id: str) -> bool:
        try:
            result = await self.ctx.send.text(text, stream_id)
            return result is not False
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("推送失败 stream=%s", stream_id)
            return False

    # ---------- 命令 ----------
    async def _reply(self, stream_id: str, text: str) -> tuple[bool, str, int]:
        await self.ctx.send.text(text, stream_id)
        return True, text, 1

    async def _reply_image(self, stream_id: str, html: str, fallback_text: str) -> tuple[bool, str, int]:
        return await send_html_image(self.ctx, stream_id, html, fallback_text, self.ctx.logger)

    async def _denied(self) -> tuple[bool, str, int]:
        return True, "", 1

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
        self._drop_pending_for_stream(stream_id)
        return await self._reply(stream_id, "已暂停战绩推送，/wows on 可恢复")

    @Command("wows_add", pattern=r"^/wows\s+add\s+(?P<server>\S+)\s+(?P<account>\S+)(?:\s+(?P<me>me))?$")
    async def cmd_add(self, **kwargs):
        stream_id = kwargs["stream_id"]
        groups = kwargs.get("matched_groups") or {}
        server = normalize_server(groups.get("server", ""))
        account = groups.get("account", "").strip()
        me = bool(groups.get("me"))
        qq = self._get_sender_id(kwargs) if me else ""

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

        # 添加监控账号（已存在则跳过）
        accounts = binding.setdefault("accounts", [])
        exists = any(
            str(a.get("server", "")).upper() == server and int(a.get("account_id", 0)) == account_id
            for a in accounts
        )
        if not exists:
            accounts.append({"server": server, "account_id": account_id, "nickname": ""})

        # me 绑定：把发送者 QQ 绑定到该 UID；已绑定过则替换为新 UID
        bind_msg = ""
        if me:
            if not qq:
                return await self._reply(stream_id, "无法获取发送者QQ，无法绑定")
            qb = binding.setdefault("qq_bindings", {})
            old = qb.get(qq)
            qb[qq] = {"server": server, "account_id": account_id}
            if old and (str(old.get("server", "")).upper() != server or int(old.get("account_id", 0)) != account_id):
                bind_msg = f"，已将你的QQ从 {old.get('server', '')} {old.get('account_id', '')} 替换绑定到 {server} {account_id}"
            else:
                bind_msg = f"，已将你的QQ绑定到 {server} {account_id}"

        self._save_bindings()
        if not exists:
            text = f"已添加监控账号 {server} {account_id}" if account.isdigit() else f"已添加监控账号 {server} {game_name}（UID: {account_id}）"
        else:
            text = f"账号 {server} {account_id} 已在监控列表中"
        return await self._reply(stream_id, text + bind_msg)

    @Command("wows_remove", pattern=r"^/wows\s+remove\s+(?P<server>\S+)\s+(?P<account>\S+)$")
    async def cmd_remove(self, **kwargs):
        stream_id = kwargs["stream_id"]
        groups = kwargs.get("matched_groups") or {}
        server = normalize_server(groups.get("server", ""))
        account = groups.get("account", "").strip()

        if server not in SERVER_VORTEX:
            return await self._reply(stream_id, f"服务器参数无效：{server}（可用 CN/ASIA/EU/NA/RU 或 国服/亚服/欧服/美服/俄服）")

        # 数字直接当 UID，非数字调用昵称搜索
        if account.isdigit():
            account_id = int(account)
        else:
            result = await self._api.search_player(server, account)
            if result is None:
                return await self._reply(stream_id, f"未找到玩家：{account}（请检查昵称和服务器）")
            account_id, _ = result

        if account_id <= 0:
            return await self._reply(stream_id, "账号ID格式错误")

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
        return True, "", 1

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
        extra = self._get_extra(stream_id)
        kd = get_king_data(self._state, stream_id)
        board_enabled = kd["enabled"]
        extra_text = " ".join(f"{k}={'开' if v else '关'}" for k, v in extra.items())
        board_text = " ".join(f"{leaderboard.BOARDS[key]['title_cn']}={'开' if v else '关'}" for key, v in board_enabled.items())
        if dmg_low == 0 and dmg_high == 0:
            range_text = "全部播报"
        else:
            parts = []
            if dmg_low:
                parts.append(f"≤{dmg_low} 播报")
            if dmg_high:
                parts.append(f"≥{dmg_high} 播报")
            range_text = " / ".join(parts)
        fallback = (
            f"推送状态：{'暂停' if binding.get('paused') else '运行中'}\n"
            f"自然语言查询：{'开启' if binding.get('nl_enabled', NL_DEFAULT_ENABLED) else '关闭'}\n"
            f"榜单开关：{board_text}\n"
            f"显示模式：{mode}（{mode_text}）\n"
            f"伤害范围：{range_text}\n"
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
        """手动触发一次立即拉取（等价于本群账号此刻跑一轮轮询）。

        本身不产生战斗日志：日志仍然由"快照差分"写入，且一次差分只写一次。
        若后台轮询正在检查同一账号，这里直接跳过并提示，避免重复检测。
        """
        stream_id = kwargs["stream_id"]
        binding = self._get_binding(stream_id)
        if not binding or not binding.get("accounts"):
            return await self._reply(stream_id, "本群暂无监控账号")
        await self._ensure_ship_db()
        enabled_types = self._enabled_types()
        has_new = False
        busy: list[str] = []
        for acc in binding["accounts"]:
            server = str(acc.get("server", "")).upper()
            account_id = int(acc.get("account_id", 0))
            if self._is_account_busy(server, account_id):
                busy.append(f"{server} {account_id}")
                continue
            try:
                results = await self._check_account(
                    server, account_id, enabled_types, push=True,
                    stream_ids={stream_id}, queue_others=True,
                )
                if any(results.values()):
                    has_new = True
            except Exception:  # noqa: BLE001
                self.ctx.logger.exception("手动检查失败 %s:%s", server, account_id)
        if has_new:
            return True, "", 1
        if busy:
            return await self._reply(
                stream_id,
                f"以下账号正在检查中，稍后会由这一轮结果自动播报：{'、'.join(busy)}",
            )
        return await self._reply(stream_id, "检查完成：当前无新对局")

    @Command("wows_nick", pattern=r"^/wows\s+nick\s+(?P<server>\S+)\s+(?P<account>\S+)(?:\s+(?P<nickname>.+))?$")
    async def cmd_nick(self, **kwargs):
        if not await self._check_permission("wows_nick", kwargs):
            return await self._denied()
        stream_id = kwargs["stream_id"]
        groups = kwargs.get("matched_groups") or {}
        server = normalize_server(groups.get("server", ""))
        account = groups.get("account", "").strip()
        nickname = (groups.get("nickname") or "").strip()
        if len(nickname) > 16:
            return await self._reply(stream_id, "昵称限制16字符")
        if server not in SERVER_VORTEX:
            return await self._reply(stream_id, f"服务器参数无效：{server}（可用 {', '.join(SERVER_VORTEX)}）")

        # 数字直接当 UID，非数字调用昵称搜索
        if account.isdigit():
            account_id = int(account)
        else:
            result = await self._api.search_player(server, account)
            if result is None:
                return await self._reply(stream_id, f"未找到玩家：{account}（请检查昵称和服务器）")
            account_id, _ = result

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
        return await self._reply(stream_id, f"已{'开启' if value else '关闭'}自然语言查询")

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
            month = (kwargs.get("matched_groups") or {}).get("month", "")
            return await self._cmd_board_month(board_key, stream_id, month or None)
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

    async def _cmd_board_month(self, board_key: str, stream_id: str, month: str | None = None) -> tuple[bool, str, int]:
        board = leaderboard.BOARDS[board_key]
        if not board.get("supports_month") or not board.get("build_month_fn"):
            return await self._reply(stream_id, f"{board['title_cn']}榜不支持月榜")
        kd = get_king_data(self._state, stream_id)
        current = (month_str() or "").replace("-", "")  # "2026-09" -> "202609"
        target = (month or current).strip()
        if len(target) != 6 or not target.isdigit():
            return await self._reply(stream_id, "月份格式错误，如 /wows king month 202608")
        year, mon = int(target[:4]), int(target[4:6])
        month_iso = f"{year:04d}-{mon:02d}"
        if target == current:
            # 当月：从战斗日志实时计算
            if not self._battle_log or not board.get("month_rank_from_logs_fn"):
                return await self._reply(stream_id, f"本月暂无{board['title_cn']}榜数据")
            next_month = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
            records = self._battle_log.get_by_date_range(
                f"{month_iso}-01", (next_month - timedelta(days=1)).isoformat())
            ranked = board["month_rank_from_logs_fn"](
                records, self._monitored_keys(stream_id), self._get_group_nickname, stream_id)
        else:
            # 历史月：读取月初已固定的数据
            monthly = kd.get("monthly", {}).get(month_iso, {}).get(board_key, {})
            ranked = board["month_rank_fn"](monthly)
        if not ranked:
            return await self._reply(stream_id, f"{month_iso} 暂无{board['title_cn']}榜数据")
        self._refresh_ranked_nicknames(stream_id, ranked)
        html = board["build_month_fn"](ranked, month_iso)
        return await self._reply_image(stream_id, html, f"{month_iso} {board['title_cn']}榜")

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

    @Command("cn_add", pattern=r"^/添加\s+(?P<server>\S+)\s+(?P<account>\S+)(?:\s+(?P<me>me))?$")
    async def cn_add(self, **kwargs): return await self.cmd_add(**kwargs)

    @Command("cn_remove", pattern=r"^/移除\s+(?P<server>\S+)\s+(?P<account>\S+)$")
    async def cn_remove(self, **kwargs): return await self.cmd_remove(**kwargs)

    @Command("cn_list", pattern=r"^/列表$")
    async def cn_list(self, **kwargs): return await self.cmd_list(**kwargs)

    @Command("cn_check", pattern=r"^/检查$")
    async def cn_check(self, **kwargs): return await self.cmd_check(**kwargs)

    @Command("cn_on", pattern=r"^/开启$")
    async def cn_on(self, **kwargs): return await self.cmd_on(**kwargs)

    @Command("cn_off", pattern=r"^/暂停$")
    async def cn_off(self, **kwargs): return await self.cmd_off(**kwargs)

    @Command("cn_nick", pattern=r"^/昵称\s+(?P<server>\S+)\s+(?P<account>\S+)(?:\s+(?P<nickname>.+))?$")
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
