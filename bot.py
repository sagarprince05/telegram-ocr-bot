"""
Telegram OCR -> Google Sheets bot (100% FREE version, no credit card needed)
============================================================================

What it does
------------
* Listens for messages you send to your Telegram bot.
* If you send TEXT, it saves that text to your Google Sheet.
* If you send a PHOTO (or an image file), it reads the text inside the image
  using the free OCR.space service, saves the extracted text to your Google
  Sheet, and replies to you with what it found.

You do NOT need to edit this file. Everything it needs is provided through
"environment variables" (settings you fill in on the hosting site). The setup
guide walks you through every one of them.

Required settings (environment variables):
    TELEGRAM_BOT_TOKEN        The token BotFather gives you.
    GOOGLE_CREDENTIALS_JSON   The entire contents of your Google service-account
                              JSON key file, pasted as one value. (Used only
                              for Google Sheets — free, no billing needed.)
    SHEET_ID                  The long ID from your Google Sheet's URL.
    OCR_SPACE_API_KEY         Your free API key from ocr.space (no card needed).

Optional settings:
    WORKSHEET_NAME            Tab name inside the sheet. Default: "Sheet1".
    TIMEZONE                  e.g. "Asia/Kolkata". Default: "UTC".
    OCR_LANGUAGE              OCR language code, e.g. "eng" (English) or
                              "hin" (Hindi). Default: "eng".
    MODE                      "polling" (run on your PC) or "webhook" (cloud).
                              Default: "polling".
    WEBHOOK_URL               Only for MODE=webhook. Your app's public https URL.
    PORT                      Only for MODE=webhook. The host sets this for you.
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
except ImportError:  # pragma: no cover
    ZoneInfo = None

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("ocr-bot")

# --------------------------------------------------------------------------- #
# Read configuration from environment variables
# --------------------------------------------------------------------------- #
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
SHEET_ID = os.environ.get("SHEET_ID", "").strip()
OCR_SPACE_API_KEY = os.environ.get("OCR_SPACE_API_KEY", "").strip()
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "Sheet1").strip() or "Sheet1"
TIMEZONE = os.environ.get("TIMEZONE", "UTC").strip() or "UTC"
OCR_LANGUAGE = os.environ.get("OCR_LANGUAGE", "eng").strip() or "eng"
MODE = os.environ.get("MODE", "polling").strip().lower()
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))

OCR_SPACE_URL = "https://api.ocr.space/parse/image"
# OCR.space free plan allows files up to 1 MB. We shrink bigger images.
MAX_OCR_BYTES = 1_000_000

# Google API permissions the service account needs (Sheets only — free).
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADER_ROW = ["Timestamp", "Name", "Username", "Type", "Extracted Text"]


def _fail(message: str) -> None:
    """Print a clear error and stop the program."""
    logger.error(message)
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
# Google Sheets client
# --------------------------------------------------------------------------- #
def build_gspread_client():
    if not GOOGLE_CREDENTIALS_JSON:
        _fail("GOOGLE_CREDENTIALS_JSON is not set. Paste your service-account "
              "JSON key into that environment variable.")
    try:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
    except json.JSONDecodeError as exc:
        _fail(f"GOOGLE_CREDENTIALS_JSON is not valid JSON: {exc}")

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_worksheet(gspread_client):
    """Open the target worksheet and make sure it has a header row."""
    if not SHEET_ID:
        _fail("SHEET_ID is not set. Copy the long ID from your Google Sheet URL.")
    try:
        spreadsheet = gspread_client.open_by_key(SHEET_ID)
    except Exception as exc:  # noqa: BLE001
        _fail(f"Could not open the Google Sheet with ID '{SHEET_ID}'. "
              f"Did you share the sheet with the service-account email? ({exc})")

    try:
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=WORKSHEET_NAME, rows=1000, cols=len(HEADER_ROW)
        )

    if not worksheet.get_all_values():
        worksheet.append_row(HEADER_ROW, value_input_option="USER_ENTERED")
    return worksheet


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def now_string() -> str:
    """Current time as a readable string in the configured timezone."""
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(TIMEZONE)
            return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            pass
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") + " UTC"


def shrink_image_if_needed(image_bytes: bytes) -> bytes:
    """OCR.space's free plan accepts files up to ~1 MB.

    If the image is bigger, resize/re-encode it as JPEG until it fits.
    """
    if len(image_bytes) <= MAX_OCR_BYTES:
        return image_bytes

    img = Image.open(io.BytesIO(image_bytes))
    # Convert to RGB so we can always save as JPEG.
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    quality = 85
    for _ in range(8):
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        data = out.getvalue()
        if len(data) <= MAX_OCR_BYTES:
            return data
        # Still too big: shrink dimensions and lower quality a bit.
        img = img.resize((int(img.width * 0.8), int(img.height * 0.8)))
        quality = max(50, quality - 5)
    return data  # best effort


async def ocr_image(image_bytes: bytes) -> str:
    """Send the image to OCR.space and return the extracted text."""
    image_bytes = shrink_image_if_needed(image_bytes)

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            OCR_SPACE_URL,
            data={
                "apikey": OCR_SPACE_API_KEY,
                "language": OCR_LANGUAGE,
                "OCREngine": "2",       # engine 2: better for receipts/photos
                "scale": "true",        # upscale small text
                "detectOrientation": "true",  # fix tilted photos
                "isTable": "true",      # keep receipt line layout
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


def save_row(worksheet, name, username, kind, text) -> None:
    """Append one row to the Google Sheet."""
    worksheet.append_row(
        [now_string(), name, username, kind, text],
        value_input_option="USER_ENTERED",
    )


# --------------------------------------------------------------------------- #
# Telegram handlers
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hi! I'm your OCR logging bot.\n\n"
        "• Send me any *text* and I'll save it to your Google Sheet.\n"
        "• Send me a *photo* (a receipt, a screenshot, a document) and I'll "
        "read the text inside it, save it, and reply with what I found.\n\n"
        "Go ahead — send me something!",
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    worksheet = context.bot_data["worksheet"]
    user = update.effective_user
    text = update.message.text
    try:
        save_row(worksheet, user.full_name, user.username or "", "Text", text)
        await update.message.reply_text("✅ Saved your text to the sheet.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to save text")
        await update.message.reply_text(f"⚠️ Sorry, I couldn't save that: {exc}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    worksheet = context.bot_data["worksheet"]
    user = update.effective_user

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    # A photo comes in several sizes; the last one is the largest/best quality.
    if update.message.photo:
        tg_file = await update.message.photo[-1].get_file()
    else:
        tg_file = await update.message.document.get_file()

    buffer = io.BytesIO()
    await tg_file.download_to_memory(out=buffer)
    image_bytes = buffer.getvalue()

    try:
        text = await ocr_image(image_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.exception("OCR failed")
        await update.message.reply_text(f"⚠️ I couldn't read that image: {exc}")
        return

    if not text:
        await update.message.reply_text(
            "I looked at the image but couldn't find any readable text."
        )
        save_row(worksheet, user.full_name, user.username or "", "Image", "(no text found)")
        return

    try:
        save_row(worksheet, user.full_name, user.username or "", "Image", text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to save OCR text")
        await update.message.reply_text(
            f"I read the image but couldn't save it to the sheet: {exc}"
        )
        return

    # Telegram messages max out at 4096 characters; trim long results.
    preview = text if len(text) <= 3500 else text[:3500] + "\n…(truncated)"
    await update.message.reply_text(
        "✅ Saved! Here's the text I extracted:\n\n" + preview
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle image files sent as documents; ignore non-images politely."""
    doc = update.message.document
    if doc and doc.mime_type and doc.mime_type.startswith("image/"):
        await handle_photo(update, context)
    else:
        await update.message.reply_text(
            "I can only read images right now. Please send a photo or an image file."
        )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error", exc_info=context.error)


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        _fail("TELEGRAM_BOT_TOKEN is not set. Get it from BotFather on Telegram.")
    if not OCR_SPACE_API_KEY:
        _fail("OCR_SPACE_API_KEY is not set. Get a free key at "
              "https://ocr.space/ocrapi/freekey (no card needed).")

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
