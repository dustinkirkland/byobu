'use strict';

// ── state ──────────────────────────────────────────────────────────────────
let ws = null;
let sessions = [];
let currentPane = null;
let currentSessionId = null;
let currentWindowId = null;
let forcedSessionId = null; // set after creating a new session
let statusInterval = null;

// ── DOM refs ───────────────────────────────────────────────────────────────
const pairOverlay   = document.getElementById('pair-overlay');
const pairCodeInput = document.getElementById('pair-code');
const pairBtn       = document.getElementById('pair-btn');
const pairError     = document.getElementById('pair-error');
const xyzLabel      = document.getElementById('xyz-label');
const btnRefresh    = document.getElementById('btn-refresh');
const output        = document.getElementById('output');
const statusbar     = document.getElementById('statusbar');
const statusText    = document.getElementById('status-text');
const cmdInput      = document.getElementById('cmd');
const btnSend       = document.getElementById('btn-send');
const btnKbdMode    = document.getElementById('btn-kbd-mode');
const machineSelect    = document.getElementById('machine-select');
const btnInstall       = document.getElementById('btn-install');
const iosInstallTip    = document.getElementById('ios-install-tip');
const hostnameDisplay  = document.getElementById('hostname-display');
const headerClock      = document.getElementById('header-clock');
const statuslineLeft   = document.getElementById('statusline-left');
const statuslineRight  = document.getElementById('statusline-right');
const ctxOverlay       = document.getElementById('ctx-overlay');
const ctxMain          = document.getElementById('ctx-main');
const ctxRenamePane    = document.getElementById('ctx-rename-pane');
const ctxRenameWindow  = document.getElementById('ctx-rename-window');
const ctxRenameSession = document.getElementById('ctx-rename-session');
const ctxRenameForm    = document.getElementById('ctx-rename-form');
const ctxRenameLabel   = document.getElementById('ctx-rename-label');
const ctxRenameInput   = document.getElementById('ctx-rename-input');
const ctxCancel        = document.getElementById('ctx-cancel');
const createOverlay    = document.getElementById('create-overlay');
const createMain       = document.getElementById('create-main');
const createNameForm   = document.getElementById('create-name-form');
const createNameLabel  = document.getElementById('create-name-label');
const createNameInput  = document.getElementById('create-name-input');
const btnNew           = document.getElementById('btn-new');
const btnPrev          = document.getElementById('btn-prev');
const btnNext          = document.getElementById('btn-next');

// ── pane names (user-defined, stored in localStorage) ─────────────────────
// Key is scoped to the server hostname so names don't bleed across machines.
function _paneKey(paneId) { return `pane-name:${location.hostname}:${paneId}`; }
function getPaneName(paneId, fallback) { return localStorage.getItem(_paneKey(paneId)) || fallback; }
function setPaneName(paneId, name) {
  if (name) localStorage.setItem(_paneKey(paneId), name);
  else localStorage.removeItem(_paneKey(paneId));
}

// ── status ─────────────────────────────────────────────────────────────────
function setStatus(msg, cls) {
  statusText.textContent = msg;
  statusbar.className = cls || '';
}

// ── WebSocket ──────────────────────────────────────────────────────────────
function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen  = () => {
    setStatus('connected', 'connected');
    startClock();
    send({ type: 'list_sessions' });
    if (currentPane) send({ type: 'subscribe', pane_id: currentPane, lines: 300 });
  };
  ws.onclose = (evt) => {
    stopClock();
    if (evt.code === 4401) {
      showPairScreen();
      return;
    }
    setStatus('disconnected — reconnecting…', 'error');
    setTimeout(connect, 3000);
  };
  ws.onerror = () => setStatus('connection error', 'error');

  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch { return; }

    if (msg.server_ts) _serverOffset = msg.server_ts - Date.now();
    if (msg.server_tz) _serverTz = msg.server_tz;
    if (msg.type === 'sessions') {
      sessions = msg.data || [];
      if (msg.new_session) forcedSessionId = msg.new_session;
      rebuildPaneTree();
    } else if (msg.type === 'snapshot') {
      if (msg.pane_id === currentPane) renderOutput(msg.data, /*scroll=*/true);
    } else if (msg.type === 'update') {
      if (msg.pane_id !== currentPane) return;
      const atBottom = output.scrollHeight - output.scrollTop <= output.clientHeight + 60;
      renderOutput(msg.data, atBottom);
    } else if (msg.type === 'error') {
      setStatus(`error: ${msg.message}`, 'error');
    }
  };
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// ── xyz label ─────────────────────────────────────────────────────────────
function activePaneXYZ() {
  if (!currentPane) return '-:-:-';
  for (const s of sessions) {
    const x = parseInt(s.id.replace('$', ''), 10);
    for (const w of (s.windows || [])) {
      for (const p of (w.panes || [])) {
        if (p.id === currentPane) return `${x}:${w.index}:${p.index}`;
      }
    }
  }
  return '-:-:-';
}

