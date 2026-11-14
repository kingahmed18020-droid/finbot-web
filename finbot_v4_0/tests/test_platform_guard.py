"""اختبارات أوفلاين: حارس الاختبارات + مطابقة الخيارات + حالة tests_pending."""
import os
import sys
import asyncio
from io import BytesIO

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("BOT_TOKEN", "x")

import core  # noqa: E402
import bot  # noqa: E402


def _mock_curriculum_units(monkeypatch, is_ready=lambda _page: True):
    monkeypatch.setattr(
        core,
        "_read_units",
        lambda page: ([{"name": "unit"}], []) if is_ready(page) else ([], []),
    )


def test_graded_test_detection():
    assert core.is_graded_test("اختبار المعرفة: محاكاة حياة العمل - العمل والكسب والحياة")
    assert core.is_graded_test("اختبار المستوى 3")
    assert core.is_graded_test("Quiz 2")
    assert not core.is_graded_test("فهم النفقات")
    assert not core.is_graded_test("النقاط المهمة")
    assert not core.is_graded_test("مراجعة المفاهيم")


def test_resume_button_marks_an_active_quiz_page(monkeypatch):
    assert "استكمال الاختبار" in core.TEST_PAGE_MARKERS

    class _Content:
        def inner_text(self, timeout=None):
            return "اختبار المعرفة استكمال الاختبار"

    monkeypatch.setattr(core, "main_content", lambda page: _Content())
    assert core.on_test_page(object())


def test_quiz_result_is_not_mistaken_for_an_active_test():
    result = "النتيجة: 100% ناجح! رجوع للاختبار"
    assert core.is_quiz_result_page(result)
    assert core.is_quiz_result_page("النتيجة: 60% راسب — رجوع للاختبار")
    active_attempt = (
        "المحاولة رقم 1 جاري التنفيذ 10 الأسئلة 80% درجة النجاح "
        "ابدأ من جديد استكمال الاختبار"
    )
    assert core.has_resumable_quiz_attempt(active_attempt)
    assert not core.is_quiz_result_page(active_attempt)
    assert not core.is_quiz_result_page("درجة النجاح 80% ابدأ الاختبار")
    screenshot_result = (
        "السجل المحاولة رقم 1 أحسن 100% درجة النجاح 80% "
        "1 محاولات سابقة ابدأ الاختبار"
    )
    assert core.is_quiz_result_page(screenshot_result)
    attempt_history = (
        "المحاولة رقم 2 أحسن نتيجة 100% درجة النجاح 80% "
        "محاولات سابقة 2 ابدأ الاختبار"
    )
    assert core.is_quiz_result_page(attempt_history)
    assert core.is_quiz_result_page(
        "ابدأ الاختبار\nالأحسن المحاولة رقم 1\n١٢ نوفمبر، ٠٩:٣٢ م\n100%"
    )
    assert not core.is_quiz_result_page("10 الأسئلة 80% درجة النجاح ابدأ الاختبار")


def test_quiz_progress_counter_is_removed_before_answer_matching():
    question = (
        "بينما تبحث في خيارات التأمين، تتعلّم عن “التكافل”، آلية تعاضد "
        "تنتشر في مصر أيضًا. أي مما يلي يصف بأفضل شكل خاصية التكافل؟"
    )
    entries = core.parse_answer_file(core.locate_answer_file())

    prompt = core.clean_quiz_prompt("38 / 50\n" + question)
    answer, source = core.match_answer(prompt, entries)

    assert prompt == question
    assert source == question
    assert answer == "الدفعات تأتي من تعاضد المشتركين، والمتبقي يعود للمشتركين"


def test_answer_feedback_wait_returns_on_platform_feedback_or_times_out_safely():
    class _Page:
        def __init__(self, timeout=False):
            self.timeout = timeout
            self.script = None
            self.kwargs = None

        def wait_for_function(self, script, **kwargs):
            self.script = script
            self.kwargs = kwargs
            if self.timeout:
                raise core.PWTimeout("feedback not rendered")

    page = _Page()
    assert core.wait_for_answer_feedback(page, "question", timeout=250)
    assert 'count(text) > count(previousText || "")' in page.script
    assert page.kwargs == {"arg": {"previousText": "question"}, "timeout": 250}

    page = _Page(timeout=True)
    assert not core.wait_for_answer_feedback(page, "question", timeout=250)
    assert page.kwargs["timeout"] == 250


def test_visible_page_text_detects_result_controls_outside_main(monkeypatch):
    class _Text:
        def __init__(self, value):
            self.value = value

        def inner_text(self, timeout=None):
            return self.value

    class _Page:
        def locator(self, selector):
            assert selector == "body"
            return _Text(
                "ابدأ الاختبار\nالأحسن المحاولة رقم 1\n١٢ نوفمبر، ٠٩:٣٢ م\n100%"
            )

    monkeypatch.setattr(core, "main_content", lambda _page: _Text("الثقافة المالية"))
    text = core.visible_page_text(_Page())

    assert "ابدأ الاختبار" in text
    assert core.is_quiz_result_page(text)


def test_visible_page_text_reuses_main_text_when_supplied(monkeypatch):
    class _Body:
        def inner_text(self, timeout=None):
            return "نص الصفحة بالكامل"

    class _Page:
        def locator(self, selector):
            assert selector == "body"
            return _Body()

    def unexpected_main_read(_page):
        raise AssertionError("The already-read main content should be reused")

    monkeypatch.setattr(core, "main_content", unexpected_main_read)

    assert core.visible_page_text(_Page(), main_text="محتوى الدرس") == (
        "محتوى الدرس\nنص الصفحة بالكامل"
    )


def test_open_finance_card_waits_for_dashboard_actions():
    class _Button:
        def __init__(self):
            self.clicked = False

        def click(self, **kwargs):
            assert kwargs == {"force": True, "no_wait_after": True}
            self.clicked = True

    class _Actions:
        def __init__(self, page):
            self.page = page
            self.button = _Button()

        @property
        def first(self):
            return self

        def wait_for(self, **kwargs):
            assert kwargs == {"state": "visible", "timeout": 15000}
            self.page.loaded = True

        def evaluate_all(self, script):
            assert "ancestors" in script
            if not self.page.loaded:
                return []
            return [{
                "i": 0,
                "label": "كمل",
                "ancestors": ["كمل", "الثقافة المالية تعرّف على المال"],
                "visible": True,
            }]

        def nth(self, index):
            assert index == 0
            return self.button

    class _Page:
        def __init__(self):
            self.loaded = False
            self.actions = _Actions(self)

        def locator(self, selector):
            assert selector == "button, a, [role='button'], [role='link']"
            return self.actions

        def wait_for_timeout(self, _duration):
            self.loaded = True

    page = _Page()
    assert core._open_finance_card(page)
    assert page.loaded
    assert page.actions.button.clicked


def test_finance_card_action_requires_the_card_not_a_shared_dashboard_parent():
    candidates = [
        {
            "i": 0,
            "label": "كمل",
            "ancestors": ["كمل", "Global Finance Quest", "الثقافة المالية Global Finance Quest"],
            "visible": True,
        },
        {
            "i": 1,
            "label": "كمل",
            "ancestors": ["كمل", "الثقافة المالية"],
            "visible": True,
        },
        {
            "i": 2,
            "label": "ابدأ",
            "ancestors": ["ابدأ", "Global Finance Quest"],
            "visible": True,
        },
        {
            "i": 3,
            "label": "زر آخر",
            "ancestors": ["زر آخر", "الثقافة المالية"],
            "visible": True,
        },
    ]

    assert core._finance_card_action_index(candidates) == 1


def test_open_finance_card_skips_other_cards_and_selects_its_continue_link():
    class _Action:
        def __init__(self):
            self.clicked = False

        def click(self, **kwargs):
            self.clicked = True

    class _Actions:
        def __init__(self):
            self.items = [_Action(), _Action(), _Action()]

        @property
        def first(self):
            return self

        def wait_for(self, **kwargs):
            assert kwargs == {"state": "visible", "timeout": 15000}

        def evaluate_all(self, _script):
            return [
                {
                    "i": 0,
                    "label": "ابدأ",
                    "ancestors": ["ابدأ", "Global Finance Quest لعبة"],
                    "visible": True,
                },
                {
                    "i": 1,
                    "label": "كمل",
                    "ancestors": ["كمل", "مادة أخرى"],
                    "visible": True,
                },
                {
                    "i": 2,
                    "label": "كمل",
                    "ancestors": [
                        "كمل",
                        "الثقافة المالية",
                        "الثقافة المالية Global Finance Quest",
                    ],
                    "visible": True,
                },
            ]

        def nth(self, index):
            return self.items[index]

    class _Page:
        def __init__(self):
            self.actions = _Actions()

        def locator(self, selector):
            assert selector == "button, a, [role='button'], [role='link']"
            return self.actions

    page = _Page()
    assert core._open_finance_card(page)
    assert not page.actions.items[0].clicked
    assert not page.actions.items[1].clicked
    assert page.actions.items[2].clicked


def test_subject_overview_continue_button_accepts_arabic_label_and_link():
    class _Action:
        clicked = False

        def click(self, **kwargs):
            assert kwargs == {"force": True, "no_wait_after": True}
            self.clicked = True

    action = _Action()

    class _Actions:
        @property
        def first(self):
            return self

        def evaluate_all(self, _script):
            return [
                {"i": 0, "label": "كمل التعلم", "visible": True},
                {"i": 1, "label": "المنهج الدراسي", "visible": True},
            ]

        def nth(self, index):
            assert index == 0
            return action

    class _Page:
        def locator(self, selector):
            assert selector == "button, a, [role='button'], [role='link']"
            return _Actions()

    assert core._open_subject_continue_button(_Page())
    assert action.clicked


def test_open_curriculum_does_not_guess_a_dashboard_card(monkeypatch):
    class _Page:
        def __init__(self):
            self.url = "https://example.test/dashboard"
            self.visited = []

        def goto(self, url, **kwargs):
            self.visited.append(url)

    page = _Page()
    monkeypatch.setattr(core, "_open_finance_card", lambda _page: False)

    try:
        core.open_curriculum(page)
    except core.PageNotReady as exc:
        assert "الثقافة المالية" in str(exc)
    else:
        raise AssertionError("Expected the dashboard card lookup to fail explicitly")

    assert page.visited == [core.TARGET_URL]


