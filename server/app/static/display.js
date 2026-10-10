/* 电子相册显示端逻辑。
 *
 * 设计要点：
 *  - 只用两层 <div>，交叉淡入淡出，避免切换时黑屏；
 *  - 下一张先 decode() 完再上屏，慢速机械盘 / 大图也不会闪；
 *  - 通过 SSE 接收管理端的「保存配置」「上一张 / 下一张」指令；
 *  - 夜间息屏在页面内用黑幕实现，不依赖 xset（容器里也没有 X）。
 */
'use strict';

const els = {
  stage: document.getElementById('stage'),
  layerA: document.getElementById('layerA'),
  layerB: document.getElementById('layerB'),
  clock: document.getElementById('clock'),
  caption: document.getElementById('caption'),
  banner: document.getElementById('banner'),
  notice: document.getElementById('notice'),
  noticeTitle: document.getElementById('notice-title'),
  noticeBody: document.getElementById('notice-body'),
  noticeUrl: document.getElementById('notice-url'),
};

const S = {
  settings: null,
  items: [],
  index: -1,
  activeSlot: 0,
  timer: null,
  paused: false,
  asleep: false,
  generation: 0,
  reloadTimer: null,
  shown: false,
  preloaded: new Set(),
  bannerTimer: null,
  emptyTimer: null,
  retryTimer: null,
  failed: new Set(), // 浏览器解不了的视频（按 url 记）
  stalled: false,    // 全部内容都播不了，已停下并给出提示
};

const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'];
const pad = (n) => String(n).padStart(2, '0');

// 播放列表请求的序号：SSE 事件、10 秒重试、定时重抽可能同时发出好几个
// loadPlaylist，慢的那个后到就会用旧 settings / 旧列表覆盖新的，必须让过期结果作废。
//
// 注意它必须声明在 boot() **调用之前**（见下面那行 boot()）：boot() 是同步跑到
// 第一个 await 的，如果这个 let 声明在文件靠后的位置，await loadPlaylist() 会读到
// 还在 TDZ 里的变量，抛 ReferenceError —— 而 async 函数会把同步异常变成
// promise rejection，window.onerror 抓不到，表现是页面完全空白且毫无报错。
let playlistToken = 0;

/* ------------------------------------------------------------------ 启动 */
boot();

async function boot() {
  tickClock();
  setInterval(tickClock, 1000);
  setInterval(checkSchedule, 20000);
  // 立刻判一次，不能只靠 20 秒的定时器：如果正好在息屏时段里开机/重启，
  // 屏幕会白白亮最多 20 秒才黑下去（定时重启的场景每天都会复现）。
  checkSchedule();
  installInput();
  installSSE();
  await loadPlaylist();
}

