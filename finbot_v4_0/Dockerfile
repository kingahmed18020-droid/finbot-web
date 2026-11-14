# صورة Playwright الرسمية: فيها Chromium + كل مكتبات النظام جاهزة.
# رقم النسخة هنا لازم يطابق playwright== في requirements.txt
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HEADLESS=true \
    DB_PATH=/data/bot.db

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# مكان قاعدة البيانات (اربط عليه Volume في Railway عشان الداتا متضيعش مع كل نشر)
RUN mkdir -p /data

# فحص وقت البناء: لو المتصفح أو الملفات ناقصة البناء يفشل هنا بدل ما البوت يقع بعد النشر
RUN python selfcheck.py --build

CMD ["sh", "-c", "python selfcheck.py && exec python bot.py"]
