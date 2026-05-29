'use strict';

// ── state ──────────────────────────────────────────────────────────────────
let ws = null;
let sessions = [];
let currentPane = null;
let currentSessionId = null;
let currentWindowId = null;
let forcedSessionId = null; // set after creating a new session
let forcedPaneId = null;    // set after creating a new window (specific pane to navigate to)
let _scrollTopOnNextSnapshot = false; // scroll to top instead of bottom on next snapshot
let statusInterval = null;

// ── biometric lock ─────────────────────────────────────────────────────────
// Uses WebAuthn platform authenticator (fingerprint/face/PIN) as an
// idle/background lock — not a replacement for server-side auth.

const LOCK_IDLE_MS   = 5 * 60 * 1000;  // lock after 5 min inactivity
const LOCK_HIDDEN_MS = 30 * 1000;       // lock if backgrounded > 30s

let _lockEnabled = localStorage.getItem('lock-enabled');  // 'true'/'false'/null
let _lockCredId  = localStorage.getItem('lock-cred-id');  // base64url
let _lockTimer   = null;
let _hiddenAt    = 0;
let _isLocked    = false;
let _skipThisSession = false;

function _b64uEncode(buf) {
  return btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
}
function _b64uDecode(s) {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  const pad = s.length % 4;
  if (pad) s += '='.repeat(4 - pad);
  return Uint8Array.from(atob(s), c => c.charCodeAt(0));
}

function _webauthnAvailable() {
  return window.isSecureContext && typeof PublicKeyCredential !== 'undefined';
}

function resetLockTimer() {
  clearTimeout(_lockTimer);
  if (_lockEnabled === 'true' && _lockCredId) {
    _lockTimer = setTimeout(lockApp, LOCK_IDLE_MS);
  }
}

async function _registerCredential() {
  const cred = await navigator.credentials.create({
    publicKey: {
      challenge: crypto.getRandomValues(new Uint8Array(32)),
      rp: { id: location.hostname, name: 'Trustmux' },
      user: {
        id: crypto.getRandomValues(new Uint8Array(16)),
        name: 'user',
        displayName: 'Trustmux User',
      },
      pubKeyCredParams: [
        { type: 'public-key', alg: -7 },
        { type: 'public-key', alg: -257 },
      ],
      authenticatorSelection: {
        authenticatorAttachment: 'platform',
        userVerification: 'required',
        residentKey: 'discouraged',
      },
      timeout: 60000,
    },
  });
  return _b64uEncode(cred.rawId);
}

async function _verifyCredential() {
  await navigator.credentials.get({
    publicKey: {
      challenge: crypto.getRandomValues(new Uint8Array(32)),
      rpId: location.hostname,
      allowCredentials: [{ id: _b64uDecode(_lockCredId), type: 'public-key' }],
      userVerification: 'required',
      timeout: 60000,
    },
  });
}

function lockApp() {
  if (_isLocked) return;
  _isLocked = true;
  clearTimeout(_lockTimer);
  document.getElementById('lock-overlay').style.display = 'flex';
}

function unlockApp() {
  _isLocked = false;
  document.getElementById('lock-overlay').style.display = 'none';
  resetLockTimer();
}

function disableLock() {
  _lockEnabled = 'false';
  _lockCredId  = null;
  localStorage.setItem('lock-enabled', 'false');
  localStorage.removeItem('lock-cred-id');
  clearTimeout(_lockTimer);
  unlockApp();
}

function maybeOfferBiometric() {
  if (!_webauthnAvailable()) return;
  if (_lockEnabled !== null) return;
  if (_skipThisSession) return;
  document.getElementById('bio-setup-overlay').style.display = 'flex';
}

// Activity events reset the idle lock timer
['touchstart', 'keydown', 'mousedown'].forEach(ev =>
  document.addEventListener(ev, () => {
    if (_lockEnabled === 'true' && !_isLocked) resetLockTimer();
  }, { passive: true })
);

// Lock when app returns from background (if gone > LOCK_HIDDEN_MS)
document.addEventListener('visibilitychange', () => {
  if (_lockEnabled !== 'true') return;
  if (document.hidden) {
    _hiddenAt = Date.now();
  } else {
    if (_hiddenAt && Date.now() - _hiddenAt >= LOCK_HIDDEN_MS) lockApp();
    _hiddenAt = 0;
  }
});

