# -*- coding: utf-8 -*-
"""自然语言查询引擎：从战斗日志按玩家/舰种/指标汇总，返回可读文本"""

from typing import Any

from ship_db import SHIP_TYPE_CN, SHIP_TYPE_EN, ShipDb

# 自然语言查询默认开关状态（管理员可随时通过命令关闭）
DEFAULT_ENABLED = True

# Tool 描述：供 LLM 判断何时调用，并约束回复行为（防止回复后发散 / 乱说数据）
TOOL_DESCRIPTION = (
    "当用户用自然语言询问《战舰世界》战绩数据时调用，例如："
    "'XXX今天最高伤害是多少'、'XXX这个星期玩了多少把航母'、'今天群里谁玩了潜艇'、"
    "'这个月谁赢了最多'等。会查询本群绑定玩家的战斗日志并返回统计结果。"
    "注意：只有群内明确询问战绩/伤害/场次/舰种/胜负等数据时才调用；无关闲聊不要调用。"
    "参数填写规则："
    "player 可用群昵称或游戏ID（如'XXX'），用户明确提到某人时必填，不填表示统计本群全部绑定玩家；"
    "date 支持'今天/昨天/这个星期/上周/这个月/上月/最近N天/YYYY-MM-DD'，不填默认今天；"
    "ship_type 为舰种中文名（航母/战列舰/巡洋舰/驱逐舰/潜艇，潜艇俗称'小人'）。"
    "用户提到舰种/船种/某类船时，必须把 ship_type 填上交给工具过滤，禁止自行判断某艘船属于什么舰种；"
    "metric 必填：最高伤害/最高击杀/场均伤害/场次/胜场/玩家列表。"
    "回复规范（必须严格遵守）："
    "只汇报工具返回的数据，数字、船名、场次、舰种、昵称等必须逐字照抄，严禁改动、添加、猜测或联想；"
    "工具未返回的信息（如某艘船属于什么舰种）禁止自行补充说明，更不得把舰种张冠李戴；"
    "若工具返回'没有找到/没有记录'，如实转述即可，不要编造数据；"
    "即使角色爱整活，涉及战绩数据时也只可在措辞和语气上发挥，严禁改动数据内容；"
    "查询完成并回复结果后，本轮任务即结束，不要追加与查询无关的建议、闲聊或额外发挥，"
    "除非用户再次提问，不要重复调用本工具或继续发表其他言论。"
)

METRICS = ["最高伤害", "最高击杀", "场均伤害", "场次", "胜场", "玩家列表"]
METRIC_ALIASES = {
    "最高伤害": "最高伤害", "最高输出": "最高伤害", "最大伤害": "最高伤害", "伤害最高": "最高伤害",
    "最高击杀": "最高击杀", "击杀最多": "最高击杀", "最多击杀": "最高击杀",
    "场均伤害": "场均伤害", "平均伤害": "场均伤害", "场均输出": "场均伤害",
    "场次": "场次", "局数": "场次", "多少把": "场次", "多少盘": "场次", "多少局": "场次",
    "胜场": "胜场", "赢了几把": "胜场", "胜利": "胜场",
    "玩家列表": "玩家列表", "谁玩了": "玩家列表", "谁": "玩家列表",
}


def normalize_metric(metric: str) -> str:
    """把 LLM 给的指标名归一化到 METRICS"""
    m = (metric or "").strip()
    return METRIC_ALIASES.get(m, m if m in METRICS else "场次")


def normalize_ship_type(ship_type: str, ship_db: ShipDb) -> str | None:
    """把用户舰种说法归一化为英文代码；无法识别返回 None（不限舰种）"""
    t = (ship_type or "").strip()
    if not t:
        return None
    if t in SHIP_TYPE_EN:
        return SHIP_TYPE_EN[t]
    if t in SHIP_TYPE_CN.values():
        return t
    # 常见说法兜底
    fuzzy = {
        "航母": "AirCarrier", "空母": "AirCarrier", "cv": "AirCarrier", "空中小人": "AirCarrier",
        "战列": "Battleship", "bb": "Battleship", "大船": "Battleship",
        "巡洋": "Cruiser", "ca": "Cruiser", "cl": "Cruiser",
        "驱逐": "Destroyer", "dd": "Destroyer", "小船": "Destroyer",
        "潜艇": "Submarine", "小人": "Submarine", "ss": "Submarine", "水下小人": "Submarine",
    }
    for k, v in fuzzy.items():
        if k in t.lower():
            return v
    return None


def _display_name(acc: dict, record: dict) -> str:
    """优先群昵称，其次游戏ID"""
    return acc.get("nickname") or record.get("account_name") or f"玩家{record.get('account_id')}"


def _match_account(records: list[dict], player_query: str, group_accounts: list[dict]) -> list[dict]:
    """把玩家标识匹配到本群绑定账号；匹配不到返回空列表"""
    q = (player_query or "").strip()
    if not q:
        return list(group_accounts)
    # 数字直接当 account_id
    if q.isdigit():
        return [a for a in group_accounts if str(a.get("account_id")) == q]
    # 群昵称包含匹配（输入短昵称匹配群内完整昵称）
    hits = [a for a in group_accounts if q in (a.get("nickname") or "")]
    if hits:
        return hits
    # 游戏ID包含匹配（battle 记录里找）
    matched_ids = {r.get("account_id") for r in records if q in (r.get("account_name") or "")}
    if matched_ids:
        return [a for a in group_accounts if a.get("account_id") in matched_ids]
    return []