def test_return_to_curriculum_uses_subject_overview_continue_button(monkeypatch):
    class _Empty:
        def count(self):
            return 0

    class _Page:
        def __init__(self):
            self.url = "https://example.test/quiz/result"

        def get_by_role(self, *_args, **_kwargs):
            return _Empty()

        def get_by_text(self, *_args, **_kwargs):
            return _Empty()

        def locator(self, _selector):
            return _Empty()

        def go_back(self, **_kwargs):
            self.url = "https://example.test/subjects/7"

        def wait_for_timeout(self, _duration):
            pass

    page = _Page()
    monkeypatch.setattr(core, "visible_page_text", lambda _page: "ملخص الثقافة المالية")
    _mock_curriculum_units(
        monkeypatch,
        lambda current_page: current_page.url.endswith("/curriculum"),
    )
    clicked = []

    def continue_learning(current_page, timeout):
        assert current_page.url.endswith("/subjects/7")
        clicked.append(timeout)
        current_page.url = core.TARGET_URL
        return True

    monkeypatch.setattr(core, "_open_subject_continue_button", continue_learning)

    assert core.return_to_curriculum(page)
    assert page.url == core.TARGET_URL
    assert clicked == [15000]


def test_progress_message_shows_completed_tasks_only_and_deduplicates():
    progress = bot.Progress(None, 1, 1)
    progress.handle("open", "درس قيد التنفيذ")
    progress.handle("quiz_solving", "اختبار قيد التنفيذ")
    progress.handle("plan_todo", "وحدة|1|5")
    progress.handle("plan_locked", "وحدة مقفولة")
    progress.handle("done", "درس مكتمل")
    progress.handle("done", "درس مكتمل")
    progress.handle("quiz_done", "اختبار مكتمل")

    rendered = progress.render()
    assert "درس مكتمل" in rendered
    assert "اختبار مكتمل" in rendered
    assert "قيد التنفيذ" not in rendered
    assert "باقي" not in rendered
    assert "مقفولة" not in rendered
    assert rendered.count("درس مكتمل") == 1
    assert progress.activities == 2


def test_duration_and_success_message_are_clear_and_escape_activity_names():
    assert bot.format_duration(45) == "٤٥ ث"
    assert bot.format_duration(125) == "٢ د ٠٥ ث"
    assert bot.format_duration(3661) == "١ س ٠١ د ٠١ ث"

    message = bot.completion_message(14, 3, 125, "درس <مكتمل>")

    assert "اكتملت المهمة بنجاح" in message
    assert "لم يتبق نشاط مفتوح متاح" in message
    assert "الأسئلة المجابة: <b>١٤</b>" in message
    assert "الأنشطة المكتملة: <b>٣</b>" in message
    assert "إجمالي الوقت: <b>٢ د ٠٥ ث</b>" in message
    assert "درس &lt;مكتمل&gt;" in message
    assert "قد تظل أنشطة مقفولة" in message


def test_progress_message_includes_elapsed_time_and_activity_sections():
    progress = bot.Progress(None, 1, 1)
    rendered = progress.render()

    assert "الأنشطة المكتملة" in rendered
    assert "الأسئلة المجابة" in rendered
    assert "الوقت المنقضي" in rendered
    assert "آخر التحديثات" in rendered


def test_mark_review_points_clicks_unmarked_cards_and_scrolls_them():
    class _Card:
        def __init__(self, content):
            self.content = content
            self.marked = False
            self.scrolled = False

        def is_visible(self):
            return True

        def get_attribute(self, name):
            if name == "aria-pressed":
                return str(self.marked).lower()
            return None

        def scroll_into_view_if_needed(self):
            self.scrolled = True

        def click(self, **kwargs):
            self.marked = not self.marked
            self.content.done += 1 if self.marked else -1

    class _Cards:
        def __init__(self, cards):
            self.cards = cards

        def count(self):
            return len(self.cards)

        @property
        def first(self):
            return self.cards[0]

        def nth(self, index):
            return self.cards[index]

    class _Content:
        def __init__(self):
            self.done = 0
            self.cards = [_Card(self) for _ in range(4)]

        def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return self

        def get_by_role(self, role, name):
            assert role == "button"
            if name == core.REVIEW_BUTTON:
                return _Cards([])
            assert name == core.REVIEW_CARD_BUTTON
            return _Cards(self.cards)

        def inner_text(self, timeout=None):
            return f"{self.done}/4 متعلّم"

    class _Page:
        def __init__(self, content):
            self.content = content
            self.waits = []

        def locator(self, selector):
            assert selector == "main"
            return self.content

        def wait_for_timeout(self, duration):
            self.waits.append(duration)

    content = _Content()
    page = _Page(content)

    while core.mark_review_points(page):
        pass

    assert content.done == 4
    assert all(card.marked for card in content.cards)
    assert all(card.scrolled for card in content.cards)


def test_return_to_curriculum_reloads_when_result_is_on_curriculum_url(monkeypatch):
    class _Waiter:
        def wait_for(self, **kwargs):
            assert kwargs == {"timeout": 15000}

    class _Page:
        def __init__(self):
            self.url = "https://example.test/subjects/7/curriculum"
            self.result_visible = True
            self.visited = []

        def goto(self, url, **kwargs):
            self.visited.append(url)
            self.url = url
            self.result_visible = False

        def get_by_role(self, role):
            assert role == "complementary"
            return _Waiter()

    page = _Page()
    monkeypatch.setattr(
        core,
        "main_content",
        lambda current_page: _FakeContent(
            "النتيجة: 100% ناجح! رجوع للاختبار"
            if current_page.result_visible
            else "المنهج"
        ),
    )
    _mock_curriculum_units(monkeypatch, lambda current_page: not current_page.result_visible)

    assert core.return_to_curriculum(page)
    assert page.visited == [core.TARGET_URL]


def test_return_to_curriculum_uses_visible_back_control_first(monkeypatch):
    class _Waiter:
        def wait_for(self, **kwargs):
            assert kwargs == {"timeout": 15000}

    class _BackControl:
        def __init__(self, page):
            self.page = page

        def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return self

        def is_visible(self):
            return True

        def click(self, **kwargs):
            assert kwargs == {"force": True, "no_wait_after": True}
            self.page.back_clicked = True
            self.page.result_visible = False
            self.page.url = core.TARGET_URL

    class _Page:
        def __init__(self):
            self.result_visible = True
            self.back_clicked = False
            self.waits = []
            self.visited = []
            self.url = "https://example.test/quiz/result"

        def get_by_role(self, role, name=None):
            if role == "button":
                assert name.pattern == r"^\s*رجوع\s*$"
                return _BackControl(self)
            assert role == "complementary"
            return _Waiter()

        def wait_for_timeout(self, duration):
            self.waits.append(duration)

    page = _Page()
    monkeypatch.setattr(
        core,
        "main_content",
        lambda current_page: _FakeContent(
            "النتيجة: 100% ناجح! رجوع للاختبار"
            if current_page.result_visible
            else "قائمة الفصول"
        ),
    )
    _mock_curriculum_units(monkeypatch, lambda current_page: not current_page.result_visible)

    assert core.return_to_curriculum(page)
    assert page.back_clicked
    assert not page.visited


def test_return_to_curriculum_clicks_nonsemantic_back_control(monkeypatch):
    class _Empty:
        def count(self):
            return 0

    class _BackControl:
        def __init__(self, page):
            self.page = page

        def is_visible(self):
            return True

        def inner_text(self, timeout=None):
            return "رجوع"

        def get_attribute(self, name):
            return None

        def scroll_into_view_if_needed(self, **kwargs):
            pass

        def click(self, **kwargs):
            self.page.url = core.TARGET_URL
            self.page.result_visible = False

    class _Controls:
        def __init__(self, page):
            self.page = page

        def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return _BackControl(self.page)

    class _Page:
        def __init__(self):
            self.url = "https://example.test/quiz/result"
            self.result_visible = True

        def get_by_role(self, *_args, **_kwargs):
            return _Empty()

        def get_by_text(self, *_args, **_kwargs):
            return _Empty()

        def locator(self, selector):
            assert selector == "button, a, [role='button'], [role='link'], [onclick]"
            return _Controls(self)

    page = _Page()
    monkeypatch.setattr(
        core,
        "main_content",
        lambda current_page: _FakeContent(
            "النتيجة: 100% ناجح! رجوع للاختبار"
            if current_page.result_visible
            else "قائمة الفصول"
        ),
    )
    _mock_curriculum_units(monkeypatch, lambda current_page: not current_page.result_visible)

    assert core.return_to_curriculum(page)
    assert page.url == core.TARGET_URL


def test_return_to_curriculum_reloads_when_result_content_survives_navigation(monkeypatch):
    class _Empty:
        def count(self):
            return 0

    class _Page:
        def __init__(self):
            self.url = "https://example.test/quiz/result"
            self.result_visible = True
            self.visited = []
            self.reloads = 0
            self.clock = 0.0

        def get_by_role(self, _role, name=None):
            return _Empty()

        def get_by_text(self, _name):
            return _Empty()

        def goto(self, url, **kwargs):
            self.visited.append(url)
            self.url = url

        def wait_for_timeout(self, duration):
            self.clock += duration / 1000

        def reload(self, **kwargs):
            self.reloads += 1
            self.result_visible = False

    page = _Page()
    monkeypatch.setattr(core.time, "monotonic", lambda: page.clock)
    monkeypatch.setattr(
        core,
        "main_content",
        lambda current_page: _FakeContent(
            "النتيجة: 100% ناجح! رجوع للاختبار"
            if current_page.result_visible
            else "قائمة الفصول"
        ),
    )
    _mock_curriculum_units(monkeypatch, lambda current_page: not current_page.result_visible)

    assert core.return_to_curriculum(page)
    assert page.visited == [core.TARGET_URL]
    assert page.reloads == 1


def test_return_to_curriculum_requires_the_chapter_sidebar(monkeypatch):
    class _Empty:
        def count(self):
            return 0

    class _Page:
        def __init__(self):
            self.url = "https://example.test/quiz/result"
            self.sidebar_ready = False
            self.reloads = 0
            self.clock = 0.0

        def get_by_role(self, *_args, **_kwargs):
            return _Empty()

        def get_by_text(self, *_args, **_kwargs):
            return _Empty()

        def locator(self, _selector):
            return _Empty()

        def goto(self, url, **_kwargs):
            self.url = url

        def wait_for_timeout(self, duration):
            self.clock += duration / 1000

        def reload(self, **_kwargs):
            self.reloads += 1
            self.sidebar_ready = True

    page = _Page()
    monkeypatch.setattr(core.time, "monotonic", lambda: page.clock)
    monkeypatch.setattr(core, "visible_page_text", lambda _page: "محتوى غير مكتمل")
    _mock_curriculum_units(monkeypatch, lambda current_page: current_page.sidebar_ready)

    assert core.return_to_curriculum(page)
    assert page.reloads == 1


