FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
# Runtime dependencies come only from the hashed lock so rebuilding a commit is reproducible;
# pyproject.toml keeps ranges for development installs.
COPY requirements.lock ./
RUN python -m pip wheel --require-hashes --only-binary=:all: --no-deps --wheel-dir /wheels -r requirements.lock
COPY pyproject.toml ./
COPY app ./app
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12-slim

ARG ROOMOS_RELEASE=development
ARG ROOMOS_REVISION=unknown
ARG ROOMOS_BUILD_CREATED=1970-01-01T00:00:00Z
ARG ROOMOS_SOURCE_URI=urn:roomos:source

LABEL org.opencontainers.image.revision="${ROOMOS_REVISION}" \
      org.opencontainers.image.version="${ROOMOS_RELEASE}" \
      org.opencontainers.image.created="${ROOMOS_BUILD_CREATED}" \
      org.opencontainers.image.source="${ROOMOS_SOURCE_URI}" \
      org.opencontainers.image.title="RoomOS Core" \
      io.metehantech.roomos.component="core"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/home/roomos/.local/bin:$PATH \
    ROOMOS_RELEASE=${ROOMOS_RELEASE} \
    ROOMOS_REVISION=${ROOMOS_REVISION} \
    ROOMOS_COMPONENT=core
RUN groupadd --gid 10001 roomos \
    && useradd --uid 10001 --gid roomos --create-home --shell /usr/sbin/nologin roomos
COPY --from=builder /wheels /wheels
COPY requirements.lock tools/check_lock.py /tmp/lock/
RUN python -m pip install --no-cache-dir --no-index --find-links /wheels --require-hashes -r /tmp/lock/requirements.lock \
    && python -m pip install --no-cache-dir --no-index --no-deps /wheels/roomos_core-*.whl \
    && python -m pip check \
    && python /tmp/lock/check_lock.py --installed --lock /tmp/lock/requirements.lock \
    && rm -rf /wheels /tmp/lock
WORKDIR /app
USER 10001:10001
EXPOSE 5501
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5501", "--proxy-headers", "--forwarded-allow-ips=127.0.0.1"]
