#!/bin/sh
# Kiosk 入口：cage（wlroots kiosk 合成器）+ Chromium，直接接管 HDMI。
#
# 关键设计（都是踩过坑才这么写的，理由写在注释里，改之前先读）：
#
#   1. 用 LIBSEAT_BACKEND=builtin，不跑 seatd 守护进程。
#      Debian 编译 libseat 时开了 -Dlibseat-builtin=enabled，内置 seatd 服务端可直接用，
#      于是不需要 socket、不需要 logind。再配 SEATD_VTBOUND=0 让 seat 不绑定 VT，
#      连 /dev/tty0 都不需要。想换回独立守护进程就设 LIBSEAT_BACKEND=seatd。
#
#   2. 无输入设备时必须设 WLR_LIBINPUT_NO_DEVICES=1。
#      否则 wlroots 会以 "libinput initialization failed, no input devices"
#      直接放弃创建 backend —— cage 连启动都启动不了。
#
#   3. 看门狗轮询 /sys/class/drm/*/status，而不是等内核 uevent。
#      容器（尤其是 userns 重映射的）收不到 uevent；而 cage 0.2.0 在 HDMI 拔出时
#      *不会*退出，只会静静停在没有输出的状态。所以必须自己发现「插回来了」并重启。
#
#   4. Debian 打包的 cage 是 release 构建（#ifdef DEBUG 关掉了），日志只输出 ERROR。
#      也就是说：cage 启动后**没有任何输出就是成功**，别把安静当成失败。
set -u

log() { printf '%s [kiosk] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

# ------------------------------------------------------------------ 配置
PICFRAME_URL="${PICFRAME_URL:-http://picframe:8080}"
DISPLAY_URL="${DISPLAY_URL:-$PICFRAME_URL/display}"
PROFILE_DIR="${CHROMIUM_USER_DATA_DIR:-/tmp/chromium-profile}"
RESTART_DELAY="${RESTART_DELAY:-5}"
HOTPLUG_WATCHDOG="${HOTPLUG_WATCHDOG:-1}"
HOTPLUG_POLL="${HOTPLUG_POLL:-3}"
DRM_WAIT_TRIES="${DRM_WAIT_TRIES:-60}"
WAIT_SERVER_TRIES="${WAIT_SERVER_TRIES:-150}"   # ×2 秒；服务端首扫改成后台任务后会立刻可用，这里只是给慢启动留余量
CAGE_OPTS="${CAGE_OPTS:--m last -d}"
EXTRA_FLAGS="${CHROMIUM_EXTRA_FLAGS:-}"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/0}"
install -d -m 0700 "$XDG_RUNTIME_DIR" 2>/dev/null || true
install -d -m 0700 "$PROFILE_DIR" 2>/dev/null || true

export HOME="${HOME:-/tmp/chromium-home}"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_CACHE_HOME="$HOME/.cache"
install -d -m 0700 "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" 2>/dev/null || true

# compose 会把「未设置」的变量传成**空字符串**，而 wlroots 判断的是 getenv() 是否为 NULL：
#   WLR_RENDERER=""           -> 它以为你指定了渲染器，于是关掉自动 pixman 兜底，GLES2 起不来就致命
#   WLR_DRM_NO_MODIFIERS=""   -> 被当成「已启用」，强行关掉 DRM modifier
# 所以空值必须清掉，让 wlroots 回到自动探测。
[ -z "${WLR_RENDERER:-}" ] && unset WLR_RENDERER
[ -z "${WLR_DRM_NO_MODIFIERS:-}" ] && unset WLR_DRM_NO_MODIFIERS
[ -z "${WLR_DRM_NO_ATOMIC:-}" ] && unset WLR_DRM_NO_ATOMIC
[ -z "${WLR_DRM_DEVICES:-}" ] && unset WLR_DRM_DEVICES
[ -z "${WLR_RENDER_DRM_DEVICE:-}" ] && unset WLR_RENDER_DRM_DEVICE
[ -z "${WLR_NO_HARDWARE_CURSORS:-}" ] && unset WLR_NO_HARDWARE_CURSORS
[ -z "${WLR_LIBINPUT_NO_DEVICES:-}" ] && unset WLR_LIBINPUT_NO_DEVICES

# ------------------------------------------------------------------ 诊断
conn_state() {
    # 注意不要用 ${f##*/} —— 那是 "status"，所有条目的键名都一样，没法定位具体接口。
    # 取上一级目录名（card0-HDMI-A-1）才有意义。
    for f in /sys/class/drm/card*-*/status; do
        [ -r "$f" ] || continue
        _conn="${f%/status}"
        printf '%s=%s;' "${_conn##*/}" "$(cat "$f" 2>/dev/null || echo '?')"
    done
}

primary_node() {
    for c in /dev/dri/card[0-9]*; do
        [ -c "$c" ] && { printf '%s' "$c"; return 0; }
    done
    return 1
}

