# syntax=docker/dockerfile:1
FROM python:3.11-slim

# system deps (ffmpeg may be needed depending on elevenlabs sdk usage; keep small)
RUN apt-get update && apt-get install -y --no-install-recommends \
  ffmpeg \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# copy only requirements first to leverage cache
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# copy app code
COPY . /app

# uvicorn settings
ENV PORT 8000
EXPOSE 8000

# non-root user
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
