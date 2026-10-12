# 相册服务：Web 后端 + 管理界面 + 幻灯片页面
#
# 构建：
#   docker build -t ghcr.io/alfredhuang211/picframe:latest ./server
# 想要更小的镜像（代价是视频只能显示占位图）：
#   docker build -t ghcr.io/alfredhuang211/picframe:latest --build-arg INSTALL_FFMPEG=false ./server

FROM python:3.12-slim-bookworm

# INSTALL_FFMPEG=false 可以省掉约 80MB
ARG INSTALL_FFMPEG=true

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        gosu \
        tzdata; \
    if [ "$INSTALL_FFMPEG" = "true" ]; then \
        apt-get install -y --no-install-recommends ffmpeg; \
    fi; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && mkdir -p /data /cache

# 注意：这里**不**写 USER。entrypoint 会先以 root 修好 /data 与 /cache 的属主，
# 再用 gosu 降权到 PUID:PGID 执行 uvicorn —— 这也是拷到 NAS 上权限最不容易出错的写法。
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
