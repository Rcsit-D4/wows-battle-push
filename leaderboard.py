# -*- coding: utf-8 -*-
"""排行榜：榜单注册表 + 窝窝king/窝批实现 + 日/月/历史 + HTML 生成"""

from datetime import date, timedelta
from html import escape
from typing import Any, Callable

from constants import KING_DAMAGE_TIE_THRESHOLD, RESERVED_BOARD_KEYS
from utils import bg_style, read_template

BOARD_KING = "king"
BOARD_WOPI = "wopi"
PERIOD_DAILY = "daily"
PERIOD_MONTHLY = "monthly"
MONTHLY_SCORE = {1: 5, 2: 3, 3: 1}

# 舰种称号：按总场次中玩得最多的舰种显示（潜艇未定义称号则不显示）
SHIP_TITLE_MAP: dict[str, str] = {
    "AirCarrier": "航出",
    "Battleship": "BB大爷",
    "Cruiser": "达利特",
    "Destroyer": "DD糕手",
}

# 舰种解析器（plugin 注入 ship_db.ship_type_en），供窝批榜统计舰种分布
_ship_type_fn: Callable[[Any], str] | None = None


def set_ship_type_resolver(fn: Callable[[Any], str]) -> None:
    global _ship_type_fn
    _ship_type_fn = fn


# ---------- 通用工具 ----------

def today_str() -> str:
    return date.today().isoformat()


def month_str() -> str:
    return date.today().strftime("%Y-%m")


def parse_date(s: str) -> str | None:
    """20260831 -> 2026-08-31"""
    try:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    except (IndexError, ValueError):
        return None


def _display_name(acc: dict, show_ship: bool = False) -> str:
    """群昵称(游戏ID) 或 游戏ID；show_ship=True 时追加船名。返回 HTML 片段，用户数据已转义"""
    name_raw = acc.get("name", "未知") or "未知"
    name = escape(str(name_raw))
    nick = acc.get("group_nickname")
    html = f'{escape(nick)}<span class="game-id">({name})</span>' if nick and nick != name_raw else name
    if show_ship:
        ship = acc.get("ship_name")
        if ship and ship != "未知舰船":
            html += f'<span class="ship-name">{escape(str(ship))}</span>'
    return html


def _empty_panel(text: str) -> str:
    return f'<div class="panel"><div class="empty">{text}</div></div>'


# ---------- 榜单注册表 ----------
# 新增榜单：写 rank_fn + build_html_fn（+ 月榜/月度/昨日信息可选），再 register_board 一行即可。
# 命令、帮助、状态、跨天推送均由此自动生成。

BOARDS: dict[str, dict] = {}


def register_board(
    *,
    key: str,
    title_cn: str,
    title_en: str,
    rank_fn,                 # (records, monitored_keys, get_nickname_fn=None, stream_id=None) -> 当日榜
    build_html_fn,           # (ranked, date_str, last=None, period=PERIOD_DAILY) -> HTML
    month_rank_fn=None,      # (monthly_data) -> 月榜（固定数据）
    build_month_fn=None,     # (ranked, month) -> 月榜 HTML
    month_rank_from_logs_fn=None,  # (records, monitored_keys, get_nickname_fn, stream_id) -> 当月实时月榜
    monthly_update_fn=None,  # (sd, ranked) -> 跨天更新月度统计
    last_info_fn=None,       # (ranked, date) -> 昨日/底部信息 dict
    supports_history: bool = True,
    supports_month: bool = True,
    view_public: bool = True,
    cmd_cn_view: str = "",
    cmd_cn_history: str = "",
    cmd_cn_month: str = "",
    cmd_cn_on: str = "",
    cmd_cn_off: str = "",
) -> str:
    assert key not in RESERVED_BOARD_KEYS, f"榜单 key '{key}' 与内置命令保留名冲突"
    BOARDS[key] = {
        "key": key, "title_cn": title_cn, "title_en": title_en,
        "rank_fn": rank_fn, "build_html_fn": build_html_fn,
        "month_rank_fn": month_rank_fn, "build_month_fn": build_month_fn,
        "month_rank_from_logs_fn": month_rank_from_logs_fn,
        "monthly_update_fn": monthly_update_fn, "last_info_fn": last_info_fn,
        "supports_history": supports_history, "supports_month": supports_month,
        "view_public": view_public,
        "cmd_cn_view": cmd_cn_view, "cmd_cn_history": cmd_cn_history,
        "cmd_cn_month": cmd_cn_month, "cmd_cn_on": cmd_cn_on, "cmd_cn_off": cmd_cn_off,
    }
    return key


