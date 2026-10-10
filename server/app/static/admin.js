/* 管理端逻辑：选文件夹、挑照片、调参数、遥控显示端。 */
'use strict';

/* ------------------------------------------------------------------ 小工具 */
const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'checked') node.checked = !!value;
    else if (key === 'value') node.value = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (_) {
    data = { detail: text };
  }
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === 'string' ? detail : `请求失败 (HTTP ${res.status})`);
  }
  return data;
}

let toastTimer = null;
function toast(message, kind = '') {
  const box = $('#toast');
  box.textContent = message;
  box.className = kind;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    box.hidden = true;
  }, kind === 'error' ? 8000 : 2600);
}

const getPath = (obj, path) => path.split('.').reduce((acc, k) => (acc == null ? acc : acc[k]), obj);
function setPath(obj, path, value) {
  const keys = path.split('.');
  const last = keys.pop();
  const target = keys.reduce((acc, k) => (acc[k] = acc[k] || {}), obj);
  target[last] = value;
}

/* ------------------------------------------------------------------ 状态 */
const state = {
  settings: null,
  saved: null,
  revision: 0,
  roots: [],
  stats: {},
  caps: {},
  browse: null,
  browsePath: null,
  page: 1,
  pageSize: 120,
  countTimer: null,
  bootTimer: null,
};

// 浏览请求的序号。/api/browse 在索引过期时会触发整库重扫，大库上要好几秒；
// 用户等不及点了别的目录时，先发的慢响应后到，会把界面覆盖回旧目录 ——
// 用户以为自己在 A 目录里勾照片，其实勾的是 B 目录的文件。
let navToken = 0;

/* ------------------------------------------------------------------ 启动 */
boot();

async function boot() {
  try {
    const data = await api('/api/state');
    state.settings = data.settings;
    state.saved = JSON.parse(JSON.stringify(data.settings));
    state.revision = data.revision;
    state.roots = data.roots || [];
    state.stats = data.stats || {};
    state.caps = data.capabilities || {};

    $('#version').textContent = 'v' + data.version;
    renderHeadline();
    renderCapabilities();
    renderNavigators();
    bindSettingsForm();
    bindTabs();
    bindToolbar();
    bindMaintenance();

    const first = state.roots.find((r) => r.exists) || state.roots[0];
    if (first) await navigate(first.path);
    else renderEmptyLibrary();
  } catch (err) {
    // 自动重试：否则一次偶发失败（服务端还在起、网络抖一下）会让标签页和所有按钮
    // 永远停在「未绑定」状态，整页只剩一个报错提示，什么都点不动。
    toast('加载失败：' + err.message + '，5 秒后自动重试', 'error');
    clearTimeout(state.bootTimer);
    state.bootTimer = setTimeout(boot, 5000);
  }
}

function renderEmptyLibrary() {
  $('#dirlist').appendChild(
    el('div', { class: 'empty' },
      el('div', { text: '没有找到任何照片目录。' }),
      el('div', { class: 'note', text: '请在 docker-compose.yml 里把 NAS 上的照片目录挂载到容器内，并设置 PHOTO_ROOTS 环境变量。' }),
    )
  );
}

/* ------------------------------------------------------------------ 顶栏信息 */
function renderHeadline() {
  const s = state.stats || {};
  const total = s.total_media || 0;
  const imgs = s.images || 0;
  const vids = s.videos || 0;
  const selected = state.settings.sources.length + state.settings.include_files.length;
  $('#headline').textContent =
    `媒体库 ${total} 项（图片 ${imgs} / 视频 ${vids}）· 已选 ${selected} 个来源`;
}

function renderCapabilities() {
  const box = $('#capabilities');
  if (!box) return;
  box.innerHTML = '';
  box.appendChild(
    el('div', {}, state.caps.heif
      ? '✅ HEIC/HEIF 解码可用（iPhone 照片可以直接显示）'
      : el('span', { class: 'bad', text: '⚠️ HEIC 解码不可用，iPhone 的 .heic 照片会被跳过' }))
  );
  box.appendChild(
    el('div', {}, state.caps.ffmpeg
      ? '✅ ffmpeg 可用（视频可以生成封面缩略图）'
      : el('span', { class: 'bad', text: '⚠️ 未安装 ffmpeg，视频只能显示占位图' }))
  );
}