function updateXYZLabel() {
  xyzLabel.textContent = activePaneXYZ();
}

// ── pane navigation ───────────────────────────────────────────────────────
function rebuildPaneTree() {
  const forced = forcedSessionId;
  if (forced) forcedSessionId = null;

  let autoFromForced = null;
  let autoFirst = null;
  let prevTarget = null;

  for (const s of sessions) {
    for (const w of (s.windows || [])) {
      for (const p of (w.panes || [])) {
        if (!p.dead) {
          const entry = { sessionId: s.id, windowId: w.id, paneId: p.id };
          if (forced === s.id && !autoFromForced) autoFromForced = entry;
          if (!autoFirst) autoFirst = entry;
          if (p.id === currentPane) prevTarget = entry;
        }
      }
    }
  }

  const target = prevTarget ?? autoFromForced ?? autoFirst ?? null;

  if (!target) {
    currentSessionId = null;
    currentWindowId  = null;
    currentPane      = null;
    cmdInput.disabled = true;
    btnSend.disabled  = true;
    updateXYZLabel();
    return;
  }

  if (target.paneId !== currentPane) {
    navigateTo(target.sessionId, target.windowId, target.paneId);
  } else {
    updateXYZLabel();
  }
}

function navigateTo(sessionId, windowId, paneId) {
  currentSessionId = sessionId;
  currentWindowId  = windowId;
  currentPane      = paneId;
  cmdInput.disabled = false;
  btnSend.disabled  = false;
  output.className  = '';
  output.textContent = 'loading…';
  send({ type: 'subscribe', pane_id: paneId, lines: 300 });
  updateXYZLabel();
}

// ── output rendering ───────────────────────────────────────────────────────
function renderOutput(text, scrollToBottom) {
  output.className = '';
  output.textContent = text;
  if (scrollToBottom) output.scrollTop = output.scrollHeight;
}

// ── send keys ─────────────────────────────────────────────────────────────
function sendKeys() {
  const keys = cmdInput.value;
  if (!keys || !currentPane) return;
  send({ type: 'send_keys', pane_id: currentPane, keys, enter: true });
  cmdInput.value = '';
  cmdInput.style.height = 'auto';
}

// ── events ─────────────────────────────────────────────────────────────────
btnRefresh.addEventListener('click',  () => send({ type: 'list_sessions' }));
cmdInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendKeys(); }
});
cmdInput.addEventListener('input', () => {
  cmdInput.style.height = 'auto';
  cmdInput.style.height = Math.min(cmdInput.scrollHeight, 160) + 'px';
});
btnSend.addEventListener('click', sendKeys);

// ── keyboard mode toggle (terminal ↔ text) ────────────────────────────────
let textMode = false;
function applyKbdMode() {
  if (textMode) {
    cmdInput.setAttribute('spellcheck', 'true');
    cmdInput.setAttribute('autocorrect', 'on');
    cmdInput.setAttribute('autocapitalize', 'sentences');
    output.style.whiteSpace = 'pre-wrap';
    btnKbdMode.textContent = 'Aa';
    btnKbdMode.title = 'Text mode — tap for terminal mode';
    btnKbdMode.style.color = 'var(--accent)';
  } else {
    cmdInput.setAttribute('spellcheck', 'false');
    cmdInput.setAttribute('autocorrect', 'off');
    cmdInput.setAttribute('autocapitalize', 'none');
    output.style.whiteSpace = 'pre';
    btnKbdMode.textContent = '$_';
    btnKbdMode.title = 'Terminal mode — tap to enable spell check';
    btnKbdMode.style.color = '';
  }
}
btnKbdMode.addEventListener('click', () => {
  textMode = !textMode;
  applyKbdMode();
  // blur + refocus so Android keyboard re-evaluates spellcheck state
  cmdInput.blur();
  setTimeout(() => cmdInput.focus(), 50);
});

// ── pane list (depth-first: panes → windows → sessions) ──────────────────
function flatPaneList() {
  const list = [];
  for (const s of sessions) {
    for (const w of (s.windows || [])) {
      for (const p of (w.panes || [])) {
        if (!p.dead) list.push({ sessionId: s.id, windowId: w.id, paneId: p.id });
      }
    }
  }
  return list;
}

