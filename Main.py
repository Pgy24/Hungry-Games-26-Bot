from __future__ import annotations
import os, json, time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from telegram import (
    Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)

import gspread
from oauth2client.service_account import ServiceAccountCredentials

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GSHEET_NAME = os.getenv("GSHEET_NAME", "AmazingRace Live")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
ATTEMPTS_PER_Q = int(os.getenv("ATTEMPTS_PER_Q", 3))
HINT_PENALTY = float(os.getenv("HINT_PENALTY", 0.5))
USE_GEOFENCE = os.getenv("USE_GEOFENCE", "false").lower() in {"1", "true", "yes"}
STATE_FILE = Path("state.json")

# ---------------- Game Data ----------------
# Replace with your 10 locations; answer_code must only be discoverable on-site.
QUESTIONS = [
    {
        "id": 1,
        "title": "Civic District — Old Supreme Court",
        "prompt": "Find the bronze statue of X. Submit the 6-letter password under the emblem.",
        "answer_code": "MERLION",  # placeholder
        "hints": [
            "It is near the steps facing the Padang.",
            "Look beneath the coat of arms plate."
        ],
        "geofence": {"lat": 1.29027, "lon": 103.8515, "radius_m": 120},
    },
    # Add Q2..Q10 similarly
]

# pad to exactly 10 for safety during early testing
while len(QUESTIONS) < 10:
    i = len(QUESTIONS) + 1
    QUESTIONS.append({
        "id": i,
        "title": f"Spot #{i}",
        "prompt": f"Go to location #{i} and submit the on-site code.",
        "answer_code": f"CODE{i}",
        "hints": ["Check the signboard.", "Try the ticket counter area."],
        "geofence": None,
    })

# ---------------- Data Classes ----------------
@dataclass
class TeamState:
    team_name: str
    user_id: int
    current_q: int = 1
    score: float = 0.0
    attempts_left: int = ATTEMPTS_PER_Q
    hints_used_current_q: int = 0
    history: List[Dict] = field(default_factory=list)  # [{q, correct, points, attempts, hints}]
    last_location: Optional[Dict] = None               # {lat, lon, ts}

# ---------------- Persistence ----------------
class Store:
    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, TeamState] = {}
        self.load()

    def load(self):
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.data = {k: TeamState(**v) for k, v in raw.items()}

    def save(self):
        self.path.write_text(json.dumps({k: asdict(v) for k, v in self.data.items()}, indent=2))

    def get(self, team: str) -> Optional[TeamState]:
        return self.data.get(team)

    def upsert(self, team: TeamState):
        self.data[team.team_name] = team
        self.save()

STORE = Store(STATE_FILE)

# ---------------- Google Sheets ----------------
SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

def gs_client():
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"], SCOPES
    )
    return gspread.authorize(creds)

SHEET_COLUMNS = [
    "team_name", "user_id", "current_q", "score",
    "attempts_left", "hints_used_current_q",
    "last_lat", "last_lon", "last_ts",
]

def sync_row(team: TeamState):
    try:
        gc = gs_client()
        sh = gc.open(GSHEET_NAME)
        try:
            ws = sh.worksheet("live")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet("live", rows=200, cols=len(SHEET_COLUMNS))
            ws.update("A1:I1", [SHEET_COLUMNS])
        try:
            cell = ws.find(team.team_name)
        except gspread.exceptions.CellNotFound:
            cell = None
        row = [
            team.team_name, team.user_id, team.current_q, team.score,
            team.attempts_left, team.hints_used_current_q,
            (team.last_location or {}).get("lat"),
            (team.last_location or {}).get("lon"),
            (team.last_location or {}).get("ts"),
        ]
        if cell:
            ws.update(f"A{cell.row}:I{cell.row}", [row])
        else:
            ws.append_row(row)
    except Exception as e:
        print("[GSHEET] sync failed:", e)

