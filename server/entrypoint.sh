#!/bin/sh
# 让容器支持 PUID/PGID —— fnOS 用户对这套约定最熟悉，社区 compose 文件也都这么写。
#
# 以 root 启动时：建好对应用户 -> 修正 /data 和 /cache 的属主 -> 降权执行真正的命令。
# 以非 root 启动时（compose 里写了 user:）：原样执行，不改任何东西。
set -eu

if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

PUID="${PUID:-1000}"
PGID="${PGID:-100}"

if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -g "$PGID" picframe
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin picframe
fi

# 只改顶层目录本身，不递归 —— 缓存目录里可能有几十万个缩略图，-R 会拖慢启动。
# 子目录由应用自己以 PUID 身份创建，属主天然是对的。
for dir in /data /cache; do
    if [ -d "$dir" ]; then
        owner="$(stat -c '%u:%g' "$dir" 2>/dev/null || echo '')"
        if [ "$owner" != "$PUID:$PGID" ]; then
            chown "$PUID:$PGID" "$dir" 2>/dev/null || \
                echo "警告: 无法修改 $dir 的属主，若写入失败请在宿主机执行 chown -R $PUID:$PGID <对应目录>" >&2
        fi
    else
        mkdir -p "$dir"
        chown "$PUID:$PGID" "$dir" 2>/dev/null || true
    fi
done

if [ -n "${UMASK:-}" ]; then
    umask "$UMASK"
fi

exec gosu "$PUID:$PGID" "$@"
