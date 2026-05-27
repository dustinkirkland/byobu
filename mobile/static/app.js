'use strict';

// ── state ──────────────────────────────────────────────────────────────────
let ws = null;
let sessions = [];
let currentPane = null;
let forcedSessionId = null; // set after creating a new session
let statusInterval = null;

// ── DOM refs ───────────────────────────────────────────────────────────────
const pairOverlay   = document.getElementById('pair-overlay');
const pairCodeInput = document.getElementById('pair-code');
const pairBtn       = document.getElementById('pair-btn');
const pairError     = document.getElementById('pair-error');
const selSession    = document.getElementById('sel-session');
const selWindow     = document.getElementById('sel-window');
const selPane       = document.getElementById('sel-pane');
const btnRefresh    = document.getElementById('btn-refresh');
const btnNewSession = document.getElementById('btn-new-session');
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
const ctxOverlay    = document.getElementById('ctx-overlay');
const ctxMain       = document.getElementById('ctx-main');
const ctxConfirm    = document.getElementById('ctx-confirm');
const ctxConfirmMsg = document.getElementById('ctx-confirm-msg');
const ctxKill       = document.getElementById('ctx-kill');
const ctxNewPane    = document.getElementById('ctx-new-pane');
const ctxNewWindow  = document.getElementById('ctx-new-window');
const ctxNewSession = document.getElementById('ctx-new-session');
const ctxCancel     = document.getElementById('ctx-cancel');
const ctxConfirmYes = document.getElementById('ctx-confirm-yes');
const ctxConfirmNo  = document.getElementById('ctx-confirm-no');

// ── status ─────────────────────────────────────────────────────────────────
function setStatus(msg, cls) {
  statusText.textContent = msg;
  statusbar.className = cls || '';
}

// ── WebSocket ──────────────────────────────────────────────────────────────
function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen  = () => { setStatus('connected', 'connected'); startClock(); };
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

    if (msg.type === 'sessions') {
      sessions = msg.data || [];
      if (msg.new_session) forcedSessionId = msg.new_session;
      rebuildSessionList();
    } else if (msg.type === 'snapshot') {
      renderOutput(msg.data, /*scroll=*/true);
    } else if (msg.type === 'update') {
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

// ── session / window / pane pickers ───────────────────────────────────────

// Pick the most relevant session: attached > single > first.
function bestSessionId() {
  if (!sessions.length) return null;
  return (sessions.find(s => s.attached) ?? sessions[0]).id;
}

function rebuildSessionList() {
  const prev = selSession.value;
  selSession.innerHTML = '<option value="">Session…</option>';
  sessions.forEach(s => {
    const o = document.createElement('option');
    o.value = s.id;
    o.textContent = s.attached ? `S: ● ${s.name}` : `S: ${s.name}`;
    selSession.appendChild(o);
  });

  // Forced selection (after creating a new session) takes priority.
  if (forcedSessionId && sessions.find(s => s.id === forcedSessionId)) {
    selSession.value = forcedSessionId;
    forcedSessionId = null;
    onSessionChange(/*auto=*/true);
  } else if (prev && sessions.find(s => s.id === prev)) {
    selSession.value = prev;
    onSessionChange(/*auto=*/true); // always auto so new windows/panes get selected
  } else {
    selSession.value = bestSessionId() ?? '';
    onSessionChange(/*auto=*/true);
  }
}

function onSessionChange(auto = false) {
  const sid = selSession.value;
  selWindow.innerHTML = '<option value="">Window…</option>';
  selWindow.disabled     = !sid;
  btnNewWindow.disabled  = !sid;
  selPane.innerHTML    = '<option value="">Pane…</option>';
  selPane.disabled       = true;
  btnNewPane.disabled    = true;

  const sess = sessions.find(s => s.id === sid);
  if (!sess) return;

  let activeWid = null;
  sess.windows.forEach(w => {
    const o = document.createElement('option');
    o.value = w.id;
    o.textContent = `W: ${w.index}: ${w.name}`;
    if (w.active) { o.textContent += ' ●'; activeWid = w.id; }
    selWindow.appendChild(o);
  });

  if (auto) {
    selWindow.value = activeWid ?? (sess.windows.length === 1 ? sess.windows[0].id : '');
  }
  onWindowChange(auto);
}

function onWindowChange(auto = false) {
  const sid = selSession.value;
  const wid = selWindow.value;
  selPane.innerHTML  = '<option value="">Pane…</option>';
  selPane.disabled      = !wid;
  btnNewPane.disabled   = !wid;

  const sess = sessions.find(s => s.id === sid);
  const win  = sess?.windows.find(w => w.id === wid);
  if (!win) return;

  let activePid = null;
  win.panes.forEach(p => {
    const o = document.createElement('option');
    o.value = p.id;
    o.textContent = `P: ${p.index}: ${p.command}`;
    if (p.active) { o.textContent += ' ●'; activePid = p.id; }
    selPane.appendChild(o);
  });

  if (auto) {
    selPane.value = activePid ?? (win.panes.length === 1 ? win.panes[0].id : '');
  }
  onPaneChange();
}

function onPaneChange() {
  const pid = selPane.value;
  cmdInput.disabled = !pid;
  btnSend.disabled  = !pid;
  if (!pid) return;

  currentPane = pid;
  output.className = '';
  output.textContent = 'loading…';
  send({ type: 'subscribe', pane_id: pid, lines: 300 });
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
selSession.addEventListener('change', () => onSessionChange(true));
selWindow.addEventListener('change',  () => onWindowChange(true));
selPane.addEventListener('change',    onPaneChange);
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
    btnKbdMode.textContent = 'Aa';
    btnKbdMode.title = 'Text mode — tap for terminal mode';
    btnKbdMode.style.color = 'var(--accent)';
  } else {
    cmdInput.setAttribute('spellcheck', 'false');
    cmdInput.setAttribute('autocorrect', 'off');
    cmdInput.setAttribute('autocapitalize', 'none');
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

// ── swipe navigation (depth-first: panes → windows → sessions) ───────────
function flatPaneList() {
  const list = [];
  for (const s of sessions) {
    for (const w of (s.windows || [])) {
      for (const p of (w.panes || [])) {
        list.push({ sessionId: s.id, windowId: w.id, paneId: p.id });
      }
    }
  }
  return list;
}

function navigateTo(sessionId, windowId, paneId) {
  selSession.value = sessionId;
  onSessionChange(false);   // rebuilds window options
  selWindow.value = windowId;
  onWindowChange(false);    // rebuilds pane options
  selPane.value = paneId;
  onPaneChange();            // subscribes
}

function navigateRelative(delta) {
  const list = flatPaneList();
  if (list.length < 2) return;
  const idx = list.findIndex(e => e.paneId === currentPane);
  const next = list[((idx < 0 ? 0 : idx) + delta + list.length) % list.length];
  navigateTo(next.sessionId, next.windowId, next.paneId);
}

// ── unified touch handler: long press (context menu) + swipe (navigation) ─
let _touchX = 0, _touchY = 0;
let _longPressTimer = null, _longPressTriggered = false;

output.addEventListener('touchstart', e => {
  _touchX = e.touches[0].clientX;
  _touchY = e.touches[0].clientY;
  _longPressTriggered = false;
  _longPressTimer = setTimeout(() => {
    _longPressTimer = null;
    _longPressTriggered = true;
    showCtxMenu();
  }, 500);
}, { passive: true });

output.addEventListener('touchmove', e => {
  if (_longPressTimer) {
    const dx = Math.abs(e.touches[0].clientX - _touchX);
    const dy = Math.abs(e.touches[0].clientY - _touchY);
    if (dx > 10 || dy > 10) { clearTimeout(_longPressTimer); _longPressTimer = null; }
  }
}, { passive: true });

output.addEventListener('touchend', e => {
  if (_longPressTimer) { clearTimeout(_longPressTimer); _longPressTimer = null; }
  if (_longPressTriggered) return;
  const dx = e.changedTouches[0].clientX - _touchX;
  const dy = e.changedTouches[0].clientY - _touchY;
  if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy) * 2) return;
  navigateRelative(dx < 0 ? 1 : -1);
}, { passive: true });

