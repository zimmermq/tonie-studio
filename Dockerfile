# By digest as well as tag: the tag moves with every rebuild upstream, so the
# same commit could otherwise build on a different base. Renovate moves both.
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

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

# ffmpeg cuts the chapters out of a single long file (see splitting.py); it
# only copies the audio, the encode itself is teddyCloud's. A static build,
# since Debian's package brings some 400 MB of video and display libraries
# along. By digest as well as tag, as the base image is.
COPY --from=mwader/static-ffmpeg:9.0.2-1@sha256:f5d100184884eb3e82b0eb14bb5ebf98da99826ac824989720297fb9ef21c822 /ffmpeg /usr/local/bin/ffmpeg

# requirements.txt is the lock compiled from requirements.in, every package
# with its hashes: what is installed is exactly what was locked, or nothing.
# Wheels only: the slim image has no compiler, and a package without a wheel
# for this Python on amd64 and arm64 should fail here by name, not half way
# through building it.
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt

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
