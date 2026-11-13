FROM cloudflare/cloudflared:2026.11.0 AS cloudflared

FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FINANCE_DATA_DIR=/data \
    FINANCE_OPTIONS=/data/options.json

COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared

WORKDIR /opt/finance
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