function navigateRelative(delta) {
  const list = flatPaneList();
  if (list.length < 2) return;
  const idx = list.findIndex(e => e.paneId === currentPane);
  const next = list[((idx < 0 ? 0 : idx) + delta + list.length) % list.length];
  navigateTo(next.sessionId, next.windowId, next.paneId);
}

// ── touch handler: double-tap opens context menu ──────────────────────────
let _touchX = 0, _touchY = 0;
let _lastTap = 0;

output.addEventListener('touchstart', e => {
  _touchX = e.touches[0].clientX;
  _touchY = e.touches[0].clientY;
}, { passive: true });

output.addEventListener('touchend', e => {
  const dx = e.changedTouches[0].clientX - _touchX;
  const dy = e.changedTouches[0].clientY - _touchY;
  if (Math.abs(dx) < 20 && Math.abs(dy) < 20) {
    const now = Date.now();
    if (now - _lastTap < 300) { showCtxMenu(); _lastTap = 0; }
    else { _lastTap = now; }
  }
}, { passive: true });

btnPrev.addEventListener('click', () => navigateRelative(-1));
btnNext.addEventListener('click', () => navigateRelative(1));

// ── rename menu (double-tap) ──────────────────────────────────────────────
let _pendingRename = null; // { type, id }

function showCtxMenu() {
  if (!currentPane) return;
  const sess = sessions.find(s => s.id === currentSessionId);
  const win  = sess?.windows.find(w => w.id === currentWindowId);
  const pane = win?.panes.find(p => p.id === currentPane);
  const sNum = currentSessionId?.replace('$', '');

  ctxRenamePane.textContent    = `✎  Rename pane P${pane?.index ?? '?'} · ${getPaneName(currentPane, pane?.command ?? '?')}`;
  ctxRenameWindow.textContent  = `✎  Rename window W${win?.index ?? '?'} · ${win?.name ?? '?'}`;
  ctxRenameSession.textContent = `✎  Rename session S${sNum} · ${sess?.name ?? '?'}`;

  ctxMain.style.display = '';
  ctxRenameForm.style.display = 'none';
  ctxOverlay.style.display = 'flex';
}

function hideCtxMenu() {
  ctxOverlay.style.display = 'none';
  _pendingRename = null;
}

ctxCancel.addEventListener('click', hideCtxMenu);
ctxOverlay.addEventListener('click', e => { if (e.target === ctxOverlay) hideCtxMenu(); });

ctxRenamePane.addEventListener('click', () => {
  const win  = sessions.flatMap(s => s.windows).find(w => w.id === currentWindowId);
  const pane = win?.panes.find(p => p.id === currentPane);
  _pendingRename = { type: 'rename_pane', id: currentPane };
  ctxRenameLabel.textContent = `Rename pane P${pane?.index ?? '?'}:`;
  ctxRenameInput.value = getPaneName(currentPane, pane?.command ?? '');
  ctxMain.style.display = 'none';
  ctxRenameForm.style.display = '';
  setTimeout(() => { ctxRenameInput.focus(); ctxRenameInput.select(); }, 80);
});

ctxRenameWindow.addEventListener('click', () => {
  const win = sessions.flatMap(s => s.windows).find(w => w.id === currentWindowId);
  _pendingRename = { type: 'rename_window', id: currentWindowId };
  ctxRenameLabel.textContent = `Rename window W${win?.index ?? '?'}:`;
  ctxRenameInput.value = win?.name ?? '';
  ctxMain.style.display = 'none';
  ctxRenameForm.style.display = '';
  setTimeout(() => { ctxRenameInput.focus(); ctxRenameInput.select(); }, 80);
});

ctxRenameSession.addEventListener('click', () => {
  const sess = sessions.find(s => s.id === currentSessionId);
  const sNum = currentSessionId?.replace('$', '');
  _pendingRename = { type: 'rename_session', id: currentSessionId };
  ctxRenameLabel.textContent = `Rename session S${sNum}:`;
  ctxRenameInput.value = sess?.name ?? '';
  ctxMain.style.display = 'none';
  ctxRenameForm.style.display = '';
  setTimeout(() => { ctxRenameInput.focus(); ctxRenameInput.select(); }, 80);
});

function submitRename() {
  if (!_pendingRename) return;
  const { type, id } = _pendingRename;
  const name = ctxRenameInput.value.trim();
  if (type === 'rename_pane') {
    // Stored locally only — panes have no native tmux name.
    // Empty name clears the override and reverts to auto-detected name.
    setPaneName(id, name);
    hideCtxMenu();
    rebuildPaneTree();
  } else {
    if (!name) { ctxRenameInput.focus(); return; }
    if (type === 'rename_window')       send({ type, window_id:  id, name });
    else if (type === 'rename_session') send({ type, session_id: id, name });
    hideCtxMenu();
  }
}