# ---------------- Helpers ----------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_q(i: int) -> Dict:
    return next(q for q in QUESTIONS if q["id"] == i)

def within_geofence(q: Dict, lat: float, lon: float) -> bool:
    if not (USE_GEOFENCE and q.get("geofence")):
        return True
    from math import radians, cos, sin, asin, sqrt
    g = q["geofence"]
    R = 6371000.0
    dlat = radians(lat - g["lat"])
    dlon = radians(lon - g["lon"])
    a = sin(dlat/2)**2 + cos(radians(lat))*cos(radians(g["lat"]))*sin(dlon/2)**2
    d = 2 * R * asin(sqrt(a))
    return d <= g["radius_m"]

def _require_team(update: Update) -> Optional[TeamState]:
    user_id = update.effective_user.id
    for t in STORE.data.values():
        if t.user_id == user_id:
            return t
    try:
        update.message.reply_text("Please /register <TEAM_NAME> first.")
    except Exception:
        pass
    return None

# ---------------- Handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to Amazing Race!\n\n"
        "Use /register <TEAM_NAME> to begin (one participant per team).\n"
        "Then /begin to get your first location clue.\n\n"
        "Commands: /status /answer <code> /hint /location /scoreboard\n"
        "Admin: /broadcast /where /force /scoreboard"
    )

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /register <TEAM_NAME>")
        return
    team_name = " ".join(context.args).strip()
    user_id = update.effective_user.id
    if STORE.get(team_name):
        await update.message.reply_text("This team name is taken. Choose another.")
        return
    team = TeamState(team_name=team_name, user_id=user_id)
    STORE.upsert(team)
    sync_row(team)
    await update.message.reply_text(
        f"Team {team_name} registered!\nUse /begin when ready."
    )

async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team = _require_team(update)
    if not team:
        return
    q = get_q(team.current_q)
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("Send live location", request_location=True)]],
        one_time_keyboard=True, resize_keyboard=True
    )
    await update.message.reply_text(
        f"Q{q['id']}: {q['title']}\n{q['prompt']}\n\n"
        f"Attempts left: {team.attempts_left}. Use /answer <code> when ready.\n"
        f"Need a hint? /hint (-{HINT_PENALTY} point if you get it right).",
        reply_markup=kb
    )

async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team = _require_team(update)
    if not team:
        return
    loc = update.message.location
    team.last_location = {"lat": loc.latitude, "lon": loc.longitude, "ts": int(time.time())}
    STORE.upsert(team)
    sync_row(team)
    await update.message.reply_text("Location received. Good luck!", reply_markup=ReplyKeyboardRemove())

async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team = _require_team(update)
    if not team:
        return
    if not context.args:
        await update.message.reply_text("Usage: /answer <code>")
        return
    code = " ".join(context.args).strip().upper()
    q = get_q(team.current_q)

    if team.last_location and not within_geofence(q, team.last_location["lat"], team.last_location["lon"]):
        await update.message.reply_text(
            "You do not appear to be at the location yet. Send your live location first (/location)."
        )
        return

    if code == q["answer_code"].upper():
        points = max(0.0, 1.0 - team.hints_used_current_q * HINT_PENALTY)
        team.score += points
        team.history.append({
            "q": team.current_q,
            "correct": True,
            "points": points,
            "attempts": ATTEMPTS_PER_Q - team.attempts_left + 1,
            "hints": team.hints_used_current_q,
        })
        team.current_q += 1
        team.attempts_left = ATTEMPTS_PER_Q
        team.hints_used_current_q = 0
        STORE.upsert(team)
        sync_row(team)

        if team.current_q > len(QUESTIONS):
            await update.message.reply_text(f"Finished! Final score: {team.score:.2f}")
        else:
            await update.message.reply_text(f"Correct! +{points:.2f} points. Total: {team.score:.2f}")
            await begin(update, context)
    else:
        team.attempts_left -= 1
        STORE.upsert(team)
        sync_row(team)
        if team.attempts_left <= 0:
            team.history.append({
                "q": team.current_q,
                "correct": False,
                "points": 0.0,
                "attempts": ATTEMPTS_PER_Q,
                "hints": team.hints_used_current_q,
            })
            team.current_q += 1
            team.attempts_left = ATTEMPTS_PER_Q
            team.hints_used_current_q = 0
            STORE.upsert(team)
            sync_row(team)
            await update.message.reply_text("Out of attempts. Moving to next question.")
            if team.current_q <= len(QUESTIONS):
                await begin(update, context)
            else:
                await update.message.reply_text(f"Game over! Final score: {team.score:.2f}")
        else:
            await update.message.reply_text(f"Incorrect. Attempts left: {team.attempts_left}")