def cmd_specs(key: str, board: dict) -> list[dict]:
    """生成某榜单的全部命令规格，供动态注册与帮助生成使用"""
    b = board
    specs = [
        dict(method=f"wows_{key}", name=f"wows_{key}",
             pattern=f"^/wows\\s+{key}$", public=b["view_public"],
             action="view", cmd=f"/wows {key}", cmd_cn=f"/{b['cmd_cn_view']}" if b.get("cmd_cn_view") else ""),
    ]
    if b["supports_history"]:
        specs.append(dict(method=f"wows_{key}_history", name=f"wows_{key}_history",
                          pattern=f"^/wows\\s+{key}\\s+(?P<date>\\d{{8}})$", public=b["view_public"],
                          action="history", cmd=f"/wows {key} <日期>",
                          cmd_cn=f"/{b['cmd_cn_history']} <日期>" if b.get("cmd_cn_history") else ""))
    if b["supports_month"]:
        specs.append(dict(method=f"wows_{key}_month", name=f"wows_{key}_month",
                          pattern=f"^/wows\\s+{key}\\s+month(?:\\s+(?P<month>\\d{{6}}))?$", public=b["view_public"],
                          action="month", cmd=f"/wows {key} month <202608>",
                          cmd_cn=f"/{b['cmd_cn_month']} <202608>" if b.get("cmd_cn_month") else ""))
    specs.append(dict(method=f"wows_{key}_toggle", name=f"wows_{key}_toggle",
                      pattern=f"^/wows\\s+{key}\\s+(?P<action>on|off)$", public=False,
                      action="toggle", cmd=f"/wows {key} <on|off>",
                      cmd_cn=f"/{b['cmd_cn_on']} /{b['cmd_cn_off']}" if b.get("cmd_cn_on") else ""))
    if b.get("cmd_cn_view"):
        specs.append(dict(method=f"cn_{key}", name=f"cn_{key}",
                          pattern=f"^/{b['cmd_cn_view']}$", public=b["view_public"],
                          action="view", cmd=f"/{b['cmd_cn_view']}", cmd_cn=""))
    if b.get("cmd_cn_history") and b["supports_history"]:
        specs.append(dict(method=f"cn_{key}_history", name=f"cn_{key}_history",
                          pattern=f"^/{b['cmd_cn_history']}\\s+(?P<date>\\d{{8}})$", public=b["view_public"],
                          action="history", cmd=f"/{b['cmd_cn_history']} <日期>", cmd_cn=""))
    if b.get("cmd_cn_month") and b["supports_month"]:
        specs.append(dict(method=f"cn_{key}_month", name=f"cn_{key}_month",
                          pattern=f"^/{b['cmd_cn_month']}(?:\\s+(?P<month>\\d{{6}}))?$", public=b["view_public"],
                          action="month", cmd=f"/{b['cmd_cn_month']} <202608>", cmd_cn=""))
    if b.get("cmd_cn_on"):
        specs.append(dict(method=f"cn_{key}_on", name=f"cn_{key}_on",
                          pattern=f"^/{b['cmd_cn_on']}$", public=False,
                          action="toggle", toggle_value=True, cmd=f"/{b['cmd_cn_on']}", cmd_cn=""))
    if b.get("cmd_cn_off"):
        specs.append(dict(method=f"cn_{key}_off", name=f"cn_{key}_off",
                          pattern=f"^/{b['cmd_cn_off']}$", public=False,
                          action="toggle", toggle_value=False, cmd=f"/{b['cmd_cn_off']}", cmd_cn=""))
    return specs


# ---------- 状态数据管理 ----------

def _migrate_enabled(sd: dict) -> dict[str, bool]:
    """enabled 字段迁移：旧格式为 bool(king)/xxx_enabled，统一为 {key: bool}"""
    enabled = sd.get("enabled")
    if isinstance(enabled, dict):
        for key in BOARDS:
            enabled.setdefault(key, False)
        return enabled
    return {key: (bool(enabled) if key == BOARD_KING else bool(sd.get(f"{key}_enabled", False))) for key in BOARDS}


