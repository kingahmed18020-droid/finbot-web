"""
محرّك حل المنصة (Playwright) — مبني على السكريبت الأصلي، ومعدّل ليشتغل من البوت:
- أحداث تقدّم عبر hook بدل print
- إيقاف آمن عبر threading.Event
- تتبّع الدروس المكتملة قبل كده / اللي خلّصها البوت
"""

import contextvars
import functools
import difflib
import io
from collections import defaultdict
from datetime import datetime, timezone
import getpass
import os
import re
import sys
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from PIL import Image, ImageDraw
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


SITE_URL = os.getenv("SITE_URL", "https://egy.app.spx-learning-square.com/login")
TARGET_URL = os.getenv(
    "TARGET_URL", "https://egy.app.spx-learning-square.com/subjects/7/curriculum"
)
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
MAX_STEPS = int(os.getenv("MAX_STEPS", "5000"))  # 654 سؤال + تنقل ومراجعات يتجاوزون 500 خطوة


# الاختبارات المقيّمة تُعالج كأنشطة؛ لا يتم تقديمها قبل حل كل أسئلتها.
RETRIES = int(os.getenv("RETRIES", "2"))  # إعادة تشغيل المتصفح والمتابعة عند انقطاع/مهلة
BLOCK_ASSETS = os.getenv("BLOCK_ASSETS", "false").lower() == "true"

# ── إعدادات الـ Proxy (اختياري) ──────────────────────────────────────────────
# لو حددت PROXY_SERVER بيتعمل proxy لكل الطلبات
# الصيغة: http://host:port  أو  http://user:pass@host:port
PROXY_SERVER   = os.getenv("PROXY_SERVER", "").strip()      # مثال: http://1.2.3.4:8080
PROXY_USERNAME = os.getenv("PROXY_USERNAME", "").strip()    # يُستخدم لو ما وضعتهوش في الرابط
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD", "").strip()    # يُستخدم لو ما وضعتهوش في الرابط

_hook = contextvars.ContextVar("hook", default=None)
_timing = contextvars.ContextVar("timing", default=None)
_cancel = contextvars.ContextVar("cancel", default=None)
_job = contextvars.ContextVar("job", default=None)
_report_consent = contextvars.ContextVar("report_consent", default=False)


class Stopped(RuntimeError):
    pass


class LoginFailed(RuntimeError):
    pass


class PageNotReady(RuntimeError):
    pass


def _redact_screenshot_bytes(image_bytes):
    with Image.open(io.BytesIO(image_bytes)) as image:
        image = image.convert("RGB")
        ImageDraw.Draw(image).rectangle(
            (0, 0, image.width, min(125, image.height)), fill="black"
        )
        result = io.BytesIO()
        image.save(result, format="PNG")
        return result.getvalue()


def _redacted_screenshot(page):
    """Capture a viewport screenshot while masking login fields and the account header."""
    try:
        image_bytes = page.screenshot(
            full_page=False,
            type="png",
            timeout=5000,
            mask=[page.locator("input, textarea"), page.locator("header, [role='banner']")],
            mask_color="#000000",
        )
        return _redact_screenshot_bytes(image_bytes)
    except Exception as exc:  # noqa: BLE001
        say_debug(f"تعذر إنشاء لقطة تشخيص منقحة ({type(exc).__name__}).")
        return None


def _diagnostic_report(status, error, last, screenshot, page):
    page_url = urlsplit(page) if page else None
    page_location = ""
    if page_url:
        safe_path = re.sub(r"(?i)[0-9a-f-]{16,}", "[id]", page_url.path)
        safe_path = re.sub(r"\d+", "[id]", safe_path)
        page_location = f"{page_url.netloc}{safe_path}"[:200]
    safe_error = str(error or "لم يكتمل النشاط المتوقع.")
    safe_error = re.sub(r"(?i)(password|token|authorization|cookie)=\S+", r"\1=[redacted]", safe_error)
    safe_error = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", safe_error)
    report = {
        "id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "status": status,
        "error": safe_error[:500],
        "activity": str(last or "غير محدد")[:150],
        "page": page_location or "غير متاح",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "screenshot": screenshot,
    }
    return report


def _send_diagnostic(report):
    if _report_consent.get():
        say(report, "diagnostic")


def say(msg, kind="log"):
    hook = _hook.get()
    if hook:
        hook(kind, msg)
    else:
        print(f"[{kind}] {msg}")


def say_debug(*args):
    say(" ".join(str(a) for a in args), "debug")


def check_cancel():
    event = _cancel.get()
    if event is not None and event.is_set():
        raise Stopped("stopped")

QUESTION_RE = re.compile(
    r"^السؤال\s+(?:الأول|الاول|الثاني|الثالث|الرابع|الخامس|السادس|السابع|"
    r"الثامن|التاسع|العاشر|[0-9٠-٩]+)(?:\s+(.*))?$"
)
ANSWER_LABELS = {"الاجابة", "الإجابة"}
REVIEW_BUTTON = re.compile(r"^علّم كمراجع$")
REVIEW_CARD_BUTTON = re.compile(r"النقاط المهمة", re.IGNORECASE)
CHECK_BUTTON = re.compile(
    r"^(راجع إجابتي|تحقق|إجابة|Answer|Submit|تحقق من الإجابة)$", re.IGNORECASE
)
NEXT_BUTTON = re.compile(
    r"^(التالي|إكمال|كمّل|كمّل التعلم|Next|Complete|Continue|متابعة)$",
    re.IGNORECASE,
)
TEST_MARKERS = (
    "اختبار",
    "امتحان",
    "quiz",
    "exam",
    "assessment",
)
# علامات صفحة الاختبار نفسها (البدء/الاستكمال/التقديم/درجة النجاح)
TEST_PAGE_MARKERS = ("ابدأ الاختبار", "استكمال الاختبار", "تقديم الاختبار", "درجة النجاح")
QUIZ_RESULT_MARKERS = ("نتيجة", "ناجح", "راسب")

# مهلات أقصر وأكثر تكيفًا لتسريع الحل بدون التضحية بالاستقرار
WAIT_SHORT = int(os.getenv("WAIT_SHORT", "150"))
WAIT_MEDIUM = int(os.getenv("WAIT_MEDIUM", "250"))
WAIT_AFTER_ANSWER = int(os.getenv("WAIT_AFTER_ANSWER", "250"))
WAIT_QUIZ = int(os.getenv("WAIT_QUIZ", "600"))  # انتظار بعد الضغط في شاشة الاختبار
QUIZ_QUESTION_TIMEOUT = 10000
QUIZ_NEXT_BUTTON = re.compile(r"(?:التالي|Next)", re.IGNORECASE)
QUIZ_RESUME_BUTTON = re.compile(r"استكمال\s+الاختبار", re.IGNORECASE)
QUIZ_START_BUTTON = re.compile(r"ابدأ\s+الاختبار", re.IGNORECASE)
QUIZ_SUBMIT_BUTTON = re.compile(r"(تقديم\s+الاختبار|Submit)", re.IGNORECASE)
QUIZ_ANSWER_INSTRUCTION = re.compile(
    r"اخت(?:ار|ر) إجابة واحدة|Choose one answer!?|Answer the question",
    re.IGNORECASE,
)
QUIZ_CONFIRM_BUTTON = re.compile(
    r"(?:تأكيد\s+(?:تقديم\s+الاختبار|التقديم|الإرسال)|إرسال\s+الاختبار|"
    r"Confirm(?:\s+submission)?)",
    re.IGNORECASE,
)
QUIZ_SCROLL_JS = (
    "() => { const root = document.scrollingElement; "
    "const nodes = [root, ...document.querySelectorAll('*')].filter((el) => { "
    "if (!el || el.clientHeight <= 0 || el.scrollHeight <= el.clientHeight + 1) return false; "
    "const overflow = getComputedStyle(el).overflowY; "
    "return overflow === 'auto' || overflow === 'scroll' || el === root; }); "
    "let moved = false; for (const el of nodes) { const before = el.scrollTop; "
    "el.scrollTop = Math.min(before + Math.max(el.clientHeight * 0.85, 250), el.scrollHeight); "
    "moved = moved || el.scrollTop > before; } "
    "window.scrollTo(0, Math.min(window.scrollY + Math.max(window.innerHeight * 0.85, 250), "
    "document.documentElement.scrollHeight)); return moved; }"
)
QUIZ_DIALOG_CONFIRM_BUTTON = re.compile(
    r"(?:تأكيد\s+(?:تقديم\s+الاختبار|التقديم|الإرسال)|إرسال\s+الاختبار|"
    r"Confirm(?:\s+submission)?|Submit)",
    re.IGNORECASE,
)

# قراءة كل عناصر محدد معين في طلب واحد للمتصفح بدل طلبين أو أكثر لكل عنصر
_VISIBLE_JS = (
    "(el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); "
    "return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'; }"
)
_LEAVES_JS = (
    "(els) => els.map((el, i) => ({ i, title: el.getAttribute('title') || '', "
    "text: (el.innerText || '').trim(), visible: (" + _VISIBLE_JS + ")(el) }))"
)
_BUTTONS_JS = (
    "(els) => els.map((el, i) => ({ i, label: (el.innerText || '').trim(), "
    "disabled: el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true' }))"
)
_RADIOS_JS = (
    "(els) => els.map((el) => ({ visible: (" + _VISIBLE_JS + ")(el), "
    "disabled: el.getAttribute('aria-disabled') }))"
)
_TEXTS_JS = "(els) => els.map((el) => el.innerText || '')"


class AnswerMatchError(RuntimeError):
    pass


def normalize_text(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.translate(
        str.maketrans(
            {
                "أ": "ا",
                "إ": "ا",
                "آ": "ا",
                "ٱ": "ا",
                "ى": "ي",
                "ـ": None,
            }
        )
    )
    value = re.sub(r"[\u064B-\u065F\u0670]", "", value)
    value = re.sub(r"[^\w\u0600-\u06FF]+", " ", value)
    return " ".join(value.split())


class AnswerIndex:
    """فهرس الإجابات: التطبيع بيتعمل مرة واحدة وقت التحميل + قاموس للتطابق التام."""

    def __init__(self, entries):
        self.entries = list(entries)
        self.rows = []
        self.exact = defaultdict(list)
        self.match_cache = {}
        for question, answer in self.entries:
            row = (normalize_text(question), normalize_text(answer), question, answer)
            self.rows.append(row)
            self.exact[row[0]].append(row)

    def __len__(self):
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)


def locate_answer_file():
    candidates = sorted(
        p
        for p in Path(__file__).resolve().parent.glob("*.txt")
        if not p.name.lower().startswith("requirements")
    )
    if len(candidates) != 1:
        raise FileNotFoundError(
            "يجب وجود ملف إجابات نصي واحد بجوار الأداة؛ لم أستطع تحديده تلقائيًا."
        )
    return candidates[0]


def parse_answer_file(path):
    path = Path(path)
    if path.suffix.casefold() == ".md":
        return parse_markdown_answer_file(path)

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    entries = []
    question_lines = []
    answer_lines = []
    state = "seek"

    def save_entry():
        question = " ".join(line.strip() for line in question_lines if line.strip())
        answer = " ".join(line.strip() for line in answer_lines if line.strip())
        if question and answer:
            entries.append((question, answer))

    for raw_line in lines:
        line = raw_line.strip()
        marker = QUESTION_RE.match(line)
        if marker:
            if state == "answer":
                save_entry()
            question_lines = [marker.group(1) or ""]
            answer_lines = []
            state = "question"
            continue

        if line in ANSWER_LABELS:
            if state == "question":
                state = "answer"
            continue

        if state == "answer" and (
            line.startswith(("المستوي", "المستوى", "الدرس", "نضغط "))
            or line.startswith(("الف مبروك", "ألف مبروك"))
            or line.startswith("…")
            or line.startswith("---")
        ):
            save_entry()
            question_lines = []
            answer_lines = []
            state = "seek"
            continue

        if state == "question" and line:
            question_lines.append(line)
        elif state == "answer" and line:
            answer_lines.append(line)

    if state == "answer":
        save_entry()

    if not entries:
        raise ValueError(f"لم أجد أسئلة وأجوبة قابلة للقراءة في: {path.name}")
    return AnswerIndex(entries)


