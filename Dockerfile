FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY sepa_scanner ./sepa_scanner
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["uvicorn", "sepa_scanner.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
