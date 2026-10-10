# PicFrame · 把 NAS 的 HDMI 变成电子相册

**飞牛 fnOS / 群晖 / 任意 Debian 都能跑** —— 装两个容器，把 NAS 的 HDMI 接口变成一块全屏电子相册：**手机或电脑上打开网页，勾选要播的文件夹和照片，电视上立刻生效。**

```
                 你的手机 / 电脑                       HDMI 显示器 / 电视
                        │                                      ▲
                        │ http://<NAS>:18080/                  │ 全屏幻灯片
                        ▼                                      │
        ┌───────────────────────────────┐          ┌───────────┴───────────────┐
        │  picframe（相册服务）          │          │  picframe-display         │
        │                               │  HTTP    │                           │
        │  · 扫描只读挂载的照片目录      │◄─────────│  cage (Wayland kiosk)     │
        │  · 缩略图 / 预览图缓存         │          │  + Chromium 全屏          │
        │  · 管理界面（选文件夹/照片）   │  SSE     │                           │
        │  · 幻灯片页面 /display         │─────────►│  设置一改，画面立刻更新   │
        └───────────────┬───────────────┘          └───────────┬───────────────┘
                        │ 只读挂载                              │ /dev/dri 直通
                        ▼                                      ▼
              /vol1/1000/Photos 等                        HDMI 输出
```

---

## 一、为什么是这样设计的

动手之前先确认了几个事实，它们直接决定了方案：

| 事实 | 影响 |
|---|---|
| fnOS 是 **headless** 的，HDMI 插上显示器只有黑底白字的 TTY，官方没有桌面、也没有 TV 版相册 | 必须自己拉起一个图形层 |
| 官方**没有** HDMI 直出能力，`飞牛TV` 是连局域网的 Android 客户端，不是 HDMI 驱动 | 不能指望官方功能 |
| 把核显 **VFIO 直通给虚拟机**会屏蔽宿主机驱动，导致飞牛相册 AI 识别和影视硬解**彻底失效** | 这条路不能走 |
| 装 GNOME/KDE 完整桌面要多占几百 MB 内存，对 24 小时跑的 NAS 是负担 | 用极轻量的 kiosk 合成器 |
| fnOS 的相册接口**不是公开接口**，官方一改就可能失效 | 直接读硬盘文件夹，不碰私有 API |

于是分成两半：**能干的活（选片、扫描、转码）放在服务端；HDMI 那一侧只负责显示一个网页。** 这样显示端逻辑极薄、几乎不会坏，而所有功能都在你随时能改的 Web 界面里。

几个具体取舍：

- **容器内跑 `cage`（wlroots 系 kiosk 合成器）而不是 Xorg** —— 宿主机什么都不用装，`docker compose down` 就是干净卸载。
- **照片目录只读挂载** —— 相册服务物理上不可能改你的照片。
- **服务端转码** —— iPhone 的 `.heic` 浏览器放不了，Pillow 转成 JPEG 再送出去；30MB 原图也统一缩到屏幕够用的尺寸，低功耗机器才带得动。
- **不做鉴权** —— 它只监听局域网，且内容就是自家照片。真要暴露到公网请套反向代理加认证（见「安全说明」）。

---

## 二、部署

### 0. 前置条件

- x86_64 的 fnOS，Docker 应用**已经初始化过一次**（打开「Docker」应用完成引导，否则其他应用装不上）
- 开启 SSH：**【系统设置】→【SSH】打开服务**，**并且** 【系统设置】→【用户管理】→ 你的账号 → **打开 SSH 登录权限**。v1.1.19 之后这两个开关默认都是关的，**每次系统升级后都要复查**。
- 确认自己的 uid/gid：

  ```bash
  ssh 你的用户名@NAS的IP
  id
  # 典型输出 uid=1000(xxx) gid=1000(xxx) —— 记下这两个数字，下面要用
  ```

### 1. 把项目拷到 NAS 上

在 NAS 上建目录（路径按你的习惯改）：

