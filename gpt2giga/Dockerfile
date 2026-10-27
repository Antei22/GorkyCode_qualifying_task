# Optional local OpenAI-compatible proxy for GigaChat; credentials are excluded.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "gpt2giga==0.3.0"
RUN useradd --create-home --uid 10001 gptproxy \
    && mkdir -p /app/data \
    && chown -R gptproxy:gptproxy /app
USER gptproxy
EXPOSE 8090
CMD ["gpt2giga", "--proxy.mode", "PROD", "--proxy.host", "0.0.0.0", "--proxy.port", "8090", "--proxy.model-max-connections-default", "1", "--proxy.log-filename", "/app/data/gpt2giga.log"]
