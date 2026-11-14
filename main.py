"""
واجهة ويب لبوت حل الثقافة المالية — v3
FastAPI + WebSocket لمتابعة التقدم live
"""
import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import deque, defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

load_dotenv()

BOT_DIR = Path(os.getenv("BOT_DIR", "./finbot_v4_0")).resolve()
sys.path.insert(0, str(BOT_DIR))

import core            # noqa: E402  (من مجلد البوت)
import db as botdb     # noqa: E402

try:
    import pw_speed    # noqa: E402  (أدوات تسريع/قياس Playwright)
except Exception:      # pragma: no cover
    pw_speed = None

ANSWERS_FILE    = BOT_DIR / os.getenv("ANSWERS_FILE", "answers.txt")
MAX_JOBS        = int(os.getenv("MAX_JOBS", "2"))
MAX_QUEUE       = int(os.getenv("MAX_QUEUE", "10"))
JOB_TIMEOUT_MIN = int(os.getenv("JOB_TIMEOUT_MIN", "240"))
DB_PATH         = os.getenv("DB_PATH", str(BOT_DIR / "bot.db"))
# دومينات إضافية مسموح لها (مفصولة بفاصلة). فاضية = نفس الدومين بس.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
# شغّلها 1 على Railway/Nginx عشان نقرأ الـ IP الحقيقي من X-Forwarded-For
TRUST_PROXY     = os.getenv("TRUST_PROXY", "1") == "1"
# PROFILE_SLEEPS=1 يطبع بعد كل حل كام ثانية ضاعت في sleep وفي أنهي سطر (شغّله مع MAX_JOBS=1)
PROFILE_SLEEPS  = os.getenv("PROFILE_SLEEPS", "0") == "1"

KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
SID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


# ── Helpers ──────────────────────────────────────────────────────────

def make_uid(key: str) -> int:
    """معرّف ثابت بين كل مرات التشغيل (hash() بتاع بايثون بيتغير كل run)."""
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:12], 16) % (10 ** 9)


_AR = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

def ar_num(n):
    return f"{n:,}".replace(",", "٬").translate(_AR)

def format_duration(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{ar_num(h)}س {ar_num(m)}د"
    if m:
        return f"{ar_num(m)}د {ar_num(s)}ث"
    return f"{ar_num(s)}ث"


def client_ip(conn) -> str:
    if TRUST_PROXY:
        xff = conn.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[-1].strip()   # آخر عنصر = اللي أضافه البروكسي
    return conn.client.host if conn.client else "unknown"


def origin_ok(conn) -> bool:
    """يمنع صفحة من دومين تاني إنها تفتح WebSocket على السيرفر ده."""
    origin = conn.headers.get("origin")
    if not origin:                       # مش متصفح
        return True
    if origin in ALLOWED_ORIGINS:
        return True
    netloc = urlparse(origin).netloc
    return netloc in (conn.headers.get("host", ""), conn.headers.get("x-forwarded-host", ""))


class RateLimiter:
    """Sliding window بسيط في الذاكرة."""
    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self.hits) > 5000:        # تنظيف دوري
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] > self.window]:
                self.hits.pop(k, None)
        return True


ws_limiter      = RateLimiter(int(os.getenv("WS_RATE_LIMIT", "6")), 60)   # محاولات حل/دقيقة لكل IP
salawat_limiter = RateLimiter(200, 60)
api_limiter     = RateLimiter(60, 60)


def check_rate(limiter: RateLimiter, request: Request):
    if not limiter.allow(client_ip(request)):
        raise HTTPException(429, "طلبات كتير، استنى شوية")


def check_key(key: str):
    if not KEY_RE.match(key):
        raise HTTPException(400, "bad key")


def fetch_history(uid: int, limit: int = 10):
    """يفضّل دالة get_jobs في db.py (شوف README). لو مش موجودة يرجع للاستعلام المباشر."""
    fn = getattr(botdb, "get_jobs", None)
    if callable(fn):
        return fn(uid, limit)
    return botdb._conn.execute(
        "SELECT started, finished, status, questions, activities FROM jobs "
        "WHERE tg_id=? ORDER BY started DESC LIMIT ?", (uid, limit)
    ).fetchall()


# ── App ──────────────────────────────────────────────────────────────

