# Platform API Dockerfile
# Python FastAPI — Engineering Platform Control Plane

FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir -e "."

ENV ENG_PLATFORM_MOCK_MODE=true

EXPOSE 8000

CMD ["uvicorn", "eng_platform_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