// ── DOM refs ───────────────────────────────────────────────────────────────
const pairOverlay   = document.getElementById('pair-overlay');
const pairCodeInput = document.getElementById('pair-code');
const pairBtn       = document.getElementById('pair-btn');
const pairError     = document.getElementById('pair-error');
const xyzLabel      = document.getElementById('xyz-label');
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
const ctxRenameLabel   = document.getElementById('ctx-rename-label');
const ctxRenameInput   = document.getElementById('ctx-rename-input');
const ctxCancel        = document.getElementById('ctx-cancel');
const ctxName          = document.getElementById('ctx-name');
const createOverlay    = document.getElementById('create-overlay');
const createMain       = document.getElementById('create-main');
const createNameForm   = document.getElementById('create-name-form');
const createNameLabel  = document.getElementById('create-name-label');
const createNameInput  = document.getElementById('create-name-input');
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
      if (msg.new_pane) forcedPaneId = msg.new_pane;
      rebuildPaneTree();
    } else if (msg.type === 'snapshot') {
      if (msg.pane_id === currentPane) {
        const scrollTop = _scrollTopOnNextSnapshot;
        if (scrollTop) _scrollTopOnNextSnapshot = false;
        renderOutput(msg.data, !scrollTop);
        if (scrollTop) output.scrollTop = 0;
      }
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
  const list = flatPaneList();
  if (!currentPane || list.length === 0) return '-/-';
  // Match by pane first, then by window (current pane may be a non-representative split pane)
  let idx = list.findIndex(e => e.paneId === currentPane);
  if (idx < 0) idx = list.findIndex(e => e.windowId === currentWindowId);
  return idx < 0 ? '-/-' : `${idx + 1}/${list.length}`;
}

function updateXYZLabel() {
  xyzLabel.textContent = activePaneXYZ();
}

