FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/London

RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata && \
    ln -sf /usr/share/zoneinfo/Europe/London /etc/localtime && \
    echo "Europe/London" > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g "${APP_GID}" app && useradd -u "${APP_UID}" -g app -m app
WORKDIR /app

COPY pyproject.toml README.md LICENSE uv.lock /app/
COPY tradedesk/ /app/tradedesk/

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

RUN mkdir -p /data && chown -R app:app /data

USER app
VOLUME ["/data"]

ENTRYPOINT ["python"]
