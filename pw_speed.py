"""
pw_speed.py — تسريع Playwright بأقل تعديل على core.py

التركيب (3 أماكن في core.py):

    import pw_speed

    # 1) عند تشغيل المتصفح
    browser = p.chromium.launch(headless=True, args=pw_speed.LAUNCH_ARGS)

    # 2) عند إنشاء الـ context
    context = browser.new_context(**pw_speed.context_opts())      # لو عندك خيارات: context_opts(viewport=...)

    # 3) بعد إنشاء الـ context مباشرة
    pw_speed.apply_speedups(context)            # sync
    # await pw_speed.apply_speedups_async(context)   # لو بتستخدم async API

بيعمل إيه:
  - يحجب الصور والفيديو والخطوط وسكربتات التتبع (analytics) — مش محتاجينها في الحل
  - يلغي الـ animations والـ transitions: Playwright بيستنى العنصر "يثبت" قبل أي ضغطة،
    فكل animation في الموقع بيتحول لانتظار
  - CSS مش بيتحجب افتراضياً (حجبه ممكن يكشف عناصر كانت مخفية ويلخبط الـ selectors):
    apply_speedups(context, block_css=True) لو جرّبته وشغال معاك

أداة القياس (بدون أي تعديل في core.py):
    PROFILE_SLEEPS=1 في الـ .env  ← main.py بيطبع تقرير بعد كل حل يوضح:
    كام ثانية ضاعت في time.sleep / page.wait_for_timeout وفي أنهي سطر بالظبط.
"""
import asyncio
import collections
import os
import sys
import threading
import time

# ── خيارات التشغيل ───────────────────────────────────────────────────

LAUNCH_ARGS = [
    "--disable-dev-shm-usage",          # يمنع انهيار المتصفح في Docker (/dev/shm صغير)
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--mute-audio",
    "--no-first-run",
    "--no-default-browser-check",
]


def context_opts(**overrides) -> dict:
    """خيارات new_context الآمنة. أي مفتاح تبعته بيغلب الافتراضي."""
    opts = {
        "service_workers": "block",     # مفيش caching/background من الموقع
        "reduced_motion": "reduce",     # لو الموقع بيحترم prefers-reduced-motion
    }
    opts.update(overrides)
    return opts


# ── الحجب ────────────────────────────────────────────────────────────

_BLOCK_TYPES = {"image", "media", "font"}
_BLOCK_HOSTS = (
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.net", "facebook.com/tr", "hotjar.com", "clarity.ms",
    "fonts.googleapis.com", "fonts.gstatic.com",
)

KILL_ANIM_JS = """
(() => {
  const css = '*,*::before,*::after{animation-duration:0s!important;animation-delay:0s!important;' +
              'transition-duration:0s!important;transition-delay:0s!important;scroll-behavior:auto!important}';
  const add = () => { const s = document.createElement('style'); s.textContent = css;
                      (document.head || document.documentElement).appendChild(s); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add); else add();
})();
"""


def should_block(resource_type: str, url: str, block_css: bool = False, extra=()) -> bool:
    if resource_type in _BLOCK_TYPES:
        return True
    if block_css and resource_type == "stylesheet":
        return True
    u = url.lower()
    return any(h in u for h in _BLOCK_HOSTS + tuple(extra))


def _sync_handler(block_css, extra):
    def handler(route):
        r = route.request
        if should_block(r.resource_type, r.url, block_css, extra):
            route.abort()
        else:
            route.continue_()
    return handler


def _async_handler(block_css, extra):
    async def handler(route):
        r = route.request
        if should_block(r.resource_type, r.url, block_css, extra):
            await route.abort()
        else:
            await route.continue_()
    return handler


