# =====================================================================
# Stage 1: Discord Activity フロント (React + Vite) のビルド
# =====================================================================
FROM node:20-bookworm AS activity_builder
WORKDIR /app

# Vite は `VITE_` プレフィックス付きの env をビルド時にバンドルへ焼き込む。
# docker-compose の build.args から SMILEMUSIC3_DISCORD_CLIENT_ID を渡す。
ARG VITE_DISCORD_CLIENT_ID
ARG VITE_TOKEN_ENDPOINT=/.proxy/api/token
ENV VITE_DISCORD_CLIENT_ID=$VITE_DISCORD_CLIENT_ID
ENV VITE_TOKEN_ENDPOINT=$VITE_TOKEN_ENDPOINT

COPY activity/package.json activity/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; \
    else npm install --no-audit --no-fund; fi
COPY activity/ ./
RUN npm run build


# =====================================================================
# Stage 2: BOT + FastAPI (Python)
# =====================================================================
FROM python:3.14-bookworm
USER root

ENV SMILEMUSIC_PREFIX=?
ENV SMILEMUSIC_ENV=Prod

ENV TZ JST-9
ENV TERM xterm

COPY ./ffmpeg /bin
COPY ./ffplay /bin
COPY ./ffprobe /bin

COPY ./requirements.txt /opt
COPY ./python /opt
WORKDIR /opt


# apt の lists キャッシュが壊れているとビルド全体が落ちるので、
# clean → update を 3 回までリトライする。GPG 署名エラーは
# 古い lists を消すと回復するケースが多い。
RUN set -eux; \
    for i in 1 2 3; do \
        rm -rf /var/lib/apt/lists/*; \
        apt-get clean; \
        if apt-get update -o Acquire::Retries=3 -o Acquire::ForceIPv4=true; then \
            break; \
        fi; \
        echo "apt-get update failed, retry $i..."; \
        sleep 3; \
    done
RUN apt-get -y install --no-install-recommends \
        software-properties-common libopus-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

RUN pip install -r requirements.txt

ENV DENO_INSTALL="/root/.deno"
ENV PATH="${DENO_INSTALL}/bin:${PATH}"
RUN curl -fsSL https://deno.land/install.sh | sh

# Stage 1 でビルドした Activity フロントを取り込む。FastAPI が StaticFiles で配信する。
# `./python:/opt` の bind mount に上書きされないよう /opt の外に置く。
COPY --from=activity_builder /app/dist /srv/activity_dist

# Activity API (FastAPI / WebSocket) を公開するポート
EXPOSE 8080

CMD ["python", "smile_music3.py"]