# -*- coding: utf-8 -*-
"""WP1-WP7 修复的回归测试（不依赖真实 MaiBot 宿主/网络）。

运行方式（在插件目录）：
    python -m unittest discover -s tests -v
"""

import json
import logging
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import wows_leaderboard as leaderboard  # noqa: E402
import wows_nl_query as nl_query  # noqa: E402
import wows_stats as stats  # noqa: E402
from plugin import WowsBattlePushPlugin  # noqa: E402
from wows_state_store import StateStore  # noqa: E402
from wows_utils import send_html_image  # noqa: E402


def _record(day: str, account_id: int = 1, damage: int = 1000) -> dict:
    return {
        "date": day,
        "server": "ASIA",
        "account_id": account_id,
        "account_name": "Tester",
        "ship_id": 1,
        "ship_name": "TestShip",
        "battle_type": "pvp_solo",
        "battles": 1,
        "wins": 1,
        "losses": 0,
        "damage": damage,
        "kills": 1,
        "xp": 100,
        "potential": 0,
        "scouting": 0,
    }


def _sd(day: str, king: bool = True, wopi: bool = False) -> dict:
    return {
        "date": day,
        "enabled": {"king": king, "wopi": wopi},
        "history": {},
        "monthly": {},
        "last": {},
        "accounts": {},
        "wopi_accounts": {},
    }


class StreamDataTests(unittest.TestCase):
    def test_get_stream_data_preserves_date_and_filters_unknown_boards(self) -> None:
        old_date = "2020-01-01"
        state = {
            "daily_king": {
                "s1": {
                    "date": old_date,
                    "enabled": {"king": True, "wopi": False, "removed": True},
                    "history": {},
                    "monthly": {},
                }
            }
        }
        sd = leaderboard.get_stream_data(state, "s1")
        self.assertEqual(sd["date"], old_date)
        self.assertEqual(set(sd["enabled"]), set(leaderboard.BOARDS))
        self.assertNotIn("removed", sd["enabled"])

    def test_get_stream_data_creates_today_for_new_stream(self) -> None:
        state = {"daily_king": {}}
        sd = leaderboard.get_stream_data(state, "new")
        self.assertEqual(sd["date"], date.today().isoformat())
        self.assertEqual(set(sd["enabled"]), set(leaderboard.BOARDS))


class MonthlyTests(unittest.TestCase):
    def test_monthly_update_uses_explicit_month(self) -> None:
        sd = {"monthly": {}}
        ranked = [{
            "key": "ASIA:1", "name": "Tester", "group_nickname": None,
            "rank": 1, "best_damage": 1234, "ship_name": "TestShip",
        }]
        leaderboard._update_monthly_king(sd, ranked, "2026-09")
        self.assertIn("2026-09", sd["monthly"])
        self.assertNotIn("2026-10", sd["monthly"])
        self.assertEqual(sd["monthly"]["2026-09"]["king"]["ASIA:1"]["score"], 5)


class MonthlyBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_day_of_month_credits_previous_month(self) -> None:
        class FakeDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 11, 1)

        original_date = leaderboard.date
        leaderboard.date = FakeDate
        try:
            state = {"daily_king": {"s1": _sd("2026-10-31")}}
            sent: list[str] = []

            async def send(_ctx, stream_id, _html, _fallback, _logger):
                sent.append(stream_id)

            def get_records(_stream_id, day):
                return [_record(day)] if day == "2026-10-31" else []

            await leaderboard.check_daily_reset(
                state, object(), send, logging.getLogger("test"),
                get_records_fn=get_records,
                is_monitored_fn=lambda _sid, _server, _aid: True,
                send_push=True,
            )
            sd = state["daily_king"]["s1"]
            self.assertIn("2026-10-31", sd["history"])
            self.assertIn("2026-10", sd["monthly"])
            self.assertNotIn("2026-11", sd["monthly"])
            self.assertEqual(sent, ["s1"])
        finally:
            leaderboard.date = original_date


class BattleFormatTests(unittest.TestCase):
    def test_win_is_good_news_and_loss_is_bad_news(self) -> None:
        win = stats.format_battle("A", "X", {"battles": 1, "wins": 1, "losses": 0, "damage": 1000}, "pvp_solo", 3, {})
        loss = stats.format_battle("A", "X", {"battles": 1, "wins": 0, "losses": 1, "damage": 1000}, "pvp_solo", 3, {})
        self.assertTrue(win.startswith("喜报"))
        self.assertTrue(loss.startswith("悲报"))