def _migrate_last(sd: dict) -> dict:
    last = sd.get("last")
    if not isinstance(last, dict):
        last = {}
    for key, old in ((BOARD_KING, "last_king"), (BOARD_WOPI, "last_wopi")):
        if key in BOARDS and key not in last and sd.get(old):
            last[key] = sd[old]
    return last


def get_stream_data(state: dict, stream_id: str) -> dict:
    """获取某群的排行榜状态，跨天自动重置当日数据"""
    dk = state.setdefault("daily_king", {})
    sd = dk.get(stream_id)
    today = today_str()
    if sd is None or sd.get("date") != today:
        prev = sd or {}
        dk[stream_id] = {
            "date": today,
            "enabled": _migrate_enabled(prev),
            "accounts": {}, "wopi_accounts": {},  # 旧字段，兼容旧数据跨天
            "last": _migrate_last(prev),
            "history": prev.get("history", {}),
            "monthly": prev.get("monthly", {}),
        }
    else:
        sd["enabled"] = _migrate_enabled(sd)
        sd["last"] = _migrate_last(sd)
        sd.setdefault("history", {})
        sd.setdefault("monthly", {})
    return dk[stream_id]


# ---------- 排名（窝窝king / 窝批） ----------

def _composite_score(acc) -> float:
    """伤害接近时的综合评分：胜负、击杀、裸经验"""
    win = 1.0 if acc.get("best_win") else 0.0
    xp = min(acc.get("best_xp", 0) / 3000, 3.0)
    return win * 40 + min(acc.get("best_kills", 0), 10) * 5 + xp * 20


def rank_king(accounts, tie_threshold=KING_DAMAGE_TIE_THRESHOLD):
    """按单场最高伤害排名；伤害差距≤阈值时用综合评分调整"""
    items = [dict(v, key=k) for k, v in accounts.items() if v.get("best_damage", 0) > 0]
    if not items:
        return []
    items.sort(key=lambda a: a.get("best_damage", 0), reverse=True)
    changed = True
    while changed:
        changed = False
        for i in range(1, len(items)):
            if items[i - 1].get("best_damage", 0) - items[i].get("best_damage", 0) <= tie_threshold:
                if _composite_score(items[i]) > _composite_score(items[i - 1]):
                    items[i - 1], items[i] = items[i], items[i - 1]
                    changed = True
    for i, acc in enumerate(items):
        acc["rank"] = i + 1
    return items


def rank_wopi(accounts):
    """按总场次排名"""
    items = [dict(v, key=k) for k, v in accounts.items() if v.get("battles", 0) > 0]
    items.sort(key=lambda a: a.get("battles", 0), reverse=True)
    for i, acc in enumerate(items):
        acc["rank"] = i + 1
    return items


# ---------- 从战斗日志实时计算 ----------

def rank_king_from_logs(records, monitored_keys, get_nickname_fn=None, stream_id=None):
    """窝窝king：仅单场(battles==1)，取每账号最高伤害"""
    best: dict[str, dict] = {}
    for r in records:
        if r.get("battles", 0) != 1:
            continue
        key = f"{r.get('server', '')}:{r.get('account_id', 0)}"
        if key not in monitored_keys:
            continue
        acc = best.get(key)
        if acc is None or r.get("damage", 0) > acc.get("best_damage", 0):
            nick = get_nickname_fn(stream_id, r.get("server", ""), r.get("account_id", 0)) if get_nickname_fn and stream_id else None
            best[key] = {
                "name": r.get("account_name", "未知"),
                "group_nickname": nick,
                "server": r.get("server", ""),
                "account_id": r.get("account_id", 0),
                "ship_name": r.get("ship_name", ""),
                "best_damage": r.get("damage", 0),
                "best_kills": r.get("kills", 0),
                "best_xp": r.get("xp", 0),
                "best_potential": r.get("potential", 0),
                "best_scouting": r.get("scouting", 0),
                "best_win": r.get("wins", 0) > r.get("losses", 0),
            }
    return rank_king(best)


def _ship_title(ship_types: dict[str, int]) -> str:
    """按舰种场次最多的类型返回称号；并列取先出现的类型，未知舰种返回空"""
    if not ship_types:
        return ""
    top_type = max(ship_types, key=ship_types.get)
    return SHIP_TITLE_MAP.get(top_type, "")