```bash
sudo -i
mkdir -p /vol1/1000/docker/picframe
exit
```

然后把本项目的 `docker-compose.yml`、`.env.example`、`server/`、`display/` 传到 `/vol1/1000/docker/picframe/`（用 fnOS 文件管理上传压缩包再解压，或者 `scp`）。

> 用「文件管理 → 右键文件夹 → 详细信息 → **复制原始路径**」拿到的路径最准，**原样使用**，不要自己把 `/vol02/...` 改写成 `/vol1/...`。

### 2. 改配置

```bash
cd /vol1/1000/docker/picframe
cp .env.example .env
vi .env
```

至少要改这几个：

```ini
# 建议先挂上层目录，这样在管理界面里能自由进入任意子文件夹挑选
PHOTO_DIR_1=/vol1/1000

PICFRAME_DATA=/vol1/1000/docker/picframe

# 避开 fnOS 自己占用的 22/80/443/5666/5667/8000-8006
PICFRAME_PORT=18080

PUID=1000     # 上面 `id` 查到的，fnOS 首个用户通常是 1000
PGID=100      # 上面 `id` 查到的**组** id；fnOS 通常是 100，别想当然写成 1000
```

建好数据目录并交给你自己的账号：

```bash
sudo -i
mkdir -p /vol1/1000/docker/picframe/{config,cache}
chown -R 1000:1000 /vol1/1000/docker/picframe
```

### 3. 确认端口没被占

```bash
ss -tlnp | grep 18080    # 应该没有任何输出
```

### 4. 构建并启动

```bash
cd /vol1/1000/docker/picframe
sudo docker compose up -d --build
sudo docker compose ps
```

> **如果你的电脑是 ARM 的 Mac**（在本地构建再传到 NAS），必须指定目标架构：
> `docker buildx build --platform linux/amd64 -t fnos-picframe:latest ./server --load`
> 在 x86 的 NAS 上直接 `docker compose up -d --build` 则不需要。

**也可以走 fnOS 的图形界面**：Docker → **Compose** → 新增项目 → 粘贴 `docker-compose.yml` 内容 → 勾选「创建项目后立即启动」。

> ⚠️ **走 GUI 粘贴 YAML 有两个坑**：
>
> 1. `build:` 需要构建上下文存在于 NAS 上；想省事可以先在别处构建好镜像、`docker save` 传过去 `docker load`，再让 compose 直接用镜像。
> 2. YAML 里的 `${PHOTO_DIR_1:?请先复制 .env.example 为 .env 并设置 PHOTO_DIR_1}` 是**必填变量**。GUI 生成的项目目录不会读你手写的 `.env`，`docker compose` 会在变量插值阶段直接报错退出。走 GUI 时请把那一行改成你的真实路径（例如 `- /vol1/1000:/photos/main:ro`），或者干脆用上面的命令行方式。

### 5. 验证

```bash
# 两个容器都应该是 Up
sudo docker compose ps

# 重启策略必须是 always（fnOS 的自启开关有已知的不一致问题，只能靠这个兜底）
sudo docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' picframe picframe-display

# 权限确认
sudo docker exec picframe-display ls -l /dev/dri                        # 应看到 card0 / renderD128
sudo docker exec picframe ls /photos/main | head                       # 应看到你的照片目录

# 内核真的能把画面点起来吗（把 -M i915 换成 amdgpu / nouveau / radeon）
sudo docker exec picframe-display modetest -c -M i915 2>&1 | head -30

# 服务本身
curl -s http://127.0.0.1:18080/api/health

# 显示端日志
sudo docker compose logs --tail=60 picframe-display
```

浏览器打开 `http://<NAS的IP>:18080/`，能进管理界面就成功了一半；插着 HDMI 的屏幕应该已经在放幻灯片了。

> **重要：cage 启动后没有任何输出就是成功。** Debian 打包的 cage 是 release 构建，
> 日志级别固定为 ERROR，所以「Seat opened with backend 'builtin'」这类信息**永远不会出现**。
> 别把安静当成失败——真正的成功证据是屏幕亮起来，或者用下面的 `grim` 截图。

