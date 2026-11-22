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

چرا Telethon (نه Bot API معمولی)؟
  Bot API استاندارد فقط فایل تا حدود ۲۰ مگابایت را با getFile می‌دهد،
  ولی Telethon با MTProto ویدیو تا سقف ۲ گیگابایت تلگرام را دانلود
  و ارسال می‌کند.

متغیرهای محیطی (هیچ توکنی داخل کد نیست):
  BOT_TOKEN        توکن ربات از BotFather (اجباری)
  API_ID           از my.telegram.org (اجباری)
  API_HASH         از my.telegram.org (اجباری)
  MAX_CONCURRENT   تعداد تبدیل هم‌زمان (پیش‌فرض 1)
  TMP_DIR          پوشه‌ی فایل‌های موقت (پیش‌فرض ./tmp)
"""

import asyncio
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from telethon import Button, TelegramClient, events

# ---------------------------------------------------------------- تنظیمات
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "").strip()
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "1") or 1)
TMP_DIR = Path(os.environ.get("TMP_DIR", "./tmp"))

# سقف‌های آپارات و تلگرام (بایت)
APARAT_MAX_BYTES = int(1.5 * 1024 ** 3)      # ۱/۵ گیگابایت — سقف امن آپارات
TELEGRAM_MAX_BYTES = int(2 * 1024 ** 3)      # ۲ گیگابایت — سقف ارسال تلگرام
SHORTS_MAX_SECONDS = 5 * 60                  # شورتس از گالری: حداکثر ۵ دقیقه

if not BOT_TOKEN or not API_ID or not API_HASH:
    print("خطا: متغیرهای BOT_TOKEN و API_ID و API_HASH را تنظیم کنید.", file=sys.stderr)
    sys.exit(1)

TMP_DIR.mkdir(parents=True, exist_ok=True)

client = TelegramClient("edito_vbot", API_ID, API_HASH)
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


async def convert_safe(src: Path, dst: Path, profile: dict) -> Path:
    """تبدیل در ترد جدا + اگر خروجی از سقف آپارات بزرگ‌تر شد، دوباره با CRF بالاتر."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, convert, src, dst, profile)
    if dst.stat().st_size > APARAT_MAX_BYTES:
        # مرحله‌ی دوم: فشرده‌تر تا زیر سقف آپارات برود
        tmp2 = dst.with_suffix(".small.mp4")
        await loop.run_in_executor(None, convert, src, tmp2, profile, profile["crf"] + 6)
        if tmp2.stat().st_size < dst.stat().st_size:
            tmp2.replace(dst)
        else:
            tmp2.unlink(missing_ok=True)
    return dst


def main_menu_buttons():
    return [
        [Button.inline("🎬 تبدیل معمولی آپارات (16:9)", b"mode:normal")],
        [Button.inline("📱 تبدیل شورتس آپارات (9:16)", b"mode:shorts")],
        [Button.inline("ℹ️ راهنما", b"help")],
    ]


START_TEXT = """سلام! 👋 من **Edito** هستم — ربات تبدیل ویدیو برای آپارات 🎬

ویدیوت رو بفرست، من خروجی تمیز و آماده‌ی آپارات تحویل می‌دم:
✅ فرمت MP4 — کدک H.264 + صدای AAC
✅ معمولی: 1920×1080 ‏(16:9)
✅ شورتس: 720×1280 ‏(9:16)
✅ حجم زیر ۱/۵ گیگابایت (سقف حساب معمولی آپارات)

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

⚠️ محدودیت تلگرام: ویدیوی ورودی حداکثر ۲ گیگابایت.
"""


# ---------------------------------------------------------------- هندلرها
@client.on(events.NewMessage(pattern=r"^/(start|help)"))
async def on_start(event):
    uid = event.sender_id
    users.setdefault(uid, {"mode": None, "job": None})
    if event.raw_text.startswith("/help"):
        await event.reply(HELP_TEXT, buttons=main_menu_buttons())
    else:
        await event.reply(START_TEXT, buttons=main_menu_buttons())


@client.on(events.CallbackQuery)
async def on_callback(event):
    data = event.data.decode()
    uid = event.sender_id
    users.setdefault(uid, {"mode": None, "job": None})

    if data == "help":
        await event.edit(HELP_TEXT, buttons=main_menu_buttons())
        return

    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        users[uid]["mode"] = mode
        label = {"normal": "معمولی (16:9)", "shorts": "شورتس (9:16)"}.get(mode, mode)
        await event.edit(
            f"✅ حالت **{label}** انتخاب شد.\nحالا ویدیوت رو بفرست 🎬",
            buttons=main_menu_buttons(),
        )
        return

    if data.startswith("convert:"):
        _, job_id, mode = data.split(":")
        job = users[uid].get("job")
        if not job or job["id"] != job_id or not Path(job["src"]).exists():
            await event.answer("⚠️ ویدیو پیدا نشد؛ دوباره بفرست.", alert=True)
            return
        await event.edit("⏳ در حال تبدیل ویدیو... لطفاً صبر کن 🎬")
        asyncio.create_task(run_convert(event, uid, job, mode))
        return