def rank_wopi_from_logs(records, monitored_keys, get_nickname_fn=None, stream_id=None):
    """窝批：统计总场次 + 组排分布（单野/双排/三排）+ 舰种分布"""
    counts: dict[str, dict] = {}
    for r in records:
        key = f"{r.get('server', '')}:{r.get('account_id', 0)}"
        if key not in monitored_keys:
            continue
        battles = r.get("battles", 0)
        bt = r.get("battle_type", "")
        ship_type = _ship_type_fn(r.get("ship_id", 0)) if _ship_type_fn else ""
        acc = counts.get(key)
        if acc is None:
            nick = get_nickname_fn(stream_id, r.get("server", ""), r.get("account_id", 0)) if get_nickname_fn and stream_id else None
            acc = counts[key] = {
                "name": r.get("account_name", "未知"),
                "group_nickname": nick,
                "server": r.get("server", ""),
                "account_id": r.get("account_id", 0),
                "battles": battles,
                "solo": battles if bt == "pvp_solo" else 0,
                "div2": battles if bt == "pvp_div2" else 0,
                "div3": battles if bt == "pvp_div3" else 0,
                "ship_types": {ship_type: battles} if ship_type else {},
            }
        else:
            acc["battles"] += battles
            if bt == "pvp_solo":
                acc["solo"] += battles
            elif bt == "pvp_div2":
                acc["div2"] += battles
            elif bt == "pvp_div3":
                acc["div3"] += battles
            if ship_type:
                acc["ship_types"][ship_type] = acc["ship_types"].get(ship_type, 0) + battles
    for acc in counts.values():
        acc["title"] = _ship_title(acc.get("ship_types", {}))
    return rank_wopi(counts)


def rank_monthly_king(data):
    """月度窝窝king（固定数据）：按积分和前三次数排序；条目可能为 uid-key 或昵称-key 两种旧格式"""
    items = []
    for k, v in data.items():
        acc = dict(v)
        acc["key"] = k
        acc["name"] = v.get("name") or k
        items.append(acc)
    items.sort(key=lambda a: (a.get("score", 0), a.get("top3_count", 0)), reverse=True)
    for i, acc in enumerate(items):
        acc["rank"] = i + 1
    return items


def rank_king_monthly_from_logs(records, monitored_keys, get_nickname_fn=None, stream_id=None):
    """月度窝窝king（实时）：逐日排名累计前三/积分 + 本月单局最佳"""
    by_day: dict[str, list] = {}
    for r in records:
        by_day.setdefault(r.get("date", ""), []).append(r)
    accs: dict[str, dict] = {}
    for day_recs in by_day.values():
        for acc in rank_king_from_logs(day_recs, monitored_keys, get_nickname_fn, stream_id):
            e = accs.get(acc["key"])
            if e is None:
                e = accs[acc["key"]] = {
                    "name": acc.get("name", "未知"), "group_nickname": acc.get("group_nickname"),
                    "server": acc.get("server", ""), "account_id": acc.get("account_id", 0),
                    "top3_count": 0, "score": 0, "champion": 0, "runner_up": 0, "third": 0,
                    "best_damage": 0, "best_kills": 0, "best_xp": 0, "best_win": False, "ship_name": "",
                }
            if acc["rank"] <= 3:
                e["top3_count"] += 1
                e["score"] += MONTHLY_SCORE.get(acc["rank"], 0)
                rk = {1: "champion", 2: "runner_up", 3: "third"}.get(acc["rank"])
                if rk:
                    e[rk] = e.get(rk, 0) + 1
            if acc.get("best_damage", 0) > e["best_damage"]:
                e["best_damage"] = acc.get("best_damage", 0)
                e["best_kills"] = acc.get("best_kills", 0)
                e["best_xp"] = acc.get("best_xp", 0)
                e["best_win"] = acc.get("best_win", False)
                e["ship_name"] = acc.get("ship_name", "")
    items = [dict(v, key=k) for k, v in accs.items() if v["top3_count"] > 0]
    items.sort(key=lambda a: (a.get("score", 0), a.get("top3_count", 0)), reverse=True)
    for i, acc in enumerate(items):
        acc["rank"] = i + 1
    return items


def rank_monthly_wopi(data):
    """月度窝批：按总场次排序；条目可能为 uid-key 或昵称-key 两种旧格式"""
    items = []
    for k, v in data.items():
        acc = dict(v)
        acc["key"] = k
        acc["name"] = v.get("name") or k
        items.append(acc)
    items.sort(key=lambda a: a.get("battles", 0), reverse=True)
    for i, acc in enumerate(items):
        acc["rank"] = i + 1
    return items


