FROM python:3.12-slim

WORKDIR /app

# Install ffmpeg, curl and yt-dlp dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install deno (JS runtime required by yt-dlp for YouTube extraction)
RUN curl -fsSL https://deno.land/install.sh | sh && \
    ln -s /root/.deno/bin/deno /usr/local/bin/deno

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create downloads directory
RUN mkdir -p downloads

EXPOSE 5000

CMD gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 300 app:app
