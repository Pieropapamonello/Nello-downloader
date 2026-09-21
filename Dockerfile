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
    ffmpeg fonts-dejavu-core tesseract-ocr tesseract-ocr-all git curl ca-certificates gnupg unzip gcc \
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
RUN curl -fL --retry 3 https://huggingface.co/Pomni/whisper-small-ggml-allquants/resolve/533726aaaec964d851d3b2e403be2d7957ddd94d/ggml-small-q4_1.bin -o /opt/whisper/ggml-small-q4_1.bin \
 && echo '08642d58aa9aaa2a348eb248f366096f6d109eddd00a4001c03fa4310e5f6850  /opt/whisper/ggml-small-q4_1.bin' | sha256sum -c -
RUN curl -fL --retry 3 https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base-q5_1.bin -o /opt/whisper/ggml-base-q5_1.bin \
 && echo '422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898  /opt/whisper/ggml-base-q5_1.bin' | sha256sum -c -
RUN curl -fL --retry 3 https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin -o /opt/whisper/ggml-silero-v5.1.2.bin \
 && echo '29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf  /opt/whisper/ggml-silero-v5.1.2.bin' | sha256sum -c -
COPY fonts/ /usr/local/share/fonts/nello/
COPY *.py ./
COPY fonts/ ./fonts/
COPY start-downloader.sh ./
ENV DENO_V8_FLAGS=--max-old-space-size=144,--max-semi-space-size=1,--jitless
ENV OMP_NUM_THREADS=1 OMP_THREAD_LIMIT=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
EXPOSE 10000
CMD ["bash", "start-downloader.sh"]
