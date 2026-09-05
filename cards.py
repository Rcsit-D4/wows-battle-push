# -*- coding: utf-8 -*-
"""HTML 卡片生成：help / 排行榜help / status / list（状态与开关从榜单注册表自动生成）"""

from html import escape

from constants import EXTRA_ITEMS
from leaderboard import BOARDS, cmd_specs
from utils import bg_style, read_template

HELP_TEXT = (
    "===== 窝窝屎战绩推送 =====\n"
    "\n"
    "【常用命令】\n"
    "/wows help (/帮助) - 帮助\n"
    "/wows adminhelp (/管理员帮助) - 管理员命令\n"
    "/wows add <服务器> <ID> [me] (/添加) - 添加播报账号（末尾加 me 绑定本人QQ）\n"
    "/wows remove <服务器> <ID> (/移除) - 移除播报账号\n"
    "/wows list (/列表) - 查看监控列表\n"
    "/wows check (/检查) - 立即检查推送\n"
    "/wows lbhelp (/排行榜帮助) - 排行榜命令\n"
    "\n"
    "【自然语言查询(beta)】\n"
    "群内 @bot 用自然语言提问战绩，如“小仓空今天最高伤害是多少”\n"
    "\n"
    "管理员命令请使用 /wows adminhelp (/管理员帮助)\n"
    "\n"
    "【服务器】\n"
    "CN=国服 ASIA=亚服 EU=欧服 NA=美服 RU=俄服\n"
    "\n"
    "【mode说明】\n"
    "1=单野  2=单野/组排  3=ALL\n"
)


def _board_toggle_text() -> list[str]:
    """从注册表生成排行榜开关的文本行"""
    lines = []
    for key, board in BOARDS.items():
        toggle = [s for s in cmd_specs(key, board) if s["action"] == "toggle"]
        if not toggle:
            continue
        spec = toggle[0]
        lines.append(f"/wows {key} <on|off> - 开/关{board['title_cn']}榜")
        if spec.get("cmd_cn"):
            lines.append(f"  {spec['cmd_cn']}")
    return lines


def _admin_help_text() -> str:
    lines = [
        "===== 管理员命令 =====", "",
        "【推送控制】",
        "/wows on (/开启) - 开启/恢复推送",
        "/wows off (/暂停) - 暂停推送",
        "/wows status (/状态) - 查看推送配置", "",
        "【账号与昵称】",
        "/wows nick <服务器> <ID> [昵称] (/昵称) - 设置播报昵称", "",
        "【播报设置】",
        "/wows mode <1|2|3> (/模式) - 对局类型显示模式",
        "/wows range <低> <高> (/伤害范围) - 伤害播报范围",
        "/wows extra <项> <on|off> (/额外播报) - 开关额外播报", "",
        "【自然语言查询(beta)】",
        "/wows nl <on|off> (/开启自然语言查询 /关闭自然语言查询) - 开关自然语言查询", "",
        "【排行榜开关】",
    ]
    lines.extend(_board_toggle_text())
    lines += [
        "", "【extra项】",
        "kills=击杀  xp=经验  pot=潜在伤害  scout=点亮伤害",
        "plane=击落飞机  spot=发现舰船  surv=存活",
        "cap=占点  drop=防守  record=破纪录",
    ]
    return "\n".join(lines)


ADMIN_HELP_TEXT = _admin_help_text()


def build_help_html() -> str:
    return read_template("help_page.html").substitute(bg_style=bg_style("help"))


def _board_switch_rows() -> str:
    """从注册表生成管理员帮助的排行榜开关行（HTML）"""
    rows = ""
    for key, board in BOARDS.items():
        toggle = [s for s in cmd_specs(key, board) if s["action"] == "toggle"]
        if not toggle:
            continue
        spec = toggle[0]
        en = escape(spec["cmd"])
        cn = escape(spec.get("cmd_cn", ""))
        cn_html = f'<span class="cmd-cn">{cn}</span>' if cn else ""
        rows += (f'<div class="cmd-item"><div class="cmd-left">'
                 f'<span class="cmd-en">{en}</span>{cn_html}'
                 f'</div><span class="cmd-desc">开/关{board["title_cn"]}榜</span></div>')
    return rows or '<div class="content-block">暂无榜单</div>'


def build_admin_help_html() -> str:
    return read_template("admin_help_page.html").substitute(
        bg_style=bg_style("help"), board_switches=_board_switch_rows()
    )


