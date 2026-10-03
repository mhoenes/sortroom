FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# requirements-lock.txt pins every package (also the indirect ones) to the versions the tests ran with
COPY requirements.txt requirements-api.txt requirements-lock.txt ./
RUN pip install -r requirements-api.txt -c requirements-lock.txt

COPY email_sorter ./email_sorter
COPY config ./config
COPY LICENSE ./

# Sortroom runs unprivileged: as PUID:PGID (default 1000:1000, this user). The container starts as root
# only so the entrypoint can give the mounted folders (config/, mailboxes/, logs/) to that user, then
# it drops root for good - see email_sorter/container.py. Started with --user, it runs as that user.
RUN useradd --system --uid 1000 --home /app sorter \
    && mkdir -p logs mailboxes \
    && chown -R sorter:sorter /app

EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=4)"

ENTRYPOINT ["python", "-m", "email_sorter.container"]
CMD ["uvicorn", "email_sorter.api:app", "--host", "0.0.0.0", "--port", "8765", "--no-access-log"]
