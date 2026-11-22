# -*- coding: utf-8 -*-
"""
Edito_VBot — ابزار ویرایش ویدیو
@Edito_VBot

کاربر ویدیو می‌فرستد و نسخه‌ی آماده‌ی انتشار تحویل می‌گیرد:
  • خروجی MP4 — H.264 برای تصویر + AAC برای صدا
  • ویدیوی معمولی: 16:9 با رزولوشن 1920x1080
  • شورتس: 9:16 عمودی با رزولوشن 720x1280
  • فشرده: 1280x720 با حجم کمتر
  • کم کردن حجم: همان رزولوشن (حداکثر 1080p)، حجم کمتر بدون افت محسوس کیفیت
  • حجم خروجی زیر ۱/۵ گیگابایت نگه داشته می‌شود

امکانات دیگر:
  • دانلود ویدیو از لینک یوتیوب، ریلز اینستاگرام و آپارات
  • استخراج صدای ویدیو به MP3
  • ریلز گردی: تماشای ویدیوهای کوتاه یوتیوب شورتس و آپارات شورتس، یکی‌یکی

اجرا با python-telegram-bot و polling روی Bot API است.
محدودیت تلگرام: ربات فقط فایل تا ۲۰ مگابایت را می‌تواند دانلود کند
و فقط فایل تا ۵۰ مگابایت را می‌تواند بفرستد.

متغیرهای محیطی (هیچ توکنی داخل کد نیست):
  BOT_TOKEN        توکن ربات از BotFather (اجباری)
  MAX_CONCURRENT   تعداد تبدیل هم‌زمان (پیش‌فرض 1)
  TMP_DIR          پوشه‌ی فایل‌های موقت (پیش‌فرض ./tmp)
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
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
BOT_SEND_MAX_BYTES = 50 * 1024 * 1024        # ۵۰ مگابایت — سقف ارسال فایل ربات در Bot API
SHORTS_MAX_SECONDS = 5 * 60                  # شورتس از گالری: حداکثر ۵ دقیقه

PROFILE_PHOTO = Path(__file__).resolve().parent / (
    "media-generation-edito-vbot-profile-visible-0-558f3e77-48f5-45c9-8f5b-23ed1d7427a1.png"
)

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

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
    # کم کردن حجم: رزولوشن اصلی (حداکثر 1080p)، بدون افت محسوس کیفیت
    "slim": {
        "label": "🗜️ کم کردن حجم",
        "slim": True, "crf": 26, "preset": "slow", "audio": "96k",
    },
}


def slim_filter() -> str:
    """رزولوشن اصلی، فقط اگر بزرگ‌تر از 1080p بود کوچک‌تر می‌شود."""
    return (
        "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease,"
        "format=yuv420p"
    )


def convert(src: Path, dst: Path, profile: dict, crf: int | None = None) -> None:
    """تبدیل با ffmpeg به MP4 استاندارد آپارات."""
    crf = profile["crf"] if crf is None else crf
    if profile.get("slim"):
        cmd = [
            "ffmpeg", "-y", "-v", "error", "-i", str(src),
            "-vf", slim_filter(),
            "-c:v", "libx264", "-preset", profile.get("preset", "slow"), "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", profile["audio"],
            "-movflags", "+faststart",
            "-f", "mp4", str(dst),
        ]
        subprocess.run(cmd, check=True, timeout=7200)
        return
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


def extract_audio(src: Path, dst: Path) -> Path:
    """جدا کردن صدای ویدیو به MP3."""
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", str(src),
        "-vn", "-c:a", "libmp3lame", "-q:a", "4", str(dst),
    ]
    subprocess.run(cmd, check=True, timeout=3600)
    return dst


def http_get_json(url: str, timeout: int = 30) -> dict:
    """GET ساده با هدر مرورگر؛ خروجی JSON."""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def download_url(url: str, dst: Path, max_bytes: int = BOT_SEND_MAX_BYTES,
                 timeout: int = 600) -> Path:
    """دانلود مستقیم یک فایل؛ اگر از سقف بزرگ‌تر شد وسط راه قطع می‌شود."""
    req = urllib.request.Request(url, headers=UA)
    size = 0
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dst, "wb") as f:
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("too_big")
            f.write(chunk)
    return dst


# ---------------------------------------------------------------- دانلود از لینک
def ytdlp_download(url: str, out_template: str, timeout: int = 600) -> Path:
    """دانلود ویدیو با yt-dlp؛ خروجی MP4 حداکثر ۵۰ مگابایت.

    روی این سرور پروکسی خروجی گواهی TLS را عوض می‌کند، پس
    --no-check-certificates لازم است.
    """
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-check-certificates", "--js-runtimes", "node",
        "-f", "bv*[height<=720]+ba/b[height<=720]",
        "--max-filesize", str(BOT_SEND_MAX_BYTES),
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", out_template,
        url,
    ]
    subprocess.run(cmd, check=True, timeout=timeout,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # out_template همیشه .../video.%(ext)s است؛ فایل نهایی را پیدا کن
    for cand in Path(out_template).parent.glob("video.*"):
        if cand.suffix.lower() in (".mp4", ".webm", ".mkv") and cand.stat().st_size > 0:
            return cand
    raise FileNotFoundError("no_output")


def aparat_show(uid: str) -> dict | None:
    """مشخصات یک ویدیوی آپارات + لینک مستقیم فایل."""
    try:
        data = http_get_json(
            f"https://www.aparat.com/api/fa/v1/video/video/show/videohash/{uid}")
        attrs = data.get("data", {}).get("attributes", {})
        if attrs.get("file_link"):
            return attrs
    except Exception:
        pass
    return None


def aparat_search_shorts(perpage: int = 10, text: str = "شورتس") -> list[dict]:
    """جست‌وجوی ویدیوهای کوتاه آپارات؛ لیستی از {uid, title, duration}."""
    q = urllib.parse.quote(text)
    data = http_get_json(
        "https://www.aparat.com/api/fa/v1/video/video/search"
        f"?perpage={perpage}&text={q}")
    out = []
    for item in data.get("included", []):
        if item.get("type") != "Video":
            continue
        a = item.get("attributes", {})
        uid = a.get("uid")
        try:
            dur = int(float(a.get("duration") or 0))
        except (TypeError, ValueError):
            dur = 0
        if uid and 5 <= dur <= 300:
            out.append({"uid": uid, "title": a.get("title") or "ویدیو",
                        "duration": dur})
    return out


YT_SHORTS_CHANNELS = ["MrBeast", "zachking", "DudePerfect", "AlanBeckerTutorials"]
_yt_channel_idx = 0


def youtube_search_shorts(limit: int = 12) -> list[dict]:
    """شورتس‌های واقعی یوتیوب از تب Shorts چند کانال پرمخاطب.

    جست‌وجوی متنی یوتیوب بیشتر ویدیوهای بلند درباره‌ی شورتس برمی‌گرداند،
    ولی تب /shorts هر کانال فقط ویدیوهای کوتاه (حداکثر ۶۰ ثانیه) دارد.
    """
    global _yt_channel_idx
    for _ in range(len(YT_SHORTS_CHANNELS)):
        ch = YT_SHORTS_CHANNELS[_yt_channel_idx % len(YT_SHORTS_CHANNELS)]
        _yt_channel_idx += 1
        try:
            cmd = [
                sys.executable, "-m", "yt_dlp",
                "--no-check-certificates", "--js-runtimes", "node",
                "--flat-playlist", "--playlist-end", str(limit),
                "-J", f"https://www.youtube.com/@{ch}/shorts",
            ]
            out = subprocess.run(cmd, check=True, timeout=120,
                                 capture_output=True, text=True)
            data = json.loads(out.stdout or "{}")
            res = [{"id": e["id"], "title": e.get("title") or "ویدیو",
                    "duration": 0}
                   for e in data.get("entries", []) if e and e.get("id")]
            if res:
                return res
        except Exception:
            continue
    return []


def main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 ویدیوی معمولی", callback_data="mode:normal")],
        [InlineKeyboardButton("📱 شورتس", callback_data="mode:shorts")],
        [InlineKeyboardButton("🔗 دانلود از لینک", callback_data="download_link")],
        [InlineKeyboardButton("🎬 ریلز گردی", callback_data="reels")],
        [InlineKeyboardButton("ℹ️ راهنما", callback_data="help")],
    ])


def reels_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ یوتیوب شورتس", callback_data="reels:yt")],
        [InlineKeyboardButton("▶️ آپارات شورتس", callback_data="reels:aparat")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu")],
    ])


def reels_nav_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ بعدی", callback_data="reels:next")],
        [InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu")],
    ])


START_TEXT = """سلام 👋