// ── pane navigation ───────────────────────────────────────────────────────
function rebuildPaneTree() {
  const forced     = forcedSessionId;
  const forcedPane = forcedPaneId;
  if (forced)     forcedSessionId = null;
  if (forcedPane) forcedPaneId    = null;

  let autoFromForced     = null;
  let autoFromForcedPane = null;
  let autoFirst          = null;
  let prevTarget         = null;

  for (const s of sessions) {
    for (const w of (s.windows || [])) {
      for (const p of (w.panes || [])) {
        if (!p.dead) {
          const entry = { sessionId: s.id, windowId: w.id, paneId: p.id };
          if (forced === s.id && !autoFromForced) autoFromForced = entry;
          if (forcedPane === p.id) autoFromForcedPane = entry;
          if (!autoFirst) autoFirst = entry;
          if (p.id === currentPane) prevTarget = entry;
        }
      }
    }
  }

  // Prefer exact forced pane (new window), then current pane, then forced session, then first
  const target = autoFromForcedPane ?? prevTarget ?? autoFromForced ?? autoFirst ?? null;

  // Scroll to top when navigating to a freshly created window/session
  if (autoFromForcedPane && autoFromForcedPane !== prevTarget) _scrollTopOnNextSnapshot = true;

  if (!target) {
    currentSessionId = null;
    currentWindowId  = null;
    currentPane      = null;
    cmdInput.disabled = true;
    btnSend.disabled  = true;
    updateXYZLabel();
    updateContextName();
    return;
  }

  if (target.paneId !== currentPane) {
    navigateTo(target.sessionId, target.windowId, target.paneId);
  } else {
    updateXYZLabel();
    updateContextName();
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
  updateContextName();
}

// ── context name (custom label or command fallback) ───────────────────────
function updateContextName() {
  if (!currentPane) { ctxName.textContent = ''; return; }
  const custom = getPaneName(currentPane, '');
  ctxName.textContent = custom || currentPaneCommand() || 'shell';
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
xyzLabel.addEventListener('click', () => send({ type: 'list_sessions' }));
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

// ── pane list — one context per window (active/first live pane) ──────────
function flatPaneList() {
  const list = [];
  for (const s of sessions) {
    for (const w of (s.windows || [])) {
      const live = (w.panes || []).filter(p => !p.dead);
      if (!live.length) continue;
      const rep = live.find(p => p.active) ?? live[0];
      list.push({ sessionId: s.id, windowId: w.id, paneId: rep.id });
    }
  }
  return list;
}

function navigateRelative(delta) {
  const list = flatPaneList();
  if (list.length < 2) return;
  let idx = list.findIndex(e => e.paneId === currentPane);
  if (idx < 0) idx = list.findIndex(e => e.windowId === currentWindowId);
  const next = list[((idx < 0 ? 0 : idx) + delta + list.length) % list.length];
  navigateTo(next.sessionId, next.windowId, next.paneId);
}

// ── touch handler: double-tap → new window ────────────────────────────────
let _touchX = 0, _touchY = 0;
let _lastTap = 0;
let _touchDoubleTapFired = false; // suppress synthetic dblclick after touch double-tap

output.addEventListener('touchstart', e => {
  _touchX = e.touches[0].clientX;
  _touchY = e.touches[0].clientY;
}, { passive: true });

output.addEventListener('touchend', e => {
  const dx = e.changedTouches[0].clientX - _touchX;
  const dy = e.changedTouches[0].clientY - _touchY;
  if (Math.abs(dx) < 20 && Math.abs(dy) < 20) {
    const now = Date.now();
    if (now - _lastTap < 300) {
      // Mark so the synthetic dblclick that fires after touch doesn't duplicate this
      _touchDoubleTapFired = true;
      setTimeout(() => { _touchDoubleTapFired = false; }, 600);
      if (currentSessionId) send({ type: 'new_window', session_id: currentSessionId });
      _lastTap = 0;
    } else { _lastTap = now; }
  }
}, { passive: true });

output.addEventListener('dblclick', () => {
  if (_touchDoubleTapFired) return; // already handled by touchend, skip synthetic event
  if (currentSessionId) send({ type: 'new_window', session_id: currentSessionId });
});

btnPrev.addEventListener('click', () => navigateRelative(-1));
btnNext.addEventListener('click', () => navigateRelative(1));

// ── rename overlay (tap name label in header) ─────────────────────────────
let _pendingRenameId = null;

function currentPaneCommand() {
  if (!currentPane) return '';
  for (const s of sessions) {
    for (const w of (s.windows || [])) {
      for (const p of (w.panes || [])) {
        if (p.id === currentPane) return p.command || '';
      }
    }
  }
  return '';
}

function showRenameOverlay() {
  if (!currentPane) return;
  _pendingRenameId = currentPane;
  const custom = getPaneName(currentPane, '');
  const cmd = currentPaneCommand();
  ctxRenameLabel.textContent = custom
    ? `Rename "${custom}":`
    : `Name this context (${cmd || 'shell'}):`;
  ctxRenameInput.value = custom;
  ctxOverlay.style.display = 'flex';
  setTimeout(() => { ctxRenameInput.focus(); ctxRenameInput.select(); }, 80);
}

function hideRenameOverlay() {
  ctxOverlay.style.display = 'none';
  _pendingRenameId = null;
}

ctxName.addEventListener('click', showRenameOverlay);
ctxCancel.addEventListener('click', hideRenameOverlay);
ctxOverlay.addEventListener('click', e => { if (e.target === ctxOverlay) hideRenameOverlay(); });

function submitRename() {
  if (!_pendingRenameId) return;
  const name = ctxRenameInput.value.trim();
  setPaneName(_pendingRenameId, name);
  hideRenameOverlay();
  updateContextName();
}

document.getElementById('ctx-rename-confirm').addEventListener('click', submitRename);
ctxRenameInput.addEventListener('keydown', e => { if (e.key === 'Enter') submitRename(); });

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
      maybeOfferBiometric();
      resetLockTimer();
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

// ── biometric button wiring ───────────────────────────────────────────────

document.getElementById('bio-enable-btn').addEventListener('click', async () => {
  const btn = document.getElementById('bio-enable-btn');
  btn.disabled = true;
  try {
    const credId = await _registerCredential();
    _lockCredId  = credId;
    _lockEnabled = 'true';
    localStorage.setItem('lock-enabled', 'true');
    localStorage.setItem('lock-cred-id', credId);
    document.getElementById('bio-setup-overlay').style.display = 'none';
    resetLockTimer();
  } catch {
    btn.disabled = false;
  }
});

document.getElementById('bio-skip-btn').addEventListener('click', () => {
  _skipThisSession = true;
  document.getElementById('bio-setup-overlay').style.display = 'none';
});

document.getElementById('bio-never-btn').addEventListener('click', () => {
  _lockEnabled = 'false';
  localStorage.setItem('lock-enabled', 'false');
  document.getElementById('bio-setup-overlay').style.display = 'none';
});

document.getElementById('lock-unlock-btn').addEventListener('click', async () => {
  const btn = document.getElementById('lock-unlock-btn');
  btn.disabled = true;
  try {
    await _verifyCredential();
    unlockApp();
  } catch {
    // stay locked — user dismissed or biometric failed
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('lock-disable-btn').addEventListener('click', disableLock);

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
      if (_lockEnabled === 'true' && _lockCredId) {
        lockApp();
      } else {
        maybeOfferBiometric();
        resetLockTimer();
      }
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
