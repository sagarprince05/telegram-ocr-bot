"""
Telegram OCR -> AI extraction -> Google Sheets bot (FREE, no credit card)
"""

import io
import json
import logging
import os
from datetime import datetime

import gspread
import httpx
from google.oauth2.service_account import Credentials
from PIL import Image
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("ocr-bot")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
SHEET_ID = os.environ.get("SHEET_ID", "").strip()
OCR_SPACE_API_KEY = os.environ.get("OCR_SPACE_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "Sheet1").strip() or "Sheet1"
TIMEZONE = os.environ.get("TIMEZONE", "UTC").strip() or "UTC"
OCR_LANGUAGE = os.environ.get("OCR_LANGUAGE", "eng").strip() or "eng"
MODE = os.environ.get("MODE", "polling").strip().lower()
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))

OCR_SPACE_URL = "https://api.ocr.space/parse/image"
MAX_OCR_BYTES = 1_000_000

GEMINI_MODEL_FALLBACKS = [
    GEMINI_MODEL,
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-flash-latest",
    "gemini-2.5-flash-lite",
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADER_ROW = ["Timestamp", "Name", "Type", "Store", "Date", "Total", "Items", "Raw Text"]

_working_model = {"name": None}


def _fail(message):
    logger.error(message)
    raise SystemExit(1)


def build_gspread_client():
    if not GOOGLE_CREDENTIALS_JSON:
        _fail("GOOGLE_CREDENTIALS_JSON is not set.")
    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
    except json.JSONDecodeError as exc:
        _fail(f"GOOGLE_CREDENTIALS_JSON is not valid JSON: {exc}")
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_worksheet(gspread_client):
    if not SHEET_ID:
        _fail("SHEET_ID is not set.")
    try:
        spreadsheet = gspread_client.open_by_key(SHEET_ID)
    except Exception as exc:
        _fail(f"Could not open the Google Sheet with ID '{SHEET_ID}'. "
              f"Did you share the sheet with the service-account email? ({exc})")

    try:
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=WORKSHEET_NAME, rows=1000, cols=len(HEADER_ROW)
        )

    existing = worksheet.get_all_values()
    if not existing:
        worksheet.append_row(HEADER_ROW, value_input_option="USER_ENTERED")
    elif existing[0] != HEADER_ROW:
        worksheet.update([HEADER_ROW], "A1")
    return worksheet


def now_string():
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") + " UTC"


def shrink_image_if_needed(image_bytes):
    if len(image_bytes) <= MAX_OCR_BYTES:
        return image_bytes
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    quality = 85
    data = image_bytes
    for _ in range(8):
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        data = out.getvalue()
        if len(data) <= MAX_OCR_BYTES:
            return data
        img = img.resize((int(img.width * 0.8), int(img.height * 0.8)))
        quality = max(50, quality - 5)
    return data


async def ocr_image(image_bytes):
    image_bytes = shrink_image_if_needed(image_bytes)
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            OCR_SPACE_URL,
            data={
                "apikey": OCR_SPACE_API_KEY,
                "language": OCR_LANGUAGE,
                "OCREngine": "2",
                "scale": "true",
                "detectOrientation": "true",
                "isTable": "true",
            },
            files={"file": ("photo.jpg", image_bytes, "image/jpeg")},
        )
    response.raise_for_status()
    result = response.json()
    if result.get("IsErroredOnProcessing"):
        messages = result.get("ErrorMessage") or ["Unknown OCR error"]
        if isinstance(messages, str):
            messages = [messages]
        raise RuntimeError("; ".join(messages))
    parsed = result.get("ParsedResults") or []
    texts = [p.get("ParsedText", "") for p in parsed]
    return "\n".join(t for t in texts if t).strip()


EXTRACT_PROMPT = (
    "You are an expense/receipt parser. Read the text below (it may come from a "
    "photo of a bill, or be a plain note) and extract these fields. If a field "
    "is not present, return an empty string for it.\n"
    "- store: the shop / restaurant / vendor / company name.\n"
    "- date: the bill or transaction date exactly as written.\n"
    "- total: the final total amount paid, including the currency symbol/word "
    "if shown (e.g. 'Rs. 1529.00').\n"
    "- items: a short comma-separated list of the item names purchased, "
    "WITHOUT prices or quantities. If it's not a purchase, leave empty.\n\n"
    "TEXT:\n"
)

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "store": {"type": "STRING"},
        "date": {"type": "STRING"},
        "total": {"type": "STRING"},
        "items": {"type": "STRING"},
    },
    "required": ["store", "date", "total", "items"],
}


