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

// ── pane snapshot cache (in-memory only — never persisted) ────────────────
// Stores the last-rendered HTML + scroll position for each pane so that
// switching back to a pane is instant while the fresh snapshot loads.
const _paneCache = new Map();
const _PANE_CACHE_MAX = 50;

// ── offline / connectivity helpers ────────────────────────────────────────
let _offlineCountdownTimer = null;
let _offlineRetryTimer = null;
const OFFLINE_RETRY_SECS = 8; // auto-retry interval while offline screen is shown

/** True if the current host looks like a Tailscale address. */
function isTailscaleHost() {
  const h = location.hostname;
  // Tailscale CGNAT range: 100.64.0.0/10  →  100.64.x.x – 100.127.x.x
  const m = h.match(/^100\.(\d+)\./);
  if (m && +m[1] >= 64 && +m[1] <= 127) return true;
  // MagicDNS: *.ts.net or *.taile*.net
  if (/\.ts\.net$/.test(h) || /\.taile[a-z0-9-]*\.net$/.test(h)) return true;
  return false;
}

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
const btnEscape        = document.getElementById('btn-escape');
const escapePopup      = document.getElementById('escape-popup');

// offline overlay elements
const offlineOverlay       = document.getElementById('offline-overlay');
const offlineHost          = document.getElementById('offline-host');
const offlineTailscaleHint = document.getElementById('offline-tailscale-hint');
const offlineRetryBtn      = document.getElementById('offline-retry-btn');
const offlineCountdown     = document.getElementById('offline-countdown');

function showOfflineScreen() {
  offlineHost.textContent = location.host;
  if (isTailscaleHost()) {
    offlineTailscaleHint.classList.add('visible');
  } else {
    offlineTailscaleHint.classList.remove('visible');
  }
  offlineOverlay.classList.add('visible');
  _startOfflineCountdown();
}

function hideOfflineScreen() {
  offlineOverlay.classList.remove('visible');
  _clearOfflineTimers();
}

function _clearOfflineTimers() {
  if (_offlineCountdownTimer) { clearInterval(_offlineCountdownTimer); _offlineCountdownTimer = null; }
  if (_offlineRetryTimer)     { clearTimeout(_offlineRetryTimer);      _offlineRetryTimer = null; }
  offlineCountdown.textContent = '';
}

function _startOfflineCountdown() {
  _clearOfflineTimers();
  let secs = OFFLINE_RETRY_SECS;
  offlineCountdown.textContent = `Auto-retry in ${secs}s`;
  _offlineCountdownTimer = setInterval(() => {
    secs--;
    if (secs > 0) {
      offlineCountdown.textContent = `Auto-retry in ${secs}s`;
    } else {
      clearInterval(_offlineCountdownTimer);
      _offlineCountdownTimer = null;
      offlineCountdown.textContent = 'Retrying…';
    }
  }, 1000);
  _offlineRetryTimer = setTimeout(() => {
    offlineCountdown.textContent = 'Retrying…';
    init();
  }, OFFLINE_RETRY_SECS * 1000);
}

offlineRetryBtn.addEventListener('click', () => {
  offlineCountdown.textContent = 'Retrying…';
  _clearOfflineTimers();
  init();
});

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
    if (currentPane) send({ type: 'subscribe', pane_id: currentPane, lines: 300, ansi: true });
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
  const idx = list.findIndex(e => e.paneId === currentPane);
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
  // Save departing pane's rendered content + scroll position.
  if (currentPane && output.innerHTML) {
    _paneCache.set(currentPane, { html: output.innerHTML, scrollTop: output.scrollTop });
    if (_paneCache.size > _PANE_CACHE_MAX) _paneCache.delete(_paneCache.keys().next().value);
  }

  currentSessionId = sessionId;
  currentWindowId  = windowId;
  currentPane      = paneId;
  cmdInput.disabled = false;
  btnSend.disabled  = false;
  output.className  = '';

  const cached = _paneCache.get(paneId);
  if (cached) {
    output.innerHTML  = cached.html;
    output.scrollTop  = cached.scrollTop;
  } else {
    output.textContent = 'loading…';
  }

  send({ type: 'subscribe', pane_id: paneId, lines: 300, ansi: true });
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
function scrollOutputToBottom() {
  requestAnimationFrame(() => { output.scrollTop = output.scrollHeight; });
}

