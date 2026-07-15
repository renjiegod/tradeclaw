import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from doyoutrade.assistant.trading_sessions import (
    ashare_continuous_trading_skip_reason,
    is_ashare_continuous_trading,
)


class AshareTradingSessionTests(unittest.TestCase):
    def test_continuous_trading_boundaries(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        monday = datetime(2026, 6, 8, tzinfo=tz)
        cases = {
            monday.replace(hour=9, minute=14): False,
            monday.replace(hour=9, minute=15): True,
            monday.replace(hour=11, minute=30): True,
            monday.replace(hour=11, minute=35): False,
            monday.replace(hour=12, minute=59): False,
            monday.replace(hour=13, minute=0): True,
            monday.replace(hour=15, minute=0): True,
            monday.replace(hour=15, minute=5): False,
        }
        for instant, expected in cases.items():
            with self.subTest(instant=instant.strftime("%H:%M"), expected=expected):
                self.assertEqual(
                    is_ashare_continuous_trading(
                        instant.astimezone(timezone.utc),
                        timezone="Asia/Shanghai",
                    ),
                    expected,
                )

    def test_skip_reason_manual_bypass(self) -> None:
        instant = datetime(2026, 6, 8, 8, 0, tzinfo=timezone.utc)
        self.assertIsNone(
            ashare_continuous_trading_skip_reason(
                instant,
                timezone="Asia/Shanghai",
                trading_session="ashare",
                manual=True,
            ),
        )

    def test_skip_reason_outside_session(self) -> None:
        instant = datetime(2026, 6, 8, 1, 0, tzinfo=timezone.utc)  # 09:00 CST
        reason = ashare_continuous_trading_skip_reason(
            instant,
            timezone="Asia/Shanghai",
            trading_session="ashare",
            manual=False,
        )
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertEqual(reason["reason"], "outside_trading_session")
