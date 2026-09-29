FROM python:3.14-slim

# The audio archive is mounted read-only at /archive and browser uploads are
# spooled under /data; both come from volumes. Europe/Berlin so a job timestamp
# shown in the UI matches the clock on the wall.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Berlin \
    TS_ARCHIVE_DIR=/archive \
    TS_DATA_DIR=/data \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Non-root. /archive is only ever read, so it is created here merely to exist
# when no volume is mounted over it.
RUN useradd --uid 10001 --create-home app \
    && mkdir -p /archive /data \
    && chown -R app:app /data
USER app

# Build identity for the footer. CI passes these on release; the defaults keep
# a source run sane. Placed late so a version bump does not invalidate the
# dependency layers above.
ARG TS_VERSION=dev
ARG TS_COMMIT=unknown
ENV TS_VERSION=$TS_VERSION \
    TS_COMMIT=$TS_COMMIT

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "--factory", "tonie_studio.main:app", \
     "--host", "0.0.0.0", "--port", "8080"]