class ShipTypeTests(unittest.TestCase):
    def test_english_and_chinese_ship_types(self) -> None:
        self.assertEqual(nl_query.normalize_ship_type("Battleship", None), "Battleship")
        self.assertEqual(nl_query.normalize_ship_type("航母", None), "AirCarrier")
        self.assertIsNone(nl_query.normalize_ship_type("不存在的舰种", None))


class SenderTests(unittest.TestCase):
    def test_sender_from_host_user_id(self) -> None:
        plugin = WowsBattlePushPlugin()
        self.assertEqual(plugin._get_sender_id({"user_id": "123"}), "123")

    def test_sender_from_documented_message_object(self) -> None:
        plugin = WowsBattlePushPlugin()
        kwargs = {
            "message": {
                "message_info": {
                    "user_info": {"user_id": "456", "user_nickname": "tester"},
                }
            }
        }
        self.assertEqual(plugin._get_sender_id(kwargs), "456")


class DailyResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_day_pushes_and_updates_history_monthly(self) -> None:
        today = date.today()
        yesterday = today - timedelta(days=1)
        state = {"daily_king": {"s1": _sd(yesterday.isoformat())}}
        sent: list[str] = []

        async def send(_ctx, stream_id, _html, _fallback, _logger):
            sent.append(stream_id)

        records = {yesterday.isoformat(): [_record(yesterday.isoformat())]}

        def get_records(_stream_id, day):
            return records.get(day, [])

        changed = await leaderboard.check_daily_reset(
            state, object(), send, logging.getLogger("test"),
            get_records_fn=get_records,
            is_monitored_fn=lambda _sid, _server, _aid: True,
            send_push=True,
        )
        self.assertTrue(changed)
        self.assertEqual(state["daily_king"]["s1"]["date"], today.isoformat())
        self.assertIn(yesterday.isoformat(), state["daily_king"]["s1"]["history"])
        self.assertIn(yesterday.strftime("%Y-%m"), state["daily_king"]["s1"]["monthly"])
        self.assertEqual(sent, ["s1"])

    async def test_paused_stream_settles_without_push(self) -> None:
        today = date.today()
        yesterday = today - timedelta(days=1)
        state = {"daily_king": {"s1": _sd(yesterday.isoformat())}}
        sent: list[str] = []

        async def send(_ctx, stream_id, _html, _fallback, _logger):
            sent.append(stream_id)

        def get_records(_stream_id, day):
            return [_record(day)] if day == yesterday.isoformat() else []

        await leaderboard.check_daily_reset(
            state, object(), send, logging.getLogger("test"),
            get_records_fn=get_records,
            is_monitored_fn=lambda _sid, _server, _aid: True,
            is_paused_fn=lambda _sid: True,
            send_push=True,
        )
        self.assertEqual(sent, [])
        self.assertIn(yesterday.isoformat(), state["daily_king"]["s1"]["history"])

    async def test_repeated_settlement_does_not_push_again(self) -> None:
        today = date.today()
        yesterday = today - timedelta(days=1)
        state = {"daily_king": {"s1": _sd(yesterday.isoformat())}}
        sent: list[str] = []

        async def send(_ctx, stream_id, _html, _fallback, _logger):
            sent.append(stream_id)

        def get_records(_stream_id, day):
            return [_record(day)] if day == yesterday.isoformat() else []

        kwargs = dict(
            get_records_fn=get_records,
            is_monitored_fn=lambda _sid, _server, _aid: True,
            send_push=True,
        )
        await leaderboard.check_daily_reset(state, object(), send, logging.getLogger("test"), **kwargs)
        await leaderboard.check_daily_reset(state, object(), send, logging.getLogger("test"), **kwargs)
        self.assertEqual(sent, ["s1"])

    async def test_disabled_push_still_settles(self) -> None:
        today = date.today()
        yesterday = today - timedelta(days=1)
        state = {"daily_king": {"s1": _sd(yesterday.isoformat())}}
        sent: list[str] = []

        async def send(_ctx, stream_id, _html, _fallback, _logger):
            sent.append(stream_id)

        def get_records(_stream_id, day):
            return [_record(day)] if day == yesterday.isoformat() else []

        await leaderboard.check_daily_reset(
            state, object(), send, logging.getLogger("test"),
            get_records_fn=get_records,
            is_monitored_fn=lambda _sid, _server, _aid: True,
            send_push=False,
        )
        self.assertEqual(sent, [])
        self.assertIn(yesterday.isoformat(), state["daily_king"]["s1"]["history"])
        self.assertEqual(state["daily_king"]["s1"]["date"], today.isoformat())

    async def test_backfill_settles_multiple_days_without_push(self) -> None:
        today = date.today()
        start = today - timedelta(days=3)
        state = {"daily_king": {"s1": _sd(start.isoformat())}}
        sent: list[str] = []
        records = {
            (start + timedelta(days=offset)).isoformat(): [_record((start + timedelta(days=offset)).isoformat())]
            for offset in range(3)
        }

        async def send(_ctx, stream_id, _html, _fallback, _logger):
            sent.append(stream_id)

        def get_records(_stream_id, day):
            return records.get(day, [])
        await leaderboard.check_daily_reset(
            state, object(), send, logging.getLogger("test"),
            get_records_fn=get_records,
            is_monitored_fn=lambda _sid, _server, _aid: True,
            send_push=True,
        )
        sd = state["daily_king"]["s1"]
        self.assertEqual(sent, [])
        for offset in range(3):
            self.assertIn((start + timedelta(days=offset)).isoformat(), sd["history"])
        self.assertEqual(sd["date"], today.isoformat())


class FakeLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


class FakeApiPartial:
    async def fetch_user_ships(self, _server, _account_id, battle_type):
        if battle_type == "rank_solo":
            raise RuntimeError("simulated failure")
        return "Tester", {
            1: {
                "battles_count": 2,
                "wins": 2,
                "losses": 0,
                "damage_dealt": 5000,
                "frags": 2,
                "original_exp": 500,
                "scouting_damage": 0,
            }
        }


class FakeStore:
    def __init__(self):
        self.pending = []
        self.snapshots = {}

    def save_pending(self):
        return True

    def save_snapshots(self):
        return True


class FakeSend:
    def __init__(self, sink: list[tuple[str, str]]):
        self.sink = sink

    async def text(self, text, stream_id):
        self.sink.append((stream_id, text))
        return True

    async def image(self, image_data, stream_id):
        return True


class FakeCtx:
    def __init__(self, sink: list[tuple[str, str]] | None = None):
        self.logger = FakeLogger()
        self.send = FakeSend(sink if sink is not None else [])


class FakeApiManual:
    async def fetch_user_ships(self, _server, _account_id, _battle_type):
        return "Tester", {
            1: {
                "battles_count": 2,
                "wins": 2,
                "losses": 0,
                "damage_dealt": 2000,
                "frags": 2,
                "original_exp": 200,
                "scouting_damage": 0,
            }
        }


class SnapshotMergeTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_failure_keeps_old_battle_type(self) -> None:
        plugin = WowsBattlePushPlugin()
        plugin._ctx = FakeCtx()
        plugin._api = FakeApiPartial()
        plugin._battle_log = None
        plugin._store = FakeStore()
        old_pvp = {
            "battles_count": 1, "wins": 1, "losses": 0, "damage_dealt": 1000,
            "frags": 1, "original_exp": 100, "scouting_damage": 0,
        }
        plugin._state = {
            "version": 1, "bindings": {}, "daily_king": {}, "pending": [],
            "snapshots": {
                "ASIA:1": {
                    "name": "Tester",
                    "battle_types": {
                        "pvp_solo": {1: old_pvp},
                        "rank_solo": {2: dict(old_pvp, battles_count=3)},
                    },
                }
            },
        }
        results = await plugin._check_account("ASIA", 1, ["pvp_solo", "rank_solo"], push=True)
        self.assertIn("pvp_solo", results)
        battle_types = plugin._state["snapshots"]["ASIA:1"]["battle_types"]
        self.assertIn("pvp_solo", battle_types)
        self.assertIn("rank_solo", battle_types)
        self.assertEqual(battle_types["pvp_solo"][1]["battles_count"], 2)
        self.assertEqual(battle_types["rank_solo"][2]["battles_count"], 3)