function markDirty() {
  const dirty = JSON.stringify(state.settings) !== JSON.stringify(state.saved);
  $('#btn-save').disabled = !dirty;
  $('#status-text').innerHTML = dirty
    ? '<span class="dirty">有未保存的改动</span>'
    : '已保存';
  renderHeadline();
  if (dirty) scheduleCountUpdate();
}

/* ------------------------------------------------------------------ 标签页 */
function bindTabs() {
  $('#tabs').addEventListener('click', (ev) => {
    const tab = ev.target.closest('.tab');
    if (!tab) return;
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t === tab));
    document.querySelectorAll('.panel').forEach((p) => p.classList.remove('active'));
    $('#panel-' + tab.dataset.tab).classList.add('active');
  });
}

/* ------------------------------------------------------------------ 目录浏览 */
function rootFor(path) {
  return state.roots.find((r) => path === r.path || path.startsWith(r.path + '/')) || null;
}

function renderNavigators() {
  for (const id of ['#navigator', '#navigator-photos']) {
    const box = $(id);
    if (!box) continue;
    box.innerHTML = '';
    state.roots.forEach((root) => {
      const btn = el('button', { class: 'navroot', onclick: () => navigate(root.path) },
        root.name,
        el('span', { class: 'muted', text: root.media_count + ' 张' }),
      );
      if (state.browsePath && state.browsePath.startsWith(root.path)) btn.classList.add('active');
      if (!root.exists) btn.classList.add('bad');
      box.appendChild(btn);
    });
  }
}

async function navigate(path) {
  const token = ++navToken;
  state.browsePath = path;
  state.page = 1;
  try {
    const data = await api(`/api/browse?path=${encodeURIComponent(path)}&page=1&page_size=${state.pageSize}`);
    if (token !== navToken) return; // 用户已经点了别的目录，这次结果作废
    state.browse = data;
    renderNavigators();
    renderCrumb('#crumb-folders', path);
    renderCrumb('#crumb-photos', path);
    renderDirList();
    renderGrid();
  } catch (err) {
    if (token !== navToken) return;
    toast('无法打开目录：' + err.message, 'error');
  }
}

function renderCrumb(selector, path) {
  const box = $(selector);
  if (!box) return;
  box.innerHTML = '';
  const root = rootFor(path);
  if (!root) return;

  box.appendChild(el('button', { text: root.name, onclick: () => navigate(root.path) }));
  const rel = path.slice(root.path.length).split('/').filter(Boolean);
  let acc = root.path;
  rel.forEach((seg, i) => {
    box.appendChild(el('span', { class: 'sep', text: '/' }));
    acc += '/' + seg;
    if (i === rel.length - 1) {
      box.appendChild(el('span', { class: 'current', text: seg }));
    } else {
      const target = acc;
      box.appendChild(el('button', { text: seg, onclick: () => navigate(target) }));
    }
  });
}

function renderDirList() {
  const box = $('#dirlist');
  box.innerHTML = '';
  const b = state.browse;
  if (!b) return;

  box.appendChild(
    makeDirRow(
      { name: '当前整个文件夹（含子目录）', path: b.path, media_count: b.subtree_count ?? b.direct_count },
      { self: true }
    )
  );

  (b.dirs || []).forEach((d) => {
    if (!d.media_count) return;
    box.appendChild(makeDirRow(d));
  });

  if (!(b.dirs || []).some((d) => d.media_count)) {
    box.appendChild(el('div', { class: 'empty', text: '这个目录下没有包含照片的子文件夹。' }));
  }
}

function makeDirRow(dir, { self = false } = {}) {
  const checkbox = el('input', { type: 'checkbox' });
  checkbox.checked = state.settings.sources.includes(dir.path);
  checkbox.addEventListener('change', () => toggleSource(dir.path));

  const name = el('span', {
    class: 'name',
    text: dir.name,
    title: dir.path,
    onclick: () => { if (!self) navigate(dir.path); },
  });

  const row = el('div', { class: 'dirrow' }, checkbox, name,
    el('span', { class: 'meta', text: `${dir.media_count} 张` }));

  if (!self) {
    row.appendChild(el('button', { class: 'btn small', text: '进入', onclick: () => navigate(dir.path) }));
  }
  return row;
}

