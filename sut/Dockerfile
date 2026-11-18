# Объект исследования: один образ для всех ролей (mono | auth | catalog | delivery | telemetry).
# Базовый образ и uv закреплены по тегу и digest; зависимости — по uv.lock (--frozen).
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
# uv 0.5.11 ставится из PyPI (файлы версии на PyPI неизменяемы): так сборка не зависит от ghcr.io,
# где Docker Desktop может подставить устаревшие учётные данные пользователя (см. ПР4, «Отладка»).
RUN pip install --no-cache-dir --disable-pip-version-check uv==0.5.11

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app

RUN useradd --uid 10001 --no-create-home appuser
USER appuser
ENV PATH="/app/.venv/bin:$PATH" ROLE=mono WORKERS=1 PROMETHEUS_MULTIPROC_DIR=/tmp/prom
EXPOSE 8000
# Журнал доступа выключен: запись строки на каждый запрос искажает замер CPU и задержки.
# keep-alive 75 с > 60 с у nginx: иначе nginx шлёт запрос в закрытое uvicorn соединение (ложные 502).
CMD ["sh", "-c", "rm -rf $PROMETHEUS_MULTIPROC_DIR && mkdir -p $PROMETHEUS_MULTIPROC_DIR && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers $WORKERS --loop uvloop --http httptools --no-access-log --log-level warning --timeout-keep-alive 75"]