// ── context menu ──────────────────────────────────────────────────────────
function showCtxMenu() {
  const sess = sessions.find(s => s.id === selSession.value);
  const win  = sess?.windows.find(w => w.id === selWindow.value);
  const isSingle = (win?.panes.length ?? 0) === 1;
  ctxKill.textContent = isSingle ? '✕  Kill window' : '✕  Kill pane';
  ctxKill.style.display = currentPane ? '' : 'none';
  ctxMain.style.display = '';
  ctxConfirm.style.display = 'none';
  ctxOverlay.style.display = 'flex';
}

function hideCtxMenu() { ctxOverlay.style.display = 'none'; }

ctxCancel.addEventListener('click', hideCtxMenu);
ctxOverlay.addEventListener('click', e => { if (e.target === ctxOverlay) hideCtxMenu(); });

ctxKill.addEventListener('click', () => {
  const sess = sessions.find(s => s.id === selSession.value);
  const win  = sess?.windows.find(w => w.id === selWindow.value);
  const isSingle = (win?.panes.length ?? 0) === 1;
  const pane = win?.panes.find(p => p.id === currentPane);
  const label = isSingle
    ? `window "${win?.name ?? selWindow.value}"`
    : `pane ${pane?.command ?? currentPane}`;
  ctxConfirmMsg.textContent = `Kill ${label}?`;
  ctxMain.style.display = 'none';
  ctxConfirm.style.display = '';
});

ctxConfirmYes.addEventListener('click', () => {
  const sess = sessions.find(s => s.id === selSession.value);
  const win  = sess?.windows.find(w => w.id === selWindow.value);
  const isSingle = (win?.panes.length ?? 0) === 1;
  if (isSingle) {
    send({ type: 'kill_window', window_id: selWindow.value });
  } else {
    send({ type: 'kill_pane', pane_id: currentPane });
  }
  hideCtxMenu();
});

ctxConfirmNo.addEventListener('click', () => {
  ctxConfirm.style.display = 'none';
  ctxMain.style.display = '';
});

ctxNewPane.addEventListener('click', () => {
  const wid = selWindow.value;
  if (wid) send({ type: 'new_pane', window_id: wid });
  hideCtxMenu();
});

ctxNewWindow.addEventListener('click', () => {
  hideCtxMenu();
  const sid = selSession.value;
  if (!sid) return;
  const name = window.prompt('New window name (optional):') ?? '';
  send({ type: 'new_window', session_id: sid, name: name.trim() });
});

ctxNewSession.addEventListener('click', () => {
  hideCtxMenu();
  const name = window.prompt('New session name:');
  if (!name?.trim()) return;
  send({ type: 'new_session', name: name.trim() });
});

// ── status bar clock (only ticks when connected — frozen clock = disconnected) ─
let _clockInterval = null;

function startClock() {
  if (_clockInterval) return;
  function tick() {
    const now = new Date();
    const date = now.toLocaleDateString('en-US', {month:'short', day:'numeric'});
    const time = now.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false});
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
