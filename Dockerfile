FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg git curl ca-certificates gnupg unzip gcc \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip -o /tmp/deno.zip \
 && unzip /tmp/deno.zip -d /usr/local/bin/ && rm /tmp/deno.zip
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
 && cd /opt/bgutil/server && npm install && npx tsc
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY start-downloader.sh ./
ENV DENO_V8_FLAGS=--max-old-space-size=144,--max-semi-space-size=1,--jitless
EXPOSE 10000
CMD ["bash", "start-downloader.sh"]
