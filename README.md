# 战舰世界战绩推送插件（wows-battle-push）

监控《战舰世界》账号对局，检测到新战绩结算时自动推送到绑定群。由 [wows-real](https://github.com/wows-yuyuko/wows-real)（Java）重置的 Python 实现，数据源为 Vortex 公共接口。

## 功能特性

- **战绩结算自动推送**：按对局类型差值检测新战绩，实时播报伤害、胜负、击杀等
- **播报文案**：昵称支持群内自定义（默认游戏 ID）
- **播报控制**：暂停/恢复、对局类型显示模式（1=单野 2=单野/组排 3=ALL）、伤害范围过滤（≤低 或 ≥高）、单场破纪录提醒
- **额外播报项**：击杀/经验/潜在/点亮/击落飞机/发现舰船/存活/占点/防守，可批量开关
- **排行榜**：窝窝king（单场最高伤害）、窝批（总场次），支持日/月/历史查询，0 点自动结算推送；**榜单注册表驱动，新增榜单无需改动其他文件**（详见 `boards_help.md`）
- **权限控制**：查看类命令全员可用，开关/设置类命令仅管理员白名单可用
- **图片输出**：帮助、状态、列表、排行榜均渲染为带背景图的卡片图片，失败自动降级为文本

## 部署

1. 将整个 `wows-battle-push` 目录放入 MaiBot 插件目录（如 `/MaiBot/plugins/`）
2. 确认 `_manifest.json` 声明的能力已授权：`send.text`、`send.image`、`render.html2png`
3. 重启 MaiBot，插件自动加载；日志出现「插件已加载」即成功
4. 编辑 `config.toml` 配置（见下）

> 依赖 MaiBot SDK + Chrome（HTML 渲染）+ napcat（QQ 收发），纯 Python 实现，Windows/Linux 均可部署。

## 配置（config.toml）

| 配置项 | 说明 | 默认 |
|---|---|---|
| `poll_interval_minutes` | 轮询检测间隔（分钟） | 3 |
| `enabled_battle_types` | 监控的对局类型 | pvp_solo/div2/div3/rank_solo |
| `push_enabled` | 是否自动推送 | true |
| `admin_qq` | 管理员 QQ 白名单 | 需自行填写 |
| `log_retention_days` | 战斗日志保留天数 | 30 |

对局类型：`pvp_solo`(单野) `pvp_div2`(双排) `pvp_div3`(三排) `rank_solo`(排位) `pve`(人机)

## 命令总览

### 公开命令（全员可用）

| 命令 | 中文 | 说明 |
|---|---|---|
| `/wows help` | `/帮助` | 帮助 |
| `/wows lbhelp` | `/排行榜帮助` | 排行榜命令帮助 |
| `/wows add <服务器> <ID>` | `/添加` | 添加播报账号（ID 可为数字 UID 或游戏昵称） |
| `/wows remove <服务器> <ID>` | `/移除` | 移除播报账号 |
| `/wows list` | `/列表` | 查看监控列表 |
| `/wows check` | `/检查` | 立即检查推送 |
| `/wows king` / `/窝王` | — | 查看本日窝窝king榜 |
| `/wows king <日期>` / `/查看历史窝王 <日期>` | — | 查看某日窝窝king榜 |
| `/wows king month` / `/本月窝王` | — | 查看本月窝窝king榜 |
| `/wows wopi` / `/窝批` | — | 查看本日窝批榜 |
| `/wows wopi <日期>` / `/查看历史窝批 <日期>` | — | 查看某日窝批榜 |
| `/wows wopi month` / `/本月窝批` | — | 查看本月窝批榜 |

### 管理员命令（仅 `admin_qq` 白名单）

| 命令 | 中文 | 说明 |
|---|---|---|
| `/wows adminhelp` | `/管理员帮助` | 管理员命令帮助 |
| `/wows on` | `/开启` | 开启/恢复推送 |
| `/wows off`（`/wows pause`） | `/暂停` | 暂停推送 |
| `/wows status` | `/状态` | 查看推送配置（模式/范围/额外/榜单开关） |
| `/wows nick <服务器> <ID> [昵称]` | `/昵称` | 设置播报昵称（≤16字符，留空清除） |
| `/wows mode <1|2|3>` | `/模式` | 对局类型显示模式 |
| `/wows range <低> <高>` | `/伤害范围` | 伤害过滤（0 表示不限制） |
| `/wows extra <项...> <on|off>` | `/额外播报` | 批量开关额外播报项 |
| `/wows king <on|off>` | `/开启窝王榜` `/关闭窝王榜` | 窝窝king榜开关 |
| `/wows wopi <on|off>` | `/开启窝批榜` `/关闭窝批榜` | 窝批榜开关 |

额外播报项：`kills` 击杀 `xp` 经验 `pot` 潜在 `scout` 点亮 `plane` 击落飞机 `spot` 发现舰船 `surv` 存活 `cap` 占点 `drop` 防守 `record` 破纪录

服务器：`CN` 国服 `ASIA` 亚服 `EU` 欧服 `NA` 美服 `RU` 俄服

## 数据文件

| 文件 | 内容 | 说明 |
|---|---|---|
| `data.json` | 群绑定、战舰快照、榜单状态 | 旧格式自动迁移 |
| `battle.json` | 战斗日志（按日期归档） | 排行榜数据源，超过保留天数自动清理 |

`data.json` 结构：`version` / `bindings`（群→账号列表）/ `snapshots`（每账号各舰快照）/ `daily_king`（每群榜单状态：enabled 开关、last 昨日、history 历史、monthly 月度）

## 文件结构

```
wows-battle-push/
├── plugin.py        插件主入口：生命周期、轮询、命令分发（榜单命令由注册表动态挂载）
├── leaderboard.py   榜单注册表（BOARDS）+ 排行榜实现 + 帮助自动生成
├── constants.py     常量：服务器、对局类型、额外播报项、公开命令
├── config.py        配置模型
├── api.py           Vortex API 客户端（图鉴/玩家查询）
├── stats.py         统计快照、差值检测、播报格式化
├── cards.py         help/status/list 图片卡片生成（榜单开关区自动生成）
├── battle_log.py    战斗日志存储与查询
├── utils.py         模板读取、背景图、图片渲染发送
├── config.toml      插件配置
├── _manifest.json   插件清单
├── assets/templates/*.html   图片卡片模板
├── assets/images/           背景图（按页面名或通用 bg）
├── README.md        本说明
└── boards_help.md   榜单增删使用说明书
```

## 榜单扩展

新增/删除排行榜请阅读 **`boards_help.md`**。核心：在 `leaderboard.py` 注册一行，命令、排行榜帮助、管理员开关、status 状态、跨天结算全部自动生成。