### 5.1 用真实浏览器验证前端（可选）

服务端逻辑有 51 个自动化测试覆盖；前端则可以对着**已经跑起来的**相册跑一遍冒烟测试：

```bash
pip install websocket-client
python3 scripts/ui-smoke.py http://127.0.0.1:18080
```

它用真实的无头 Chrome（本机需要装 Chrome/Chromium）打开两个页面并逐项断言：

- **显示端**：照片确实加载并解码、交叉淡入淡出图层、Ken Burns 生效、时钟与说明文字已渲染、播放设置已应用、无 JS 报错
- **管理端**：目录树渲染、勾选文件夹后保存按钮变为可用、实时预估数量、**点保存后服务端播放列表真的变了**、设置面板可切换、无 JS 报错

脚本会在测试前后快照并还原播放配置，所以可以放心对着正在使用的相册跑。

### 6. 开机自启

compose 里已经写了 `restart: always`，这是 fnOS 上唯一可靠的方式（社区反馈 compose 创建容器的「开机自启」开关会出现「有的自启有的不自启」）。部署后建议：

```bash
# 顺手把界面上的开关也确认一遍：Docker → 容器 → 基本设置 → 开机自启
sudo reboot
# 重启后
sudo docker compose ps
```

---

## 三、怎么用

### 管理界面 `http://<NAS的IP>:18080/`

| 标签页 | 干什么 |
|---|---|
| **选择文件夹** | 勾选整个文件夹（含子目录）。右边会列出已选项，点 ✕ 移除。 |
| **挑选照片** | 缩略图网格，点一下切换「选中 / 不选」。想从一个大文件夹里剔掉几张，就点成红色「已排除」。 |
| **播放设置** | 停留时长、转场、顺序、是否显示时钟、夜间息屏、画质上限等。 |

改完点 **保存并应用**（或 `Ctrl+S`）。显示端会通过 SSE **立刻**收到通知并重新加载，**不需要重启任何东西**。

顶栏的 `⏮ ⏯ ⏭` 是遥控：在手机上点，电视上就跟着切。

底部状态栏会实时显示「按当前选择将播放 N 项」。

### 显示端快捷键

屏幕上接个无线键盘/遥控器就能用：

| 按键 | 作用 |
|---|---|
| `→` / `PageDown` / 点击右半屏 | 下一张 |
| `←` / `PageUp` / 点击左半屏 | 上一张 |
| `空格` | 暂停 / 继续 |
| `R` | 重新拉取播放列表 |
| `F` | 切换全屏 |

---

## 四、排查

### HDMI 是黑屏

按顺序查，每一步都能定位到不同的层次：

```bash
# 1. 宿主机上显卡驱动正常吗？必须有 card0，只有 renderD128 是不够的
ls -l /dev/dri/
lsmod | grep -E '^(i915|amdgpu|nouveau|radeon|xe)\b'
for f in /sys/class/drm/card*-*/status; do echo "$f: $(cat $f)"; done   # HDMI 口应显示 connected

# 2. 有没有别的进程已经占着显卡？（宿主机上的桌面/plymouth/X）
sudo fuser -v /dev/dri/card0 || echo "没有占用者 —— 正常"

# 3. 容器里能看到设备吗
sudo docker exec picframe-display ls -l /dev/dri

# 4. 内核能不能点亮它
sudo docker exec picframe-display modetest -c -M i915 2>&1 | head -40

# 5. 容器日志
sudo docker compose logs --tail=100 picframe-display
```

