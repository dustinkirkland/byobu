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
let _serverVersion = null;

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
const connIndicator = document.getElementById('conn-indicator');
const cmdInput      = document.getElementById('cmd');
const pwdInput      = document.getElementById('pwd');
const btnSend       = document.getElementById('btn-send');
const btnKbdMode    = document.getElementById('btn-kbd-mode');
const machineSelect    = document.getElementById('machine-select');
const btnInstall       = document.getElementById('btn-install');
const iosInstallTip    = document.getElementById('ios-install-tip');
const hostnameDisplay  = document.getElementById('hostname-display');
let serverHostname = '';
function setHostnameDisplay(name) { serverHostname = name; hostnameDisplay.textContent = '🖥️ ' + name; }
const headerClock      = document.getElementById('header-clock');
const updateBadge      = document.getElementById('update-badge');
const infoPopup       = document.getElementById('info-popup');
const infoPopupHost   = document.getElementById('info-popup-host');
const infoPopupBody   = document.getElementById('info-popup-body');
const infoPopupReload = document.getElementById('info-popup-reload');
const statuslineLeft   = document.getElementById('statusline-left');
const statuslineRight  = document.getElementById('statusline-right');
const ctxOverlay       = document.getElementById('ctx-overlay');
const ctxListView      = document.getElementById('ctx-list-view');
const ctxList          = document.getElementById('ctx-list');
const ctxRenameOpen    = document.getElementById('ctx-rename-open');
const ctxRenameForm    = document.getElementById('ctx-rename-form');
const ctxRenameLabel   = document.getElementById('ctx-rename-label');
const ctxRenameInput   = document.getElementById('ctx-rename-input');
const ctxRenameBack    = document.getElementById('ctx-rename-back');
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

// ── last-viewed pane (persisted so reopening the PWA restores context) ────
function _lastPaneKey() { return `last-pane:${location.hostname}`; }
function _saveLastPane(paneId) { localStorage.setItem(_lastPaneKey(), paneId); }
function _loadLastPane() { return localStorage.getItem(_lastPaneKey()); }

// ── keyboard mode per pane (persisted so context switches restore correctly) ─
function _kbdModeKey(paneId) { return `kbd-mode:${location.hostname}:${paneId}`; }
function _getKbdMode(paneId) {
  const v = localStorage.getItem(_kbdModeKey(paneId));
  return v !== null ? parseInt(v, 10) : 0;
}
function _saveKbdMode(paneId, mode) { localStorage.setItem(_kbdModeKey(paneId), mode); }

// ── key-bar visibility per pane (persisted like kbdMode, restored on switch) ─
function _keybarKey(paneId) { return `keybar:${location.hostname}:${paneId}`; }
function _getKeybar(paneId) { return localStorage.getItem(_keybarKey(paneId)) === 'true'; }
function _saveKeybar(paneId, on) { localStorage.setItem(_keybarKey(paneId), on); }

// ── line wrap per pane (persisted like kbdMode, restored on switch) ────────
function _wrapKey(paneId) { return `wrap:${location.hostname}:${paneId}`; }
function _getWrap(paneId) { return localStorage.getItem(_wrapKey(paneId)) === 'true'; }
function _saveWrap(paneId, on) { localStorage.setItem(_wrapKey(paneId), on); }

// ── status ─────────────────────────────────────────────────────────────────
function setStatus(msg, cls) {
  connIndicator.title = msg;
  connIndicator.className = cls || '';
  // Close the info popup on any state change rather than let it sit open
  // showing a snapshot from before the transition. hideInfoPopup is a
  // hoisted function declaration, safe to call here even though it's
  // defined later in the file.
  hideInfoPopup();
}