def test_learning_flow_returns_from_quiz_result_and_checks_curriculum(monkeypatch):
    class _Waiter:
        def wait_for(self, **kwargs):
            assert kwargs == {"timeout": 15000}

    class _ResultPage:
        def __init__(self):
            self.url = "https://example.test/quiz/take"
            self.visited = []

        def goto(self, url, **kwargs):
            self.visited.append(url)
            self.url = url

        def get_by_role(self, role):
            assert role == "complementary"
            return _Waiter()

    result_text = (
        "المحاولة رقم 2 أحسن نتيجة 100% درجة النجاح 80% "
        "محاولات سابقة 2 ابدأ الاختبار"
    )
    page = _ResultPage()
    monkeypatch.setattr(
        core,
        "main_content",
        lambda _page: _FakeContent(
            result_text
            if "/quiz/result" in _page.url
            else "اختبار المعرفة استكمال الاختبار"
            if "/quiz/" in _page.url
            else "المنهج"
        ),
    )
    _mock_curriculum_units(monkeypatch)
    monkeypatch.setattr(
        core,
        "current_context",
        lambda _page: (
            "محاكاة حياة العمل"
            if "/quiz/" in _page.url
            else "المنهج"
        ),
    )
    monkeypatch.setattr(
        core,
        "solve_quiz",
        lambda current_page, _entries: (
            setattr(current_page, "url", "https://example.test/quiz/result") or True
        ),
    )
    monkeypatch.setattr(core, "solve_one_visible_question", lambda *args: False)
    monkeypatch.setattr(core, "answer_text_question_if_supported", lambda *args: False)
    monkeypatch.setattr(core, "mark_review_points", lambda *args: False)
    monkeypatch.setattr(core, "click_next", lambda *args: False)
    opened_items = []

    def open_next_from_curriculum(current_page, *_args):
        if "/curriculum" in current_page.url and not opened_items:
            opened_items.append("فصل غير مكتمل")
            return True, "الوحدة التالية"
        return False, "الوحدة التالية"

    monkeypatch.setattr(
        core, "open_next_available_activity", open_next_from_curriculum
    )
    monkeypatch.setattr(core, "dismiss_intro", lambda *args: False)
    core._hook.set(lambda *args: None)
    core._job.set({
        "leaf": None,
        "worked": set(),
        "announced": set(),
        "scan": None,
        "unmatched": [],
        "stuck": None,
        "tests": [],
        "test_reached": None,
        "completed_tests": set(),
        "opened_units": {"الوحدة الحالية"},
    })

    core.run_learning_flow(page, [])

    assert page.visited == [core.TARGET_URL]
    assert "/curriculum" in page.url
    assert core._job.get()["completed_tests"] == {"محاكاة حياة العمل"}
    assert core._job.get()["opened_units"] == set()
    assert opened_items == ["فصل غير مكتمل"]


def test_start_or_resume_quiz_prefers_existing_attempt():
    class _Button:
        def __init__(self, visible):
            self.visible = visible
            self.clicked = False

        def count(self):
            return 1 if self.visible else 0

        @property
        def first(self):
            return self

        def nth(self, index):
            assert index == 0
            return self

        def is_visible(self):
            return self.visible

        def click(self, **kwargs):
            self.clicked = True

    class _Content:
        def __init__(self):
            self.buttons = {
                "استكمال الاختبار": _Button(True),
                "ابدأ الاختبار": _Button(True),
            }

        def get_by_role(self, role, name):
            assert role == "button"
            for label, button in self.buttons.items():
                if name.search(label):
                    return button
            raise AssertionError(f"Unexpected quiz button pattern: {name.pattern}")

    class _Page:
        def __init__(self):
            self.waits = []

        def wait_for_timeout(self, duration):
            self.waits.append(duration)

    content = _Content()
    page = _Page()

    assert core.start_or_resume_quiz(page, content)
    assert content.buttons["استكمال الاختبار"].clicked
    assert not content.buttons["ابدأ الاختبار"].clicked
    assert page.waits == [core.WAIT_QUIZ]


def test_start_quiz_accepts_button_with_icon_and_skips_hidden_matches():
    class _Button:
        def __init__(self, visible):
            self.visible = visible
            self.clicked = False

        def is_visible(self):
            return self.visible

        def click(self, **kwargs):
            self.clicked = True

    class _Buttons:
        def __init__(self, buttons):
            self.buttons = buttons

        def count(self):
            return len(self.buttons)

        def nth(self, index):
            return self.buttons[index]

    class _Content:
        def __init__(self):
            self.resume = _Buttons([])
            self.start = _Buttons([_Button(False), _Button(True)])

        def get_by_role(self, role, name):
            assert role == "button"
            if name == core.QUIZ_RESUME_BUTTON:
                return self.resume
            assert name == core.QUIZ_START_BUTTON
            assert name.search("ابدأ الاختبار ▶")
            return self.start

        def get_by_text(self, pattern):
            return _Buttons([])

    class _Page:
        def __init__(self):
            self.waits = []
            self.transition_waits = []
            self.timeout_transition = False

        def evaluate(self, script):
            return False

        def wait_for_function(self, script, **kwargs):
            self.transition_waits.append((script, kwargs))
            if self.timeout_transition:
                raise core.PWTimeout("question did not change")

        def get_by_role(self, role, name):
            assert role == "button"
            return _Buttons([])

        def wait_for_timeout(self, duration):
            self.waits.append(duration)

    content = _Content()
    page = _Page()
    assert core.start_or_resume_quiz(page, content)
    assert not content.start.buttons[0].clicked
    assert content.start.buttons[1].clicked
    assert page.waits == [core.WAIT_QUIZ]


def test_start_quiz_falls_back_to_clickable_text_when_button_role_is_missing():
    class _Button:
        def __init__(self):
            self.clicked = False

        def is_visible(self):
            return True

        def click(self, **kwargs):
            self.clicked = True

    class _Matches:
        def __init__(self, items):
            self.items = items

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class _Content:
        def __init__(self):
            self.start_text = _Button()

        def get_by_role(self, role, name):
            assert role == "button"
            return _Matches([])

        def get_by_text(self, pattern):
            if pattern == core.QUIZ_START_BUTTON:
                return _Matches([self.start_text])
            return _Matches([])

    class _Page:
        def __init__(self):
            self.waits = []

        def wait_for_timeout(self, duration):
            self.waits.append(duration)

    content = _Content()
    page = _Page()
    assert core.start_or_resume_quiz(page, content)
    assert content.start_text.clicked
    assert page.waits == [core.WAIT_QUIZ]


def test_quiz_question_position_accepts_ascii_and_arabic_digits():
    assert core.quiz_question_position("السؤال 10 من 10") == (10, 10)
    assert core.quiz_question_position("السؤال ٨ من ١٠") == (8, 10)
    assert core.quiz_question_position("بدون مؤشر سؤال") is None


def test_quiz_advances_or_requires_final_question():
    class _Button:
        def __init__(self, enabled, text=""):
            self.enabled = enabled
            self.text = text
            self.clicked = False

        def is_visible(self):
            return True

        def is_enabled(self):
            return self.enabled

        def inner_text(self, timeout=None):
            return self.text

        def scroll_into_view_if_needed(self, **kwargs):
            self.scrolled = True

        def click(self, **kwargs):
            self.clicked = True

    class _Buttons:
        def __init__(self, buttons):
            self.buttons = buttons

        def count(self):
            return len(self.buttons)

        def nth(self, index):
            return self.buttons[index]

        @property
        def first(self):
            return self.buttons[0]

    class _Content:
        def __init__(self, next_buttons, submit_buttons=None):
            self.next_buttons = _Buttons(next_buttons)
            self.submit_buttons = _Buttons(submit_buttons or [])

        def get_by_role(self, role, name):
            assert role == "button"
            if name == core.QUIZ_NEXT_BUTTON:
                return self.next_buttons
            assert name == core.QUIZ_SUBMIT_BUTTON
            return self.submit_buttons

    class _Page:
        def __init__(self):
            self.waits = []
            self.transition_waits = []
            self.timeout_transition = False

        def evaluate(self, script):
            return False

        def wait_for_function(self, script, **kwargs):
            self.transition_waits.append((script, kwargs))
            if self.timeout_transition:
                raise core.PWTimeout("question did not change")

        def get_by_role(self, role, name):
            assert role == "button"
            return _Buttons([])

        def wait_for_timeout(self, duration):
            self.waits.append(duration)

    pagination_next = _Button(True, "")
    question_next = _Button(True, "← التالي")
    page = _Page()
    content = _Content([pagination_next, question_next])
    assert core.advance_quiz_question(page, content, "السؤال 2 من 10") == "advanced"
    assert question_next.clicked
    assert question_next.scrolled
    assert not pagination_next.clicked
    assert not page.waits
    assert page.transition_waits[0][1]["timeout"] == 1500

    page = _Page()
    page.timeout_transition = True
    blocked_button = _Button(True, "التالي")
    assert core.advance_quiz_question(
        page, _Content([blocked_button]), "السؤال 2 من 10"
    ) == "blocked"
    assert blocked_button.clicked

    disabled_next = _Button(False, "التالي")
    submit = _Button(True, "تقديم الاختبار")
    content = _Content([disabled_next], [submit])
    assert core.advance_quiz_question(_Page(), content, "السؤال 50") == "last"

    content = _Content([disabled_next])
    assert core.advance_quiz_question(_Page(), content, "السؤال 10 من 10") == "last"
    assert core.advance_quiz_question(_Page(), content, "السؤال 2 من 10") == "blocked"


def test_advance_quiz_scrolls_until_next_button_is_rendered():
    class _Button:
        def __init__(self):
            self.clicked = False

        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def inner_text(self, timeout=None):
            return "التالي"

        def scroll_into_view_if_needed(self, **kwargs):
            self.scrolled = True

        def click(self, **kwargs):
            self.clicked = True

    class _Buttons:
        def __init__(self, page, button):
            self.page = page
            self.button = button

        def count(self):
            return 1 if self.page.scrolls >= 2 else 0

        def nth(self, index):
            assert index == 0
            return self.button

    class _Page:
        def __init__(self, button):
            self.scrolls = 0
            self.waits = []
            self.transition_waits = []
            self.button = button
            self.buttons = _Buttons(self, button)

        def evaluate(self, script):
            assert script == core.QUIZ_SCROLL_JS
            self.scrolls += 1
            return True

        def get_by_role(self, role, name):
            assert role == "button"
            if name != core.QUIZ_NEXT_BUTTON:
                return _Buttons(self, self.button)
            return self.buttons

        def wait_for_function(self, _script, **kwargs):
            self.transition_waits.append(kwargs)

        def wait_for_timeout(self, duration):
            self.waits.append(duration)

    class _Content:
        def __init__(self, page):
            self.page = page

        def get_by_role(self, role, name):
            return self.page.get_by_role(role, name)

    button = _Button()
    page = _Page(button)
    assert core.advance_quiz_question(page, _Content(page), "السؤال 1 من 50") == "advanced"
    assert page.scrolls == 2
    assert button.clicked
    assert page.waits == [core.WAIT_SHORT, core.WAIT_SHORT]
    assert page.transition_waits[0]["timeout"] == 1500


