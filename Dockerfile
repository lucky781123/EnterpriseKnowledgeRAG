FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY scripts ./scripts

ARG PIP_EXTRAS=""

RUN python -m pip install --no-cache-dir --upgrade pip \
    && if [ -n "$PIP_EXTRAS" ]; then \
         python -m pip install --no-cache-dir ".[$PIP_EXTRAS]"; \
       else \
         python -m pip install --no-cache-dir .; \
       fi \
    && useradd --create-home --uid 10001 appuser

USER appuser

EXPOSE 8000

CMD ["uvicorn", "enterprise_rag.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
