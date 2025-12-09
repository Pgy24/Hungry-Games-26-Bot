# Amazing Race Telegram Bot

Telegram bot for a 10-question, 10-20-team Amazing Race/Hungry Games. Supports per-team registration, on-site answer codes, hints with penalties, attempts per question, optional geofence check, live Google Sheets sync, scoreboard, and admin controls.

## Quick setup

Install deps:

```
pip install python-telegram-bot==21.4 gspread oauth2client python-dotenv
```

Copy `.env.example` to `.env` and fill with your secrets:

```
BOT_TOKEN=123:ABC
GSHEET_NAME=AmazingRace Live
ADMIN_IDS=11111111,22222222
ATTEMPTS_PER_Q=3
HINT_PENALTY=0.5
USE_GEOFENCE=true
GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json
```

## Google Sheets

- Create a sheet named e.g. AmazingRace Live.
- In Google Cloud console: enable Google Sheets API, create a service account, download the JSON key.
- Share the sheet with the service account email.
- First run creates a live worksheet and updates rows like:
  team_name, user_id, current_q, score, attempts_left, hints_used_current_q, last_lat, last_lon, last_ts.

## Fill your locations/codes
Edit Main.py in the QUESTIONS list. For each item set:
- title - where they are headed
- prompt - what they must find/do
- answer_code - the secret on-site code (only discoverable physically)
- hints - 1-2 hints (each hint deducts HINT_PENALTY from that question's 1 point)
- optional geofence - {"lat": <lat>, "lon": <lon>, "radius_m": <meters>} to require being on site

## Run

```
python Main.py
```

## What the bot already supports
- /register <TEAM_NAME> (one participant per team)
- /begin -> shows Q prompt + "Send live location" button
- /location -> asks for live location (if geofence enabled)
- /answer <code> -> checks code; if correct: points = 1 - hints_used * penalty, advances to next Q
- /hint -> sends next hint and applies the deduction if they later solve it
- 3 attempts per question -> auto-advance with 0 point if they fail
- /status and /scoreboard
- Live sync to Google Sheets on each change

## Admin tools (for emergencies)
- /broadcast <msg> -> DM all teams
- /where <TEAM_NAME> -> shows current Q, score, last location
- /force <TEAM_NAME> <Q_NO> -> jump a team to a specific question
- /scoreboard -> quick in-chat leaderboard

## Anti-cheat ideas you can enable
- Geofencing: USE_GEOFENCE=true and set lat/lon/radius for each question.
- On-site codes: make answer_code something only readable on a plaque/sign (or a small sticker you place).
- QR variant: put a short code after a hidden # so OCR/AI from photos will not guess it easily.

## Scoring rule (built in)
- Correct answer = 1 point minus HINT_PENALTY per hint used on that question (floors at 0).
- Wrong attempt reduces attempts_left. After 3 misses -> 0 point and auto-advance.

## Nice extras you can add later
- Add inline buttons for Ask hint, Share location, Skip (-time or -points).
- Push a live public scoreboard using Apps Script that reads the same sheet.
- Add team check-ins by scanning a QR code that triggers a deep-link to your bot.
- Export a CSV at the end (for example, add an /export admin command to dump STORE.data).

## Deploying later
- Easy path: keep it on a small VM/Raspberry Pi with a process manager (systemd or pm2).
- Cloud: add a Dockerfile and deploy to Render, Railway, or Cloud Run. If you want, I can add that scaffold.
