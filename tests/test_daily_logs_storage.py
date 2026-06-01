import tempfile
import sys
import types
import unittest
from pathlib import Path

telegram_stub = types.ModuleType("telegram")
telegram_stub.ReplyKeyboardMarkup = object
telegram_stub.ReplyKeyboardRemove = object
telegram_stub.Update = object

telegram_ext_stub = types.ModuleType("telegram.ext")
telegram_ext_stub.Application = object
telegram_ext_stub.ApplicationBuilder = object
telegram_ext_stub.CommandHandler = object
telegram_ext_stub.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
telegram_ext_stub.MessageHandler = object
telegram_ext_stub.filters = object

sys.modules.setdefault("telegram", telegram_stub)
sys.modules.setdefault("telegram.ext", telegram_ext_stub)

from bot import Database


class DailyLogsStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = Database(str(root / "activity.db"), str(root / "users.db"))

    def tearDown(self) -> None:
        self.db.conn.close()
        self.tmp.cleanup()

    def add_user(self, chat_id: int, name: str) -> None:
        self.db.ensure_user(chat_id)
        self.db.set_authenticated(chat_id, True)
        self.db.set_display_name(chat_id, name)

    def insert_old_log(self, chat_id: int, log_date: str, pushups: int, pullups: int) -> None:
        self.db.conn.execute(
            """
            INSERT INTO logs(chat_id, log_date, pushups, pullups, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, log_date, pushups, pullups, "2026-05-01T08:00:00"),
        )
        self.db.conn.commit()

    def daily_totals(self, chat_id: int) -> list[tuple[str, int]]:
        return [
            (row["log_date"], int(row["pushups"]) + int(row["pullups"]))
            for row in self.db.get_daily_breakdown(chat_id)
        ]

    def test_existing_old_logs_only_are_included_in_stats(self) -> None:
        self.add_user(101, "Old")
        self.insert_old_log(101, "2026-05-01", 10, 2)
        self.insert_old_log(101, "2026-05-02", 20, 3)

        self.assertEqual(self.db.get_totals(101), (30, 5, 35))
        self.assertEqual(self.db.get_total_in_date_range(101, "2026-05-02", "2026-05-02"), 23)
        self.assertEqual(
            self.daily_totals(101),
            [("2026-05-01", 12), ("2026-05-02", 23)],
        )
        self.assertTrue(self.db.has_log_for_day(101, "2026-05-01"))

    def test_new_add_log_writes_to_daily_logs_only(self) -> None:
        self.add_user(202, "New")
        self.db.add_log(202, "2026-05-03", 12, 0)
        self.db.add_log(202, "2026-05-04", 0, 4)

        old_count = self.db.conn.execute("SELECT COUNT(1) AS c FROM logs").fetchone()["c"]
        daily_count = self.db.conn.execute("SELECT COUNT(1) AS c FROM daily_logs").fetchone()["c"]

        self.assertEqual(old_count, 0)
        self.assertEqual(daily_count, 2)
        self.assertEqual(self.db.get_totals(202), (12, 4, 16))
        self.assertEqual(self.db.get_total_in_date_range(202, "2026-05-03", "2026-05-04"), 16)

    def test_mixed_old_and_new_data_are_combined(self) -> None:
        self.add_user(303, "Mixed")
        self.insert_old_log(303, "2026-05-01", 10, 1)
        self.db.add_log(303, "2026-05-02", 20, 2)

        self.assertEqual(self.db.get_totals(303), (30, 3, 33))
        self.assertEqual(
            self.daily_totals(303),
            [("2026-05-01", 11), ("2026-05-02", 22)],
        )

    def test_duplicate_new_logs_on_same_date_are_upserted(self) -> None:
        self.add_user(404, "Duplicate")
        self.db.add_log(404, "2026-05-05", 10, 0)
        self.db.add_log(404, "2026-05-05", 5, 2)

        rows = self.db.conn.execute(
            "SELECT chat_id, log_date, pushups, pullups FROM daily_logs WHERE chat_id = ?",
            (404,),
        ).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["pushups"], rows[0]["pullups"]), (15, 2))
        self.assertEqual(self.db.get_totals(404), (15, 2, 17))

    def test_negative_corrections_are_preserved(self) -> None:
        self.add_user(505, "Negative")
        self.insert_old_log(505, "2026-05-05", 30, 5)
        self.db.add_log(505, "2026-05-05", -10, -2)

        self.assertEqual(self.db.get_totals(505), (20, 3, 23))
        self.assertEqual(self.db.get_total_in_date_range(505, "2026-05-05", "2026-05-05"), 23)

    def test_zero_entries_create_a_daily_record_and_count_as_logged(self) -> None:
        self.add_user(606, "Zero")
        self.db.add_log(606, "2026-05-06", 0, 0)

        self.assertTrue(self.db.has_log_for_day(606, "2026-05-06"))
        self.assertEqual(self.db.get_totals(606), (0, 0, 0))
        self.assertEqual(
            self.daily_totals(606),
            [("2026-05-06", 0)],
        )

    def test_leaderboard_totals_include_both_tables(self) -> None:
        self.add_user(701, "A")
        self.add_user(702, "B")
        self.insert_old_log(701, "2026-05-01", 10, 1)
        self.db.add_log(701, "2026-05-02", 15, 2)
        self.db.add_log(702, "2026-05-02", 30, 0)

        push_rows = self.db.get_leaderboard_by_metric("pushups", limit=2)
        pull_rows = self.db.get_leaderboard_by_metric("pullups", limit=2)
        overall_rows = self.db.get_overall_leaderboard(limit=2)

        self.assertEqual([(r["chat_id"], r["total"]) for r in push_rows], [(702, 30), (701, 25)])
        self.assertEqual([(r["chat_id"], r["total"]) for r in pull_rows], [(701, 3), (702, 0)])
        self.assertEqual([(r["chat_id"], r["total"]) for r in overall_rows], [(702, 30), (701, 28)])

    def test_has_log_for_day_checks_old_and_new_tables(self) -> None:
        self.add_user(808, "HasLog")
        self.insert_old_log(808, "2026-05-01", 1, 0)
        self.db.add_log(808, "2026-05-02", 0, 1)

        self.assertTrue(self.db.has_log_for_day(808, "2026-05-01"))
        self.assertTrue(self.db.has_log_for_day(808, "2026-05-02"))
        self.assertFalse(self.db.has_log_for_day(808, "2026-05-03"))


if __name__ == "__main__":
    unittest.main()
