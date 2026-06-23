FROM python:3.12-slim

WORKDIR /app

# Install ffmpeg and yt-dlp dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create downloads directory
RUN mkdir -p downloads

EXPOSE 5000

CMD gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 300 app:app