# ---------- 月度统计更新与昨日信息 ----------

def _update_monthly_king(sd, ranked) -> None:
    m = sd.setdefault("monthly", {}).setdefault(month_str(), {})
    mk = m.setdefault(BOARD_KING, {})
    rank_key_map = {1: "champion", 2: "runner_up", 3: "third"}
    for acc in ranked[:3]:
        key = acc.get("key") or acc.get("name")
        if not key:
            continue
        e = mk.get(key)
        if e is None:
            e = mk[key] = {"name": acc.get("name", "未知"), "group_nickname": acc.get("group_nickname"),
                           "top3_count": 0, "score": 0, "champion": 0, "runner_up": 0, "third": 0, "best_damage": 0}
        e["name"] = acc.get("name", e["name"])  # 覆盖为最新游戏名
        e["group_nickname"] = acc.get("group_nickname")
        e["top3_count"] += 1
        e["score"] += MONTHLY_SCORE.get(acc.get("rank"), 0)
        rk = rank_key_map.get(acc.get("rank"))
        if rk:
            e[rk] = e.get(rk, 0) + 1
        if acc.get("best_damage", 0) > e["best_damage"]:
            e["best_damage"] = acc.get("best_damage", 0)
            e["ship_name"] = acc.get("ship_name", "")
        mk[key] = e


def _update_monthly_wopi(sd, ranked) -> None:
    m = sd.setdefault("monthly", {}).setdefault(month_str(), {})
    mw = m.setdefault(BOARD_WOPI, {})
    for acc in ranked:
        key = acc.get("key") or acc.get("name")
        if not key:
            continue
        e = mw.get(key)
        if e is None:
            e = mw[key] = {"name": acc.get("name", "未知"), "group_nickname": acc.get("group_nickname"),
                           "battles": 0, "solo": 0, "div2": 0, "div3": 0, "ship_types": {}}
        e["name"] = acc.get("name", e["name"])  # 覆盖为最新游戏名
        e["group_nickname"] = acc.get("group_nickname")
        e["battles"] += acc.get("battles", 0)
        e["solo"] += acc.get("solo", 0)
        e["div2"] += acc.get("div2", 0)
        e["div3"] += acc.get("div3", 0)
        for t, n in (acc.get("ship_types") or {}).items():
            e["ship_types"][t] = e["ship_types"].get(t, 0) + n
        e["title"] = _ship_title(e["ship_types"])


def _last_info_king(ranked, day) -> dict:
    top = ranked[0]
    return {"name": top.get("name", "未知"), "group_nickname": top.get("group_nickname"),
            "damage": top.get("best_damage", 0), "date": day}


def _last_info_wopi(ranked, day) -> dict:
    top = ranked[0]
    return {"name": top.get("name", "未知"), "group_nickname": top.get("group_nickname"),
            "battles": top.get("battles", 0), "date": day}


# ---------- 跨天检查与推送 ----------

async def check_daily_reset(state, ctx, send_image_fn, logger,
                            get_records_fn=None, is_monitored_fn=None, get_nickname_fn=None) -> bool:
    """跨天：对每个已开启的榜单推送昨日榜、保存历史、更新月度，并重置当日"""
    today = today_str()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    changed = False
    for stream_id, sd in list(state.get("daily_king", {}).items()):
        if sd.get("date") == today:
            continue
        # 停机/暂停多天恢复：跳过中间天的结算，从昨天开始
        old_date = sd.get("date", "")
        try:
            old_d = date.fromisoformat(old_date) if old_date else None
        except (TypeError, ValueError):
            old_d = None
        if old_d is None or old_d < date.today() - timedelta(days=1):
            sd["date"] = yesterday
        enabled = _migrate_enabled(sd)
        sd["enabled"] = enabled
        for key, board in BOARDS.items():
            if not enabled.get(key):
                continue
            ranked = None
            if get_records_fn:
                day_records = get_records_fn(stream_id, yesterday)
                monitored = set()
                if is_monitored_fn:
                    for r in day_records:
                        if is_monitored_fn(stream_id, r.get("server", ""), r.get("account_id", 0)):
                            monitored.add(f"{r.get('server','')}:{r.get('account_id',0)}")
                ranked = board["rank_fn"](day_records, monitored, get_nickname_fn, stream_id)
            elif key == BOARD_KING:  # 兼容旧数据
                ranked = rank_king(sd.get("accounts", {}))
            elif key == BOARD_WOPI:
                ranked = rank_wopi(sd.get("wopi_accounts", {}))
            if not ranked:
                continue
            sd.setdefault("history", {}).setdefault(yesterday, {})[key] = ranked
            if board.get("monthly_update_fn"):
                board["monthly_update_fn"](sd, ranked)
            last_info = board["last_info_fn"](ranked, yesterday) if board.get("last_info_fn") else None
            if last_info:
                sd.setdefault("last", {})[key] = last_info
            try:
                html = board["build_html_fn"](ranked, yesterday, last_info, PERIOD_DAILY)
                await send_image_fn(ctx, stream_id, html, f"【{board['title_cn']} {yesterday}】详见图片", logger)
            except Exception:  # noqa: BLE001
                logger.exception("推送%s失败 stream=%s", board["title_cn"], stream_id)
        sd["date"] = today
        sd["accounts"], sd["wopi_accounts"] = {}, {}
        changed = True
    return changed