def parse_markdown_answer_file(path):
    """Load question/answer pairs from the structured Markdown answer bank."""
    entries = []
    question_lines = []
    answer_lines = []
    state = None

    def save_entry():
        question = " ".join(line.strip() for line in question_lines if line.strip())
        answer = " ".join(line.strip() for line in answer_lines if line.strip())
        if question and answer:
            entries.append((question, answer))

    for raw_line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if line.startswith("**السؤال:**"):
            save_entry()
            question_lines = [line.removeprefix("**السؤال:**").strip()]
            answer_lines = []
            state = "question"
        elif line.startswith("**الإجابة:**"):
            answer_lines = [line.removeprefix("**الإجابة:**").strip()]
            state = "answer"
        elif line.startswith("#") or line.startswith("**") or line == "---":
            save_entry()
            question_lines = []
            answer_lines = []
            state = None
        elif state == "question" and line:
            question_lines.append(line)
        elif state == "answer" and line:
            answer_lines.append(line)

    save_entry()
    if not entries:
        raise ValueError(f"لم أجد أسئلة وأجوبة Markdown قابلة للقراءة في: {Path(path).name}")
    return AnswerIndex(entries)


def match_answer(question, entries):
    index = entries if isinstance(entries, AnswerIndex) else AnswerIndex(entries)
    normalized_question = normalize_text(question)
    if not normalized_question:
        raise AnswerMatchError("لم أستطع استخراج نص السؤال من الصفحة.")

    cached = index.match_cache.get(normalized_question)
    if cached:
        if cached[0] == "answer":
            return cached[1], cached[2]
        if cached[0] == "ambiguous":
            raise AnswerMatchError(
                f"وجدت أكثر من إجابة محتملة للسؤال: {question[:180]}"
            )
        raise AnswerMatchError(f"لا يوجد تطابق مؤكد للسؤال: {question[:180]}")

    exact_rows = index.exact.get(normalized_question)
    if exact_rows:
        if len({row[1] for row in exact_rows}) != 1:
            index.match_cache[normalized_question] = ("ambiguous",)
            raise AnswerMatchError(
                f"وجدت إجابات متعارضة للسؤال: {question[:180]}"
            )
        result = (exact_rows[0][3], exact_rows[0][2])
        index.match_cache[normalized_question] = ("answer", *result)
        return result

    # حدود التطابق: 0.82 للفحص السريع، 0.85 للقبول النهائي (أكثر مرونة قليلاً مع الحفاظ على الدقة)
    floor = 0.82
    accept_threshold = 0.85
    matcher = difflib.SequenceMatcher(None, autojunk=False)
    matcher.set_seq2(normalized_question)
    q_len = len(normalized_question)

    scored = []
    for normalized_source, normalized_answer, source_question, answer in index.rows:
        src_len = len(normalized_source)
        # تطابق احتوائي قوي للأسئلة الطويلة
        if (
            min(q_len, src_len) >= 25
            and (
                normalized_question in normalized_source
                or normalized_source in normalized_question
            )
        ):
            score = 0.96
        else:
            matcher.set_seq1(normalized_source)
            if (
                matcher.real_quick_ratio() < floor
                or matcher.quick_ratio() < floor
            ):
                continue
            score = matcher.ratio()
            if score < floor:
                continue
        scored.append((score, answer, source_question, normalized_answer))

    if not scored:
        index.match_cache[normalized_question] = ("no_match",)
        raise AnswerMatchError(f"لا يوجد تطابق مؤكد للسؤال: {question[:180]}")

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_answer, best_question, best_norm_answer = scored[0]
    if best_score < accept_threshold:
        index.match_cache[normalized_question] = ("no_match",)
        raise AnswerMatchError(f"لا يوجد تطابق مؤكد للسؤال: {question[:180]}")

    # رفض لو فيه إجابات متعارضة قريبة جدًا
    close_matches = [
        item
        for item in scored
        if best_score - item[0] < 0.03 and item[3] != best_norm_answer
    ]
    if close_matches:
        index.match_cache[normalized_question] = ("ambiguous",)
        raise AnswerMatchError(
            f"وجدت أكثر من إجابة محتملة للسؤال: {question[:180]}"
        )
    index.match_cache[normalized_question] = ("answer", best_answer, best_question)
    return best_answer, best_question


def _option_variants(option):
    """صيغ الخيار المحتملة: كما هو، وبدون شارة الحرف (A/B/C/D)، وبدون ترقيم '1.' الصريح."""
    no_letter = re.sub(r"^\s*[A-Da-d]\s*[\.\)\-:]?\s+", "", option)
    no_number = re.sub(r"^\s*[0-9٠-٩]+\s*[\.\)\-]\s+", "", no_letter)
    variants = {normalize_text(option), normalize_text(no_letter), normalize_text(no_number)}
    variants.discard("")
    return variants


def answer_option_index(options, answer):
    normalized_answer = normalize_text(answer)
    exact = []
    partial = []
    fuzzy = []

    for index, option in enumerate(options):
        variants = _option_variants(option)
        if not variants:
            continue

        if normalized_answer in variants:
            exact.append(index)
        elif any(
            min(len(v), len(normalized_answer)) >= 10
            and (normalized_answer in v or v in normalized_answer)
            for v in variants
        ):
            partial.append(index)
        else:
            ratio = max(
                difflib.SequenceMatcher(None, v, normalized_answer, autojunk=False).ratio()
                for v in variants
            )
            if ratio >= 0.88:
                fuzzy.append((ratio, index))

    if exact:
        if len(exact) > 1:
            raise AnswerMatchError("وجدت أكثر من خيار مطابق تمامًا للإجابة.")
        return exact[0]

    if partial:
        if len(partial) > 1:
            raise AnswerMatchError("وجدت أكثر من خيار جزئي مطابق للإجابة.")
        return partial[0]

    if fuzzy:
        fuzzy.sort(reverse=True)
        if len(fuzzy) == 1 or (fuzzy[0][0] - fuzzy[1][0] > 0.04):
            return fuzzy[0][1]

    raise AnswerMatchError("لم أستطع تحديد خيار واحد مطابق للإجابة في المنصة.")


def extract_question_prompt(card_text, option_texts=None):
    """استخراج نص السؤال فقط من نص بطاقة الاختيار مع إزالة خيارات الإجابة."""
    text = re.sub(r"\s+", " ", (card_text or "")).strip()
    if not text:
        return ""

    options = []
    if option_texts:
        for option in option_texts:
            compact = " ".join((option or "").split())
            if compact:
                options.append(compact)
    options = sorted({option for option in options}, key=len, reverse=True)

    for option in options:
        text = text.replace(option, " ")

    text = re.sub(r"(?i)(اختار\s*إجابة\s*واحدة|اختر\s*الإجابة\s*الصحيحة|choose\s+one\s+answer|answer\s+the\s+question)[:\s]*", " ", text)
    text = re.sub(r"^(?:[A-Da-d][\)\.]|[0-9٠-٩]+[\)\.]|[①-⑩]+[\)\.]?)\s*", "", text)
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" -–—:;.,")

    if not text and card_text:
        return re.sub(r"\s+", " ", card_text).strip()
    if len(normalize_text(text)) < 20 and len(normalize_text(card_text)) >= 20:
        return re.sub(r"\s+", " ", card_text).strip()
    return text


def main_content(page):
    mains = page.locator("main")
    count = mains.count()
    if not count:
        return page.locator("body")
    return mains.nth(count - 1)


def current_context(page):
    heading = main_content(page).locator("h1, h2").first
    if heading.count():
        return normalize_text(heading.inner_text())
    return normalize_text(page.url)


def _remember_unmatched(prompt):
    job = _job.get()
    if job is not None:
        text = " ".join(prompt.split())[:120]
        if text not in job["unmatched"]:
            job["unmatched"].append(text)


def wait_for_answer_feedback(page, previous_text, timeout=WAIT_AFTER_ANSWER):
    """Return as soon as the platform shows new grading feedback, with a safe timeout."""
    try:
        page.wait_for_function(
            """({previousText}) => {
                const roots = document.querySelectorAll("main");
                const root = roots.length ? roots[roots.length - 1] : document.body;
                const text = (root && root.innerText || "");
                const markers = /إجابة\\s+خاطئة|إجابتك\\s+غير\\s+صحيحة|حاول\\s+مرة\\s+أخرى|غير\\s+صحيح|إجابة\\s+صحيحة|إجابتك\\s+صحيحة|أحسنت|ممتاز|wrong\\s+answer|incorrect|correct\\s+answer|well\\s+done/gi;
                const count = value => (value.match(markers) || []).length;
                return count(text) > count(previousText || "");
            }""",
            arg={"previousText": previous_text},
            timeout=max(1, timeout),
        )
    except PWTimeout:
        return False
    return True


def solve_one_visible_question(page, entries, completed):
    content = main_content(page)
    radios = content.get_by_role("radio")
    count = radios.count()
    if not count:
        return False

    try:
        radio_states = radios.evaluate_all(_RADIOS_JS)
    except Exception:  # noqa: BLE001
        return False

    for index in range(min(count, len(radio_states))):
        state = radio_states[index]
        if not state["visible"] or state["disabled"] == "true":
            continue
        radio = radios.nth(index)

        # البحث عن البطاقة التي تحتوي على زر التحقق (هيكل المنصة الحالي)
        card = radio.locator(
            "xpath=ancestor::div[.//button[contains(normalize-space(.), 'راجع إجابتي') "
            "or contains(normalize-space(.), 'تحقق')]][1]"
        )
        try:
            if not card.count():
                continue
            card_text = card.inner_text(timeout=2500)
        except PWTimeout:
            continue

        options = card.get_by_role("radio")
        try:
            option_texts = options.evaluate_all(_TEXTS_JS)
        except Exception:  # noqa: BLE001
            option_texts = []

        prompt = extract_question_prompt(card_text, option_texts)

        # إزالة عناوين الدرس أو أرقام الأسئلة الزائدة إن وجدت
        prompt = re.sub(r"^(السؤال\s+(?:الأول|الاول|الثاني|الثالث|الرابع|الخامس|[0-9٠-٩]+)\s*)", "", prompt).strip()

        key = current_context(page) + "|" + normalize_text(prompt)
        if key in completed:
            continue

        try:
            answer, source_question = match_answer(prompt, entries)
        except AnswerMatchError:
            # نتخطى السؤال الحالي لو مفيش تطابق مؤكد بدل ما نوقف كل الشغل
            say(f"تخطّي سؤال بلا تطابق مؤكد: {prompt[:80]}", "debug")
            _remember_unmatched(prompt)
            continue

        try:
            selected_index = answer_option_index(option_texts, answer)
        except AnswerMatchError:
            say(f"تخطّي سؤال: لم أجد خيار مطابق للإجابة — {prompt[:60]}", "debug")
            _remember_unmatched(prompt)
            continue

        try:
            options.nth(selected_index).click(force=True, timeout=4000)
        except PWTimeout:
            continue

        submit = card.get_by_role("button", name=CHECK_BUTTON)
        if not submit.count():
            # محاولة بديلة بالبحث عن الزر بالنص
            submit = card.locator("button").filter(has_text=re.compile(r"راجع|تحقق|Answer", re.I))
            if not submit.count():
                raise RuntimeError("وجدت سؤال اختيار من متعدد دون زر تحقق واضح.")

        try:
            previous_text = content.inner_text(timeout=1500)
        except PWTimeout:
            previous_text = None
        submit.first.click(force=True, no_wait_after=True)

        if previous_text is None:
            page.wait_for_timeout(WAIT_AFTER_ANSWER)
        else:
            wait_for_answer_feedback(page, previous_text)
        try:
            # تحقق سريع من وجود رسالة خطأ
            result_text = content.inner_text(timeout=1500).casefold()
            if any(
                phrase in result_text
                for phrase in ("إجابة خاطئة", "إجابتك غير صحيحة", "حاول مرة أخرى", "incorrect", "wrong answer")
            ):
                raise RuntimeError(
                    f"المنصة لم تقبل الإجابة المطابقة للسؤال: {source_question}"
                )
        except PWTimeout:
            pass

        completed.add(key)
        say(source_question[:100], "q_ok")
        return True
    return False


