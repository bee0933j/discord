FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 HEADLESS=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && python -m playwright install --with-deps chromium
COPY bot.py ./
CMD ["python", "-u", "bot.py"]