# ---------- HTML 生成（窝窝king / 窝批） ----------

def _king_top3_rows(ranked, is_monthly=False) -> str:
    rows = ""
    for acc in ranked[:3]:
        r = acc["rank"]
        crown = '<span class="crown">♛</span>' if r == 1 else ""
        if is_monthly:
            stats = f"前三{acc.get('top3_count', 0)}次 · 冠:{acc.get('champion', 0)} 亚:{acc.get('runner_up', 0)} 季:{acc.get('third', 0)} · 最佳{acc.get('best_damage', 0):,}"
        else:
            win = "胜" if acc.get("best_win") else "负"
            stats = f"伤害{acc.get('best_damage', 0):,} · 击杀{acc.get('best_kills', 0)} · 经验{acc.get('best_xp', 0):,} · 潜在{acc.get('best_potential', 0):,} · 点亮{acc.get('best_scouting', 0):,} · {win}"
        rows += f"""<div class="top3-row rank-{r}">
<div class="rank-badge">{r}</div>
<div class="top3-info"><div class="top3-name">{_display_name(acc, show_ship=True)}{crown}</div>
<div class="top3-stats">{stats}</div></div></div>"""
    return rows


def _king_rest_rows(ranked, is_monthly=False) -> str:
    rows = ""
    for acc in ranked[3:10]:
        if is_monthly:
            stats = f"前三{acc.get('top3_count', 0)}次 · 冠:{acc.get('champion', 0)} 亚:{acc.get('runner_up', 0)} 季:{acc.get('third', 0)} · 最佳{acc.get('best_damage', 0):,}"
        else:
            win = "胜" if acc.get("best_win") else "负"
            stats = f"伤害{acc.get('best_damage', 0):,} · {win} · {acc.get('best_kills', 0)}击杀"
        rows += f"""<div class="normal-row">
<span class="normal-rank">{acc['rank']}</span>
<span class="normal-name">{_display_name(acc, show_ship=True)}</span>
<span class="normal-stats">{stats}</span></div>"""
    return rows


def _wopi_group_text(acc: dict) -> str:
    """组排分布文本：单野/双排/三排（全为0时返回空）"""
    parts = []
    if acc.get("solo", 0):
        parts.append(f"单野{acc['solo']}场")
    if acc.get("div2", 0):
        parts.append(f"双排{acc['div2']}场")
    if acc.get("div3", 0):
        parts.append(f"三排{acc['div3']}场")
    return "、".join(parts)


def _wopi_top3_rows(ranked) -> str:
    rows = ""
    for acc in ranked[:3]:
        r = acc["rank"]
        crown = '<span class="crown">♛</span>' if r == 1 else ""
        title = f'<span class="ship-title">{escape(acc.get("title", ""))}</span>' if acc.get("title") else ""
        group = escape(_wopi_group_text(acc))
        rows += f"""<div class="top3-row rank-{r}">
<div class="rank-badge">{r}</div>
<div class="top3-info"><div class="top3-name">{_display_name(acc)}{title}{crown}</div></div>
<div class="wopi-battles-col"><div class="wopi-battles">{acc.get('battles', 0)}场</div>{f'<div class="wopi-group">{group}</div>' if group else ""}</div></div>"""
    return rows


