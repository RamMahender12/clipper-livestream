# Clipper image: Python app + ffmpeg (with libass for captions) + whisper.cpp
# + streamlink. Built on the server itself, so whisper.cpp is compiled for
# that CPU.

# ---- whisper.cpp, built from a pinned release
FROM debian:trixie-slim AS whisper
ARG WHISPER_VERSION=v1.9.5
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${WHISPER_VERSION}" https://github.com/ggml-org/whisper.cpp /src
WORKDIR /src
RUN cmake -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DWHISPER_BUILD_TESTS=OFF \
 && cmake --build build -j"$(nproc)" --target whisper-cli
RUN curl -fsSL -o /ggml-base.en.bin \
    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin

# ---- runtime
FROM python:3.13-slim-trixie
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fontconfig procps curl ca-certificates libgomp1 tzdata \
 && rm -rf /var/lib/apt/lists/*
# Caption font (Arial Black isn't on Linux). Anton: SIL Open Font License.
RUN mkdir -p /usr/share/fonts/truetype/anton \
 && curl -fsSL -o /usr/share/fonts/truetype/anton/Anton-Regular.ttf \
    https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf \
 && fc-cache -f
COPY --from=whisper /src/build/bin/whisper-cli /usr/local/bin/whisper-cli
RUN pip install --no-cache-dir "anthropic>=2.0,<3" "streamlink==8.6.3"

WORKDIR /app
COPY --from=whisper /ggml-base.en.bin /app/models/ggml-base.en.bin
COPY pyproject.toml config.example.toml ./
COPY clipper ./clipper

ENV PYTHONUNBUFFERED=1 \
    CLIPPER_SERVICE=1
# config.toml, secrets/ and data/ are mounted from the host (see docker-compose.yml)
CMD ["python", "-m", "clipper", "run"]
