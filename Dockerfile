FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt

RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

COPY app.py /app/app.py
COPY scheduler.py /app/scheduler.py
COPY entrypoint.sh /app/entrypoint.sh
COPY templates /app/templates
COPY static /app/static

RUN chmod +x /app/entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