class StorageTests(unittest.TestCase):
    def test_legacy_migration_and_pending_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "data.json").write_text(
                json.dumps({
                    "bindings": {"s1": {"accounts": [], "paused": False}},
                    "snapshots": {"ASIA:1": {"name": "Tester", "battle_types": {}}},
                    "daily_king": {"s1": {"date": "2026-01-01", "enabled": {"king": True}}},
                }),
                encoding="utf-8",
            )
            store = StateStore(root)
            self.assertIn("s1", store.groups["bindings"])
            self.assertIn("ASIA:1", store.snapshots)
            self.assertIn("s1", store.leaderboard)
            self.assertTrue((root / "data" / "data.json.bak").exists())
            self.assertEqual(store.pending, [])

            store.pending = [{"id": "e1", "pending_streams": ["s1"]}]
            self.assertTrue(store.save_pending())
            reloaded = StateStore(root)
            self.assertEqual(reloaded.pending, store.pending)


class RenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_render_requests_full_page(self) -> None:
        captured: dict = {}

        class Render:
            async def html2png(self, **kwargs):
                captured.update(kwargs)
                return {"image_base64": "ZmFrZQ=="}

        class Send:
            async def image(self, image_data, stream_id):
                captured["image_data"] = image_data
                captured["image_stream"] = stream_id
                return True

            async def text(self, text, stream_id):
                captured["fallback"] = text
                return True

        class Ctx:
            render = Render()
            send = Send()

        ok, text, level = await send_html_image(Ctx(), "s1", "<html></html>", "fallback")
        self.assertTrue(ok)
        self.assertEqual(text, "")
        self.assertEqual(level, 1)
        self.assertTrue(captured["full_page"])
        self.assertEqual(captured["image_stream"], "s1")


class PendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_merge_and_drain(self) -> None:
        plugin = WowsBattlePushPlugin()
        plugin._store = FakeStore()
        plugin._state = {"pending": []}
        plugin._is_stream_active_for_account = lambda _sid, _server, _aid: True
        sent: list[str] = []

        async def fake_push(_event, stream_id):
            sent.append(stream_id)
            return True

        plugin._push_event_to_stream = fake_push
        event = {"id": "e1", "server": "ASIA", "account_id": 1, "created": time.time(), "pending_streams": []}
        plugin._enqueue_pending(event, {"s2"})
        plugin._enqueue_pending(event, {"s3"})
        self.assertEqual(plugin._state["pending"][0]["pending_streams"], ["s2", "s3"])
        await plugin._drain_pending()
        self.assertEqual(sorted(sent), ["s2", "s3"])
        self.assertEqual(plugin._state["pending"], [])

    async def test_pause_drops_pending(self) -> None:
        plugin = WowsBattlePushPlugin()
        plugin._store = FakeStore()
        plugin._state = {"pending": [{"id": "e1", "pending_streams": ["s1", "s2"]}]}
        plugin._drop_pending_for_stream("s1")
        self.assertEqual(plugin._state["pending"][0]["pending_streams"], ["s2"])


class ManualCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_check_pushes_current_and_queues_other_groups(self) -> None:
        plugin = WowsBattlePushPlugin()
        sent: list[tuple[str, str]] = []
        plugin._ctx = FakeCtx(sent)
        plugin._api = FakeApiManual()
        plugin._battle_log = None
        plugin._store = FakeStore()
        old_pvp = {
            "battles_count": 1, "wins": 1, "losses": 0, "damage_dealt": 1000,
            "frags": 1, "original_exp": 100, "scouting_damage": 0,
        }
        plugin._state = {
            "version": 1,
            "bindings": {
                "s1": {"accounts": [{"server": "ASIA", "account_id": 1}], "display_mode": 3, "paused": False},
                "s2": {"accounts": [{"server": "ASIA", "account_id": 1}], "display_mode": 3, "paused": False},
            },
            "snapshots": {"ASIA:1": {"name": "Tester", "battle_types": {"pvp_solo": {1: old_pvp}}}},
            "daily_king": {},
            "pending": [],
        }

        results = await plugin._check_account(
            "ASIA", 1, ["pvp_solo"], push=True, stream_ids={"s1"}, queue_others=True,
        )
        self.assertIn("pvp_solo", results)
        self.assertEqual([sid for sid, _ in sent], ["s1"])
        self.assertEqual(plugin._state["pending"][0]["pending_streams"], ["s2"])

        await plugin._drain_pending()
        self.assertEqual([sid for sid, _ in sent], ["s1", "s2"])
        self.assertEqual(plugin._state["pending"], [])


if __name__ == "__main__":
    unittest.main()
