# Источник воздействия для экспериментов E2/E3: эмулированные плееры.
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0
# uv 0.5.11 ставится из PyPI (файлы версии на PyPI неизменяемы): так сборка не зависит от ghcr.io,
# где Docker Desktop может подставить устаревшие учётные данные пользователя (см. ПР4, «Отладка»).
RUN pip install --no-cache-dir --disable-pip-version-check uv==0.5.11

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY player ./player

RUN useradd --uid 10001 --no-create-home appuser
USER appuser
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["python", "-m", "player.run"]