function toggleSource(path) {
  const sources = state.settings.sources;
  const idx = sources.indexOf(path);
  if (idx >= 0) sources.splice(idx, 1);
  else sources.push(path);
  markDirty();
  renderDirList();
  renderGrid();
  renderPickers();
}

/* ------------------------------------------------------------------ 照片网格 */
function photoState(item) {
  const s = state.settings;
  if (s.exclude_files.includes(item.path)) return 'excluded';
  if (s.include_files.includes(item.path)) return 'included';
  // 必须跟服务端一致：recursive=false 时只取文件夹的**直接子级**，
  // 子目录里的照片根本不会播。这里如果一律按前缀判断，
  // 界面会把子目录照片标成「随文件夹」，用户以为在播，其实没有。
  const inside = (src) =>
    item.path.startsWith(src + '/') &&
    (s.recursive || item.path.indexOf('/', src.length + 1) === -1);
  if (s.sources.some(inside)) return 'inherited';
  return 'none';
}

const STATE_LABEL = { none: '', included: '已选', inherited: '随文件夹', excluded: '已排除' };

function renderGrid() {
  const box = $('#photogrid');
  box.innerHTML = '';
  const b = state.browse;
  if (!b) return;
  const files = b.files || [];

  if (!files.length) {
    // 照片全在子文件夹里是很常见的情况，别让用户以为这里空了
    const hasSubPhotos = (b.dirs || []).some((d) => d.media_count > 0);
    box.appendChild(
      el('div', { class: 'empty', text: hasSubPhotos
        ? '这个目录下没有直接的照片，照片都在下面的子文件夹里 —— 回到「选择文件夹」勾选整个文件夹即可。'
        : '这个目录下没有照片。' })
    );
  }

  files.forEach((item) => {
    const st = photoState(item);
    const img = el('img', { src: item.thumb, loading: 'lazy', alt: '', decoding: 'async' });
    // 坏图（截断/损坏）后端会返回 415，浏览器只会显示一个破图图标。
    // 加个兜底，让用户看得出是「这张照片有问题」，而不是以为界面坏了。
    img.addEventListener('error', () => {
      img.classList.add('broken');
      img.alt = '无法预览';
    });
    const cell = el('div', {
      class: `cell ${st === 'none' ? '' : st} ${item.kind === 'video' ? 'video' : ''}`,
      title: item.path,
      onclick: () => togglePhoto(item),
    }, img);
    if (STATE_LABEL[st]) cell.appendChild(el('div', { class: 'tag', text: STATE_LABEL[st] }));
    if (item.kind === 'video') cell.appendChild(el('div', { class: 'tag', text: '▶ 视频' }));
    cell.appendChild(el('div', { class: 'fname', text: item.name }));
    box.appendChild(cell);
  });

  renderPager();
  renderPickers();
}

function renderPager() {
  const box = $('#pager');
  box.innerHTML = '';
  const b = state.browse;
  if (!b) return;
  const pages = Math.max(1, Math.ceil(b.total_files / b.page_size));
  if (pages <= 1) return;

  box.appendChild(el('button', {
    class: 'btn small', text: '上一页',
    onclick: () => goPage(b.page - 1),
  }));
  box.appendChild(el('span', { text: `第 ${b.page} / ${pages} 页 · 本目录共 ${b.total_files} 张` }));
  box.appendChild(el('button', {
    class: 'btn small', text: '下一页',
    onclick: () => goPage(b.page + 1),
  }));
}