/* ------------------------------------------------------------------ 拉取播放列表 */
async function loadPlaylist({ keepPosition = false } = {}) {
  const token = ++playlistToken;
  try {
    const res = await fetch('/api/playlist', { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (token !== playlistToken) return; // 已经有更新的请求了，这次结果作废

    clearTimeout(S.retryTimer);
    S.settings = data.settings;
    S.revision = data.revision;
    S.items = data.items || [];
    // 换了内容就重新给失败过的视频一次机会；还在列表里的继续跳过
    const live = new Set(S.items.map((i) => i.url));
    for (const url of S.failed) if (!live.has(url)) S.failed.delete(url);
    S.stalled = false;
    applySettings();
    scheduleReload();

    if (!S.items.length) {
      showNotice(
        '相册里还没有照片',
        '在下面的地址里选择要在电视上播放的文件夹或照片。选好并保存后，这块屏幕会自动开始播放，不需要重启。'
      );
      // 服务端首次扫描是在后台跑的，刚开机时列表必然是空的。
      // 正常情况下 SSE 的 library-changed 会通知我们，但万一那条事件丢了
      // （连接断过、队列溢出），不轮询就会一直停在提示页上，所以自己也兜底。
      clearTimeout(S.emptyTimer);
      S.emptyTimer = setTimeout(() => loadPlaylist({ keepPosition: true }), 30000);
      return;
    }

    clearTimeout(S.emptyTimer);
    hideNotice();
    const next = keepPosition ? Math.min(S.index + 1, S.items.length - 1) : 0;
    if (next >= S.items.length) S.index = -1;
    await showIndex(next, { instant: !S.shown });
    S.shown = true;
  } catch (err) {
    if (token !== playlistToken) return;
    showNotice('连接相册服务失败', '正在重试…请确认相册服务容器已经启动。');
    // 定时器要存起来：不然失败后即使很快恢复成功，这个待触发的重试还是会再打一次
    clearTimeout(S.retryTimer);
    S.retryTimer = setTimeout(() => loadPlaylist({ keepPosition: true }), 10000);
  }
}

/* ------------------------------------------------------------------ 切图 */
async function showIndex(target, { instant = false } = {}) {
  if (!S.items.length) return;

  const gen = ++S.generation;
  S.index = ((target % S.items.length) + S.items.length) % S.items.length;
  const item = S.items[S.index];

  const next = S.activeSlot === 0 ? els.layerB : els.layerA;
  const prev = S.activeSlot === 0 ? els.layerA : els.layerB;

  const tms =
    instant || !S.settings || S.settings.transition === 'none'
      ? 0
      : S.settings.transition_ms;

  clearTimeout(S.timer);
  next.classList.remove('visible');
  await populate(next, item, gen);
  if (gen !== S.generation) return; // 期间又被切了，这一帧作废

  els.stage.style.setProperty('--tms', tms + 'ms');
  next.classList.add('visible');
  prev.classList.remove('visible');
  S.activeSlot = 1 - S.activeSlot;

  updateCaption(item);
  scheduleNext(item, gen);

  // 视频不进内存缓存，交叉淡出后就把上一个图层清掉，省显存也避免后台仍在播放
  setTimeout(() => {
    if (gen === S.generation && prev.querySelector('video')) prev.innerHTML = '';
  }, tms + 120);

  preloadAhead();
}

function populate(layer, item, gen) {
  return new Promise((resolve) => {
    const s = S.settings;
    layer.innerHTML = '';

    const frame = document.createElement('div');
    frame.className = 'frame';

    if (s.ken_burns && item.kind === 'image') {
      frame.classList.add('kb');
      const amount = Math.max(1, Math.min(1.5, s.ken_burns_amount));
      const spread = (amount - 1) * 100 * 0.6;
      const dirs = [
        [1, 1], [-1, 1], [1, -1], [-1, -1],
      ];
      const [dx, dy] = dirs[Math.floor(Math.random() * dirs.length)];
      frame.style.setProperty('--kb', String(amount));
      frame.style.setProperty('--kbx', (dx * spread).toFixed(2) + '%');
      frame.style.setProperty('--kby', (dy * spread).toFixed(2) + '%');
      frame.style.setProperty('--kborigin', dx > 0 ? '0% 50%' : '100% 50%');
      frame.style.setProperty('--kbdur', dwellSeconds(item) + s.transition_ms / 1000 + 's');
    }

    // contain 模式下用同一张图糊成背景，电视上不会有两条生硬的黑边
    if (s.fit === 'contain' && s.matte) {
      const matte = document.createElement('div');
      matte.className = 'matte';
      const bg = document.createElement('img');
      bg.className = 'bg';
      bg.alt = '';
      bg.decoding = 'async';
      bg.src = item.url;
      matte.appendChild(bg);
      frame.appendChild(matte);
    }

    let fg;
    if (item.kind === 'video') {
      fg = document.createElement('video');
      fg.className = 'fg';
      fg.src = item.url;
      fg.muted = !!s.muted;
      fg.playsInline = true;
      fg.autoplay = true;
      fg.preload = 'auto';
      fg.addEventListener('error', () => {
        // 浏览器解不了的编码（H.265 / MKV 等）直接跳过，别卡在这一张
        if (gen === S.generation) markFailed(item, gen);
      });
      fg.addEventListener('ended', () => {
        if (gen === S.generation) advance(1);
      });
    } else {
      fg = document.createElement('img');
      fg.className = 'fg';
      fg.alt = '';
      fg.decoding = 'async';
      fg.src = item.url;
    }

    frame.appendChild(fg);
    layer.appendChild(frame);

    waitReady(fg, gen).then(resolve);
  });
}

/** 等图片解码完 / 视频有画面。失败也放行，交给浏览器显示占位。 */
function waitReady(node, gen) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(finish, 15000);

    if (node.tagName === 'VIDEO') {
      if (node.readyState >= 2) return finish();
      node.addEventListener('loadeddata', finish, { once: true });
      node.addEventListener('error', finish, { once: true });
      return;
    }

    if (typeof node.decode === 'function') {
      node.decode().then(finish, finish);
      return;
    }
    if (node.complete) return finish();
    node.addEventListener('load', finish, { once: true });
    node.addEventListener('error', finish, { once: true });
  });
}

/* ------------------------------------------------------------------ 节奏控制 */
function dwellSeconds(item) {
  const s = S.settings;
  return item.kind === 'video' ? s.videos_max_seconds : s.interval_seconds;
}

function scheduleNext(item, gen) {
  clearTimeout(S.timer);
  if (S.paused || S.asleep || S.stalled || !S.settings) return;

  const ms =
    item.kind === 'video'
      ? S.settings.videos_max_seconds * 1000
      : S.settings.interval_seconds * 1000;

  S.timer = setTimeout(() => {
    if (gen === S.generation) advance(1);
  }, ms);
}

