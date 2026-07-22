"""Telegram bill logger -> Google Sheets (FREE)"""

import io
import json
import logging
import os
import re
from datetime import datetime

import gspread
import httpx
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build as _google_build
from googleapiclient.http import MediaIoBaseUpload
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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest").strip() or "gemini-flash-latest"
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "Sheet1").strip() or "Sheet1"
TIMEZONE = os.environ.get("TIMEZONE", "UTC").strip() or "UTC"
OCR_LANGUAGE = os.environ.get("OCR_LANGUAGE", "eng").strip() or "eng"
MODE = os.environ.get("MODE", "polling").strip().lower()
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
# Optional: a Google Drive folder (owned by you, shared with the service
# account as Editor) where receipt images are stored permanently. If not set,
# the bot falls back to Telegram's temporary file URL (which expires in ~1h).
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "").strip()

# Optional: Supabase Storage for permanent image hosting (preferred).
#   SUPABASE_URL       e.g. https://abcdxyz.supabase.co
#   SUPABASE_KEY       the service_role key (Project Settings -> API)
#   SUPABASE_BUCKET    a PUBLIC storage bucket name (default: "receipts")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "receipts").strip() or "receipts"

OCR_SPACE_URL = "https://api.ocr.space/parse/image"
MAX_OCR_BYTES = 1_000_000

GEMINI_MODEL_FALLBACKS = [
    GEMINI_MODEL,
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-2.0-flash",
]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADER_ROW = ["Name", "Telegram ID", "Vendor", "Category", "Date & Time", "Amount", "Image"]

# Save a fixed name for specific Telegram IDs (overrides their Telegram profile name).
NAME_OVERRIDES = {
    936117308: "Ashutosh Kashyap",
}

CATEGORIES = [
    "Food", "Groceries", "Travel", "Shopping", "Utilities",
    "Bills", "Rent", "EMI", "Education", "Health", "Entertainment", "Other",
]

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
    else:
        first = existing[0]
        looks_like_header = bool(first) and first[0].strip().lower() == "name"
        if looks_like_header:
            # It's a header row; correct/rename it in place if needed.
            if first != HEADER_ROW:
                worksheet.update([HEADER_ROW], "A1")
        else:
            # No header present (first row is data) -> insert one above so we
            # never overwrite a real data row.
            worksheet.insert_row(
                HEADER_ROW, 1, value_input_option="USER_ENTERED"
            )
    return worksheet


def now_string():
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") + " UTC"


def telegram_file_url(file_path):
    if not file_path:
        return ""
    if file_path.startswith("http"):
        return file_path
    return f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"


def upload_image_to_supabase(image_bytes, filename):
    """Upload the image to a public Supabase Storage bucket and return its
    permanent public URL for =IMAGE(). Returns "" on any failure (caller then
    falls back to the next option)."""
    if not (SUPABASE_URL and SUPABASE_KEY) or not image_bytes:
        return ""
    upload_url = (
        f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"
    )
    try:
        resp = httpx.post(
            upload_url,
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "apikey": SUPABASE_KEY,
                "Content-Type": "image/jpeg",
                "x-upsert": "true",
            },
            content=image_bytes,
            timeout=60,
        )
        resp.raise_for_status()
        return (
            f"{SUPABASE_URL}/storage/v1/object/public/"
            f"{SUPABASE_BUCKET}/{filename}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase upload failed, trying next option: %s", exc)
        return ""


_drive_service = {"client": None}


def _build_drive_client():
    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return _google_build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_image_to_drive(image_bytes, filename):
    """Upload the image to the configured Drive folder, make it viewable by
    anyone with the link, and return a stable image URL for =IMAGE().
    Returns "" on any failure (caller then falls back to the Telegram URL)."""
    if not DRIVE_FOLDER_ID or not image_bytes:
        return ""
    try:
        if _drive_service["client"] is None:
            _drive_service["client"] = _build_drive_client()
        drive = _drive_service["client"]
        meta = {"name": filename, "parents": [DRIVE_FOLDER_ID]}
        media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype="image/jpeg")
        created = drive.files().create(
            body=meta, media_body=media, fields="id"
        ).execute()
        file_id = created["id"]
        drive.permissions().create(
            fileId=file_id, body={"type": "anyone", "role": "reader"}
        ).execute()
        return f"https://drive.google.com/thumbnail?id={file_id}&sz=w1000"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Drive upload failed, using Telegram URL instead: %s", exc)
        return ""


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
    "You are an expense parser. The OCR text below may contain ONE OR MORE "
    "separate bills/receipts. Return a JSON object with a 'receipts' array — "
    "ONE entry per distinct bill found. If the text clearly contains multiple "
    "bills (e.g. different vendors, or several 'GRAND TOTAL' lines), return each "
    "as a separate entry. If there is no bill/amount at all, return an empty "
    "'receipts' array. Each receipt entry has these fields:\n"
    "- total: the final total amount paid as a NUMBER ONLY, with no currency "
    "symbol or words. Use only digits and an optional decimal point "
    "(e.g. '1529.00', NOT 'Rs. 1529.00'). IMPORTANT: this text comes from OCR, "
    "which often misreads the rupee sign 'Rs'/'₹' next to the total as an extra "
    "leading digit (usually a '7'), giving a grand total with one extra digit "
    "at the front. Sanity-check the grand total against the subtotal plus taxes, "
    "or the sum of the line-item amounts. If the printed grand total does not "
    "match but dropping a spurious leading digit makes it match (e.g. a printed "
    "'7724.50' where subtotal 690 + taxes 34.50 = 724.50), return the corrected, "
    "consistent value ('724.50'). Empty string if none.\n"
    "- category: choose exactly ONE that best fits, from this list: "
    + ", ".join(CATEGORIES) + ". If unsure, use 'Other'.\n"
    "- vendor: the shop / restaurant / store / company name (usually printed at "
    "the top of the bill). Empty string if not shown or it is a plain note.\n"
    "- date: the date the money was spent (the bill / transaction date shown "
    "in the text), formatted as 'YYYY-MM-DD'. If a time is also shown, use "
    "'YYYY-MM-DD HH:MM' in 24-hour time. If no date is present, empty string.\n\n"
    "TEXT:\n"
)


