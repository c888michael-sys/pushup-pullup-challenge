# Pushup/Pullup Challenge Telegram Bot

A lightweight Telegram bot for tracking pushup/pullup challenges with:
- Persistent reply menu
- Add/Minus logging (including 0)
- Challenge start/end + start date/end date/goal settings
- Progress stats (total, average/day, 7-day trend)
- Progress graph in `View Progress` — PNG chart of daily totals (last 14 days) with best-fit line
- Main-menu leaderboard (Top 3) with separate Pushup/Pullup sections
- Compact Top 20 view with separate Pushup/Pullup sections
- 8:00 PM Sydney reminder when nothing is logged that day
- Training interval reminders with `Begin Training` / `End Training`
- First-time password gate + one-time display name capture
- Admin support (first authenticated user becomes admin, can kick users)

## Tech Choice
This bot uses Python + SQLite (`sqlite3`) for a small footprint and simple deployment on a Google Cloud VM.

## Files
- `bot.py` - main bot app
- `requirements.txt` - dependencies
- `pushup_pullup_bot.db` - workout/activity DB (`logs`, `daily_logs`, sessions, reminder history)
- `user_data.db` - user profile DB (auth, names, admin, kick/mute state)

## Setup
1. Create a virtual env (optional but recommended):
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies:
```powershell
pip install -r requirements.txt
```

3. Set environment variables:
```powershell
$env:TELEGRAM_BOT_TOKEN="YOUR_BOT_TOKEN"
$env:BOT_ACCESS_PASSWORD="michael101010"
$env:DB_PATH="pushup_pullup_bot.db"
$env:USER_DB_PATH="user_data.db"
```

4. Run:
```powershell
python bot.py
```

## Behavior Details
- On first `/start`, user must enter the password once.
- After successful password entry, user is asked once for a display name.
- After name is set, user is not asked for password/name again.
- The first authenticated user becomes admin automatically.
- Main menu message always shows `Top 3` with separate Pushup then Pullup sections, then menu buttons.
- Main menu buttons:
  - `Add`
  - `Minus`
  - `View Progress`
  - `Start` or `End` (dynamic)
  - `Begin Training` or `End Training` (dynamic)
  - `Leaderboard` (shows compact top 20 with separate Pushup/Pullup sections)
  - `Admin Panel` (admin only)
- After `Start`, bot shows config menu:
  - `Start Date`
  - `End Date`
  - `Goal`
  - `Done`
- `Add`/`Minus` flow:
  - Choose `Pushup` or `Pullup`
  - Enter whole number (`0` allowed)
  - New entries are stored in `daily_logs` as one row per user/date.
  - Existing event-level `logs` rows are left untouched and remain included in all progress and leaderboard totals.
- Reminder logic:
  - Uses `Australia/Sydney`
  - Around 8:00 PM, if no logs exist for that day, sends reminder.
- Training reminder logic:
  - Tap `Begin Training`, then send interval minutes (for example, `30`)
  - Bot sends a reminder every X minutes for the next set
  - Tap `End Training` to stop interval reminders
- Admin panel:
  - `Kick User`
  - Admin can kick non-admin users by chat ID
  - Kicked users lose access
- Admin command:
  - `/adminmsg <chat_id|all> <message>`
  - Example: `/adminmsg all Workout reminder: log your reps today.`

## Deploy on Google Cloud VM
Use your existing bot host process manager. Example with systemd:

1. Create a service file (example):
```ini
[Unit]
Description=Pushup Pullup Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/bots
Environment="TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN"
Environment="BOT_ACCESS_PASSWORD=michael101010"
Environment="DB_PATH=/path/to/bots/pushup_pullup_bot.db"
Environment="USER_DB_PATH=/path/to/bots/user_data.db"
ExecStart=/path/to/bots/.venv/bin/python /path/to/bots/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

2. Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable pushup-bot
sudo systemctl start pushup-bot
sudo systemctl status pushup-bot
```

## Notes
- Date format for challenge dates is `YYYY-MM-DD`.
- Trend compares recent 7 days to the previous 7 days.
- If previous 7-day total is 0 and recent is >0, trend shows as increasing/new activity.
- `View Progress` sends a PNG chart of daily totals (last 14 days) with a best-fit trend line.

## Troubleshooting

### Menu shows stale buttons (e.g., missing `Begin Training` row) or `View Progress` has no graph
This means an older `python bot.py` process is still running alongside the systemd service and stealing some of the long-poll updates. Telegram delivers each update to exactly one polling consumer, so half the messages hit the new code and half hit the old.

Fix on the VM:
```bash
# Find every bot.py process
ps -ef | grep 'python.*bot.py' | grep -v grep

# Kill any process NOT owned by the systemd service
sudo kill <PID>           # graceful
sudo kill -9 <PID>        # if it ignores SIGTERM

# Confirm systemd has the canonical one
sudo systemctl restart pushup-bot
sudo systemctl status pushup-bot
```

Then verify the deployed file is at the latest commit:
```bash
cd /path/to/bots && git status && git log -1 --oneline
```

## Storage Notes
New workout entries use this table:

```sql
CREATE TABLE IF NOT EXISTS daily_logs (
    chat_id INTEGER NOT NULL,
    log_date TEXT NOT NULL,
    pushups INTEGER NOT NULL DEFAULT 0,
    pullups INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, log_date)
);
```

The older `logs` table is retained as historical event-level data. Read queries combine both tables with `UNION ALL`, then aggregate totals by user/date/metric as needed. This means deployment only needs the normal code update and bot restart; no manual SQLite commands are required.

Future old-data optimisation plan:

1. Add an admin-only maintenance command that previews how many old `logs` rows would compact into daily totals.
2. Write compacted historical totals into a separate archive table, leaving `logs` unchanged during preview and first rollout.
3. Run verification queries comparing totals, date ranges, leaderboards, and reminder checks before and after enabling the archive reads.
4. Only after a backup and successful verification, consider a separate optional cleanup command for old event rows. That cleanup is intentionally not part of the current rollout.
