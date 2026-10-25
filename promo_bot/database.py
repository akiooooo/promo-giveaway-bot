import json
import random
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class PromoResult:
    status: str
    code: str | None
    partner: dict


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._memory = None
        if path == ":memory:":
            self._memory = sqlite3.connect(path, check_same_thread=False)
            self._memory.row_factory = sqlite3.Row
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        if self._memory is not None:
            conn = self._memory
        else:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def close(self) -> None:
        if self._memory is not None:
            self._memory.close()
            self._memory = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if self._memory is None:
                conn.close()

    def init_schema(self) -> None:
        conn = self.connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS partners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                campaign_day TEXT NOT NULL UNIQUE,
                channel_id INTEGER NOT NULL,
                channel_url TEXT NOT NULL,
                promo_place TEXT NOT NULL,
                promo_expires_on TEXT NOT NULL,
                daily_prize TEXT NOT NULL,
                prize_instructions TEXT NOT NULL,
                winner_count INTEGER NOT NULL CHECK(winner_count > 0),
                claim_hours INTEGER NOT NULL DEFAULT 24 CHECK(claim_hours > 0)
            );

            CREATE TABLE IF NOT EXISTS promo_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                partner_id INTEGER NOT NULL REFERENCES partners(id),
                code TEXT NOT NULL,
                assigned_user_id INTEGER REFERENCES users(user_id),
                assigned_at TEXT,
                UNIQUE(partner_id, code),
                UNIQUE(partner_id, assigned_user_id)
            );

            CREATE TABLE IF NOT EXISTS promo_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                partner_id INTEGER NOT NULL REFERENCES partners(id),
                requested_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS activity_days (
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                activity_day TEXT NOT NULL,
                partner_id INTEGER NOT NULL REFERENCES partners(id),
                first_source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, activity_day)
            );

            CREATE TABLE IF NOT EXISTS partner_interactions (
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                partner_id INTEGER NOT NULL REFERENCES partners(id),
                first_interaction_at TEXT NOT NULL,
                PRIMARY KEY(user_id, partner_id)
            );

            CREATE TABLE IF NOT EXISTS participations (
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                partner_id INTEGER NOT NULL REFERENCES partners(id),
                registered_at TEXT NOT NULL,
                PRIMARY KEY(user_id, partner_id)
            );

            CREATE TABLE IF NOT EXISTS draws (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK(kind IN ('daily', 'final')),
                partner_id INTEGER REFERENCES partners(id),
                draw_day TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved')),
                created_at TEXT NOT NULL,
                approved_at TEXT,
                winner_count INTEGER NOT NULL,
                reserve_count INTEGER NOT NULL,
                participant_count INTEGER NOT NULL,
                UNIQUE(kind, partner_id, draw_day)
            );

            CREATE TABLE IF NOT EXISTS draw_entries (
                draw_id INTEGER NOT NULL REFERENCES draws(id),
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                chances INTEGER NOT NULL DEFAULT 1,
                role TEXT NOT NULL DEFAULT 'participant'
                    CHECK(role IN ('participant', 'winner', 'reserve', 'expired', 'promoted')),
                position INTEGER,
                PRIMARY KEY(draw_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS claims (
                draw_id INTEGER NOT NULL REFERENCES draws(id),
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                status TEXT NOT NULL DEFAULT 'waiting'
                    CHECK(status IN ('waiting', 'submitted', 'declined', 'expired')),
                first_name TEXT,
                last_name TEXT,
                consent INTEGER,
                deadline_at TEXT NOT NULL,
                submitted_at TEXT,
                PRIMARY KEY(draw_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                state TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(user_id),
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK(status IN ('queued', 'sent', 'failed')),
                created_at TEXT NOT NULL,
                sent_at TEXT,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_promo_available
                ON promo_codes(partner_id, assigned_user_id);
            CREATE INDEX IF NOT EXISTS idx_notifications_status
                ON notifications(status, id);
            CREATE INDEX IF NOT EXISTS idx_participations_partner
                ON participations(partner_id);
            """
        )
        conn.commit()
        if self._memory is None:
            conn.close()

    def upsert_user(self, user: dict, now: datetime) -> bool:
        user_id = int(user["user_id"])
        stamp = now.isoformat()
        with self.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone()
            conn.execute(
                """
                INSERT INTO users(user_id, username, first_name, last_name, created_at, last_seen_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    last_seen_at = excluded.last_seen_at
                """,
                (user_id, user.get("username"), user.get("first_name"), user.get("last_name"), stamp, stamp),
            )
        return exists is None

    def add_partner(self, data: dict) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO partners(
                    slug, name, campaign_day, channel_id, channel_url, promo_place,
                    promo_expires_on, daily_prize, prize_instructions, winner_count, claim_hours
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data["slug"], data["name"], data["campaign_day"], int(data["channel_id"]),
                    data["channel_url"], data["promo_place"], data["promo_expires_on"],
                    data["daily_prize"], data["prize_instructions"], int(data["winner_count"]),
                    int(data.get("claim_hours", 24)),
                ),
            )
            return int(cursor.lastrowid)

    def add_promo_codes(self, partner_slug: str, codes: list[str]) -> int:
        with self.transaction() as conn:
            partner = conn.execute("SELECT id FROM partners WHERE slug = ?", (partner_slug,)).fetchone()
            if not partner:
                raise ValueError(f"unknown partner: {partner_slug}")
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO promo_codes(partner_id, code) VALUES(?, ?)",
                [(partner["id"], code.strip()) for code in codes if code.strip()],
            )
            return conn.total_changes - before

    def get_partner_for_day(self, day: date) -> dict | None:
        conn = self.connect()
        row = conn.execute("SELECT * FROM partners WHERE campaign_day = ?", (day.isoformat(),)).fetchone()
        if self._memory is None:
            conn.close()
        return dict(row) if row else None

    def request_promo(self, user_id: int, partner_id: int, now: datetime) -> PromoResult:
        with self.transaction() as conn:
            partner = conn.execute("SELECT * FROM partners WHERE id = ?", (partner_id,)).fetchone()
            if not partner:
                raise ValueError("partner not found")
            conn.execute(
                "INSERT INTO promo_requests(user_id, partner_id, requested_at) VALUES(?, ?, ?)",
                (user_id, partner_id, now.isoformat()),
            )
            old = conn.execute(
                "SELECT code FROM promo_codes WHERE partner_id = ? AND assigned_user_id = ?",
                (partner_id, user_id),
            ).fetchone()
            if old:
                return PromoResult("existing", old["code"], dict(partner))
            promo = conn.execute(
                "SELECT id, code FROM promo_codes WHERE partner_id = ? AND assigned_user_id IS NULL ORDER BY id LIMIT 1",
                (partner_id,),
            ).fetchone()
            if not promo:
                return PromoResult("exhausted", None, dict(partner))
            conn.execute(
                "UPDATE promo_codes SET assigned_user_id = ?, assigned_at = ? WHERE id = ? AND assigned_user_id IS NULL",
                (user_id, now.isoformat(), promo["id"]),
            )
            self._add_activity(conn, user_id, partner_id, now, "promo")
            return PromoResult("assigned", promo["code"], dict(partner))

    def _add_activity(self, conn: sqlite3.Connection, user_id: int, partner_id: int, now: datetime, source: str) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO activity_days(user_id, activity_day, partner_id, first_source, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (user_id, now.date().isoformat(), partner_id, source, now.isoformat()),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO partner_interactions(user_id, partner_id, first_interaction_at)
            VALUES(?, ?, ?)
            """,
            (user_id, partner_id, now.isoformat()),
        )

    def is_participant(self, user_id: int, partner_id: int) -> bool:
        conn = self.connect()
        row = conn.execute(
            "SELECT 1 FROM participations WHERE user_id = ? AND partner_id = ?", (user_id, partner_id)
        ).fetchone()
        if self._memory is None:
            conn.close()
        return row is not None

    def register_participation(self, user_id: int, partner_id: int, now: datetime) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO participations(user_id, partner_id, registered_at) VALUES(?, ?, ?)",
                (user_id, partner_id, now.isoformat()),
            )
            created = cursor.rowcount == 1
            if created:
                self._add_activity(conn, user_id, partner_id, now, "participation")
            return created

    def get_user_progress(self, user_id: int, max_chances: int) -> dict:
        conn = self.connect()
        day_rows = conn.execute(
            "SELECT activity_day FROM activity_days WHERE user_id = ? ORDER BY activity_day", (user_id,)
        ).fetchall()
        partner_rows = conn.execute(
            """
            SELECT p.name FROM partner_interactions i JOIN partners p ON p.id = i.partner_id
            WHERE i.user_id = ? ORDER BY p.campaign_day
            """,
            (user_id,),
        ).fetchall()
        if self._memory is None:
            conn.close()
        chances = min(len(day_rows), max_chances)
        return {
            "chances": chances,
            "days": [row["activity_day"] for row in day_rows[:max_chances]],
            "partners": [row["name"] for row in partner_rows],
            "remaining": max(0, max_chances - chances),
        }

    def create_daily_draw(self, day: date, now: datetime, rng: random.Random) -> int:
        with self.transaction() as conn:
            partner = conn.execute("SELECT * FROM partners WHERE campaign_day = ?", (day.isoformat(),)).fetchone()
            if not partner:
                raise ValueError("partner for this day is not configured")
            if conn.execute(
                "SELECT 1 FROM draws WHERE kind = 'daily' AND partner_id = ? AND draw_day = ?",
                (partner["id"], day.isoformat()),
            ).fetchone():
                raise ValueError("draw already exists")
            users = [
                row["user_id"] for row in conn.execute(
                    "SELECT user_id FROM participations WHERE partner_id = ? ORDER BY user_id", (partner["id"],)
                ).fetchall()
            ]
            winners = rng.sample(users, min(partner["winner_count"], len(users)))
            left = [user_id for user_id in users if user_id not in winners]
            reserves = rng.sample(left, min(partner["winner_count"], len(left)))
            cursor = conn.execute(
                """
                INSERT INTO draws(kind, partner_id, draw_day, created_at, winner_count, reserve_count, participant_count)
                VALUES('daily', ?, ?, ?, ?, ?, ?)
                """,
                (partner["id"], day.isoformat(), now.isoformat(), len(winners), len(reserves), len(users)),
            )
            draw_id = int(cursor.lastrowid)
            winner_pos = {user_id: pos for pos, user_id in enumerate(winners, 1)}
            reserve_pos = {user_id: pos for pos, user_id in enumerate(reserves, 1)}
            for user_id in users:
                role = "winner" if user_id in winner_pos else "reserve" if user_id in reserve_pos else "participant"
                conn.execute(
                    "INSERT INTO draw_entries(draw_id, user_id, role, position) VALUES(?, ?, ?, ?)",
                    (draw_id, user_id, role, winner_pos.get(user_id) or reserve_pos.get(user_id)),
                )
            return draw_id

    def create_final_draw(self, day: date, now: datetime, winner_count: int, reserve_count: int, max_chances: int, rng: random.Random) -> int:
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM draws WHERE kind = 'final' AND draw_day = ?", (day.isoformat(),)).fetchone():
                raise ValueError("final draw already exists")
            rows = conn.execute(
                "SELECT user_id, COUNT(*) AS chances FROM activity_days GROUP BY user_id HAVING chances > 0 ORDER BY user_id"
            ).fetchall()
            pool = [(row["user_id"], min(row["chances"], max_chances)) for row in rows]
            selected = self._weighted_unique(pool, winner_count + reserve_count, rng)
            winners = selected[: min(winner_count, len(selected))]
            reserves = selected[len(winners):]
            cursor = conn.execute(
                """
                INSERT INTO draws(kind, draw_day, created_at, winner_count, reserve_count, participant_count)
                VALUES('final', ?, ?, ?, ?, ?)
                """,
                (day.isoformat(), now.isoformat(), len(winners), len(reserves), len(pool)),
            )
            draw_id = int(cursor.lastrowid)
            winner_pos = {user_id: pos for pos, user_id in enumerate(winners, 1)}
            reserve_pos = {user_id: pos for pos, user_id in enumerate(reserves, 1)}
            for user_id, chances in pool:
                role = "winner" if user_id in winner_pos else "reserve" if user_id in reserve_pos else "participant"
                conn.execute(
                    "INSERT INTO draw_entries(draw_id, user_id, chances, role, position) VALUES(?, ?, ?, ?, ?)",
                    (draw_id, user_id, chances, role, winner_pos.get(user_id) or reserve_pos.get(user_id)),
                )
            return draw_id

    @staticmethod
    def _weighted_unique(pool: list[tuple[int, int]], count: int, rng: random.Random) -> list[int]:
        left = list(pool)
        selected = []
        while left and len(selected) < count:
            point = rng.uniform(0, sum(weight for _, weight in left))
            current = 0.0
            for index, (user_id, weight) in enumerate(left):
                current += weight
                if point <= current:
                    selected.append(user_id)
                    left.pop(index)
                    break
        return selected

    def approve_draw(self, draw_id: int, now: datetime, grand_prize: str) -> int:
        with self.transaction() as conn:
            draw = conn.execute(
                """
                SELECT d.*, p.name AS partner_name, p.daily_prize, p.prize_instructions, p.claim_hours
                FROM draws d LEFT JOIN partners p ON p.id = d.partner_id WHERE d.id = ?
                """,
                (draw_id,),
            ).fetchone()
            if not draw:
                raise ValueError("draw not found")
            if draw["status"] == "approved":
                return 0
            conn.execute("UPDATE draws SET status = 'approved', approved_at = ? WHERE id = ?", (now.isoformat(), draw_id))
            winners = conn.execute(
                "SELECT user_id FROM draw_entries WHERE draw_id = ? AND role = 'winner' ORDER BY position", (draw_id,)
            ).fetchall()
            deadline = now + timedelta(hours=draw["claim_hours"] or 24)
            for row in winners:
                if draw["kind"] == "daily":
                    text = (
                        f"Ты в числе хедлайнеров дня!\n\nТы выиграл {draw['daily_prize']} от {draw['partner_name']}.\n\n"
                        f"{draw['prize_instructions']}"
                    )
                else:
                    text = f"Твой сентябрь выходит на главную сцену!\n\nТы выиграл {grand_prize}."
                conn.execute(
                    "INSERT INTO notifications(user_id, kind, text, payload, created_at) VALUES(?, 'winner', ?, ?, ?)",
                    (row["user_id"], text, json.dumps({"draw_id": draw_id}), now.isoformat()),
                )
                conn.execute(
                    "INSERT INTO claims(draw_id, user_id, deadline_at) VALUES(?, ?, ?)",
                    (draw_id, row["user_id"], deadline.isoformat()),
                )
            return len(winners)

    def set_user_state(self, user_id: int, state: str, payload: dict, now: datetime) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO user_states(user_id, state, payload, updated_at) VALUES(?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (user_id, state, json.dumps(payload), now.isoformat()),
            )

    def get_user_state(self, user_id: int) -> dict | None:
        conn = self.connect()
        row = conn.execute("SELECT * FROM user_states WHERE user_id = ?", (user_id,)).fetchone()
        if self._memory is None:
            conn.close()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def save_claim_name(self, draw_id: int, user_id: int, first_name: str, last_name: str, now: datetime) -> None:
        with self.transaction() as conn:
            claim = conn.execute(
                "SELECT status, deadline_at FROM claims WHERE draw_id = ? AND user_id = ?", (draw_id, user_id)
            ).fetchone()
            if not claim or claim["status"] != "waiting":
                raise ValueError("claim is not available")
            if datetime.fromisoformat(claim["deadline_at"]) < now:
                raise ValueError("claim deadline has passed")
            conn.execute(
                "UPDATE claims SET first_name = ?, last_name = ? WHERE draw_id = ? AND user_id = ?",
                (first_name, last_name, draw_id, user_id),
            )

    def submit_consent(self, draw_id: int, user_id: int, consent: bool, now: datetime) -> None:
        with self.transaction() as conn:
            claim = conn.execute(
                "SELECT first_name, last_name, status FROM claims WHERE draw_id = ? AND user_id = ?", (draw_id, user_id)
            ).fetchone()
            if not claim or claim["status"] != "waiting" or not claim["first_name"] or not claim["last_name"]:
                raise ValueError("contact data is incomplete")
            conn.execute(
                "UPDATE claims SET consent = ?, status = ?, submitted_at = ? WHERE draw_id = ? AND user_id = ?",
                (int(consent), "submitted" if consent else "declined", now.isoformat(), draw_id, user_id),
            )
            conn.execute("DELETE FROM user_states WHERE user_id = ?", (user_id,))

    def get_queued_notifications(self, limit: int = 100) -> list[dict]:
        conn = self.connect()
        rows = conn.execute("SELECT * FROM notifications WHERE status = 'queued' ORDER BY id LIMIT ?", (limit,)).fetchall()
        if self._memory is None:
            conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def mark_notification(self, notification_id: int, sent: bool, now: datetime, error: str = "") -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE notifications SET status = ?, sent_at = ?, error = ? WHERE id = ?",
                ("sent" if sent else "failed", now.isoformat() if sent else None, error[:500], notification_id),
            )

    def queue_push(self, text: str, now: datetime, user_id: int | None = None) -> int:
        with self.transaction() as conn:
            if user_id is None:
                users = conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()
            else:
                users = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchall()
            conn.executemany(
                "INSERT INTO notifications(user_id, kind, text, created_at) VALUES(?, 'push', ?, ?)",
                [(row["user_id"], text, now.isoformat()) for row in users],
            )
            return len(users)

    def expire_claims(self, now: datetime, grand_prize: str) -> int:
        promoted = 0
        with self.transaction() as conn:
            expired = conn.execute(
                "SELECT draw_id, user_id FROM claims WHERE status IN ('waiting', 'declined') AND deadline_at < ? ORDER BY draw_id, user_id",
                (now.isoformat(),),
            ).fetchall()
            for old in expired:
                conn.execute("UPDATE claims SET status = 'expired' WHERE draw_id = ? AND user_id = ?", (old["draw_id"], old["user_id"]))
                conn.execute("UPDATE draw_entries SET role = 'expired' WHERE draw_id = ? AND user_id = ?", (old["draw_id"], old["user_id"]))
                reserve = conn.execute(
                    "SELECT user_id FROM draw_entries WHERE draw_id = ? AND role = 'reserve' ORDER BY position LIMIT 1",
                    (old["draw_id"],),
                ).fetchone()
                if not reserve:
                    continue
                draw = conn.execute(
                    """
                    SELECT d.kind, p.name AS partner_name, p.daily_prize, p.prize_instructions, p.claim_hours
                    FROM draws d LEFT JOIN partners p ON p.id = d.partner_id WHERE d.id = ?
                    """,
                    (old["draw_id"],),
                ).fetchone()
                conn.execute("UPDATE draw_entries SET role = 'promoted' WHERE draw_id = ? AND user_id = ?", (old["draw_id"], reserve["user_id"]))
                deadline = now + timedelta(hours=draw["claim_hours"] or 24)
                conn.execute("INSERT INTO claims(draw_id, user_id, deadline_at) VALUES(?, ?, ?)", (old["draw_id"], reserve["user_id"], deadline.isoformat()))
                if draw["kind"] == "daily":
                    text = f"Ты стал победителем резерва!\n\nТвой приз: {draw['daily_prize']} от {draw['partner_name']}.\n\n{draw['prize_instructions']}"
                else:
                    text = f"Ты стал победителем резерва финального розыгрыша!\n\nТвой приз: {grand_prize}."
                conn.execute(
                    "INSERT INTO notifications(user_id, kind, text, payload, created_at) VALUES(?, 'reserve_promoted', ?, ?, ?)",
                    (reserve["user_id"], text, json.dumps({"draw_id": old["draw_id"]}), now.isoformat()),
                )
                promoted += 1
        return promoted

    def get_stats(self, day: date) -> dict:
        conn = self.connect()
        totals = {
            "total_users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "new_users": conn.execute("SELECT COUNT(*) FROM users WHERE substr(created_at, 1, 10) = ?", (day.isoformat(),)).fetchone()[0],
            "promo_requests": conn.execute("SELECT COUNT(*) FROM promo_requests").fetchone()[0],
            "promos_issued": conn.execute("SELECT COUNT(*) FROM promo_codes WHERE assigned_user_id IS NOT NULL").fetchone()[0],
        }
        totals["partners"] = [
            dict(row) for row in conn.execute(
                """
                SELECT p.slug, p.name, p.campaign_day,
                    SUM(CASE WHEN pc.id IS NOT NULL AND pc.assigned_user_id IS NULL THEN 1 ELSE 0 END) AS promos_left,
                    COUNT(DISTINCT part.user_id) AS participants
                FROM partners p LEFT JOIN promo_codes pc ON pc.partner_id = p.id
                LEFT JOIN participations part ON part.partner_id = p.id
                GROUP BY p.id ORDER BY p.campaign_day
                """
            ).fetchall()
        ]
        if self._memory is None:
            conn.close()
        return totals

    def get_draw(self, draw_id: int) -> dict | None:
        conn = self.connect()
        draw = conn.execute(
            "SELECT d.*, p.name AS partner_name FROM draws d LEFT JOIN partners p ON p.id = d.partner_id WHERE d.id = ?",
            (draw_id,),
        ).fetchone()
        if not draw:
            if self._memory is None:
                conn.close()
            return None
        entries = [
            dict(row) for row in conn.execute(
                """
                SELECT e.*, u.username, u.first_name, u.last_name FROM draw_entries e
                JOIN users u ON u.user_id = e.user_id WHERE e.draw_id = ?
                ORDER BY CASE e.role WHEN 'winner' THEN 1 WHEN 'promoted' THEN 2 WHEN 'reserve' THEN 3 ELSE 4 END,
                    e.position, e.user_id
                """,
                (draw_id,),
            ).fetchall()
        ]
        if self._memory is None:
            conn.close()
        result = dict(draw)
        result["entries"] = entries
        return result

    def list_draws(self) -> list[dict]:
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT d.*, p.name AS partner_name FROM draws d
            LEFT JOIN partners p ON p.id = d.partner_id ORDER BY d.id DESC
            """
        ).fetchall()
        if self._memory is None:
            conn.close()
        return [dict(row) for row in rows]