def _filter_by_ship_type(records: list[dict], ship_db: ShipDb, ship_type: str | None) -> list[dict]:
    """ship_type 为英文舰种代码（AirCarrier等）；None 表示不过滤"""
    if not ship_type:
        return records
    return [r for r in records if ship_db.ship_type_en(r.get("ship_id")) == ship_type]


def _format_num(n: float | int) -> str:
    return f"{n:,.0f}"


def _stats_block(records: list[dict], ship_db: ShipDb, group_accounts: list[dict]) -> list[dict]:
    """按账号聚合：{acc, records, total_damage, max_damage, max_kills, count, wins}"""
    acc_by_id = {a.get("account_id"): a for a in group_accounts}
    blocks: dict[int, dict] = {}
    for r in records:
        aid = r.get("account_id")
        b = blocks.setdefault(aid, {
            "acc": acc_by_id.get(aid) or {"nickname": r.get("account_name"), "account_id": aid},
            "records": [],
            "total_damage": 0, "max_damage": 0, "max_kills": 0, "count": 0, "wins": 0,
        })
        b["records"].append(r)
        b["count"] += 1
        dmg = r.get("damage") or 0
        b["total_damage"] += dmg
        b["max_damage"] = max(b["max_damage"], dmg)
        b["max_kills"] = max(b["max_kills"], r.get("kills") or 0)
        b["wins"] += r.get("wins") or 0
    # 按总伤害降序
    return sorted(blocks.values(), key=lambda b: b["total_damage"], reverse=True)


def run_query(
    records: list[dict],
    ship_db: ShipDb,
    group_accounts: list[dict],
    player: str = "",
    ship_type: str = "",
    metric: str = "场次",
) -> str:
    """执行一次自然语言查询，返回结构化文本供 LLM 组织回复。

    records: 某日期区间的全部 battle 记录（单局一条）
    group_accounts: 当前群绑定的账号列表
    """
    if not records:
        return "该条件下没有找到任何战斗记录。"
    st = normalize_ship_type(ship_type, ship_db)
    m = normalize_metric(metric)
    matched = _match_account(records, player, group_accounts)
    if not matched:
        return f"没有在当前群绑定列表中找到与「{player}」匹配的账号。"

    filtered = _filter_by_ship_type(records, ship_db, st)
    if not filtered:
        label = f"舰种「{SHIP_TYPE_CN.get(st, st)}」" if st else "该舰种"
        return f"该条件下（{label}）没有战斗记录。"

    if player and len(matched) == 1:
        # 单玩家明细
        acc = matched[0]
        mine = [r for r in filtered if r.get("account_id") == acc.get("account_id")]
        name = _display_name(acc, mine[0] if mine else {})
        if not mine:
            return f"{name} 该日期没有符合条件的战斗记录。"
        if m == "最高伤害":
            top = max(mine, key=lambda r: r.get("damage") or 0)
            return f"{name} 最高伤害 {_format_num(top.get('damage'))}（{top.get('ship_name')}）"
        if m == "最高击杀":
            top = max(mine, key=lambda r: r.get("kills") or 0)
            return f"{name} 单场最高击杀 {top.get('kills')}（{top.get('ship_name')}）"
        if m == "场均伤害":
            avg = sum(r.get("damage") or 0 for r in mine) / len(mine)
            return f"{name} 共 {len(mine)} 场，场均伤害 {_format_num(avg)}"
        if m == "胜场":
            wins = sum(r.get("wins") or 0 for r in mine)
            return f"{name} 共 {len(mine)} 场，胜 {wins} 场"
        # 场次
        return f"{name} 共打了 {len(mine)} 场"

    # 全群排行（或多人匹配）
    blocks = _stats_block(filtered, ship_db, group_accounts)
    if m == "玩家列表":
        lines = []
        for b in blocks:
            if not b["records"]:
                continue
            ships = "、".join(dict.fromkeys(r.get("ship_name") or "?" for r in b["records"]))
            lines.append(f"{_display_name(b['acc'], b['records'][0])}：{b['count']} 场（{ships}）")
        return "本群相关玩家：\n" + "\n".join(lines) if lines else "没有玩家符合条件。"
    if m == "最高伤害":
        lines = [f"{_display_name(b['acc'], b['records'][0])}：{_format_num(b['max_damage'])}" for b in blocks if b["records"]]
        return "各玩家最高伤害：\n" + "\n".join(lines)
    if m == "最高击杀":
        lines = [f"{_display_name(b['acc'], b['records'][0])}：{b['max_kills']}" for b in blocks if b["records"]]
        return "各玩家单场最高击杀：\n" + "\n".join(lines)
    if m == "场均伤害":
        lines = [f"{_display_name(b['acc'], b['records'][0])}：{_format_num(b['total_damage'] / b['count'])}" for b in blocks if b["records"]]
        return "各玩家场均伤害：\n" + "\n".join(lines)
    if m == "胜场":
        lines = [f"{_display_name(b['acc'], b['records'][0])}：{b['wins']}/{b['count']} 场" for b in blocks if b["records"]]
        return "各玩家胜场：\n" + "\n".join(lines)
    lines = [f"{_display_name(b['acc'], b['records'][0])}：{b['count']} 场" for b in blocks if b["records"]]
    return "各玩家场次：\n" + "\n".join(lines)
