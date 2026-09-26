FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt requirements-api.txt ./
RUN pip install -r requirements-api.txt

COPY email_sorter ./email_sorter
COPY config ./config

# unprivileged user; mailboxes/, data/, logs/ and reports/ are mounted as volumes
RUN useradd --system --uid 1000 --home /app sorter \
    && mkdir -p logs mailboxes \
    && chown -R sorter:sorter /app
USER sorter

EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=4)"

CMD ["uvicorn", "email_sorter.api:app", "--host", "0.0.0.0", "--port", "8765", "--no-access-log"]
