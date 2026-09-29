FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN python -m pip wheel --wheel-dir /wheels .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/home/roomos/.local/bin:$PATH
RUN groupadd --gid 10001 roomos \
    && useradd --uid 10001 --gid roomos --create-home --shell /usr/sbin/nologin roomos
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* && rm -rf /wheels
# Pre-create the auth state directory with the right ownership and mode. An
# empty Docker volume mounted here inherits both, so the unprivileged runtime
# user owns its own auth store with no host-side setup.
RUN install -d -o 10001 -g 10001 -m 700 /var/lib/roomos-auth
WORKDIR /app
USER 10001:10001
EXPOSE 5501
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5501", "--proxy-headers", "--forwarded-allow-ips=127.0.0.1"]
