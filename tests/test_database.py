import random
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from promo_bot.database import Database


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.zone = ZoneInfo("Europe/Moscow")
        self.now = datetime(2026, 11, 1, 12, 0, tzinfo=self.zone)
        self.partner_id = self.db.add_partner(
            {
                "slug": "partner-one",
                "name": "Партнёр дня",
                "campaign_day": "2026-11-01",
                "channel_id": -1001,
                "channel_url": "https://max.ru/partner",
                "promo_place": "сайте партнёра",
                "promo_expires_on": "2026-11-30",
                "daily_prize": "мерч",
                "prize_instructions": "Ответь в течение суток",
                "winner_count": 2,
                "claim_hours": 24,
            }
        )

    def tearDown(self):
        self.db.close()

    def add_user(self, user_id: int) -> None:
        self.db.upsert_user(
            {"user_id": user_id, "username": f"user{user_id}", "first_name": "Имя"}, self.now
        )

    def test_same_promo_is_returned_again(self):
        self.add_user(1)
        self.db.add_promo_codes("partner-one", ["CODE-1", "CODE-2"])
        first = self.db.request_promo(1, self.partner_id, self.now)
        second = self.db.request_promo(1, self.partner_id, self.now + timedelta(hours=1))
        self.assertEqual(first.status, "assigned")
        self.assertEqual(second.status, "existing")
        self.assertEqual(first.code, second.code)
        self.assertEqual(self.db.get_user_progress(1, 10)["chances"], 1)
        self.assertEqual(self.db.get_stats(date(2026, 11, 1))["promo_requests"], 2)

    def test_no_promo_does_not_block_participation(self):
        self.add_user(1)
        result = self.db.request_promo(1, self.partner_id, self.now)
        created = self.db.register_participation(1, self.partner_id, self.now)
        self.assertEqual(result.status, "exhausted")
        self.assertTrue(created)
        self.assertEqual(self.db.get_user_progress(1, 10)["chances"], 1)
        self.assertEqual(self.db.get_stats(date(2026, 11, 1))["partners"][0]["promos_left"], 0)

    def test_two_actions_in_one_day_give_one_chance(self):
        self.add_user(1)
        self.db.add_promo_codes("partner-one", ["CODE-1"])
        self.db.request_promo(1, self.partner_id, self.now)
        self.db.register_participation(1, self.partner_id, self.now)
        self.db.register_participation(1, self.partner_id, self.now)
        progress = self.db.get_user_progress(1, 10)
        self.assertEqual(progress["chances"], 1)
        self.assertEqual(progress["partners"], ["Партнёр дня"])

    def test_chances_are_capped(self):
        self.add_user(1)
        with self.db.transaction() as conn:
            for offset in range(14):
                day = date(2026, 10, 1) + timedelta(days=offset)
                conn.execute(
                    "INSERT INTO activity_days VALUES(?, ?, ?, 'promo', ?)",
                    (1, day.isoformat(), self.partner_id, self.now.isoformat()),
                )
        self.assertEqual(self.db.get_user_progress(1, 10)["chances"], 10)
        self.assertEqual(self.db.get_user_progress(1, 10)["remaining"], 0)

    def test_daily_draw_has_unique_winners_and_waits_for_approval(self):
        for user_id in range(1, 7):
            self.add_user(user_id)
            self.db.register_participation(user_id, self.partner_id, self.now)
        draw_id = self.db.create_daily_draw(date(2026, 11, 1), self.now, random.Random(4))
        draw = self.db.get_draw(draw_id)
        winners = [row["user_id"] for row in draw["entries"] if row["role"] == "winner"]
        self.assertEqual(len(winners), 2)
        self.assertEqual(len(set(winners)), 2)
        self.assertEqual(draw["status"], "pending")
        self.assertEqual(self.db.get_queued_notifications(), [])
        queued = self.db.approve_draw(draw_id, self.now, "билеты")
        self.assertEqual(queued, 2)
        self.assertEqual(len(self.db.get_queued_notifications()), 2)

    def test_daily_draw_cannot_be_repeated(self):
        self.add_user(1)
        self.db.register_participation(1, self.partner_id, self.now)
        self.db.create_daily_draw(date(2026, 11, 1), self.now, random.Random(1))
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.db.create_daily_draw(date(2026, 11, 1), self.now, random.Random(1))

    def test_final_draw_uses_unique_people(self):
        for user_id in range(1, 13):
            self.add_user(user_id)
            with self.db.transaction() as conn:
                for offset in range((user_id % 10) + 1):
                    day = date(2026, 10, 1) + timedelta(days=offset)
                    conn.execute(
                        "INSERT OR IGNORE INTO activity_days VALUES(?, ?, ?, 'promo', ?)",
                        (user_id, day.isoformat(), self.partner_id, self.now.isoformat()),
                    )
        draw_id = self.db.create_final_draw(
            date(2026, 11, 2), self.now, 5, 5, 10, random.Random(8)
        )
        draw = self.db.get_draw(draw_id)
        selected = [row["user_id"] for row in draw["entries"] if row["role"] in {"winner", "reserve"}]
        self.assertEqual(len(selected), 10)
        self.assertEqual(len(selected), len(set(selected)))
        self.assertEqual(len([row for row in draw["entries"] if row["role"] == "winner"]), 5)
        self.assertLessEqual(max(row["chances"] for row in draw["entries"]), 10)

    def test_claim_collects_name_and_consent(self):
        self.add_user(1)
        self.db.register_participation(1, self.partner_id, self.now)
        draw_id = self.db.create_daily_draw(date(2026, 11, 1), self.now, random.Random(1))
        self.db.approve_draw(draw_id, self.now, "билеты")
        self.db.save_claim_name(draw_id, 1, "Иван", "Петров", self.now)
        self.db.submit_consent(draw_id, 1, True, self.now)
        with self.db.transaction() as conn:
            claim = conn.execute("SELECT * FROM claims WHERE draw_id = ?", (draw_id,)).fetchone()
        self.assertEqual(claim["status"], "submitted")
        self.assertEqual(claim["consent"], 1)

    def test_expired_winner_is_replaced_by_reserve(self):
        for user_id in range(1, 5):
            self.add_user(user_id)
            self.db.register_participation(user_id, self.partner_id, self.now)
        draw_id = self.db.create_daily_draw(date(2026, 11, 1), self.now, random.Random(2))
        self.db.approve_draw(draw_id, self.now, "билеты")
        promoted = self.db.expire_claims(self.now + timedelta(hours=25), "билеты")
        roles = [row["role"] for row in self.db.get_draw(draw_id)["entries"]]
        self.assertEqual(promoted, 2)
        self.assertEqual(roles.count("expired"), 2)
        self.assertEqual(roles.count("promoted"), 2)


if __name__ == "__main__":
    unittest.main()