def answer_text_question_if_supported(page, entries, completed):
    content = main_content(page)
    fields = content.locator("textarea, input[type='text'], input[type='number']")
    count = fields.count()
    if not count:
        return False

    for index in range(count):
        field = fields.nth(index)
        try:
            if not field.is_visible():
                continue
        except PWTimeout:
            continue

        card = field.locator(
            "xpath=ancestor::div[.//button[contains(., 'Answer') or "
            "contains(., 'إجابة') or contains(., 'تحقق') or contains(., 'راجع')]][1]"
        )
        if not card.count():
            continue

        try:
            prompt = card.inner_text(timeout=2000).strip()
        except PWTimeout:
            continue

        key = current_context(page) + "|" + normalize_text(prompt)
        if key in completed:
            continue

        try:
            answer, source_question = match_answer(prompt, entries)
        except AnswerMatchError:
            continue

        field.fill(answer)
        submit = card.get_by_role("button", name=CHECK_BUTTON)
        if not submit.count():
            submit = card.locator("button").filter(
                has_text=re.compile(r"راجع|تحقق|Answer|إجابة", re.I)
            )
            if not submit.count():
                continue

        try:
            previous_text = content.inner_text(timeout=1500)
        except PWTimeout:
            previous_text = None
        submit.first.click(force=True, no_wait_after=True)
        if previous_text is None:
            page.wait_for_timeout(WAIT_AFTER_ANSWER)
        else:
            wait_for_answer_feedback(page, previous_text)
        completed.add(key)
        say(source_question[:100], "q_ok")
        return True
    return False


def mark_review_points(page):
    content = main_content(page)
    buttons = content.get_by_role("button", name=REVIEW_BUTTON)
    if buttons.count():
        button = buttons.first
        button.scroll_into_view_if_needed()
        before_count = buttons.count()
        button.click(force=True, no_wait_after=True)
        page.wait_for_timeout(WAIT_SHORT)
        if content.get_by_role("button", name=REVIEW_BUTTON).count() < before_count:
            return True
        return False

    def progress():
        try:
            text = content.inner_text(timeout=1500)
        except PWTimeout:
            return None
        text = unicodedata.normalize("NFKC", text)
        text = re.sub(r"[\u064B-\u065F\u0670]", "", text)
        text = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
        match = re.search(
            r"(?:(\d+)\s*/\s*(\d+)\s*(?:متعلم|تعلمت|تم التعلم)|"
            r"(?:متعلم|تعلمت|تم التعلم)\s*(\d+)\s*/\s*(\d+))",
            text,
        )
        if not match:
            return None
        done, total = match.group(1, 2) if match.group(1) else match.group(3, 4)
        return int(done), int(total)

    def is_marked(card):
        for attribute in ("aria-pressed", "aria-checked", "data-state"):
            try:
                value = (card.get_attribute(attribute) or "").casefold()
            except PWTimeout:
                continue
            if value in ("true", "checked", "selected", "complete", "completed"):
                return True
        return False

    cards = content.get_by_role("button", name=REVIEW_CARD_BUTTON)
    count = cards.count()
    if not count:
        return False

    current_progress = progress()
    if current_progress and current_progress[1] and current_progress[0] >= current_progress[1]:
        return False

    for index in range(count):
        card = cards.nth(index)
        if not card.is_visible():
            continue
        was_marked = is_marked(card)
        if was_marked:
            continue
        before = progress()
        card.scroll_into_view_if_needed()
        card.click(force=True, no_wait_after=True)
        for _ in range(5):
            page.wait_for_timeout(WAIT_SHORT)
            after = progress()
            if before and after and after[0] > before[0]:
                return True
            if not before and is_marked(card):
                return True
            if before and after and after[0] < before[0]:
                card.click(force=True, no_wait_after=True)
                page.wait_for_timeout(WAIT_SHORT)
                restored = progress()
                if not restored or restored[0] < before[0]:
                    say_debug("تعذّر استعادة علامة مراجعة كانت محددة مسبقًا؛ توقفت لحماية التقدم.")
                    return False
                break
        else:
            say_debug("لم يتغير عدّاد النقاط المهمة بعد الضغط؛ لم أعتبر العنصر مكتملًا.")
            return False
    return False


def click_next(page):
    content = main_content(page)

    # 1) حوار إكمال الدرس
    completion_dialog = page.get_by_role("dialog")
    if completion_dialog.count():
        for pattern in (
            r"الدرس اللي بعده|Next lesson|الدرس التالي|كمّل|Continue",
            r"التالي|Next",
        ):
            lesson_button = completion_dialog.get_by_role(
                "button", name=re.compile(f"^({pattern})$", re.IGNORECASE)
            )
            if lesson_button.count() and lesson_button.first.is_visible():
                lesson_button.first.click(force=True, no_wait_after=True)
                page.wait_for_timeout(WAIT_MEDIUM)
                return True

    # 2) إنهاء المراجعة
    finish_review = content.get_by_role(
        "button", name=re.compile(r"^(إنهاء المراجعة|Finish review)$", re.IGNORECASE)
    )
    if finish_review.count() and finish_review.first.is_visible():
        finish_review.first.click(force=True, no_wait_after=True)
        page.wait_for_timeout(WAIT_MEDIUM)
        return True

    # 3) زر التالي / إكمال / كمّل (باستخدام الثابت الموحّد)
    next_button = content.get_by_role("button", name=NEXT_BUTTON)
    if next_button.count() and next_button.first.is_visible():
        next_button.first.click(force=True, no_wait_after=True)
        page.wait_for_timeout(WAIT_MEDIUM)
        return True

    # 4) محاولة أخيرة بالبحث النصي داخل الأزرار الظاهرة
    for btn in content.get_by_role("button").all():
        try:
            if not btn.is_visible():
                continue
            text = " ".join(btn.inner_text().split())
            if re.match(NEXT_BUTTON, text):
                btn.click(force=True, no_wait_after=True)
                page.wait_for_timeout(WAIT_MEDIUM)
                return True
        except Exception:  # noqa: BLE001
            continue

    controls = content.locator("button, a, [role='button'], [role='link']")
    for attempt in range(4):
        try:
            rows = controls.evaluate_all(
                "(els) => els.map((el, i) => ({ "
                "i, text: (el.innerText || el.getAttribute('aria-label') || "
                "el.getAttribute('title') || '').trim(), "
                "visible: (" + _VISIBLE_JS + ")(el) }))"
            )
        except PWError:
            rows = []
        for row in rows:
            label = " ".join(row["text"].split())
            if row["visible"] and NEXT_BUTTON.fullmatch(label):
                button = controls.nth(row["i"])
                button.scroll_into_view_if_needed(timeout=1500)
                button.click(force=True, no_wait_after=True)
                page.wait_for_timeout(WAIT_MEDIUM)
                return True
        if attempt == 3:
            break
        if not scroll_quiz_page(page):
            break
        page.wait_for_timeout(WAIT_SHORT)

    return False


def is_graded_test(label):
    """هل النشاط اختبار بدرجة؟ (المراجعات العادية مش اختبارات)."""
    normalized_label = normalize_text(label)
    return any(normalize_text(marker) in normalized_label for marker in TEST_MARKERS)


def on_test_page(page):
    """هل الصفحة الحالية شاشة اختبار (بدء/تقديم/درجة نجاح)؟"""
    try:
        text = main_content(page).inner_text(timeout=1500)
    except Exception:  # noqa: BLE001
        return False
    return any(marker in text for marker in TEST_PAGE_MARKERS)


def has_resumable_quiz_attempt(text):
    normalized = normalize_text(text)
    return (
        normalize_text("استكمال الاختبار") in normalized
        and normalize_text("جاري التنفيذ") in normalized
    )


def is_quiz_result_page(text):
    normalized = normalize_text(text)
    if has_resumable_quiz_attempt(text):
        return False

    has_result_summary = (
        "رجوع للاختبار" in normalized
        and any(normalize_text(marker) in normalized for marker in QUIZ_RESULT_MARKERS)
    )
    if has_result_summary:
        return True

    # بعد التسليم تعرض المنصة سجل المحاولات وزر إعادة البدء، وقد لا تعرض
    # عبارة «رجوع للاختبار»؛ لا تخلط هذه الشاشة بصفحة اختبار جديدة.
    has_attempt_history = (
        normalize_text("المحاولة رقم") in normalized
        or normalize_text("محاولات سابقة") in normalized
    )
    has_score = re.search(r"[0-9٠-٩]{1,3}\s*%", str(text))
    return (
        normalize_text("ابدأ الاختبار") in normalized
        and has_attempt_history
        and has_score is not None
    )


def visible_page_text(page, main_text=None):
    """Combine the rendered main area and body so result overlays are not missed."""
    texts = [main_text] if main_text else []
    if not texts:
        try:
            main_text = main_content(page).inner_text(timeout=2000)
        except Exception:  # noqa: BLE001
            main_text = ""
        if main_text:
            texts.append(main_text)
    try:
        body_text = page.locator("body").inner_text(timeout=2000)
    except Exception:  # noqa: BLE001
        body_text = ""
    if body_text and body_text not in texts:
        texts.append(body_text)
    return "\n".join(texts)


