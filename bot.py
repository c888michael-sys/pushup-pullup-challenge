import asyncio
import json
import logging
import os
import sqlite3
import struct
import sys
import threading
import urllib.error
import urllib.request
import zlib
from io import BytesIO
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
DB_PATH = os.getenv("DB_PATH", "pushup_pullup_bot.db")
USER_DB_PATH = os.getenv("USER_DB_PATH", "user_data.db")
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
ACCESS_PASSWORD = os.getenv("BOT_ACCESS_PASSWORD", "michael101010")
LOG_LEVEL_ENV = "LOG_LEVEL"

BUTTON_ADD = "Add"
BUTTON_MINUS = "Minus"
BUTTON_VIEW_PROGRESS = "View Progress"
BUTTON_START = "Start"
BUTTON_END = "End"
BUTTON_START_TRAINING = "Begin Training"
BUTTON_STOP_TRAINING = "End Training"
LEGACY_BUTTON_START_TRAINING = "Start Training"
LEGACY_BUTTON_STOP_TRAINING = "Stop Training"
START_TRAINING_BUTTONS = {BUTTON_START_TRAINING, LEGACY_BUTTON_START_TRAINING}
STOP_TRAINING_BUTTONS = {BUTTON_STOP_TRAINING, LEGACY_BUTTON_STOP_TRAINING}
BUTTON_LEADERBOARD = "Leaderboard"
BUTTON_ADMIN_PANEL = "Admin Panel"
BUTTON_KICK_USER = "Kick User"
BUTTON_GLOBAL_MESSAGE = "Global Message"
BUTTON_MUTE_NOTIFICATIONS = "Mute Notifications"
BUTTON_UNMUTE_NOTIFICATIONS = "Unmute Notifications"

BUTTON_PUSHUP = "Pushup"
BUTTON_PULLUP = "Pullup"
BUTTON_BACK = "Back"

BUTTON_START_DATE = "Start Date"
BUTTON_END_DATE = "End Date"
BUTTON_GOAL = "Goal"
BUTTON_DONE = "Done"

STATE_NONE = ""
STATE_CHOOSE_EXERCISE = "choose_exercise"
STATE_ENTER_AMOUNT = "enter_amount"
STATE_CONFIG_MENU = "config_menu"
STATE_SET_START_DATE = "set_start_date"
STATE_SET_END_DATE = "set_end_date"
STATE_SET_GOAL = "set_goal"
STATE_WAIT_PASSWORD = "wait_password"
STATE_WAIT_NAME = "wait_name"
STATE_ADMIN_MENU = "admin_menu"
STATE_ADMIN_KICK_USER = "admin_kick_user"
STATE_ADMIN_GLOBAL_MESSAGE = "admin_global_message"
STATE_SET_TRAINING_INTERVAL = "set_training_interval"