# ------------------------------------------------------------------ seat
export LIBSEAT_BACKEND="${LIBSEAT_BACKEND:-builtin}"
SEATD_PID=""
if [ "$LIBSEAT_BACKEND" = "seatd" ]; then
    log "使用独立 seatd 守护进程（LIBSEAT_BACKEND=seatd）"
    export SEATD_SOCK="${SEATD_SOCK:-$XDG_RUNTIME_DIR/seatd.sock}"
    seatd -s "$SEATD_SOCK" &
    SEATD_PID=$!
    _i=0
    while [ ! -S "$SEATD_SOCK" ]; do
        _i=$((_i + 1))
        [ "$_i" -gt 50 ] && { log "错误: seatd socket 未出现"; break; }
        sleep 0.1
    done
else
    # 非 VT 绑定：seatd 不会去开 /dev/tty0，容器里也没有 tty0
    export SEATD_VTBOUND="${SEATD_VTBOUND:-0}"
fi

# ------------------------------------------------------------------ 输入设备
_have_input=0
for _e in /dev/input/event*; do [ -e "$_e" ] && { _have_input=1; break; }; done
if [ "$_have_input" = "0" ]; then
    export WLR_LIBINPUT_NO_DEVICES="${WLR_LIBINPUT_NO_DEVICES:-1}"
    log "未发现输入设备，已设置 WLR_LIBINPUT_NO_DEVICES=1（否则 wlroots 拒绝启动）"
fi

log "地址=$DISPLAY_URL uid=$(id -u)"
log "libseat=$LIBSEAT_BACKEND seatd_vtbound=${SEATD_VTBOUND:-n/a} renderer=${WLR_RENDERER:-auto}"

# ------------------------------------------------------------------ 可选音频
# 默认关闭。fnOS 的 HDMI 音频本身是社区公认的坑，先在宿主机确认好通道再打开。
if [ "${ENABLE_AUDIO:-0}" = "1" ]; then
    log "启动 PulseAudio（实验性），ALSA 设备=${AUDIO_DEVICE:-default}"
    pulseaudio --daemonize=no --exit-idle-time=-1 --log-target=stderr -n \
        --load="module-native-protocol-unix" \
        --load="module-always-sink" \
        --load="module-alsa-sink device=${AUDIO_DEVICE:-default} sink_name=hdmi" &
    sleep 2
    export PULSE_SERVER="unix:$XDG_RUNTIME_DIR/pulse/native"
fi

# ------------------------------------------------------------------ 等 DRM 节点
# docker 可能比宿主机探测显卡更早启动，所以要等。
_i=0
while ! primary_node >/dev/null; do
    _i=$((_i + 1))
    if [ "$_i" -ge "$DRM_WAIT_TRIES" ]; then
        log "致命: ${DRM_WAIT_TRIES} 秒内没有出现 /dev/dri/card*"
        log "  只有 renderD* 是不够的 —— KMS 输出必须用主节点。"
        log "  宿主机排查: ls -l /dev/dri ; lsmod | grep -E 'i915|amdgpu|nouveau|radeon'"
        log "  compose 里应当是: devices: ['/dev/dri:/dev/dri']（不能只给 renderD128）"
        exit 1
    fi
    sleep 1
done
log "DRM 主节点: $(primary_node)"
log "接口状态: $(conn_state)"

# ------------------------------------------------------------------ 等相册服务
# 必须等到服务可用再启动浏览器。Chromium 拿到 ERR_CONNECTION_REFUSED 时会渲染出
# 一个**内置错误页**，而重试逻辑在页面自己的 JS 里 —— 错误页上根本没有那段 JS，
# 它不会自己重试。结果是电视永久卡在错误页，只能手动重启容器。
wait_for_server() {
    log "等待相册服务: $PICFRAME_URL"
    _i=0
    while ! curl -fsS --max-time 3 "$PICFRAME_URL/api/health" >/dev/null 2>&1; do
        [ "${SHUTDOWN:-0}" = "1" ] && return 1
        _i=$((_i + 1))
        if [ "$_i" -ge "$WAIT_SERVER_TRIES" ]; then
            return 1
        fi
        sleep 2
    done
    return 0
}

# 可被信号打断的 sleep。POSIX 下 sleep 是前台命令，trap 要等它跑完才执行，
# 于是 docker stop 会白等一个 RESTART_DELAY。后台 sleep + wait 能立刻响应。
interruptible_sleep() {
    sleep "$1" &
    SLEEP_PID=$!
    wait "$SLEEP_PID" 2>/dev/null
    SLEEP_PID=""
}

