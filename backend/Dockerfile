FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg gnupg wget \
    && install -d /usr/share/postgresql-common/pgdg \
    && wget -qO /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 smokeline \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin smokeline \
    && install -d -o 10001 -g 10001 /data /data/tmp /data/media-store /data/backups

WORKDIR /opt/smokeline/backend
COPY backend/requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock
COPY backend/ .

FROM runtime AS test
COPY backend/requirements-dev.lock ./requirements-dev.lock
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-dev.lock
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "app.scripts.start"]

FROM runtime AS production
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "app.scripts.start"]