async function goPage(page) {
  const b = state.browse;
  if (!b || page < 1) return;
  const pages = Math.max(1, Math.ceil(b.total_files / b.page_size));
  if (page > pages) return;
  const token = ++navToken;
  try {
    const data = await api(
      `/api/browse?path=${encodeURIComponent(state.browsePath)}&page=${page}&page_size=${state.pageSize}`
    );
    if (token !== navToken) return;
    state.browse = data;
    renderGrid();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (err) {
    // 不加 try 的话这里只会是一个未捕获的 promise rejection：没有提示、状态栏还写着「就绪」，
    // 按钮看起来就是坏的（同类的 navigate() 是会弹 toast 的）
    if (token === navToken) toast('翻页失败：' + err.message, 'error');
  }
}

function togglePhoto(item) {
  const s = state.settings;
  const st = photoState(item);
  const drop = (arr, value) => {
    const i = arr.indexOf(value);
    if (i >= 0) arr.splice(i, 1);
  };

  if (st === 'excluded') {
    drop(s.exclude_files, item.path);
  } else if (st === 'included') {
    drop(s.include_files, item.path);
  } else if (st === 'inherited') {
    if (!s.exclude_files.includes(item.path)) s.exclude_files.push(item.path);
  } else if (!s.include_files.includes(item.path)) {
    s.include_files.push(item.path);
  }

  markDirty();
  renderGrid();
}

function bindToolbar() {
  $('#btn-select-page').addEventListener('click', () => {
    const s = state.settings;
    (state.browse.files || []).forEach((item) => {
      if (!s.include_files.includes(item.path)) s.include_files.push(item.path);
      const i = s.exclude_files.indexOf(item.path);
      if (i >= 0) s.exclude_files.splice(i, 1);
    });
    markDirty();
    renderGrid();
  });

  $('#btn-clear-page').addEventListener('click', () => {
    const s = state.settings;
    const files = state.browse.files || [];
    const paths = new Set(files.map((i) => i.path));
    // 先把「继承」状态算出来：下面一改 exclude_files，photoState 的结果就变了
    const inherited = files.filter((i) => photoState(i) === 'inherited').map((i) => i.path);
    s.include_files = s.include_files.filter((p) => !paths.has(p));
    s.exclude_files = s.exclude_files.filter((p) => !paths.has(p));
    // 随文件夹继承来的照片不在 include_files 里，只删 include/exclude 清不掉，
    // 必须显式加进 exclude_files —— 否则点了「清空本页选择」照片还是一片绿，像按钮坏了。
    inherited.forEach((p) => {
      if (!s.exclude_files.includes(p)) s.exclude_files.push(p);
    });
    markDirty();
    renderGrid();
  });

  $('#btn-exclude-page').addEventListener('click', () => {
    const s = state.settings;
    (state.browse.files || []).forEach((item) => {
      if (!s.exclude_files.includes(item.path)) s.exclude_files.push(item.path);
      const i = s.include_files.indexOf(item.path);
      if (i >= 0) s.include_files.splice(i, 1);
    });
    markDirty();
    renderGrid();
  });
}

function renderPickers() {
  renderChips('#chips-sources', state.settings.sources, (path) => {
    const i = state.settings.sources.indexOf(path);
    if (i >= 0) state.settings.sources.splice(i, 1);
    markDirty();
    renderDirList();
    renderGrid();
    renderPickers();
  });
  renderChips('#chips-includes', state.settings.include_files, (path) => {
    const i = state.settings.include_files.indexOf(path);
    if (i >= 0) state.settings.include_files.splice(i, 1);
    markDirty();
    renderGrid();
  });
  renderChips('#chips-excludes', state.settings.exclude_files, (path) => {
    const i = state.settings.exclude_files.indexOf(path);
    if (i >= 0) state.settings.exclude_files.splice(i, 1);
    markDirty();
    renderGrid();
  });

  const root = rootFor(state.browsePath || '');
  $('#count-sources').textContent = state.settings.sources.length;
  $('#count-includes').textContent = state.settings.include_files.length;
  $('#count-excludes').textContent = state.settings.exclude_files.length;
  void root;
}
function shortPath(path, max = 46) {
  // 路径太长时保留**末尾**（最有信息量的部分）。
  // 之前是用 CSS 的 direction:rtl 做截断，但那会让开头的 "/" 被排到最右边，
  // 显示成 "private/tmp/…/photos/"，路径看着就是错的。
  if (path.length <= max) return path;
  const cut = path.lastIndexOf('/', path.length - max);
  return cut > 0 ? '…' + path.slice(cut) : '…' + path.slice(-(max - 1));
}

function renderChips(selector, paths, onRemove) {
  const box = $(selector);
  if (!box) return;
  box.innerHTML = '';
  if (!paths.length) {
    box.appendChild(el('div', { class: 'hint', text: '（还没有选中任何条目）' }));
    return;
  }
  paths.forEach((path) => {
    box.appendChild(
      el('div', { class: 'chip', title: path },
        el('span', { class: 'label', text: shortPath(path) }),
        el('button', { text: '✕', title: '移除', onclick: () => onRemove(path) }),
      )
    );
  });
}

/* ------------------------------------------------------------------ 数量预估 */
function scheduleCountUpdate() {
  clearTimeout(state.countTimer);
  state.countTimer = setTimeout(async () => {
    try {
      const data = await api('/api/count', { method: 'POST', body: { settings: state.settings } });
      $('#status-count').textContent =
        `按当前选择将播放 ${data.total} 项（图片 ${data.images} / 视频 ${data.videos}）`;
    } catch (_) {
      $('#status-count').textContent = '';
    }
  }, 400);
}

/* ------------------------------------------------------------------ 设置表单 */
function bindSettingsForm() {
  document.querySelectorAll('[data-path]').forEach((input) => {
    const path = input.dataset.path;
    const value = getPath(state.settings, path);
    if (input.type === 'checkbox') input.checked = !!value;
    else input.value = value === null || value === undefined ? '' : value;

    const handler = () => {
      let next;
      if (input.type === 'checkbox') next = input.checked;
      else if (input.type === 'number') next = input.value === '' ? 0 : Number(input.value);
      else next = input.value;
      setPath(state.settings, path, next);
      markDirty();
    };
    input.addEventListener('input', handler);
    input.addEventListener('change', handler);
  });

  $('#btn-save').addEventListener('click', save);
  document.addEventListener('keydown', (ev) => {
    // 只在没有焦点在输入框里的时候拦截方向键，免得调数字时被抢走
    const tag = document.activeElement?.tagName || '';
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(tag);
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 's') {
      ev.preventDefault();
      if (!$('#btn-save').disabled) save();
      return;
    }
    if (typing) return;
    if (ev.key === 'ArrowRight') sendControl('next');
    if (ev.key === 'ArrowLeft') sendControl('prev');
    // 焦点在按钮上时不要抢空格 —— 那是键盘用户激活按钮的方式
    if (ev.key === ' ' && tag !== 'BUTTON') {
      ev.preventDefault();
      sendControl('toggle');
    }
  });

  $('#rc-prev').addEventListener('click', () => sendControl('prev'));
  $('#rc-next').addEventListener('click', () => sendControl('next'));
  $('#rc-toggle').addEventListener('click', () => sendControl('toggle'));
}