document.getElementById('ctx-rename-confirm').addEventListener('click', submitRename);
ctxRenameInput.addEventListener('keydown', e => { if (e.key === 'Enter') submitRename(); });
document.getElementById('ctx-rename-back').addEventListener('click', () => {
  ctxRenameForm.style.display = 'none';
  ctxMain.style.display = '';
  _pendingRename = null;
});

// ── create overlay (+ button) ─────────────────────────────────────────────
let _createType = null; // 'pane' | 'window' | 'session'

function showCreateOverlay() {
  createMain.style.display = '';
  createNameForm.style.display = 'none';
  createNameInput.value = '';
  createOverlay.style.display = 'flex';
}

function hideCreateOverlay() { createOverlay.style.display = 'none'; _createType = null; }

function showCreateNameForm(type) {
  _createType = type;
  createNameLabel.textContent = type === 'session'
    ? 'New session name:'
    : `New ${type} name (optional):`;
  createNameInput.placeholder = type === 'session' ? 'e.g. work' : 'optional';
  createMain.style.display = 'none';
  createNameForm.style.display = '';
  setTimeout(() => createNameInput.focus(), 80);
}

btnNew.addEventListener('click', showCreateOverlay);
createOverlay.addEventListener('click', e => { if (e.target === createOverlay) hideCreateOverlay(); });

document.getElementById('create-cancel').addEventListener('click', hideCreateOverlay);
document.getElementById('create-name-back').addEventListener('click', () => {
  createNameForm.style.display = 'none';
  createMain.style.display = '';
  _createType = null;
});

document.getElementById('create-pane').addEventListener('click', () => {
  if (currentWindowId) send({ type: 'new_pane', window_id: currentWindowId });
  hideCreateOverlay();
});

document.getElementById('create-window').addEventListener('click', () => showCreateNameForm('window'));
document.getElementById('create-session').addEventListener('click', () => showCreateNameForm('session'));

function submitCreate() {
  const name = createNameInput.value.trim();
  if (_createType === 'session') {
    if (!name) { createNameInput.focus(); return; }
    send({ type: 'new_session', name });
  } else if (_createType === 'window') {
    if (currentSessionId) send({ type: 'new_window', session_id: currentSessionId, name });
  }
  hideCreateOverlay();
}

document.getElementById('create-name-confirm').addEventListener('click', submitCreate);
createNameInput.addEventListener('keydown', e => { if (e.key === 'Enter') submitCreate(); });

// ── status bar clock (only ticks when connected — frozen clock = disconnected) ─
let _clockInterval = null;
let _serverOffset = 0;  // ms: server clock minus browser clock
let _serverTz = 'UTC';  // IANA timezone of the host machine

function startClock() {
  if (_clockInterval) return;
  function tick() {
    const now = new Date(Date.now() + _serverOffset);
    const opts = { timeZone: _serverTz };
    const date = new Intl.DateTimeFormat('en-US', {...opts, month:'short', day:'numeric'}).format(now);
    const time = new Intl.DateTimeFormat('en-US', {...opts, hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false}).format(now);
    headerClock.textContent = `${date} ${time}`;
  }
  tick();
  _clockInterval = setInterval(tick, 1000);
}

function stopClock() {
  if (_clockInterval) { clearInterval(_clockInterval); _clockInterval = null; }
}

// ── byobu status line ─────────────────────────────────────────────────────
function makeChip(c) {
  const el = document.createElement('span');
  el.className = 'chip';
  el.textContent = c.text;
  el.title = c.label;
  el.style.background = c.bg;
  el.style.color = c.color;
  return el;
}

async function fetchByobuStatus() {
  try {
    const data = await fetch('/status').then(r => r.json());
    const left  = data.left  || [];
    const right = data.right || [];
    statuslineLeft.innerHTML  = '';
    statuslineRight.innerHTML = '';
    left.forEach(c  => statuslineLeft.appendChild(makeChip(c)));
    right.forEach(c => statuslineRight.appendChild(makeChip(c)));
  } catch { /* byobu not running */ }
}

function startStatusPolling() {
  if (!statusInterval) {
    fetchByobuStatus();
    statusInterval = setInterval(fetchByobuStatus, 10000);
  }
}