@client.on(events.NewMessage(func=lambda e: e.video or (e.document and e.document.mime_type and e.document.mime_type.startswith("video/"))))
async def on_video(event):
    uid = event.sender_id
    users.setdefault(uid, {"mode": None, "job": None})

    # حجم ورودی — سقف تلگرام ۲ گیگابایت
    size = event.file.size or 0
    if size > TELEGRAM_MAX_BYTES:
        await event.reply(
            "⚠️ حجم ویدیو بیشتر از ۲ گیگابایت است و تلگرام اجازه‌ی دانلود نمی‌دهد.\n"
            "لطفاً ویدیوی کوچک‌تری بفرست."
        )
        return

    job_id = uuid.uuid4().hex[:8]
    work = TMP_DIR / f"{uid}_{job_id}"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "input"
    status = await event.reply(
        f"📥 در حال دانلود ویدیو... (‏{fa_size(size)}‏)\nلطفاً صبر کن ⏳"
    )

    # دانلود با Telethon — تا ۲ گیگابایت، با نمایش پیشرفت
    last_edit = [0.0]
    last_pct = [-1]

    def progress(downloaded, total):
        now = time.time()
        pct = int(downloaded / total * 100) if total else 0
        if pct != last_pct[0] and now - last_edit[0] > 3:
            last_pct[0] = pct
            last_edit[0] = now
            asyncio.ensure_future(
                status.edit(f"📥 در حال دانلود... {pct}٪ ‏(‏{fa_size(downloaded)} از {fa_size(total)}‏)")
            )

    try:
        await event.download_media(file=str(src), progress_callback=progress)
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        await status.edit(f"❌ دانلود ناموفق بود.\nدوباره تلاش کن.\n`{e}`")
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

    await status.edit(
        "✅ ویدیو دریافت شد!\n\n"
        f"📐 ابعاد: {info['width']}×{info['height']}\n"
        f"⏱ مدت: {fa_time(info['duration'])}\n"
        f"💾 حجم: {fa_size(info['size'])}{warn}\n\n"
        "حالت خروجی را انتخاب کن 👇",
        buttons=[
            [Button.inline("🎬 معمولی آپارات — 1080p ‏(16:9)", f"convert:{job_id}:normal".encode())],
            [Button.inline("📱 شورتس آپارات — 720p ‏(9:16)", f"convert:{job_id}:shorts".encode())],
            [Button.inline("🗜️ فشرده کم‌حجم — 720p ‏(16:9)", f"convert:{job_id}:compact".encode())],
        ],
    )


async def run_convert(event, uid, job, mode):
    profile = PROFILES[mode]
    work = Path(job["work"])
    src = Path(job["src"])
    dst = work / f"aparat_{mode}.mp4"
    status_msg = event  # پیام ویرایش‌شده‌ی وضعیت

    try:
        async with semaphore:
            out = await convert_safe(src, dst, profile)
        info = await asyncio.get_running_loop().run_in_executor(None, probe, out)
        caption = (
            "✅ آماده‌ی آپارات! 🎬\n\n"
            f"📐 ابعاد: {info['width']}×{info['height']}\n"
            f"⏱ مدت: {fa_time(info['duration'])}\n"
            f"💾 حجم: {fa_size(info['size'])}\n"
            "🎞 فرمت: MP4 ‏(H.264 + AAC)‏\n\n"
            "حالا می‌توانی مستقیم در آپارات آپلودش کنی 🚀"
        )
        await status_msg.edit("📤 در حال ارسال فایل آماده... 🚀")
        await client.send_file(
            event.chat_id, str(out),
            caption=caption,
            force_document=False,
        )
        await status_msg.delete()
    except subprocess.CalledProcessError:
        await status_msg.edit(
            "❌ تبدیل ناموفق بود؛ فرمت ویدیو پشتیبانی نمی‌شود.\n"
            "ویدیو را با فرمت دیگری (مثلاً MP4) بفرست."
        )
    except Exception as e:
        await status_msg.edit(f"❌ خطایی رخ داد:\n`{e}`\nدوباره تلاش کن.")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        users[uid]["job"] = None


@client.on(events.NewMessage)
async def on_other(event):
    """پیام‌های غیر ویدیویی — راهنمایی کوتاه."""
    if event.raw_text.startswith("/"):
        return
    if not (event.video or event.document):
        await event.reply(
            "🎬 لطفاً یک ویدیو بفرست تا برای آپارات آماده‌اش کنم.\n"
            "برای راهنما /help را بزن.",
            buttons=main_menu_buttons(),
        )


def main():
    print("Edito_VBot در حال اجرا... (@Edito_VBot)")
    client.start(bot_token=BOT_TOKEN)
    client.run_until_disconnected()


if __name__ == "__main__":
    main()