_state = {
    "sem": None,
    "cancels": {},          # session_id -> threading.Event
    "queue": [],            # session_ids المنتظرة بالترتيب
    "active_users": set(),  # حسابات عليها جلسة شغالة (مفيش جلستين لنفس الحساب)
    "entries": [],
    "index": "",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not ANSWERS_FILE.exists():
        raise RuntimeError(f"ملف الإجابات مش موجود: {ANSWERS_FILE}")
    botdb.init(DB_PATH)
    _state["sem"] = asyncio.Semaphore(MAX_JOBS)
    _state["entries"] = core.parse_answer_file(ANSWERS_FILE)
    _state["index"] = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    if PROFILE_SLEEPS and pw_speed:
        pw_speed.profile_sleeps(core)
    yield


app = FastAPI(title="حل الثقافة المالية", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=800)
if ALLOWED_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith(("/api", "/cancel")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ── API endpoints ────────────────────────────────────────────────────

class UsernameBody(BaseModel):
    username: str = Field("", max_length=100)


@app.get("/api/salawat")
def get_salawat():
    return {"count": botdb.get_salawat()}


@app.post("/api/salawat")
def add_salawat(request: Request):
    check_rate(salawat_limiter, request)
    return {"count": botdb.add_salawat()}


@app.get("/api/username/{key}")
def get_username(key: str, request: Request):
    check_key(key)
    check_rate(api_limiter, request)
    return {"username": botdb.get_username(make_uid(key)) or ""}


@app.post("/api/username/{key}")
def save_username(key: str, body: UsernameBody, request: Request):
    check_key(key)
    check_rate(api_limiter, request)
    uid = make_uid(key)
    username = body.username.strip()
    botdb.touch_user(uid, key)
    if username:
        botdb.save_username(uid, username)
    return {"ok": True}


@app.get("/api/history/{key}")
def get_history(key: str, request: Request):
    check_key(key)
    check_rate(api_limiter, request)
    now = int(time.time())
    result = []
    for started, finished, status, q, a in fetch_history(make_uid(key), 10):
        result.append({
            "started":    started,
            "elapsed":    format_duration((finished or now) - (started or now)),
            "status":     status,
            "questions":  q,
            "activities": a,
        })
    return {"history": result}


@app.post("/cancel/{session_id}")
def cancel_job(session_id: str, request: Request):
    # الـ session_id عشوائي 128-bit فهو نفسه السر؛ ومن POST عشان مايتفعلش بالغلط من لينك
    if not SID_RE.match(session_id):
        raise HTTPException(400, "bad session")
    check_rate(api_limiter, request)
    ev = _state["cancels"].get(session_id)
    if ev:
        ev.set()
        return {"ok": True}
    return {"ok": False}


@app.get("/health")
def health():
    return {"ok": True, "running": len(_state["active_users"]) - len(_state["queue"]),
            "queued": len(_state["queue"])}


# ── WebSocket ────────────────────────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def ws_endpoint(ws: WebSocket, session_id: str):
    if not SID_RE.match(session_id) or not origin_ok(ws):
        await ws.close(code=1008)
        return
    await ws.accept()

    async def send(msg: dict):
        try:
            await ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass     # العميل قفل الصفحة — الحل بيكمل على السيرفر عادي

    if not ws_limiter.allow(client_ip(ws)):
        await send({"type": "error", "msg": "⚠️ محاولات كتير، استنى دقيقة وجرّب تاني."})
        return

    password = ""
    registered_user = None
    in_queue = False
    cancel = threading.Event()
    timed_out = threading.Event()
    timer = None
    ticker = None
    sem = _state["sem"]

    try:
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=20)
        except asyncio.TimeoutError:
            return
        if len(raw) > 4096:
            await send({"type": "error", "msg": "طلب غير صالح"})
            return
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            await send({"type": "error", "msg": "طلب غير صالح"})
            return

        username   = str(data.get("username", "")).strip()
        password   = str(data.get("password", "")).strip()
        client_key = str(data.get("client_key") or session_id)
        if not KEY_RE.match(client_key):
            client_key = session_id

        if not username or not password:
            await send({"type": "error", "msg": "الكود أو الباسورد فاضل!"})
            return
        if len(username) > 100 or len(password) > 200:
            await send({"type": "error", "msg": "البيانات طويلة بشكل غير طبيعي"})
            return

        ukey = username.lower()
        if ukey in _state["active_users"]:
            await send({"type": "error", "msg": "⚠️ فيه جلسة شغالة على الحساب ده بالفعل، استنى لما تخلص."})
            return
        if sem.locked() and len(_state["queue"]) >= MAX_QUEUE:
            await send({"type": "error", "msg": "⚠️ الطابور ممتلئ، جرّب بعد شوية."})
            return

        _state["active_users"].add(ukey)
        registered_user = ukey
        _state["cancels"][session_id] = cancel

        uid = make_uid(client_key)
        botdb.touch_user(uid, client_key)
        botdb.save_username(uid, username)

        # ── حالة التقدم (بتتحدث من thread الحل، فمحتاجة lock) ──
        t0    = time.monotonic()
        stats = {"questions": 0, "activities": 0}
        lines = deque(maxlen=14)
        lock  = threading.Lock()

        async def flush(header="⚙️ جاري الحل…", show_stop=True):
            with lock:
                snap, q, a = list(lines), stats["questions"], stats["activities"]
            await send({
                "type": "progress", "header": header, "lines": snap,
                "questions": q, "activities": a,
                "elapsed": format_duration(time.monotonic() - t0),
                "show_stop": show_stop,
            })

        def hook(kind, text):
            # بيتنادى من thread البوت: بنحدّث الحالة بس، والـ ticker هو اللي بيبعت
            # (رسالة كل ثانية بدل رسالة لكل سؤال)
            try:
                t = str(text or "")[:200]
                with lock:
                    if kind == "q_ok":
                        stats["questions"] += 1
                    elif kind == "done":
                        lines.append(f"✅ نشاط: {t}");  stats["activities"] += 1
                    elif kind == "quiz_done":
                        lines.append(f"✅ اختبار: {t}"); stats["activities"] += 1
                    elif kind == "group_done":
                        lines.append(f"✅ وحدة: {t}")
                    elif kind in ("plan_done", "group_skipped"):
                        lines.append(f"⏭️ مكتملة: {t}")
            except Exception:
                pass

        await send({"type": "started"})

        # ── الانتظار في الطابور (مع ترتيب حقيقي وإمكانية الإلغاء) ──
        acquired = False
        if sem.locked():
            _state["queue"].append(session_id)
            in_queue = True
        try:
            while True:
                try:
                    await asyncio.wait_for(sem.acquire(), timeout=3)
                    acquired = True
                    break
                except asyncio.TimeoutError:
                    if cancel.is_set():
                        break
                    pos = _state["queue"].index(session_id) + 1 if in_queue else 1
                    await flush(f"⏳ في الانتظار — رقمك {ar_num(pos)}")
        finally:
            if in_queue:
                in_queue = False
                if session_id in _state["queue"]:
                    _state["queue"].remove(session_id)

        if not acquired:
            await send({"type": "done", "status": "stopped",
                        "summary": {"questions": 0, "activities": 0,
                                    "elapsed": format_duration(time.monotonic() - t0), "last": "—"},
                        "error": "", "test": ""})
            return

        # ── التشغيل ──
        with lock:
            lines.append("🔐 جاري تسجيل الدخول…")
        await flush()

        async def _tick():
            while True:
                await asyncio.sleep(1.0)
                await flush()
        ticker = asyncio.create_task(_tick())

        def _on_timeout():
            timed_out.set()
            cancel.set()
        timer = threading.Timer(JOB_TIMEOUT_MIN * 60, _on_timeout)
        timer.daemon = True
        timer.start()

        started_ts = int(time.time())
        if PROFILE_SLEEPS and pw_speed:
            pw_speed.reset()
        try:
            result = await asyncio.to_thread(
                core.run_job, username, password, _state["entries"],
                hook, cancel, True, False,
            )
        except Exception as ex:
            result = {"status": "error", "error": str(ex)}
        finally:
            sem.release()

        def mask(s):
            s = str(s or "")
            return s.replace(password, "***")[:300] if password else s[:300]

        if PROFILE_SLEEPS and pw_speed:
            print(pw_speed.report(), flush=True)
        status = result.get("status", "error")
        if timed_out.is_set():
            status = "timeout"
        err_txt = mask(result.get("error"))
        with lock:
            q, a = stats["questions"], stats["activities"]
        summary = {
            "questions":  q,
            "activities": a,
            "elapsed":    format_duration(time.monotonic() - t0),
            "last":       mask(result.get("last")) or "—",
        }
        password = None
        botdb.log_job(uid, started_ts, status, q, a, err_txt)
        await send({"type": "done", "status": status, "summary": summary,
                    "error": err_txt, "test": mask(result.get("test"))})

    except Exception as ex:
        msg = str(ex)
        if password:
            msg = msg.replace(password, "***")
        await send({"type": "error", "msg": msg[:300]})
    finally:
        if ticker:
            ticker.cancel()
        if timer:
            timer.cancel()
        if in_queue and session_id in _state["queue"]:
            _state["queue"].remove(session_id)
        _state["cancels"].pop(session_id, None)
        if registered_user:
            _state["active_users"].discard(registered_user)


# ── Static ───────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_state["index"])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