def _wopi_rest_rows(ranked) -> str:
    rows = ""
    for acc in ranked[3:10]:
        title = f'<span class="ship-title">{escape(acc.get("title", ""))}</span>' if acc.get("title") else ""
        group = escape(_wopi_group_text(acc))
        rows += f"""<div class="normal-row">
<span class="normal-rank">{acc['rank']}</span>
<span class="normal-name">{_display_name(acc)}{title}</span>
<span class="normal-stats"><span class="wopi-battles-small">{acc.get('battles', 0)}场</span>{f'<span class="wopi-group">{group}</span>' if group else ""}</span></div>"""
    return rows


def _bottom_panel(title, name, sub, date) -> str:
    """name 为 _display_name 生成的 HTML 片段（内部已转义），其余参数为纯文本"""
    return f"""<div class="panel yesterday-king">
<div class="yesterday-title">{escape(title)}</div>
<div class="yesterday-name">{name}</div>
<div class="yesterday-damage">{escape(sub)}</div>
<div class="yesterday-date">{escape(date)}</div></div>"""


def build_king_html(ranked, date_str, last=None, period=PERIOD_DAILY) -> str:
    tpl = read_template("king_page.html")
    if not ranked:
        return tpl.substitute(bg_style=bg_style("king"), title="本日窝窝king", subtitle="WOWS KING",
                              date=date_str, content=_empty_panel("暂无有效战绩"))
    title = "本月前三" if period == PERIOD_MONTHLY else "前三"
    parts = [f'<div class="panel"><div class="section-title">{title}</div>{_king_top3_rows(ranked, period == PERIOD_MONTHLY)}</div>']
    if ranked[3:10]:
        parts.append(f'<div class="panel">{_king_rest_rows(ranked, period == PERIOD_MONTHLY)}</div>')
    if period == PERIOD_MONTHLY and ranked:
        mk = ranked[0]
        parts.append(_bottom_panel("本月窝窝king", _display_name(mk), f"前三{mk.get('top3_count', 0)}次 · 最佳{mk.get('best_damage', 0):,}", date_str))
    elif last:
        parts.append(_bottom_panel("昨日窝窝king", _display_name(last), f"最高伤害 {last.get('damage', 0):,}", last.get("date", "")))
    return tpl.substitute(bg_style=bg_style("king"), title="本日窝窝king", subtitle="WOWS KING",
                          date=date_str, content="".join(parts))


def build_wopi_html(ranked, date_str, last=None, period=PERIOD_DAILY) -> str:
    tpl = read_template("king_page.html")
    if not ranked:
        return tpl.substitute(bg_style=bg_style("wopi"), title="本日窝批", subtitle="WO PI",
                              date=date_str, content=_empty_panel("暂无有效战绩"))
    top_title = "本月大窝批" if period == PERIOD_MONTHLY else "三位大窝批"
    parts = [f'<div class="panel"><div class="section-title">{top_title}</div>{_wopi_top3_rows(ranked)}</div>']
    if ranked[3:10]:
        parts.append(f'<div class="panel"><div class="section-title">小窝批们</div>{_wopi_rest_rows(ranked)}</div>')
    if period == PERIOD_DAILY and last:
        parts.append(_bottom_panel("昨日窝批", _display_name(last), f"{last.get('battles', 0)}场", last.get("date", "")))
    return tpl.substitute(bg_style=bg_style("wopi"), title="本日窝批", subtitle="WO PI",
                          date=date_str, content="".join(parts))


def build_monthly_king_html(ranked, month) -> str:
    tpl = read_template("king_page.html")
    if not ranked:
        return tpl.substitute(bg_style=bg_style("king"), title="本月窝窝king", subtitle="WOWS KING",
                              date=month, content=_empty_panel("本月暂无数据"))
    parts = [f'<div class="panel"><div class="section-title">本月前三</div>{_king_top3_rows(ranked, True)}</div>']
    if ranked[3:10]:
        parts.append(f'<div class="panel">{_king_rest_rows(ranked, True)}</div>')
    if ranked:
        mk = ranked[0]
        parts.append(_bottom_panel("本月窝窝king", _display_name(mk), f"前三{mk.get('top3_count', 0)}次 · 最佳{mk.get('best_damage', 0):,}", month))
    return tpl.substitute(bg_style=bg_style("king"), title="本月窝窝king", subtitle="WOWS KING",
                          date=month, content="".join(parts))


