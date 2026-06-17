FROM python:3.11-slim

WORKDIR /app

# Install system deps for pdfplumber
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpoppler-cpp-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.worker.txt .
RUN pip install --no-cache-dir -r requirements.worker.txt

# Copy source code
COPY internal/ ./internal/
COPY Skills/ ./Skills/
COPY worker/ ./worker/

# Make packages importable
RUN touch internal/__init__.py worker/__init__.py 2>/dev/null || true

CMD ["python3", "-m", "worker.entrypoint"]
