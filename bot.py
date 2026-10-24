"""Watermark bot: koi bhi image bhejo, fixed corner mein watermark laga ke wapas milegi."""
import asyncio
import io
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("wmbot")

# ================================================================ CONFIG (env se badal sakte ho)
BOT_TOKEN = os.environ["BOT_TOKEN"]
WM_FILE = os.getenv("WATERMARK_FILE", "watermark.png")      # logo (PNG, transparent best). Na mile to text use hoga
WM_TEXT = os.getenv("WATERMARK_TEXT")                       # text (logo ke upar). Logo ho aur ye khali ho to sirf logo
TEXT_RATIO = float(os.getenv("WATERMARK_TEXT_RATIO", "0.85"))  # text ki chaudai = logo ki chaudai ka itna hissa
GAP = float(os.getenv("WATERMARK_GAP", "0.04"))             # text aur logo ke beech ki jagah
POSITION = os.getenv("WATERMARK_POS", "bottom-right")       # top-left | top-right | bottom-left | bottom-right
SCALE = float(os.getenv("WATERMARK_SCALE", "0.22"))         # watermark ki chaudai = image ki chaudai ka itna hissa
OPACITY = float(os.getenv("WATERMARK_OPACITY", "0.85"))     # 0.1 (halka) se 1.0 (poora saaf)
MARGIN = float(os.getenv("WATERMARK_MARGIN", "0.02"))       # kone se doori (image ki chaudai ka hissa)
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USERS", "").replace(",", " ").split() if x.isdigit()}
# ALLOWED_USERS khali = sab use kar sakte hain. Sirf apne liye chahiye to apna Telegram ID daalo.


# ================================================================ WATERMARK
def text_mark(text):
    font = ImageFont.load_default(size=120)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    box = probe.textbbox((0, 0), text, font=font, stroke_width=5)
    w, h = box[2] - box[0], box[3] - box[1]
    img = Image.new("RGBA", (w + 24, h + 24), (0, 0, 0, 0))
    ImageDraw.Draw(img).text(
        (12 - box[0], 12 - box[1]), text, font=font,
        fill=(255, 255, 255, 255), stroke_width=5, stroke_fill=(0, 0, 0, 255),
    )
    return img


def build_mark():
    """Text upar, uske neeche logo. Sirf logo ya sirf text bhi chalega."""
    logo = Image.open(WM_FILE).convert("RGBA") if os.path.exists(WM_FILE) else None
    text = WM_TEXT if WM_TEXT is not None else ("" if logo else "@YourChannel")
    text = text.strip()
    if not text:
        return logo if logo else text_mark("@YourChannel")
    t = text_mark(text)
    if not logo:
        return t

    W = 600
    logo = logo.resize((W, max(1, int(logo.height * W / logo.width))), Image.LANCZOS)
    tw = int(W * TEXT_RATIO)
    th = int(t.height * tw / t.width)
    cap = int(W * 0.22)                      # bahut chhota text ho to bahut bada na dikhe
    if th > cap:
        tw, th = int(tw * cap / th), cap
    t = t.resize((max(1, tw), max(1, th)), Image.LANCZOS)
    gap = int(W * GAP)

    canvas = Image.new("RGBA", (W, th + gap + logo.height), (0, 0, 0, 0))
    canvas.alpha_composite(t, ((W - t.width) // 2, 0))
    canvas.alpha_composite(logo, (0, th + gap))
    return canvas


BASE_MARK = build_mark()


def apply_watermark(data: bytes):
    """image bytes -> (watermarked bytes, extension)"""
    src = Image.open(io.BytesIO(data))
    fmt = (src.format or "JPEG").upper()
    img = ImageOps.exif_transpose(src).convert("RGBA")
    W, H = img.size

    mw = max(40, int(W * SCALE))
    mh = max(1, int(BASE_MARK.height * mw / BASE_MARK.width))
    mark = BASE_MARK.resize((mw, mh), Image.LANCZOS)
    alpha = mark.getchannel("A").point(lambda p: int(p * OPACITY))
    mark.putalpha(alpha)

    m = int(W * MARGIN)
    x = m if "left" in POSITION else W - mw - m
    y = m if "top" in POSITION else H - mh - m
    img.alpha_composite(mark, (max(0, x), max(0, y)))

    out = io.BytesIO()
    if fmt == "PNG":
        img.save(out, "PNG")
        return out.getvalue(), "png"
    img.convert("RGB").save(out, "JPEG", quality=95, subsampling=0)
    return out.getvalue(), "jpg"


# ================================================================ HANDLERS
def allowed(update: Update) -> bool:
    return not ALLOWED or update.effective_user.id in ALLOWED


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text(
        "🖼 Koi bhi image bhejo, main watermark laga ke wapas bhej dunga.\n\n"
        "💡 Full quality chahiye to image ko <b>File / Document</b> ki tarah bhejo.",
        parse_mode="HTML",
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        f = await update.message.photo[-1].get_file()
        data = bytes(await f.download_as_bytearray())
        out, _ = await asyncio.to_thread(apply_watermark, data)
        await update.message.reply_photo(io.BytesIO(out))
    except Exception as e:
        log.exception("photo fail")
        await update.message.reply_text(f"❌ Image process nahi hui: {e}")


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    doc = update.message.document
    try:
        f = await doc.get_file()
        data = bytes(await f.download_as_bytearray())
        out, ext = await asyncio.to_thread(apply_watermark, data)
        base = os.path.splitext(doc.file_name or "image")[0]
        await update.message.reply_document(io.BytesIO(out), filename=f"{base}_wm.{ext}")
    except Exception as e:
        log.exception("document fail")
        await update.message.reply_text(f"❌ Image process nahi hui: {e}")


# ================================================================ MAIN
def start_health_server():
    """Render jaise free hosts ke liye (PORT set ho tabhi)."""
    port = os.getenv("PORT")
    if not port:
        return

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", int(port)), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server port %s pe chalu", port)


def main():
    start_health_server()
    # Python 3.14 mein event loop khud nahi banta, isliye pehle bana do
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE, on_document))
    log.info("Watermark bot chalu ho gaya")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