def build_status_html(binding: dict | None, board_enabled: dict[str, bool], nl_enabled: bool = True) -> str:
    """推送配置状态卡片 HTML；排行榜开关区遍历注册表自动生成"""
    tpl = read_template("status_page.html")
    if not binding:
        content = '<div class="panel"><div class="empty">本群尚未开启推送，请先 /wows on</div></div>'
        return tpl.substitute(bg_style=bg_style("status"), content=content)

    paused = binding.get("paused", False)
    mode = binding.get("display_mode", 3)
    mode_text = {1: "单野", 2: "单野/组排", 3: "ALL"}.get(mode, str(mode))
    extra = binding.get("extra", {})
    accounts = binding.get("accounts", [])

    def tag(on: bool) -> str:
        cls = "tag-on" if on else "tag-off"
        return f'<span class="tag {cls}">{"开启" if on else "关闭"}</span>'

    board_rows = "".join(
        f'<div class="row"><span class="row-label">{board["title_cn"]}榜</span>'
        f'<span class="row-value">{tag(board_enabled.get(key, False))}</span></div>'
        for key, board in BOARDS.items()
    )
    extra_rows = "".join(
        f'<div class="row"><span class="row-label">{label}</span>'
        f'<span class="row-value">{tag(extra.get(key, False))}</span></div>'
        for key, label in EXTRA_ITEMS.items()
    )

    content = f"""
<div class="panel">
<div class="section-title">运行状态</div>
<div class="row"><span class="row-label">推送服务</span><span class="row-value">{tag(not paused)}</span></div>
<div class="row"><span class="row-label">自然语言查询(beta)</span><span class="row-value">{tag(nl_enabled)}</span></div>
{board_rows}
<div class="row"><span class="row-label">监控账号</span><span class="row-value">{len(accounts)} 个</span></div>
</div>
<div class="panel">
<div class="section-title">播报设置</div>
<div class="row"><span class="row-label">显示模式</span><span class="row-value">模式 {mode}（{mode_text}）</span></div>
<div class="row"><span class="row-label">伤害范围</span><span class="row-value">≤{binding.get('damage_low', 0)} 或 ≥{binding.get('damage_high', 0)} 播报</span></div>
</div>
<div class="panel">
<div class="section-title">额外播报</div>
{extra_rows}
</div>
"""
    return tpl.substitute(bg_style=bg_style("status"), content=content)


def _render_list_rows(accounts: list, snapshots: dict) -> str:
    rows = ""
    for acc in accounts:
        server = str(acc.get("server", "")).upper()
        aid = acc.get("account_id", 0)
        snap_key = f"{server}:{aid}"
        game_name = snapshots.get(snap_key, {}).get("name") or "未拉取"
        nick = acc.get("nickname") or "无"
        rows += f"""<div class="list-row">
<span class="col-server">{server}</span>
<span class="col-uid">{aid}</span>
<span class="col-name">{game_name}</span>
<span class="col-nick">{nick}</span>
</div>"""
    return rows


_LIST_HEADER_ROW = """<div class="list-row list-header">
<span class="col-server">服务器</span>
<span class="col-uid">UID</span>
<span class="col-name">游戏ID</span>
<span class="col-nick">群昵称</span>
</div>"""


def build_list_html(binding: dict | None, snapshots: dict, subtitle: str = "") -> str:
    """监控列表卡片 HTML"""
    tpl = read_template("list_page.html")
    if not binding or not binding.get("accounts"):
        content = '<div class="panel"><div class="empty">暂无监控账号，请先 /wows add 添加</div></div>'
        return tpl.substitute(bg_style=bg_style("list"), title="监控列表",
                              subtitle="MONITORED ACCOUNTS", content=content)
    accounts = binding["accounts"]
    content = f'<div class="panel"><div class="list-table">{_LIST_HEADER_ROW}{_render_list_rows(accounts, snapshots)}</div></div>'
    return tpl.substitute(bg_style=bg_style("list"), title="监控列表",
                          subtitle=subtitle or f"MONITORED ACCOUNTS · {len(accounts)} 个账号", content=content)


def build_list_html_pages(binding: dict | None, snapshots: dict, max_per_page: int = 30) -> list[str]:
    """分页生成监控列表 HTML，每页最多 max_per_page 个账号"""
    if not binding or not binding.get("accounts"):
        return [build_list_html(binding, snapshots)]

    accounts = binding["accounts"]
    total = len(accounts)
    total_pages = (total + max_per_page - 1) // max_per_page
    if total_pages == 1:
        return [build_list_html(binding, snapshots)]

    tpl = read_template("list_page.html")
    pages = []
    for i in range(0, total, max_per_page):
        page_accounts = accounts[i:i + max_per_page]
        page_num = i // max_per_page + 1
        subtitle = f"MONITORED ACCOUNTS · {total} 个账号 (第{page_num}/{total_pages}页)"
        content = f'<div class="panel"><div class="list-table">{_LIST_HEADER_ROW}{_render_list_rows(page_accounts, snapshots)}</div></div>'
        pages.append(tpl.substitute(bg_style=bg_style("list"), title="监控列表", subtitle=subtitle, content=content))
    return pages
