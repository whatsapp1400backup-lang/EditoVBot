# -*- coding: utf-8 -*-
"""
Edito_VBot — ربات تبدیل ویدیو برای آپارات
@Edito_VBot

کار ربات:
  کاربر ویدیو می‌فرستد (هر فرمتی که تلگرام قبول کند) و ربات خروجی
  تمیز و آماده‌ی آپارات تحویل می‌دهد:
    • فرمت MP4 — کدک H.264 برای تصویر + AAC برای صدا
    • معمولی: 16:9 با رزولوشن 1920x1080 (Full HD)
    • شورتس:  9:16 عمودی با رزولوشن 720x1280
    • فشرده:  1280x720 با حجم کمتر
    • حجم خروجی همیشه زیر ۱/۵ گیگابایت (سقف حساب معمولی آپارات)

چرا Bot API (نه Telethon)؟
  اتصال مستقیم MTProto از این سرور بسته است (تلاش با Telethon هنگام
  ساخت کلید احراز هویت قطع می‌شود)، ولی فراخوانی HTTPS به
  api.telegram.org بدون مشکل کار می‌کند. پس ربات با
  python-telegram-bot نسخه ۲۱ و polling روی Bot API اجرا می‌شود.

⚠️ محدودیت صادقانه‌ی Bot API:
  تلگرام به ربات‌ها فقط اجازه می‌دهد فایل تا حدود ۲۰ مگابایت را با
  getFile دانلود کنند. ویدیوی بزرگ‌تر از ۲۰ مگابایت دانلود نمی‌شود و
  ربات به‌صراحت همین را به کاربر می‌گوید. برای ویدیوی بزرگ‌تر باید
  اول فشرده‌اش کرد یا تکه‌تکه فرستاد.

متغیرهای محیطی (هیچ توکنی داخل کد نیست):
  BOT_TOKEN        توکن ربات از BotFather (اجباری)
  MAX_CONCURRENT   تعداد تبدیل هم‌زمان (پیش‌فرض 1)
  TMP_DIR          پوشه‌ی فایل‌های موقت (پیش‌فرض ./tmp)
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------- تنظیمات
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "1") or 1)
TMP_DIR = Path(os.environ.get("TMP_DIR", "./tmp"))

# سقف‌های آپارات و Bot API تلگرام (بایت)
APARAT_MAX_BYTES = int(1.5 * 1024 ** 3)      # ۱/۵ گیگابایت — سقف امن آپارات
BOT_API_MAX_BYTES = 20 * 1024 * 1024         # ۲۰ مگابایت — سقف دانلود ربات در Bot API
SHORTS_MAX_SECONDS = 5 * 60                  # شورتس از گالری: حداکثر ۵ دقیقه

if not BOT_TOKEN:
    print("خطا: متغیر BOT_TOKEN را تنظیم کنید.", file=sys.stderr)
    sys.exit(1)

TMP_DIR.mkdir(parents=True, exist_ok=True)

semaphore = asyncio.Semaphore(MAX_CONCURRENT)

# وضعیت هر کاربر: {user_id: {"mode": "normal"|"shorts"|None, "job": {...}}}
users: dict[int, dict] = {}


# ---------------------------------------------------------------- ابزارها
def fa_size(n: int) -> str:
    """نمایش حجم به فارسی: بایت / کیلوبایت / مگابایت / گیگابایت."""
    for unit in ("بایت", "کیلوبایت", "مگابایت", "گیگابایت"):
        if n < 1024 or unit == "گیگابایت":
            return f"{n:.1f} {unit}" if unit != "بایت" else f"{n} بایت"
        n /= 1024
    return f"{n:.1f} گیگابایت"


def fa_time(sec: float) -> str:
    sec = int(round(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def probe(path: Path) -> dict:
    """اطلاعات ویدیو با ffprobe: مدت، عرض، ارتفاع، حجم."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    data = json.loads(out.stdout or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    fmt = data.get("format", {})
    return {
        "duration": float(fmt.get("duration") or video.get("duration") or 0),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "size": int(fmt.get("size") or path.stat().st_size),
    }