def test_find_quiz_button_scrolls_offscreen_control_without_page_scrolling():
    class _Button:
        def __init__(self):
            self.scrolled_into_view = False

        def is_visible(self):
            return True

        def scroll_into_view_if_needed(self, **kwargs):
            self.scrolled_into_view = True

    class _Buttons:
        def __init__(self, button):
            self.button = button

        def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return self.button

    class _Page:
        def get_by_role(self, role, name):
            assert role == "button"
            return _Buttons(button)

        def evaluate(self, script):
            raise AssertionError("Should target-scroll the button instead of scrolling the page")

    class _Content:
        def get_by_role(self, role, name):
            assert role == "button"
            return _Buttons(button)

    button = _Button()
    assert core.find_quiz_button(_Page(), _Content(), core.QUIZ_NEXT_BUTTON) is button
    assert button.scrolled_into_view


def test_submit_quiz_scrolls_until_submit_button_is_rendered(monkeypatch):
    class _Button:
        def __init__(self, page):
            self.page = page
            self.clicked = False

        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def scroll_into_view_if_needed(self, **kwargs):
            self.scrolled = True

        def click(self, **kwargs):
            self.clicked = True
            self.page.result_text = "النتيجة: ناجح! رجوع للاختبار"

    class _Buttons:
        def __init__(self, page, button):
            self.page = page
            self.button = button

        def count(self):
            return 1 if self.page.scrolls >= 2 else 0

        def nth(self, index):
            assert index == 0
            return self.button

    class _Page:
        def __init__(self):
            self.scrolls = 0
            self.waits = []
            self.result_text = ""
            self.button = _Button(self)
            self.buttons = _Buttons(self, self.button)

        def evaluate(self, script):
            self.scrolls += 1
            return True

        def get_by_role(self, role, name=None):
            if role == "dialog":
                return _EmptyButtons()
            assert role == "button"
            return self.buttons if name == core.QUIZ_SUBMIT_BUTTON else _EmptyButtons()

        def wait_for_timeout(self, duration):
            self.waits.append(duration)

    class _EmptyButtons:
        def count(self):
            return 0

    class _Content:
        def __init__(self, page):
            self.page = page

        def get_by_role(self, role, name):
            return self.page.get_by_role(role, name)

    class _ResultContent:
        def __init__(self, page):
            self.page = page

        def inner_text(self, timeout=None):
            return self.page.result_text

    page = _Page()
    monkeypatch.setattr(core, "main_content", lambda _page: _ResultContent(page))

    assert core.submit_quiz(page, _Content(page))
    assert page.scrolls == 2
    assert page.button.clicked


def test_extract_quiz_prompt_climbs_past_question_mark_inside_choice_text():
    class _Node:
        def __init__(self, text, parent=None):
            self.text = text
            self.parent = parent

        def inner_text(self, timeout=None):
            return self.text

        def locator(self, selector):
            assert selector == "xpath=.."
            return _Parent(self.parent)

    class _Parent:
        def __init__(self, node):
            self.node = node

        def count(self):
            return 1 if self.node else 0

        def inner_text(self, timeout=None):
            return self.node.inner_text(timeout=timeout)

        def locator(self, selector):
            return self.node.locator(selector)

    options_text = (
        'سؤال صديق محلي مقرّب "ما رأيك في سلع كهذه؟"، وبما أنه قال "جميل، '
        'سأدعمك"، التفكير أنه مطلوب\n'
        "التفكير أنه يجب أن يكون هناك طلب بالتأكيد، ودون سؤال أحد\n"
        "الاعتماد فقط على النص المكتوب على السبورة\n"
        "سؤال زملاء المركز وطلاب النادي"
    )
    question = (
        "إنها مرحلة قبل الصنع بجدية. تبدأ بفتح الشاشة لطلب مواد بالجملة، ثم "
        "توقف يدك. لعدم الانزعاج بصنع الكثير هدرًا، تتأكد أولًا من وجود "
        "أشخاص يريدونه فعلًا. كدراسة سوق، أي مما يلي هو الفعل الأكثر ملاءمة؟"
    )
    container = _Node(f"{question}\nاختر إجابة واحدة!\n{options_text}")
    option_group = _Node(options_text, container)

    extracted = core.extract_quiz_prompt(option_group)
    assert extracted == question
    answer, source = core.match_answer(
        extracted, core.parse_answer_file(core.locate_answer_file())
    )
    assert "سؤال زملاء المركز" in answer

    question_without_mark = question.removesuffix("؟")
    extracted = core.extract_quiz_prompt(
        _Node(f"{question_without_mark}\nاختر إجابة واحدة!\n{options_text}")
    )
    assert extracted == question_without_mark
    answer, source = core.match_answer(
        extracted, core.parse_answer_file(core.locate_answer_file())
    )
    assert "سؤال زملاء المركز" in answer


def test_open_next_activity_includes_unfinished_test_leaves(monkeypatch):
    test_title = "اختبار المعرفة: محاكاة رائد الأعمال - من الفكرة إلى المشروع"

    class _Leaf:
        def __init__(self):
            self.clicked = False

        def click(self, **kwargs):
            self.clicked = True

    class _Leaves:
        def __init__(self, leaf):
            self.leaf = leaf

        def evaluate_all(self, script):
            return [{"i": 0, "title": "", "text": test_title, "visible": True}]

        def nth(self, index):
            assert index == 0
            return self.leaf

    class _Buttons:
        def count(self):
            return 0

    class _Side:
        def __init__(self, leaf):
            self.leaf = leaf

        def count(self):
            return 1

        def locator(self, selector):
            assert selector == "div.cursor-pointer"
            return _Leaves(self.leaf)

        def get_by_role(self, role):
            assert role == "button"
            return _Buttons()

    class _Page:
        def __init__(self, side):
            self.side = side
            self.waits = []

        def get_by_role(self, role):
            assert role == "complementary"
            return self.side

        def wait_for_timeout(self, duration):
            self.waits.append(duration)

    leaf = _Leaf()
    page = _Page(_Side(leaf))
    monkeypatch.setattr(core, "_activity_view_snapshot", lambda _page: {})
    monkeypatch.setattr(core, "_wait_for_activity_view_change", lambda *_args: True)
    monkeypatch.setattr(core, "_activity_is_current", lambda *_args: False)
    core._job.set({
        "leaf": None,
        "worked": set(),
        "announced": set(),
        "scan": None,
        "tests": [],
        "completed_tests": set(),
    })

    opened, group = core.open_next_available_activity(
        page, set(), "ريادة الأعمال"
    )

    assert opened
    assert group == "ريادة الأعمال"
    assert leaf.clicked

    leaf.clicked = False
    core._job.set({
        "leaf": None,
        "worked": set(),
        "announced": set(),
        "scan": None,
        "tests": [],
        "completed_tests": {core.normalize_text(test_title)},
    })
    opened, group = core.open_next_available_activity(
        page, set(), "ريادة الأعمال"
    )
    assert not opened
    assert group == "ريادة الأعمال"
    assert not leaf.clicked
    assert core._job.get()["tests"] == []


def test_open_next_activity_falls_back_to_api_when_sidebar_click_does_not_navigate(monkeypatch):
    class _Leaf:
        clicked = False

        def click(self, **_kwargs):
            self.clicked = True

    class _Leaves:
        def __init__(self, leaf):
            self.leaf = leaf

        def evaluate_all(self, _script):
            return [{"i": 0, "title": "", "text": "نشاط متاح", "visible": True}]

        def nth(self, index):
            assert index == 0
            return self.leaf

    class _Buttons:
        def count(self):
            return 0

    class _Side:
        def __init__(self, leaf):
            self.leaf = leaf

        def count(self):
            return 1

        def locator(self, selector):
            assert selector == "div.cursor-pointer"
            return _Leaves(self.leaf)

        def get_by_role(self, role):
            assert role == "button"
            return _Buttons()

    class _Page:
        def __init__(self, side):
            self.side = side

        def get_by_role(self, role):
            assert role == "complementary"
            return self.side

        def wait_for_timeout(self, _duration):
            pass

    leaf = _Leaf()
    page = _Page(_Side(leaf))
    api_fallbacks = []
    monkeypatch.setattr(core, "_activity_view_snapshot", lambda _page: {})
    monkeypatch.setattr(core, "_wait_for_activity_view_change", lambda *_args: False)
    monkeypatch.setattr(core, "_activity_is_current", lambda *_args: False)
    monkeypatch.setattr(
        core,
        "open_next_via_api",
        lambda current_page: api_fallbacks.append(current_page) or True,
    )
    monkeypatch.setattr(core, "_fallback_open", lambda *_args: False)
    core._job.set({
        "leaf": None,
        "worked": set(),
        "announced": set(),
        "scan": None,
        "tests": [],
        "completed_tests": set(),
    })
    visited = set()

    opened, group = core.open_next_available_activity(page, visited, "الوحدة المفتوحة")

    assert opened
    assert group == "الوحدة المفتوحة"
    assert leaf.clicked
    assert api_fallbacks == [page]
    assert not visited
    assert core._job.get()["leaf"] is None

    monkeypatch.setattr(core, "_activity_is_current", lambda _page, title: title == "نشاط متاح")
    core._job.set({
        "leaf": None,
        "worked": set(),
        "announced": set(),
        "scan": None,
        "tests": [],
        "completed_tests": set(),
    })
    visited.clear()
    opened, group = core.open_next_available_activity(page, visited, "الوحدة المفتوحة")

    assert opened
    assert group == "الوحدة المفتوحة"
    assert core._job.get()["leaf"] == "نشاط متاح"
    assert api_fallbacks == [page]
    assert len(visited) == 1


def test_open_next_activity_reads_sidebar_buttons_in_one_browser_call(monkeypatch):
    class _Button:
        def __init__(self):
            self.clicked = False

        def is_disabled(self):
            raise AssertionError("The bulk-read path should not query button state separately")

        def inner_text(self, timeout=None):
            raise AssertionError("The bulk-read path should not query button text separately")

        def click(self, **kwargs):
            self.clicked = True

    class _Buttons:
        def __init__(self):
            self.button = _Button()
            self.script = None

        def evaluate_all(self, script):
            self.script = script
            return [
                {"i": 0, "label": "وحدة مفتوحة", "disabled": False},
                {"i": 1, "label": "وحدة مقفولة", "disabled": True},
            ]

        def nth(self, index):
            assert index == 0
            return self.button

    class _Leaves:
        def evaluate_all(self, _script):
            return []

    class _Side:
        def __init__(self):
            self.buttons = _Buttons()

        def count(self):
            return 1

        def locator(self, selector):
            assert selector == "div.cursor-pointer"
            return _Leaves()

        def get_by_role(self, role):
            assert role == "button"
            return self.buttons

    class _Page:
        def __init__(self, side):
            self.side = side

        def get_by_role(self, role):
            assert role == "complementary"
            return self.side

        def wait_for_timeout(self, _duration):
            pass

    side = _Side()
    monkeypatch.setattr(core, "_fallback_open", lambda *_args: False)
    core._job.set({
        "leaf": None,
        "worked": set(),
        "announced": set(),
        "scan": None,
        "tests": [],
        "completed_tests": set(),
    })

    opened, group = core.open_next_available_activity(_Page(side), set(), None)

    assert not opened
    assert group == "وحدة مفتوحة"
    assert side.buttons.script == core._BUTTONS_JS
    assert side.buttons.button.clicked


