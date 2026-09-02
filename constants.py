# -*- coding: utf-8 -*-
"""常量定义"""

SERVER_VORTEX: dict[str, str] = {
    "CN": "https://vortex.wowsgame.cn",
    "ASIA": "https://vortex.worldofwarships.asia",
    "EU": "https://vortex.worldofwarships.eu",
    "NA": "https://vortex.worldofwarships.com",
    "RU": "https://vortex.korabli.su",
}

SERVER_CN_ALIAS: dict[str, str] = {
    "国服": "CN", "国": "CN",
    "亚服": "ASIA", "亚": "ASIA",
    "欧服": "EU", "欧": "EU",
    "美服": "NA", "美": "NA",
    "俄服": "RU", "俄": "RU",
}


def normalize_server(s: str) -> str:
    """将服务器名规范化为大写英文代码，支持中文别名"""
    if not s:
        return ""
    upper = s.upper()
    if upper in SERVER_VORTEX:
        return upper
    return SERVER_CN_ALIAS.get(s, upper)


VALID_BATTLE_TYPES: set[str] = {"pvp_solo", "rank_solo", "pvp_div2", "pvp_div3", "pve"}

BATTLE_TYPE_LABEL: dict[str, str] = {
    "pvp_solo": "单野",
    "pvp_div2": "双排",
    "pvp_div3": "三排",
    "rank_solo": "排位",
    "pve": "人机",
}

ENCYCLOPEDIA_URL = "https://v3-api.wows.shinoaki.com/public/wows/encyclopedia/ship/search"

# Vortex（WG 官方）请求头：中性客户端标识，不依赖 yuyuko 前缀
VORTEX_HEADERS = {"Client-Type": "SWAGGER;test"}
# 图鉴（shinoaki 私有服务）请求头：保留原项目客户端标识
ENCYCLOPEDIA_HEADERS = {"Yuyuko-Client-Type": "SWAGGER;test"}

DATA_FILE = "data.json"
SHIP_MAP_REFRESH_SECONDS = 6 * 3600

# 额外播报项：key=指令参数, value=显示名
EXTRA_ITEMS: dict[str, str] = {
    "kills": "击杀",
    "xp": "经验",
    "pot": "潜在伤害",
    "scout": "点亮伤害",
    "plane": "击落飞机",
    "spot": "发现舰船",
    "surv": "存活",
    "cap": "占点",
    "drop": "防守",
    "record": "破纪录",
}

# 指令参数 -> 快照字段名（record 无对应字段，特殊处理）
EXTRA_SNAPSHOT_KEY: dict[str, str] = {
    "kills": "kills",
    "xp": "xp",
    "pot": "potential",
    "scout": "scouting",
    "plane": "planes_killed",
    "spot": "ships_spotted",
    "surv": "survived",
    "cap": "capture_points",
    "drop": "dropped_capture_points",
}

# 旧长参数名 -> 新短参数名（兼容旧数据）
EXTRA_KEY_ALIAS: dict[str, str] = {
    "potential": "pot",
    "scouting": "scout",
    "planes": "plane",
    "planes_killed": "plane",
    "spotted": "spot",
    "ships_spotted": "spot",
    "survived": "surv",
    "capture": "cap",
    "capture_points": "cap",
    "dropped": "drop",
    "dropped_capture_points": "drop",
}

# 所有人可用的命令，其余为管理员命令；榜单命令由 leaderboard 注册表动态合并
PUBLIC_COMMANDS: set[str] = {
    "wows_help", "wows_add", "wows_remove", "wows_list", "wows_check",
    "wows_lbhelp",
    "cn_help", "cn_add", "cn_remove", "cn_list", "cn_check", "cn_lbhelp",
}

# 每日排行榜：伤害差距≤此值时综合判定
KING_DAMAGE_TIE_THRESHOLD = 20000
