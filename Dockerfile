FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -r -u 10001 gateway && mkdir -p /data && chown gateway:gateway /data
COPY app.py .
COPY scripts/ scripts/
USER gateway
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8080","--workers","1"]
