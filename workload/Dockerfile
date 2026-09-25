# syntax=docker/dockerfile:1.7
# One image, three roles. SERVICE_NAME and DOWNSTREAM select behaviour, so the
# reference topology is configuration rather than code.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY service.py ./

RUN groupadd --system --gid 10002 wl \
 && useradd --system --uid 10002 --gid wl wl
USER wl

EXPOSE 8080
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]
