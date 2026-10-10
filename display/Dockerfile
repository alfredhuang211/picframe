# 显示端：在容器里直接驱动 HDMI，全屏跑 Chromium 幻灯片。
#
# 为什么用 Debian trixie 而不是 bookworm —— cage 的版本差异在这里是决定性的：
#   bookworm = cage 0.1.4   任何输出被移除都会直接退出
#   trixie   = cage 0.2.0   只有「嵌套输出」（X11/Wayland 后端）被移除才退出，
#                           HDMI 拔出时它不会死，配合看门狗就能自动恢复
# 顺带 trixie 自带 chromium 150 和 Mesa 25，对 Ozone/Wayland 支持更好。
#
# 构建：
#   docker build -t fnos-picframe-display:latest ./display

FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

# 砍掉 man 和 doc，省约 30MB，这里没有东西需要它们
RUN printf 'path-exclude=/usr/share/doc/*\npath-exclude=/usr/share/man/*\n' \
      > /etc/dpkg/dpkg.cfg.d/01-nodoc

# 关于 --no-install-recommends 的一个坑：
#   chromium 只是「Recommends」libgl1-mesa-dri，但 wlroots 的 GLES2 渲染器和
#   Chromium 的 GBM/EGL 路径都要 dlopen 里面的 dri/*_dri.so。
#   所以必须显式装上，否则表现是「compositor 活着但屏幕全黑」。
#   （用 dpkg -L libgl1-mesa-dri 只看到 drirc 是正常的：Debian 在 debian/rules 里
#    额外安装了 dri 驱动，不走 .install 清单。）
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        cage \
        chromium \
        libseat1 \
        seatd \
        libdrm2 \
        libgl1-mesa-dri \
        libegl-mesa0 \
        libegl1 \
        libgles2 \
        libgbm1 \
        libinput10 \
        libudev1 \
        libxkbcommon0 \
        libwayland-client0 \
        xkb-data \
        ca-certificates \
        tini \
        curl \
        tzdata \
        procps \
        alsa-utils \
        pulseaudio \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        fonts-dejavu-core \
        libdrm-tests \
        wayland-utils \
        grim; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# /run/user/0 是 XDG_RUNTIME_DIR：libwayland 没有它拒绝创建 socket，
# cage 会直接以 "XDG_RUNTIME_DIR is not set in the environment" 退出。权限必须 0700。
RUN set -eux; \
    install -d -m 0700 /run/user/0; \
    install -d -m 0700 /tmp/chromium-profile

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

# 默认值，全部可以被 compose 覆盖。
#
# 这里**故意不设** WLR_RENDERER：wlroots 只在 WLR_RENDERER 未设置时才走「自动」路径
# （先试 GLES2，失败再退回 pixman 软渲染）。一旦显式设成 gles2，GLES2 创建失败就变成
# 致命错误、直接退出，老核显上会表现为反复重启而不是自动降级。
# 真需要强制时，在 compose 的 environment 里设 WLR_RENDERER=pixman。
ENV XDG_RUNTIME_DIR=/run/user/0 \
    LIBSEAT_BACKEND=builtin \
    SEATD_VTBOUND=0 \
    WLR_NO_HARDWARE_CURSORS=1 \
    XCURSOR_SIZE=1 \
    PICFRAME_URL=http://picframe:8080 \
    HOTPLUG_WATCHDOG=1 \
    RESTART_DELAY=5 \
    ENABLE_AUDIO=0

# 刻意不写 USER，也刻意不建普通用户：
#   · 用 LIBSEAT_BACKEND=builtin 时，libseat 需要以 root 直接打开设备节点；
#   · Chromium 的沙箱在 Docker 默认 seccomp 下本来就起不来（缺 CLONE_NEWPID/NEWNET），
#     所以「降权」并不能换来真正的沙箱，反而多出一堆 GID 对不上的问题。
# 代价是 Chromium 必须带 --no-sandbox —— 它只加载本机相册页面。见 README「安全说明」。

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
