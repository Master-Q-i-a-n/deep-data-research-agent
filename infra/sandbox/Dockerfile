FROM python:3.13-slim-bookworm

ARG DEBIAN_MIRROR=https://mirrors.aliyun.com
ARG PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV MPLCONFIGDIR=/tmp/matplotlib

ENV OMP_NUM_THREADS=1
ENV OPENBLAS_NUM_THREADS=1
ENV MKL_NUM_THREADS=1
ENV NUMEXPR_NUM_THREADS=1

# 国内网络下 Debian 官方源偶发 502。构建参数仍允许切回其他镜像源。
RUN sed -i -E \
        "s|https?://deb.debian.org/debian-security|${DEBIAN_MIRROR}/debian-security|g; \
         s|https?://deb.debian.org/debian|${DEBIAN_MIRROR}/debian|g" \
        /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
        'Acquire::Retries "5";' \
        'Acquire::http::Timeout "60";' \
        'Acquire::https::Timeout "60";' \
        > /etc/apt/apt.conf.d/80-network-retries

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        fontconfig \
        fonts-noto-cjk \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz-subset0 \
        shared-mime-info \
        libgomp1 \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# 沙箱镜像使用 infra/sandbox 作为独立构建上下文。
COPY requirements.txt /tmp/sandbox-requirements.txt

RUN python -m pip install --index-url "${PYPI_INDEX_URL}" --upgrade pip \
    && python -m pip install --index-url "${PYPI_INDEX_URL}" --only-binary=:all: \
        -r /tmp/sandbox-requirements.txt \
    && python -m pip check \
    && weasyprint --info \
    && rm /tmp/sandbox-requirements.txt

RUN mkdir -p \
    /workspace/input \
    /workspace/output \
    /workspace/scripts \
    /workspace/profile

WORKDIR /workspace