class Database:
    def __init__(self, activity_path: str, user_path: str) -> None:
        self.conn = sqlite3.connect(activity_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("ATTACH DATABASE ? AS usersdb", (user_path,))
        self.lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS usersdb.users (
                    chat_id INTEGER PRIMARY KEY,
                    started INTEGER NOT NULL DEFAULT 0,
                    start_date TEXT,
                    end_date TEXT,
                    goal INTEGER NOT NULL DEFAULT 0,
                    authenticated INTEGER NOT NULL DEFAULT 0,
                    display_name TEXT,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    is_kicked INTEGER NOT NULL DEFAULT 0,
                    notifications_muted INTEGER NOT NULL DEFAULT 0,
                    training_active INTEGER NOT NULL DEFAULT 0,
                    training_interval_minutes INTEGER NOT NULL DEFAULT 0,
                    training_last_sent_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    log_date TEXT NOT NULL,
                    pushups INTEGER NOT NULL DEFAULT 0,
                    pullups INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_logs_chat_date ON logs(chat_id, log_date);

                CREATE TABLE IF NOT EXISTS sessions (
                    chat_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT '',
                    op TEXT NOT NULL DEFAULT '',
                    exercise TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS reminders_sent (
                    chat_id INTEGER NOT NULL,
                    reminder_date TEXT NOT NULL,
                    PRIMARY KEY (chat_id, reminder_date)
                );
                """
            )
            self._migrate_schema()
            self._migrate_legacy_users_if_needed()
            self.conn.commit()

    def _migrate_schema(self) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA usersdb.table_info(users)").fetchall()
        }
        if "authenticated" not in columns:
            self.conn.execute(
                "ALTER TABLE usersdb.users ADD COLUMN authenticated INTEGER NOT NULL DEFAULT 0"
            )
        if "display_name" not in columns:
            self.conn.execute("ALTER TABLE usersdb.users ADD COLUMN display_name TEXT")
        if "is_admin" not in columns:
            self.conn.execute(
                "ALTER TABLE usersdb.users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"
            )
        if "is_kicked" not in columns:
            self.conn.execute(
                "ALTER TABLE usersdb.users ADD COLUMN is_kicked INTEGER NOT NULL DEFAULT 0"
            )
        if "notifications_muted" not in columns:
            self.conn.execute(
                "ALTER TABLE usersdb.users ADD COLUMN notifications_muted INTEGER NOT NULL DEFAULT 0"
            )
        if "training_active" not in columns:
            self.conn.execute(
                "ALTER TABLE usersdb.users ADD COLUMN training_active INTEGER NOT NULL DEFAULT 0"
            )
        if "training_interval_minutes" not in columns:
            self.conn.execute(
                "ALTER TABLE usersdb.users ADD COLUMN training_interval_minutes INTEGER NOT NULL DEFAULT 0"
            )
        if "training_last_sent_at" not in columns:
            self.conn.execute(
                "ALTER TABLE usersdb.users ADD COLUMN training_last_sent_at TEXT"
            )

    def _table_exists(self, schema: str, table_name: str) -> bool:
        row = self.conn.execute(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (table_name,),
        ).fetchone()
        return row is not None

    def _migrate_legacy_users_if_needed(self) -> None:
        if not self._table_exists("main", "users"):
            return

        user_count = int(
            self.conn.execute("SELECT COUNT(1) AS c FROM usersdb.users").fetchone()["c"]
        )
        if user_count > 0:
            return

        legacy_rows = self.conn.execute("SELECT * FROM main.users").fetchall()
        if not legacy_rows:
            return

        now = sydney_now().isoformat(timespec="seconds")
        for row in legacy_rows:
            keys = set(row.keys())
            self.conn.execute(
                """
                INSERT INTO usersdb.users(
                    chat_id, started, start_date, end_date, goal,
                    authenticated, display_name, is_admin, is_kicked,
                    notifications_muted, training_active, training_interval_minutes,
                    training_last_sent_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO NOTHING
                """,
                (
                    int(row["chat_id"]),
                    int(row["started"]) if "started" in keys and row["started"] is not None else 0,
                    row["start_date"] if "start_date" in keys else None,
                    row["end_date"] if "end_date" in keys else None,
                    int(row["goal"]) if "goal" in keys and row["goal"] is not None else 0,
                    int(row["authenticated"]) if "authenticated" in keys and row["authenticated"] is not None else 0,
                    row["display_name"] if "display_name" in keys else None,
                    int(row["is_admin"]) if "is_admin" in keys and row["is_admin"] is not None else 0,
                    int(row["is_kicked"]) if "is_kicked" in keys and row["is_kicked"] is not None else 0,
                    int(row["notifications_muted"]) if "notifications_muted" in keys and row["notifications_muted"] is not None else 0,
                    int(row["training_active"]) if "training_active" in keys and row["training_active"] is not None else 0,
                    int(row["training_interval_minutes"]) if "training_interval_minutes" in keys and row["training_interval_minutes"] is not None else 0,
                    row["training_last_sent_at"] if "training_last_sent_at" in keys else None,
                    row["created_at"] if "created_at" in keys and row["created_at"] else now,
                    row["updated_at"] if "updated_at" in keys and row["updated_at"] else now,
                ),
            )

    def ensure_user(self, chat_id: int) -> None:
        now = sydney_now().isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO usersdb.users(chat_id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (chat_id, now, now),
            )
            self.conn.execute(
                """
                INSERT INTO sessions(chat_id)
                VALUES (?)
                ON CONFLICT(chat_id) DO NOTHING
                """,
                (chat_id,),
            )
            self.conn.commit()

    def get_user(self, chat_id: int) -> sqlite3.Row:
        with self.lock:
            row = self.conn.execute("SELECT * FROM usersdb.users WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None:
            self.ensure_user(chat_id)
            with self.lock:
                row = self.conn.execute("SELECT * FROM usersdb.users WHERE chat_id = ?", (chat_id,)).fetchone()
        return row

    def find_user(self, chat_id: int) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute("SELECT * FROM usersdb.users WHERE chat_id = ?", (chat_id,)).fetchone()

    def set_started(self, chat_id: int, started: bool) -> None:
        now = sydney_now().isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                "UPDATE usersdb.users SET started = ?, updated_at = ? WHERE chat_id = ?",
                (1 if started else 0, now, chat_id),
            )
            self.conn.commit()

    def update_user_field(self, chat_id: int, field: str, value) -> None:
        if field not in {"start_date", "end_date", "goal"}:
            raise ValueError("Unsupported field update")
        now = sydney_now().isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                f"UPDATE usersdb.users SET {field} = ?, updated_at = ? WHERE chat_id = ?",
                (value, now, chat_id),
            )
            self.conn.commit()

    def set_authenticated(self, chat_id: int, authenticated: bool) -> None:
        now = sydney_now().isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                "UPDATE usersdb.users SET authenticated = ?, updated_at = ? WHERE chat_id = ?",
                (1 if authenticated else 0, now, chat_id),
            )
            self.conn.commit()

    def set_display_name(self, chat_id: int, display_name: str) -> None:
        now = sydney_now().isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                "UPDATE usersdb.users SET display_name = ?, updated_at = ? WHERE chat_id = ?",
                (display_name, now, chat_id),
            )
            self.conn.commit()

    def has_any_admin(self) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM usersdb.users WHERE is_admin = 1 AND is_kicked = 0 LIMIT 1"
            ).fetchone()
        return row is not None

    def get_admin_chat_ids(self) -> list[int]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT chat_id FROM usersdb.users WHERE is_admin = 1 AND is_kicked = 0"
            ).fetchall()
        return [int(r["chat_id"]) for r in rows]

    def set_admin(self, chat_id: int, is_admin: bool) -> None:
        now = sydney_now().isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                "UPDATE usersdb.users SET is_admin = ?, updated_at = ? WHERE chat_id = ?",
                (1 if is_admin else 0, now, chat_id),
            )
            self.conn.commit()

    def set_kicked(self, chat_id: int, kicked: bool) -> None:
        now = sydney_now().isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                """
                UPDATE usersdb.users
                SET is_kicked = ?, authenticated = CASE WHEN ? = 1 THEN 0 ELSE authenticated END,
                    started = CASE WHEN ? = 1 THEN 0 ELSE started END,
                    updated_at = ?
                WHERE chat_id = ?
                """,
                (1 if kicked else 0, 1 if kicked else 0, 1 if kicked else 0, now, chat_id),
            )
            self.conn.commit()

    def set_notifications_muted(self, chat_id: int, muted: bool) -> None:
        now = sydney_now().isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                "UPDATE usersdb.users SET notifications_muted = ?, updated_at = ? WHERE chat_id = ?",
                (1 if muted else 0, now, chat_id),
            )
            self.conn.commit()

    def start_training(self, chat_id: int, interval_minutes: int) -> None:
        now = sydney_now().isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                """
                UPDATE usersdb.users
                SET training_active = 1, training_interval_minutes = ?, training_last_sent_at = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (interval_minutes, now, now, chat_id),
            )
            self.conn.commit()

    def stop_training(self, chat_id: int) -> None:
        now = sydney_now().isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                """
                UPDATE usersdb.users
                SET training_active = 0, training_last_sent_at = NULL, updated_at = ?
                WHERE chat_id = ?
                """,
                (now, chat_id),
            )
            self.conn.commit()

    def update_training_last_sent(self, chat_id: int, sent_at_iso: str) -> None:
        now = sydney_now().isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                """
                UPDATE usersdb.users
                SET training_last_sent_at = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (sent_at_iso, now, chat_id),
            )
            self.conn.commit()

    def get_training_users(self) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                """
                SELECT chat_id, training_interval_minutes, training_last_sent_at
                FROM usersdb.users
                WHERE training_active = 1 AND authenticated = 1 AND is_kicked = 0
                """
            ).fetchall()

    def get_session(self, chat_id: int) -> sqlite3.Row:
        with self.lock:
            row = self.conn.execute("SELECT * FROM sessions WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None:
            with self.lock:
                self.conn.execute("INSERT INTO sessions(chat_id) VALUES (?)", (chat_id,))
                self.conn.commit()
                row = self.conn.execute("SELECT * FROM sessions WHERE chat_id = ?", (chat_id,)).fetchone()
        return row

    def set_session(self, chat_id: int, state: str, op: str = "", exercise: str = "") -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO sessions(chat_id, state, op, exercise)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    state = excluded.state,
                    op = excluded.op,
                    exercise = excluded.exercise
                """,
                (chat_id, state, op, exercise),
            )
            self.conn.commit()

    def add_log(self, chat_id: int, log_date: str, pushups: int, pullups: int) -> None:
        now = sydney_now().isoformat(timespec="seconds")
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO logs(chat_id, log_date, pushups, pullups, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, log_date, pushups, pullups, now),
            )
            self.conn.commit()

    def get_totals(self, chat_id: int) -> tuple[int, int, int]:
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(pushups), 0) AS p, COALESCE(SUM(pullups), 0) AS u FROM logs WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        pushups = int(row["p"])
        pullups = int(row["u"])
        return pushups, pullups, pushups + pullups

    def get_total_in_date_range(self, chat_id: int, start_date: str, end_date: str) -> int:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT COALESCE(SUM(pushups + pullups), 0) AS total
                FROM logs
                WHERE chat_id = ? AND log_date >= ? AND log_date <= ?
                """,
                (chat_id, start_date, end_date),
            ).fetchone()
        return int(row["total"])

    def get_daily_breakdown(
        self,
        chat_id: int,
        since_date: str | None = None,
        limit_days: int = 14,
    ) -> list[sqlite3.Row]:
        with self.lock:
            if since_date is None:
                rows = self.conn.execute(
                    """
                    SELECT
                        log_date,
                        COALESCE(SUM(pushups), 0) AS pushups,
                        COALESCE(SUM(pullups), 0) AS pullups
                    FROM logs
                    WHERE chat_id = ?
                    GROUP BY log_date
                    ORDER BY log_date DESC
                    LIMIT ?
                    """,
                    (chat_id, limit_days),
                ).fetchall()
                return list(reversed(rows))

            rows = self.conn.execute(
                """
                SELECT
                    log_date,
                    COALESCE(SUM(pushups), 0) AS pushups,
                    COALESCE(SUM(pullups), 0) AS pullups
                FROM logs
                WHERE chat_id = ? AND log_date >= ?
                GROUP BY log_date
                ORDER BY log_date ASC
                """,
                (chat_id, since_date),
            ).fetchall()
        return list(rows)

    def has_log_for_day(self, chat_id: int, log_date: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM logs WHERE chat_id = ? AND log_date = ? LIMIT 1",
                (chat_id, log_date),
            ).fetchone()
        return row is not None

    def get_started_users(self) -> list[int]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT chat_id FROM usersdb.users WHERE started = 1 AND authenticated = 1 AND is_kicked = 0 AND notifications_muted = 0"
            ).fetchall()
        return [int(r["chat_id"]) for r in rows]

    def get_active_user_ids(self) -> list[int]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT chat_id FROM usersdb.users WHERE authenticated = 1 AND is_kicked = 0"
            ).fetchall()
        return [int(r["chat_id"]) for r in rows]

    def get_leaderboard_by_metric(self, metric: str, limit: int = 20) -> list[sqlite3.Row]:
        if metric == "pushups":
            sum_expr = "COALESCE(SUM(l.pushups), 0)"
        elif metric == "pullups":
            sum_expr = "COALESCE(SUM(l.pullups), 0)"
        else:
            raise ValueError("metric must be pushups or pullups")

        with self.lock:
            rows = self.conn.execute(
                f"""
                SELECT
                    u.chat_id,
                    u.display_name,
                    {sum_expr} AS total
                FROM usersdb.users u
                LEFT JOIN main.logs l ON l.chat_id = u.chat_id
                WHERE u.authenticated = 1 AND u.is_kicked = 0
                GROUP BY u.chat_id, u.display_name
                ORDER BY total DESC, u.chat_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return rows

    def get_overall_leaderboard(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT
                    u.chat_id,
                    u.display_name,
                    COALESCE(SUM(l.pushups + l.pullups), 0) AS total
                FROM usersdb.users u
                LEFT JOIN main.logs l ON l.chat_id = u.chat_id
                WHERE u.authenticated = 1 AND u.is_kicked = 0
                GROUP BY u.chat_id, u.display_name
                ORDER BY total DESC, u.chat_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return rows

    def reminder_already_sent(self, chat_id: int, reminder_date: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM reminders_sent WHERE chat_id = ? AND reminder_date = ? LIMIT 1",
                (chat_id, reminder_date),
            ).fetchone()
        return row is not None

    def mark_reminder_sent(self, chat_id: int, reminder_date: str) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO reminders_sent(chat_id, reminder_date)
                VALUES (?, ?)
                ON CONFLICT(chat_id, reminder_date) DO NOTHING
                """,
                (chat_id, reminder_date),
            )
            self.conn.commit()


def sydney_now() -> datetime:
    return datetime.now(SYDNEY_TZ)


def sydney_today() -> date:
    return sydney_now().date()


def parse_iso_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def parse_iso_datetime(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def normalize_display_name(raw: str) -> str:
    name = " ".join(raw.strip().split())
    if len(name) > 24:
        name = name[:24]
    return name


def main_menu(started: bool, is_admin: bool, notifications_muted: bool, training_active: bool) -> ReplyKeyboardMarkup:
    start_or_end = BUTTON_END if started else BUTTON_START
    mute_or_unmute = BUTTON_UNMUTE_NOTIFICATIONS if notifications_muted else BUTTON_MUTE_NOTIFICATIONS
    training_button = BUTTON_STOP_TRAINING if training_active else BUTTON_START_TRAINING
    rows = [
        [BUTTON_ADD, BUTTON_MINUS],
        [BUTTON_VIEW_PROGRESS, start_or_end],
        [training_button],
        [BUTTON_LEADERBOARD, mute_or_unmute],
    ]
    if is_admin:
        rows.append([BUTTON_ADMIN_PANEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def exercise_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BUTTON_PUSHUP, BUTTON_PULLUP], [BUTTON_BACK]],
        resize_keyboard=True,
        is_persistent=True,
    )


def config_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BUTTON_START_DATE, BUTTON_END_DATE], [BUTTON_GOAL], [BUTTON_DONE]],
        resize_keyboard=True,
        is_persistent=True,
    )


def training_interval_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BUTTON_BACK]],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BUTTON_KICK_USER], [BUTTON_GLOBAL_MESSAGE], [BUTTON_BACK]],
        resize_keyboard=True,
        is_persistent=True,
    )


def display_name_or_fallback(row: sqlite3.Row) -> str:
    name = (row["display_name"] or "").strip() if "display_name" in row.keys() else ""
    if name:
        return name
    return f"User{int(row['chat_id'])}"


def compact_entry(rank: int, row: sqlite3.Row) -> str:
    name = display_name_or_fallback(row)
    safe_name = "".join(ch for ch in name if ch.isalnum() or ch in {" ", "_", "-"})
    safe_name = " ".join(safe_name.split())
    if not safe_name:
        safe_name = f"User{int(row['chat_id'])}"
    if len(safe_name) > 12:
        safe_name = safe_name[:12]
    return f"{rank}. {safe_name} {int(row['total'])}"


def format_side_by_side_leaderboard(
    push_rows: list[sqlite3.Row], pull_rows: list[sqlite3.Row], limit_label: str
) -> str:
    if not push_rows and not pull_rows:
        return f"Leaderboard {limit_label}\nNo data yet."

    lines = [f"Leaderboard {limit_label}", "", "Pushups"]
    if push_rows:
        for idx, row in enumerate(push_rows, start=1):
            lines.append(compact_entry(idx, row))
    else:
        lines.append("No pushup data.")

    lines.extend(["", "Pullups"])
    if pull_rows:
        for idx, row in enumerate(pull_rows, start=1):
            lines.append(compact_entry(idx, row))
    else:
        lines.append("No pullup data.")

    return "\n".join(lines)


def trend_text(recent: int, previous: int) -> str:
    if previous == 0:
        if recent == 0:
            return "No change (100%)"
        return "Increasing (new activity; previous 7 days was 0)"

    ratio = (recent / previous) * 100
    if recent > previous:
        direction = "Increasing"
    elif recent < previous:
        direction = "Decreasing"
    else:
        direction = "No change"
    return f"{direction} ({ratio:.1f}%)"


def build_daily_breakdown_series(
    daily_rows: list[sqlite3.Row],
    days: int = 14,
    start_date: date | None = None,
) -> tuple[list[date], list[int], list[int]]:
    pushups_by_date: dict[date, int] = {}
    pullups_by_date: dict[date, int] = {}
    for row in daily_rows:
        parsed = parse_iso_date(str(row["log_date"]))
        if parsed is None:
            continue
        pushups_by_date[parsed] = int(row["pushups"] or 0)
        pullups_by_date[parsed] = int(row["pullups"] or 0)

    end = sydney_today()
    if start_date is not None:
        start = min(start_date, end)
    else:
        start = end - timedelta(days=days - 1)
    span = max(1, (end - start).days + 1)
    dates = [start + timedelta(days=offset) for offset in range(span)]
    pushup_totals = [pushups_by_date.get(day, 0) for day in dates]
    pullup_totals = [pullups_by_date.get(day, 0) for day in dates]
    return dates, pushup_totals, pullup_totals


def calculate_slope(values: list[int]) -> float:
    if len(values) < 2:
        return 0.0

    x_values = [float(i) for i in range(len(values))]
    y_values = [float(value) for value in values]
    x_mean = sum(x_values) / len(x_values)
    y_mean = sum(y_values) / len(y_values)
    denom = sum((x - x_mean) ** 2 for x in x_values)
    if not denom:
        return 0.0
    return sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, y_values)) / denom


FONT_5X7 = {
    "0": ["111", "101", "101", "101", "101", "101", "111"],
    "1": ["010", "110", "010", "010", "010", "010", "111"],
    "2": ["111", "001", "001", "111", "100", "100", "111"],
    "3": ["111", "001", "001", "111", "001", "001", "111"],
    "4": ["101", "101", "101", "111", "001", "001", "001"],
    "5": ["111", "100", "100", "111", "001", "001", "111"],
    "6": ["111", "100", "100", "111", "101", "101", "111"],
    "7": ["111", "001", "001", "010", "010", "010", "010"],
    "8": ["111", "101", "101", "111", "101", "101", "111"],
    "9": ["111", "101", "101", "111", "001", "001", "111"],
    "-": ["000", "000", "000", "111", "000", "000", "000"],
    " ": ["000", "000", "000", "000", "000", "000", "000"],
}


def render_daily_trend_chart(
    daily_rows: list[sqlite3.Row], start_date: date | None = None
) -> tuple[BytesIO | None, float]:
    if not daily_rows and start_date is None:
        return None, 0.0

    dates, pushup_totals, pullup_totals = build_daily_breakdown_series(
        daily_rows, start_date=start_date
    )
    combined_totals = [p + u for p, u in zip(pushup_totals, pullup_totals)]
    slope = calculate_slope(combined_totals)
    x_values = list(range(len(combined_totals)))
    average = sum(combined_totals) / len(combined_totals)
    center = (len(combined_totals) - 1) / 2
    fit_values = [slope * (x - center) + average for x in x_values]

    width = 960
    height = 560
    left = 78
    right = 34
    top = 42
    bottom = 76
    plot_width = width - left - right
    plot_height = height - top - bottom

    background = (248, 250, 252)
    panel = (255, 255, 255)
    grid = (226, 232, 240)
    axis = (148, 163, 184)
    text = (51, 65, 85)
    pushup_fill = (147, 197, 253)
    pushup_edge = (37, 99, 235)
    pullup_fill = (167, 243, 208)
    pullup_edge = (5, 150, 105)
    line_blue = (29, 78, 216)
    trend_orange = (249, 115, 22)

    pixels = [[background for _ in range(width)] for _ in range(height)]

    def set_pixel(x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < width and 0 <= y < height:
            pixels[y][x] = color

    def draw_rect(x1: int, y1: int, x2: int, y2: int, color: tuple[int, int, int]) -> None:
        x1, x2 = sorted((max(0, x1), min(width - 1, x2)))
        y1, y2 = sorted((max(0, y1), min(height - 1, y2)))
        for y in range(y1, y2 + 1):
            row = pixels[y]
            for x in range(x1, x2 + 1):
                row[x] = color

    def draw_line(x1: int, y1: int, x2: int, y2: int, color: tuple[int, int, int], thickness: int = 1) -> None:
        dx = abs(x2 - x1)
        dy = -abs(y2 - y1)
        sx = 1 if x1 < x2 else -1
        sy = 1 if y1 < y2 else -1
        err = dx + dy
        x, y = x1, y1
        radius = max(0, thickness // 2)
        while True:
            draw_rect(x - radius, y - radius, x + radius, y + radius, color)
            if x == x2 and y == y2:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x += sx
            if e2 <= dx:
                err += dx
                y += sy

    def draw_circle(cx: int, cy: int, radius: int, color: tuple[int, int, int]) -> None:
        for y in range(cy - radius, cy + radius + 1):
            for x in range(cx - radius, cx + radius + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
                    set_pixel(x, y, color)

    def draw_text(raw: str, x: int, y: int, color: tuple[int, int, int], scale: int = 2) -> None:
        cursor = x
        for char in raw:
            glyph = FONT_5X7.get(char, FONT_5X7[" "])
            for gy, row in enumerate(glyph):
                for gx, bit in enumerate(row):
                    if bit == "1":
                        draw_rect(
                            cursor + gx * scale,
                            y + gy * scale,
                            cursor + (gx + 1) * scale - 1,
                            y + (gy + 1) * scale - 1,
                            color,
                        )
            cursor += (len(glyph[0]) + 1) * scale

    def png_bytes() -> bytes:
        raw_rows = []
        for row in pixels:
            raw_rows.append(b"\x00" + b"".join(bytes(color) for color in row))
        raw = b"".join(raw_rows)

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b"")
        )

    draw_rect(18, 18, width - 18, height - 18, panel)

    max_total = max(
        [
            *pushup_totals,
            *pullup_totals,
            *[max(0, value) for value in fit_values],
            5,
        ]
    )
    y_max = max_total * 1.18

    def to_x(index: int) -> int:
        if len(combined_totals) == 1:
            return left + plot_width // 2
        return left + round(index * (plot_width / (len(combined_totals) - 1)))

    def to_y(value: float) -> int:
        ratio = max(0.0, min(1.0, value / y_max))
        return top + plot_height - round(ratio * plot_height)

    for step in range(6):
        value = round((y_max / 5) * step)
        y = to_y(value)
        draw_line(left, y, width - right, y, grid, 1)
        draw_text(str(value), 24, y - 8, text, 2)

    draw_line(left, top, left, top + plot_height, axis, 2)
    draw_line(left, top + plot_height, width - right, top + plot_height, axis, 2)

    n = len(combined_totals)
    bar_slot = plot_width / n
    bar_width = max(2, min(16, round(bar_slot * 0.30)))
    bar_gap = max(1, round(bar_width * 0.18))
    baseline = top + plot_height

    show_value_labels = bar_width >= 12
    show_circles = n <= 30
    label_stride = max(1, (n + 9) // 10)

    def draw_bar(x_left: int, y_top: int, fill: tuple[int, int, int], edge: tuple[int, int, int]) -> None:
        x_right = x_left + bar_width
        draw_rect(x_left, y_top, x_right, baseline, fill)
        edge_thickness = 2 if bar_width >= 8 else 1
        draw_line(x_left, y_top, x_right, y_top, edge, edge_thickness)
        if bar_width >= 4:
            draw_line(x_left, y_top, x_left, baseline, edge, 1)
            draw_line(x_right, y_top, x_right, baseline, edge, 1)

    for idx in range(n):
        x_center = to_x(idx)
        pushup_x = x_center - bar_gap // 2 - bar_width
        pullup_x = x_center + bar_gap // 2

        push_y = to_y(pushup_totals[idx])
        pull_y = to_y(pullup_totals[idx])
        draw_bar(pushup_x, push_y, pushup_fill, pushup_edge)
        draw_bar(pullup_x, pull_y, pullup_fill, pullup_edge)

        if show_value_labels:
            if pushup_totals[idx] > 0:
                label = str(pushup_totals[idx])
                draw_text(label, pushup_x + bar_width // 2 - len(label) * 4, max(top + 2, push_y - 22), pushup_edge, 2)
            if pullup_totals[idx] > 0:
                label = str(pullup_totals[idx])
                draw_text(label, pullup_x + bar_width // 2 - len(label) * 4, max(top + 2, pull_y - 22), pullup_edge, 2)
        if idx % label_stride == 0 or idx == n - 1:
            draw_text(str(dates[idx].day), x_center - 6, baseline + 18, text, 2)

    trend_points = [(to_x(idx), to_y(value)) for idx, value in enumerate(fit_values)]
    for (x1, y1), (x2, y2) in zip(trend_points, trend_points[1:]):
        draw_line(x1, y1, x2, y2, trend_orange, 3)

    pushup_points = [(to_x(idx), to_y(total)) for idx, total in enumerate(pushup_totals)]
    line_thickness = 3 if n <= 30 else 2
    for (x1, y1), (x2, y2) in zip(pushup_points, pushup_points[1:]):
        draw_line(x1, y1, x2, y2, line_blue, line_thickness)
    if show_circles:
        for x, y in pushup_points:
            draw_circle(x, y, 6, line_blue)
            draw_circle(x, y, 3, panel)

    image = BytesIO(png_bytes())
    image.name = "progress-chart.png"
    image.seek(0)
    return image, slope


def compute_average(total: int, user_row: sqlite3.Row) -> str:
    start_raw = user_row["start_date"]
    if not start_raw:
        return "N/A (set a challenge start date first)"

    start = parse_iso_date(start_raw)
    if start is None:
        return "N/A (invalid start date in data)"

    if int(user_row["started"]) == 1:
        finish = sydney_today()
    else:
        finish_raw = user_row["end_date"]
        parsed_finish = parse_iso_date(finish_raw) if finish_raw else None
        finish = parsed_finish if parsed_finish else sydney_today()

    if finish < start:
        return "N/A (end date is before start date)"

    days = (finish - start).days + 1
    avg = total / days
    return f"{avg:.2f} per day over {days} day(s)"


def main_menu_for_user(user: sqlite3.Row) -> ReplyKeyboardMarkup:
    return main_menu(
        bool(user["started"]),
        bool(user["is_admin"]),
        bool(user["notifications_muted"]),
        bool(user["training_active"]),
    )


async def send_main_menu(update: Update, db: Database, text: str) -> None:
    chat_id = update.effective_chat.id
    user = db.get_user(chat_id)
    top3_push = db.get_leaderboard_by_metric("pushups", limit=3)
    top3_pull = db.get_leaderboard_by_metric("pullups", limit=3)
    board = format_side_by_side_leaderboard(top3_push, top3_pull, "Top 3")
    reminder_status = "Muted" if bool(user["notifications_muted"]) else "On"
    if bool(user["training_active"]):
        interval = int(user["training_interval_minutes"] or 0)
        training_status = f"On ({interval} min)" if interval > 0 else "On"
    else:
        training_status = "Off"
    menu_text = f"{board}\n\nMain Menu\nReminders: {reminder_status}\nTraining: {training_status}\n{text}"
    await update.message.reply_text(menu_text, reply_markup=main_menu_for_user(user))


async def refresh_and_send_main_menu(update: Update, db: Database, text: str) -> None:
    await update.message.reply_text("Updating menu...", reply_markup=ReplyKeyboardRemove())
    await send_main_menu(update, db, text)


async def send_action_then_main_menu(update: Update, db: Database, action_text: str) -> None:
    chat_id = update.effective_chat.id
    user = db.get_user(chat_id)
    await update.message.reply_text(action_text, reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text("Main menu:", reply_markup=main_menu_for_user(user))


async def send_submenu_with_refresh(update: Update, prompt_text: str, markup: ReplyKeyboardMarkup) -> None:
    await update.message.reply_text("Updating menu...", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text(prompt_text, reply_markup=markup)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    db.ensure_user(chat_id)
    user = db.get_user(chat_id)

    if bool(user["is_kicked"]):
        await update.message.reply_text("Access has been removed by admin.")
        return

    if not bool(user["authenticated"]):
        db.set_session(chat_id, STATE_WAIT_PASSWORD)
        await update.message.reply_text("Welcome. Please enter the access password.")
        return

    if not (user["display_name"] or "").strip():
        db.set_session(chat_id, STATE_WAIT_NAME)
        await update.message.reply_text("Password accepted. Please enter your display name.")
        return

    db.set_session(chat_id, STATE_NONE)

    await refresh_and_send_main_menu(
        update,
        db,
        "Challenge tracker ready. Use the menu to Add/Minus reps, view progress, or Start/End your challenge.",
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    db.ensure_user(chat_id)

    user = db.get_user(chat_id)
    if bool(user["is_kicked"]):
        await update.message.reply_text("Access has been removed by admin.")
        return

    if not bool(user["authenticated"]):
        db.set_session(chat_id, STATE_WAIT_PASSWORD)
        await update.message.reply_text("Please enter the access password first.")
        return

    if not (user["display_name"] or "").strip():
        db.set_session(chat_id, STATE_WAIT_NAME)
        await update.message.reply_text("Please enter your display name first.")
        return

    db.set_session(chat_id, STATE_NONE)
    await refresh_and_send_main_menu(update, db, "Main menu:")


async def send_admin_message(
    app: Application, db: Database, target_arg: str, message_text: str
) -> str:
    if target_arg == "all":
        targets = db.get_active_user_ids()
        sent = 0
        failed = 0
        for target_chat_id in targets:
            try:
                await app.bot.send_message(chat_id=target_chat_id, text=message_text)
                sent += 1
            except Exception:
                failed += 1
                logging.exception("Failed admin broadcast to chat_id=%s", target_chat_id)
        return f"Broadcast done. Sent: {sent}, Failed: {failed}."

    try:
        target_chat_id = int(target_arg)
    except ValueError as exc:
        raise ValueError("Target must be a numeric chat ID or 'all'.") from exc

    try:
        await app.bot.send_message(chat_id=target_chat_id, text=message_text)
    except Exception as exc:
        logging.exception("Failed admin direct message to chat_id=%s", target_chat_id)
        raise RuntimeError(f"Failed to send message to {target_chat_id}.") from exc

    return f"Message sent to {target_chat_id}."


async def admin_message_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_chat is None:
        return

    db: Database = context.application.bot_data["db"]
    sender_chat_id = update.effective_chat.id
    db.ensure_user(sender_chat_id)
    sender = db.get_user(sender_chat_id)

    if bool(sender["is_kicked"]) or not bool(sender["authenticated"]) or not bool(sender["is_admin"]):
        await update.message.reply_text("Admin access only.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /adminmsg <chat_id|all> <message>\nExample: /adminmsg all Reminder: workout today."
        )
        return

    target_arg = context.args[0].strip().lower()
    message_text = " ".join(context.args[1:]).strip()
    if not message_text:
        await update.message.reply_text("Message text cannot be empty.")
        return

    try:
        result = await send_admin_message(context.application, db, target_arg, message_text)
        await update.message.reply_text(result)
    except (ValueError, RuntimeError) as exc:
        await update.message.reply_text(str(exc))


async def show_progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    user = db.get_user(chat_id)

    pushups, pullups, total = db.get_totals(chat_id)

    avg_text = compute_average(total, user)

    today = sydney_today()
    recent_start = (today - timedelta(days=6)).isoformat()
    previous_start = (today - timedelta(days=13)).isoformat()
    previous_end = (today - timedelta(days=7)).isoformat()
    recent_end = today.isoformat()

    recent_total = db.get_total_in_date_range(chat_id, recent_start, recent_end)
    previous_total = db.get_total_in_date_range(chat_id, previous_start, previous_end)

    trend = trend_text(recent_total, previous_total)
    start_date_raw = user["start_date"]
    chart_start = parse_iso_date(start_date_raw) if start_date_raw else None
    if chart_start is not None and chart_start > today:
        chart_start = today
    if chart_start is not None:
        daily_rows = db.get_daily_breakdown(chat_id, since_date=chart_start.isoformat())
    else:
        daily_rows = db.get_daily_breakdown(chat_id, limit_days=14)
    chart_image, slope = render_daily_trend_chart(daily_rows, start_date=chart_start)

    goal = int(user["goal"] or 0)
    if goal == 0:
        goal_line = "Goal: not set"
    else:
        goal_pct = (total / goal) * 100 if goal > 0 else 0.0
        remaining = goal - total
        goal_line = f"Goal: {goal} | Progress: {goal_pct:.1f}% | Remaining: {remaining}"

    slope_label = "upward" if slope > 0 else ("downward" if slope < 0 else "flat")

    text = (
        "Progress Summary\n"
        f"Total progress: {total}\n"
        f"Pushups / Pullups: {pushups} / {pullups}\n"
        f"Average/day: {avg_text}\n"
        f"Trend (7d vs prev 7d): {trend}\n"
        f"Best-fit line slope: {slope:.2f} ({slope_label})\n"
        f"Recent 7d / Previous 7d: {recent_total} / {previous_total}\n"
        f"{goal_line}"
    )

    menu_markup = main_menu_for_user(user)
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())

    if chart_image is not None:
        if chart_start is not None:
            chart_caption = (
                f"Progress graph (from {chart_start.isoformat()} to {today.isoformat()}, Sydney days). "
                "Blue = pushups, green = pullups, orange = best-fit on daily total."
            )
        else:
            chart_caption = (
                "Progress graph (last 14 days, Sydney days). "
                "Blue = pushups, green = pullups, orange = best-fit on daily total."
            )
        await update.message.reply_photo(
            photo=chart_image,
            caption=chart_caption,
            reply_markup=menu_markup,
        )
    else:
        await update.message.reply_text("Progress graph: no workout logs yet.", reply_markup=menu_markup)


async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE, limit: int = 20) -> None:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    user = db.get_user(chat_id)
    push_rows = db.get_leaderboard_by_metric("pushups", limit=limit)
    pull_rows = db.get_leaderboard_by_metric("pullups", limit=limit)
    text = format_side_by_side_leaderboard(push_rows, pull_rows, f"Top {limit}")
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text("Main menu:", reply_markup=main_menu_for_user(user))


async def process_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE, session: sqlite3.Row) -> None:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    raw = update.message.text.strip()

    try:
        amount = int(raw)
    except ValueError:
        await update.message.reply_text("Please send a whole number (0 or greater).")
        return

    if amount < 0:
        await update.message.reply_text("Please send 0 or a positive whole number.")
        return

    op = session["op"]
    exercise = session["exercise"]
    signed_amount = amount if op == "add" else -amount

    pushups = signed_amount if exercise == "pushup" else 0
    pullups = signed_amount if exercise == "pullup" else 0

    today_str = sydney_today().isoformat()
    db.add_log(chat_id, today_str, pushups, pullups)
    db.set_session(chat_id, STATE_NONE)

    action_word = "Added" if op == "add" else "Subtracted"
    exercise_word = "pushup" if exercise == "pushup" else "pullup"

    await send_action_then_main_menu(
        update,
        db,
        f"Logged: {action_word} {amount} {exercise_word}(s) for {today_str} (Sydney time).",
    )


async def process_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str) -> None:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    raw = update.message.text.strip()
    parsed = parse_iso_date(raw)
    if parsed is None:
        await update.message.reply_text("Please use YYYY-MM-DD format, for example 2026-05-13.")
        return

    user = db.get_user(chat_id)
    current_start = parse_iso_date(user["start_date"]) if user["start_date"] else None
    current_end = parse_iso_date(user["end_date"]) if user["end_date"] else None

    if state == STATE_SET_START_DATE:
        if current_end and parsed > current_end:
            await update.message.reply_text("Start date cannot be after the current end date.")
            return
        db.update_user_field(chat_id, "start_date", parsed.isoformat())
        db.set_session(chat_id, STATE_CONFIG_MENU)
        await update.message.reply_text(
            f"Start date saved: {parsed.isoformat()}",
            reply_markup=config_menu(),
        )
        return

    if state == STATE_SET_END_DATE:
        if current_start and parsed < current_start:
            await update.message.reply_text("End date cannot be before the current start date.")
            return
        db.update_user_field(chat_id, "end_date", parsed.isoformat())
        db.set_session(chat_id, STATE_CONFIG_MENU)
        await update.message.reply_text(
            f"End date saved: {parsed.isoformat()}",
            reply_markup=config_menu(),
        )


async def process_goal_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    raw = update.message.text.strip()
    try:
        goal = int(raw)
    except ValueError:
        await update.message.reply_text("Goal must be a whole number (0 or greater).")
        return

    if goal < 0:
        await update.message.reply_text("Goal must be 0 or greater.")
        return

    db.update_user_field(chat_id, "goal", goal)
    db.set_session(chat_id, STATE_CONFIG_MENU)
    refreshed = db.get_user(chat_id)
    current_goal = int(refreshed["goal"] or 0)
    await update.message.reply_text(f"Goal saved: {current_goal}", reply_markup=config_menu())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_chat is None:
        return

    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    db.ensure_user(chat_id)

    text = update.message.text.strip()
    user = db.get_user(chat_id)
    session = db.get_session(chat_id)
    state = session["state"]

    if bool(user["is_kicked"]):
        await update.message.reply_text("Access has been removed by admin.")
        return

    if not bool(user["authenticated"]):
        if text == ACCESS_PASSWORD:
            db.set_authenticated(chat_id, True)
            if not db.has_any_admin():
                db.set_admin(chat_id, True)
            db.set_session(chat_id, STATE_WAIT_NAME)
            await update.message.reply_text("Access granted. Please enter your display name.")
            return

        db.set_session(chat_id, STATE_WAIT_PASSWORD)
        await update.message.reply_text("Incorrect password. Please try again.")
        return

    if state == STATE_WAIT_NAME or not (user["display_name"] or "").strip():
        display_name = normalize_display_name(text)
        if len(display_name) < 2:
            await update.message.reply_text("Please enter a name with at least 2 characters.")
            return
        db.set_display_name(chat_id, display_name)
        db.set_session(chat_id, STATE_NONE)
        updated_user = db.get_user(chat_id)
        extra = " You are the admin." if bool(updated_user["is_admin"]) else ""
        await refresh_and_send_main_menu(update, db, f"Name saved as {display_name}.{extra}")
        return

    if state in {STATE_ADMIN_MENU, STATE_ADMIN_KICK_USER, STATE_ADMIN_GLOBAL_MESSAGE} and not bool(user["is_admin"]):
        db.set_session(chat_id, STATE_NONE)
        await refresh_and_send_main_menu(update, db, "Admin access only.")
        return

    if state == STATE_ADMIN_KICK_USER:
        if text == BUTTON_BACK:
            db.set_session(chat_id, STATE_ADMIN_MENU)
            await update.message.reply_text("Admin action cancelled.", reply_markup=admin_menu())
            return
        try:
            target_chat_id = int(text)
        except ValueError:
            await update.message.reply_text("Send a numeric chat ID to kick, or tap Back.", reply_markup=admin_menu())
            return

        if target_chat_id == chat_id:
            await update.message.reply_text("You cannot kick yourself.", reply_markup=admin_menu())
            return

        target = db.find_user(target_chat_id)
        if target is None:
            await update.message.reply_text("User not found.", reply_markup=admin_menu())
            return
        if bool(target["is_admin"]):
            await update.message.reply_text("You cannot kick another admin.", reply_markup=admin_menu())
            return

        db.set_kicked(target_chat_id, True)
        db.set_session(chat_id, STATE_ADMIN_MENU)
        kicked_name = (target["display_name"] or f"User{target_chat_id}").strip()
        await update.message.reply_text(f"Kicked: {kicked_name} ({target_chat_id})", reply_markup=admin_menu())
        return

    if state == STATE_ADMIN_GLOBAL_MESSAGE:
        if text == BUTTON_BACK:
            db.set_session(chat_id, STATE_ADMIN_MENU)
            await update.message.reply_text("Global message cancelled.", reply_markup=admin_menu())
            return

        if not text:
            await update.message.reply_text("Global message cannot be empty. Send a message, or tap Back.", reply_markup=admin_menu())
            return

        result = await send_admin_message(context.application, db, "all", text)
        db.set_session(chat_id, STATE_ADMIN_MENU)
        await update.message.reply_text(result, reply_markup=admin_menu())
        return

    if state == STATE_ADMIN_MENU:
        if text == BUTTON_BACK:
            db.set_session(chat_id, STATE_NONE)
            await refresh_and_send_main_menu(update, db, "Back to main menu.")
            return
        if text == BUTTON_KICK_USER:
            top20 = db.get_overall_leaderboard(limit=20)
            id_lines = []
            for idx, row in enumerate(top20, start=1):
                id_lines.append(f"{idx}. {display_name_or_fallback(row)} -> {int(row['chat_id'])}")
            id_block = "\n".join(id_lines) if id_lines else "No users."
            db.set_session(chat_id, STATE_ADMIN_KICK_USER)
            await update.message.reply_text(
                f"Overall ranking IDs:\n{id_block}\n\nSend a chat ID to kick, or Back.",
                reply_markup=admin_menu(),
            )
            return
        if text == BUTTON_GLOBAL_MESSAGE:
            db.set_session(chat_id, STATE_ADMIN_GLOBAL_MESSAGE)
            await update.message.reply_text(
                "Send the message to broadcast to all active users, or tap Back.",
                reply_markup=admin_menu(),
            )
            return
        await update.message.reply_text("Choose Kick User, Global Message, or Back.", reply_markup=admin_menu())
        return

    if state == STATE_ENTER_AMOUNT:
        await process_amount_input(update, context, session)
        return

    if state in {STATE_SET_START_DATE, STATE_SET_END_DATE}:
        await process_date_input(update, context, state)
        return

    if state == STATE_SET_GOAL:
        await process_goal_input(update, context)
        return

    if state == STATE_SET_TRAINING_INTERVAL:
        if text == BUTTON_BACK:
            db.set_session(chat_id, STATE_NONE)
            await refresh_and_send_main_menu(update, db, "Training setup cancelled.")
            return

        try:
            interval_minutes = int(text)
        except ValueError:
            await update.message.reply_text(
                "Send interval in whole minutes (for example: 30).",
                reply_markup=training_interval_menu(),
            )
            return

        if interval_minutes <= 0:
            await update.message.reply_text(
                "Interval must be at least 1 minute.",
                reply_markup=training_interval_menu(),
            )
            return

        db.start_training(chat_id, interval_minutes)
        db.set_session(chat_id, STATE_NONE)
        await refresh_and_send_main_menu(update, db, f"Training started. You will get reminders every {interval_minutes} minute(s).")
        return

    if state == STATE_CHOOSE_EXERCISE:
        if text == BUTTON_BACK:
            db.set_session(chat_id, STATE_NONE)
            await refresh_and_send_main_menu(update, db, "Cancelled.")
            return

        if text not in {BUTTON_PUSHUP, BUTTON_PULLUP}:
            await update.message.reply_text("Choose Pushup or Pullup.", reply_markup=exercise_menu())
            return

        exercise_value = "pushup" if text == BUTTON_PUSHUP else "pullup"
        db.set_session(chat_id, STATE_ENTER_AMOUNT, op=session["op"], exercise=exercise_value)
        op_text = "add" if session["op"] == "add" else "minus"
        await update.message.reply_text(
            f"Send the amount to {op_text} for {text.lower()} (0 or greater).",
            reply_markup=exercise_menu(),
        )
        return

    if state == STATE_CONFIG_MENU:
        if text == BUTTON_DONE:
            db.set_session(chat_id, STATE_NONE)
            await refresh_and_send_main_menu(update, db, "Challenge settings saved.")
            return

        if text == BUTTON_START_DATE:
            db.set_session(chat_id, STATE_SET_START_DATE)
            await update.message.reply_text("Send start date as YYYY-MM-DD.")
            return

        if text == BUTTON_END_DATE:
            db.set_session(chat_id, STATE_SET_END_DATE)
            await update.message.reply_text("Send end date as YYYY-MM-DD.")
            return

        if text == BUTTON_GOAL:
            db.set_session(chat_id, STATE_SET_GOAL)
            await update.message.reply_text("Send goal as a whole number (0 or greater).")
            return

        await update.message.reply_text("Choose one of: Start Date, End Date, Goal, Done.", reply_markup=config_menu())
        return

    if text == BUTTON_ADD:
        db.set_session(chat_id, STATE_CHOOSE_EXERCISE, op="add")
        await send_submenu_with_refresh(update, "Choose exercise to add.", exercise_menu())
        return

    if text == BUTTON_MINUS:
        db.set_session(chat_id, STATE_CHOOSE_EXERCISE, op="minus")
        await send_submenu_with_refresh(update, "Choose exercise to subtract.", exercise_menu())
        return

    if text == BUTTON_VIEW_PROGRESS:
        await show_progress(update, context)
        return

    if text == BUTTON_LEADERBOARD:
        await show_leaderboard(update, context, limit=20)
        return

    if text in START_TRAINING_BUTTONS:
        db.set_session(chat_id, STATE_SET_TRAINING_INTERVAL)
        await send_submenu_with_refresh(
            update,
            "Send training interval in minutes (for example: 30), or tap Back to cancel.",
            training_interval_menu(),
        )
        return

    if text in STOP_TRAINING_BUTTONS:
        db.stop_training(chat_id)
        db.set_session(chat_id, STATE_NONE)
        await refresh_and_send_main_menu(update, db, "Training stopped.")
        return

    if text == BUTTON_MUTE_NOTIFICATIONS:
        db.set_notifications_muted(chat_id, True)
        await refresh_and_send_main_menu(update, db, "Notifications muted. 8 PM reminders are now off.")
        return

    if text == BUTTON_UNMUTE_NOTIFICATIONS:
        db.set_notifications_muted(chat_id, False)
        await refresh_and_send_main_menu(update, db, "Notifications unmuted. 8 PM reminders are now on.")
        return

    if text == BUTTON_ADMIN_PANEL:
        if not bool(user["is_admin"]):
            await refresh_and_send_main_menu(update, db, "Admin access only.")
            return
        db.set_session(chat_id, STATE_ADMIN_MENU)
        await send_submenu_with_refresh(update, "Admin panel:", admin_menu())
        return

    if text == BUTTON_START:
        if bool(user["started"]):
            db.set_session(chat_id, STATE_CONFIG_MENU)
            await send_submenu_with_refresh(
                update,
                "Challenge is already started. Update settings below.",
                config_menu(),
            )
            return

        db.set_started(chat_id, True)
        if not user["start_date"]:
            db.update_user_field(chat_id, "start_date", sydney_today().isoformat())
        db.set_session(chat_id, STATE_CONFIG_MENU)
        await send_submenu_with_refresh(
            update,
            "Challenge started. Set or review Start Date, End Date, and Goal.",
            config_menu(),
        )
        return

    if text == BUTTON_END:
        if not bool(user["started"]):
            await refresh_and_send_main_menu(update, db, "Challenge is not currently started.")
            return

        db.set_started(chat_id, False)
        db.update_user_field(chat_id, "end_date", sydney_today().isoformat())
        db.set_session(chat_id, STATE_NONE)
        await refresh_and_send_main_menu(update, db, f"Challenge ended on {sydney_today().isoformat()} (Sydney time).")
        return

    await update.message.reply_text(
        "Use the menu buttons below. You can also use /menu.",
        reply_markup=main_menu_for_user(user),
    )


async def reminder_loop(app: Application) -> None:
    db: Database = app.bot_data["db"]

    while True:
        now = sydney_now()
        today = now.date().isoformat()
        should_run = now.hour == 20 and now.minute < 5

        if should_run:
            for chat_id in db.get_started_users():
                if db.reminder_already_sent(chat_id, today):
                    continue
                if db.has_log_for_day(chat_id, today):
                    continue

                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text="8:00 PM reminder (Sydney): no workout logged yet today. Time to get a set in.",
                    )
                except Exception:
                    logging.exception("Failed to send reminder to chat_id=%s", chat_id)
                finally:
                    db.mark_reminder_sent(chat_id, today)

        for row in db.get_training_users():
            chat_id = int(row["chat_id"])
            interval_minutes = int(row["training_interval_minutes"] or 0)
            if interval_minutes <= 0:
                continue

            last_sent_raw = row["training_last_sent_at"]
            last_sent = parse_iso_datetime(last_sent_raw) if last_sent_raw else None
            if last_sent is None:
                db.update_training_last_sent(chat_id, now.isoformat(timespec="seconds"))
                continue

            if last_sent.tzinfo is None:
                last_sent = last_sent.replace(tzinfo=SYDNEY_TZ)

            if now >= last_sent + timedelta(minutes=interval_minutes):
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=f"Training reminder: time for your next set. Interval: {interval_minutes} minute(s).",
                    )
                except Exception:
                    logging.exception("Failed training reminder to chat_id=%s", chat_id)
                finally:
                    db.update_training_last_sent(chat_id, now.isoformat(timespec="seconds"))

        await asyncio.sleep(60)


async def on_startup(app: Application) -> None:
    task = asyncio.create_task(reminder_loop(app))
    app.bot_data["reminder_task"] = task


async def on_shutdown(app: Application) -> None:
    task = app.bot_data.get("reminder_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def build_app() -> Application:
    token = os.getenv(TOKEN_ENV)
    if not token:
        raise RuntimeError(f"Missing {TOKEN_ENV} environment variable")

    db = Database(DB_PATH, USER_DB_PATH)

    app = ApplicationBuilder().token(token).post_init(on_startup).post_shutdown(on_shutdown).build()
    app.bot_data["db"] = db

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("adminmsg", admin_message_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


DUPLICATE_ALERT_STATE = Path(__file__).with_name(".duplicate_alert_count")
DUPLICATE_ALERT_MAX = 3


def _telegram_call(token: str, method: str, payload: dict | None = None, timeout: float = 15) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error_code": exc.code, "description": str(exc)}


def check_for_duplicate_poller(token: str, admin_ids: list[int]) -> None:
    me = _telegram_call(token, "getMe", timeout=10)
    if not me.get("ok"):
        logging.error("getMe failed during startup probe: %s", me)
        return

    username = me["result"]["username"]
    logging.warning("Bot @%s starting as PID %d (pushup bot)", username, os.getpid())

    probe = _telegram_call(token, "getUpdates", {"timeout": 0, "limit": 1}, timeout=15)
    if probe.get("error_code") != 409:
        try:
            DUPLICATE_ALERT_STATE.unlink()
        except FileNotFoundError:
            pass
        return

    try:
        count = int(DUPLICATE_ALERT_STATE.read_text().strip())
    except (FileNotFoundError, ValueError):
        count = 0

    if count < DUPLICATE_ALERT_MAX and admin_ids:
        host = os.uname().nodename if hasattr(os, "uname") else "unknown"
        text = (
            f"pushup bot: another instance is polling.\n"
            f"Host: {host}\n"
            f"PID refusing to start: {os.getpid()}\n"
            f"Alert {count + 1}/{DUPLICATE_ALERT_MAX} — will go silent after this.\n"
            f"Fix: ssh in, find the duplicate (ps -ef | grep 'pushup-pullup'), "
            f"check /proc/<pid>/cgroup, kill the one not under pushup-bot.service."
        )
        for admin_id in admin_ids:
            send_result = _telegram_call(
                token, "sendMessage", {"chat_id": admin_id, "text": text}, timeout=10
            )
            if not send_result.get("ok"):
                logging.warning("Failed to send duplicate-alert to %s: %s", admin_id, send_result)
        try:
            DUPLICATE_ALERT_STATE.write_text(str(count + 1))
        except Exception as exc:
            logging.warning("Failed to write alert state: %s", exc)

    logging.error(
        "409 Conflict on startup probe — another instance is polling. Refusing to start (alert %d/%d).",
        min(count + 1, DUPLICATE_ALERT_MAX),
        DUPLICATE_ALERT_MAX,
    )
    sys.exit(1)


def main() -> None:
    raw_level = os.getenv(LOG_LEVEL_ENV, "WARNING").upper()
    level = getattr(logging, raw_level, logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    token = os.getenv(TOKEN_ENV)
    if not token:
        raise RuntimeError(f"Missing {TOKEN_ENV} environment variable")

    # Best-effort admin lookup before starting; safe to fail (DB may not exist yet on first run).
    admin_ids: list[int] = []
    try:
        probe_db = Database(DB_PATH, USER_DB_PATH)
        admin_ids = probe_db.get_admin_chat_ids()
        probe_db.conn.close()
    except Exception as exc:
        logging.warning("Could not pre-load admin IDs for duplicate-alert: %s", exc)

    check_for_duplicate_poller(token, admin_ids)

    application = build_app()
    application.run_polling()


if __name__ == "__main__":
    main()