| 现象 / 日志关键字 | 原因 | 处理 |
|---|---|---|
| 宿主机上**只有** `renderD128`，没有 `card*` | 没有 modesetting 主节点，任何容器都点不亮 | 检查内核驱动和 BIOS 里的核显设置 |
| `fuser` 显示 card0 **被别人占用** | DRM master 是独占的，`EBUSY` 状态下**加 `privileged` 也没用** | 在宿主机停掉占用者（桌面环境、X、plymouth） |
| `Permission denied` 打开 `/dev/dri/*` | 设备没直通 | 确认 `devices: - /dev/dri:/dev/dri`；实在不行再取消 `privileged: true` 的注释 |
| `XDG_RUNTIME_DIR is not set` / `is invalid` | 运行时目录缺失或权限不是 0700 | 镜像里已建好；若被覆盖请检查 `XDG_RUNTIME_DIR` |
| `Could not open terminal for VT 0` | seat 绑定了 VT，而容器里没有 `/dev/tty0` | 确认 `SEATD_VTBOUND=0` |
| `libinput initialization failed, no input devices` | 没有输入设备且没告诉 wlroots | entrypoint 会自动设 `WLR_LIBINPUT_NO_DEVICES=1`；若日志里没这行说明 `/dev/input` 已映射 |
| `failed to connect to seat` | seat 后端不对 | 默认 `LIBSEAT_BACKEND=builtin`；可改成 `seatd` 让 entrypoint 拉起守护进程 |
| compositor 活着但**屏幕全黑** | 缺 Mesa 的 DRI 驱动，或核显比镜像里的 Mesa 新 | 在 `.env` 里依次试 `WLR_RENDERER=pixman`、`DISABLE_GPU=1`、`WLR_DRM_NO_MODIFIERS=1`，每改一次 `docker compose up -d` |
| cage 反复重启 | 某个启动参数不对 | 日志会打印连续失败次数；秒退超过 2 次后自动退避（最长 120 秒），并提示该试哪几个开关 |
| 画面出来但**是命令行** | 容器没拿到 DRM master | 回到第 2 步查占用者 |
| HDMI 拔了再插没反应 | 容器收不到内核 uevent，cage 0.2.0 又不会自己退出 | entrypoint 里有 sysfs 看门狗，默认开启；确认 `HOTPLUG_WATCHDOG=1` |

**确认画面真的渲染出来了**（比盯着电视看更可靠）：

```bash
# 注意取**最新**的那个 socket：容器被 SIGKILL 时 /run/user/0 会残留 wayland-0，
# 而 cage 下次会改用 wayland-1。按字典序取第一个会连到死 socket 上，grim 直接失败，
# 看起来就像「画面没渲染出来」。
sudo docker exec picframe-display sh -c \
  'SOCK=$(ls -t /run/user/0/wayland-* 2>/dev/null | head -1); \
   WAYLAND_DISPLAY=$(basename "$SOCK") grim -o HDMI-A-1 /tmp/frame.png'
sudo docker cp picframe-display:/tmp/frame.png ./frame.png
file ./frame.png        # 分辨率应该等于电视的分辨率
```

输出名不一定是 `HDMI-A-1`，可能是 `HDMI-A-2` / `DP-1`，用 `wayland-info` 或 `/sys/class/drm` 里看到的实际名字。

**兜底方案**：如果 `cage` 在你的核显上就是跑不起来，把 `picframe-display` 停掉，改用宿主机上的 Xorg 方案（社区验证过的老路子）：

```bash
sudo -i
apt update && apt install -y xorg chromium openbox fonts-wqy-zenhei
cat > /usr/local/bin/start-picframe.sh <<'EOF'
#!/bin/bash
openbox-session &
exec chromium --no-sandbox --kiosk --no-first-run \
  http://127.0.0.1:18080/display
EOF
chmod +x /usr/local/bin/start-picframe.sh
# 再写一个 systemd 服务用 xinit 拉起它即可
```

注意这条路会在宿主机装包，`apt` 升级 fnOS 时可能被覆盖。

### 相册里没有照片 / 少了照片

```bash
# 容器里能看到什么
sudo docker exec picframe ls -la /photos/main | head -20
```