async def extract_fields(text):
    if not text.strip():
        return {"store": "", "date": "", "total": "", "items": ""}

    body = {
        "contents": [{"parts": [{"text": EXTRACT_PROMPT + text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "temperature": 0,
        },
    }

    models = []
    for m in ([_working_model["name"]] + GEMINI_MODEL_FALLBACKS):
        if m and m not in models:
            models.append(m)

    async with httpx.AsyncClient(timeout=45) as client:
        last_err = None
        for model in models:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/"
                f"models/{model}:generateContent"
            )
            try:
                resp = await client.post(
                    url,
                    headers={"x-goog-api-key": GEMINI_API_KEY},
                    json=body,
                )
                if resp.status_code == 404:
                    last_err = f"model {model} not found"
                    continue
                resp.raise_for_status()
                data = resp.json()
                raw = data["candidates"][0]["content"]["parts"][0]["text"]
                fields = json.loads(raw)
                _working_model["name"] = model
                return {
                    "store": str(fields.get("store", "")).strip(),
                    "date": str(fields.get("date", "")).strip(),
                    "total": str(fields.get("total", "")).strip(),
                    "items": str(fields.get("items", "")).strip(),
                }
            except Exception as exc:
                last_err = str(exc)
                continue

    logger.warning("Gemini extraction failed: %s", last_err)
    return {"store": "", "date": "", "total": "", "items": ""}


def save_row(worksheet, name, kind, fields, raw_text):
    worksheet.append_row(
        [
            now_string(),
            name,
            kind,
            fields.get("store", ""),
            fields.get("date", ""),
            fields.get("total", ""),
            fields.get("items", ""),
            raw_text,
        ],
        value_input_option="USER_ENTERED",
    )


def summary_reply(fields, raw_text):
    lines = ["✅ Saved to your sheet!"]
    if fields.get("store"):
        lines.append(f"🏪 Store: {fields['store']}")
    if fields.get("date"):
        lines.append(f"📅 Date: {fields['date']}")
    if fields.get("total"):
        lines.append(f"💰 Total: {fields['total']}")
    if fields.get("items"):
        lines.append(f"🧾 Items: {fields['items']}")
    if len(lines) == 1:
        preview = raw_text if len(raw_text) <= 500 else raw_text[:500] + "…"
        lines.append(preview)
    return "\n".join(lines)


async def start(update, context):
    await update.message.reply_text(
        "Hi! I'm your smart receipt logger.\n\n"
        "• Send me a photo of a bill/receipt and I'll read it, pull out the "
        "store, date, total and items, and save them in neat columns.\n"
        "• Send me text (like an expense note) and I'll do the same.\n\n"
        "Try sending a receipt photo!"
    )


async def handle_text(update, context):
    worksheet = context.bot_data["worksheet"]
    user = update.effective_user
    text = update.message.text
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )
    try:
        fields = await extract_fields(text)
        save_row(worksheet, user.full_name, "Text", fields, text)
        await update.message.reply_text(summary_reply(fields, text))
    except Exception as exc:
        logger.exception("Failed to handle text")
        await update.message.reply_text(f"⚠️ Sorry, I couldn't save that: {exc}")


async def handle_photo(update, context):
    worksheet = context.bot_data["worksheet"]
    user = update.effective_user

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    if update.message.photo:
        tg_file = await update.message.photo[-1].get_file()
    else:
        tg_file = await update.message.document.get_file()

    buffer = io.BytesIO()
    await tg_file.download_to_memory(out=buffer)
    image_bytes = buffer.getvalue()

    try:
        raw_text = await ocr_image(image_bytes)
    except Exception as exc:
        logger.exception("OCR failed")
        await update.message.reply_text(f"⚠️ I couldn't read that image: {exc}")
        return

    if not raw_text:
        await update.message.reply_text(
            "I looked at the image but couldn't find any readable text."
        )
        save_row(worksheet, user.full_name, "Image",
                 {"store": "", "date": "", "total": "", "items": ""},
                 "(no text found)")
        return

    fields = await extract_fields(raw_text)
    try:
        save_row(worksheet, user.full_name, "Image", fields, raw_text)
    except Exception as exc:
        logger.exception("Failed to save row")
        await update.message.reply_text(
            f"I read the image but couldn't save it to the sheet: {exc}"
        )
        return

    await update.message.reply_text(summary_reply(fields, raw_text))


async def handle_document(update, context):
    doc = update.message.document
    if doc and doc.mime_type and doc.mime_type.startswith("image/"):
        await handle_photo(update, context)
    else:
        await update.message.reply_text(
            "I can only read images right now. Please send a photo or an image file."
        )


async def on_error(update, context):
    logger.error("Unhandled error", exc_info=context.error)


def main():
    if not TELEGRAM_BOT_TOKEN:
        _fail("TELEGRAM_BOT_TOKEN is not set.")
    if not OCR_SPACE_API_KEY:
        _fail("OCR_SPACE_API_KEY is not set (get a free key at ocr.space).")
    if not GEMINI_API_KEY:
        _fail("GEMINI_API_KEY is not set (get a free key at aistudio.google.com).")

    gspread_client = build_gspread_client()
    worksheet = get_worksheet(gspread_client)
    logger.info("Connected to Google Sheet, worksheet '%s'.", WORKSHEET_NAME)

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.bot_data["worksheet"] = worksheet

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    application.add_error_handler(on_error)

    if MODE == "webhook":
        if not WEBHOOK_URL:
            _fail("MODE=webhook but WEBHOOK_URL is not set.")
        logger.info("Starting in WEBHOOK mode on port %s.", PORT)
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}",
            drop_pending_updates=True,
        )
    else:
        logger.info("Starting in POLLING mode.")
        application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
