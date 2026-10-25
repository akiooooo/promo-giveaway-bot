import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from promo_bot.app import PromoBot
from promo_bot.config import Settings
from promo_bot.database import Database


class FakeClient:
    def __init__(self, subscribed: bool = True):
        self.subscribed = subscribed
        self.messages = []

    def is_channel_member(self, channel_id: int, user_id: int) -> bool:
        return self.subscribed

    def send_message(self, user_id, text, buttons=None, image_token="", notify=True):
        self.messages.append((user_id, text, buttons))

    def answer_callback(self, callback_id, text, buttons=None):
        self.messages.append((callback_id, text, buttons))


class FixedBot(PromoBot):
    def now(self):
        return datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))


class AppTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.settings = Settings(
            token="token",
            db_path=":memory:",
            api_url="https://example.test",
            timezone=ZoneInfo("Europe/Moscow"),
            grand_prize="билеты",
            max_chances=10,
            final_winners=5,
            final_reserves=5,
            banner_token="",
        )
        self.partner_id = self.db.add_partner(
            {
                "slug": "partner-one",
                "name": "Партнёр",
                "campaign_day": "2026-11-01",
                "channel_id": -1001,
                "channel_url": "https://max.ru/partner",
                "promo_place": "сайте",
                "promo_expires_on": "2026-11-30",
                "daily_prize": "приз",
                "prize_instructions": "ответить",
                "winner_count": 1,
            }
        )
        self.user = {"user_id": 7, "first_name": "Иван", "username": "ivan"}
        self.db.upsert_user(self.user, datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo("Europe/Moscow")))

    def tearDown(self):
        self.db.close()

    def test_successful_subscription_registers_user(self):
        bot = FixedBot(self.settings, self.db, FakeClient(True))
        text, _ = bot.callback_response(7, "check:subscription")
        self.assertIn("зарегистрирована", text)
        self.assertTrue(self.db.is_participant(7, self.partner_id))

    def test_failed_subscription_can_be_retried(self):
        bot = FixedBot(self.settings, self.db, FakeClient(False))
        text, buttons = bot.callback_response(7, "check:subscription")
        self.assertIn("Пока не вижу подписку", text)
        self.assertEqual(buttons[1][0]["payload"], "check:subscription")
        self.assertFalse(self.db.is_participant(7, self.partner_id))


if __name__ == "__main__":
    unittest.main()
