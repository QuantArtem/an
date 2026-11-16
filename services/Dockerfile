# Один Dockerfile на все сервисы: имя пакета сервиса передаётся аргументом сборки.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ARG SERVICE
COPY libs/lf_common ./lf_common
COPY services/${SERVICE} ./${SERVICE}
ENV SERVICE_MODULE=${SERVICE}
RUN useradd --system app
USER app
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --retries=20 \
  CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/health', timeout=2)"
CMD ["sh", "-c", "exec uvicorn ${SERVICE_MODULE}.main:app --host 0.0.0.0 --port 8000"]
