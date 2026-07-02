# syntax=docker/dockerfile:1
# Imagen de la demo de voz Gemini Live.
# Inspirada en el Dockerfile de agente_gkm: uv + build reproducible + usuario no-root.
# Diferencias: es un CLI (no un servidor web), sin healthcheck ni puerto expuesto.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_LINK_MODE=copy

WORKDIR /app

# PortAudio: sounddevice (wrapper CFFI) lo necesita en runtime para abrir
# el microfono y los altavoces. Sin esta lib, "import sounddevice" falla.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

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

# Instalar el paquete (solo codigo; deps ya instaladas).
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

USER appuser

# Por defecto muestra la ayuda (no revienta si el contenedor no tiene audio).
# Para conversar:  docker run --rm -it --device /dev/snd <imagen> run
# (el passthrough de audio /dev/snd solo funciona con host Linux)
ENTRYPOINT [".venv/bin/gemini-live-demo"]
CMD ["--help"]
