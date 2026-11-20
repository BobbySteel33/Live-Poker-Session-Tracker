"""
Live Poker Session Tracker Telegram Bot.
Runs 24/7 on Render / Railway or any Python environment.
"""
import os
import sys
import re
import csv
import logging
import threading
from datetime import datetime
from enum import Enum
from typing import Dict, Any, List, Optional
from http.server import HTTPServer, BaseHTTPRequestHandler

# Sanitize NO_PROXY for httpx compatibility with Linux cloud environments
for k in ["NO_PROXY", "no_proxy"]:
    if k in os.environ:
        os.environ[k] = ",".join(p for p in os.environ[k].split(",") if p not in ["::1", "[::1]"])

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# Configuration
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "9167477453:AAG2W8M1FSShDTpEbEGfv-4u2E0_lcNMfvs")
GOOGLE_SHEET_WEBHOOK_URL = os.environ.get(
    "GOOGLE_SHEET_WEBHOOK_URL",
    "https://script.google.com/macros/s/AKfycbyLbpvK-rKHBPkr84xugAPIf-aZKC9DAdoilrvK34cYjqibVzfKsdVBZ98K2A2XgscQVA/exec"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class Outcome(str, Enum):
    WON = "WON"
    LOST = "LOST"
    WHIFFED_FOLDED = "WHIFFED / FOLDED"
    NO_VPIP = "NO HANDS PLAYED"

class HandRecord:
    def __init__(
        self,
        timestamp: str,
        block_number: int,
        raw_message: str,
        cards: str = "N/A",
        position: str = "N/A",
        outcome: Outcome = Outcome.NO_VPIP,
        is_multiway: bool = False,
        mental_state: str = "Feeling Good",
        notes: str = "",
        net_bb: Optional[float] = None
    ):
        self.timestamp = timestamp
        self.block_number = block_number
        self.raw_message = raw_message
        self.cards = cards
        self.position = position
        self.outcome = outcome
        self.is_multiway = is_multiway
        self.mental_state = mental_state
        self.notes = notes
        self.net_bb = net_bb

def log_to_google_sheet(records: List[HandRecord]):
    for r in records:
        row = [
            r.timestamp,
            f"Block {r.block_number}",
            r.cards,
            r.position,
            r.outcome.value,
            r.mental_state,
            "Yes" if r.is_multiway else "No",
            f"{r.net_bb:+.1f}" if r.net_bb is not None else "—",
            "—",
            r.notes,
            r.raw_message
        ]
        if GOOGLE_SHEET_WEBHOOK_URL:
            try:
                requests.post(GOOGLE_SHEET_WEBHOOK_URL, json={"row": row}, timeout=5)
            except Exception as e:
                logger.warning(f"Google Sheet webhook sync error: {e}")

def parse_shorthand_hands(text: str, block_number: int) -> List[HandRecord]:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    clean_text = text.strip()

    valid_ranks = "AKQJT98765432"
    card_pattern = r"\b([AKQJT2-9][AKQJT2-9][so]?|[AKQJT2-9][shdc][AKQJT2-9][shdc])\b"
    stop_words = {"at", "in", "to", "as", "so", "it", "is", "on", "no", "he", "we", "be", "do", "go"}

    candidate_matches = list(re.finditer(card_pattern, clean_text, re.IGNORECASE))
    card_matches = []
    for m in candidate_matches:
        val = m.group(1)
        if val.lower() in stop_words:
            continue
        if len(val) == 4 and val[1].lower() in "shdc" and val[3].lower() in "shdc":
            if val[0].upper() in valid_ranks and val[2].upper() in valid_ranks:
                card_matches.append((val.upper(), m.start(), m.end()))
        else:
            if val[0].upper() in valid_ranks and val[1].upper() in valid_ranks:
                card_matches.append((val.upper(), m.start(), m.end()))

    hand_segments = []
    if len(card_matches) > 1:
        for i, (card_str, start_idx, end_idx) in enumerate(card_matches):
            seg_start = start_idx
            seg_end = card_matches[i + 1][1] if i + 1 < len(card_matches) else len(clean_text)
            hand_segments.append((card_str, clean_text[seg_start:seg_end].strip()))
    elif len(card_matches) == 1:
        hand_segments.append((card_matches[0][0], clean_text))
    else:
        hand_segments.append(("Hand", clean_text))

    hands = []
    for card_label, chunk in hand_segments:
        pos_match = re.search(r"\b(btn|button|co|cutoff|hj|hijack|utg(\+1)?|sb|small blind|bb|big blind|ep|mp|lp|late|early)\b", chunk, re.IGNORECASE)
        position = pos_match.group(1).upper() if pos_match else "N/A"
        is_multiway = bool(re.search(r"\b(multiway|multi-way|family pot|3-way|4-way|limped pot)\b", chunk, re.IGNORECASE))

        hands.append(HandRecord(
            timestamp=timestamp,
            block_number=block_number,
            raw_message=clean_text,
            cards=card_label,
            position=position,
            outcome=Outcome.WHIFFED_FOLDED,
            is_multiway=is_multiway,
            notes=chunk
        ))
    return hands

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Live Poker Session Tracker is Healthy and Running 24/7.")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        server.serve_forever()
    except Exception as e:
        logger.warning(f"Health server failed: {e}")

user_sessions: Dict[int, Dict[str, Any]] = {}

def get_session(chat_id: int) -> Dict[str, Any]:
    if chat_id not in user_sessions:
        user_sessions[chat_id] = {
            "active": False,
            "block": 1,
            "total_vpip": 0,
            "pending_hands": [],
            "current_hand_idx": 0,
            "completed_hands": [],
            "state": "IDLE"
        }
    return user_sessions[chat_id]

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_session(chat_id)
    sess["active"] = True
    sess["block"] = 1
    sess["total_vpip"] = 0
    sess["pending_hands"] = []
    sess["completed_hands"] = []
    sess["state"] = "IDLE"

    current_jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    for job in current_jobs:
        job.schedule_removal()

    context.job_queue.run_repeating(
        send_checkin_prompt,
        interval=1800,
        first=1800,
        chat_id=chat_id,
        name=str(chat_id)
    )

    welcome_text = (
        "🟢 Session active. Keeping everything quiet and discreet.\n\n"
        "I will ping you every 30 minutes.\n"
        "• Type /ping anytime to test a check-in right now.\n"
        "• Type /end when you leave the table."
    )
    await update.message.reply_text(welcome_text)

async def test_ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_session(chat_id)
    sess["active"] = True
    await send_checkin_to_chat(context.bot, chat_id, sess)

async def send_checkin_prompt(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.chat_id
    sess = get_session(chat_id)
    if not sess["active"]:
        return
    await send_checkin_to_chat(context.bot, chat_id, sess)

async def send_checkin_to_chat(bot, chat_id: int, sess: Dict[str, Any]):
    sess["state"] = "WAITING_HANDS"
    sess["pending_hands"] = []
    sess["completed_hands"] = []
    sess["current_hand_idx"] = 0

    block_num = sess["block"]
    elapsed_mins = block_num * 30

    keyboard = [
        [InlineKeyboardButton("⚪ 0 Hands (Folded All)", callback_data="zero_hands")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = f"⏰ {elapsed_mins}m check-in (Block #{block_num}):\nAny hands entered this block?"
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_session(chat_id)

    if not sess["active"]:
        await update.message.reply_text("No active session. Send /start to begin.")
        return

    text = update.message.text.strip()

    if sess["state"] in ["WAITING_HANDS", "IDLE"]:
        lower = text.lower()
        if any(w in lower for w in ["0 hands", "folded all", "card dead", "nothing", "no action"]):
            record = HandRecord(
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
                block_number=sess["block"],
                raw_message=text,
                cards="None",
                position="—",
                outcome=Outcome.NO_VPIP,
                notes="Card dead / Folded all hands"
            )
            sess["completed_hands"] = [record]
            sess["state"] = "WAITING_MOOD"
            await prompt_mental_state(update.message, sess)
            return

        parsed = parse_shorthand_hands(text, sess["block"])
        sess["pending_hands"] = parsed
        sess["current_hand_idx"] = 0
        sess["completed_hands"] = []
        sess["state"] = "WAITING_OUTCOME"
        await prompt_hand_outcome(update.message, sess)
        return

    await update.message.reply_text("Tap the button above, or type your next hand.")

async def prompt_hand_outcome(message_or_query, sess: Dict[str, Any]):
    hand = sess["pending_hands"][sess["current_hand_idx"]]
    cards = hand.cards
    pos = f" ({hand.position})" if hand.position != "N/A" else ""

    keyboard = [
        [
            InlineKeyboardButton("🟢 Won", callback_data="outcome_WON"),
            InlineKeyboardButton("🔴 Lost", callback_data="outcome_LOST"),
            InlineKeyboardButton("⚪ Whiffed / Folded", callback_data="outcome_WHIFFED")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = f"Outcome for {cards}{pos}?"
    if hasattr(message_or_query, "reply_text"):
        await message_or_query.reply_text(text, reply_markup=reply_markup)
    else:
        await message_or_query.message.reply_text(text, reply_markup=reply_markup)

async def prompt_mental_state(message_or_query, sess: Dict[str, Any]):
    keyboard = [
        [
            InlineKeyboardButton("😊 Feeling Good", callback_data="mood_Feeling Good"),
            InlineKeyboardButton("🎯 Focused / Sharp", callback_data="mood_Focused")
        ],
        [
            InlineKeyboardButton("🧘 Patient / Calm", callback_data="mood_Patient"),
            InlineKeyboardButton("🥱 Bored / Restless", callback_data="mood_Bored")
        ],
        [
            InlineKeyboardButton("😤 Tilted / Frustrated", callback_data="mood_Tilted"),
            InlineKeyboardButton("⚠️ Fatigued / Tired", callback_data="mood_Fatigued")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "How's your mental state right now?"
    if hasattr(message_or_query, "reply_text"):
        await message_or_query.reply_text(text, reply_markup=reply_markup)
    else:
        await message_or_query.message.reply_text(text, reply_markup=reply_markup)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    sess = get_session(chat_id)
    data = query.data

    if data == "zero_hands":
        record = HandRecord(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
            block_number=sess["block"],
            raw_message="0 hands (Folded all)",
            cards="None",
            position="—",
            outcome=Outcome.NO_VPIP,
            notes="Card dead / Folded all hands"
        )
        sess["completed_hands"] = [record]
        sess["state"] = "WAITING_MOOD"
        await query.edit_message_text(f"Block #{sess['block']}: 0 hands logged.")
        await prompt_mental_state(query, sess)
        return

    if data.startswith("outcome_"):
        outcome_str = data.replace("outcome_", "")
        outcome_val = Outcome.WON if outcome_str == "WON" else (Outcome.LOST if outcome_str == "LOST" else Outcome.WHIFFED_FOLDED)
        
        current_h = sess["pending_hands"][sess["current_hand_idx"]]
        current_h.outcome = outcome_val
        sess["completed_hands"].append(current_h)
        sess["total_vpip"] += 1

        await query.edit_message_text(f"Logged: {current_h.cards} • {outcome_val.value.split()[0]}")

        sess["current_hand_idx"] += 1
        if sess["current_hand_idx"] < len(sess["pending_hands"]):
            await prompt_hand_outcome(query, sess)
        else:
            sess["state"] = "WAITING_MOOD"
            await prompt_mental_state(query, sess)
        return

    if data.startswith("mood_"):
        mood_str = data.replace("mood_", "")
        await query.edit_message_text(f"Mental State: {mood_str}")

        for r in sess["completed_hands"]:
            r.mental_state = mood_str

        log_to_google_sheet(sess["completed_hands"])

        sess["state"] = "IDLE"
        confirm_text = (
            f"Logged to spreadsheet. Total VPIP today: {sess['total_vpip']}.\n"
            f"Staying locked in. Next ping in 30m."
        )
        sess["block"] += 1
        await context.bot.send_message(chat_id=chat_id, text=confirm_text)
        return

async def end_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_session(chat_id)
    if not sess["active"]:
        await update.message.reply_text("No active session.")
        return

    current_jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    for job in current_jobs:
        job.schedule_removal()

    total_blocks = sess["block"] - 1
    total_hours = round(total_blocks * 0.5, 1)
    vpip = sess["total_vpip"]
    rate = round(vpip / max(total_hours, 0.5), 1)

    sess["active"] = False
    summary = (
        f"🏁 Session completed.\n"
        f"• Duration: {total_hours} hrs ({total_blocks} blocks)\n"
        f"• Total Hands VPIP'd: {vpip} (~{rate} hands/hr)\n"
        f"• All hands and mental states saved to Google Sheet."
    )
    await update.message.reply_text(summary)

def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ping", test_ping_cmd))
    app.add_handler(CommandHandler("end", end_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
