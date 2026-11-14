"""فحص جاهزية قبل التشغيل.

  python selfcheck.py --build        وقت بناء Docker (من غير توكن)
  python selfcheck.py                وقت تشغيل البوت (بيفحص التوكن ومكان الداتا كمان)
  python selfcheck.py --no-browser   نفس الفحص من غير تشغيل Chromium (للتجربة على جهاز من غير متصفح)
"""
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
BASE = Path(__file__).resolve().parent
BUILD = "--build" in sys.argv
SKIP_BROWSER = "--no-browser" in sys.argv
failed = []


def check(name, fn, hard=True):
    try:
        detail = fn()
        print(f"✅ {name}" + (f" — {detail}" if detail else ""), flush=True)
    except Exception as exc:  # noqa: BLE001
        mark = "❌" if hard else "⚠️"
        print(f"{mark} {name}: {exc}", flush=True)
        if hard:
            failed.append(name)


def check_answers():
    import core

    path = BASE / os.getenv("ANSWERS_FILE", "answers.txt")
    if not path.exists():
        raise FileNotFoundError(f"ملف الإجابات مش موجود: {path.name}")
    n = len(core.parse_answer_file(path))
    if n < 100:
        raise RuntimeError(f"عدد الأسئلة المحمّلة قليل ({n})")
    return f"{n} سؤال"


def check_assets():
    images = sorted((BASE / "assets").glob("welcome_*.jpg"))
    if not images:
        raise FileNotFoundError("مفيش صور ترحيب في assets/")
    return f"{len(images)} صورة ترحيب"


def check_token():
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("متغير BOT_TOKEN فاضي — ضيفه في Variables")
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", token):
        raise RuntimeError("شكل BOT_TOKEN غلط (لازم يبقى زي 123456:ABC...) — تأكد إنك نسخته كامل من BotFather")


def check_db_path():
    path = Path(os.getenv("DB_PATH", str(BASE / "bot.db")))
    path.parent.mkdir(parents=True, exist_ok=True)
    probe = path.parent / ".write_probe"
    probe.write_text("ok")
    probe.unlink()
    if os.getenv("RAILWAY_ENVIRONMENT") and not os.getenv("RAILWAY_VOLUME_MOUNT_PATH"):
        raise RuntimeError(
            "مفيش Volume مربوط — الداتا (اليوزرات وعدّاد الصلاة) هتتمسح مع كل نشر. اربط Volume على /data"
        )
    return str(path)


def check_browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            page = browser.new_page()
            page.set_content("<main><button>التالي</button></main>")
            text = page.locator("main button").inner_text()
            if text != "التالي":
                raise RuntimeError("المتصفح اشتغل بس قراءة النص العربي غلط")
            return f"Chromium {browser.version}"
        finally:
            browser.close()


check("ملف الإجابات", check_answers)
check("صور الترحيب", check_assets)
if not BUILD:
    check("BOT_TOKEN", check_token)
    check("مكان قاعدة البيانات", check_db_path, hard=not os.getenv("RAILWAY_ENVIRONMENT"))
if not SKIP_BROWSER:
    check("متصفح Chromium", check_browser)

if failed:
    print(f"\n🛑 الفحص فشل في: {', '.join(failed)}", flush=True)
    sys.exit(1)
print("\n🚀 كل شيء جاهز", flush=True)
