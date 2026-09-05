# -*- coding: utf-8 -*-
"""Vortex API 客户端：用户舰船统计与战舰图鉴"""

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from constants import ENCYCLOPEDIA_HEADERS, ENCYCLOPEDIA_URL, SERVER_VORTEX, VORTEX_HEADERS


class WowsApi:
    def __init__(self, timeout: int = 30, retries: int = 2) -> None:
        self.timeout = timeout
        self.retries = retries

    def _get(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        """同步 HTTP GET，带重试；headers 缺省使用 Vortex 头"""
        headers = headers or VORTEX_HEADERS
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    return resp.read()
            except (urllib.error.URLError, OSError, RuntimeError) as e:
                last_exc = e
                if attempt < self.retries - 1:
                    continue
        raise RuntimeError(f"请求失败 {url}: {last_exc}") from last_exc

    async def _get_async(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        return await asyncio.to_thread(self._get, url, headers)

    @staticmethod
    def _parse_json(raw: bytes, url: str) -> dict:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"JSON 解析失败 {url}: {e}") from e

    async def fetch_user_ships(
        self, server: str, account_id: int, battle_type: str
    ) -> tuple[str, dict[int, dict[str, Any]]]:
        """返回 (账号昵称, {shipId: 该对局类型统计})"""
        server_upper = server.upper()
        if server_upper not in SERVER_VORTEX:
            raise ValueError(f"无效服务器: {server}")
        base = SERVER_VORTEX[server_upper]
        bt = battle_type.lower()
        url = f"{base}/api/accounts/{account_id}/ships/{bt}/"

        data = self._parse_json(await self._get_async(url), url)
        if data.get("status") != "ok":
            raise RuntimeError(f"Vortex API 异常 status={data.get('status')} url={url}")

        records = data.get("data") or {}
        if not records:
            return f"Account{account_id}", {}

        first = next(iter(records.values()))
        name = first.get("name") or f"Account{account_id}"
        stats: dict[int, dict[str, Any]] = {}
        for sid, v in (first.get("statistics") or {}).items():
            bt_stats = v.get(bt) or {}
            if bt_stats:
                stats[int(sid)] = bt_stats
        return name, stats

    async def fetch_encyclopedia(self) -> dict[int, str]:
        """返回 {shipId: 船名}，中文名优先"""
        data = self._parse_json(
            await self._get_async(ENCYCLOPEDIA_URL, ENCYCLOPEDIA_HEADERS), ENCYCLOPEDIA_URL
        )
        out: dict[int, str] = {}
        for it in data.get("data") or []:
            sid = it.get("shipId")
            if sid is None:
                continue
            name = it.get("nameCn") or it.get("nameEnglish") or it.get("name") or f"Ship{sid}"
            out[int(sid)] = name
        return out

    async def search_player(self, server: str, nickname: str) -> tuple[int, str] | None:
        """通过昵称搜索玩家，返回 (account_id, name)，未找到返回 None"""
        server_upper = server.upper()
        if server_upper not in SERVER_VORTEX:
            raise ValueError(f"无效服务器: {server}")
        base = SERVER_VORTEX[server_upper]
        url = f"{base}/api/accounts/search/{urllib.parse.quote(nickname)}/"

        try:
            data = self._parse_json(await self._get_async(url), url)
        except RuntimeError:
            return None
        if data.get("status") != "ok":
            return None

        results = data.get("data") or []
        target = nickname.lower()
        for r in results:  # 精确匹配（大小写不敏感）
            if str(r.get("name", "")).lower() == target and r.get("spa_id"):
                return int(r["spa_id"]), str(r["name"])
        for r in results:  # 无精确匹配时取第一个有效结果
            if r.get("spa_id"):
                return int(r["spa_id"]), str(r.get("name", nickname))
        return None
