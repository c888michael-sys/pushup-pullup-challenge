# Pushup/Pullup Challenge Telegram Bot

A lightweight Telegram bot for tracking pushup/pullup challenges with:
- Persistent reply menu
- Add/Minus logging (including 0)
- Challenge start/end + start date/end date/goal settings
- Progress stats (total, average/day, 7-day trend)
- Main-menu leaderboard (Top 3) with separate Pushup/Pullup columns
- Compact Top 20 view with separate Pushup/Pullup columns
- 8:00 PM Sydney reminder when nothing is logged that day
- First-time password gate + one-time display name capture
- Admin support (first authenticated user becomes admin, can kick users)

## Tech Choice
This bot uses Python + SQLite (`sqlite3`) for a small footprint and simple deployment on a Google Cloud VM.

## Files
- `bot.py` - main bot app
- `requirements.txt` - dependencies
- `pushup_pullup_bot.db` - SQLite DB (auto-created at runtime)

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
- Main menu message always shows `Top 3` with Pushup/Pullup side-by-side, then menu buttons.
- Main menu buttons:
  - `Add`
  - `Minus`
  - `View Progress`
  - `Start` or `End` (dynamic)
  - `Leaderboard` (shows compact top 20 with Pushup/Pullup side-by-side)
  - `Admin Panel` (admin only)
- After `Start`, bot shows config menu:
  - `Start Date`
  - `End Date`
  - `Goal`
  - `Done`
- `Add`/`Minus` flow:
  - Choose `Pushup` or `Pullup`
  - Enter whole number (`0` allowed)
- Reminder logic:
  - Uses `Australia/Sydney`
  - Around 8:00 PM, if no logs exist for that day, sends reminder.
- Admin panel:
  - `Kick User`
  - Admin can kick non-admin users by chat ID
  - Kicked users lose access

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
