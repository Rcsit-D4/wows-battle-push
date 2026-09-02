# 榜单增删使用说明书（BOARDS.md）

排行榜采用**注册表驱动**：所有榜单定义集中在 `leaderboard.py` 的 `BOARDS` 注册表中。新增/删除一个榜单，**只需改 `leaderboard.py` 一个文件**，以下内容全部自动生成，无需改动其他文件：

- 查看 / 历史 / 月榜 / 开关命令（中英文）
- 排行榜帮助页（`/wows lbhelp`）中的命令条目
- 管理员帮助页（`/wows adminhelp`）中的开关行
- status 状态页中的榜单开关状态
- 每日 0 点跨天结算与推送

## 一、新增榜单

在 `leaderboard.py` 末尾（`register_board` 注册区）追加一段注册代码即可。

### 1. 定义排名函数 rank_fn

排行榜数据来自 `battle.json` 战斗日志（每场一条记录），按日读取后实时计算。

```
rank_fn(records, monitored_keys, get_nickname_fn=None, stream_id=None) -> 当日榜
```

- `records`：当日全部战斗记录列表，每条含 `server / account_id / account_name / ship_name / damage / kills / xp / wins / losses / battles` 等字段
- `monitored_keys`：本群已绑定账号集合，格式 `"服务器:UID"`（如 `"ASIA:2052160294"`），只统计集合内的账号
- `get_nickname_fn(stream_id, server, account_id)`：查询群昵称，无则返回 `None`
- 返回按名次排好的列表，每项含 `name`（游戏ID）、`group_nickname`（群昵称，可为空）、`server`、`account_id`、`rank`（从 1 开始）以及你的榜所需的自定义字段

> 参考内置实现：`rank_king_from_logs`（单场最高伤害）、`rank_wopi_from_logs`（总场次）。

### 2. 定义 HTML 构建函数 build_html_fn

```
build_html_fn(ranked, date_str, last=None, period=PERIOD_DAILY) -> HTML 字符串
```

- `ranked`：`rank_fn` 返回的当日榜
- `date_str`：日期或月份字符串
- `last`：跨天结算时的昨日信息（来自 `last_info_fn`），查看当日/历史时通常为 `None`
- `period`：`PERIOD_DAILY`（日榜）或 `PERIOD_MONTHLY`（月榜）
- 返回 HTML 内容块，会被 `substitute` 到模板

**复用内置模板 `king_page.html`**：它提供 `$bg_style / $title / $subtitle / $date / $content` 占位符，以及现成的样式类（`top3-row` 前三、`normal-row` 其余、`section-title`、`yesterday-king` 底部冠军等）。参考 `build_king_html` / `build_wopi_html` 的写法。

若要完全自定义样式，也可新增 `assets/templates/<你的模板>.html`，用 `read_template()` 读取，`$` 开头的占位符由 `substitute` 填充。

### 3. 注册

```python
register_board(
    key="xxx",                # 唯一标识（命令 = /wows xxx），小写
    title_cn="某榜",           # 中文标题
    title_en="SOME BOARD",    # 英文小标题
    rank_fn=my_rank_fn,       # 必填
    build_html_fn=my_build_html_fn,  # 必填
    # ---- 以下可选 ----
    month_rank_fn=my_month_rank_fn,   # 月榜排序（接收 monthly 数据）
    build_month_fn=my_build_month_fn, # 月榜 HTML
    monthly_update_fn=my_monthly_update_fn,  # 跨天时更新月度统计 (sd, ranked)
    last_info_fn=my_last_info_fn,            # 跨天时生成昨日信息 (ranked, date)
    supports_history=True,    # 是否支持历史查询 /wows xxx <日期>
    supports_month=True,      # 是否支持月榜 /wows xxx month
    view_public=True,         # 查看类命令是否全员可用（False = 仅管理员）
    cmd_cn_view="某榜",        # 中文查看命令，如 /某榜
    cmd_cn_history="查看历史某榜",  # 中文历史命令
    cmd_cn_month="本月某榜",        # 中文月榜命令
    cmd_cn_on="开启某榜",           # 中文开启命令
    cmd_cn_off="关闭某榜",          # 中文关闭命令
)
```

注册后自动生成的命令（以 `xxx` 为例）：

| 命令 | 权限 | 说明 |
|---|---|---|
| `/wows xxx` / `/某榜` | 公开 | 查看当日榜 |
| `/wows xxx <日期>` / `/查看历史某榜 <日期>` | 公开 | 查询某日榜 |
| `/wows xxx month` / `/本月某榜` | 公开 | 查看本月榜 |
| `/wows xxx <on\|off>` / `/开启某榜` `/关闭某榜` | 管理员 | 榜单开关 |