# ------------------------------------------------------------------ Chromium 参数
# 只列在这个栈上真正起作用的开关。已经失效的（--disable-infobars、
# --disable-session-crashed-bubble、--enable-features=UseOzonePlatform）不再写。
chromium_flags() {
    cat <<EOF
--ozone-platform=wayland
--kiosk
--no-sandbox
--disable-gpu-sandbox
--disable-dev-shm-usage
--user-data-dir=$PROFILE_DIR
--no-first-run
--no-default-browser-check
--disable-search-engine-choice-screen
--test-type
--noerrdialogs
--hide-crash-restore-bubble
--disable-component-update
--check-for-update-interval=31536000
--password-store=basic
--autoplay-policy=no-user-gesture-required
--hide-scrollbars
--disable-pinch
--force-device-scale-factor=1
--disable-background-networking
--disable-features=Translate,MediaRouter,OptimizationHints,InterestFeedContentSuggestions
--lang=zh-CN
EOF
}

# 核显 GL 有问题（画面全黑但 compositor 活着）时的兜底
GPU_FLAGS=""
case "${DISABLE_GPU:-0}" in
    1|yes|true) GPU_FLAGS="--disable-gpu --enable-unsafe-swiftshader" ;;
esac

# ------------------------------------------------------------------ 看门狗
PIDFILE="$XDG_RUNTIME_DIR/kiosk.pid"
WATCHDOG_PID=""

start_watchdog() {
    [ "$HOTPLUG_WATCHDOG" = "1" ] || return 0
    (
        prev="$(conn_state)"
        while :; do
            sleep "$HOTPLUG_POLL"
            cur="$(conn_state)"
            if [ "$prev" != "$cur" ]; then
                case "$prev" in
                    *disconnected*)
                        case "$cur" in
                            *connected*)
                                log "看门狗: HDMI 插回来了，重启 kiosk"
                                _target="$(cat "$PIDFILE" 2>/dev/null || true)"
                                # 绝不能写 `|| echo 0`：kill 0 会把信号发给**整个进程组**，
                                # 等于把容器自己干掉
                                [ -n "$_target" ] && kill -TERM "$_target" 2>/dev/null
                                ;;
                        esac
                        ;;
                esac
            fi
            prev="$cur"
        done
    ) &
    WATCHDOG_PID=$!
}

stop_watchdog() {
    [ -n "$WATCHDOG_PID" ] && kill -TERM "$WATCHDOG_PID" 2>/dev/null
    WATCHDOG_PID=""
}

# ------------------------------------------------------------------ 主循环
SHUTDOWN=0
STACK_PID=""
SLEEP_PID=""

on_signal() {
    SHUTDOWN=1
    log "收到停止信号"
    [ -n "$STACK_PID" ] && kill -TERM "$STACK_PID" 2>/dev/null
    [ -n "$SEATD_PID" ] && kill -TERM "$SEATD_PID" 2>/dev/null
    [ -n "$SLEEP_PID" ] && kill -TERM "$SLEEP_PID" 2>/dev/null
}
trap on_signal TERM INT

FAILS=0

while :; do
    if ! wait_for_server; then
        [ "$SHUTDOWN" = "1" ] && break
        log "相册服务仍不可用，${RESTART_DELAY} 秒后重试（先不启动浏览器，避免卡在错误页）"
        interruptible_sleep "$RESTART_DELAY"
        continue
    fi

    start_watchdog

    _flags="$(chromium_flags | tr '\n' ' ')"
    log "启动 cage -> Chromium"
    # shellcheck disable=SC2086
    cage $CAGE_OPTS -- chromium $_flags $GPU_FLAGS $EXTRA_FLAGS "$DISPLAY_URL" &
    STACK_PID=$!
    printf '%s' "$STACK_PID" > "$PIDFILE"

    _started=$(date +%s)
    wait "$STACK_PID"
    _rc=$?
    _ran=$(( $(date +%s) - _started ))
    STACK_PID=""

    stop_watchdog
    rm -f "$PIDFILE"

    [ "$SHUTDOWN" = "1" ] && break

    # 秒退基本可以断定是配置/环境问题，不是偶发崩溃。指数退避 ——
    # 否则一个起不来的配置会以 5 秒一次、每天几万行地把日志刷满，
    # 而容器状态始终是 Up，Docker 的 restart 策略永远不会介入，
    # 用户和监控都看不出「它其实一直在失败」。
    if [ "$_ran" -lt 10 ]; then
        FAILS=$((FAILS + 1))
    else
        FAILS=0
    fi

    _delay=$RESTART_DELAY
    if [ "$FAILS" -gt 2 ]; then
        _delay=$(( RESTART_DELAY * (FAILS - 1) ))
        [ "$_delay" -gt 120 ] && _delay=120
        log "kiosk 连续 $_FAILS 次在 10 秒内退出（码 $_rc）—— 很可能是配置或显卡问题，退避到 ${_delay} 秒"
        log "  排查: docker exec picframe-display modetest -c 2>&1 | head -30"
        log "  兜底: 在 compose 的 environment 里设 WLR_RENDERER=pixman，或设 DISABLE_GPU=1"
    else
        log "kiosk 退出（码 $_rc，运行 ${_ran}s），${_delay} 秒后重启"
    fi
    interruptible_sleep "$_delay"
done

log "已停止"
exit 0