// ── WebSocket ──────────────────────────────────────────────────────────────
function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen  = () => {
    setStatus('connected', 'connected');
    _connectedAt = Date.now();
    startClock();
    send({ type: 'list_sessions' });
    if (currentPane) send({ type: 'subscribe', pane_id: currentPane, lines: 300, ansi: true, join: wrapOn });
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
    if (msg.server_ip) _serverIp = msg.server_ip;
    if (msg.type === 'pong') {
      if (_pendingPing) { _pendingPing(); _pendingPing = null; }
    } else if (msg.type === 'sessions') {
      sessions = msg.data || [];
      if (msg.new_session) forcedSessionId = msg.new_session;
      if (msg.new_pane) forcedPaneId = msg.new_pane;
      rebuildPaneTree();
      // An open picker must track topology: without this, rows for a window
      // that just died stay tappable and navigate to a dead pane, and
      // _ctxResolveLevel's fallback never runs until the next interaction.
      // Refocus only when a picker row had focus: focus() can scroll the
      // list, which would yank a touch user's position on unrelated updates.
      if (ctxPickerActive()) {
        const ae = document.activeElement;
        if (ae && ae.closest('#ctx-list')) _ctxRerender(ae.dataset.ctxId);
        else renderCtxList();
      }
    } else if (msg.type === 'snapshot') {
      if (msg.pane_id === currentPane) {
        const forceTop = _scrollTopOnNextSnapshot;
        if (forceTop) _scrollTopOnNextSnapshot = false;
        const atBottom = output.scrollHeight - output.scrollTop <= output.clientHeight + 60;
        renderOutput(msg.data, !forceTop && atBottom);
        if (forceTop) output.scrollTop = 0;
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

// ── current context helpers ───────────────────────────────────────────────
function currentSession() {
  return sessions.find(s => s.id === currentSessionId) || null;
}

function currentWindow() {
  const s = currentSession();
  if (!s) return null;
  return (s.windows || []).find(w => w.id === currentWindowId) || null;
}

function currentPaneObj() {
  const w = currentWindow();
  if (!w) return null;
  return (w.panes || []).find(p => p.id === currentPane) || null;
}

function livePanesInWindow(w) {
  return (w?.panes || []).filter(p => !p.dead);
}

function firstLivePaneInWindow(w) {
  return livePanesInWindow(w)[0] || null;
}

// tmux defaults pane_title to the hostname when no program set one; that is
// noise, so such a title falls through to the running command instead.
function isDefaultPaneTitle(title) {
  if (!title || !serverHostname) return false;
  const short = s => s.split('.')[0].toLowerCase();
  return short(title) === short(serverHostname);
}

function paneDisplayName(p) {
  if (!p) return 'shell';
  const title = isDefaultPaneTitle(p.title) ? '' : p.title;
  return getPaneName(p.id, '') || title || p.command || 'shell';
}

// ── position label ────────────────────────────────────────────────────────
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
  let savedTarget        = null;
  const savedPaneId      = _loadLastPane();

  for (const s of sessions) {
    for (const w of (s.windows || [])) {
      for (const p of (w.panes || [])) {
        if (!p.dead) {
          const entry = { sessionId: s.id, windowId: w.id, paneId: p.id };
          if (forced === s.id && !autoFromForced) autoFromForced = entry;
          if (forcedPane === p.id) autoFromForcedPane = entry;
          if (!autoFirst) autoFirst = entry;
          if (p.id === currentPane) prevTarget = entry;
          if (savedPaneId && p.id === savedPaneId) savedTarget = entry;
        }
      }
    }
  }

  // Prefer exact forced pane (new window), then current pane, then forced session,
  // then last-viewed pane restored from localStorage, then first available pane.
  const target = autoFromForcedPane ?? prevTarget ?? autoFromForced ?? savedTarget ?? autoFirst ?? null;

  // Scroll to top when navigating to a freshly created window/session
  if (autoFromForcedPane && autoFromForcedPane !== prevTarget) _scrollTopOnNextSnapshot = true;

  if (!target) {
    currentSessionId = null;
    currentWindowId  = null;
    currentPane      = null;
    cmdInput.disabled = true;
    pwdInput.disabled = true;
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
  // Save departing pane's rendered content + scroll position + keyboard mode.
  if (currentPane && output.innerHTML) {
    _paneCache.set(currentPane, { html: output.innerHTML, scrollTop: output.scrollTop });
    if (_paneCache.size > _PANE_CACHE_MAX) _paneCache.delete(_paneCache.keys().next().value);
  }
  if (currentPane) {
    _saveKbdMode(currentPane, kbdMode);
    _saveKeybar(currentPane, keybarVisible());
    _saveWrap(currentPane, wrapOn);
  }
  // Scroll mode is a per-view state: switching panes while in it would leave
  // the new pane top-anchored under a stale SCROLL chip, since the bottom
  // re-anchor stays suppressed.
  if (_scrollMode) exitScrollMode();

  currentSessionId = sessionId;
  currentWindowId  = windowId;
  currentPane      = paneId;
  _saveLastPane(paneId);

  // Restore keyboard mode, wrap state and key-bar visibility for the
  // arriving pane. setKeybarVisible applies the keyboard mode itself.
  kbdMode = _getKbdMode(paneId);
  wrapOn  = _getWrap(paneId);
  applyWrap();
  setKeybarVisible(_getKeybar(paneId));
  cmdInput.disabled = false;
  pwdInput.disabled = false;
  btnSend.disabled  = false;
  output.className  = '';

  const cached = _paneCache.get(paneId);
  if (cached) {
    output.innerHTML  = cached.html;
    output.scrollTop  = cached.scrollTop;
  } else {
    output.textContent = 'loading…';
  }

  send({ type: 'subscribe', pane_id: paneId, lines: 300, ansi: true, join: wrapOn });
  updateXYZLabel();
  updateContextName();
}

// ── context name ──────────────────────────────────────────────────────────
// The breadcrumb is three tap targets, one per picker level: the session
// segment opens the sessions list, the window segment the current session's
// windows, the pane segment the current window's panes. A tap that misses
// every segment falls through to the container listener, which keeps the
// windows-level default.
function _ctxSeg(text, cls, level) {
  const el = document.createElement('span');
  el.className = 'ctx-seg ' + cls;
  el.textContent = text;
  // No stopPropagation: the document-level closers for the info and escape
  // popups must keep seeing the click; the container listener skips segment
  // targets itself.
  el.addEventListener('click', () => showCtxOverlayAt(level));
  return el;
}

function _ctxSegSep() {
  const el = document.createElement('span');
  el.className = 'ctx-seg-sep';
  el.textContent = '/';
  return el;
}

function updateContextName() {
  ctxName.textContent = '';
  if (!currentPane) return;
  const s = currentSession();
  const w = currentWindow();
  const p = currentPaneObj();
  if (!s || !w || !p) {
    ctxName.textContent = getPaneName(currentPane, '') || 'shell';
    return;
  }
  ctxName.append(
    _ctxSeg(s.name, 'ctx-seg-session', 'sessions'),
    _ctxSegSep(),
    _ctxSeg(`${w.index}:${w.name}`, 'ctx-seg-window', 'windows'),
    _ctxSegSep(),
    _ctxSeg(paneDisplayName(p), 'ctx-seg-pane', 'panes'),
  );
}

// ── output rendering ───────────────────────────────────────────────────────
function scrollOutputToBottom() {
  // Scroll mode owns the viewport: a one-line j/k step stays inside the 60px
  // bottom-snap zone, so re-anchoring here would yank a live pane back down.
  if (_scrollMode) return;
  requestAnimationFrame(() => { output.scrollTop = output.scrollHeight; });
}

// 16-color ANSI palettes, one per theme. Dark is the Tango set the app has
// always used. Light keeps each hue recognizable but darkens the entries that
// vanish on a light background (bright yellow, green, cyan, and both whites);
// every value holds >= 4.5:1 contrast on the light --term-bg (#fafafa).
// The same array serves SGR background colors (40-47/100-107), so darkened
// entries make colored backgrounds darker than canonical light schemes;
// acceptable for rare colored-background output, revisit if it bites.
const C16_DARK = [
  '#1e1e1e','#cc0000','#4e9a06','#c4a000','#3465a4','#75507b','#06989a','#d3d7cf',
  '#555753','#ef2929','#8ae234','#fce94f','#729fcf','#ad7fa8','#34e2e2','#eeeeec',
];
const C16_LIGHT = [
  '#000000','#cc0000','#2d7004','#8a5c00','#3465a4','#75507b','#067a7c','#5d6157',
  '#555753','#c81e1e','#1c7d1c','#7a6000','#2a65b0','#8f5a8a','#0c7878','#303030',
];

// Convert ANSI SGR escape codes to HTML spans.
// Handles: 16/256/truecolor fg+bg, bold, italic, underline. Other sequences discarded.
function ansiToHtml(text) {
  // Palette follows the active theme; renders are theme-baked, so a theme
  // switch clears _paneCache and resubscribes (see rerenderTerminal).
  const C16 = document.documentElement.dataset.theme === 'light' ? C16_LIGHT : C16_DARK;
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
  const TOK = /([^\x1b]+)|\x1b(?:\[([0-9;]*)([A-Za-z])|\][^\x07\x1b]*(?:\x07|\x1b\\)|(.))/g;
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
function activeInput() { return kbdMode === 2 ? pwdInput : cmdInput; }

function sendKeys() {
  const inp  = activeInput();
  const keys = inp.value;
  if (!keys || !currentPane) return;
  send({ type: 'send_keys', pane_id: currentPane, keys, enter: true });
  inp.value = '';
  if (inp === cmdInput) {
    cmdInput.style.height = 'auto';
  }
}

// ── events ─────────────────────────────────────────────────────────────────
xyzLabel.addEventListener('click', () => send({ type: 'list_sessions' }));
cmdInput.addEventListener('keydown', e => {
  // The Enter that commits an IME composition arrives here with
  // isComposing=true (or as Android keyCode 229); it must not reach the pane.
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    keybarVisible() ? sendNamedKey('Enter') : sendKeys();
  } else if (e.key === 'Backspace' && keybarVisible() && !cmdInput.value) {
    e.preventDefault();
    sendNamedKey('BSpace');
  }
});
cmdInput.addEventListener('input', () => {
  cmdInput.style.height = 'auto';
  cmdInput.style.height = Math.min(cmdInput.scrollHeight, 160) + 'px';
});
pwdInput.addEventListener('keydown', e => {
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === 'Enter') {
    e.preventDefault();
    keybarVisible() ? sendNamedKey('Enter') : sendKeys();
  } else if (e.key === 'Backspace' && keybarVisible() && !pwdInput.value) {
    e.preventDefault();
    sendNamedKey('BSpace');
  }
});
btnSend.addEventListener('click', sendKeys);

// ── keyboard mode toggle ($_ / Aa / **) ───────────────────────────────────
// 0 = terminal ($_)   no spell-check, no autocorrect
// 1 = text     (Aa)   spell-check + autocorrect on
// 2 = password (**)   type="password" input — keyboard does not learn text
let kbdMode = 0;
function applyKbdMode() {
  const inPwd = kbdMode === 2;
  cmdInput.style.display = inPwd ? 'none' : '';
  pwdInput.style.display = inPwd ? 'block' : 'none';

  // Direct mode needs raw keystrokes: under spell-check Gboard composes
  // plain typing and only commits whole words, so force terminal attributes
  // while the key bar is visible; hiding it restores the user's mode.
  const direct = keybarVisible();

  if (kbdMode === 1) {
    cmdInput.setAttribute('spellcheck', direct ? 'false' : 'true');
    cmdInput.setAttribute('autocorrect', direct ? 'off' : 'on');
    cmdInput.setAttribute('autocapitalize', direct ? 'none' : 'sentences');
    btnKbdMode.textContent = 'Aa';
    btnKbdMode.title = 'Text mode — tap for terminal mode';
    btnKbdMode.style.color = 'var(--accent)';
    // Don't advertise spell check while direct mode has it forced off.
    if (direct) {
      btnKbdMode.title = 'Text mode: spell check suspended in direct mode';
      btnKbdMode.style.color = '';
    }
  } else if (kbdMode === 0) {
    cmdInput.setAttribute('spellcheck', 'false');
    cmdInput.setAttribute('autocorrect', 'off');
    cmdInput.setAttribute('autocapitalize', 'none');
    btnKbdMode.textContent = '$_';
    btnKbdMode.title = 'Terminal mode — tap to enable spell check';
    btnKbdMode.style.color = '';
  } else {
    btnKbdMode.textContent = '**';
    btnKbdMode.title = 'Password mode — keyboard will not learn this text';
    btnKbdMode.style.color = 'var(--accent)';
  }
  scrollOutputToBottom();
}
btnKbdMode.addEventListener('click', () => {
  kbdMode = (kbdMode + 1) % 3;
  if (currentPane) _saveKbdMode(currentPane, kbdMode);
  // Aa defaults to wrapped text, Terminal/Password to unwrapped -- applied
  // once here, on the mode switch itself, not forced on every render. A
  // manual Wrap toggle afterward (escape popup) still sticks until the
  // keyboard mode is cycled again.
  wrapOn = (kbdMode === 1);
  if (currentPane) {
    _saveWrap(currentPane, wrapOn);
    send({ type: 'subscribe', pane_id: currentPane, lines: 300, ansi: true, join: wrapOn });
  }
  applyWrap();
  applyKbdMode();
  // blur + refocus so Android keyboard re-evaluates input type/spellcheck
  const inp = activeInput();
  inp.blur();
  setTimeout(() => inp.focus(), 50);
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

document.getElementById('escape-popup-keys').addEventListener('click', () => {
  toggleKeybar();
  hideEscapePopup();
});

// ── line wrap toggle ───────────────────────────────────────────────────────
// Off (default): tmux's own line breaks, pre with horizontal scroll. On: the
// daemon captures with -J so soft-wrapped lines come back joined, and
// pre-wrap reflows them at the phone width. Its own independent, per-pane-
// persisted state -- not driven by kbdMode -- but the kbdMode cycle button
// sets a sensible default on mode switch (Aa on, Terminal/Password off) so
// entering Aa mode wraps immediately without a separate manual step; a
// Wrap toggle after that still sticks until the mode is cycled again.
let wrapOn = false;
const escapePopupWrap = document.getElementById('escape-popup-wrap');

function applyWrap() {
  output.style.whiteSpace = wrapOn ? 'pre-wrap' : 'pre';
  // Checkmark prefix like the context picker: on-state must not rely on
  // color alone.
  escapePopupWrap.textContent = (wrapOn ? '✓ ' : '⤶ ') + 'Wrap';
  escapePopupWrap.style.color = wrapOn ? 'var(--accent)' : '';
}

escapePopupWrap.addEventListener('click', () => {
  wrapOn = !wrapOn;
  if (currentPane) {
    _saveWrap(currentPane, wrapOn);
    // Resubscribe so the snapshot is re-captured with the new join flag.
    send({ type: 'subscribe', pane_id: currentPane, lines: 300, ansi: true, join: wrapOn });
  }
  applyWrap();
  scrollOutputToBottom();
  hideEscapePopup();
});

document.addEventListener('click', () => hideEscapePopup());
document.addEventListener('touchstart', e => {
  if (!escapePopup.contains(e.target) && e.target !== btnEscape) hideEscapePopup();
}, { passive: true });

// ── key bar (Esc, Ctrl, Tab, arrows; direct key mode for TUIs like vi) ─────
// While the bar is visible the text box drains on every input event: a
// single typed character reaches the pane immediately without Enter, so
// modal editors get bare keystrokes like "i". Named keys on the bar go out
// as tmux key names (literal:false). Ctrl is a sticky modifier: it combines
// with the next typed character or the next named key (C-c, C-Up, ...) and
// then releases. Hiding the bar restores the normal chunked text-box flow.
const keybar     = document.getElementById('keybar');
const keybarCtrl = document.getElementById('keybar-ctrl');
const _cmdPlaceholder = cmdInput.placeholder;
const _pwdPlaceholder = pwdInput.placeholder;
const _directPlaceholder = 'Direct mode: keys go straight to the pane';
// The password box keeps its keyboard-privacy hint in direct mode too.
const _directPwdPlaceholder = 'Direct mode: not saved by keyboard';
let _ctrlArmed = false;

function keybarVisible() { return keybar.style.display !== 'none'; }

function setCtrlArmed(on) {
  _ctrlArmed = on;
  keybarCtrl.classList.toggle('armed', on);
}

function setKeybarVisible(show) {
  // No-op when the state is unchanged (the common pane-switch case), so the
  // blur + refocus dance below does not bounce the keyboard on every switch.
  if (show === keybarVisible()) { applyKbdMode(); return; }
  keybar.style.display = show ? '' : 'none';
  // Persistent direct-mode cues: accent border on the input row and a dimmed
  // Send button, driven by CSS off this class. Unlike the placeholder these
  // survive typing.
  document.body.classList.toggle('keybar-on', show);
  if (!show) setCtrlArmed(false);
  cmdInput.placeholder = show ? _directPlaceholder : _cmdPlaceholder;
  pwdInput.placeholder = show ? _directPwdPlaceholder : _pwdPlaceholder;
  // Re-apply so direct mode forces terminal input attributes and hiding
  // the bar restores the user's mode. Also scrolls output to bottom.
  applyKbdMode();
  // Like btnKbdMode: Android keyboards only re-evaluate input attributes on
  // refocus, so toggling the bar while the box is focused needs the same
  // blur + refocus dance for the forced attributes to take effect.
  const inp = activeInput();
  if (document.activeElement === inp) {
    inp.blur();
    setTimeout(() => inp.focus(), 50);
  }
}

function toggleKeybar() {
  const show = !keybarVisible();
  setKeybarVisible(show);
  if (currentPane) _saveKeybar(currentPane, show);
}

function sendNamedKey(name) {
  if (!currentPane) return;
  if (_ctrlArmed) { name = 'C-' + name; setCtrlArmed(false); }
  send({ type: 'send_keys', pane_id: currentPane, keys: name, enter: false, literal: false });
}

keybar.querySelectorAll('button[data-key]').forEach(btn => {
  btn.addEventListener('click', () => sendNamedKey(btn.dataset.key));
  // Keep focus (and the soft keyboard) in the text box on browsers that
  // focus buttons on tap; Android and iOS mostly do not, so defensive only.
  btn.addEventListener('pointerdown', e => e.preventDefault());
});

keybarCtrl.addEventListener('click', () => setCtrlArmed(!_ctrlArmed));
keybarCtrl.addEventListener('pointerdown', e => e.preventDefault());

// The X on the bar itself: same toggle path as the escape popup entry, so
// per-pane persistence stays in one place.
document.getElementById('keybar-close').addEventListener('click', toggleKeybar);

function drainDirectInput(inp) {
  const text = inp.value;
  if (!text) return;
  inp.value = '';
  if (inp === cmdInput) cmdInput.style.height = 'auto';
  if (!currentPane) return;
  if (_ctrlArmed) {
    setCtrlArmed(false);
    // Ctrl combines with the characters tmux can ctrl-modify (letters plus
    // @ [ \ ] ^ _ and space); iterate by code point so an emoji first
    // character stays intact instead of splitting its surrogate pair into a
    // bogus C-<half> key. Anything else falls through and is sent literally.
    const first = [...text][0];
    if (/^[a-z@\[\\\]^_ ]$/i.test(first)) {
      const key = first === ' ' ? 'C-Space' : 'C-' + first.toLowerCase();
      send({ type: 'send_keys', pane_id: currentPane,
             keys: key, enter: false, literal: false });
      const rest = text.slice(first.length);
      if (rest) send({ type: 'send_keys', pane_id: currentPane, keys: rest, enter: false, literal: true });
      return;
    }
  }
  send({ type: 'send_keys', pane_id: currentPane, keys: text, enter: false, literal: true });
}

// IME guard: mobile keyboards fire input events mid-composition; draining
// (which clears the box) then would duplicate or drop characters. Skip
// while composing and drain once on compositionend.
for (const inp of [cmdInput, pwdInput]) {
  inp.addEventListener('input', e => {
    if (keybarVisible() && !e.isComposing) drainDirectInput(inp);
  });
  inp.addEventListener('compositionend', () => {
    if (keybarVisible()) drainDirectInput(inp);
  });
  // Gboard reports empty-field backspace as keyCode 229, so the keydown
  // path misses it; catch it here instead. When keydown does handle a
  // backspace it calls preventDefault, which cancels this event, so one
  // physical backspace never sends two BSpace. A keyboard that emits
  // neither event on an empty field gets a dead backspace; the bar's
  // backspace button covers that, and the sentinel-character trick is the
  // known fix if it ever matters.
  inp.addEventListener('beforeinput', e => {
    if (keybarVisible() && e.inputType === 'deleteContentBackward' && !inp.value) {
      e.preventDefault();
      sendNamedKey('BSpace');
    }
  });
}

// ── pane list: all live panes across all windows and sessions ─────────────
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

function navigateRelativePane(delta) {
  const w = currentWindow();
  const panes = livePanesInWindow(w);
  if (!w || panes.length < 2) return;
  const idx = panes.findIndex(p => p.id === currentPane);
  const nextPane = panes[((idx < 0 ? 0 : idx) + delta + panes.length) % panes.length];
  navigateTo(currentSessionId, w.id, nextPane.id);
}

// Next/previous window within the current session, wrapping; lands on the
// window's first live pane. Used by the hardware prefix+n / prefix+p bindings.
function navigateRelativeWindow(delta) {
  const s = currentSession();
  if (!s) return;
  const windows = (s.windows || []).filter(w => firstLivePaneInWindow(w));
  if (windows.length < 2) return;
  const idx = windows.findIndex(w => w.id === currentWindowId);
  const next = windows[((idx < 0 ? 0 : idx) + delta + windows.length) % windows.length];
  const p = firstLivePaneInWindow(next);
  if (p) navigateTo(s.id, next.id, p.id);
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

// ── context jump list (tap context name in header) ─────────────────────────
// Drill-down picker: one list per level (sessions, windows, panes) instead of
// one flat indented tree, which turned into a giant scroll with many contexts.
// _ctxLevel/_ctxSessionId/_ctxWindowId hold where the picker is;
// showCtxOverlayAt resets them to the requested level on every open.
let _ctxLevel     = 'windows'; // 'sessions' | 'windows' | 'panes'
let _ctxSessionId = null;
let _ctxWindowId  = null;

function liveWindowsInSession(s) {
  return (s?.windows || []).filter(w => firstLivePaneInWindow(w));
}

function _ctxSessionObj() {
  return sessions.find(s => s.id === _ctxSessionId) || null;
}

function _ctxWindowObj() {
  return (_ctxSessionObj()?.windows || []).find(w => w.id === _ctxWindowId) || null;
}

// A snapshot update can leave the picker pointing at a session or
// window that no longer has live panes; fall back to the level above.
function _ctxResolveLevel() {
  if (_ctxLevel === 'panes' && !livePanesInWindow(_ctxWindowObj()).length) _ctxLevel = 'windows';
  if (_ctxLevel === 'windows' && !liveWindowsInSession(_ctxSessionObj()).length) _ctxLevel = 'sessions';
}

function _ctxAddRow(label, isCurrent, onClick) {
  const btn = document.createElement('button');
  btn.className = 'ctx-btn' + (isCurrent ? ' ctx-current' : '');
  btn.textContent = (isCurrent ? '✓ ' : '') + label;
  btn.addEventListener('click', onClick);
  ctxList.appendChild(btn);
  return btn;
}

function _ctxAddTitle(text) {
  const el = document.createElement('div');
  el.className = 'ctx-title';
  el.textContent = text;
  ctxList.appendChild(el);
}

function _ctxAddBackRow(label, onClick) {
  const btn = _ctxAddRow('← ' + label, false, onClick);
  btn.classList.add('ctx-dim');
  const sep = document.createElement('div');
  sep.className = 'ctx-sep';
  ctxList.appendChild(sep);
}

// Re-render after a level change and move real DOM focus so prefix+w j/k
// keeps working: prefer the row we came from, then the current-context row.
function _ctxRerender(focusId) {
  renderCtxList();
  const rows = pickerRows();
  const target = (focusId && rows.find(b => b.dataset.ctxId === focusId))
    || rows.filter(b => b.classList.contains('ctx-current')).pop()
    // In a non-current session or window nothing is ctx-current; prefer the
    // first real entry over the back row so Enter keeps drilling down.
    || rows.find(b => b.dataset.ctxId)
    || rows[0];
  if (target) target.focus();
}

function renderCtxList() {
  ctxList.innerHTML = '';
  _ctxResolveLevel();

  if (_ctxLevel === 'sessions') {
    _ctxAddTitle('Sessions');
    for (const s of sessions) {
      if (!liveWindowsInSession(s).length) continue;
      const btn = _ctxAddRow(s.name, s.id === currentSessionId, () => {
        _ctxSessionId = s.id;
        _ctxLevel = 'windows';
        _ctxRerender(null);
      });
      btn.dataset.ctxId = s.id;
      btn.dataset.descend = '1';
    }
  } else if (_ctxLevel === 'windows') {
    const s = _ctxSessionObj();
    _ctxAddBackRow('Sessions', () => {
      _ctxLevel = 'sessions';
      _ctxRerender(s.id);
    });
    _ctxAddTitle(s.name);
    for (const w of liveWindowsInSession(s)) {
      const isCurrent = s.id === currentSessionId && w.id === currentWindowId;
      const btn = _ctxAddRow(`${w.index}:${w.name}`, isCurrent, () => {
        _ctxWindowId = w.id;
        _ctxLevel = 'panes';
        _ctxRerender(null);
      });
      btn.dataset.ctxId = w.id;
      btn.dataset.descend = '1';
    }
  } else {
    const s = _ctxSessionObj();
    const w = _ctxWindowObj();
    _ctxAddBackRow(s.name, () => {
      _ctxLevel = 'windows';
      _ctxRerender(w.id);
    });
    _ctxAddTitle(`${w.index}:${w.name}`);
    for (const p of livePanesInWindow(w)) {
      const isCurrent = p.id === currentPane;
      const btn = _ctxAddRow(paneDisplayName(p), isCurrent, () => {
        hideCtxOverlay();
        if (!isCurrent) navigateTo(s.id, w.id, p.id);
      });
      btn.dataset.ctxId = p.id;
    }
  }

  if (!pickerRows().length) {
    ctxList.innerHTML = '';
    const empty = document.createElement('div');
    empty.className = 'ctx-dim';
    empty.style.padding = '16px';
    empty.textContent = 'No contexts';
    ctxList.appendChild(empty);
  }
}

// Open at an explicit level: 'sessions' lists all sessions, 'windows' the
// current session's windows, 'panes' the current window's panes. No
// persistence across opens.
function showCtxOverlayAt(level) {
  // Like navigateTo: the keydown dispatch checks scroll mode before the
  // picker, so an open picker under scroll mode would have a dead keyboard.
  if (_scrollMode) exitScrollMode();
  _ctxLevel     = level;
  _ctxSessionId = currentSessionId;
  _ctxWindowId  = level === 'panes' ? currentWindowId : null;
  renderCtxList();
  ctxListView.style.display = '';
  ctxRenameForm.style.display = 'none';
  ctxOverlay.style.display = 'flex';
}

function showCtxOverlay() {
  // Windows is the middle ground: sessions are one Back away and the current
  // window's panes one tap away.
  showCtxOverlayAt('windows');
}
function hideCtxOverlay() {
  ctxOverlay.style.display = 'none';
}
ctxName.addEventListener('click', e => {
  // Segment clicks open their own level; only a miss keeps the default.
  if (e.target.closest('.ctx-seg')) return;
  showCtxOverlay();
});
ctxCancel.addEventListener('click', hideCtxOverlay);
ctxOverlay.addEventListener('click', e => { if (e.target === ctxOverlay) hideCtxOverlay(); });

// ── rename sub-form (reached via "Rename current" inside the jump list) ────
let _pendingRenameId = null;

function currentPaneDisplayName() {
  for (const s of sessions) {
    for (const w of (s.windows || [])) {
      for (const p of (w.panes || [])) {
        if (p.id === currentPane) return paneDisplayName(p);
      }
    }
  }
  return paneDisplayName(null);
}

function showRenameForm() {
  if (!currentPane) return;
  _pendingRenameId = currentPane;
  const custom = getPaneName(currentPane, '');
  const name = currentPaneDisplayName();
  ctxRenameLabel.textContent = custom
    ? `Rename "${custom}":`
    : `Name this context (${name}):`;
  ctxRenameInput.value = custom;
  ctxListView.style.display = 'none';
  ctxRenameForm.style.display = '';
  setTimeout(() => { ctxRenameInput.focus(); ctxRenameInput.select(); }, 80);
}
function backToCtxList() {
  ctxRenameForm.style.display = 'none';
  // List visible before _ctxRerender: focus() on a hidden element is a no-op.
  ctxListView.style.display = '';
  _ctxRerender(null);
}
ctxRenameOpen.addEventListener('click', showRenameForm);
ctxRenameBack.addEventListener('click', backToCtxList);

function submitRename() {
  if (!_pendingRenameId) return;
  const name = ctxRenameInput.value.trim();
  setPaneName(_pendingRenameId, name);
  _pendingRenameId = null;
  hideCtxOverlay();
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
let _serverIp = '';     // machine's IP, for the connection-info popup
let _connectedAt = 0;   // Date.now() when the current ws connection opened
let _pendingPing = null; // resolver for an in-flight latency ping, or null

// Round-trip latency via a dedicated ping/pong (not list_sessions — that
// queries tmux, adding noise to a number meant to reflect network time).
// Resolves to null if no pong arrives within 3s (matches how the rest of
// the app treats a stalled connection).
function measureLatency() {
  return new Promise(resolve => {
    if (_pendingPing) { resolve(null); return; }
    const start = performance.now();
    _pendingPing = () => resolve(Math.round(performance.now() - start));
    send({ type: 'ping' });
    setTimeout(() => {
      if (_pendingPing) { _pendingPing = null; resolve(null); }
    }, 3000);
  });
}

function startClock() {
  if (_clockInterval) return;
  // YYYY-MM-DD HH:MM:SS, always, in the connected machine's timezone (never
  // the browser's) — built from formatToParts rather than trusting a
  // locale's default field order, so it's exact regardless of browser/locale.
  // Rebuilt every tick since _serverTz can change (e.g. switching machines).
  function tick() {
    const now = new Date(Date.now() + _serverOffset);
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: _serverTz,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hourCycle: 'h23',
    });
    const parts = {};
    for (const p of fmt.formatToParts(now)) parts[p.type] = p.value;
    headerClock.textContent =
      `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
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

let _swRegistration = null;
let _versionText = '';       // last text shown in the info popup

function formatDuration(ms) {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

// ── info popup (tap hostname, connection indicator, or update badge) ──────
// Combines machine identity (FQDN), connection health (method, latency,
// uptime), and daemon version into one popup, in that order: who/where
// you're connected to, how well, then what it's running.
async function showInfoPopup() {
  // location.hostname is the real FQDN/Tailscale MagicDNS name the browser
  // actually used to reach the server — distinct from (and often longer
  // than) the short OS hostname shown in the header via socket.gethostname().
  infoPopupHost.textContent = location.hostname;

  const rect = hostnameDisplay.getBoundingClientRect();
  infoPopup.style.display = 'block';
  infoPopup.style.top   = (rect.bottom + 8) + 'px';
  infoPopup.style.right = (window.innerWidth - rect.right) + 'px';

  const connected = connIndicator.classList.contains('connected');
  const method    = isTailscaleHost() ? 'Tailscale' : 'Direct';
  const ip        = _serverIp || '—';
  const since     = connected && _connectedAt
    ? formatDuration(Date.now() - _connectedAt) : 'not connected';

  const render = latencyText =>
    `IP: ${ip}\n` +
    `Connection: ${method}\n` +
    `Latency: ${latencyText}\n` +
    `Connected: ${since}\n` +
    `Version: ${_versionText}`;

  infoPopupBody.textContent = render(connected ? 'measuring…' : 'not connected');

  if (connected) {
    const latency = await measureLatency();
    // Bail if the popup was closed (or reopened, wiping this stale request)
    // while the ping was in flight.
    if (infoPopup.style.display === 'none') return;
    infoPopupBody.textContent = render(latency !== null ? latency + ' ms' : 'timed out');
  }
}
function hideInfoPopup() {
  infoPopup.style.display = 'none';
}
function toggleInfoPopup(e) {
  e.stopPropagation();
  infoPopup.style.display === 'none' ? showInfoPopup() : hideInfoPopup();
}
hostnameDisplay.addEventListener('click', toggleInfoPopup);
updateBadge.addEventListener('click', toggleInfoPopup);
connIndicator.addEventListener('click', toggleInfoPopup);
infoPopupReload.addEventListener('click', async () => {
  if (_swRegistration) await _swRegistration.update().catch(() => {});
  location.reload();
});
document.addEventListener('click', () => hideInfoPopup());
document.addEventListener('touchstart', e => {
  const isTrigger = e.target === hostnameDisplay || e.target === updateBadge || e.target === connIndicator;
  if (!infoPopup.contains(e.target) && !isTrigger) hideInfoPopup();
}, { passive: true });

function applyVersion(v) {
  if (!v) return;
  const isUpdate = _serverVersion && v !== _serverVersion;
  _versionText = isUpdate ? `v${v} — server updated, tap Reload` : `v${v}`;
  updateBadge.style.display = isUpdate ? '' : 'none';
  if (!isUpdate) _serverVersion = v;
}

async function applyHostname() {
  try {
    const data = await fetch('/ping').then(r => r.json());
    if (data.hostname) setHostnameDisplay(data.hostname);
    if (data.version) applyVersion(data.version);
  } catch { /* ignore */ }
}

// ── display settings (font family, terminal font size, theme) ─────────────
// Device-global (plain localStorage keys, not per pane). The theme itself is
// two CSS palettes on html[data-theme]; the inline head script applies the
// saved choice before first paint, this section handles live changes. The
// terminal output follows the theme too (see --term-* in the CSS), and
// ansiToHtml swaps between C16_DARK and C16_LIGHT to match.
// Local fonts only, no downloads: every entry is listed, and ones the device
// cannot render are annotated "(not installed)" but stay selectable (see
// fontResolves and renderSettings; hiding them made selection a one-way
// door). The list mixes desktop staples with the monospace families Android
// ships (Droid Sans Mono, Cutive Mono, and OEM extras).
const FONT_FAMILIES = [
  { label: 'System',         value: "'SF Mono', 'Fira Code', 'Cascadia Code', monospace" },
  { label: 'Fira Code',      value: "'Fira Code', monospace",      probe: 'Fira Code' },
  { label: 'JetBrains Mono', value: "'JetBrains Mono', monospace", probe: 'JetBrains Mono' },
  { label: 'Cascadia Code',  value: "'Cascadia Code', monospace",  probe: 'Cascadia Code' },
  { label: 'Droid Sans Mono', value: "'Droid Sans Mono', monospace", probe: 'Droid Sans Mono' },
  { label: 'Roboto Mono',    value: "'Roboto Mono', monospace",    probe: 'Roboto Mono' },
  { label: 'Noto Sans Mono', value: "'Noto Sans Mono', monospace", probe: 'Noto Sans Mono' },
  { label: 'Cutive Mono',    value: "'Cutive Mono', monospace",    probe: 'Cutive Mono' },
  { label: 'Courier',        value: "'Courier New', Courier, monospace", probe: 'Courier New' },
];

// True when the bare family renders, measured against proportional generics.
// Width measurement rather than document.fonts.check: Chrome on Android
// resolves 'Courier New' through a font alias that fonts.check misses. The
// comparison is against serif and sans-serif, NOT monospace: most monospace
// fonts share the 0.6em advance, so a present family can be width-identical
// to the monospace default while looking entirely different. A missing
// family falls back to the generic and measures equal, so this fails closed.
const _fontProbe = document.createElement('canvas').getContext('2d');
function fontResolves(family) {
  const sample = 'mmmmmmmmmmillWW##1234567890';
  const width = font => {
    _fontProbe.font = `16px ${font}`;
    return _fontProbe.measureText(sample).width;
  };
  return width(`"${family}", serif`) !== width('serif')
      && width(`"${family}", sans-serif`) !== width('sans-serif');
}

// Installed fonts cannot change mid-session; probe once at load.
const _resolvedFonts = new Set(
  FONT_FAMILIES.filter(f => !f.probe || fontResolves(f.probe)).map(f => f.label));
const THEME_CHOICES = [
  ['dark',  'Dark'],
  ['light', 'Light'],
  ['auto',  'Device (auto)'],
];
const FONT_SIZE_MIN = 10, FONT_SIZE_MAX = 16, FONT_SIZE_DEFAULT = 12;

let fontFamily = localStorage.getItem('font-family') || 'System';
if (!FONT_FAMILIES.some(f => f.label === fontFamily)) fontFamily = 'System';
let fontSize   = parseInt(localStorage.getItem('font-size'), 10);
if (!(fontSize >= FONT_SIZE_MIN && fontSize <= FONT_SIZE_MAX)) fontSize = FONT_SIZE_DEFAULT;
let themePref  = localStorage.getItem('theme') || 'dark';
if (!THEME_CHOICES.some(([v]) => v === themePref)) themePref = 'dark';

const settingsOverlay   = document.getElementById('settings-overlay');
const settingsThemeList = document.getElementById('settings-theme-list');
const settingsFontList  = document.getElementById('settings-font-list');
const settingsSizeValue = document.getElementById('settings-size-value');
const themeColorMeta    = document.querySelector('meta[name="theme-color"]');
const _lightSchemeMq    = matchMedia('(prefers-color-scheme: light)');
// Captured before the first applyFont() so it is the CSS default stack, not
// a saved override; used to preview the System row from the CSS source.
const _cssDefaultFont   =
  getComputedStyle(document.documentElement).getPropertyValue('--font');

function applyFont() {
  const f = FONT_FAMILIES.find(f => f.label === fontFamily) || FONT_FAMILIES[0];
  // 'System' leaves --font alone so the CSS default stays the single source
  // of truth for the default stack.
  if (f === FONT_FAMILIES[0]) {
    document.documentElement.style.removeProperty('--font');
  } else {
    document.documentElement.style.setProperty('--font', f.value);
  }
  // Size scales the terminal output only; the rest of the UI keeps its
  // tuned per-element sizes.
  output.style.fontSize = fontSize + 'px';
}

function applyTheme() {
  const next = themePref === 'auto' ? (_lightSchemeMq.matches ? 'light' : 'dark') : themePref;
  // The head script set the theme before app.js ran, so on startup this is a
  // no-op and only real flips trigger a terminal re-render.
  const flipped = document.documentElement.dataset.theme !== next;
  document.documentElement.dataset.theme = next;
  // PWA chrome follows the active background.
  themeColorMeta.content =
    getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
  if (flipped) rerenderTerminal();
}

// Rendered output has ansiToHtml palette colors baked into its HTML, so a
// theme flip invalidates every cached snapshot. Clear the cache and
// resubscribe the current pane; the next snapshot renders with the new
// palette.
function rerenderTerminal() {
  _paneCache.clear();
  if (currentPane) {
    // join must ride every resubscribe: without it the daemon captures this
    // pane unjoined, and with wrap on the re-render hard-breaks long lines
    // at the tmux pane width until the next pane switch.
    send({ type: 'subscribe', pane_id: currentPane, lines: 300, ansi: true, join: wrapOn });
  }
}

// Live-update while in auto mode when the device scheme flips.
_lightSchemeMq.addEventListener('change', () => {
  if (themePref === 'auto') applyTheme();
});

function renderSettings() {
  // Checkmark prefix like the context picker: the selected row must not
  // rely on color alone.
  settingsThemeList.innerHTML = '';
  for (const [value, label] of THEME_CHOICES) {
    const on = value === themePref;
    const btn = document.createElement('button');
    btn.className = 'ctx-btn' + (on ? ' ctx-current' : '');
    btn.textContent = (on ? '✓ ' : '') + label;
    btn.addEventListener('click', () => {
      themePref = value;
      localStorage.setItem('theme', value);
      applyTheme();
      renderSettings();
    });
    settingsThemeList.appendChild(btn);
  }
  settingsFontList.innerHTML = '';
  for (const f of FONT_FAMILIES) {
    const on = f.label === fontFamily;
    const available = _resolvedFonts.has(f.label);
    // Entries the probe says are missing are hidden, not just dimmed, so the
    // list only shows choices that actually do something on this device.
    // Exception: the currently selected font always stays visible, even if
    // unresolved -- the probe can false-negative (Chrome on Android may not
    // expose raw platform family names), and a selected font disappearing
    // out from under you is worse than an unreachable row in the list.
    if (!available && !on) continue;
    const btn = document.createElement('button');
    btn.className = 'ctx-btn' + (on ? ' ctx-current' : '');
    btn.textContent = (on ? '✓ ' : '') + f.label + (available ? '' : ' (not installed)');
    if (!available) btn.style.opacity = '0.55';
    // Preview each row in its own face; System previews with the CSS default
    // captured at load so the CSS stays the single source of truth.
    btn.style.fontFamily = f === FONT_FAMILIES[0] ? _cssDefaultFont : f.value;
    btn.addEventListener('click', () => {
      fontFamily = f.label;
      localStorage.setItem('font-family', f.label);
      applyFont();
      renderSettings();
    });
    settingsFontList.appendChild(btn);
  }
  settingsSizeValue.textContent = fontSize + 'px';
}

function stepFontSize(delta) {
  const next = Math.min(FONT_SIZE_MAX, Math.max(FONT_SIZE_MIN, fontSize + delta));
  if (next === fontSize) return;
  fontSize = next;
  localStorage.setItem('font-size', next);
  applyFont();
  settingsSizeValue.textContent = next + 'px';
}

function showSettingsOverlay() {
  renderSettings();
  settingsOverlay.style.display = 'flex';
}
function hideSettingsOverlay() {
  settingsOverlay.style.display = 'none';
}

document.getElementById('btn-settings').addEventListener('click', showSettingsOverlay);
document.getElementById('settings-close').addEventListener('click', hideSettingsOverlay);
document.getElementById('settings-size-down').addEventListener('click', () => stepFontSize(-1));
document.getElementById('settings-size-up').addEventListener('click', () => stepFontSize(1));
settingsOverlay.addEventListener('click', e => { if (e.target === settingsOverlay) hideSettingsOverlay(); });

applyFont();
applyTheme();

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


// ── hardware-keyboard tmux bindings (Ctrl+B prefix) ────────────────────────
// Local capture for hardware keyboards: Ctrl+B arms a 2s pending-prefix state
// (chip in the statusbar, Esc cancels), then w opens the context picker with
// j/k navigation, n/p cycle windows, [ enters a client-side scroll mode on
// the snapshot div. byobu's remote prefix stays F12/Ctrl-A and the key bar's
// sticky Ctrl still sends a literal C-b, so capturing Ctrl+B here strands no
// one. Everything is inert until a hardware Ctrl+B actually arrives, so
// touch-only users see zero UI change. In scroll mode, unhandled keys
// (printable or not) are swallowed and ignored so nothing leaks into the
// inputs; q or Esc exits.
const kbdModeChip = document.getElementById('kbd-mode-chip');
const PREFIX_TIMEOUT_MS = 2000;
let _prefixArmed = false;
let _prefixTimer = null;
let _scrollMode  = false;

function _showKbdChip(text) {
  kbdModeChip.textContent = text;
  kbdModeChip.style.display = '';
}
function _hideKbdChip() { kbdModeChip.style.display = 'none'; }

// The one definition of the prefix chord, used by every mode: unshifted
// plain Ctrl+B (Ctrl+Shift+B is the browser's bookmarks-bar toggle). Four
// call sites once drifted apart; keep them on this predicate.
function isPrefixChord(e) {
  return e.ctrlKey && !e.shiftKey && !e.altKey && !e.metaKey
      && e.key.toLowerCase() === 'b';
}

function armPrefix() {
  _prefixArmed = true;
  clearTimeout(_prefixTimer);
  _prefixTimer = setTimeout(disarmPrefix, PREFIX_TIMEOUT_MS);
  _showKbdChip('C-b');
}
function disarmPrefix() {
  _prefixArmed = false;
  clearTimeout(_prefixTimer);
  _prefixTimer = null;
  _hideKbdChip();
}

// ── scroll mode (prefix+[) ── pure scrollTop manipulation of #output
function enterScrollMode() {
  _scrollMode = true;
  // Move focus off the inputs so nothing types into them while scrolling.
  const ae = document.activeElement;
  if (ae && typeof ae.blur === 'function') ae.blur();
  _showKbdChip('SCROLL');
}
function exitScrollMode() {
  _scrollMode = false;
  _hideKbdChip();
  // Entry blurred the input, so q/Esc would strand focus on body and the
  // next keystrokes would go nowhere. Scroll mode is only reachable via a
  // hardware Ctrl+B, so refocusing cannot pop a soft keyboard.
  activeInput().focus();
}

function handleScrollKey(e) {
  // Bare modifier keydowns pass; the chord they start is judged on its own.
  if (['Control', 'Shift', 'Alt', 'Meta'].includes(e.key)) return;
  const line = parseFloat(getComputedStyle(output).lineHeight) || 18;
  const page = Math.max(line, output.clientHeight - line);
  const half = Math.max(line, Math.round(output.clientHeight / 2));
  let step = null;
  if (!e.ctrlKey && !e.altKey && !e.metaKey) {
    const k = e.key;
    if (k === 'q' || k === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      exitScrollMode();
      return;
    }
    if      (k === 'j' || k === 'ArrowDown') step = line;
    else if (k === 'k' || k === 'ArrowUp')   step = -line;
    else if (k === 'PageDown')               step = page;
    else if (k === 'PageUp')                 step = -page;
  } else if (e.ctrlKey && !e.shiftKey && !e.altKey && !e.metaKey) {
    const k = e.key.toLowerCase();
    if      (k === 'd') step = half;
    else if (k === 'u') step = -half;
    else if (isPrefixChord(e)) {
      // Never let Ctrl+B reach the browser (Firefox opens the bookmarks
      // sidebar): leave scroll mode and re-arm the prefix instead.
      e.preventDefault();
      e.stopPropagation();
      exitScrollMode();
      armPrefix();
      return;
    }
    else return; // other Ctrl chords (reload, tab switch, ...) pass through
  } else {
    return; // Alt/Meta chords pass through
  }
  e.preventDefault();
  e.stopPropagation();
  if (step !== null) output.scrollTop += step;
}

// ── context picker keyboard navigation (prefix+w, prefix+s) ────────────────
function ctxPickerActive() {
  return ctxOverlay.style.display !== 'none' && ctxRenameForm.style.display === 'none';
}

function pickerRows() {
  return [...ctxList.querySelectorAll('.ctx-btn')];
}

function movePickerFocus(delta) {
  const rows = pickerRows();
  if (!rows.length) return;
  const idx = rows.indexOf(document.activeElement);
  const next = idx < 0
    ? rows[delta > 0 ? 0 : rows.length - 1]
    : rows[(idx + delta + rows.length) % rows.length];
  next.focus();
  next.scrollIntoView({ block: 'nearest' });
}

function openCtxPickerKeyboard(level) {
  showCtxOverlayAt(level || 'windows');
  // The ctx-current row is the current window at the windows level and the
  // current session at the sessions level: Enter descends into it. Fall back
  // to the first row.
  const rows = pickerRows();
  const marked = rows.filter(b => b.classList.contains('ctx-current'));
  const start = marked[marked.length - 1] || rows[0];
  if (start) start.focus();
}

// Esc or h at a deeper picker level goes up one; returns false at the top so
// the caller closes instead.
function ctxPickerUp() {
  if (_ctxLevel === 'panes')   { _ctxLevel = 'windows';  _ctxRerender(_ctxWindowId);  return true; }
  if (_ctxLevel === 'windows') { _ctxLevel = 'sessions'; _ctxRerender(_ctxSessionId); return true; }
  return false;
}

function handlePickerKey(e) {
  if (e.key === 'Escape') {
    e.preventDefault();
    e.stopPropagation();
    if (!ctxPickerUp()) hideCtxOverlay();
    return;
  }
  const ae = document.activeElement;
  const isField = ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA');
  if (isPrefixChord(e)) {
    // Never let Ctrl+B reach the browser (Firefox opens the bookmarks
    // sidebar): close the picker and re-arm the prefix, with the same
    // foreign-field exemption as the neutral-state capture below.
    if (isField && ae !== cmdInput && ae !== pwdInput) return;
    e.preventDefault();
    e.stopPropagation();
    hideCtxOverlay();
    armPrefix();
    return;
  }
  // With a text field focused (picker opened by tap while typing), only Esc
  // acts; j/k keep typing and arrows keep moving the caret.
  if (isField) return;
  if (e.key === 'q') {
    // Close outright from any depth, like scroll mode and tmux choose-tree;
    // Esc walks up a level first, so q is the one-keystroke close.
    e.preventDefault();
    e.stopPropagation();
    hideCtxOverlay();
  } else if (e.key === 'j' || e.key === 'ArrowDown') {
    e.preventDefault();
    e.stopPropagation();
    movePickerFocus(1);
  } else if (e.key === 'k' || e.key === 'ArrowUp') {
    e.preventDefault();
    e.stopPropagation();
    movePickerFocus(-1);
  } else if (e.key === 'h' || e.key === 'ArrowLeft') {
    e.preventDefault();
    e.stopPropagation();
    ctxPickerUp();
  } else if (e.key === 'l' || e.key === 'ArrowRight') {
    // Descend only: pane rows and footer buttons have no descend flag, so l
    // never navigates or closes by accident.
    e.preventDefault();
    e.stopPropagation();
    if (ae && ae.dataset.descend) ae.click();
  }
  // Enter falls through: the focused button's native activation fires its
  // existing click handler.
}

// ── prefix key dispatch ─────────────────────────────────────────────────────
function handlePrefixKey(e) {
  // Bare modifier keydowns (Ctrl going down for a chord) keep the prefix armed.
  if (['Control', 'Shift', 'Alt', 'Meta'].includes(e.key)) return;
  e.preventDefault();
  e.stopPropagation();
  if (isPrefixChord(e)) { armPrefix(); return; } // key repeat re-arms
  disarmPrefix();
  // Modified keys are unbound: Ctrl+W while armed must not open the picker.
  const k = (e.ctrlKey || e.altKey || e.metaKey) ? '' : e.key;
  if      (k === 'w') openCtxPickerKeyboard();
  else if (k === 's') openCtxPickerKeyboard('sessions'); // tmux choose-session analog
  else if (k === 'n') navigateRelativeWindow(1);
  else if (k === 'p') navigateRelativeWindow(-1);
  else if (k === 'o') navigateRelativePane(1);
  else if (k === 'O') navigateRelativePane(-1); // no tmux default for backward; mirrors o
  // tmux binds prefix+arrows to directional pane selection; the PWA shows one
  // pane at a time, so down/j means next and up/k means previous.
  else if (k === 'ArrowDown' || k === 'j') navigateRelativePane(1);
  else if (k === 'ArrowUp'   || k === 'k') navigateRelativePane(-1);
  else if (k === '[') enterScrollMode();
  // Esc and any unbound key: prefix cancelled, keystroke swallowed (tmux-like).
}

// Capture phase so the bindings run ahead of the input handlers; a handled
// key calls preventDefault, which also stops the direct-mode input event
// from ever firing (Ctrl+B itself produces no input event at all).
document.addEventListener('keydown', e => {
  // Same IME guard as the input handlers above.
  if (e.isComposing || e.keyCode === 229) return;
  if (_isLocked) return;

  if (_scrollMode)       { handleScrollKey(e); return; }
  if (ctxPickerActive()) { handlePickerKey(e); return; }
  if (_prefixArmed)      { handlePrefixKey(e); return; }

  if (isPrefixChord(e)) {
    // Arm from anywhere except foreign text fields (pairing code, rename and
    // create forms); cmdInput and pwdInput are where keyboard users live.
    const ae = document.activeElement;
    const isField = ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA');
    if (isField && ae !== cmdInput && ae !== pwdInput) return;
    e.preventDefault(); // Ctrl+B is bold/bookmark-ish in some browser contexts
    e.stopPropagation();
    armPrefix();
  }
}, true);

// ── keyboard-aware viewport (visualViewport) ───────────────────────────────
// iOS/Android don't resize the layout viewport when the on-screen keyboard
// opens — they resize the *visual* viewport and, on iOS Safari, also scroll
// the page to bring the focused input into view. #app's `position:fixed;
// inset:0` is pinned to the layout viewport, so it drifts out of sync: the
// header scrolls off the top and dead space appears at the bottom. Re-pin
// #app to the current visual viewport on every change (keyboard open/close,
// resize, orientation) and cancel any page-level scroll iOS applies.
const appEl = document.getElementById('app');
function syncViewportToKeyboard() {
  const vv = window.visualViewport;
  if (!vv) return;
  appEl.style.height = vv.height + 'px';
  appEl.style.top = vv.offsetTop + 'px';
  window.scrollTo(0, 0);
}
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', syncViewportToKeyboard);
  window.visualViewport.addEventListener('scroll', syncViewportToKeyboard);
  syncViewportToKeyboard();
}

// ── init: check auth, then connect or show pair screen ────────────────────
async function init() {
  setStatus('connecting…', 'connecting');
  try {
    const r = await fetch('/ping');
    const data = await r.json();
    if (r.ok) {
      if (data.hostname) setHostnameDisplay(data.hostname);
      if (data.version) applyVersion(data.version);
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
  navigator.serviceWorker.register('/sw.js').then(r => { _swRegistration = r; });
}
