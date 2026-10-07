# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    HOST=0.0.0.0

WORKDIR /srv

COPY fitsaudit/ fitsaudit/
COPY verify/ verify/
COPY tests/ tests/

# Build step: byte-compile everything and run as an unprivileged user.
RUN python -m compileall -q fitsaudit verify tests \
    && useradd --system --uid 10001 --home /srv fitsaudit \
    && chown -R fitsaudit:fitsaudit /srv

USER fitsaudit

EXPOSE 8000

CMD ["python", "-m", "fitsaudit"]