def test_submit_quiz_confirms_final_submission_dialog(monkeypatch):
    class _Button:
        def __init__(self, on_click=None):
            self.clicked = False
            self.on_click = on_click

        def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return self

        @property
        def first(self):
            return self

        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def scroll_into_view_if_needed(self, **kwargs):
            pass

        def click(self, **kwargs):
            self.clicked = True
            if self.on_click:
                self.on_click()

    class _Dialog:
        def __init__(self, button):
            self.button = button

        def is_visible(self):
            return True

        def get_by_role(self, role, name):
            assert role == "button"
            assert "تأكيد" in name.pattern
            assert name.search("تأكيد تقديم الاختبار")
            return self.button

    class _Dialogs:
        def __init__(self, dialog):
            self.dialog = dialog

        def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return self.dialog

    class _Page:
        def __init__(self, dialog):
            self.dialog = dialog
            self.waits = []
            self.result_text = ""

        def wait_for_timeout(self, duration):
            self.waits.append(duration)

        def get_by_role(self, role):
            assert role == "dialog"
            return self.dialog

    class _ResultContent:
        def __init__(self, page):
            self.page = page

        def inner_text(self, timeout=None):
            return self.page.result_text

    class _Content:
        def __init__(self, button):
            self.button = button

        def get_by_role(self, role, name):
            assert role == "button"
            assert "Submit" in name.pattern
            return self.button

    initial_submit = _Button()
    page = _Page(None)
    confirm_submit = _Button(
        lambda: setattr(page, "result_text", "النتيجة: 100% ناجح! رجوع للاختبار")
    )
    page.dialog = _Dialogs(_Dialog(confirm_submit))
    monkeypatch.setattr(core, "main_content", lambda _page: _ResultContent(_page))

    assert core.submit_quiz(page, _Content(initial_submit))
    assert initial_submit.clicked
    assert confirm_submit.clicked
    assert page.waits == [core.WAIT_MEDIUM]


def test_submit_quiz_confirms_visual_overlay_without_dialog_role(monkeypatch):
    class _Button:
        def __init__(self, on_click=None):
            self.clicked = False
            self.on_click = on_click

        def count(self):
            return 1

        @property
        def first(self):
            return self

        def nth(self, index):
            return self.buttons[index]

        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def scroll_into_view_if_needed(self, **kwargs):
            pass

        def click(self, **kwargs):
            self.clicked = True
            if self.on_click:
                self.on_click()

    class _Buttons:
        def __init__(self, buttons):
            self.buttons = buttons

        @property
        def first(self):
            return self.buttons[0]

        def count(self):
            return len(self.buttons)

        def nth(self, index):
            return self.buttons[index]

    class _Page:
        def __init__(self):
            self.waits = []
            self.result_text = ""
            self.confirm_button = _Button(
                lambda: setattr(self, "result_text", "النتيجة: 100% ناجح! رجوع للاختبار")
            )

        def wait_for_timeout(self, duration):
            self.waits.append(duration)

        def get_by_role(self, role, name=None):
            if role == "dialog":
                return _Buttons([])
            assert role == "button" and name is not None
            if "تأكيد" in name.pattern:
                assert name.search("تأكيد تقديم الاختبار")
                return _Buttons([self.confirm_button])
            raise AssertionError(f"Unexpected button pattern: {name.pattern}")

    class _Content:
        def get_by_role(self, role, name):
            assert role == "button"
            assert "Submit" in name.pattern
            return _Buttons([_Button()])

    class _ResultContent:
        def __init__(self, page):
            self.page = page

        def inner_text(self, timeout=None):
            return self.page.result_text

    page = _Page()
    monkeypatch.setattr(core, "main_content", lambda _page: _ResultContent(_page))
    assert core.submit_quiz(page, _Content())
    assert page.confirm_button.clicked
    assert page.waits == [core.WAIT_MEDIUM]


def test_parse_structured_markdown_answer_bank(tmp_path):
    answer_file = tmp_path / "answers.md"
    answer_file.write_text(
        "## المستوى 1\n\n"
        "### السؤال 1\n"
        "**السؤال:** ما هو السؤال الأول؟\n"
        "**الإجابة:** الإجابة الأولى\n\n"
        "### السؤال 2\n"
        "**السؤال:** ما هو السؤال الثاني؟\n"
        "**الإجابة:** إجابة\nتتكون من سطرين\n",
        encoding="utf-8",
    )

    entries = core.parse_answer_file(answer_file)

    assert len(entries) == 2
    assert core.match_answer("ما هو السؤال الأول؟", entries)[0] == "الإجابة الأولى"
    assert core.match_answer("ما هو السؤال الثاني؟", entries)[0] == "إجابة تتكون من سطرين"


def test_next_button_never_starts_a_test():
    assert not core.NEXT_BUTTON.match("ابدأ الاختبار")
    assert not core.NEXT_BUTTON.match("تقديم الاختبار")
    assert core.NEXT_BUTTON.match("التالي")
    assert core.NEXT_BUTTON.match("إكمال")
    assert core.INTRO_BUTTON.match("تمام!")


def test_circled_digit_options():
    answer = "② و④"
    with_letters = ["A\n① و ②", "B\n① و ③", "C\n③ و ④", "D\n② و ④"]
    bare = ["① و ②", "① و ③", "③ و ④", "② و ④"]
    assert core.answer_option_index(with_letters, answer) == 3
    assert core.answer_option_index(bare, answer) == 3


def test_numbered_options_still_work():
    assert core.answer_option_index(["A. 100 جنيه", "B. 200 جنيه"], "200 جنيه") == 1
    assert core.answer_option_index(["A 100 جنيه", "B 200 جنيه"], "100 جنيه") == 0


def test_failed_answer_match_is_cached_for_repeated_prompt(monkeypatch):
    entries = core.AnswerIndex([("سؤال معروف؟", "إجابة")])
    original_matcher = core.difflib.SequenceMatcher
    matcher_calls = []

    def counted_matcher(*args, **kwargs):
        matcher_calls.append(True)
        return original_matcher(*args, **kwargs)

    monkeypatch.setattr(core.difflib, "SequenceMatcher", counted_matcher)
    for _ in range(2):
        try:
            core.match_answer("سؤال مختلف تمامًا", entries)
        except core.AnswerMatchError as exc:
            assert "لا يوجد تطابق مؤكد" in str(exc)
        else:
            raise AssertionError("An unrelated prompt must not match")

    assert len(matcher_calls) == 1


def test_extract_question_prompt_removes_answer_options_from_card_text():
    options = [
        "تحديد الدخل والنفقات مسبقًا ووضع خطة لتجنّب الإنفاق العشوائي",
        "تجاهل المصاريف المتغيرة بالكامل",
        "الاعتماد فقط على الذاكرة عند الإنفاق",
        "تخصيص المال بعد الإنفاق فقط",
    ]
    card_text = (
        "أي مما يلي وصف صحيح للميزانية؟ "
        "تحديد الدخل والنفقات مسبقًا ووضع خطة لتجنّب الإنفاق العشوائي "
        "تجاهل المصاريف المتغيرة بالكامل "
        "الاعتماد فقط على الذاكرة عند الإنفاق "
        "تخصيص المال بعد الإنفاق فقط"
    )

    assert core.extract_question_prompt(card_text, options) == "أي مما يلي وصف صحيح للميزانية؟"


def test_entrepreneurship_exam_last_answer_excludes_completion_message():
    entries = core.parse_answer_file(core.locate_answer_file())
    question, answer = next(
        (question, answer)
        for question, answer in entries
        if "الأهمية الاجتماعية" in question and "كمثال على" in question
    )
    matched_answer, _source = core.match_answer(question, entries)
    options = [
        "تبسيط إجراءات العمل الداخلية والنجاح في تقليل ساعات العمل الإضافية الخاصة بك قليلًا",
        "آلية جديدة لم تكن موجودة من قبل انتشرت في العالم، وأصبحت حياة كثير من الناس أكثر راحة",
        "إصدار إعلانات كثيرة بحيث يصبح اسم المحل معروفًا على نطاق واسع في المنطقة مؤقتًا",
        "تزيين واجهة المحل بشكل فاخر بحيث يأتي العابرون لمديحها",
    ]

    assert "مبروك" not in answer
    assert matched_answer == answer
    assert core.answer_option_index(options, answer) == 1


def test_wait_for_quiz_question_waits_for_visible_radio():
    class _Radio:
        def __init__(self):
            self.wait = None

        def wait_for(self, **kwargs):
            self.wait = kwargs

    class _Radios:
        first = _Radio()

    class _Content:
        def get_by_role(self, role):
            assert role == "radio"
            return _Radios()

    content = _Content()

    assert core.wait_for_quiz_question(content)
    assert _Radios.first.wait == {
        "state": "visible",
        "timeout": core.QUIZ_QUESTION_TIMEOUT,
    }


def test_wait_for_quiz_question_reports_missing_radio():
    class _Radio:
        def wait_for(self, **kwargs):
            raise core.PWTimeout("timed out")

    class _Radios:
        first = _Radio()

    class _Content:
        def get_by_role(self, role):
            return _Radios()

    assert not core.wait_for_quiz_question(_Content())


def test_diagnostic_screenshot_redacts_top_account_bar():
    original = BytesIO()
    core.Image.new("RGB", (20, 200), "white").save(original, format="PNG")

    redacted = core._redact_screenshot_bytes(original.getvalue())
    with core.Image.open(BytesIO(redacted)) as image:
        assert image.getpixel((10, 10)) == (0, 0, 0)
        assert image.getpixel((10, 150)) == (255, 255, 255)


def test_diagnostic_report_omits_query_and_masks_numeric_path_ids():
    report = core._diagnostic_report(
        "error",
        "failure token=private https://example.test/route?session=secret",
        "activity",
        None,
        "https://example.test/subjects/123/activity?token=secret",
    )

    assert report["page"] == "example.test/subjects/[id]/activity"
    assert "session=secret" not in report["error"]
    assert "token=private" not in report["error"]
    assert "secret" not in report["page"]


def test_diagnostic_report_is_sent_only_with_consent():
    events = []
    hook_token = core._hook.set(lambda kind, report: events.append((kind, report)))
    consent_token = core._report_consent.set(False)
    try:
        core._send_diagnostic({"status": "error"})
        assert events == []

        core._report_consent.set(True)
        report = {"status": "stuck"}
        core._send_diagnostic(report)
        assert events == [("diagnostic", report)]
    finally:
        core._report_consent.reset(consent_token)
        core._hook.reset(hook_token)