def build_filter(target_w: int, target_h: int) -> str:
    """اسکیل + پد به ابعاد دقیق هدف، بدون کشیدگی تصویر."""
    return (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
        f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,"
        "format=yuv420p"
    )


PROFILES = {
    # معمولی آپارات: 16:9 — 1080p
    "normal": {
        "label": "🎬 معمولی آپارات — 1080p ‏(16:9)",
        "w": 1920, "h": 1080, "crf": 23, "audio": "128k", "fps": 30,
    },
    # شورتس آپارات: 9:16 عمودی — 720p
    "shorts": {
        "label": "📱 شورتس آپارات — 720p ‏(9:16)",
        "w": 720, "h": 1280, "crf": 23, "audio": "128k", "fps": 30,
    },
    # فشرده: حجم کمتر با کیفیت قابل قبول
    "compact": {
        "label": "🗜️ فشرده کم‌حجم — 720p ‏(16:9)",
        "w": 1280, "h": 720, "crf": 28, "audio": "96k", "fps": 30,
    },
}


def convert(src: Path, dst: Path, profile: dict, crf: int | None = None) -> None:
    """تبدیل با ffmpeg به MP4 استاندارد آپارات."""
    crf = profile["crf"] if crf is None else crf
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-vf", build_filter(profile["w"], profile["h"]),
        "-r", str(profile["fps"]),
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", profile["audio"],
        "-movflags", "+faststart",
        "-f", "mp4", str(dst),
    ]
    subprocess.run(cmd, check=True, timeout=7200)


def convert_safe(src: Path, dst: Path, profile: dict) -> Path:
    """تبدیل + اگر خروجی از سقف آپارات بزرگ‌تر شد، دوباره با CRF بالاتر."""
    convert(src, dst, profile)
    if dst.stat().st_size > APARAT_MAX_BYTES:
        # مرحله‌ی دوم: فشرده‌تر تا زیر سقف آپارات برود
        tmp2 = dst.with_suffix(".small.mp4")
        convert(src, tmp2, profile, profile["crf"] + 6)
        if tmp2.stat().st_size < dst.stat().st_size:
            tmp2.replace(dst)
        else:
            tmp2.unlink(missing_ok=True)
    return dst


def main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 تبدیل معمولی آپارات (16:9)", callback_data="mode:normal")],
        [InlineKeyboardButton("📱 تبدیل شورتس آپارات (9:16)", callback_data="mode:shorts")],
        [InlineKeyboardButton("ℹ️ راهنما", callback_data="help")],
    ])


START_TEXT = """سلام! 👋 من **Edito** هستم — ربات تبدیل ویدیو برای آپارات 🎬

ویدیوت رو بفرست، من خروجی تمیز و آماده‌ی آپارات تحویل می‌دم:
✅ فرمت MP4 — کدک H.264 + صدای AAC
✅ معمولی: 1920×1080 ‏(16:9)
✅ شورتس: 720×1280 ‏(9:16)
✅ حجم زیر ۱/۵ گیگابایت (سقف حساب معمولی آپارات)

⚠️ ویدیوی ورودی باید حداکثر ۲۰ مگابایت باشد (محدودیت تلگرام برای ربات‌ها).

اول حالت رو انتخاب کن، یا مستقیم ویدیو رو بفرست 👇"""

