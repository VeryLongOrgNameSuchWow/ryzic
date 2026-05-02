# syntax=docker/dockerfile:1.7

# Build stage: install uv, materialize the locked dependency tree.
FROM python:3.13-slim-bookworm AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/ryzic

WORKDIR /src

# Cache deps independently from source so source-only changes skip the resolver.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable --no-dev


# Runtime stage: copy the prepared environment, drop privileges.
FROM python:3.13-slim-bookworm AS runtime

ENV PATH="/opt/ryzic/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RYZIC_CACHE_DIR=/var/cache/ryzic

RUN groupadd --gid 1001 ryzic \
 && useradd --uid 1001 --gid ryzic --home-dir /home/ryzic --create-home --shell /usr/sbin/nologin ryzic \
 && mkdir -p /var/cache/ryzic \
 && chown -R ryzic:ryzic /var/cache/ryzic

COPY --from=build --chown=ryzic:ryzic /opt/ryzic /opt/ryzic

USER ryzic
WORKDIR /home/ryzic

ENTRYPOINT ["python", "-m", "ryzic"]