/** 视频解不出来时的处理。
 *
 * 这里必须**先停顿再跳**：坏视频的 error 是立刻触发的，如果直接 advance(1)，
 * 遇到连续多个坏视频就会变成每秒上千次的空转（实测过 4000+ 次/秒，浏览器直接假死）。
 * 全部内容都放不了时干脆停下并给出可执行的提示，而不是无限刷。
 */
function markFailed(item, gen) {
  S.failed.add(item.url);

  if (S.items.length && S.failed.size >= S.items.length) {
    S.stalled = true;
    clearTimeout(S.timer);
    showNotice(
      '这些视频浏览器播不了',
      'Chromium 解不了它们的编码（常见于 H.265/HEVC 和 MKV）。请到管理界面把「播放视频」关掉，或者只选图片文件夹；改完保存后这块屏幕会自动恢复。'
    );
    return;
  }

  setTimeout(() => {
    if (gen === S.generation && !S.stalled) advance(1);
  }, 1500);
}

function advance(delta) {
  if (!S.items.length) return;
  // 跳过已知播不出来的条目，否则按「下一张」会在坏视频之间反复横跳
  let idx = S.index;
  for (let k = 0; k < S.items.length; k++) {
    idx = ((idx + delta) % S.items.length + S.items.length) % S.items.length;
    if (!S.failed.has(S.items[idx].url)) break;
  }
  showIndex(idx);
}

function preloadAhead() {
  const n = Math.max(1, S.settings.preload_count);
  for (let k = 1; k <= n; k++) {
    const item = S.items[(S.index + k) % S.items.length];
    if (!item || item.kind !== 'image') continue;
    if (S.preloaded.has(item.url)) continue;
    const img = new Image();
    img.decoding = 'async';
    img.src = item.url;
    S.preloaded.add(item.url);
    if (S.preloaded.size > 40) S.preloaded.clear();
  }
}

/* ------------------------------------------------------------------ 画面设置 */
function applySettings() {
  const s = S.settings;
  if (!s) return;
  document.body.dataset.transition = s.transition;
  document.body.dataset.fit = s.fit;
  document.body.dataset.matte = String(!!s.matte && s.fit === 'contain');
  els.stage.style.setProperty('--bg', s.background);
  els.clock.hidden = !s.show_clock;
  els.caption.hidden = !(s.show_caption && s.caption_mode !== 'none');
}

function scheduleReload() {
  clearInterval(S.reloadTimer);
  const minutes = S.settings && S.settings.reload_playlist_minutes;
  if (!minutes || minutes <= 0) return;
  // 定期重新拉取：随机顺序的大库靠这个慢慢把所有照片轮一遍
  S.reloadTimer = setInterval(() => loadPlaylist({ keepPosition: true }), minutes * 60000);
}

/* ------------------------------------------------------------------ 时钟 / 说明文字 */
function tickClock() {
  if (!S.settings || !S.settings.show_clock) return;
  const now = new Date();
  let time;
  if (S.settings.clock_24h) {
    time = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
  } else {
    const h12 = now.getHours() % 12 || 12;
    time = `${now.getHours() < 12 ? '上午' : '下午'} ${h12}:${pad(now.getMinutes())}`;
  }
  const dateStr = `${now.getFullYear()}年${now.getMonth() + 1}月${now.getDate()}日 星期${WEEKDAYS[now.getDay()]}`;
  els.clock.innerHTML = `${time}<span class="date">${dateStr}</span>`;
}

function updateCaption(item) {
  const s = S.settings;
  if (!s.show_caption || s.caption_mode === 'none') {
    els.caption.textContent = '';
    return;
  }
  const d = new Date(item.mtime * 1000);
  const dateStr = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const name = item.name.replace(/\.[^.]+$/, '');
  els.caption.textContent =
    s.caption_mode === 'date' ? dateStr : s.caption_mode === 'name' ? name : `${dateStr} · ${name}`;
}

/* ------------------------------------------------------------------ 夜间息屏 */
function checkSchedule() {
  if (!S.settings) return;
  const shouldSleep = computeAsleep(new Date());
  if (shouldSleep === S.asleep) return;

  S.asleep = shouldSleep;
  document.body.classList.toggle('asleep', shouldSleep);
  if (shouldSleep) {
    clearTimeout(S.timer);
    flashBanner('已进入息屏时段');
  } else {
    flashBanner('已恢复播放');
    advance(1);
  }
}

function computeAsleep(now) {
  const sched = S.settings.schedule;
  if (!sched || !sched.enabled) return false;
  const cur = now.getHours() * 60 + now.getMinutes();
  const toMin = (v) => {
    const [h, m] = String(v).split(':').map(Number);
    return (h || 0) * 60 + (m || 0);
  };
  const off = toMin(sched.off);
  const on = toMin(sched.on);
  if (off === on) return false;
  return off < on ? cur >= off && cur < on : cur >= off || cur < on;
}

