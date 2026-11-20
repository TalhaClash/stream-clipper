FROM python:3.13-slim

# Install system dependencies including FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fontconfig \
    curl \
    git \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno: the JavaScript runtime yt-dlp needs to solve YouTube's JS
# challenges. Without it YouTube extraction is deprecated and the
# "Sign in to confirm you're not a bot" wall appears far more often.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && deno --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Whisper 'base' model, downloaded once into the image so the app user can
# read it at runtime without re-downloading on every deploy.
RUN python3 -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8', download_root='/app/models')" \
    && chmod -R a+rX /app/models

COPY . .

# Montserrat ExtraBold (the heavy viral-subtitle font, resolved by libass
# via fontconfig) and the YuNet face-detection model are downloaded at
# build time so the repo stays text-only — no binaries checked in.
RUN mkdir -p /usr/share/fonts/truetype/montserrat /app/src/models \
    && curl -fsSL -o /usr/share/fonts/truetype/montserrat/Montserrat-ExtraBold.ttf \
         https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-ExtraBold.ttf \
    && chmod 644 /usr/share/fonts/truetype/montserrat/Montserrat-ExtraBold.ttf \
    && curl -fsSL -o /app/src/models/face_detection_yunet_2023mar.onnx \
         https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
    && chmod 644 /app/src/models/face_detection_yunet_2023mar.onnx \
    && fc-cache -f

# Run as a non-root user. uid 1000 matches the host's opc user, who owns
# the bind-mounted data files, so the app can still write them.
RUN groupadd -g 1000 app && useradd -u 1000 -g app -m -d /home/app app \
    && mkdir -p /home/app/.cache && chown -R app:app /app /home/app
ENV HOME=/home/app
USER app

EXPOSE 8000

CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
