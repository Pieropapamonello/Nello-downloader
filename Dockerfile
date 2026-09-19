FROM python:3.11-slim AS speech-build
RUN apt-get update && apt-get install -y --no-install-recommends git cmake g++ make curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch v1.7.6 https://github.com/ggml-org/whisper.cpp.git /src/whisper \
 && cmake -S /src/whisper -B /src/whisper/build -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF -DWHISPER_BUILD_TESTS=OFF \
 && cmake --build /src/whisper/build --config Release --target whisper-cli -j2 \
 && mkdir -p /opt/whisper \
 && cp /src/whisper/build/bin/whisper-cli /opt/whisper/whisper-cli
RUN curl -fL --retry 3 https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny-q5_1.bin \
      -o /opt/whisper/ggml-tiny-q5_1.bin \
 && echo '818710568da3ca15689e31a743197b520007872ff9576237bda97bd1b469c3d7  /opt/whisper/ggml-tiny-q5_1.bin' | sha256sum -c -

FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg fonts-dejavu-core tesseract-ocr tesseract-ocr-eng git curl ca-certificates gnupg unzip gcc \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip -o /tmp/deno.zip \
 && unzip /tmp/deno.zip -d /usr/local/bin/ && rm /tmp/deno.zip
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
 && cd /opt/bgutil/server && npm install && npx tsc
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN curl -fL --retry 3 https://argos-net.com/v1/translate-en_it-1_0.argosmodel -o /tmp/en_it.zip \
 && echo 'dde2180001a47904ecbbd688a41e35db8a040e4fd5b52e4f29b4bb499516ab32  /tmp/en_it.zip' | sha256sum -c - \
 && mkdir -p /opt/translation \
 && unzip /tmp/en_it.zip 'en_it/model/*' 'en_it/sentencepiece.model' 'en_it/metadata.json' 'en_it/README.md' -d /opt/translation \
 && rm /tmp/en_it.zip
COPY --from=speech-build /opt/whisper /opt/whisper
COPY *.py ./
COPY start-downloader.sh ./
ENV DENO_V8_FLAGS=--max-old-space-size=144,--max-semi-space-size=1,--jitless
ENV OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
EXPOSE 10000
CMD ["bash", "start-downloader.sh"]