// Convert ANSI SGR escape codes to HTML spans.
// Handles: 16/256/truecolor fg+bg, bold, italic, underline. Other sequences discarded.
function ansiToHtml(text) {
  const C16 = [
    '#1e1e1e','#cc0000','#4e9a06','#c4a000','#3465a4','#75507b','#06989a','#d3d7cf',
    '#555753','#ef2929','#8ae234','#fce94f','#729fcf','#ad7fa8','#34e2e2','#eeeeec',
  ];
  function c256(n) {
    if (n < 16) return C16[n];
    if (n < 232) {
      const i = n - 16, lv = [0, 95, 135, 175, 215, 255];
      return `rgb(${lv[~~(i/36)]},${lv[~~((i%36)/6)]},${lv[i%6]})`;
    }
    const v = 8 + (n - 232) * 10;
    return `rgb(${v},${v},${v})`;
  }
  function _rgb(r, g, b) {
    const ok = v => Number.isInteger(v) && v >= 0 && v <= 255;
    return (ok(r) && ok(g) && ok(b)) ? `rgb(${r},${g},${b})` : null;
  }
  let fg = null, bg = null, bold = false, italic = false, ul = false;
  let spanCss = null, out = '';

  function css() {
    const p = [];
    if (fg) p.push(`color:${fg}`);
    if (bg) p.push(`background:${bg}`);
    if (bold) p.push('font-weight:bold');
    if (italic) p.push('font-style:italic');
    if (ul) p.push('text-decoration:underline');
    return p.join(';');
  }
  function emit(s) {
    if (!s) return;
    const c = css();
    if (c !== spanCss) {
      if (spanCss !== null) out += '</span>';
      if (c) out += `<span style="${c}">`;
      spanCss = c || null;
    }
    out += s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function sgr(ps) {
    let i = 0;
    while (i < ps.length) {
      const p = ps[i];
      if (!p)            { fg = bg = null; bold = italic = ul = false; }
      else if (p === 1)  bold   = true;
      else if (p === 3)  italic = true;
      else if (p === 4)  ul     = true;
      else if (p === 22) bold   = false;
      else if (p === 23) italic = false;
      else if (p === 24) ul     = false;
      else if (p >= 30 && p <= 37) fg = C16[p - 30];
      else if (p === 38 && ps[i+1] === 5) { fg = c256(ps[i+2]); i += 2; }
      else if (p === 38 && ps[i+1] === 2) { fg = _rgb(ps[i+2],ps[i+3],ps[i+4]); i += 4; }
      else if (p === 39) fg = null;
      else if (p >= 40 && p <= 47) bg = C16[p - 40];
      else if (p === 48 && ps[i+1] === 5) { bg = c256(ps[i+2]); i += 2; }
      else if (p === 48 && ps[i+1] === 2) { bg = _rgb(ps[i+2],ps[i+3],ps[i+4]); i += 4; }
      else if (p === 49) bg = null;
      else if (p >= 90  && p <= 97)  fg = C16[p - 82];
      else if (p >= 100 && p <= 107) bg = C16[p - 92];
      i++;
    }
  }
  const TOK = /([^\x1b]+)|\x1b(?:\[([0-9;]*)([A-Za-z])|\][^\x07]*(?:\x07|\x1b\\)|(.))/g;
  for (const m of text.matchAll(TOK)) {
    if (m[1])              emit(m[1]);
    else if (m[3] === 'm') sgr(m[2] ? m[2].split(';').map(Number) : [0]);
  }
  if (spanCss !== null) out += '</span>';
  return out;
}

function renderOutput(text, scrollToBottom) {
  output.className = '';
  output.innerHTML = ansiToHtml(text);
  if (scrollToBottom) scrollOutputToBottom();
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
  scrollOutputToBottom();
}
btnKbdMode.addEventListener('click', () => {
  textMode = !textMode;
  applyKbdMode();
  // blur + refocus so Android keyboard re-evaluates spellcheck state
  cmdInput.blur();
  setTimeout(() => cmdInput.focus(), 50);
});

// ── escape / ctrl-c popup ─────────────────────────────────────────────────
function showEscapePopup() {
  const rect = btnEscape.getBoundingClientRect();
  escapePopup.style.display = 'flex';
  escapePopup.style.right   = (window.innerWidth - rect.right) + 'px';
  escapePopup.style.bottom  = (window.innerHeight - rect.top + 8) + 'px';
}

function hideEscapePopup() {
  escapePopup.style.display = 'none';
}

btnEscape.addEventListener('click', e => {
  e.stopPropagation();
  escapePopup.style.display === 'none' ? showEscapePopup() : hideEscapePopup();
});

document.getElementById('escape-popup-esc').addEventListener('click', () => {
  if (currentPane) send({ type: 'send_keys', pane_id: currentPane, keys: 'Escape', enter: false, literal: false });
  hideEscapePopup();
});

document.getElementById('escape-popup-ctrlc').addEventListener('click', () => {
  if (currentPane) send({ type: 'send_keys', pane_id: currentPane, keys: 'C-c', enter: false, literal: false });
  hideEscapePopup();
});

document.addEventListener('click', () => hideEscapePopup());
document.addEventListener('touchstart', e => {
  if (!escapePopup.contains(e.target) && e.target !== btnEscape) hideEscapePopup();
}, { passive: true });

// ── pane list — all live panes across all windows and sessions ────────────
// Deduplicated by pane ID: byobu exposes the same windows/panes under multiple
// sessions (linked windows / multi-client attach), so skip any already seen.
function flatPaneList() {
  const list = [];
  const seen = new Set();
  for (const s of sessions) {
    for (const w of (s.windows || [])) {
      for (const p of (w.panes || [])) {
        if (!p.dead && !seen.has(p.id)) {
          seen.add(p.id);
          list.push({ sessionId: s.id, windowId: w.id, paneId: p.id });
        }
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

// ── touch swipe tracking (used for swipe nav) ─────────────────────────────
let _touchX = 0, _touchY = 0;

output.addEventListener('touchstart', e => {
  _touchX = e.touches[0].clientX;
  _touchY = e.touches[0].clientY;
}, { passive: true });

btnPrev.addEventListener('click', () => navigateRelative(-1));
btnNext.addEventListener('click', () => navigateRelative(1));
document.getElementById('btn-create').addEventListener('click', showCreateOverlay);

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
  const autoCode = (window.location.hash.slice(1) || '').replace(/\D/g, '').slice(0, 6);
  if (autoCode && /^\d{6}$/.test(autoCode)) {
    pairCodeInput.value = `${autoCode.slice(0,3)}-${autoCode.slice(3)}`;
    setTimeout(submitPair, 400);
  } else {
    setTimeout(() => pairCodeInput.focus(), 80);
  }
}

function hidePairScreen() {
  pairOverlay.style.display = 'none';
  if (window.location.hash) {
    history.replaceState(null, '', window.location.pathname + window.location.search);
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

// Show install button OR context name in the center slot — never both.
function _syncCenterSlot() {
  const showInstall = btnInstall.style.display !== 'none';
  ctxName.style.display = showInstall ? 'none' : '';
}

if (!isStandalone) {
  if (isIOS) {
    // iOS Safari: no beforeinstallprompt — show button that explains manual steps.
    btnInstall.style.display = '';
    _syncCenterSlot();
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
      _syncCenterSlot();
    });
    btnInstall.addEventListener('click', async () => {
      if (!deferredInstallPrompt) return;
      deferredInstallPrompt.prompt();
      const { outcome } = await deferredInstallPrompt.userChoice;
      deferredInstallPrompt = null;
      btnInstall.style.display = 'none';
      _syncCenterSlot();
    });
    window.addEventListener('appinstalled', () => {
      btnInstall.style.display = 'none';
      deferredInstallPrompt = null;
      _syncCenterSlot();
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


// ── init: check auth, then connect or show pair screen ────────────────────
async function init() {
  setStatus('connecting…', 'connecting');
  try {
    const r = await fetch('/ping');
    const data = await r.json();
    if (r.ok) {
      if (data.hostname) hostnameDisplay.textContent = data.hostname;
      hideOfflineScreen();
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
      hideOfflineScreen();
      showPairScreen();
    }
  } catch {
    showOfflineScreen();
  }
}
init();

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js');
}