async def hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team = _require_team(update)
    if not team:
        return
    q = get_q(team.current_q)
    idx = team.hints_used_current_q
    if idx >= len(q["hints"]):
        await update.message.reply_text("No more hints for this question.")
        return
    team.hints_used_current_q += 1
    STORE.upsert(team)
    sync_row(team)
    await update.message.reply_text(
        f"Hint {idx+1}: {q['hints'][idx]} (-{HINT_PENALTY} point if you solve this question)."
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team = _require_team(update)
    if not team:
        return
    await update.message.reply_text(
        f"Team: {team.team_name}\n"
        f"Q: {team.current_q}/{len(QUESTIONS)}\n"
        f"Score: {team.score:.2f}\n"
        f"Attempts left: {team.attempts_left}\n"
        f"Hints used (this Q): {team.hints_used_current_q}"
    )

async def location_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("Send live location", request_location=True)]],
        one_time_keyboard=True, resize_keyboard=True
    )
    await update.message.reply_text("Tap to share your live location:", reply_markup=kb)

async def scoreboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teams = sorted(STORE.data.values(), key=lambda t: (-t.score, t.current_q))
    lines = ["Scoreboard:"]
    for i, t in enumerate(teams, 1):
        lines.append(f"{i}. {t.team_name} — {t.score:.2f} pts (Q{t.current_q})")
    await update.message.reply_text("\n".join(lines))

# ---------------- Admin ----------------
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = " ".join(context.args) if context.args else "(empty)"
    for t in STORE.data.values():
        try:
            await context.bot.send_message(chat_id=t.user_id, text=f"Admin: {msg}")
        except Exception as e:
            print("broadcast fail:", t.team_name, e)
    await update.message.reply_text("Broadcast sent.")

async def where(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("/where <TEAM_NAME>")
        return
    team_name = " ".join(context.args)
    team = STORE.get(team_name)
    if not team:
        await update.message.reply_text("Team not found")
        return
    loc = team.last_location or {}
    await update.message.reply_text(
        f"Team {team.team_name}: Q{team.current_q}, score {team.score:.2f}\n"
        f"Attempts {team.attempts_left}, hints {team.hints_used_current_q}\n"
        f"Loc: {loc}"
    )

async def force(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("/force <TEAM_NAME> <Q_NUMBER>")
        return
    team_name = context.args[0]
    qn = int(context.args[1])
    team = STORE.get(team_name)
    if not team:
        await update.message.reply_text("Team not found")
        return
    team.current_q = max(1, min(qn, len(QUESTIONS)))
    team.attempts_left = ATTEMPTS_PER_Q
    team.hints_used_current_q = 0
    STORE.upsert(team)
    sync_row(team)
    await update.message.reply_text(f"Forced {team_name} to Q{team.current_q}")

# ---------------- Main ----------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN not set")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("begin", begin))
    app.add_handler(CommandHandler("answer", answer))
    app.add_handler(CommandHandler("hint", hint))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("location", location_request))
    app.add_handler(CommandHandler("scoreboard", scoreboard))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("where", where))
    app.add_handler(CommandHandler("force", force))
    app.add_handler(MessageHandler(filters.LOCATION, on_location))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
