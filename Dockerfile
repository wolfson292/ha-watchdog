FROM python:3.13-alpine

RUN apk add --no-cache openssh-client

COPY watchdog.py /app/watchdog.py

ENV PYTHONUNBUFFERED=1
VOLUME /data

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import os,time,sys; sys.exit(time.time()-os.path.getmtime('/tmp/heartbeat') > 300)"

CMD ["python", "/app/watchdog.py"]