def return_to_curriculum(page):
    """Return from a quiz result through its back control, with a curriculum URL fallback."""
    back_name = re.compile(r"^\s*رجوع\s*$", re.IGNORECASE)
    controls = (
        lambda: page.get_by_role("button", name=back_name),
        lambda: page.get_by_role("link", name=back_name),
        lambda: page.get_by_text(back_name),
        lambda: page.locator(
            "button, a, [role='button'], [role='link'], [onclick]"
        ),
    )

    expected_path = urlsplit(TARGET_URL).path.rstrip("/")

    def curriculum_is_ready(timeout=30000):
        deadline = time.monotonic() + timeout / 1000
        while True:
            try:
                current_path = urlsplit(page.url).path.rstrip("/")
                if current_path == expected_path:
                    text = visible_page_text(page)
                    units, _locked = _read_units(page)
                    if text.strip() and not is_quiz_result_page(text) and units:
                        return True
            except Exception:  # noqa: BLE001
                pass
            if time.monotonic() >= deadline:
                return False
            page.wait_for_timeout(250)

    overview_path = urlsplit(TARGET_URL).path.rstrip("/").rsplit("/", 1)[0]

    def continue_from_subject_overview(timeout=25000):
        try:
            if urlsplit(page.url).path.rstrip("/") != overview_path:
                return False
        except Exception:  # noqa: BLE001
            return False
        if not _open_subject_continue_button(page, timeout=15000):
            say_debug("وصلت لملخص الثقافة المالية لكن زر «كمل التعلم» لم يظهر.")
            return False
        return curriculum_is_ready(timeout=timeout)

    def curriculum_or_overview_ready(timeout=30000):
        try:
            if urlsplit(page.url).path.rstrip("/") == overview_path:
                return continue_from_subject_overview(timeout=timeout)
        except Exception:  # noqa: BLE001
            return False
        return curriculum_is_ready(timeout=timeout)

    back_clicked = False
    for control_index, locate in enumerate(controls):
        try:
            candidates = locate()
            for index in range(candidates.count()):
                control = candidates.nth(index)
                if not control.is_visible():
                    continue
                if control_index == len(controls) - 1:
                    try:
                        labels = (
                            control.inner_text(timeout=500),
                            control.get_attribute("aria-label"),
                            control.get_attribute("title"),
                        )
                    except Exception:  # noqa: BLE001
                        continue
                    if not any(
                        normalize_text(label or "") == normalize_text("رجوع")
                        for label in labels
                    ):
                        continue
                try:
                    control.scroll_into_view_if_needed(timeout=1500)
                except Exception:  # noqa: BLE001
                    pass
                control.click(force=True, no_wait_after=True)
                back_clicked = True
                if curriculum_or_overview_ready(timeout=12000):
                    return True
                break
        except Exception as exc:  # noqa: BLE001
            say_debug(f"تعذر استخدام زر الرجوع الظاهر ({type(exc).__name__}).")
        if back_clicked:
            break
    if back_clicked:
        say_debug("زر الرجوع لم يُظهر قائمة الوحدات؛ أتابع بخطة الاستعادة.")

    try:
        page.go_back(wait_until="domcontentloaded", timeout=15000)
        if curriculum_or_overview_ready(timeout=10000):
            return True
    except Exception as exc:  # noqa: BLE001
        say_debug(f"تعذر الرجوع بسجل المتصفح ({type(exc).__name__}).")

    say_debug("لم يفتح زر الرجوع قائمة المنهج؛ سأفتح رابط المنهج مباشرة.")
    try:
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:  # noqa: BLE001
        say_debug(f"تعذر فتح رابط المنهج ({type(exc).__name__}).")
        return False

    if curriculum_or_overview_ready(timeout=35000):
        return True

    say_debug("رابط المنهج لم يحمّل محتوى القائمة؛ سأعيد تحميل الصفحة مرة واحدة.")
    try:
        page.reload(wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:  # noqa: BLE001
        say_debug(f"تعذرت إعادة تحميل المنهج ({type(exc).__name__}).")
        return False
    if curriculum_or_overview_ready(timeout=35000):
        return True

    say_debug("تعذر عرض المنهج بعد إعادة التحميل؛ سأرجع للرئيسية وأفتح بطاقة الثقافة المالية.")
    target = urlsplit(TARGET_URL)
    dashboard_url = urlunsplit(
        (target.scheme, target.netloc, "/dashboard", "", "")
    )
    try:
        page.goto(dashboard_url, wait_until="domcontentloaded", timeout=45000)
        if not _open_finance_card(page):
            say_debug("لم أجد بطاقة الثقافة المالية في الصفحة الرئيسية.")
            return False
        page.wait_for_url(
            re.compile(r".*/subjects/7(?:/curriculum)?/?$"),
            timeout=25000,
        )
    except Exception as exc:  # noqa: BLE001
        say_debug(f"تعذر فتح المنهج من الصفحة الرئيسية ({type(exc).__name__}).")
        return False
    if curriculum_or_overview_ready(timeout=35000):
        return True

    say_debug("بقيت نتيجة الاختبار ظاهرة بعد فتح المنهج؛ لن أكرر فحص الصفحة.")
    return False


def wait_for_quiz_question(content):
    """ينتظر تحميل أول خيار بعد بدء الاختبار بدل اعتباره غير موجود فورًا."""
    try:
        content.get_by_role("radio").first.wait_for(
            state="visible", timeout=QUIZ_QUESTION_TIMEOUT
        )
    except PWTimeout:
        return False
    return True


def start_or_resume_quiz(page, content):
    """Prefer resuming an existing attempt; only start a new attempt if offered."""
    for label, pattern in (
        ("استكمال", QUIZ_RESUME_BUTTON),
        ("بدء", QUIZ_START_BUTTON),
    ):
        buttons = content.get_by_role("button", name=pattern)
        for index in range(buttons.count()):
            button = buttons.nth(index)
            if not button.is_visible():
                continue
            say("🏁 بيستكمل الاختبار…" if label == "استكمال" else "🏁 بيبدأ الاختبار…", "log")
            button.click(force=True, no_wait_after=True)
            page.wait_for_timeout(WAIT_QUIZ)
            return True
        text_matches = content.get_by_text(pattern)
        for index in range(text_matches.count()):
            text = text_matches.nth(index)
            if not text.is_visible():
                continue
            say("🏁 بيستكمل الاختبار…" if label == "استكمال" else "🏁 بيبدأ الاختبار…", "log")
            text.click(force=True, no_wait_after=True)
            page.wait_for_timeout(WAIT_QUIZ)
            return True
    return False


def quiz_question_position(text):
    match = re.search(
        r"السؤال\s*([0-9٠-٩]+)\s*من\s*([0-9٠-٩]+)",
        text,
    )
    if not match:
        return None
    to_ascii = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    return tuple(int(value.translate(to_ascii)) for value in match.groups())


def scroll_quiz_page(page):
    """Scroll the document and nested scrollable quiz containers toward their bottoms."""
    try:
        return bool(page.evaluate(QUIZ_SCROLL_JS))
    except Exception:  # noqa: BLE001
        return False


def find_quiz_button(page, content, name, max_scrolls=40):
    """Find a visible quiz control, scrolling the page when long content hides its footer."""
    for attempt in range(max_scrolls + 1):
        for scope in (content, page):
            buttons = scope.get_by_role("button", name=name)
            for index in range(buttons.count()):
                button = buttons.nth(index)
                if button.is_visible():
                    button.scroll_into_view_if_needed(timeout=2000)
                    return button
        if attempt == max_scrolls or not scroll_quiz_page(page):
            break
        page.wait_for_timeout(WAIT_SHORT)
    return None


def extract_quiz_prompt(card):
    """Find the question text in nearby containers when options are nested separately."""
    current = card
    fallback = ""
    for _ in range(8):
        try:
            text = current.inner_text(timeout=1500)
        except PWTimeout:
            text = ""
        has_answer_instruction = QUIZ_ANSWER_INSTRUCTION.search(text) is not None
        prompt = QUIZ_ANSWER_INSTRUCTION.split(text, maxsplit=1)[0].strip()
        prompt = clean_quiz_prompt(prompt)
        if len(normalize_text(prompt)) > len(normalize_text(fallback)):
            fallback = prompt
        if len(normalize_text(prompt)) >= 20 and (
            has_answer_instruction or re.search(r"[؟?]\s*$", prompt)
        ):
            return prompt
        parent = current.locator("xpath=..")
        if not parent.count():
            break
        current = parent
    return fallback


def clean_quiz_prompt(prompt):
    """Remove quiz progress counters that may be included in the extracted question."""
    prompt = re.sub(
        r"^\s*(?:(?:السؤال\s*[0-9٠-٩]+(?:\s*من\s*[0-9٠-٩]+)?)|"
        r"[0-9٠-٩]+\s*/\s*[0-9٠-٩]+)\s*",
        "",
        prompt,
    )
    return prompt.strip()


def advance_quiz_question(page, content, question_text):
    """Advance after answering, or report whether advancement is unexpectedly blocked."""
    find_quiz_button(page, content, QUIZ_NEXT_BUTTON)
    next_buttons = content.get_by_role("button", name=QUIZ_NEXT_BUTTON)
    if not any(next_buttons.nth(i).is_visible() for i in range(next_buttons.count())):
        next_buttons = page.get_by_role("button", name=QUIZ_NEXT_BUTTON)
    enabled_buttons = []
    exact_text_buttons = []
    for index in range(next_buttons.count()):
        button = next_buttons.nth(index)
        if not button.is_visible() or not button.is_enabled():
            continue
        enabled_buttons.append(button)
        try:
            label = normalize_text(button.inner_text(timeout=500))
        except Exception:  # noqa: BLE001
            label = ""
        if label in ("التالي", "next"):
            exact_text_buttons.append(button)

    if exact_text_buttons or enabled_buttons:
        # Prefer the labeled question-advance button over pagination arrow controls.
        button = exact_text_buttons[-1] if exact_text_buttons else enabled_buttons[-1]
        button.scroll_into_view_if_needed(timeout=2000)
        button.click(force=True, no_wait_after=True)
        previous_position = quiz_question_position(question_text)
        try:
            page.wait_for_function(
                """({previousText, previousPosition}) => {
                    const roots = document.querySelectorAll("main");
                    const root = roots.length ? roots[roots.length - 1] : document.body;
                    const text = (root && root.innerText || "").trim();
                    if (previousPosition) {
                        const match = text.match(/السؤال\\s*([0-9٠-٩]+)\\s*من\\s*([0-9٠-٩]+)/);
                        if (match) {
                            const number = value => Number(value.replace(/[٠-٩]/g,
                                digit => String("٠١٢٣٤٥٦٧٨٩".indexOf(digit))));
                            return number(match[1]) !== previousPosition[0]
                                || number(match[2]) !== previousPosition[1];
                        }
                    }
                    return text !== previousText;
                }""",
                arg={
                    "previousText": question_text.strip(),
                    "previousPosition": previous_position,
                },
                timeout=1500,
            )
        except PWTimeout:
            return "blocked"
        return "advanced"

    position = quiz_question_position(question_text)
    if position and position[0] >= position[1]:
        return "last"
    submit_button = find_quiz_button(page, content, QUIZ_SUBMIT_BUTTON)
    if submit_button is not None and submit_button.is_enabled():
        return "last"
    return "blocked"


def submit_quiz(page, content):
    """Submit answered quiz and confirm the platform's final-submit dialog."""
    submit_button = find_quiz_button(page, content, QUIZ_SUBMIT_BUTTON)
    if submit_button is None or not submit_button.is_enabled():
        return False

    say("📤 بيقدّم الاختبار…", "log")
    submit_button.click(force=True, no_wait_after=True)
    page.wait_for_timeout(WAIT_MEDIUM)

    confirm_clicked = False
    dialogs = page.get_by_role("dialog")
    for index in range(dialogs.count()):
        dialog = dialogs.nth(index)
        if not dialog.is_visible():
            continue
        confirm_button = dialog.get_by_role("button", name=QUIZ_DIALOG_CONFIRM_BUTTON)
        if confirm_button.count() and confirm_button.first.is_visible():
            confirm_button.first.click(force=True, no_wait_after=True)
            confirm_clicked = True
            break

    if not confirm_clicked:
        # Some platform builds render the confirmation as an overlay without dialog semantics.
        confirm_buttons = page.get_by_role("button", name=QUIZ_CONFIRM_BUTTON)
        for index in range(confirm_buttons.count()):
            button = confirm_buttons.nth(index)
            if button.is_visible():
                button.click(force=True, no_wait_after=True)
                confirm_clicked = True
                break

    if not confirm_clicked:
        # Other builds repeat the original label on the confirmation button.
        visible_buttons = []
        all_submit_buttons = page.get_by_role("button", name=QUIZ_SUBMIT_BUTTON)
        for index in range(all_submit_buttons.count()):
            button = all_submit_buttons.nth(index)
            if button.is_visible():
                visible_buttons.append(button)
        if len(visible_buttons) > 1:
            visible_buttons[-1].click(force=True, no_wait_after=True)
            confirm_clicked = True

    deadline = time.monotonic() + (15 if confirm_clicked else 3)
    while True:
        try:
            result_text = main_content(page).inner_text(timeout=2000)
        except Exception:  # noqa: BLE001
            result_text = ""
        if is_quiz_result_page(result_text):
            return True
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(WAIT_MEDIUM)

    if confirm_clicked:
        say_debug("تم الضغط على تأكيد التقديم لكن صفحة نتيجة الاختبار لم تظهر.")
    else:
        say_debug("ظهر زر التقديم الأول لكن لم أجد زر تأكيد ظاهرًا أو نتيجة للاختبار.")
    return False


def solve_quiz(page, entries):
    """
    يحل اختبار المستوى / اختبار المعرفة تلقائيًا:
    1) يستكمل محاولة قائمة، أو يبدأ اختبارًا جديدًا لو لم توجد محاولة.
    2) يحل كل أسئلة الاختبار (radio) بنفس منطق solve_one_visible_question.
    3) يضغط «تقديم الاختبار» في النهاية.
    يرجّع True لو عولج الاختبار، False لو تعذّر حل كل الأسئلة بأمان.
    """
    content = main_content(page)

    # ─── الخطوة 1: استكمل المحاولة القائمة قبل عرض خيار البدء من جديد ───────
    start_or_resume_quiz(page, content)

    if not wait_for_quiz_question(content):
        say_debug("لم تظهر خيارات الاختبار بعد انتظار تحميلها؛ لم يتم العثور على سؤال قابل للحل.")
        return False

    # ─── الخطوة 2: حل الأسئلة ────────────────────────────────────────────────
    completed = set()
    answered_any = False
    missed_question = False
    MAX_QUIZ_STEPS = 200

    for _step in range(MAX_QUIZ_STEPS):
        check_cancel()
        try:
            full_text = content.inner_text(timeout=2000)
        except Exception:  # noqa: BLE001
            break

        # انتهى الاختبار؟ (ظهرت نتيجة أو اختفت أسئلة وأزرار التحقق)
        if "تقديم الاختبار" not in full_text and "ابدأ الاختبار" not in full_text:
            radios = content.get_by_role("radio")
            if not radios.count():
                break

        # حاول تحل سؤال ظاهر
        try:
            radios = content.get_by_role("radio")
            count = radios.count()
        except Exception:  # noqa: BLE001
            break

        if not count:
            break

        try:
            radio_states = radios.evaluate_all(_RADIOS_JS)
        except Exception:  # noqa: BLE001
            break

        solved_this_round = False
        for index in range(min(count, len(radio_states))):
            state = radio_states[index]
            if not state["visible"] or state["disabled"] == "true":
                continue
            radio = radios.nth(index)

            card = radio.locator(
                "xpath=ancestor::div[count(.//*[@role='radio'] | .//input[@type='radio']) >= 2][1]"
            )
            if not card.count():
                card = radio.locator(
                    "xpath=ancestor::div[.//button[contains(normalize-space(.), 'راجع إجابتي') "
                    "or contains(normalize-space(.), 'تحقق') "
                    "or contains(normalize-space(.), 'Answer')]"
                    "or .//button[contains(normalize-space(.), 'تقديم الاختبار')]][1]"
                )
            if not card.count():
                # في الاختبارات بعض الأسئلة مش مغلّفة ببطاقة مع زر تحقق فردي
                # نحاول نجيب البطاقة الأكبر اللي فيها السؤال
                card = radio.locator("xpath=ancestor::div[contains(@class,'question') or contains(@class,'card')][1]")
                if not card.count():
                    continue

            prompt = extract_quiz_prompt(card)

            key = "quiz|" + normalize_text(prompt)
            if key in completed:
                continue

            try:
                answer, source_question = match_answer(prompt, entries)
            except AnswerMatchError:
                say(f"تخطّي سؤال اختبار بلا تطابق: {prompt[:80]}", "debug")
                _remember_unmatched(prompt)
                missed_question = True
                continue

            options = card.get_by_role("radio")
            try:
                option_texts = options.evaluate_all(_TEXTS_JS)
            except Exception:  # noqa: BLE001
                continue

            try:
                selected_index = answer_option_index(option_texts, answer)
            except AnswerMatchError:
                say(f"تخطّي سؤال اختبار: مفيش خيار مطابق — {prompt[:60]}", "debug")
                _remember_unmatched(prompt)
                missed_question = True
                continue

            try:
                options.nth(selected_index).click(force=True, timeout=4000)
            except PWTimeout:
                continue

            # زر تحقق فردي لو موجود (بعض الاختبارات بيتحقق سؤال سؤال)
            submitted_individually = False
            try:
                submit_one = card.get_by_role("button", name=re.compile(r"راجع إجابتي|تحقق|Answer", re.IGNORECASE))
                if submit_one.count() and submit_one.first.is_visible():
                    submit_one.first.click(force=True, no_wait_after=True)
                    wait_for_answer_feedback(page, full_text, WAIT_MEDIUM)
                    submitted_individually = True
            except Exception:  # noqa: BLE001
                pass
            if not submitted_individually:
                page.wait_for_timeout(WAIT_AFTER_ANSWER)

            completed.add(key)
            say(source_question[:100], "q_ok")
            answered_any = True
            advancement = advance_quiz_question(page, content, full_text)
            if advancement == "blocked":
                say_debug("تمت الإجابة لكن زر الانتقال غير متاح قبل آخر سؤال؛ لم أقدّم الاختبار جزئيًا.")
                _remember_unmatched("تعذّر الانتقال بين أسئلة الاختبار.")
                return False
            solved_this_round = advancement == "advanced"
            break  # ابدأ جولة جديدة بعد كل سؤال (الصفحة ممكن تتحدّث)

        if not solved_this_round:
            break

    if not answered_any or missed_question:
        return False

    # ─── الخطوة 3: قدّم الاختبار وأكّد نافذة التأكيد إن ظهرت ───────────────
    page.wait_for_timeout(WAIT_MEDIUM)
    if not submit_quiz(page, content):
        say_debug("زر تقديم الاختبار غير ظاهر بعد الإجابة؛ لن أعتبر الاختبار منتهيًا.")
        return False

    return True


_UNIT_COUNT = re.compile(r"(\d+)\s*/\s*(\d+)\s*عنصر")


def _read_units(page):
    """وحدات الشريط الجانبي (الأزرار اللي عليها «N/M عنصر») + المقفولة."""
    units, locked = [], []
    try:
        side = page.get_by_role("complementary")
        if not side.count():
            return units, locked
        buttons = side.get_by_role("button")
        total = buttons.count()
    except Exception:  # noqa: BLE001
        return units, locked
    skip_words = ("رجوع", "توسيع الشريط الجانبي", "طيّ الشريط الجانبي", "طي الشريط")
    for index in range(total):
        button = buttons.nth(index)
        try:
            label = " ".join(button.inner_text(timeout=1000).split())
            disabled = button.is_disabled()
        except Exception:  # noqa: BLE001
            continue
        if not label or any(word in label for word in skip_words):
            continue
        name = re.sub(r"\s+", " ", re.sub(r"\d+\s*/\s*\d+\s*(?:عنصر)?|عنصر", "", label)).strip(" -–:")
        count = _UNIT_COUNT.search(label)
        if count:
            units.append({"index": index, "label": label, "name": name, "disabled": disabled,
                          "done": int(count.group(1)), "total": int(count.group(2))})
        elif disabled or "مقفل" in label or "🔒" in label:
            locked.append(name)
    return units, locked


def _side_size(side):
    try:
        return side.get_by_role("button").count() + side.locator("div.cursor-pointer").count()
    except Exception:  # noqa: BLE001
        return 0


def expand_next_unit(page, side):
    """يفتح أول وحدة ناقصة ومطوية حاليًا. بيرجّع True لو فتح حاجة."""
    job = _job.get()
    opened_units = job.setdefault("opened_units", set())
    units, _locked = _read_units(page)
    for unit in units:
        if unit["disabled"] or (unit["total"] and unit["done"] >= unit["total"]):
            continue
        key = normalize_text(unit["name"])
        button = side.get_by_role("button").nth(unit["index"])
        try:
            expanded = button.get_attribute("aria-expanded")
            if expanded == "true":
                opened_units.add(key)
                continue  # مفتوحة بالفعل
            if key in opened_units and expanded is None:
                continue
            if expanded not in (None, "false"):
                continue  # مفتوحة أصلًا
            before = _side_size(side)
            button.click(force=True, no_wait_after=True)
            page.wait_for_timeout(WAIT_SHORT)
            if _side_size(side) < before:
                # كانت مفتوحة وقفلناها بالغلط → نفتحها تاني
                side.get_by_role("button").nth(unit["index"]).click(force=True, no_wait_after=True)
                page.wait_for_timeout(WAIT_SHORT)
        except Exception:  # noqa: BLE001
            continue
        opened_units.add(key)
        say(unit["name"], "unit_open")
        return True
    return False


CURRICULUM_API = "/api/v1/subjects/7/curriculum"


def fetch_curriculum(page):
    """بيانات المنهج من الـ API (نفس اللي الصفحة بتستخدمه) — أدق من قراءة الشريط الجانبي."""
    try:
        data = page.evaluate(
            "async (url) => { const r = await fetch(url, {credentials: 'include'}); return await r.json(); }",
            CURRICULUM_API,
        )
        if isinstance(data, dict) and data.get("success"):
            return data.get("data") or []
    except Exception:  # noqa: BLE001
        pass
    return []


def _unit_items(unit):
    for sec in unit.get("sections") or []:
        for item in sec.get("items") or []:
            yield sec.get("name", ""), item
    for item in unit.get("items") or []:
        yield unit.get("title", ""), item


def _is_test_item(item):
    kind = normalize_text(str(item.get("item_type", "")))
    return is_graded_test(item.get("title", "")) or any(
        word in kind for word in ("test", "quiz", "exam", "assessment")
    )


def summarize_curriculum(curriculum):
    """[(اسم الوحدة، المخلّص، الإجمالي، مقفولة؟)] من بيانات الـ API."""
    out = []
    for unit in curriculum:
        items = [it for _sec, it in _unit_items(unit)]
        done = sum(1 for it in items if it.get("is_completed"))
        locked = bool(items) and not done and not any(it.get("is_unlocked") for it in items)
        out.append((unit.get("title", ""), done, len(items), locked))
    return out


def next_pending_item(curriculum, completed_tests=()):
    """أول عنصر مفتوح وغير مكتمل، مع استبعاد اختبارات أُنجزت في المهمة الحالية."""
    completed_test_titles = {
        normalize_text(title) for title in completed_tests if normalize_text(title)
    }
    for unit in curriculum:
        for sec_name, item in _unit_items(unit):
            if item.get("is_completed") or not item.get("is_unlocked"):
                continue
            title = normalize_text(item.get("title", ""))
            if _is_test_item(item) and title in completed_test_titles:
                continue
            return ("item", unit.get("title", ""), sec_name, item)
    return None


def _visible_matches(side, text):
    """Prefer exact visible sidebar labels before considering partial matches."""
    if not text:
        return []
    normalized_text = normalize_text(text)
    try:
        exact = side.get_by_text(text, exact=True)
    except Exception:  # noqa: BLE001
        exact = None

    def visible_matches(locator):
        found = []
        if locator is None:
            return found
        try:
            for index in range(min(locator.count(), 12)):
                match = locator.nth(index)
                if match.is_visible():
                    found.append(match)
        except Exception:  # noqa: BLE001
            return []
        return found

    exact_matches = visible_matches(exact)
    if exact_matches:
        return exact_matches

    try:
        partial = side.get_by_text(text[: min(len(text), 40)])
    except Exception:  # noqa: BLE001
        return []
    matches = visible_matches(partial)
    ranked = []
    for match in matches:
        try:
            label = normalize_text(match.inner_text(timeout=750))
        except Exception:  # noqa: BLE001
            continue
        if normalized_text in label:
            ranked.append((len(label), match))
    ranked.sort(key=lambda result: result[0])
    return [match for _length, match in ranked]


def _visible_activity_matches(side, title):
    """Find an activity using the same leaf rows as curriculum scanning."""
    normalized_title = normalize_text(title)
    if not normalized_title:
        return []
    leaves = side.locator("div.cursor-pointer")
    try:
        rows = leaves.evaluate_all(_LEAVES_JS)
    except PWError:
        return []
    ranked = []
    for row in rows:
        if not row["visible"]:
            continue
        label = normalize_text(row["text"])
        if not label:
            continue
        if label == normalized_title:
            rank = 0
        elif normalized_title in label or label in normalized_title:
            rank = 1
        else:
            continue
        ranked.append((rank, len(label), leaves.nth(row["i"])))
    ranked.sort(key=lambda match: (match[0], match[1]))
    return [match for _rank, _length, match in ranked]


def _activity_view_snapshot(page):
    return page.evaluate(
        "() => { const roots = document.querySelectorAll('main'); "
        "const root = roots.length ? roots[roots.length - 1] : document.body; "
        "return { url: location.href, text: (root && root.innerText || '').trim() }; }"
    )


def _activity_is_current(page, title):
    normalized_title = normalize_text(title)
    if not normalized_title:
        return False
    try:
        content = main_content(page)
        content_text = content.inner_text(timeout=1500)
        normalized_content = normalize_text(content_text)
        if not normalized_content or "اختر درسا من القائمة" in normalized_content:
            return False
        headings = content.locator("h1, h2")
        for index in range(min(headings.count(), 3)):
            heading = normalize_text(headings.nth(index).inner_text(timeout=750))
            if heading and (heading == normalized_title or normalized_title in heading):
                return True
        lines = content_text.splitlines()
    except PWError:
        return False
    for line in lines[:3]:
        normalized_line = normalize_text(line)
        if not normalized_line:
            continue
        if normalized_line == normalized_title or normalized_title in normalized_line:
            return True
    return False


def _wait_for_activity_view_change(page, before):
    try:
        page.wait_for_function(
            """({url, text}) => {
                const roots = document.querySelectorAll("main");
                const root = roots.length ? roots[roots.length - 1] : document.body;
                return location.href !== url
                    || (root && root.innerText || "").trim() !== text;
            }""",
            arg=before,
            timeout=2000,
        )
    except PWTimeout:
        return False
    return True


def _expand_unit_named(page, side, unit_name):
    """Open a specific unlocked unit from the curriculum API pick."""
    normalized_name = normalize_text(unit_name)
    if not normalized_name:
        return False
    units, _locked = _read_units(page)
    matches = [
        unit for unit in units
        if normalize_text(unit["name"]) == normalized_name
    ]
    if not matches:
        matches = [
            unit for unit in units
            if normalized_name in normalize_text(unit["name"])
            or normalize_text(unit["name"]) in normalized_name
        ]
    if len(matches) != 1:
        return False

    unit = matches[0]
    if unit["disabled"] or (unit["total"] and unit["done"] >= unit["total"]):
        return False
    button = side.get_by_role("button").nth(unit["index"])
    try:
        expanded = button.get_attribute("aria-expanded")
        if expanded == "true":
            return True
        before = _side_size(side)
        button.click(force=True, no_wait_after=True)
        page.wait_for_timeout(WAIT_SHORT)
        after = _side_size(side)
        expanded = button.get_attribute("aria-expanded")
        if expanded == "true" or after > before:
            return True
        if after < before:
            button.click(force=True, no_wait_after=True)
            page.wait_for_timeout(WAIT_SHORT)
            return _side_size(side) >= before
        return expanded is None
    except Exception as exc:  # noqa: BLE001
        say_debug(f"تعذر فتح الوحدة «{unit_name}» ({type(exc).__name__}).")
        return False


def _expand_collapsed_sidebar(page, side):
    """Open the curriculum sidebar when it is collapsed after returning from an activity."""
    toggle_name = re.compile(
        r"(توسيع الشريط الجانبي|فتح الشريط الجانبي|expand sidebar|open sidebar)",
        re.IGNORECASE,
    )
    for scope in (side, page):
        try:
            toggles = scope.get_by_role("button", name=toggle_name)
            for index in range(toggles.count()):
                toggle = toggles.nth(index)
                if not toggle.is_visible():
                    continue
                toggle.click(force=True, no_wait_after=True)
                page.wait_for_timeout(WAIT_SHORT)
                if _read_units(page)[0]:
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def open_next_via_api(page):
    """بديل مستقل عن شكل الـ HTML: يجيب العنصر الجاي من الـ API ويفتحه بالنص."""
    job = _job.get()
    curriculum = fetch_curriculum(page)
    if not curriculum:
        say_debug("API المنهج رجّع فاضي")
        return False
    pick = next_pending_item(
        curriculum, _job.get().get("completed_tests", set())
    )
    if not pick:
        return False
    _kind, unit_title, sec_name, item = pick
    title = item.get("title", "")
    key = str(item.get("id") or title)
    attempts = job.setdefault("api_attempts", {})
    if _activity_is_current(page, title):
        attempts.pop(key, None)
        job["api_current"] = title
        say_debug(f"النشاط «{title}» مفتوح بالفعل؛ لن أكرر النقر عليه.")
        return False

    failed_attempts = attempts.get(key, 0)
    if failed_attempts >= 3:
        job["stuck"] = f"العنصر «{title}» ({unit_title}) مكملش بعد 3 محاولات"
        return False

    side = page.get_by_role("complementary")
    targets = []
    unit_expanded = False
    section_expanded = False
    section_found = False
    normalized_title = normalize_text(title)
    normalized_section = normalize_text(sec_name)
    section_is_parent = bool(
        normalized_section
        and normalized_section != normalize_text(unit_title)
    )

    def exact_activity_matches(matches):
        exact = []
        for match in matches:
            try:
                if normalize_text(match.inner_text(timeout=750)) == normalized_title:
                    exact.append(match)
            except Exception:  # noqa: BLE001
                continue
        return exact

    for _ in range(6):
        activity_matches = _visible_activity_matches(side, title)
        targets = exact_activity_matches(activity_matches)
        if targets:
            break
        if activity_matches:
            targets = activity_matches
            break
        if not _read_units(page)[0] and _expand_collapsed_sidebar(page, side):
            continue
        if not unit_expanded:
            unit_expanded = _expand_unit_named(page, side, unit_title)
            if unit_expanded:
                continue
        if not section_expanded and section_is_parent:
            matches = _visible_matches(side, sec_name)
            if matches:
                section_found = True
                section = matches[0]
                try:
                    expanded = section.get_attribute("aria-expanded")
                except Exception:  # noqa: BLE001
                    expanded = None
                if expanded != "true":
                    section.click(force=True, no_wait_after=True)
                    page.wait_for_timeout(WAIT_SHORT)
                section_expanded = True
                continue
            section_expanded = True
        if not section_found:
            targets = _visible_matches(side, title)
            if targets:
                break
        if section_is_parent:
            break
    if not targets:
        say_debug(f"مش لاقي «{title}» ظاهر في الشريط الجانبي (وحدة: {unit_title} / قسم: {sec_name})")
        refreshed = job.setdefault("api_refreshed", set())
        if key not in refreshed:
            refreshed.add(key)
            try:
                page.reload(wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(WAIT_MEDIUM)
            except PWError as exc:
                say_debug(f"تعذر تحديث صفحة المنهج للعنصر «{title}» ({type(exc).__name__}).")
                return False
            updated_curriculum = fetch_curriculum(page)
            if not updated_curriculum:
                say_debug(f"لم تتوفر بيانات المنهج بعد تحديث الصفحة للعنصر «{title}».")
                return False
            return open_next_via_api(page)
        attempts[key] = failed_attempts + 1
        job["stuck"] = f"النشاط «{title}» متاح في بيانات المنهج، لكن لم أستطع فتحه من الشريط الجانبي."
        return False

    index = 1 if (item.get("item_type") == "review" and len(targets) >= 2) else 0
    previous_leaf = job.get("leaf")
    before = _activity_view_snapshot(page)
    targets[index].click(force=True, no_wait_after=True)
    if not _wait_for_activity_view_change(page, before) and not _activity_is_current(page, title):
        say_debug(f"تم الضغط على «{title}» لكن محتوى النشاط لم يتغير؛ سأحدّث المنهج وأعيد المحاولة.")
        refreshed = job.setdefault("api_refreshed", set())
        if key not in refreshed:
            refreshed.add(key)
            try:
                page.reload(wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(WAIT_MEDIUM)
            except PWError as exc:
                say_debug(f"تعذر تحديث المنهج لإعادة فتح «{title}» ({type(exc).__name__}).")
                return False
            updated_curriculum = fetch_curriculum(page)
            if not updated_curriculum:
                say_debug(f"لم تتوفر بيانات المنهج بعد تحديث الصفحة لإعادة فتح «{title}».")
                return False
            return open_next_via_api(page)
        attempts[key] = failed_attempts + 1
        job["stuck"] = f"النشاط «{title}» متاح، لكن صفحة النشاط لم تفتح بعد إعادة المحاولة."
        return False
    attempts.pop(key, None)
    job.get("api_refreshed", set()).discard(key)
    job.pop("api_current", None)
    page.wait_for_timeout(WAIT_SHORT)
    if previous_leaf and not is_graded_test(previous_leaf):
        say(previous_leaf, "done")
    job["leaf"] = " ".join(title.split())[:100]
    job["worked"].add(unit_title)
    say(job["leaf"], "open")
    return True


def _fallback_open(page, side):
    if expand_next_unit(page, side):
        return True
    return open_next_via_api(page)


def open_next_available_activity(page, visited, current_group, expanded_course=False):
    side = page.get_by_role("complementary")
    if not side.count():
        return open_next_via_api(page), current_group

    def open_unfinished_leaf(group_name):
        scan = {"done": 0, "locked": 0, "quiz": 0, "pending": 0}
        leaves = side.locator("div.cursor-pointer")
        try:
            infos = leaves.evaluate_all(_LEAVES_JS)
        except Exception:  # noqa: BLE001
            infos = []

        for info in infos:
            if not info["visible"]:
                continue

            index = info["i"]
            status = info["title"]
            label = info["text"]
            normalized_label = normalize_text(label)

            completed_test_titles = _job.get().get("completed_tests", set())
            locally_completed_test = is_graded_test(label) and any(
                title and title in normalized_label
                for title in completed_test_titles
            )

            if status == "مكتمل" or "مكتمل" in label:
                scan["done"] += 1
            elif status == "مقفول" or "مقفول" in label or "🔒" in label:
                scan["locked"] += 1
            elif is_graded_test(label):
                scan["quiz"] += 1
                if not locally_completed_test:
                    _job.get()["tests"].append(" ".join(label.split())[:100])

            if locally_completed_test:
                continue

            if (
                not normalized_label
                or status in ("مكتمل", "مقفول")
                or "مكتمل" in label
                or "ممتاز" in label
                or "توسيع الشريط الجانبي" in label
                or "رجوع" in label
                or "مقفول" in label
            ):
                continue

            key = f"{normalize_text(group_name)}|{normalized_label}|{index}"
            if key in visited:
                scan["pending"] += 1  # فتحناه قبل كده وما اكتملش على المنصة
                continue

            job = _job.get()
            if job.get("leaf") and not is_graded_test(job["leaf"]):
                previous_leaf = job["leaf"]
            else:
                previous_leaf = None

            before = _activity_view_snapshot(page)
            leaves.nth(index).click(force=True, no_wait_after=True)
            if not _wait_for_activity_view_change(page, before):
                if _activity_is_current(page, label):
                    visited.add(key)
                    job = _job.get()
                    job["leaf"] = " ".join(label.split())[:100]
                    job["worked"].add(group_name)
                    job.pop("api_current", None)
                    say_debug(f"النشاط «{label}» كان مفتوحًا بالفعل؛ سأتابع التعامل مع محتواه.")
                    say(job["leaf"], "open")
                    return True
                say_debug(f"النقر على «{label}» لم يفتح النشاط؛ سأحاول تحديده من بيانات المنهج.")
                if open_next_via_api(page):
                    return True
                if not _job.get().get("stuck"):
                    _job.get()["stuck"] = (
                        f"ظهر النشاط «{label}» في الشريط الجانبي، لكن لم تتغير صفحة المحتوى بعد إعادة المحاولة."
                    )
                scan["pending"] += 1
                _job.get()["scan"] = scan
                continue
            page.wait_for_timeout(WAIT_SHORT)
            visited.add(key)
            if previous_leaf:
                say(previous_leaf, "done")
            job["leaf"] = " ".join(label.split())[:100]
            job["worked"].add(group_name)
            say(job["leaf"], "open")
            return True

        _job.get()["scan"] = scan
        return False

    # أولوية: أكمل المجموعة الحالية أولاً
    if current_group:
        if open_unfinished_leaf(current_group):
            return True, current_group
        job = _job.get()
        if current_group in job["worked"] and current_group not in job["announced"]:
            job["announced"].add(current_group)
            say(current_group, "group_done")

    buttons = side.get_by_role("button")
    button_data = []
    try:
        button_rows = buttons.evaluate_all(_BUTTONS_JS)
    except Exception:  # noqa: BLE001
        button_rows = None

    if button_rows is not None:
        for row in button_rows:
            label = " ".join(row["label"].split())
            if row["disabled"] or not label or is_graded_test(label):
                continue
            if any(
                skip in label
                for skip in ("عنصر", "رجوع", "توسيع الشريط الجانبي", "طيّ الشريط الجانبي", "طي الشريط")
            ):
                continue
            button_data.append((label, buttons.nth(row["i"])))
    else:
        for index in range(buttons.count()):
            button = buttons.nth(index)
            try:
                if button.is_disabled():
                    continue
                label = " ".join(button.inner_text(timeout=1000).split())
            except Exception:  # noqa: BLE001
                continue

            if not label or is_graded_test(label):
                continue
            if any(
                skip in label
                for skip in ("عنصر", "رجوع", "توسيع الشريط الجانبي", "طيّ الشريط الجانبي", "طي الشريط")
            ):
                continue
            button_data.append((label, button))

    if not button_data:
        # الشريط مطوي: افتح أول وحدة لسه ناقصة (مش بالضرورة الأولى)
        if _fallback_open(page, side):
            return True, current_group
        return False, current_group

    for label, button in button_data:
        if normalize_text(label) == normalize_text(current_group or ""):
            continue

        job = _job.get()
        count_match = re.search(r"(\d+)\s*/\s*(\d+)", label)
        if (
            (count_match and count_match.group(1) == count_match.group(2) and int(count_match.group(2)) > 0)
            or "100%" in label
        ):
            if label not in job["announced"] and label not in job["worked"]:
                job["announced"].add(label)
                say(label, "group_skipped")
            continue
        if "🔒" in label or "مقفل" in label:
            continue

        try:
            button.click(force=True, no_wait_after=True)
            page.wait_for_timeout(WAIT_SHORT)
        except Exception:  # noqa: BLE001
            continue

        if open_unfinished_leaf(label):
            return True, label

        job = _job.get()
        scan = job.get("scan") or {}
        if label not in job["worked"] and label not in job["announced"]:
            if scan.get("done") and not scan.get("locked"):
                job["announced"].add(label)
                say(label, "group_skipped")
        current_group = label

    # مفيش شغل في الأقسام المفتوحة → افتح الوحدة الناقصة التالية (لو فيه)
    if _fallback_open(page, side):
        return True, current_group
    return False, current_group


INTRO_BUTTON = re.compile(
    r"^(ابدأ|ابدأ الآن|يلا نبدأ|فهمت|تمام!?|حسنًا|حسنا|Got it|OK|Okay|Start|Let.?s go|Skip|تخطي|تخطّي|إغلاق|Close)$",
    re.IGNORECASE,
)


def dismiss_intro(page):
    """يغلق نافذة ترحيب/تعليمات ظاهرة (شائعة في أول دخول لحساب جديد)."""
    try:
        dialogs = page.get_by_role("dialog")
        if not dialogs.count():
            return False
        button = dialogs.last.get_by_role("button", name=INTRO_BUTTON)
        if button.count() and button.first.is_visible():
            button.first.click(force=True, no_wait_after=True)
            page.wait_for_timeout(WAIT_MEDIUM)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def describe_page(page):
    """وصف مختصر للصفحة الحالية (للتشخيص لما صفحة الدخول متظهرش زي المتوقع)."""
    try:
        title = page.title()
    except Exception:  # noqa: BLE001
        title = "?"
    try:
        inputs = [
            (el.get_attribute("type") or "text") + ":" + (el.get_attribute("placeholder") or "-")
            for el in page.locator("input").all()[:5]
        ]
    except Exception:  # noqa: BLE001
        inputs = []
    try:
        body = " ".join(page.locator("body").inner_text(timeout=3000).split())[:160]
    except Exception:  # noqa: BLE001
        body = "?"
    return f"url={page.url} | title={title} | inputs={inputs} | text={body}"


def login(page, username, password):
    page.goto(SITE_URL, wait_until="domcontentloaded", timeout=45000)
    say_debug("تم فتح صفحة تسجيل الدخول.")

    password_field = page.locator("input[type='password']").first
    try:
        password_field.wait_for(state="visible", timeout=40000)
    except PWTimeout:
        raise PageNotReady("صفحة الدخول مظهرتش زي المتوقع. " + describe_page(page))

    user_field = page.locator(
        "input:not([type='password']):not([type='hidden']):not([type='checkbox'])"
        ":not([type='radio']):not([type='submit']):not([type='button'])"
    ).first
    user_field.fill(username, timeout=15000)
    say_debug("تم إدخال كود الدخول.")
    password_field.fill(password, timeout=15000)
    say_debug("تم إدخال كلمة المرور.")

    submit = page.get_by_role("button", name=re.compile(r"Let.?s Go|تسجيل الدخول|دخول|Log ?in|Sign ?in", re.IGNORECASE))
    if submit.count():
        submit.first.click(no_wait_after=True, timeout=10000)
    else:
        password_field.press("Enter")
    say_debug("تم إرسال طلب تسجيل الدخول.")
    try:
        page.wait_for_url(re.compile(r".*/dashboard$"), timeout=20000)
    except PWTimeout:
        page.wait_for_timeout(1000)
    if "login" in page.url.lower():
        message = normalize_text(page.locator("body").inner_text())[-300:]
        raise LoginFailed(
            "لم تؤكد المنصة تسجيل الدخول. "
            f"تحقق من البيانات أو حالة الحساب. رسالة الصفحة: {message}"
        )
    say_debug("تم تسجيل الدخول.")


def _finance_card_action_index(candidates):
    continue_labels = {"كمل", "استكمل", "استكمال", "continue", "continue learning"}
    start_labels = {"ابدأ", "ابدا", "start"}
    for candidate in candidates:
        if not candidate["visible"]:
            continue
        label = normalize_text(candidate["label"])
        if label not in continue_labels | start_labels:
            continue
        ancestor_texts = [normalize_text(text) for text in candidate["ancestors"]]
        finance_card = next(
            (
                text
                for text in ancestor_texts
                if "الثقافة المالية" in text
                or "financial literacy" in text
                or ("ثقافة" in text and "مالية" in text)
            ),
            "",
        )
        if not finance_card or "global finance quest" in finance_card:
            continue
        return candidate["i"]
    return None


def _open_finance_card(page):
    """ينتظر زر بطاقة الثقافة المالية نفسها ثم يضغطه دون لمس بطاقة أخرى."""
    actions = page.locator("button, a, [role='button'], [role='link']")
    candidate_script = (
        "(els) => els.map((el, i) => { "
        "const ancestors = []; let node = el; "
        "for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) "
        "{ ancestors.push((node.innerText || '').trim()); } "
        "return { i, label: (el.innerText || el.getAttribute('aria-label') || '').trim(), "
        "ancestors, visible: (" + _VISIBLE_JS + ")(el) }; })"
    )
    deadline = time.monotonic() + 15
    while True:
        try:
            candidates = actions.evaluate_all(candidate_script)
        except PWError as exc:
            say_debug(f"تعذر فحص أزرار بطاقات الرئيسية ({type(exc).__name__}).")
            return False
        action_index = _finance_card_action_index(candidates)
        if action_index is not None:
            actions.nth(action_index).click(force=True, no_wait_after=True)
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            say_debug("لم يظهر زر «كمل» أو «ابدأ» داخل بطاقة الثقافة المالية.")
            return False
        page.wait_for_timeout(min(250, max(1, int(remaining * 1000))))


def _open_subject_continue_button(page, timeout=15000):
    """Open the course overview's continue-learning button or link."""
    actions = page.locator("button, a, [role='button'], [role='link']")
    continue_labels = {
        "كمل التعلم",
        "كمّل التعلم",
        "استكمل التعلم",
        "ابدأ التعلم",
        "continue learning",
        "resume learning",
    }
    deadline = time.monotonic() + timeout / 1000
    while True:
        try:
            candidates = actions.evaluate_all(
                "(els) => els.map((el, i) => ({ "
                "i, label: (el.innerText || el.getAttribute('aria-label') || '').trim(), "
                "visible: (" + _VISIBLE_JS + ")(el) }))"
            )
        except PWError as exc:
            say_debug(f"تعذر فحص زر متابعة الثقافة المالية ({type(exc).__name__}).")
            return False
        for candidate in candidates:
            if (
                candidate["visible"]
                and normalize_text(candidate["label"]) in continue_labels
            ):
                actions.nth(candidate["i"]).click(force=True, no_wait_after=True)
                return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        page.wait_for_timeout(min(250, max(1, int(remaining * 1000))))


def open_curriculum(page):
    page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)

    # قد يظهر زر «ابدأ» للحساب الجديد أو «كمل» للحساب الذي لديه تقدم.
    if "/dashboard" in page.url:
        if not _open_finance_card(page):
            raise PageNotReady("لم أجد زر بطاقة «الثقافة المالية» في الصفحة الرئيسية.")
        try:
            page.wait_for_url(re.compile(r".*/subjects/7(?:/curriculum)?/?$"), timeout=25000)
        except PWTimeout as exc:
            if not re.search(r"/subjects/7(?:/curriculum)?/?$", page.url):
                raise PageNotReady(
                    "تم الضغط على بطاقة «الثقافة المالية» لكن المنهج لم يفتح."
                ) from exc

    if "/curriculum" not in urlsplit(page.url).path:
        if not _open_subject_continue_button(page):
            raise PageNotReady("لم يظهر زر «كمل التعلم» في صفحة الثقافة المالية.")
        expected_path = urlsplit(TARGET_URL).path.rstrip("/")
        try:
            page.wait_for_url(re.compile(r".*" + re.escape(expected_path) + r"/?$"), timeout=25000)
        except PWTimeout as exc:
            if urlsplit(page.url).path.rstrip("/") != expected_path:
                raise PageNotReady(
                    "تم الضغط على «كمل التعلم» لكن قائمة الوحدات لم تفتح."
                ) from exc

    page.get_by_role("complementary").wait_for(timeout=30000)
    # ننتظر أي وحدة ظاهرة (مش بس الأولى)
    page.get_by_role(
        "button", name=re.compile(r"أساسيات المال|أساسيات الأعمال|التمويل الشخصي|ريادة")
    ).first.wait_for(timeout=25000)


def announce_plan(page):
    """يبلّغ الطالب: المخلّص قبل كده، اللي هيتكمّل، والمقفول (من الـ API، وإلا من الشريط الجانبي)."""
    job = _job.get()
    curriculum = fetch_curriculum(page)
    if curriculum:
        rows = summarize_curriculum(curriculum)
        locked = []
        for name, done, total, is_locked in rows:
            if is_locked:
                locked.append(name)
            elif total and done >= total:
                say(f"{name}|{done}|{total}", "plan_done")
            else:
                say(f"{name}|{done}|{total}", "plan_todo")
        if locked:
            say("، ".join(locked), "plan_locked")
        tests = [
            it.get("title", "")
            for unit in curriculum
            for _sec, it in _unit_items(unit)
            if _is_test_item(it) and not it.get("is_completed")
        ]
        if tests:
            say(f"{len(tests)}|" + "، ".join(" ".join(t.split())[:50] for t in tests[:5]), "plan_tests")
        return
    units, locked = _read_units(page)
    for unit in units:
        if unit["total"] and unit["done"] >= unit["total"]:
            job["announced"].add(unit["label"])
            say(f"{unit['name']}|{unit['done']}|{unit['total']}", "plan_done")
        else:
            say(f"{unit['name']}|{unit['done']}|{unit['total']}", "plan_todo")
    if locked:
        say("، ".join(locked), "plan_locked")


def _timed(fn):
    """يسجّل عدد مرات وزمن كل خطوة (يظهر في اللوج آخر الحل لتحديد أبطأ مرحلة)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            table = _timing.get()
            if table is not None:
                row = table.setdefault(fn.__name__, [0, 0.0])
                row[0] += 1
                row[1] += time.perf_counter() - started
    return wrapper


for _name in (
    "solve_one_visible_question", "answer_text_question_if_supported",
    "mark_review_points", "click_next", "open_next_available_activity",
    "dismiss_intro",
):
    globals()[_name] = _timed(globals()[_name])


def run_learning_flow(page, entries):
    completed = set()
    visited = set()
    handled_test_pages = set()
    current_activity_rechecks = set()
    current_group = None
    last_state = None
    stagnant_rounds = 0
    pending_test_completion = None
    retried_quiz_pages = set()

    for step in range(MAX_STEPS):
        check_cancel()

        test_hit = False
        result_hit = False
        try:
            ctx = current_context(page)
            full_text = main_content(page).inner_text(timeout=2000)
            page_text = visible_page_text(page, main_text=full_text)
            page_fingerprint = normalize_text(page_text)
            result_hit = is_quiz_result_page(page_text)
            test_hit = not result_hit and any(marker in full_text for marker in TEST_PAGE_MARKERS)
        except Exception:  # noqa: BLE001
            ctx = page.url
            page_fingerprint = ""

        state = (page.url, ctx, page_fingerprint)
        if state == last_state:
            stagnant_rounds += 1
            if stagnant_rounds >= 3:
                say_debug("الصفحة ثابتة لعدة جولات؛ توقفت لتجنب التكرار.")
                _job.get()["stuck"] = "الصفحة لم تتغير بعد عدة محاولات."
                return
        else:
            stagnant_rounds = 0
        last_state = state

        if pending_test_completion is not None:
            completed_key, completed_context = pending_test_completion
            if test_hit and (page.url, ctx) == completed_key:
                if (
                    has_resumable_quiz_attempt(page_text)
                    and completed_key not in retried_quiz_pages
                ):
                    retried_quiz_pages.add(completed_key)
                    handled_test_pages.discard(completed_key)
                    pending_test_completion = None
                    last_state = None
                    stagnant_rounds = 0
                    say_debug(f"المحاولة ما زالت قيد التنفيذ؛ سأستأنف الاختبار «{ctx}» مرة واحدة.")
                    continue
                message = f"صفحة الاختبار «{ctx}» بقيت ظاهرة بعد محاولة معالجتها؛ أوقفت التكرار."
                _job.get()["stuck"] = message
                say_debug(message)
                return
            job = _job.get()
            completed_source = (
                job.get("leaf")
                if is_graded_test(job.get("leaf") or "")
                else completed_context
            )
            completed_title = normalize_text(completed_source)
            job.setdefault("completed_tests", set()).add(completed_title)
            job["tests"] = [
                title
                for title in job.get("tests", [])
                if completed_title not in normalize_text(title)
            ]
            say(completed_context, "quiz_done")
            pending_test_completion = None

        if result_hit:
            say_debug("ظهرت نتيجة الاختبار؛ أرجع للمنهج وأفحص النشاط التالي.")
            if not return_to_curriculum(page):
                _job.get()["stuck"] = "ظهرت نتيجة الاختبار لكن تعذّر الرجوع إلى قائمة المنهج."
                return
            _job.get().get("opened_units", set()).clear()
            last_state = None
            stagnant_rounds = 0
            continue

        # صفحة اختبار؟
        if test_hit:
            job = _job.get()
            test_key = (page.url, ctx)
            if test_key in handled_test_pages:
                message = f"صفحة الاختبار «{ctx}» بقيت ظاهرة بعد محاولة معالجتها؛ أوقفت التكرار."
                job["stuck"] = message
                say_debug(message)
                return
            handled_test_pages.add(test_key)
            say(ctx, "quiz_solving")
            solved = solve_quiz(page, entries)
            if solved:
                pending_test_completion = (test_key, ctx)
                last_state = None
                stagnant_rounds = 0
                continue
            else:
                if (
                    has_resumable_quiz_attempt(page_text)
                    and test_key not in retried_quiz_pages
                ):
                    retried_quiz_pages.add(test_key)
                    handled_test_pages.discard(test_key)
                    last_state = None
                    stagnant_rounds = 0
                    continue
                # لا نعيد بدء/معالجة نفس صفحة الاختبار تلقائيًا بعد فشل المحاولة.
                job["test_reached"] = ctx
                say(ctx, "quiz_skipped")
                return

        # الأولوية القصوى: حل أي سؤال ظاهر
        if solve_one_visible_question(page, entries, completed):
            last_state = None
            stagnant_rounds = 0
            continue

        if answer_text_question_if_supported(page, entries, completed):
            last_state = None
            stagnant_rounds = 0
            continue

        # نقاط المراجعة (علّم كمراجع)
        if mark_review_points(page):
            last_state = None
            stagnant_rounds = 0
            continue

        # أزرار التالي / إكمال
        if click_next(page):
            last_state = None
            stagnant_rounds = 0
            continue

        # فتح نشاط جديد من الشريط الجانبي
        opened, current_group = open_next_available_activity(
            page, visited, current_group
        )
        if opened:
            last_state = None
            stagnant_rounds = 0
            continue

        if dismiss_intro(page):
            last_state = None
            stagnant_rounds = 0
            continue

        # لو وصلنا هنا ومفيش تقدم
        if _job.get().get("api_current") and not _job.get().get("stuck"):
            current = _job.get()["api_current"]
            recheck_key = (page.url, normalize_text(current))
            if recheck_key not in current_activity_rechecks:
                current_activity_rechecks.add(recheck_key)
                say_debug(f"النشاط «{current}» مفتوح بالفعل؛ أعيد فحصه بعد تمرير المحتوى.")
                scroll_quiz_page(page)
                page.wait_for_timeout(WAIT_MEDIUM)
                _job.get().pop("api_current", None)
                last_state = None
                stagnant_rounds = 0
                continue
            _job.get()["stuck"] = (
                f"النشاط «{current}» مفتوح، لكن لم يظهر سؤال أو زر متابعة مدعوم "
                "بعد إعادة فحص محتواه."
            )
            say_debug(_job.get()["stuck"])
            return
        pending = (_job.get().get("scan") or {}).get("pending", 0)
        if pending and not _job.get().get("stuck"):
            _job.get()["stuck"] = f"بقي {pending} نشاط مفتوح لم يكتمل على المنصة."
        say_debug("لا توجد خطوة آمنة مدعومة في الصفحة الحالية؛ توقفت دون تخمين.")
        say_debug("الصفحة الحالية:", page.url)
        say_debug("النشاط الحالي:", ctx)
        try:
            buttons = main_content(page).get_by_role("button")
            visible_btns = []
            for i in range(min(buttons.count(), 12)):
                b = buttons.nth(i)
                if b.is_visible():
                    visible_btns.append(" ".join(b.inner_text().split())[:70])
            say_debug("الأزرار الظاهرة:", visible_btns)
        except Exception:  # noqa: BLE001
            pass
        return

    raise RuntimeError(f"تجاوزت الحد الأقصى للخطوات ({MAX_STEPS}).")


def _attempt(username, password, entries, headless):
    """تشغيل واحد للمتصفح: دخول ثم حل. التقدم محفوظ على المنصة فإعادة التشغيل تكمل من حيث توقف."""
    page = None
    job = _job.get()
    job["diagnostic_screenshot"] = None
    job["diagnostic_page"] = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions",
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-default-apps",
                "--no-first-run",
            ],
        )
        try:
            # ── Proxy configuration ──────────────────────────────────────────
            proxy_config = None
            if PROXY_SERVER:
                proxy_config = {"server": PROXY_SERVER}
                if PROXY_USERNAME:
                    proxy_config["username"] = PROXY_USERNAME
                if PROXY_PASSWORD:
                    proxy_config["password"] = PROXY_PASSWORD
                say_debug(f"🌐 بيستخدم Proxy: {PROXY_SERVER}")
            # ─────────────────────────────────────────────────────────────────
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                viewport={"width": 1366, "height": 768},
                **({"proxy": proxy_config} if proxy_config else {}),
            )
            if BLOCK_ASSETS:
                context.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in ("image", "font", "media")
                    else route.continue_(),
                )
            page = context.new_page()
            login(page, username, password)
            job["diagnostic_page"] = page.url
            say("", "login_ok")
            check_cancel()
            open_curriculum(page)
            announce_plan(page)
            run_learning_flow(page, entries)
            job = _job.get()
            if not (job.get("test_reached") or job.get("tests") or job.get("stuck") or job.get("unmatched")):
                curriculum = fetch_curriculum(page)
                if curriculum:
                    pick = next_pending_item(curriculum)
                    left = [pick[3].get("title", "")] if pick and pick[0] == "item" else []
                else:
                    left = [
                        f"{u['name']} ({u['done']}/{u['total']})"
                        for u in _read_units(page)[0]
                        if not u["disabled"] and u["total"] and u["done"] < u["total"]
                    ]
                if left:
                    job["stuck"] = "لسه فيه وحدات ناقصة ومقدرتش أوصل لعناصرها: " + "، ".join(left)
            if (
                _report_consent.get()
                and (job.get("test_reached") or job.get("tests") or job.get("stuck") or job.get("unmatched"))
            ):
                job["diagnostic_page"] = page.url
                job["diagnostic_screenshot"] = _redacted_screenshot(page)
        except Exception:
            if _report_consent.get() and page is not None:
                job["diagnostic_page"] = page.url
                job["diagnostic_screenshot"] = _redacted_screenshot(page)
            raise
        finally:
            browser.close()


def run_job(username, password, entries, hook=None, cancel=None, headless=True, report_consent=False):
    """يشغّل مهمة كاملة لحساب واحد. يرجّع dict فيه status: finished/stopped/login_failed/error."""
    _hook.set(hook)
    _cancel.set(cancel)
    _report_consent.set(report_consent)
    _job.set({"leaf": None, "worked": set(), "announced": set(), "scan": None,
              "unmatched": [], "stuck": None, "tests": [], "test_reached": None,
              "completed_tests": set()})
    _timing.set({})

    def clean(text):
        text = str(text)
        for secret in (password, username):
            if secret:
                text = text.replace(secret, "***")
        return text

    def report(status, error, last):
        _send_diagnostic(
            _diagnostic_report(
                status,
                clean(error),
                last,
                _job.get().get("diagnostic_screenshot"),
                _job.get().get("diagnostic_page"),
            )
        )

    try:
        for attempt in range(RETRIES + 1):
            job = _job.get()
            job["unmatched"].clear()
            job["stuck"] = None
            try:
                _attempt(username, password, entries, headless)
                break
            except (PWTimeout, PageNotReady, PWError) as exc:
                if attempt >= RETRIES:
                    raise
                say_debug(f"انقطاع مؤقت ({type(exc).__name__}) — إعادة المحاولة {attempt + 1}/{RETRIES}")
                check_cancel()
                time.sleep(3)
        job = _job.get()
        table = _timing.get() or {}
        if table:
            parts = [f"{n}: {c} مرة / {t:.1f}ث" for n, (c, t) in sorted(table.items(), key=lambda kv: -kv[1][1])]
            say_debug("⏱️ زمن الخطوات → " + " | ".join(parts))
        if job["unmatched"]:
            shown = "\n".join("• " + q for q in job["unmatched"][:5])
            error = f"توقف الحل عند أسئلة لا يوجد لها تطابق مؤكد في بنك الإجابات:\n{shown}"
            report("stuck", error, job.get("leaf"))
            return {
                "status": "stuck",
                "error": error,
                "last": job.get("leaf"),
            }
        if job["stuck"]:
            report("stuck", job["stuck"], job.get("leaf"))
            return {"status": "stuck", "error": job["stuck"], "last": job.get("leaf")}
        if job.get("test_reached") or job.get("tests"):
            test = job.get("test_reached") or (job["tests"][0] if job["tests"] else "")
            report("tests_pending", f"لم يكتمل الاختبار: {test}", job.get("leaf"))
            return {"status": "tests_pending", "last": job.get("leaf"),
                    "test": test}
        return {"status": "finished", "last": job.get("leaf")}
    except Stopped:
        return {"status": "stopped", "last": _job.get().get("leaf")}
    except LoginFailed as exc:
        error = clean(exc)[:300]
        report("login_failed", error, _job.get().get("leaf"))
        return {"status": "login_failed", "error": error}
    except PageNotReady as exc:
        error = clean(exc)[:500]
        report("error", error, _job.get().get("leaf"))
        return {"status": "error", "error": error, "last": _job.get().get("leaf")}
    except PWTimeout as exc:
        error = "انتهت مهلة انتظار المنصة. " + clean(exc)[:200]
        report("error", error, _job.get().get("leaf"))
        return {"status": "error", "error": error, "last": _job.get().get("leaf")}
    except Exception as exc:  # noqa: BLE001
        error = clean(exc)[:300]
        report("error", error, _job.get().get("leaf"))
        return {"status": "error", "error": error, "last": _job.get().get("leaf")}


def main():
    """تشغيل محلي من سطر الأوامر (بدون بوت)."""
    answer_file = locate_answer_file()
    entries = parse_answer_file(answer_file)
    print(f"تم تحميل {len(entries)} سؤالًا من ملف الإجابات: {answer_file.name}")
    username = os.getenv("SITE_USER") or input("أدخل كود الدخول (ID): ").strip()
    password = os.getenv("SITE_PASS") or getpass.getpass("أدخل كلمة المرور: ")
    if not username or not password:
        raise ValueError("كود الدخول وكلمة المرور مطلوبان.")
    result = run_job(username, password, entries, headless=HEADLESS)
    print(result)


if __name__ == "__main__":
    main()