def build_monthly_wopi_html(ranked, month) -> str:
    tpl = read_template("king_page.html")
    if not ranked:
        return tpl.substitute(bg_style=bg_style("wopi"), title="本月窝批", subtitle="WO PI",
                              date=month, content=_empty_panel("本月暂无数据"))
    parts = [f'<div class="panel"><div class="section-title">本月大窝批</div>{_wopi_top3_rows(ranked)}</div>']
    if ranked[3:10]:
        parts.append(f'<div class="panel"><div class="section-title">小窝批们</div>{_wopi_rest_rows(ranked)}</div>')
    return tpl.substitute(bg_style=bg_style("wopi"), title="本月窝批", subtitle="WO PI",
                          date=month, content="".join(parts))


# ---------- 历史查询 ----------

def get_history(sd, key, date_iso):
    return sd.get("history", {}).get(date_iso, {}).get(key, [])


# ---------- 排行榜帮助（自动生成） ----------

def _board_help_rows() -> str:
    """从注册表生成排行榜帮助内容块（HTML），命令文本做 HTML 转义。
    仅遍历英文命令规格（已内嵌中文别名），跳过中文规格与开关，避免重复"""
    rows = ""
    for key, board in BOARDS.items():
        rows += f'<div class="cmd-item"><div class="cmd-left">'
        for spec in cmd_specs(key, board):
            if spec["action"] == "toggle" or spec["method"].startswith("cn_"):
                continue
            rows += f'<span class="cmd-en">{escape(spec["cmd"])}</span>'
            if spec["cmd_cn"]:
                rows += f'<span class="cmd-cn">{escape(spec["cmd_cn"])}</span>'
        rows += f'</div><span class="cmd-desc">{board["title_cn"]}榜</span></div>'
    return rows


def build_lbhelp_html() -> str:
    """排行榜帮助页：遍历注册表自动生成"""
    tpl = read_template("lbhelp_page.html")
    content = f'<div class="panel"><div class="section-title">排行榜命令</div>{_board_help_rows()}</div>'
    return tpl.substitute(bg_style=bg_style("help"), content=content)


def lbhelp_text() -> str:
    """排行榜帮助降级文本（仅英文命令，中文别名内嵌）"""
    lines = ["===== 排行榜帮助 =====", ""]
    for key, board in BOARDS.items():
        lines.append(f"【{board['title_cn']}榜】")
        for spec in cmd_specs(key, board):
            if spec["action"] == "toggle" or spec["method"].startswith("cn_"):
                continue
            line = spec["cmd"]
            if spec["cmd_cn"]:
                line += f" ({spec['cmd_cn']})"
            lines.append(f"  {line}")
        toggle = [spec["cmd"] for spec in cmd_specs(key, board) if spec["action"] == "toggle"]
        if toggle:
            lines.append(f"  开关: {toggle[0]}（管理员）")
        lines.append("")
    return "\n".join(lines)


# ---------- 注册内置榜单 ----------

register_board(
    key=BOARD_KING,
    title_cn="窝窝king", title_en="WOWS KING",
    rank_fn=rank_king_from_logs,
    build_html_fn=build_king_html,
    month_rank_fn=rank_monthly_king,
    build_month_fn=build_monthly_king_html,
    month_rank_from_logs_fn=rank_king_monthly_from_logs,
    monthly_update_fn=_update_monthly_king,
    last_info_fn=_last_info_king,
    cmd_cn_view="窝王",
    cmd_cn_history="查看历史窝王",
    cmd_cn_month="本月窝王",
    cmd_cn_on="开启窝王榜",
    cmd_cn_off="关闭窝王榜",
)

register_board(
    key=BOARD_WOPI,
    title_cn="窝批", title_en="WO PI",
    rank_fn=rank_wopi_from_logs,
    build_html_fn=build_wopi_html,
    month_rank_fn=rank_monthly_wopi,
    build_month_fn=build_monthly_wopi_html,
    month_rank_from_logs_fn=rank_wopi_from_logs,
    monthly_update_fn=_update_monthly_wopi,
    last_info_fn=_last_info_wopi,
    cmd_cn_view="窝批",
    cmd_cn_history="查看历史窝批",
    cmd_cn_month="本月窝批",
    cmd_cn_on="开启窝批榜",
    cmd_cn_off="关闭窝批榜",
)