> `cmd_cn_view/history/month/on/off` 留空则只生成英文命令。

### 4. 完整示例（新增「存活榜」，统计单场存活次数）

```python
def rank_alive(records, monitored_keys, get_nickname_fn=None, stream_id=None):
    best = {}
    for r in records:
        key = f"{r.get('server', '')}:{r.get('account_id', 0)}"
        if key not in monitored_keys:
            continue
        acc = best.get(key)
        if acc is None or r.get("survived", 0) > acc.get("alive", 0):
            nick = get_nickname_fn(stream_id, r.get("server", ""), r.get("account_id", 0)) if get_nickname_fn and stream_id else None
            best[key] = {
                "name": r.get("account_name", "未知"), "group_nickname": nick,
                "server": r.get("server", ""), "account_id": r.get("account_id", 0),
                "alive": r.get("survived", 0),
            }
    items = sorted(best.values(), key=lambda a: a.get("alive", 0), reverse=True)
    for i, acc in enumerate(items):
        acc["rank"] = i + 1
    return items


def build_alive_html(ranked, date_str, last=None, period=PERIOD_DAILY):
    tpl = read_template("king_page.html")
    if not ranked:
        return tpl.substitute(bg_style=bg_style("alive"), title="本日存活榜", subtitle="ALIVE",
                              date=date_str, content=_empty_panel("暂无有效战绩"))
    rows = "".join(
        f'<div class="normal-row"><span class="normal-rank">{a["rank"]}</span>'
        f'<span class="normal-name">{_display_name(a)}</span>'
        f'<span class="normal-stats">存活{a.get("alive", 0)}次</span></div>'
        for a in ranked
    )
    return tpl.substitute(bg_style=bg_style("alive"), title="本日存活榜", subtitle="ALIVE",
                          date=date_str, content=f'<div class="panel">{rows}</div>')


register_board(
    key="alive", title_cn="存活", title_en="ALIVE",
    rank_fn=rank_alive, build_html_fn=build_alive_html,
    supports_history=False, supports_month=False,
    cmd_cn_view="存活榜", cmd_cn_on="开启存活榜", cmd_cn_off="关闭存活榜",
)
```

保存重启后即可使用 `/wows alive`、`/存活榜`、`/开启存活榜`、`/关闭存活榜`；`/wows lbhelp`、管理员帮助、status 中自动出现该榜。可选用 `alive_bg.png/jpg` 作为该榜专属背景图（放入 `assets/images/`）。

## 二、删除榜单

在 `leaderboard.py` 末尾删除该榜对应的 `register_board(...)` 调用块即可。

删除后：
- 该榜的查看/历史/月榜/开关命令自动消失
- 排行榜帮助、管理员开关、status 状态页自动移除该榜
- 跨天结算不再遍历该榜

**残留数据说明**：`data.json` 的 `daily_king` 中可能残留该榜的 `enabled` / `last` / `history` / `monthly` 数据。这些数据**不会影响运行**（迁移与查询都只遍历当前注册的 `BOARDS`，查不到的 key 自然返回空），但若想彻底清理，可手动编辑 `data.json`，删除 `daily_king.<群>.enabled.<key>`、`daily_king.<群>.last.<key>`、`history` 与 `monthly` 中对应 key 的条目（操作前建议先备份）。

## 三、注意事项

1. **key 唯一且小写**：`key` 会拼进命令名与 pattern，改动后旧命令失效
2. **注册顺序即展示顺序**：`BOARDS` 按 `register_board` 调用顺序遍历，帮助/status 按此顺序显示
3. **排行榜只统计单场记录**：内置榜按 `battles == 1` 过滤（每条日志是一场的差值），自定义 rank_fn 如需排除多场叠加数据，注意同样过滤
4. **跨天流程**：每日 0 点首次轮询时，对每个已开启的榜：读昨日日志 → `rank_fn` 排名 → 存 `history` → `monthly_update_fn` 更新月度 → `last_info_fn` 生成昨日信息 → `build_html_fn` 渲染推送 → 重置当日数据
5. **月榜数据**：`monthly[month][key]` 由 `monthly_update_fn` 跨天累加，`month_rank_fn` 负责排序；如需月榜，三个函数（`month_rank_fn` / `build_month_fn` / `monthly_update_fn`）通常一起提供
6. **图片背景**：背景图查找优先级为 `<key>_bg`（如 `king_bg.png`）→ 通用 `panel_bg` / `background` / `bg`；无图时用浅蓝渐变
7. **改动后重启插件生效**；`data.json` / `battle.json` 无需迁移（内置旧格式自动兼容）
