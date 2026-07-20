# Platform API Dockerfile
# Python FastAPI — Engineering Platform Control Plane

FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
COPY templates/ templates/
COPY .github/workflows/platform-rollback.yml .github/workflows/platform-rollback.yml

RUN python -m pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e "."

ENV ENG_PLATFORM_MOCK_MODE=true

RUN useradd --create-home --uid 10001 appuser

USER appuser

EXPOSE 8000

CMD ["uvicorn", "eng_platform_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
