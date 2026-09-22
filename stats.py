# -*- coding: utf-8 -*-
"""统计快照、差值检测、播报格式化、破纪录检测"""

from typing import Any

from constants import BATTLE_TYPE_LABEL, EXTRA_ITEMS, EXTRA_KEY_ALIAS, EXTRA_SNAPSHOT_KEY


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _broadcast_fields(ship: dict[str, int]) -> dict[str, int]:
    """从原始字段计算播报简化字段；potential = 所有非max的*_agro字段之和"""
    potential = sum(
        _int(v) for k, v in ship.items() if k.endswith("_agro") and not k.startswith("max_")
    )
    return {
        "battles": _int(ship.get("battles_count")),
        "wins": _int(ship.get("wins")),
        "losses": _int(ship.get("losses")),
        "damage": _int(ship.get("damage_dealt")),
        "kills": _int(ship.get("frags")),
        "xp": _int(ship.get("original_exp") or ship.get("exp")),
        "potential": potential,
        "scouting": _int(ship.get("scouting_damage")),
    }


# 快照保留字段白名单（其余字段丢弃以减小文件体积）
SNAPSHOT_KEEP: set[str] = {
    "battles_count", "wins", "losses", "damage_dealt", "frags",
    "exp", "original_exp", "scouting_damage",
    "survived", "win_and_survived",
    "planes_killed", "ships_spotted",
    "capture_points", "dropped_capture_points",
    "shots_by_main", "hits_by_main", "shots_by_tpd", "hits_by_tpd",
}


def summarize(stats: dict[int, dict[str, Any]]) -> dict[int, dict[str, int]]:
    """压缩 API 原始统计为快照，仅保留有对局的船（battles>0）与必要字段"""
    out: dict[int, dict[str, int]] = {}
    for ship_id, st in stats.items():
        if _int(st.get("battles_count")) <= 0:
            continue
        snap: dict[str, int] = {}
        for k, v in st.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if k in SNAPSHOT_KEEP or k.endswith("_agro") or k.startswith("max_"):
                    snap[k] = _int(v)
        out[int(ship_id)] = snap
    return out


def detect_new_battles(
    old: dict[int, dict[str, int]],
    new: dict[int, dict[str, int]],
    max_battles: int | None = 5,
) -> dict[int, dict[str, int]]:
    """对比新旧快照，返回有新增对局的船及其差值。
    - 首次绑定（old 为空）：所有船跳过，不报历史
    - 老船（old 有）：差值 = new - old
    - 新船（old 无但 old 非空）：差值 = new 累计值；battles==1 正常播报，>1 只落盘不播报（历史遗留防刷屏）
    max_battles=None 返回所有新增（不限局数，供日志完整落盘），默认仅返回 1~max_battles 局（供播报）。"""
    # 兼容旧快照：ship_id key 可能是字符串，统一转成整数
    old = {int(k): v for k, v in old.items() if str(k).isdigit()}
    result: dict[int, dict[str, int]] = {}
    old_empty = not old
    for ship_id, n in new.items():
        o = old.get(ship_id)
        if o is None:
            if old_empty:
                continue
            d = _broadcast_fields(n)
            b = d.get("battles", 0)
            if b <= 0:
                continue
            if max_battles is None or b <= max_battles:
                if b > 1:
                    d["_skip_broadcast"] = True
                result[ship_id] = d
            continue
        d = {k: n.get(k, 0) - o.get(k, 0) for k in set(n) | set(o)}
        d.update(_broadcast_fields(d))
        b = d.get("battles", 0)
        if b > 0 and (max_battles is None or b <= max_battles):
            result[ship_id] = d
    return result


def should_broadcast_type(battle_type: str, display_mode: int) -> bool:
    """按显示模式判断是否播报该对局类型"""
    if display_mode == 1:
        return battle_type in ("pvp_solo", "rank_solo")
    if display_mode == 2:
        return battle_type != "pve"
    return True