async function sendControl(action) {
  try {
    await api('/api/control', { method: 'POST', body: { action } });
  } catch (err) {
    toast('遥控失败：' + err.message, 'error');
  }
}

async function save() {
  const btn = $('#btn-save');
  btn.disabled = true;
  $('#status-text').textContent = '正在保存…';
  // 必须在 await **之前**快照。PUT 在途时用户还能继续改设置，
  // 如果等响应回来再读 state.settings，那些改动会被一并标成「已保存」，
  // 但它们从未发给服务端 —— 改动静默丢失、按钮还变灰了，用户不会再去点第二次。
  const snapshot = JSON.parse(JSON.stringify(state.settings));
  try {
    const data = await api('/api/settings', { method: 'PUT', body: { settings: snapshot } });
    state.revision = data.revision;
    state.saved = snapshot;
    toast('已保存，显示端会自动应用', 'ok');
    markDirty();
    // 重扫一遍媒体库：保存期间可能刚好有新照片落盘
    await api('/api/rescan', { method: 'POST' }).catch(() => {});
    if (state.browsePath) await navigate(state.browsePath);
  } catch (err) {
    toast('保存失败：' + err.message, 'error');
    btn.disabled = false;
  }
}

function bindMaintenance() {
  $('#btn-rescan').addEventListener('click', async () => {
    try {
      const data = await api('/api/rescan', { method: 'POST' });
      state.stats = data.stats;
      renderHeadline();
      toast(`已重新扫描：共 ${data.stats.total_media} 项`, 'ok');
      if (state.browsePath) await navigate(state.browsePath);
    } catch (err) {
      toast('扫描失败：' + err.message, 'error');
    }
  });

  $('#btn-purge').addEventListener('click', async () => {
    try {
      const data = await api('/api/cache/purge', { method: 'POST' });
      toast(`已清理 ${data.removed} 个缓存文件`, 'ok');
    } catch (err) {
      toast('清理失败：' + err.message, 'error');
    }
  });
}