HELP_TEXT = """ℹ️ **راهنمای Edito**

1️⃣ ویدیو را بفرست (هر فرمتی — MP4 ،MKV ،MOV ،AVI و...).
2️⃣ از دکمه‌ها حالت خروجی را انتخاب کن:
   • 🎬 **معمولی** — 1080p ‏(16:9) برای ویدیوی عادی آپارات
   • 📱 **شورتس** — 720p ‏(9:16) برای ویدیوی عمودی؛ حداکثر ۵ دقیقه
   • 🗜️ **فشرده** — 720p ‏(16:9) با حجم کمتر
3️⃣ صبر کن تا تبدیل تمام شود؛ فایل MP4 آماده‌ی آپارات را تحویل می‌گیری.

📌 **چرا ویدیو در آپارات رد می‌شد؟**
• حجم بالای ۱/۵ گیگابایت (حساب معمولی) یا ۳ گیگابایت
• فرمت/کدک غیراستاندارد — آپارات MP4 با H.264 و AAC می‌خواهد
• ابعاد غیراستاندارد — خروجی من دقیقاً 1920×1080 یا 720×1280 است

⚠️ **محدودیت مهم:** تلگرام به ربات‌ها فقط اجازه‌ی دانلود فایل تا
۲۰ مگابایت را می‌دهد. اگر ویدیوت بزرگ‌تر است، اول فشرده‌اش کن
(مثلاً با همین ربات؟ نه — با یک اپ فشرده‌سازی ویدیو) یا تکه‌تکه بفرست.
"""


# ---------------------------------------------------------------- هندلرها
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    users.setdefault(uid, {"mode": None, "job": None})
    text = HELP_TEXT if update.message.text.startswith("/help") else START_TEXT
    await update.message.reply_text(
        text, reply_markup=main_menu_markup(), parse_mode="Markdown"
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    uid = update.effective_user.id
    users.setdefault(uid, {"mode": None, "job": None})

    if data == "help":
        await query.edit_message_text(HELP_TEXT, reply_markup=main_menu_markup(), parse_mode="Markdown")
        return

    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        users[uid]["mode"] = mode
        label = {"normal": "معمولی (16:9)", "shorts": "شورتس (9:16)"}.get(mode, mode)
        await query.edit_message_text(
            f"✅ حالت **{label}** انتخاب شد.\nحالا ویدیوت رو بفرست 🎬",
            reply_markup=main_menu_markup(), parse_mode="Markdown",
        )
        return

    if data.startswith("convert:"):
        _, job_id, mode = data.split(":")
        job = users[uid].get("job")
        if not job or job["id"] != job_id or not Path(job["src"]).exists():
            await query.answer("⚠️ ویدیو پیدا نشد؛ دوباره بفرست.", show_alert=True)
            return
        await query.edit_message_text("⏳ در حال تبدیل ویدیو... لطفاً صبر کن 🎬")
        asyncio.create_task(run_convert(query, uid, job, mode))
        return


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    uid = update.effective_user.id
    users.setdefault(uid, {"mode": None, "job": None})

    media = msg.video or msg.document
    size = getattr(media, "file_size", 0) or 0

    # سقف واقعی Bot API: ربات فقط تا ~۲۰ مگابایت می‌تواند دانلود کند
    if size > BOT_API_MAX_BYTES:
        await msg.reply_text(
            "⚠️ حجم این ویدیو بیشتر از ۲۰ مگابایت است.\n\n"
            "تلگرام به ربات‌ها فقط اجازه می‌دهد فایل تا ۲۰ مگابایت را دانلود "
            "کنند، پس نمی‌توانم این ویدیو را بگیرم.\n\n"
            "لطفاً ویدیو را اول فشرده کن (با یک اپ فشرده‌سازی ویدیو) یا "
            "تکه‌تکه بفرست تا زیر ۲۰ مگابایت باشد، بعد دوباره بفرست 🎬"
        )
        return

    job_id = uuid.uuid4().hex[:8]
    work = TMP_DIR / f"{uid}_{job_id}"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "input.mp4"
    status = await msg.reply_text(
        f"📥 در حال دانلود ویدیو... (‏{fa_size(size)}‏)\nلطفاً صبر کن ⏳"
    )

    try:
        tg_file = await context.bot.get_file(media.file_id)
        await tg_file.download_to_drive(str(src))
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        await status.edit_text(
            "❌ دانلود ناموفق بود.\n"
            "اگر حجم ویدیو نزدیک ۲۰ مگابایت است، نسخه‌ی کوچک‌تری بفرست.\n"
            f"`{e}`",
            parse_mode="Markdown",
        )
        return

    # بررسی مشخصات ویدیو
    try:
        info = await asyncio.get_running_loop().run_in_executor(None, probe, src)
    except Exception:
        info = {"duration": 0, "width": 0, "height": 0, "size": src.stat().st_size}

    users[uid]["job"] = {"id": job_id, "src": str(src), "work": str(work), "info": info}

    warn = ""
    if info["duration"] > SHORTS_MAX_SECONDS:
        warn = "\n⚠️ مدت ویدیو بیشتر از ۵ دقیقه است؛ برای شورتس آپارات مناسب نیست."

    await status.edit_text(
        "✅ ویدیو دریافت شد!\n\n"
        f"📐 ابعاد: {info['width']}×{info['height']}\n"
        f"⏱ مدت: {fa_time(info['duration'])}\n"
        f"💾 حجم: {fa_size(info['size'])}{warn}\n\n"
        "حالت خروجی را انتخاب کن 👇",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 معمولی آپارات — 1080p ‏(16:9)", callback_data=f"convert:{job_id}:normal")],
            [InlineKeyboardButton("📱 شورتس آپارات — 720p ‏(9:16)", callback_data=f"convert:{job_id}:shorts")],
            [InlineKeyboardButton("🗜️ فشرده کم‌حجم — 720p ‏(16:9)", callback_data=f"convert:{job_id}:compact")],
        ]),
    )


