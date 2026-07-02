# syntax=docker/dockerfile:1
# Imagen del servidor web de Gemini Live (FastAPI + WebSocket).
# Inspirada en el Dockerfile de agente_gkm: uv + build reproducible + usuario no-root.
#
# Nota: esta imagen corre SOLO el servidor web (gemini-live-web), que NO usa
# microfono/altavoz locales (el audio va por WebSocket desde el navegador).
# Por eso NO instala PortAudio y NO necesita /dev/snd: ideal para Dokploy.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_LINK_MODE=copy

WORKDIR /app

# Usuario no privilegiado (buenas practicas)
ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

# Instalar uv (gestor de paquetes rapido)
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

# Instalar dependencias primero (capa cacheada si pyproject.toml/uv.lock no cambian)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Instalar el paquete + servir el frontend estatico.
COPY src ./src
COPY frontend ./frontend
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

USER appuser

# El servidor escucha en 0.0.0.0:8000 (ver web/server.py:main()).
ENV HOST=0.0.0.0
ENV PORT=8000
EXPOSE 8000

ENTRYPOINT [".venv/bin/gemini-live-web"]
