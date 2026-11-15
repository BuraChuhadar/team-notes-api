FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home --uid 10001 app

COPY --chown=app:app app ./app
COPY --chown=app:app scripts ./scripts
USER app

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=10 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).close()"]

CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers \"${WEB_CONCURRENCY:-1}\" --limit-concurrency \"${API_CONCURRENCY_LIMIT:-100}\" --timeout-keep-alive \"${API_KEEP_ALIVE_SECONDS:-5}\" --timeout-graceful-shutdown \"${API_GRACEFUL_SHUTDOWN_SECONDS:-25}\""]

FROM runtime AS test
USER root
COPY requirements-dev.txt .
RUN python -m pip install --no-cache-dir -r requirements-dev.txt
COPY pyproject.toml .
COPY --chown=app:app tests ./tests
USER app
HEALTHCHECK NONE
CMD ["sh", "-c", "python -m pip check && python -m ruff check --no-cache app tests scripts && python -m pyright && python -W error::DeprecationWarning -W error::PendingDeprecationWarning -m unittest discover -s tests -v"]

FROM runtime AS production