def should_broadcast_damage(damage: int, low: int, high: int) -> bool:
    """伤害范围过滤；low/high 均<=0 表示不过滤"""
    if low <= 0 and high <= 0:
        return True
    if low > 0 and damage <= low:
        return True
    if high > 0 and damage >= high:
        return True
    return False


def get_type_label(battle_type: str, display_mode: int) -> str:
    """对局类型标签；模式1不显示"""
    if display_mode == 1:
        return ""
    return BATTLE_TYPE_LABEL.get(battle_type, "")


def format_battle(
    account_name: str,
    ship_name: str,
    d: dict[str, int],
    battle_type: str = "",
    display_mode: int = 3,
    extra: dict[str, bool] | None = None,
) -> str:
    """额外项每项一行，类型标签在最后。"""
    extra = extra or {}
    is_win = d.get("wins", 0) > d.get("losses", 0)
    title = "悲报" if is_win else "喜报"
    battles = d.get("battles", 1)
    damage = d.get("damage", 0)

    if battles > 1:
        wins = d.get("wins", 0)
        losses = d.get("losses", 0)
        lines = [
            f"{title}：",
            f"{account_name}刚刚打完{battles}场对局（{wins}胜{losses}负）",
            f"使用{ship_name},共{battles}局总伤害{damage}",
        ]
    else:
        result = "赢了" if is_win else "输了"
        lines = [
            f"{title}：",
            f"{account_name}刚刚{result}一场对局",
            f"使用{ship_name},伤害{damage}",
        ]

    # record 为特殊项，由插件在末尾追加破纪录文本，不在此生成行
    # 多局合并不显示额外播报项（经验/击杀等），仅保留基础伤害与类型标签
    if battles == 1:
        for key, label in EXTRA_ITEMS.items():
            if key == "record" or not extra.get(key):
                continue
            snap_key = EXTRA_SNAPSHOT_KEY.get(key, key)
            lines.append(f"{label}{d.get(snap_key, 0)}")

    type_label = get_type_label(battle_type, display_mode)
    if type_label:
        lines.append(type_label)

    return "\n".join(lines)


def default_extra() -> dict[str, bool]:
    """默认额外播报配置（全部关闭）"""
    return {k: False for k in EXTRA_ITEMS}


def normalize_extra(extra: dict[str, Any] | None) -> dict[str, bool]:
    """规范化 extra 配置，兼容旧的长参数名"""
    result = default_extra()
    if not isinstance(extra, dict):
        return result
    for k, v in extra.items():
        key = EXTRA_KEY_ALIAS.get(k, k)
        if key in result and isinstance(v, bool):
            result[key] = v
    return result


# 破纪录检测：使用 WG API 返回的生涯最高字段（max_*）
RECORD_MAX_FIELDS: dict[str, str] = {
    "max_damage_dealt": "最高伤害",
    "max_frags": "最高击杀",
    "max_exp": "最高经验",
    "max_total_agro": "最高潜在",
    "max_scouting_damage": "最高点亮",
    "max_planes_killed": "最高击落飞机",
}


def check_career_records(old_ship: dict, new_ship: dict) -> list[tuple[str, int, int]]:
    """用新旧快照的生涯最高字段检测破纪录，返回 [(标签, 旧值, 新值), ...]"""
    broken = []
    for max_key, label in RECORD_MAX_FIELDS.items():
        old_max = _int(old_ship.get(max_key))
        new_max = _int(new_ship.get(max_key))
        if new_max > old_max:
            broken.append((label, old_max, new_max))
    return broken


def format_record_break(broken: list[tuple[str, int, int]]) -> str:
    """格式化破纪录文本"""
    if not broken:
        return ""
    lines = ["破纪录了喵~"]
    lines.extend(f"{label}:{old}→{new}" for label, old, new in broken)
    return "\n".join(lines)
