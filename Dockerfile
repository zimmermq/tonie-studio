# By digest as well as tag: the tag moves with every rebuild upstream, so the
# same commit could otherwise build on a different base. Renovate moves both.
FROM python:3.15-slim@sha256:2da676046630d7bf14c6764ae1143dc7fa0377a53f94bf5a642f0b214aca5bce

# The audio archive is mounted read-only at /archive and browser uploads are
# spooled under /data; both come from volumes. Europe/Berlin so a job timestamp
# shown in the UI matches the clock on the wall. TMPDIR on the data volume too:
# an upload is buffered in a temporary file before it is spooled, and in the
# container's own /tmp a folder of FLACs counts against the pod's ephemeral
# storage, which gets it evicted.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Berlin \
    TS_ARCHIVE_DIR=/archive \
    TS_DATA_DIR=/data \
    TMPDIR=/data/tmp \
    PYTHONPATH=/app/src

WORKDIR /app

# requirements.txt is the lock compiled from requirements.in, every package
# with its hashes: what is installed is exactly what was locked, or nothing.
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY src/ ./src/

# Non-root. /archive is only ever read, so it is created here merely to exist
# when no volume is mounted over it.
RUN useradd --uid 10001 --create-home app \
    && mkdir -p /archive /data \
    && chown -R app:app /data
USER app

# The release this image is, written into every backup's manifest and shown
# at the top of every page with the commit it was built from. CI passes both
# on release; the defaults keep a source run sane. Placed late so a version
# bump does not invalidate the dependency layers above.
ARG TS_VERSION=dev
ENV TS_VERSION=$TS_VERSION
ARG TS_COMMIT=
ENV TS_COMMIT=$TS_COMMIT

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "--factory", "tonie_studio.main:app", \
     "--host", "0.0.0.0", "--port", "8080"]
