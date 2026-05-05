FROM python:3.12-slim

# Set labels for the image
LABEL maintainer="zephyr-bot"
LABEL description="Inventory-skewed market making bot for Kraken"

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash botuser
USER botuser

# Entry point — allows passing CLI flags via docker run
ENTRYPOINT ["python", "-m", "src.main"]

# Default: dry-run for safety
CMD ["--dry-run", "--log-level", "INFO"]
