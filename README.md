# واجهة ويب — حل الثقافة المالية (v3)

واجهة FastAPI عربية لتشغيل بوت حل الثقافة المالية مع متابعة التقدم عبر WebSocket.

## المتطلب الأساسي

هذا المستودع يحتوي واجهة الويب فقط. يجب إضافة مجلد `finbot_v4_0/` داخل جذر المشروع، ويحتوي على الأقل:

```text
finbot_v4_0/
├── core.py
├── db.py
└── answers.txt
```

وقد يحتاج `core.py` إلى ملفات البوت الأصلية الأخرى. بدون هذا المجلد سيفشل التطبيق عند الإقلاع برسالة `ModuleNotFoundError: core` أو عند عدم العثور على ملف الإجابات.

## التشغيل محليًا

```bash
cp -r /path/to/finbot_v4_0 ./finbot_v4_0
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

افتح `http://localhost:8000`.

## النشر على Railway

المشروع مجهز بـ `Dockerfile` مبني على صورة Playwright الرسمية، و`railway.json` يستخدم `/health` كفحص صحة.

1. أضف مجلد `finbot_v4_0/` قبل النشر.
2. أنشئ Service من هذا المستودع في Railway.
3. أضف متغيرات البيئة المذكورة أدناه.
4. لا تضبط `PORT` يدويًا عادةً؛ Railway يحقنه تلقائيًا، والتطبيق يقرأه.

## متغيرات البيئة

### مطلوب

- `BOT_DIR=/app/finbot_v4_0` — مسار مجلد البوت داخل حاوية Railway.

### موصى بها على Railway

- `TRUST_PROXY=1` — قراءة عنوان IP الحقيقي خلف البروكسي.
- `MAX_JOBS=1` — بداية آمنة للذاكرة؛ ارفعها فقط بعد اختبار استهلاك Playwright.
- `MAX_QUEUE=10`
- `JOB_TIMEOUT_MIN=240`
- `DB_PATH=/app/finbot_v4_0/bot.db`
- `ANSWERS_FILE=answers.txt`
- `WS_RATE_LIMIT=6`
- `PROFILE_SLEEPS=0`

### اختيارية

- `ALLOWED_ORIGINS=https://your-domain.example` — دومينات إضافية مسموحة للـ WebSocket/CORS. اتركها فارغة إذا كانت الواجهة من نفس الدومين.

> لا تضع كلمات مرور المستخدمين أو أي مفاتيح سرية في GitHub أو في `.env.example`. بيانات الدخول التي يرسلها المستخدمون تمر إلى البوت وقت التشغيل فقط.