def apply_speedups(context, block_css=False, extra_block=(), timeout_ms=None, nav_timeout_ms=None):
    """Sync API. timeout_ms/nav_timeout_ms اختياريين — مش بيتغيروا إلا لو حددتهم."""
    context.route("**/*", _sync_handler(block_css, extra_block))
    context.add_init_script(KILL_ANIM_JS)
    if timeout_ms:
        context.set_default_timeout(timeout_ms)
    if nav_timeout_ms:
        context.set_default_navigation_timeout(nav_timeout_ms)
    return context


async def apply_speedups_async(context, block_css=False, extra_block=(), timeout_ms=None, nav_timeout_ms=None):
    await context.route("**/*", _async_handler(block_css, extra_block))
    await context.add_init_script(KILL_ANIM_JS)
    if timeout_ms:
        context.set_default_timeout(timeout_ms)
    if nav_timeout_ms:
        context.set_default_navigation_timeout(nav_timeout_ms)
    return context


# ── قياس الانتظار (profiler) ─────────────────────────────────────────

_lock = threading.Lock()
_sleeps = collections.defaultdict(lambda: [0, 0.0])   # "file.py:123" -> [عدد, ثواني]
_t0 = time.monotonic()
_orig_sleep = time.sleep
_installed = False


def _caller_site() -> str:
    f = sys._getframe(1)
    while f is not None and f.f_code.co_filename == __file__:
        f = f.f_back
    return f"{os.path.basename(f.f_code.co_filename)}:{f.f_lineno}" if f else "?"


def _record(site: str, seconds: float):
    with _lock:
        rec = _sleeps[site]
        rec[0] += 1
        rec[1] += seconds


def _patched_sleep(secs):
    site = _caller_site()
    t = time.monotonic()
    _orig_sleep(secs)
    _record(site, time.monotonic() - t)


def profile_sleeps(*modules):
    """
    يبدأ يقيس كل time.sleep و page.wait_for_timeout.
    مرّر الموديولات اللي عملت فيها `from time import sleep` (زي core) عشان تتغطى كمان.
    """
    global _installed
    if _installed:
        return
    _installed = True
    time.sleep = _patched_sleep
    for m in modules:
        if getattr(m, "sleep", None) is _orig_sleep:
            m.sleep = _patched_sleep

    try:  # Playwright sync
        from playwright.sync_api import Page, Frame
        for cls in (Page, Frame):
            orig = cls.wait_for_timeout
            def make(orig):
                def wrapped(self, timeout):
                    site = _caller_site()
                    t = time.monotonic()
                    try:
                        return orig(self, timeout)
                    finally:
                        _record(site + " (wait_for_timeout)", time.monotonic() - t)
                return wrapped
            cls.wait_for_timeout = make(orig)
    except Exception:
        pass

    try:  # Playwright async
        from playwright.async_api import Page as APage, Frame as AFrame
        for cls in (APage, AFrame):
            orig = cls.wait_for_timeout
            def amake(orig):
                async def wrapped(self, timeout):
                    site = _caller_site()
                    t = time.monotonic()
                    try:
                        return await orig(self, timeout)
                    finally:
                        _record(site + " (wait_for_timeout)", time.monotonic() - t)
                return wrapped
            cls.wait_for_timeout = amake(orig)
    except Exception:
        pass


def reset():
    global _t0
    with _lock:
        _sleeps.clear()
        _t0 = time.monotonic()


def report(top: int = 8) -> str:
    wall = max(time.monotonic() - _t0, 1e-9)
    with _lock:
        items = sorted(_sleeps.items(), key=lambda kv: kv[1][1], reverse=True)
    total = sum(v[1] for _, v in items)
    lines = [f"⏱️  وقت الحل: {wall:.1f}ث | انتظار ثابت (sleep): {total:.1f}ث ({100 * total / wall:.0f}%)"]
    for site, (n, secs) in items[:top]:
        lines.append(f"   {site:<45} ×{n:<4} = {secs:6.1f}ث ({100 * secs / wall:.0f}%)")
    if not items:
        lines.append("   (مفيش sleeps متسجلة)")
    return "\n".join(lines)