def clean_amount(value):
    """Return the plain numeric amount as a string (no currency symbol/words,
    no thousands separators). '' if no number is found."""
    if not value:
        return ""
    text = str(value).replace(",", "")          # drop thousands separators
    match = re.search(r"\d+(?:\.\d+)?", text)    # first number, optional decimals
    return match.group(0) if match else ""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "receipts": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "total": {"type": "STRING"},
                    "category": {"type": "STRING"},
                    "vendor": {"type": "STRING"},
                    "date": {"type": "STRING"},
                },
                "required": ["total", "category", "vendor", "date"],
            },
        },
    },
    "required": ["receipts"],
}


async def extract_fields(text):
    """Return a LIST of receipts found in the text. Each item is
    {total, category, vendor, date}. Entries without a valid amount are dropped.
    Empty list if nothing found."""
    if not text.strip():
        return []

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
                parsed = json.loads(raw)
                _working_model["name"] = model
                out = []
                for r in parsed.get("receipts", []):
                    total = clean_amount(r.get("total", ""))
                    if not total:
                        continue  # skip bills with no amount
                    out.append({
                        "total": total,
                        "category": str(r.get("category", "")).strip() or "Other",
                        "vendor": str(r.get("vendor", "")).strip(),
                        "date": str(r.get("date", "")).strip(),
                    })
                return out
            except Exception as exc:
                last_err = str(exc)
                continue

    logger.warning("Gemini extraction failed: %s", last_err)
    return []


def save_row(worksheet, name, tg_id, vendor, category, spent_at, total, image_cell):
    worksheet.append_row(
        [name, str(tg_id), vendor, category, spent_at, total, image_cell],
        value_input_option="USER_ENTERED",
    )


def summary_reply(vendor, category, total, has_image):
    lines = ["✅ Saved to your sheet!"]
    if vendor:
        lines.append(f"🏪 Vendor: {vendor}")
    if category:
        lines.append(f"🏷️ Category: {category}")
    if total:
        lines.append(f"💰 Total: {total}")
    if has_image:
        lines.append("🖼️ Image saved (view it in the sheet).")
    return "\n".join(lines)


def multi_summary(receipts, has_image):
    """Reply for one OR many receipts saved from a single image."""
    if len(receipts) == 1:
        r = receipts[0]
        return summary_reply(r["vendor"], r["category"], r["total"], has_image)
    lines = [f"✅ Saved {len(receipts)} receipts to your sheet!"]
    for i, r in enumerate(receipts, 1):
        vendor = r["vendor"] or "—"
        lines.append(f"{i}. {vendor} — ₹{r['total']} ({r['category']})")
    if has_image:
        lines.append("🖼️ Image saved (view it in the sheet).")
    return "\n".join(lines)


async def start(update, context):
    await update.message.reply_text(
        "Hi! I'm your bill logger.\n\n"
        "📸 Send me a *photo of a bill / receipt* and I'll save it to your "
        "Google Sheet with the vendor, category, date, total, and the image.\n\n"
        "Note: only *photos* are saved — text messages are not logged.",
        parse_mode="Markdown",
    )


async def handle_text(update, context):
    # Text messages are NOT saved. Only receipt/bill photos are logged.
    await update.message.reply_text(
        "⚠️ Invalid. Please send a *photo of a bill / receipt* — only images "
        "are saved to the sheet, not text messages.",
        parse_mode="Markdown",
    )


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

    # Prefer permanent cloud storage (Supabase, then Drive); if neither is
    # configured, fall back to Telegram's temporary URL (expires in ~1h).
    filename = f"{user.id}_{tg_file.file_unique_id}.jpg"
    img_url = (
        upload_image_to_supabase(image_bytes, filename)
        or upload_image_to_drive(image_bytes, filename)
        or telegram_file_url(tg_file.file_path)
    )
    # A clickable link that opens the full photo when clicked.
    image_cell = f'=HYPERLINK("{img_url}","📷 View Photo")' if img_url else ""

    try:
        raw_text = await ocr_image(image_bytes)
    except Exception:
        logger.exception("OCR failed")
        raw_text = ""

    receipts = await extract_fields(raw_text) if raw_text else []

    # No bill amount found in any receipt -> invalid, do NOT save.
    if not receipts:
        await update.message.reply_text(
            "⚠️ Invalid — no bill amount found in this image. Please send a "
            "clear photo of a bill/receipt that shows the total amount."
        )
        return

    display_name = NAME_OVERRIDES.get(user.id, user.full_name)

    try:
        for r in receipts:
            spent_at = r.get("date") or now_string()
            save_row(worksheet, display_name, user.id,
                     r["vendor"], r["category"], spent_at,
                     r["total"], image_cell)
    except Exception as exc:
        logger.exception("Failed to save row")
        await update.message.reply_text(
            f"I read the image but couldn't save it to the sheet: {exc}"
        )
        return

    await update.message.reply_text(multi_summary(receipts, bool(image_cell)))


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