def test_owner_diagnostic_sender_delivers_report_and_redacted_image():
    import bot

    class _FakeBot:
        def __init__(self):
            self.messages = []
            self.photos = []

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)

        async def send_photo(self, **kwargs):
            self.photos.append(kwargs)

    class _App:
        bot = _FakeBot()

    screenshot = b"redacted-image"
    report = {
        "id": "test-report",
        "status": "stuck",
        "timestamp": "2026-11-12T00:00:00+00:00",
        "page": "example.test/activity",
        "activity": "lesson",
        "error": "diagnostic",
        "screenshot": screenshot,
    }

    asyncio.run(bot.send_owner_diagnostic(_App(), report))

    assert _App.bot.messages[0]["chat_id"] == bot.OWNER_CHAT_ID
    assert "diagnostic" in _App.bot.messages[0]["text"]
    assert _App.bot.photos[0]["chat_id"] == bot.OWNER_CHAT_ID
    assert _App.bot.photos[0]["photo"].getvalue() == screenshot


class _FakeContent:
    def __init__(self, text):
        self._t = text

    def inner_text(self, timeout=None):
        return self._t

    def locator(self, *_a, **_k):
        return self

    @property
    def first(self):
        return self

    def count(self):
        return 0


class _FakePage:
    url = "https://example.test/subjects/7/curriculum"


def test_run_learning_flow_attempts_test_page_from_answer_bank(monkeypatch):
    intro = "اختبار المعرفة: محاكاة حياة العمل\n50\nالأسئلة\n80%\nدرجة النجاح\nابدأ الاختبار"
    monkeypatch.setattr(core, "main_content", lambda page: _FakeContent(intro))
    monkeypatch.setattr(core, "current_context", lambda page: "اختبار المعرفة")
    attempts = []

    def no_matching_answers(page, entries):
        attempts.append((page, entries))
        return False

    monkeypatch.setattr(core, "solve_quiz", no_matching_answers)

    def boom(*a, **k):
        raise AssertionError("يجب عدم تمرير صفحة الاختبار إلى معالجات الدروس العادية")

    for name in ("solve_one_visible_question", "answer_text_question_if_supported",
                 "mark_review_points", "click_next", "open_next_available_activity", "dismiss_intro"):
        monkeypatch.setattr(core, name, boom)

    events = []
    core._hook.set(lambda kind, msg: events.append((kind, msg)))
    core._job.set({"leaf": None, "worked": set(), "announced": set(), "scan": None,
                   "unmatched": [], "stuck": None, "tests": [], "test_reached": None})
    core.run_learning_flow(_FakePage(), [])
    assert core._job.get()["test_reached"]
    assert len(attempts) == 1
    assert ("quiz_solving", "اختبار المعرفة") in events
    assert ("quiz_skipped", "اختبار المعرفة") in events


def test_run_learning_flow_does_not_reprocess_same_test_page(monkeypatch):
    test_text = "اختبار المعرفة\nالسؤال 10 من 10\nتقديم الاختبار"
    monkeypatch.setattr(core, "main_content", lambda page: _FakeContent(test_text))
    monkeypatch.setattr(core, "current_context", lambda page: "اختبار المعرفة")
    attempts = []
    monkeypatch.setattr(
        core,
        "solve_quiz",
        lambda *args: attempts.append(args) or True,
    )
    events = []
    core._hook.set(lambda kind, msg: events.append((kind, msg)))
    core._job.set({
        "leaf": None,
        "worked": set(),
        "announced": set(),
        "scan": None,
        "unmatched": [],
        "stuck": None,
        "tests": [],
        "test_reached": None,
    })

    core.run_learning_flow(_FakePage(), [])

    assert len(attempts) == 1
    assert "بقيت ظاهرة" in core._job.get()["stuck"]
    assert sum(kind == "quiz_solving" for kind, _msg in events) == 1
    assert not any(kind == "quiz_done" for kind, _msg in events)


def test_run_learning_flow_resumes_still_running_quiz_once(monkeypatch):
    class _Page:
        url = "https://example.test/quiz/take"
        text = (
            "ما هو الاستثمار؟ المحاولة رقم 1 جاري التنفيذ "
            "10 الأسئلة 80% درجة النجاح ابدأ من جديد استكمال الاختبار"
        )

        def locator(self, selector):
            assert selector == "body"
            return _FakeContent(self.text)

    page = _Page()
    attempts = []
    result_text = "النتيجة: 100% ناجح! رجوع للاختبار"

    def solve(current_page, _entries):
        attempts.append(current_page)
        if len(attempts) == 2:
            current_page.text = result_text
        return True

    monkeypatch.setattr(core, "MAX_STEPS", 5)
    monkeypatch.setattr(core, "main_content", lambda current_page: _FakeContent(current_page.text))
    monkeypatch.setattr(core, "current_context", lambda _page: "ما هو الاستثمار؟")
    monkeypatch.setattr(core, "solve_quiz", solve)
    monkeypatch.setattr(
        core,
        "return_to_curriculum",
        lambda current_page: setattr(current_page, "text", "المنهج") or True,
    )
    for name in (
        "solve_one_visible_question",
        "answer_text_question_if_supported",
        "mark_review_points",
        "click_next",
        "dismiss_intro",
    ):
        monkeypatch.setattr(core, name, lambda *_args: False)
    monkeypatch.setattr(
        core, "open_next_available_activity", lambda *_args: (False, None)
    )
    core._hook.set(lambda *_args: None)
    core._job.set({
        "leaf": None,
        "worked": set(),
        "announced": set(),
        "scan": None,
        "unmatched": [],
        "stuck": None,
        "tests": [],
        "test_reached": None,
        "completed_tests": set(),
        "opened_units": set(),
    })

    core.run_learning_flow(page, [])

    assert len(attempts) == 2
    assert core._job.get()["completed_tests"] == {"ما هو الاستثمار؟"}
    assert core._job.get()["stuck"] is None


def test_run_job_reports_tests_pending(monkeypatch):
    def fake_attempt(username, password, entries, headless):
        core._job.get()["test_reached"] = "اختبار المعرفة: محاكاة حياة العمل"

    monkeypatch.setattr(core, "_attempt", fake_attempt)
    result = core.run_job("u", "p", [], hook=lambda *a: None)
    assert result["status"] == "tests_pending"
    assert "اختبار" in result["test"]


def test_run_job_finished_without_tests(monkeypatch):
    monkeypatch.setattr(core, "_attempt", lambda *a, **k: None)
    assert core.run_job("u", "p", [], hook=lambda *a: None)["status"] == "finished"


# ───────── خطة البداية: المخلّص / الباقي / المقفول ─────────
class _Btn:
    def __init__(self, text, disabled=False):
        self._t, self._d = text, disabled

    def inner_text(self, timeout=None):
        return self._t

    def is_disabled(self):
        return self._d


class _Btns:
    def __init__(self, items):
        self._items = items

    def count(self):
        return len(self._items)

    def nth(self, i):
        return self._items[i]


class _Side:
    def __init__(self, items):
        self._b = _Btns(items)

    def count(self):
        return 1

    def get_by_role(self, role, **_k):
        return self._b


class _PlanPage:
    def __init__(self, items):
        self._side = _Side(items)

    def get_by_role(self, role, **_k):
        return self._side


def test_announce_plan_reports_done_todo_locked():
    items = [
        _Btn("رجوع"),
        _Btn("أساسيات المال والتمويل 28/28 عنصر"),
        _Btn("أساسيات الأعمال والاقتصاد 16/23 عنصر"),
        _Btn("التمويل الشخصي والائتمان واتخاذ القرار", disabled=True),
        _Btn("ريادة الأعمال", disabled=True),
    ]
    events = []
    core._hook.set(lambda kind, msg: events.append((kind, msg)))
    core._job.set({"leaf": None, "worked": set(), "announced": set(), "scan": None,
                   "unmatched": [], "stuck": None, "tests": [], "test_reached": None})
    core.announce_plan(_PlanPage(items))
    assert ("plan_done", "أساسيات المال والتمويل|28|28") in events
    assert ("plan_todo", "أساسيات الأعمال والاقتصاد|16|23") in events
    locked = [m for k, m in events if k == "plan_locked"][0]
    assert "ريادة الأعمال" in locked and "التمويل الشخصي" in locked
    # المخلّص متسجّل كمُعلَن عشان الحلقة متكررش الرسالة
    assert "أساسيات المال والتمويل 28/28 عنصر" in core._job.get()["announced"]


# ───────── فتح الوحدة الناقصة (بدل أول وحدة دايمًا) ─────────
class _DomBtn:
    def __init__(self, dom, kind, idx=None, label="", disabled=False):
        self.dom, self.kind, self.idx, self._l, self._d = dom, kind, idx, label, disabled

    def inner_text(self, timeout=None):
        return self._l

    def is_disabled(self):
        return self._d

    def get_attribute(self, name):
        if name == "aria-expanded" and self.dom.use_aria and self.kind == "unit":
            return "true" if self.dom.units[self.idx]["expanded"] else "false"
        return None

    def click(self, **_k):
        if self.kind == "unit":
            u = self.dom.units[self.idx]
            u["expanded"] = not u["expanded"]
            self.dom.clicks.append(u["name"])


class _Dom:
    def __init__(self, units, use_aria=False):
        self.units, self.use_aria, self.clicks = units, use_aria, []

    def buttons(self):
        out = [_DomBtn(self, "back", label="رجوع")]
        for i, u in enumerate(self.units):
            if u.get("locked"):
                out.append(_DomBtn(self, "locked", label=u["name"], disabled=True))
                continue
            out.append(_DomBtn(self, "unit", i, f'{u["name"]} {u["done"]}/{u["total"]} عنصر'))
            if u["expanded"]:
                out += [_DomBtn(self, "section", label=s) for s in u.get("sections", [])]
        return out


class _Coll:
    def __init__(self, items):
        self._i = items

    def count(self):
        return len(self._i)

    def nth(self, i):
        return self._i[i]


class _DomSide:
    def __init__(self, dom):
        self.dom = dom

    def count(self):
        return 1

    def get_by_role(self, role, **_k):
        return _Coll(self.dom.buttons())

    def locator(self, *_a, **_k):
        return _Coll([])


class _DomPage:
    def __init__(self, dom):
        self.dom = dom
        self.side = _DomSide(dom)

    def get_by_role(self, role, **_k):
        return self.side

    def wait_for_timeout(self, _ms):
        pass


def _units():
    return [
        {"name": "أساسيات المال والتمويل", "done": 28, "total": 28, "expanded": False, "sections": ["دور المال"]},
        {"name": "أساسيات الأعمال والاقتصاد", "done": 16, "total": 23, "expanded": False, "sections": ["الدخل"]},
        {"name": "ريادة الأعمال", "locked": True, "expanded": False},
    ]


def _fresh_job():
    core._hook.set(lambda *a: None)
    core._job.set({"leaf": None, "worked": set(), "announced": set(), "scan": None,
                   "unmatched": [], "stuck": None, "tests": [], "test_reached": None})