/* ------------------------------------------------------------------ 提示层 */
function showNotice(title, body) {
  els.noticeTitle.textContent = title;
  els.noticeBody.textContent = body;
  els.noticeUrl.textContent = location.origin + '/';
  els.notice.hidden = false;
  if (S.items.length) return;
  els.clock.hidden = true;
  els.caption.hidden = true;
}

function hideNotice() {
  els.notice.hidden = true;
  if (S.settings) applySettings();
}

function flashBanner(text) {
  els.banner.textContent = text;
  // display.html 里 #banner 自带 hidden 属性，而这里以前只加 .show 类、
  // 从来没把 hidden 去掉 —— 于是「已暂停」「已刷新播放列表」「进入息屏时段」
  // 这些反馈一条都看不见，用户按了空格以为死机了。
  els.banner.hidden = false;
  els.banner.classList.add('show');
  clearTimeout(S.bannerTimer);
  S.bannerTimer = setTimeout(() => {
    els.banner.classList.remove('show');
    els.banner.hidden = true;
  }, 2200);
}

/* ------------------------------------------------------------------ 交互 */
function installInput() {
  document.addEventListener('keydown', (ev) => {
    switch (ev.key) {
      case 'ArrowRight':
      case 'PageDown':
        advance(1);
        break;
      case 'ArrowLeft':
      case 'PageUp':
        advance(-1);
        break;
      case ' ':
      case 'Spacebar':
        togglePause();
        break;
      case 'r':
      case 'R':
        loadPlaylist({ keepPosition: true });
        flashBanner('已刷新播放列表');
        break;
      case 'f':
      case 'F':
        if (document.fullscreenElement) document.exitFullscreen();
        else document.documentElement.requestFullscreen().catch(() => {});
        break;
      default:
        return;
    }
    ev.preventDefault();
  });

  // 鼠标停手 3 秒后隐藏指针（电视上不该看到箭头）
  let cursorTimer = null;
  document.addEventListener('mousemove', () => {
    document.body.classList.add('show-cursor');
    clearTimeout(cursorTimer);
    cursorTimer = setTimeout(() => document.body.classList.remove('show-cursor'), 3000);
  });

  // 触屏 / 遥控器点击：右半屏下一张，左半屏上一张
  document.addEventListener('click', (ev) => {
    if (!S.items.length) return;
    advance(ev.clientX > window.innerWidth / 2 ? 1 : -1);
  });
}

function togglePause() {
  S.paused = !S.paused;
  if (S.paused) {
    clearTimeout(S.timer);
    flashBanner('已暂停');
  } else {
    flashBanner('继续播放');
    scheduleNext(S.items[S.index] || { kind: 'image' }, S.generation);
  }
}

function installSSE() {
  let es = null;
  let openedOnce = false;

  const connect = () => {
    es = new EventSource('/api/events');

    es.onopen = () => {
      if (!openedOnce) {
        openedOnce = true;
        return; // 首次连接：boot() 已经在拉播放列表了，不用重复
      }
      // 重连成功必须自己补一次同步：断线期间错过的 settings-changed /
      // library-changed 是**不会**补发的，只重连不重拉的话配置会永久停在旧值
      // （实测：断线期间改成 order=date-desc，重连后显示端仍然是旧顺序）。
      loadPlaylist({ keepPosition: true });
    };

    es.addEventListener('settings-changed', () => {
      // 配置变了：直接重拉，新设置即时生效
      loadPlaylist({ keepPosition: true });
    });
    es.addEventListener('library-changed', () => loadPlaylist({ keepPosition: true }));
    es.addEventListener('control', (ev) => {
      let payload = {};
      try {
        payload = JSON.parse(ev.data);
      } catch (_) {
        return;
      }
      switch (payload.action) {
        case 'next':
          advance(1);
          break;
        case 'prev':
          advance(-1);
          break;
        case 'pause':
          if (!S.paused) togglePause();
          break;
        case 'play':
          if (S.paused) togglePause();
          break;
        case 'toggle':
          togglePause();
          break;
        case 'reload':
          loadPlaylist({ keepPosition: true });
          break;
        case 'goto':
          showIndex(Number(payload.index) || 0);
          break;
        default:
          break;
      }
    });

    // 网络中断时浏览器会自动重连；但如果服务端返回了非 200（例如反代 502），
    // 规范要求直接 fail the connection，EventSource 会永久 CLOSED，不再重连。
    // 显示端要连续跑几个月，所以这里自己兜底重建。
    es.onerror = () => {
      if (es.readyState === EventSource.CLOSED) {
        es.close();
        setTimeout(connect, 5000);
      }
    };
  };

  connect();
}
