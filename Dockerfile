FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 appuser

USER appuser

EXPOSE 8000

CMD ["uvicorn", "enterprise_rag.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
