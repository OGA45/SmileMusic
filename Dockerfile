FROM python:3.9.12-bullseye
USER root

ENV SMILEMUSIC_PREFIX=?
ENV SMILEMUSIC_ENV=Prod

ENV TZ JST-9
ENV TERM xterm

COPY ./requirements.txt /opt
COPY ./python /opt
WORKDIR /opt

RUN apt-get update
RUN apt-get install -y libpq-dev

RUN \
 apt-get update -qq && apt-get -y install \
 autoconf automake build-essential cmake git-core libass-dev \
 libfreetype6-dev libtool pkg-config texinfo wget zlib1g-dev
RUN apt-get -y install software-properties-common
RUN apt-add-repository non-free
RUN apt-get update
RUN \
 apt-get -y install nasm  libnuma-dev  libvpx-dev libfdk-aac-dev libmp3lame-dev libopus-dev libaom-dev \
 libssl-dev libogg-dev libvorbis-dev libtheora-dev
RUN mkdir -p ~/ffmpeg_sources ~/bin
RUN \
 cd ~/ffmpeg_sources/ && \
 wget -O ffmpeg-snapshot.tar.bz2 https://ffmpeg.org/releases/ffmpeg-snapshot.tar.bz2 && \
 tar xjvf ffmpeg-snapshot.tar.bz2 && \
 cd ffmpeg && \
 PATH="$HOME/bin:$PATH" PKG_CONFIG_PATH="$HOME/ffmpeg_build/lib/pkgconfig" ./configure \
  --prefix="$HOME/ffmpeg_build" \
  --pkg-config-flags="--static" \
  --disable-ffplay \
  --extra-cflags="-I$HOME/ffmpeg_build/include" \
  --extra-ldflags="-L$HOME/ffmpeg_build/lib" \
  --extra-libs="-lpthread -lm" \
  --bindir="/bin" \
  --enable-gpl \
  --enable-libaom \
  --enable-libass \
  --enable-libfdk-aac \
  --enable-libfreetype \
  --enable-libmp3lame \
  --enable-libopus \
  --enable-libvorbis \
  --enable-libvpx \
  --enable-libtheora \
  --enable-openssl \
  --enable-nonfree && \
 PATH="$HOME/bin:$PATH" make -j20 && \
 make install && \
 hash -r

RUN pip install --upgrade pip

RUN pip install -r requirements.txt

CMD ["python", "smile_music.py"]