// ── pairing screen ─────────────────────────────────────────────────────────
function showPairScreen() {
  pairOverlay.style.display = 'flex';
  pairCodeInput.value = '';
  pairError.textContent = '';
  if (statusInterval) { clearInterval(statusInterval); statusInterval = null; }
  const autoCode = new URLSearchParams(window.location.search).get('pair');
  if (autoCode && /^\d{6}$/.test(autoCode)) {
    pairCodeInput.value = `${autoCode.slice(0,3)}-${autoCode.slice(3)}`;
    setTimeout(submitPair, 400);
  } else {
    setTimeout(() => pairCodeInput.focus(), 80);
  }
}

function hidePairScreen() {
  pairOverlay.style.display = 'none';
  const url = new URL(window.location);
  if (url.searchParams.has('pair')) {
    url.searchParams.delete('pair');
    history.replaceState(null, '', url);
  }
}

pairCodeInput.addEventListener('input', () => {
  let digits = pairCodeInput.value.replace(/\D/g, '').slice(0, 6);
  pairCodeInput.value = digits.length > 3 ? `${digits.slice(0,3)}-${digits.slice(3)}` : digits;
});

pairCodeInput.addEventListener('keydown', e => { if (e.key === 'Enter') submitPair(); });
pairBtn.addEventListener('click', submitPair);

async function submitPair() {
  const code = pairCodeInput.value;
  if (!code) return;
  pairBtn.disabled = true;
  pairError.textContent = '';
  try {
    const r = await fetch('/pair', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ code }),
    });
    const data = await r.json();
    if (r.ok) {
      hidePairScreen();
      applyHostname();
      connect();
      startStatusPolling();
    } else {
      pairError.textContent = data.error ?? 'Pairing failed.';
      pairCodeInput.value = '';
      pairCodeInput.focus();
    }
  } catch {
    pairError.textContent = 'Network error — is the daemon running?';
  } finally {
    pairBtn.disabled = false;
  }
}

// ── PWA install prompt ────────────────────────────────────────────────────
const isIOS        = /iPad|iPhone|iPod/.test(navigator.userAgent);
const isStandalone = window.matchMedia('(display-mode: standalone)').matches
                     || navigator.standalone === true;
let deferredInstallPrompt = null;

if (!isStandalone) {
  if (isIOS) {
    // iOS Safari: no beforeinstallprompt — show button that explains manual steps.
    btnInstall.style.display = '';
    btnInstall.addEventListener('click', () => {
      iosInstallTip.style.display = '';
    });
    document.getElementById('close-tip').addEventListener('click', () => {
      iosInstallTip.style.display = 'none';
    });
  } else {
    // Android/Chrome: capture the prompt and fire it on button click.
    window.addEventListener('beforeinstallprompt', e => {
      e.preventDefault();
      deferredInstallPrompt = e;
      btnInstall.style.display = '';
    });
    btnInstall.addEventListener('click', async () => {
      if (!deferredInstallPrompt) return;
      deferredInstallPrompt.prompt();
      const { outcome } = await deferredInstallPrompt.userChoice;
      deferredInstallPrompt = null;
      btnInstall.style.display = 'none';
    });
    window.addEventListener('appinstalled', () => {
      btnInstall.style.display = 'none';
      deferredInstallPrompt = null;
    });
  }
}

// ── machine selector ──────────────────────────────────────────────────────
async function loadMachines() {
  try {
    const machines = await fetch('/machines').then(r => r.json());
    if (!Array.isArray(machines) || machines.length < 2) return;
    machineSelect.innerHTML = '';
    machines.forEach(m => {
      const o = document.createElement('option');
      o.value = m.url;
      o.textContent = m.current ? m.name + ' ✓' : m.name;
      if (m.current) o.selected = true;
      machineSelect.appendChild(o);
    });
    machineSelect.style.display = '';
  } catch { /* /machines not configured — selector stays hidden */ }
}

machineSelect.addEventListener('change', () => {
  const url = machineSelect.value;
  if (url && /^https?:\/\//.test(url)) window.location.href = url;
});

async function applyHostname() {
  try {
    const data = await fetch('/ping').then(r => r.json());
    if (data.hostname) hostnameDisplay.textContent = data.hostname;
  } catch { /* ignore */ }
}

// ── init: check auth, then connect or show pair screen ────────────────────
async function init() {
  setStatus('connecting…', 'connecting');
  try {
    const r = await fetch('/ping');
    const data = await r.json();
    if (r.ok) {
      if (data.hostname) hostnameDisplay.textContent = data.hostname;
      hidePairScreen();
      connect();
      startStatusPolling();
      loadMachines();
    } else {
      showPairScreen();
    }
  } catch {
    showPairScreen();
  }
}
init();

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js');
}