- 管理界面 → 播放设置 → **重新扫描媒体库**
- 只支持这些格式：图片 `jpg/jpeg/png/gif/webp/bmp/tif/tiff/heic/heif/avif`，视频 `mp4/m4v/webm/mov/mkv`
- 管理界面底部会提示 HEIC 是否可用。不可用说明 `pillow-heif` 没装上，iPhone 的 `.heic` 会被跳过
- 符号链接会被刻意忽略（防越界），照片请用真实文件

### 加载慢 / NAS 变卡

- 播放设置里把**投屏分辨率上限**从 2560 降到 1920
- 关掉 **Ken Burns**
- **预加载张数** 降到 1–2
- 打开**直接使用原图**会让大图直接走网络，低功耗机器别开

### 没有声音（视频）

fnOS 的 HDMI 音频本身就是社区公认的坑（常见 `snd_hda_intel: no codecs initialized`）。先在**宿主机**上确认：

```bash
aplay -l                                  # 找到 HDMI 对应的 card/device
speaker-test -D plughw:0,3 -c 2           # 逐个试，听到白噪音就对了
```

确认后在 `.env` 里设 `ENABLE_AUDIO=1`，并在 compose 里打开 `/dev/snd` 挂载和 `AUDIO_DEVICE: "hw:0,3"`。

> 容器内的音频链路（PulseAudio → ALSA）我**没有在真机上验证过**，默认关闭。相册默认静音播放，纯看图完全不受影响。

### 会不会影响飞牛相册 AI 和影视硬解

**不会**——前提是你**没有**做核显 VFIO 直通。

- VFIO 直通 = 在宿主机屏蔽 i915 驱动，把整个核显交给虚拟机 → 飞牛相册 AI 和影视硬解会失效。**本方案不碰这个。**
- 本方案只是让一个容器**打开** `/dev/dri` 做显示输出。VA-API 硬解走的是 `renderD128` 渲染节点，本身就是多客户端接口，两者可以共存。

一个需要知道的细节：**KMS 显示输出是独占的（同一时刻只有一个 DRM master）**。本方案里 cage 拿走了 master，所以宿主机自己的 TTY 就没画面了——这正是我们想要的效果，不影响转码。

### Docker 把系统盘撑爆

fnOS 上有个已知问题：Docker ≥ v29 的 containerd 镜像存储会把数据写到**系统分区的 `/var/lib/containerd`**，即使 `data-root` 指向了存储空间，可能导致系统空间占满。

```bash
docker info | grep -i 'docker root dir'
cat /etc/docker/daemon.json
df -h /var/lib/containerd /vol1 2>/dev/null
```

本项目的镜像合计大约 500MB–1GB（Chromium 占大头），如果系统分区紧张，先处理这个问题再部署。

---

## 五、安全说明

- **照片目录是只读挂载**，相册服务无法修改或删除任何照片。
- **所有来自 HTTP 的路径都当作不可信输入**：经 `realpath` 归一化后必须落在允许的根目录内，目录穿越、越界绝对路径、**符号链接逃逸**都会被拒绝并返回 400。这部分有 14 个专门的测试用例覆盖。
- 管理界面**没有登录**。它设计为只在局域网使用。若要暴露到公网，**必须**在反向代理上加认证（并配 HTTPS），不要直接把 18080 端口映射出去。
- **显示端不需要 `privileged: true`。** 只给出 `/dev/dri` 和 `/dev/input` 两个设备节点即可——内核里第一个打开 `/dev/dri/card0` 的进程直接就是 DRM master，没有能力检查。compose 里默认用的是最小权限写法（`devices:` + `device_cgroup_rules:` + `no-new-privileges`）。
- 显示端必须带 Chromium 的 `--no-sandbox`。这不是因为以 root 运行，而是 **Docker 默认的 seccomp 配置禁止 `clone(CLONE_NEWPID/NEWNET)`**，Chromium 的命名空间沙箱本来就起不来；改成非 root 也换不来真正的沙箱。它只加载本机 `http://picframe:8080/display` 这一个地址。
  - 想再收紧：在 compose 里给它加 `network_mode` 限制，或者参考 `display/entrypoint.sh` 的注释调整参数。

