# GX-Daemon: headless genomics analysis (Carrier Screening + Platform integration).
# Based on service-daemon Dockerfile layout.

FROM python:3.11-slim

ARG TZ=Asia/Seoul
ARG HOST_UID=1000
ARG HOST_GID=1000
ARG DOCKER_GID=999

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=${TZ} \
    PYTHONUNBUFFERED=1

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      bash ca-certificates curl gnupg tzdata procps \
      openjdk-21-jre-headless \
      libglib2.0-0 libpango-1.0-0 libpangocairo-1.0-0 \
      libcairo2 libffi8 libfontconfig1 fonts-liberation \
      fonts-noto-cjk fonts-noto-cjk-extra; \
    install -m 0755 -d /etc/apt/keyrings; \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg; \
    chmod a+r /etc/apt/keyrings/docker.gpg; \
    . /etc/os-release; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends docker-ce-cli; \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime; echo $TZ > /etc/timezone; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    cd /tmp; \
    curl -fsSL https://get.nextflow.io | bash; \
    install -m 0755 nextflow /usr/local/bin/nextflow; \
    rm -f nextflow; \
    /usr/local/bin/nextflow -version

RUN groupadd -g ${HOST_GID} ken || true && \
    useradd -u ${HOST_UID} -g ${HOST_GID} -m -s /bin/bash ken || true && \
    groupadd -g ${DOCKER_GID} docker || true && \
    usermod -aG docker ken

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY data/ ./data/

RUN mkdir -p /app/logs /data/fastq /data/analysis /data/output /data/log /data/gx-daemon && \
    chown -R ken:ken /app /data

EXPOSE 8000

USER ken

CMD ["/bin/sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT:-8000} --no-access-log"]