async def run_convert(query, uid: int, job: dict, mode: str) -> None:
    profile = PROFILES[mode]
    work = Path(job["work"])
    src = Path(job["src"])
    dst = work / f"aparat_{mode}.mp4"

    try:
        async with semaphore:
            loop = asyncio.get_running_loop()
            out = await loop.run_in_executor(None, convert_safe, src, dst, profile)
        info = await asyncio.get_running_loop().run_in_executor(None, probe, out)
        caption = (
            "✅ آماده‌ی آپارات! 🎬\n\n"
            f"📐 ابعاد: {info['width']}×{info['height']}\n"
            f"⏱ مدت: {fa_time(info['duration'])}\n"
            f"💾 حجم: {fa_size(info['size'])}\n"
            "🎞 فرمت: MP4 ‏(H.264 + AAC)‏\n\n"
            "حالا می‌توانی مستقیم در آپارات آپلودش کنی 🚀"
        )
        await query.edit_message_text("📤 در حال ارسال فایل آماده... 🚀")
        with open(out, "rb") as f:
            await query.message.reply_video(video=f, caption=caption, filename=f"aparat_{mode}.mp4")
        await query.message.delete()
    except subprocess.CalledProcessError:
        await query.edit_message_text(
            "❌ تبدیل ناموفق بود؛ فرمت ویدیو پشتیبانی نمی‌شود.\n"
            "ویدیو را با فرمت دیگری (مثلاً MP4) بفرست."
        )
    except Exception as e:
        try:
            await query.edit_message_text(f"❌ خطایی رخ داد:\n`{e}`\nدوباره تلاش کن.", parse_mode="Markdown")
        except Exception:
            pass
    finally:
        shutil.rmtree(work, ignore_errors=True)
        users[uid]["job"] = None


async def on_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پیام‌های غیر ویدیویی — راهنمایی کوتاه."""
    if update.message.video or update.message.document:
        return
    await update.message.reply_text(
        "🎬 لطفاً یک ویدیو بفرست تا برای آپارات آماده‌اش کنم.\n"
        "برای راهنما /help را بزن.",
        reply_markup=main_menu_markup(),
    )


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], on_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_other))
    print("Edito_VBot در حال اجرا... (@Edito_VBot)")
    app.run_polling()


if __name__ == "__main__":
    main()