اینجا Editoـه. ویدیوت رو بفرست، نسخه‌ی آماده‌ی انتشارش رو تحویل بگیر؛ تمیز و با کیفیت بالا.

هم ویدیوی معمولی رو پوشش می‌دم، هم شورتس. لینک یوتیوب و ریلز رو هم می‌تونم دانلود کنم، و اگه حوصله‌ت سر رفت ریلز گردی هم هست.

فقط ویدیو باید زیر ۲۰ مگابایت باشه."""

HELP_TEXT = """کافیه ویدیوت رو بفرستی؛ بقیه‌ش با من.

• ویدیوی معمولی — Full HD، برای انتشار عادی
• شورتس — عمودی، برای موبایل
• فشرده — حجم کمتر، کیفیت قابل قبول
• کم کردن حجم — ویدیو کوچیک‌تر می‌شه، کیفیتش تقریباً همون می‌مونه
• استخراج صدا — صدای ویدیو رو جدا می‌کنم و MP3 می‌دم
• دانلود از لینک — لینک یوتیوب یا ریلز رو بفرست، ویدیوش رو می‌گیرم
• ریلز گردی — ویدیوهای کوتاه رو یکی‌یکی ببین

خروجی‌ها MP4 هستن و آماده‌ی انتشارن. اگه ویدیوت بالای ۲۰ مگابایته، یه نسخه‌ی سبک‌ترش رو بفرست."""


# ---------------------------------------------------------------- هندلرها
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    users.setdefault(uid, {"mode": None, "job": None, "awaiting_link": False,
                           "reels": None})
    if PROFILE_PHOTO.exists():
        with open(PROFILE_PHOTO, "rb") as f:
            await update.message.reply_photo(
                photo=f, caption=START_TEXT, reply_markup=main_menu_markup())
    else:
        await update.message.reply_text(
            START_TEXT, reply_markup=main_menu_markup())


async def on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    users.setdefault(uid, {"mode": None, "job": None, "awaiting_link": False,
                           "reels": None})
    await update.message.reply_text(
        HELP_TEXT, reply_markup=main_menu_markup())


async def edit_status(query, text: str, markup=None, parse_mode=None) -> None:
    """ویرایش پیام وضعیت؛ برای پیام عکس/ویدیویی کپشن را عوض می‌کند."""
    try:
        await query.edit_message_text(text, reply_markup=markup,
                                      parse_mode=parse_mode)
    except Exception:
        try:
            await query.edit_message_caption(caption=text, reply_markup=markup,
                                             parse_mode=parse_mode)
        except Exception:
            pass


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    uid = update.effective_user.id
    users.setdefault(uid, {"mode": None, "job": None, "awaiting_link": False,
                           "reels": None})

    if data == "menu":
        try:
            await query.edit_message_caption(
                caption=START_TEXT, reply_markup=main_menu_markup())
        except Exception:
            await query.edit_message_text(
                START_TEXT, reply_markup=main_menu_markup())
        return

    if data == "help":
        await edit_status(query, HELP_TEXT, main_menu_markup(),
                          parse_mode="Markdown")
        return

    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        users[uid]["mode"] = mode
        label = {"normal": "ویدیوی معمولی", "shorts": "شورتس"}.get(mode, mode)
        await edit_status(
            query,
            f"باشه، {label} رو انتخاب کردی.\nحالا ویدیوت رو بفرست 🎬",
            main_menu_markup(), parse_mode="Markdown",
        )
        return

    if data == "download_link":
        users[uid]["awaiting_link"] = True
        await edit_status(
            query,
            "لینک ویدیو رو بفرست — یوتیوب، ریلز اینستاگرام یا آپارات.",
            main_menu_markup())
        return

    if data == "reels":
        await edit_status(
            query,
            "ریلز کدوم رو ببینی؟",
            reels_menu_markup())
        return

    if data in ("reels:yt", "reels:aparat"):
        source = "yt" if data == "reels:yt" else "aparat"
        users[uid]["reels"] = {"source": source, "items": [], "index": 0}
        await edit_status(query, "دارم ویدیوها رو می‌گیرم… ⏳")
        asyncio.create_task(serve_reel(query, uid, first=True))
        return

    if data == "reels:next":
        if not users[uid].get("reels"):
            await edit_status(query, "اول از ریلز گردی یه منبع رو انتخاب کن.",
                              reels_menu_markup())
            return
        await edit_status(query, "دارم بعدی رو می‌گیرم… ⏳")
        asyncio.create_task(serve_reel(query, uid))
        return

    if data.startswith("audio:"):
        _, job_id = data.split(":")
        job = users[uid].get("job")
        if not job or job["id"] != job_id or not Path(job["src"]).exists():
            await query.answer("ویدیو رو پیدا نکردم؛ دوباره بفرست.", show_alert=True)
            return
        await edit_status(query, "دارم صداش رو جدا می‌کنم… 🎵")
        asyncio.create_task(run_audio(query, uid, job))
        return

    if data.startswith("convert:"):
        _, job_id, mode = data.split(":")
        job = users[uid].get("job")
        if not job or job["id"] != job_id or not Path(job["src"]).exists():
            await query.answer("ویدیو رو پیدا نکردم؛ دوباره بفرست.", show_alert=True)
            return
        await edit_status(query, "دارم تبدیلش می‌کنم… ⏳")
        asyncio.create_task(run_convert(query, uid, job, mode))
        return


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    uid = update.effective_user.id
    users.setdefault(uid, {"mode": None, "job": None, "awaiting_link": False,
                           "reels": None})

    media = msg.video or msg.document
    size = getattr(media, "file_size", 0) or 0

    # سقف واقعی Bot API: ربات فقط تا ~۲۰ مگابایت می‌تواند دانلود کند
    if size > BOT_API_MAX_BYTES:
        await msg.reply_text(
            "حجم این ویدیو بیشتر از ۲۰ مگابایته و تلگرام اجازه نمی‌ده ربات‌ها "
            "فایل بزرگ‌تر از این رو دانلود کنن.\n\n"
            "یه نسخه‌ی سبک‌تر از ویدیو رو بفرست تا درستش کنم."
        )
        return

    job_id = uuid.uuid4().hex[:8]
    work = TMP_DIR / f"{uid}_{job_id}"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "input.mp4"
    status = await msg.reply_text(
        f"دارم ویدیوت رو می‌گیرم… ⏳"
    )

    try:
        tg_file = await context.bot.get_file(media.file_id)
        await tg_file.download_to_drive(str(src))
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        await status.edit_text(
            "دانلود نشد. یه نسخه‌ی کوچک‌تر از ویدیو رو بفرست."
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
        warn = "\nمدت ویدیو بیشتر از ۵ دقیقه‌ست؛ برای شورتس مناسب نیست."

    await status.edit_text(
        "ویدیوت رسید ✅\n\n"
        f"ابعاد: {info['width']}×{info['height']}\n"
        f"مدت: {fa_time(info['duration'])}\n"
        f"حجم: {fa_size(info['size'])}{warn}\n\n"
        "حالا بگو خروجی چی باشه:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 ویدیوی معمولی — Full HD", callback_data=f"convert:{job_id}:normal")],
            [InlineKeyboardButton("📱 شورتس — عمودی", callback_data=f"convert:{job_id}:shorts")],
            [InlineKeyboardButton("🗜️ فشرده — حجم کمتر", callback_data=f"convert:{job_id}:compact")],
            [InlineKeyboardButton("🗜️ کم کردن حجم", callback_data=f"convert:{job_id}:slim")],
            [InlineKeyboardButton("🎵 استخراج صدا", callback_data=f"audio:{job_id}")],
        ]),
    )


async def run_convert(query, uid: int, job: dict, mode: str) -> None:
    profile = PROFILES[mode]
    work = Path(job["work"])
    src = Path(job["src"])
    dst = work / f"aparat_{mode}.mp4"
    before = job.get("info", {}).get("size") or src.stat().st_size

    try:
        async with semaphore:
            loop = asyncio.get_running_loop()
            out = await loop.run_in_executor(None, convert_safe, src, dst, profile)
        info = await asyncio.get_running_loop().run_in_executor(None, probe, out)
        if mode == "slim":
            caption = (
                "آماده‌ست 🎬\n\n"
                f"قبل: {fa_size(before)}\n"
                f"بعد: {fa_size(info['size'])}\n"
                f"ابعاد: {info['width']}×{info['height']}\n"
                f"مدت: {fa_time(info['duration'])}\n"
                "فرمت: MP4\n\n"
                "حجمش کم شد، کیفیتش تقریباً همون مونده."
            )
        else:
            caption = (
                "آماده‌ست 🎬\n\n"
                f"ابعاد: {info['width']}×{info['height']}\n"
                f"مدت: {fa_time(info['duration'])}\n"
                f"حجم: {fa_size(info['size'])}\n"
                "فرمت: MP4\n\n"
                "می‌تونی مستقیم منتشرش کنی."
            )
        await edit_status(query, "دارم می‌فرستمش… 📤")
        with open(out, "rb") as f:
            await query.message.reply_video(video=f, caption=caption, filename=f"aparat_{mode}.mp4")
        await query.message.delete()
    except subprocess.CalledProcessError:
        await edit_status(query,
            "این فرمت رو نمی‌تونم تبدیل کنم؛ با MP4 بفرست."
        )
    except Exception:
        try:
            await edit_status(query, "یه مشکلی پیش اومد، دوباره بفرست.")
        except Exception:
            pass
    finally:
        shutil.rmtree(work, ignore_errors=True)
        users[uid]["job"] = None


async def run_audio(query, uid: int, job: dict) -> None:
    work = Path(job["work"])
    src = Path(job["src"])
    dst = work / "audio.mp3"
    try:
        async with semaphore:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, extract_audio, src, dst)
        with open(dst, "rb") as f:
            await query.message.reply_audio(
                audio=f, caption="صداش رو جدا کردم 🎵", filename="audio.mp3")
        await query.message.delete()
    except Exception:
        try:
            await edit_status(query, "یه مشکلی پیش اومد، دوباره بفرست.")
        except Exception:
            pass
    finally:
        shutil.rmtree(work, ignore_errors=True)
        users[uid]["job"] = None


# ---------------------------------------------------------------- دانلود از لینک
LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def looks_like_video_link(text: str) -> str | None:
    """اگر متن لینک یوتیوب / اینستاگرام / آپارات بود، خود لینک را برمی‌گرداند."""
    m = LINK_RE.search(text or "")
    if not m:
        return None
    url = m.group(0).rstrip(").,")
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(h in host for h in ("youtube.com", "youtu.be", "instagram.com",
                               "aparat.com")):
        return url
    return None


async def run_link_download(msg, uid: int, url: str) -> None:
    job_id = uuid.uuid4().hex[:8]
    work = TMP_DIR / f"{uid}_link_{job_id}"
    work.mkdir(parents=True, exist_ok=True)
    status = await msg.reply_text("دارم می‌گیرمش… ⏳")
    try:
        async with semaphore:
            loop = asyncio.get_running_loop()
            title, path = await loop.run_in_executor(
                None, download_link_sync, url, work)
        if path.stat().st_size > BOT_SEND_MAX_BYTES:
            await status.edit_text(
                "این ویدیو بزرگ‌تر از ۵۰ مگابایته؛ نمی‌تونم بفرستمش.")
            return
        with open(path, "rb") as f:
            await msg.reply_video(
                video=f, caption=title[:200] if title else "این هم ویدیوش 🎬",
                filename=path.name)
        await status.delete()
    except ValueError as e:
        if str(e) == "too_big":
            await status.edit_text(
                "این ویدیو بزرگ‌تر از ۵۰ مگابایته؛ نمی‌تونم بفرستمش.")
        else:
            await status.edit_text(
                "نشد دانلودش کنم. یه لینک دیگه رو امتحان کن.")
    except Exception:
        await status.edit_text(
            "نشد دانلودش کنم. بعضی ویدیوها، مثلاً ریلزهای خصوصی "
            "اینستاگرام، اجازه‌ی دانلود نمی‌دن. یه لینک دیگه رو امتحان کن.")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def download_link_sync(url: str, work: Path) -> tuple[str, Path]:
    """دانلود هم‌گام یک لینک؛ برمی‌گرداند (عنوان، مسیر فایل)."""
    m = re.search(r"aparat\.com/v/([A-Za-z0-9]+)", url)
    if m:
        attrs = aparat_show(m.group(1))
        if not attrs or not attrs.get("file_link"):
            raise RuntimeError("aparat_failed")
        title = attrs.get("title") or "ویدیوی آپارات"
        dst = work / "video.mp4"
        download_url(attrs["file_link"], dst)
        return title, dst
    out = work / "video.%(ext)s"
    path = ytdlp_download(url, str(out))
    title = ""
    try:
        info = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--no-check-certificates",
             "--js-runtimes", "node", "--skip-download",
             "--print", "%(title)s", url],
            capture_output=True, text=True, timeout=120)
        title = (info.stdout or "").strip()
    except Exception:
        pass
    return title, path


# ---------------------------------------------------------------- ریلز گردی
async def serve_reel(query, uid: int, first: bool = False) -> None:
    state = users[uid].get("reels")
    if not state:
        return
    source = state["source"]
    try:
        async with semaphore:
            loop = asyncio.get_running_loop()
            while True:
                if state["index"] >= len(state["items"]):
                    state["items"] = await loop.run_in_executor(
                        None, fetch_reels, source)
                    state["index"] = 0
                    if not state["items"]:
                        await edit_status(
                            query,
                            "الان چیزی برای نشون دادن پیدا نکردم؛ "
                            "یه کم دیگه دوباره امتحان کن.",
                            reels_menu_markup())
                        return
                item = state["items"][state["index"]]
                state["index"] += 1
                try:
                    work = TMP_DIR / f"{uid}_reel_{uuid.uuid4().hex[:8]}"
                    work.mkdir(parents=True, exist_ok=True)
                    title, path = await loop.run_in_executor(
                        None, download_reel_sync, source, item, work)
                    if path.stat().st_size > BOT_SEND_MAX_BYTES:
                        shutil.rmtree(work, ignore_errors=True)
                        continue
                    with open(path, "rb") as f:
                        await query.message.reply_video(
                            video=f, caption=f"{title[:200]}\n\n",
                            reply_markup=reels_nav_markup())
                    shutil.rmtree(work, ignore_errors=True)
                    try:
                        await query.message.delete()
                    except Exception:
                        pass
                    return
                except Exception:
                    shutil.rmtree(work, ignore_errors=True)
                    continue
    except Exception:
        await edit_status(query, "یه مشکلی پیش اومد؛ دوباره امتحان کن.",
                          reels_menu_markup())


def fetch_reels(source: str) -> list[dict]:
    """لیست ویدیوهای کوتاه یک منبع."""
    if source == "aparat":
        return aparat_search_shorts(perpage=10, text="شورتس")
    return youtube_search_shorts(limit=20)


def download_reel_sync(source: str, item: dict, work: Path) -> tuple[str, Path]:
    """دانلود هم‌گام یک آیتم ریلز؛ برمی‌گرداند (عنوان، مسیر فایل)."""
    if source == "aparat":
        attrs = aparat_show(item["uid"])
        if not attrs or not attrs.get("file_link"):
            raise RuntimeError("aparat_failed")
        dst = work / "video.mp4"
        download_url(attrs["file_link"], dst)
        return item.get("title") or "ویدیو", dst
    url = f"https://www.youtube.com/watch?v={item['id']}"
    out = work / "video.%(ext)s"
    path = ytdlp_download(url, str(out))
    return item.get("title") or "ویدیو", path


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    uid = update.effective_user.id
    users.setdefault(uid, {"mode": None, "job": None, "awaiting_link": False,
                           "reels": None})
    text = (msg.text or "").strip()
    url = looks_like_video_link(text)
    if users[uid].get("awaiting_link") or url:
        users[uid]["awaiting_link"] = False
        if not url:
            await msg.reply_text(
                "این لینک رو نشناختم؛ لینک یوتیوب، ریلز اینستاگرام "
                "یا آپارات بفرست.")
            return
        asyncio.create_task(run_link_download(msg, uid, url))
        return
    await msg.reply_text(
        "یه ویدیو بفرست تا آماده‌ش کنم.\nاگه سوالی داری /help رو بزن.",
        reply_markup=main_menu_markup(),
    )


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("help", on_help))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("Edito_VBot در حال اجرا... (@Edito_VBot)")
    app.run_polling()


if __name__ == "__main__":
    main()