---

## 六、项目结构

```
.
├── docker-compose.yml          两个服务的编排
├── .env.example                配置模板（路径、端口、PUID/PGID）
├── server/                     相册服务
│   ├── Dockerfile
│   ├── entrypoint.sh           PUID/PGID 降权启动
│   ├── requirements.txt
│   ├── app/
│   │   ├── config.py           设置模型 + 原子写入的持久化
│   │   ├── library.py          路径安全、目录扫描、媒体索引、选片解析
│   │   ├── images.py           EXIF 方向、HEIC 解码、缩略图磁盘缓存
│   │   ├── main.py             HTTP 接口 + SSE
│   │   └── static/             admin.html/css/js · display.html/css/js
│   └── tests/test_picframe.py  51 个端到端测试
├── scripts/
│   └── ui-smoke.py             前端冒烟测试（真实浏览器 + CDP）
│   └── video-loop-check.py     视频死循环回归测试（真实浏览器 + CDP）
└── display/                    HDMI 显示端
    ├── Dockerfile              cage + Chromium（Debian trixie）
    └── entrypoint.sh           seat 初始化 + 崩溃自愈 + HDMI 热插拔看门狗
```

### 本地开发与测试

```bash
cd server
python -m venv ../.venv && ../.venv/bin/pip install -r requirements.txt pytest websocket-client
../.venv/bin/python -m pytest tests -q          # 65 项：后端逻辑 + 回归
```

测试覆盖：路径安全（穿越 / 越界 / 软链逃逸 / 前缀混淆）、EXIF 方向纠正、缩略图缓存命中与失效、递归与非递归选片、排除项优先级、失效条目容错、配置校验与损坏恢复、以及全部 HTTP 接口。

**前端要用真实浏览器测**，pytest 覆盖不到。这两个脚本都会自己起服务端和 Chrome：

```bash
# 冒烟测试：显示端和管理端的 28 项断言（渲染、可见性、保存、无 JS 报错）
.venv/bin/python scripts/ui-smoke.py http://127.0.0.1:18080

# 视频死循环回归：3 个浏览器播不了的视频 + 1 张图，断言 15 秒内请求数 < 60
.venv/bin/python scripts/video-loop-check.py
```

> 为什么不用 `chrome --screenshot`：显示页有持续运行的 CSS 动画和定时器，会让
> `--virtual-time-budget` 死锁。两个脚本都用 DevTools Protocol 驱动真实浏览器。
>
> 另外注意冒烟测试里断言的是**计算样式**而不是 `hidden` 属性 —— 作者样式里的
> `display:flex` 会盖掉 UA 的 `[hidden]{display:none}`，只查属性会漏掉
> 「整屏被提示层遮住但属性是 true」这类问题。

---

## 七、已知限制

- **管理界面没有鉴权**，仅限局域网。
- **视频缩略图**依赖 ffmpeg（默认镜像里已装；`--build-arg INSTALL_FFMPEG=false` 可以省 80MB）。
- **Live Photo** 只显示静态那一张。
- **大库会截断播放列表**：一次最多下发 3000 项（`PLAYLIST_MAX` 可调）。随机顺序 + 「定期重新抽取列表」会让整个库慢慢轮完，但如果你有十万张照片、又要求"严格每张都播到"，需要调大这个值。
- **HDMI 音频未经真机验证**，默认关闭。
- `cage` 直通方案在 Intel/AMD 核显上通用，但**具体到你的机器需要实际验证**；跑不起来就用上面的 Xorg 兜底方案。
- 显示端的 `device_cgroup_rules` 字段在个别 fnOS 版本上可能被界面过滤掉。这不影响首次启动（`devices:` 已经覆盖了创建时就存在的节点），只影响之后热插拔的输入设备；必要时改用命令行 `docker compose up -d`。

---

## 八、许可证

[MIT](LICENSE) © 2026 Alfred Huang