def test_expand_skips_finished_unit_and_opens_the_unfinished_one():
    _fresh_job()
    dom = _Dom(_units())
    page = _DomPage(dom)
    assert core.expand_next_unit(page, page.side) is True
    assert dom.clicks == ["أساسيات الأعمال والاقتصاد"]          # مش الوحدة المخلّصة
    assert dom.units[1]["expanded"] and not dom.units[0]["expanded"]
    assert core.expand_next_unit(page, page.side) is False       # مفيش وحدة ناقصة تانية


def test_expand_reopens_if_it_collapsed_an_already_open_unit():
    _fresh_job()
    units = _units()
    units[1]["expanded"] = True                                  # مفتوحة من الأول، من غير aria-expanded
    dom = _Dom(units)
    page = _DomPage(dom)
    core.expand_next_unit(page, page.side)
    assert dom.units[1]["expanded"] is True                      # اتقفلت واتفتحت تاني
    assert dom.clicks.count("أساسيات الأعمال والاقتصاد") == 2


def test_expand_does_not_click_when_aria_says_open():
    _fresh_job()
    units = _units()
    units[1]["expanded"] = True
    dom = _Dom(units, use_aria=True)
    page = _DomPage(dom)
    assert core.expand_next_unit(page, page.side) is False
    assert dom.clicks == []


def test_expand_reopens_collapsed_unit_even_if_it_was_opened_before():
    _fresh_job()
    units = _units()
    dom = _Dom(units, use_aria=True)
    page = _DomPage(dom)

    assert core.expand_next_unit(page, page.side) is True
    assert dom.units[1]["expanded"] is True

    dom.units[1]["expanded"] = False
    assert core.expand_next_unit(page, page.side) is True
    assert dom.units[1]["expanded"] is True
    assert dom.clicks.count("أساسيات الأعمال والاقتصاد") == 2


def test_expand_named_unit_opens_the_unit_selected_by_curriculum_api():
    _fresh_job()
    dom = _Dom(_units(), use_aria=True)
    page = _DomPage(dom)

    assert core._expand_unit_named(page, page.side, "أساسيات الأعمال والاقتصاد")
    assert dom.units[1]["expanded"]
    assert dom.clicks == ["أساسيات الأعمال والاقتصاد"]


def test_visible_matches_prefers_exact_sidebar_label():
    class _Match:
        def __init__(self, label):
            self.label = label

        def is_visible(self):
            return True

        def inner_text(self, timeout=None):
            return self.label

    class _Matches:
        def __init__(self, items):
            self.items = items

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class _Side:
        def __init__(self):
            self.exact = _Matches([_Match("ما هو المشروع التجاري؟")])
            self.partial = _Matches([
                _Match("ما هو المشروع التجاري؟"),
                _Match("ما هو المشروع التجاري؟ — مثال"),
            ])

        def get_by_text(self, text, exact=False):
            if exact:
                assert text == "ما هو المشروع التجاري؟"
                return self.exact
            return self.partial

    side = _Side()

    matches = core._visible_matches(side, "ما هو المشروع التجاري؟")

    assert matches == [side.exact.items[0]]


def test_visible_activity_matches_use_curriculum_leaf_rows_and_prefer_exact():
    class _Leaf:
        def __init__(self, label, visible=True):
            self.label = label
            self.visible = visible

    class _Leaves:
        def __init__(self):
            self.rows = [
                _Leaf("المخاطرة والعائد — الدرس"),
                _Leaf("المخاطرة والعائد"),
                _Leaf("المخاطرة والعائد", visible=False),
            ]

        def evaluate_all(self, script):
            assert script == core._LEAVES_JS
            return [
                {"i": i, "text": leaf.label, "visible": leaf.visible, "title": ""}
                for i, leaf in enumerate(self.rows)
            ]

        def nth(self, index):
            return self.rows[index]

    class _Side:
        def __init__(self):
            self.leaves = _Leaves()

        def locator(self, selector):
            assert selector == "div.cursor-pointer"
            return self.leaves

    side = _Side()

    matches = core._visible_activity_matches(side, "المخاطرة والعائد")

    assert matches == [side.leaves.rows[1], side.leaves.rows[0]]


def test_open_next_via_api_expands_unit_and_section_before_clicking_item(monkeypatch):
    class _Item:
        clicked = False

        def inner_text(self, timeout=None):
            return "الدرس المطلوب"

        def click(self, **kwargs):
            assert kwargs == {"force": True, "no_wait_after": True}
            self.clicked = True

    class _Parent:
        clicked = False

        def inner_text(self, timeout=None):
            return "اختبار المعرفة: الدرس المطلوب"

        def click(self, **_kwargs):
            self.clicked = True

    class _Section:
        def get_attribute(self, name):
            assert name == "aria-expanded"
            return "true" if section_open["value"] else "false"

        def click(self, **_kwargs):
            section_open["value"] = True
            events.append("section")

    class _Side:
        pass

    class _Page:
        def __init__(self):
            self.waits = []
            self.side = _Side()

        def get_by_role(self, role):
            assert role == "complementary"
            return self.side

        def wait_for_timeout(self, duration):
            self.waits.append(duration)

    curriculum = [{
        "title": "الوحدة المفتوحة",
        "sections": [{
            "name": "القسم المطوي",
            "items": [{
                "id": 7,
                "title": "الدرس المطلوب",
                "item_type": "lesson",
                "is_completed": False,
                "is_unlocked": True,
            }],
        }],
    }]
    item = _Item()
    parent = _Parent()
    events = []
    section_open = {"value": False}
    monkeypatch.setattr(core, "fetch_curriculum", lambda _page: curriculum)

    def expand_unit(_page, _side, name):
        assert name == "الوحدة المفتوحة"
        events.append("unit")
        return True

    def matches(_side, text):
        if text == "الدرس المطلوب":
            return [parent]
        if text == "القسم المطوي":
            return [_Section()]
        return []

    monkeypatch.setattr(core, "_expand_unit_named", expand_unit)
    monkeypatch.setattr(core, "_visible_matches", matches)
    monkeypatch.setattr(
        core,
        "_visible_activity_matches",
        lambda *_args: [item] if section_open["value"] else [],
    )
    monkeypatch.setattr(core, "_activity_view_snapshot", lambda _page: {})
    monkeypatch.setattr(core, "_wait_for_activity_view_change", lambda *_args: True)
    monkeypatch.setattr(core, "_activity_is_current", lambda *_args: False)
    page = _Page()
    core._job.set({
        "leaf": None,
        "worked": set(),
        "api_attempts": {},
        "completed_tests": set(),
    })

    assert core.open_next_via_api(page)
    assert item.clicked
    assert not parent.clicked
    assert events == ["unit", "section"]
    assert core._job.get()["leaf"] == "الدرس المطلوب"
    assert "الوحدة المفتوحة" in core._job.get()["worked"]


def test_open_next_via_api_expands_test_section_when_its_title_matches_item(monkeypatch):
    test_title = "اختبار المعرفة: محاكاة رائد الأعمال - من الفكرة إلى المشروع"

    class _Section:
        expanded = False

        def get_attribute(self, name):
            assert name == "aria-expanded"
            return "true" if self.expanded else "false"

        def click(self, **_kwargs):
            self.expanded = True

    class _Item:
        clicked = False

        def inner_text(self, timeout=None):
            return test_title

        def click(self, **_kwargs):
            self.clicked = True

    class _Side:
        def get_by_text(self, text, exact=False):
            assert text == test_title
            return _Matches([section])

    class _Matches:
        def __init__(self, items):
            self.items = items

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class _Page:
        def __init__(self):
            self.side = _Side()

        def get_by_role(self, role):
            assert role == "complementary"
            return self.side

        def wait_for_timeout(self, _duration):
            pass

    section = _Section()
    item = _Item()
    curriculum = [{
        "title": "ريادة الأعمال",
        "sections": [{
            "name": test_title,
            "items": [{
                "id": 44,
                "title": test_title,
                "item_type": "quiz",
                "is_completed": False,
                "is_unlocked": True,
            }],
        }],
    }]
    monkeypatch.setattr(core, "fetch_curriculum", lambda _page: curriculum)
    monkeypatch.setattr(core, "_read_units", lambda _page: ([{"name": "ريادة الأعمال"}], []))
    monkeypatch.setattr(core, "_expand_unit_named", lambda *_args: False)
    monkeypatch.setattr(
        core,
        "_visible_activity_matches",
        lambda *_args: [item] if section.expanded else [],
    )
    monkeypatch.setattr(
        core, "_visible_matches", lambda *_args: [section]
    )
    monkeypatch.setattr(core, "_activity_view_snapshot", lambda _page: {})
    monkeypatch.setattr(
        core, "_wait_for_activity_view_change", lambda *_args: item.clicked
    )
    monkeypatch.setattr(core, "_activity_is_current", lambda *_args: False)
    page = _Page()
    core._job.set({
        "leaf": None,
        "worked": set(),
        "api_attempts": {},
        "completed_tests": set(),
    })

    assert core.open_next_via_api(page)
    assert section.expanded
    assert item.clicked
    assert core._job.get()["leaf"] == test_title


def test_open_next_via_api_expands_collapsed_sidebar_before_retrying_item(monkeypatch):
    class _Button:
        def __init__(self, label, on_click=None):
            self.label = label
            self.on_click = on_click
            self.clicked = False

        def inner_text(self, timeout=None):
            return self.label

        def is_disabled(self):
            return False

        def is_visible(self):
            return True

        def click(self, **_kwargs):
            self.clicked = True
            if self.on_click:
                self.on_click()

    class _Buttons:
        def __init__(self, get_items):
            self.get_items = get_items

        def count(self):
            return len(self.get_items())

        def nth(self, index):
            return self.get_items()[index]

    class _Side:
        def __init__(self):
            self.expanded = False
            self.item = _Button("العنصر المطلوب")
            self.toggle = _Button("توسيع الشريط الجانبي", self._expand)

        def _expand(self):
            self.expanded = True

        def count(self):
            return 1

        def get_by_role(self, role, name=None):
            assert role == "button"
            if name is not None:
                return _Buttons(lambda: [self.toggle])
            return _Buttons(
                lambda: [self.toggle, _Button("الوحدة المطلوبة 0/1 عنصر")]
                if self.expanded
                else [self.toggle]
            )

        def locator(self, _selector):
            return _Buttons(lambda: [])

        def get_by_text(self, text, exact=False):
            return _Buttons(
                lambda: [self.item]
                if self.expanded and text == "العنصر المطلوب"
                else []
            )

    class _Page:
        def __init__(self, side):
            self.side = side

        def get_by_role(self, role, name=None):
            assert role == "complementary"
            return self.side

        def wait_for_timeout(self, _duration):
            pass

    side = _Side()
    page = _Page(side)
    item_data = {
        "id": 11,
        "title": "العنصر المطلوب",
        "item_type": "lesson",
        "is_completed": False,
        "is_unlocked": True,
    }
    monkeypatch.setattr(core, "fetch_curriculum", lambda _page: [{
        "title": "الوحدة المطلوبة",
        "sections": [{"name": "القسم", "items": [item_data]}],
    }])
    monkeypatch.setattr(core, "_visible_activity_matches", lambda *_args: [])
    monkeypatch.setattr(core, "_expand_unit_named", lambda *_args: False)
    monkeypatch.setattr(core, "_activity_view_snapshot", lambda _page: {})
    monkeypatch.setattr(core, "_wait_for_activity_view_change", lambda *_args: True)
    monkeypatch.setattr(core, "_activity_is_current", lambda *_args: False)
    core._job.set({
        "leaf": None,
        "worked": set(),
        "api_attempts": {},
        "completed_tests": set(),
    })

    assert core.open_next_via_api(page)
    assert side.expanded
    assert core._job.get()["leaf"] == "العنصر المطلوب"


