# 背景图说明

本目录存放各图片输出页面的背景图。**图片文件不会上传到 git 仓库**（见 .gitignore），部署到服务器后请手动放入本目录。

## 支持的格式

- .png
- .jpg / .jpeg

## 命名规则

按页面专用优先，其次通用底图：

| 类型 | 命名 | 示例 |
|---|---|---|
| 页面专用 | {页面名}_bg.png / {页面名}_bg.jpg | help_bg.jpg |
| 通用底图 | bg.png / bg.jpg | bg.jpg |
| 其他通用名 | panel_bg.* / background.* | panel_bg.png |

当前使用的页面名：help（帮助）、status（状态）、list（列表）、king（窝王榜）、wopi（窝批榜）

## 加载优先级

1. 页面专用图（如 help_bg.jpg）
2. 通用底图（bg.jpg、panel_bg.*、background.*）
3. 均不存在时使用默认浅蓝渐变背景

## 建议

- 建议使用竖版长图（如 700x1200 比例），渲染时按 cover 铺满并优先显示中上区域
- 图片较大时 base64 内嵌会拖慢渲染，建议控制在 1MB 以内
