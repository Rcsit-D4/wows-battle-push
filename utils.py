# -*- coding: utf-8 -*-
"""工具类：HTML模板读取、背景图处理、图片渲染发送"""

import base64
import os
from string import Template
from typing import Any

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(_PLUGIN_DIR, "assets", "templates")
IMAGES_DIR = os.path.join(_PLUGIN_DIR, "assets", "images")


def read_template(name: str) -> Template:
    """读取 HTML 模板文件"""
    path = os.path.join(TEMPLATES_DIR, name)
    with open(path, encoding="utf-8") as f:
        return Template(f.read())


def find_background_image(page: str = "") -> str | None:
    """查找背景图并转为 data URI，优先按页面名（如 help_bg），其次通用底图"""
    search_dirs = [IMAGES_DIR, os.path.join(_PLUGIN_DIR, "assets"), _PLUGIN_DIR]
    candidates = []
    if page:
        candidates += [(f"{page}_bg.png", "png"), (f"{page}_bg.jpg", "jpeg"), (f"{page}_bg.jpeg", "jpeg")]
    candidates += [
        ("panel_bg.png", "png"), ("panel_bg.jpg", "jpeg"),
        ("background.png", "png"), ("background.jpg", "jpeg"),
        ("bg.png", "png"), ("bg.jpg", "jpeg"),
    ]
    for d in search_dirs:
        for name, mime in candidates:
            path = os.path.join(d, name)
            if os.path.isfile(path):
                try:
                    with open(path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("ascii")
                    return f"data:image/{mime};base64,{b64}"
                except Exception:
                    pass
    return None


def bg_style(page: str = "") -> str:
    """生成背景 CSS：有底图则 cover 铺满取中上区域，否则浅蓝渐变"""
    bg_uri = find_background_image(page)
    if bg_uri:
        return (
            f"background-image: url('{bg_uri}');"
            " background-size: cover; background-position: center 30%; background-repeat: no-repeat;"
        )
    return "background: linear-gradient(160deg, #e8f0fe 0%, #d4e4fc 50%, #c3d9f9 100%);"


def extract_image_data(result: Any) -> str:
    """从 html2png 返回值提取 base64 图片字符串（兼容 bytes/str/dict）"""
    if isinstance(result, bytes):
        return base64.b64encode(result).decode("ascii")
    if isinstance(result, str):
        return result.split(",", 1)[1] if result.startswith("data:image") else result
    if isinstance(result, dict):
        for key in ("image_base64", "base64", "data", "png", "image", "image_data"):
            val = result.get(key)
            if val:
                if isinstance(val, bytes):
                    return base64.b64encode(val).decode("ascii")
                if isinstance(val, str):
                    return val.split(",", 1)[1] if val.startswith("data:image") else val
        for val in result.values():
            if isinstance(val, bytes):
                return base64.b64encode(val).decode("ascii")
    raise TypeError(f"无法从 html2png 结果提取图片: {type(result)}")


async def send_html_image(ctx, stream_id: str, html: str, fallback_text: str, logger=None) -> tuple[bool, str, int]:
    """渲染 HTML 为图片并发送，失败时降级为纯文本"""
    try:
        result = await ctx.render.html2png(html=html)
        image_data = extract_image_data(result)
        await ctx.send.image(image_data=image_data, stream_id=stream_id)
        return True, "", 0
    except Exception:
        if logger:
            logger.exception("图片渲染失败，降级为文本")
        await ctx.send.text(fallback_text, stream_id)
        return True, fallback_text, 1
