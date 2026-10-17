FROM python:3.11-slim

# Install system utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    cron \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python dependencies
COPY app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy entire repository
COPY . .

# Create application directories and set permissions
RUN mkdir -p /var/sewasetu/uploads /var/log/sewasetu \
    && chmod +x start.sh

EXPOSE 8000

CMD ["/bin/sh", "start.sh"]