def test_open_next_via_api_refreshes_stale_curriculum_sidebar_once(monkeypatch):
    class _Item:
        clicked = False

        def click(self, **kwargs):
            self.clicked = True

    class _Side:
        pass

    class _Page:
        def __init__(self):
            self.reloads = 0
            self.side = _Side()

        def get_by_role(self, role):
            assert role == "complementary"
            return self.side

        def reload(self, **kwargs):
            assert kwargs == {"wait_until": "domcontentloaded", "timeout": 45000}
            self.reloads += 1

        def wait_for_timeout(self, _duration):
            pass

    curriculum = [{
        "title": "وحدة التمويل",
        "sections": [{
            "name": "الائتمان",
            "items": [{
                "id": 8,
                "title": "ما هو العقد؟",
                "item_type": "lesson",
                "is_completed": False,
                "is_unlocked": True,
            }],
        }],
    }]
    item = _Item()
    monkeypatch.setattr(core, "fetch_curriculum", lambda _page: curriculum)
    monkeypatch.setattr(core, "_expand_unit_named", lambda *_args: False)
    monkeypatch.setattr(core, "_visible_activity_matches", lambda *_args: [])
    monkeypatch.setattr(core, "_activity_is_current", lambda *_args: False)
    monkeypatch.setattr(
        core,
        "_visible_matches",
        lambda _side, title: [item] if title == "ما هو العقد؟" and page.reloads else [],
    )
    page = _Page()
    monkeypatch.setattr(core, "_activity_view_snapshot", lambda _page: {})
    monkeypatch.setattr(
        core,
        "_wait_for_activity_view_change",
        lambda current_page, _before: current_page.reloads == 1,
    )
    core._job.set({
        "leaf": None,
        "worked": set(),
        "api_attempts": {},
        "completed_tests": set(),
    })

    assert core.open_next_via_api(page)
    assert page.reloads == 1
    assert item.clicked
    assert core._job.get()["leaf"] == "ما هو العقد؟"


def test_open_next_via_api_recognizes_activity_already_open_without_counting_retry(monkeypatch):
    class _Content:
        def locator(self, selector):
            assert selector == "h1, h2"
            return _EmptyHeadings()

        def inner_text(self, timeout=None):
            return "المخاطرة والعائد في أساسيات الاستثمار"

    class _EmptyHeadings:
        def count(self):
            return 0

    class _Page:
        def get_by_role(self, role):
            raise AssertionError("An already-open activity must not be clicked again")

    curriculum = [{
        "title": "أساسيات الاستثمار وإدارة الثروات",
        "sections": [{
            "name": "الاستثمار",
            "items": [{
                "id": 93,
                "title": "المخاطرة والعائد",
                "item_type": "lesson",
                "is_completed": False,
                "is_unlocked": True,
            }],
        }],
    }]
    monkeypatch.setattr(core, "fetch_curriculum", lambda _page: curriculum)
    monkeypatch.setattr(core, "main_content", lambda _page: _Content())
    core._job.set({
        "leaf": None,
        "worked": set(),
        "api_attempts": {"93": 3},
        "completed_tests": set(),
    })

    assert not core.open_next_via_api(_Page())
    assert core._job.get()["api_attempts"] == {}
    assert core._job.get()["api_current"] == "المخاطرة والعائد"


def test_run_learning_flow_does_not_stuck_when_activity_is_already_open(monkeypatch):
    class _Page:
        url = "https://example.test/lesson/contract"

        def wait_for_timeout(self, _duration):
            pass

    class _Content:
        def inner_text(self, timeout=None):
            return "ما هو العقد؟"

        def locator(self, selector):
            class _Headings:
                def count(self):
                    return 0
            return _Headings()

    monkeypatch.setattr(core, "current_context", lambda _page: "ما هو العقد؟")
    monkeypatch.setattr(core, "main_content", lambda _page: _Content())
    monkeypatch.setattr(core, "visible_page_text", lambda _page, main_text=None: "ما هو العقد؟")
    monkeypatch.setattr(core, "is_quiz_result_page", lambda *_args: False)
    monkeypatch.setattr(core, "solve_one_visible_question", lambda *args: False)
    monkeypatch.setattr(core, "answer_text_question_if_supported", lambda *args: False)
    monkeypatch.setattr(core, "mark_review_points", lambda *args: False)
    monkeypatch.setattr(core, "click_next", lambda *args: False)
    open_attempts = []

    def open_current_activity(*_args):
        open_attempts.append(True)
        core._job.get()["api_current"] = "ما هو العقد؟"
        return False, None

    monkeypatch.setattr(core, "open_next_available_activity", open_current_activity)
    monkeypatch.setattr(core, "dismiss_intro", lambda *args: False)
    monkeypatch.setattr(core, "scroll_quiz_page", lambda _page: False)
    core._hook.set(lambda *args: None)
    core._job.set({
        "leaf": None,
        "worked": set(),
        "announced": set(),
        "scan": {},
        "unmatched": [],
        "stuck": None,
        "tests": [],
        "test_reached": None,
        "completed_tests": set(),
    })

    core.run_learning_flow(_Page(), [])

    assert len(open_attempts) == 2
    assert "بعد إعادة فحص محتواه" in core._job.get()["stuck"]
    assert "الصفحة لم تتغير بعد عدة محاولات" not in core._job.get()["stuck"]


def test_activity_is_current_requires_non_placeholder_content(monkeypatch):
    class _Content:
        def __init__(self, value, headings=None):
            self.value = value
            self.headings = headings or []

        def inner_text(self, timeout=None):
            return self.value

        def locator(self, selector):
            assert selector == "h1, h2"
            return _Matches(self.headings)

    class _Heading:
        def __init__(self, value):
            self.value = value

        def inner_text(self, timeout=None):
            return self.value

    class _Matches:
        def __init__(self, items):
            self.items = items

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    page = object()
    content = {
        "value": _Content(
            "حوار الدرس\nرسالة تعليمية",
            headings=[_Heading("مبادئ التنويع")],
        )
    }
    monkeypatch.setattr(core, "main_content", lambda _page: content["value"])
    assert core._activity_is_current(page, "مبادئ التنويع")
    content["value"] = _Content(
        "اختر درسًا من القائمة\nمبادئ التنويع",
        headings=[_Heading("مبادئ التنويع")],
    )
    assert not core._activity_is_current(page, "مبادئ التنويع")


def test_click_next_scrolls_main_content_for_non_button_controls(monkeypatch):
    class _Matches:
        def count(self):
            return 0

        def all(self):
            return []

    class _Control:
        clicked = False
        scrolled = False

        def scroll_into_view_if_needed(self, **_kwargs):
            self.scrolled = True

        def click(self, **_kwargs):
            self.clicked = True

    control = _Control()

    class _Controls:
        calls = 0

        def evaluate_all(self, _script):
            self.calls += 1
            if self.calls == 1:
                return []
            return [{"i": 0, "text": "التالي", "visible": True}]

        def nth(self, index):
            assert index == 0
            return control

    controls = _Controls()

    class _Content:
        def get_by_role(self, *_args, **_kwargs):
            return _Matches()

        def locator(self, selector):
            assert selector == "button, a, [role='button'], [role='link']"
            return controls

    class _Dialog:
        def count(self):
            return 0

    class _Page:
        def __init__(self):
            self.waits = []

        def get_by_role(self, role):
            assert role == "dialog"
            return _Dialog()

        def wait_for_timeout(self, duration):
            self.waits.append(duration)

    monkeypatch.setattr(core, "main_content", lambda _page: _Content())
    scrolls = iter([True])
    monkeypatch.setattr(core, "scroll_quiz_page", lambda _page: next(scrolls, False))
    page = _Page()

    assert core.click_next(page)
    assert control.scrolled and control.clicked


# ───────── خطة المنهج من بيانات الـ API ─────────
def _curriculum():
    def it(i, title, kind="lesson", done=False, unlocked=True):
        return {"id": i, "title": title, "item_type": kind, "is_completed": done, "is_unlocked": unlocked}
    return [
        {"title": "الوحدة 1", "sections": [{"name": "أ", "items": [it(1, "درس 1", done=True), it(2, "درس 2", done=True)]}]},
        {"title": "الوحدة 2", "sections": [
            {"name": "ب", "items": [
                it(3, "فهم النفقات"),
                it(4, "النقاط المهمة", "review", unlocked=False),
                it(5, "اختبار المعرفة: محاكاة حياة العمل", "quiz", unlocked=False),
            ]}]},
        {"title": "الوحدة 3", "sections": [{"name": "ج", "items": [it(6, "درس 6", unlocked=False)]}]},
    ]


def test_summarize_and_next_pending_skip_tests():
    rows = core.summarize_curriculum(_curriculum())
    assert rows[0] == ("الوحدة 1", 2, 2, False)
    assert rows[1][1:3] == (0, 3) and rows[1][3] is False
    assert rows[2][3] is True                                   # مقفولة بالكامل
    pick = core.next_pending_item(_curriculum())
    assert pick[0] == "item" and pick[3]["title"] == "فهم النفقات"


def test_next_pending_includes_test_when_only_tests_left():
    cur = _curriculum()
    cur[1]["sections"][0]["items"][0]["is_completed"] = True    # خلّص الدرس
    cur[1]["sections"][0]["items"][1]["is_completed"] = True
    cur[1]["sections"][0]["items"][2]["is_unlocked"] = True     # فتح الاختبار
    pick = core.next_pending_item(cur)
    assert pick[0] == "item"
    assert pick[3]["title"] == "اختبار المعرفة: محاكاة حياة العمل"


def test_next_pending_skips_test_completed_in_current_job():
    cur = _curriculum()
    items = cur[1]["sections"][0]["items"]
    items[0]["is_completed"] = True
    items[1]["is_completed"] = True
    items[2]["is_unlocked"] = True

    pick = core.next_pending_item(
        cur, {"اختبار المعرفة: محاكاة حياة العمل"}
    )

    assert pick is None
