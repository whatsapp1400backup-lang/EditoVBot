#!/usr/bin/env bash
# اجرای Edito_VBot روی همین سرور — بدون Railway
# پیش‌نیاز: یک بار `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
set -e
cd "$(dirname "$0")"

# مهم: لیست no_proxy سیستم شامل آدرس‌های IPv6 داخل [] است که httpx
# (کتابخانه‌ی python-telegram-bot) نمی‌تواند تجزیه کند و ساخت ربات
# با خطای InvalidURL می‌شکند. پس آن را به یک لیست ساده محدود می‌کنیم.
# پروکسی خروج (http_proxy) دست نخورده می‌ماند تا اتصال HTTPS به
# api.telegram.org برقرار بماند.
export NO_PROXY="localhost,127.0.0.1"
export no_proxy="localhost,127.0.0.1"

if [ -z "$BOT_TOKEN" ]; then
  echo "خطا: متغیر BOT_TOKEN را تنظیم کنید." >&2
  exit 1
fi

exec ./.venv/bin/python bot.py
