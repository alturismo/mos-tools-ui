FROM python:3.13-slim

WORKDIR /app

# Python dependencies
COPY requirements.txt /tmp/requirements.txt

RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# MOS Tools WebUI
COPY app.py /app/app.py
COPY scheduler.py /app/scheduler.py
COPY entrypoint.sh /app/entrypoint.sh
COPY templates /app/templates
COPY static /app/static

# Host agent payload.
# These files are copied to persistent MOS appdata on first start only.
COPY agent /opt/mos-tools/agent

RUN chmod +x /app/entrypoint.sh \
    && find /opt/mos-tools/agent -maxdepth 1 -type f -name "*.sh" -exec chmod +x {} \;

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
