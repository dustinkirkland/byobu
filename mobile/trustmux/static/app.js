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
let _paneLoading = false; // output holds the loading placeholder, not pane content
// Ghost anchor from the daemon: the cursor's capture line as a
// from-the-end index (0 = last line) plus its column, riding snapshot/update
// messages and carried alone by data-less cursor messages on a bare cursor
// move. null when the daemon predates the fields or could not read the
// cursor; the ghost then falls back to its end-of-buffer anchor.
let _cursorFromEnd = null;
let _cursorX = null;
// Every message type that can carry a cursor (snapshot/update/delta/patch/
// cursor) sets these the same way; one helper instead of five copies that
// could silently diverge (e.g. delta once shipped without this call at all).
function _applyCursorFields(msg) {
  _cursorFromEnd = Number.isInteger(msg.cursor_from_end) ? msg.cursor_from_end : null;
  _cursorX = Number.isInteger(msg.cursor_x) ? msg.cursor_x : null;
}
// Per-line model of the current subscription's capture: element i mirrors
// the daemon's content.split('\n'), so patch op indices mean the same lines
// on both sides and #output's children map 1:1 onto it. null whenever the
// DOM does not hold a snapshot-built render (pane switch, cache restore,
// loading placeholder); a patch arriving then is a straggler and is dropped
// (the state that nulled this also subscribed, so a snapshot is coming).
let _lines = null;
let statusInterval = null;

// ── pane snapshot cache (in-memory only — never persisted) ────────────────
// Stores the last-rendered HTML + scroll position for each pane so that
// switching back to a pane is instant while the fresh snapshot loads.
const _paneCache = new Map();
const _PANE_CACHE_MAX = 50;

// Pane-switch subscribes are serialized to at most one in flight: a fresh
// subscribe can't preempt one already sent (once write_message() has been
// called daemon-side the bytes are committed -- cancelling afterward can't
// unsend them), so sending a second one before the first's snapshot has
// even arrived just queues more bytes behind it on the same slow link. If
// nothing is outstanding, a new pane switch sends immediately -- zero
// added latency for the common case of an isolated tap. If something is
// still outstanding, only the *latest* target is remembered and sent the
// moment the outstanding one resolves (its snapshot arrives), which is the
// earliest point it's actually safe to send more. See _requestSubscribe's
// call site in navigateTo for why this exists.
let _subscribeInFlight = false;
let _subscribeDeferredPaneId = null;
let _subscribeSafetyTimer = null;
// Backstop only: normally cleared the instant the in-flight snapshot
// arrives. Guards against a subscribe that, for whatever reason, never
// gets a reply (e.g. the pane died in the gap between tap and send),
// which would otherwise wedge every subsequent navigation behind a flag
// that never clears.
const _SUBSCRIBE_SAFETY_MS = 15000;

function _sendSubscribeNow(paneId) {
  _subscribeInFlight = true;
  _subscribeDeferredPaneId = null;
  clearTimeout(_subscribeSafetyTimer);
  _subscribeSafetyTimer = setTimeout(() => {
    _subscribeInFlight = false;
    if (_subscribeDeferredPaneId !== null) {
      const target = _subscribeDeferredPaneId;
      _subscribeDeferredPaneId = null;
      _sendSubscribeNow(target);
    }
  }, _SUBSCRIBE_SAFETY_MS);
  // Full history with ANSI, join following wrap mode, and incremental
  // patches opted into for unjoined captures only. Join subscribers stay on
  // full snapshots: -J reshuffles soft-wrap line identity on every
  // width-crossing change, so line-indexed patches would churn most of the
  // buffer anyway, and the cursor is already absent under join.
  send({ type: 'subscribe', pane_id: paneId, lines: 300, ansi: true,
         join: wrapOn, patch: !wrapOn && !_patchDisabled });
}

// A patch that fails to apply forces a resubscribe (fresh snapshot); a
// persistent client/server line-splitting disagreement would otherwise
// repeat that on every following patch forever (resubscribe -> snapshot ->
// patch fails again -> resubscribe...), spamming full-snapshot churn. After
// a few in a row, stop asking for patches for the rest of this session and
// settle for plain full updates instead.
let _patchMismatchStreak = 0;
const _PATCH_MISMATCH_LIMIT = 3;
let _patchDisabled = false;

function _requestSubscribe(paneId) {
  if (_subscribeInFlight) {
    _subscribeDeferredPaneId = paneId; // supersedes any earlier deferred target
  } else {
    _sendSubscribeNow(paneId);
  }
}

// Called whenever a subscribe's response has definitively arrived, so the
// next queued navigation (if any) can go out immediately.
function _subscribeSettled() {
  _subscribeInFlight = false;
  clearTimeout(_subscribeSafetyTimer);
  if (_subscribeDeferredPaneId !== null) {
    const target = _subscribeDeferredPaneId;
    _subscribeDeferredPaneId = null;
    _sendSubscribeNow(target);
  }
}

// Raw (pre-render) lines of the currently displayed pane, so a "delta"
// message (drop N lines off the top, append these at the bottom -- see
// the daemon's _diff_pane_lines) can be applied without re-fetching the
// whole pane. Tracked separately from _paneCache, which only ever holds
// already-rendered HTML. _currentPaneRawLinesFor guards against applying
// a delta meant for a pane we're no longer tracking raw lines for.
let _currentPaneRawLines = [];
let _currentPaneRawLinesFor = null;

// A content re-render (renderOutput's full replaceChildren, or applyPatch's
// splice of the touched lines) destroys whatever DOM nodes it touches --
// including the text nodes an in-progress native text selection is anchored
// to. Android in particular loses track of a selection handle the instant
// its anchor node is removed, and appears to recover by ballooning the
// selection to the whole container rather than collapsing it -- so a pane
// that updates while the user is mid-drag makes copying anything in it feel
// broken. The 'cursor' handler already avoids this for a bare cursor move by
// never calling renderOutput; _hasOutputSelection/_deferredForSelection
// extend the same protection to real content updates: skip applying one
// while a selection is active, and let the existing mismatch/resubscribe
// path (applyPatch's bounds check, or a forced resubscribe here) resync the
// instant the selection clears, rather than tracking a queue of missed diffs.
let _deferredForSelection = false;

function _hasOutputSelection() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return false;
  // Either end: a handle dragged off #output puts one end outside it.
  return output.contains(sel.anchorNode) || output.contains(sel.focusNode);
}

// A selection only exists once the gesture that makes it has finished: the
// long press, the double tap, the mouse drag. A re-render during that
// gesture breaks the selection before it starts, so the freeze also covers
// any pointer held down on #output, plus a grace period after it lifts
// (the gap between the two taps of a double tap, and the moment Android
// takes to turn a long press into a selection).
const OUTPUT_GESTURE_GRACE_MS = 400;
const _outputPointers = new Set();   // pointerIds down on #output
let _outputGestureUntil = 0;

function _outputFrozen() {
  return _outputPointers.size > 0
    || _outputTouching
    || performance.now() < _outputGestureUntil
    || _hasOutputSelection();
}

function _resyncIfThawed() {
  if (_deferredForSelection && !_outputFrozen()) {
    _deferredForSelection = false;
    if (currentPane) _requestSubscribe(currentPane);
  }
}

function _outputPointerUp(e) {
  if (!_outputPointers.delete(e.pointerId)) return;
  _outputGestureUntil = performance.now() + OUTPUT_GESTURE_GRACE_MS;
  setTimeout(_resyncIfThawed, OUTPUT_GESTURE_GRACE_MS + 20);
}

document.addEventListener('selectionchange', _resyncIfThawed);

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
const pendingSendBanner  = document.getElementById('pending-send-banner');
const pendingSendRestore = document.getElementById('pending-send-restore');
const btnKbdMode    = document.getElementById('btn-kbd-mode');
const keybarKbdMode = document.getElementById('keybar-kbd-mode');
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
const logoLink        = document.getElementById('logo-link');
const wordmark        = document.getElementById('wordmark');

// "Trustmux" must be all-or-nothing: CSS flex-shrink alone would let it clip
// mid-word (e.g. down to "Trust") as the hostname/clock crowd the header on
// narrow screens or with a long hostname. Instead, whenever #logo-link's
// rendered width changes for any reason (window resize, hostname text
// changing, install button or machine selector appearing), re-measure
// whether the wordmark's full text still fits and hide it outright if not.
function updateWordmarkVisibility() {
  const wasHidden = wordmark.style.display === 'none';
  if (wasHidden) wordmark.style.display = '';
  const clipped = wordmark.scrollWidth > wordmark.clientWidth;
  wordmark.style.display = clipped ? 'none' : '';
}
// Watching #logo-link alone isn't enough: once it settles to its small
// icon-only footprint, nothing about *its own* box changes again as space
// frees up (window grows, hostname shrinks back), so it would never recover.
// Instead watch every determinant of its available space — the header's own
// width (viewport/orientation changes) plus each fixed-size sibling that
// competes with it for room — so a recheck fires in both directions.
{
  const headerEl = document.getElementById('header');
  const ro = new ResizeObserver(updateWordmarkVisibility);
  [headerEl, connIndicator, hostnameDisplay, updateBadge, headerClock, btnInstall, machineSelect]
    .forEach((el) => ro.observe(el));
}
const aboutPopup      = document.getElementById('about-popup');
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
const kbdModePopup     = document.getElementById('kbdmode-popup');

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

// ── names ─────────────────────────────────────────────────────────────────
// Window and pane names live in tmux (window_name, and the pane title that
// `select-pane -T` sets), so a rename on either side shows up on the other
// within one topology poll. Pane names used to be kept per browser in
// localStorage under pane-name:*; drop those leftovers.
try {
  for (const k of Object.keys(localStorage)) {
    if (k.startsWith('pane-name:')) localStorage.removeItem(k);
  }
} catch (_) {}

// Sessions are shown by number: tmux names an unnamed session after its id
// ($3 is "3"), and the id stays the same when the session is renamed.
function sessionLabel(s) {
  return s ? String(s.id).replace(/^\$/, '') : '';
}

// tmux refuses these in a window name (they would parse as a target).
const _BAD_WINDOW_NAME = /[:.@%]/;

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
    startHealthMonitor();
    send({ type: 'list_sessions' });
    if (currentPane) _sendSubscribeNow(currentPane);
  };
  ws.onclose = (evt) => {
    stopClock();
    stopHealthMonitor();
    // In-flight tracking is scoped to one connection -- a subscribe sent
    // on the old socket will never get a reply on the new one, so start
    // clean rather than carrying a stale "in flight" flag (or a stale
    // deferred target) across the reconnect.
    _subscribeInFlight = false;
    _subscribeDeferredPaneId = null;
    clearTimeout(_subscribeSafetyTimer);
    // A dropped connection is already maximally uncertain for anything
    // still in flight -- surface the recovery banner now rather than
    // waiting out the rest of _PENDING_SEND_TIMEOUT_MS.
    if (_pendingSend) _showPendingSendBanner();
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
      const rtt = _pingSentAt !== null ? Math.round(performance.now() - _pingSentAt) : null;
      _pingSentAt = null;
      const waiters = _pendingPings; _pendingPings = [];
      waiters.forEach(fn => fn(rtt));
    } else if (msg.type === 'send_keys_ack') {
      if (_pendingSend && msg.req_id === _pendingSend.reqId) _clearPendingSend();
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
      // A snapshot is always the first (and only) reply to a subscribe,
      // so its arrival is exactly the signal that it's now safe to send
      // whatever navigation target queued up behind it, if any.
      _subscribeSettled();
      if (msg.pane_id === currentPane) {
        _currentPaneRawLines = msg.data.split('\n');
        _currentPaneRawLinesFor = msg.pane_id;
        const forceTop = _scrollTopOnNextSnapshot;
        if (forceTop) _scrollTopOnNextSnapshot = false;
        const atBottom = output.scrollHeight - output.scrollTop <= output.clientHeight + 60;
        const place = forceTop ? null : _outputPlace(atBottom);
        _applyCursorFields(msg);
        renderOutput(msg.data, !forceTop && atBottom);
        if (forceTop) output.scrollTop = 0;
        _restoreOutputPlace(place);
      }
    } else if (msg.type === 'update') {
      if (msg.pane_id !== currentPane) return;
      // Rebuilds every line's DOM node -- never while the user has an
      // active selection or gesture in the pane (see _outputFrozen above).
      if (_outputFrozen()) { _deferredForSelection = true; return; }
      _currentPaneRawLines = msg.data.split('\n');
      _currentPaneRawLinesFor = msg.pane_id;
      const atBottom = output.scrollHeight - output.scrollTop <= output.clientHeight + 60;
      const place = _outputPlace(atBottom);
      _applyCursorFields(msg);
      renderOutput(msg.data, atBottom);
      _restoreOutputPlace(place);
    } else if (msg.type === 'delta') {
      // Bandwidth-saving alternative to 'update': the daemon detected the
      // pane's captured window merely slid down (old lines dropped off
      // the top, new ones appended at the bottom -- the common case for
      // a busy pane) and sent only what changed instead of the whole
      // capture. If our tracked raw lines don't match this pane (e.g. a
      // stream from a pane we've since switched away from), there's
      // nothing safe to apply the delta to -- drop it and wait for the
      // next snapshot rather than rendering a mismatched reconstruction.
      if (msg.pane_id !== currentPane || _currentPaneRawLinesFor !== msg.pane_id) return;
      // Same reasoning as 'update': renderOutput below rebuilds every line's
      // DOM node regardless of whether it actually changed. Applying the
      // drop/append to _currentPaneRawLines while skipping the render would
      // leave it byte-correct but the DOM stale, so skip both together and
      // let the forced resubscribe once the selection clears fetch a fresh
      // snapshot -- an incremental delta computed against content the client
      // never rendered is not safe to trust on its own.
      if (_outputFrozen()) { _deferredForSelection = true; return; }
      _currentPaneRawLines = _currentPaneRawLines.slice(msg.drop).concat(msg.append);
      const atBottom = output.scrollHeight - output.scrollTop <= output.clientHeight + 60;
      const place = _outputPlace(atBottom);
      _applyCursorFields(msg);
      renderOutput(_currentPaneRawLines.join('\n'), atBottom);
      if (place) _restoreOutputPlace(place, place.idx - msg.drop);
    } else if (msg.type === 'patch') {
      if (msg.pane_id !== currentPane) return;
      // _lines null (pane switch, cache restore, loading placeholder) means
      // a subscribe is already in flight for this pane; this is a straggler
      // from the stream that subscribe is about to replace, so drop it and
      // wait for the snapshot rather than cancel-restarting the new stream.
      if (!_lines) return;
      // applyPatch only touches the lines its ops actually cover, but a
      // selection anchored on exactly one of those lines is still destroyed
      // when its span is replaced -- skip whenever a selection is active
      // rather than trying to tell whether this particular patch would
      // reach it; the next patch after the selection clears will fail its
      // own bounds check against the now-stale _lines and self-heal via the
      // same resubscribe path below, so nothing further is needed here.
      if (_outputFrozen()) { _deferredForSelection = true; return; }
      const atBottom = output.scrollHeight - output.scrollTop <= output.clientHeight + 60;
      const place = _outputPlace(atBottom);
      _applyCursorFields(msg);
      // A mismatching op means the line state diverged from the daemon's;
      // resubscribe for a fresh snapshot.
      if (!applyPatch(msg.ops || [])) {
        _lines = null;
        if (++_patchMismatchStreak >= _PATCH_MISMATCH_LIMIT) _patchDisabled = true;
        _requestSubscribe(currentPane);
        return;
      }
      _patchMismatchStreak = 0;
      if (atBottom) scrollOutputToBottom();
      else _restoreOutputPlace(place);
    } else if (msg.type === 'cursor') {
      // Bare cursor move: no data field, so no re-render (an innerHTML
      // replacement would destroy any in-progress text selection); just
      // re-anchor the ghost. Fields absent means the daemon could not read
      // the cursor and the end-of-buffer fallback applies.
      if (msg.pane_id !== currentPane) return;
      _applyCursorFields(msg);
      // Moving the ghost splits and re-joins the cursor line's text node,
      // which collapses a selection ending in it; catch up after.
      if (_outputFrozen()) { _deferredForSelection = true; return; }
      _ghostSync();
    } else if (msg.type === 'error') {
      // A definitive failure -- restore immediately rather than waiting out
      // _PENDING_SEND_TIMEOUT_MS, since there's nothing left to wait for.
      if (_pendingSend && msg.req_id === _pendingSend.reqId) _restorePendingSend();
      setStatus(`error: ${msg.message}`, 'error');
    }
  };
}

function send(obj) {
  // A named key (Enter, an arrow, C-x) moves pane state in ways an appended
  // character cannot describe; standing predictions are void. Rewrite
  // BSpaces from streamDirectInput are exempt: they reconciled _pending
  // themselves before sending.
  if (obj && obj.type === 'send_keys' && obj.literal === false && !_predSquelch) {
    _predClear(false);
  }
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
  if (p.named && p.title) return p.title;
  const title = isDefaultPaneTitle(p.title) ? '' : p.title;
  return title || p.command || 'shell';
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
  // Reading aloud belongs to the pane it was started in.
  ttsStop();
  // Save departing pane's rendered content + scroll position + keyboard mode.
  // The composition ghost is view state, never pane content: strip it before
  // the innerHTML read so it cannot be cached and replayed on a later switch.
  _ghostRemove();
  // Streamed text stays in the departing pane, un-reconciled: the pane keeps
  // whatever was streamed so far. The surviving draft (the box's text lives
  // across switches) re-streams in full to the arriving pane on the next
  // input event, so the record resets here.
  _streamed = '';
  // Predictions are per pane: the arriving pane owes no echo for characters
  // streamed into the departing one, and its echo behavior is unknown.
  _predClear(true);
  // Cached HTML carries no cursor; end-of-buffer anchor until the snapshot.
  _cursorFromEnd = null;
  _cursorX = null;
  // The DOM is about to hold cached HTML or the loading placeholder, neither
  // of which the line model describes; the arriving pane's snapshot rebuilds
  // it, and a patch racing that snapshot is dropped instead of spliced.
  _lines = null;
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
    // setKeybarVisible ran before the cached HTML landed; re-attach the
    // ghost onto the restored content (the box's text survives pane
    // switches). On a cache miss there is only the loading placeholder to
    // anchor to, so wait for the snapshot render to attach it instead.
    _ghostSync();
  } else {
    output.textContent = 'loading…';
    _paneLoading = true;
  }

  // See _requestSubscribe: sends immediately if nothing's outstanding
  // (the common case -- an isolated tap costs no added latency), or
  // remembers this as the latest target and sends it the instant the
  // outstanding subscribe's snapshot arrives, if one is already in
  // flight. Everything above (cache restore, position label, breadcrumb)
  // still updates instantly on every tap regardless.
  _requestSubscribe(paneId);
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
    ctxName.textContent = 'shell';
    return;
  }
  // session / window, plus / pane only when the window is split.
  ctxName.append(
    _ctxSeg(sessionLabel(s), 'ctx-seg-session', 'sessions'),
    _ctxSegSep(),
    _ctxSeg(w.name, 'ctx-seg-window', 'windows'),
  );
  if (livePanesInWindow(w).length > 1) {
    ctxName.append(_ctxSegSep(), _ctxSeg(paneDisplayName(p), 'ctx-seg-pane', 'panes'));
  }
}

// ── output rendering ───────────────────────────────────────────────────────
function scrollOutputToBottom() {
  // Scroll mode owns the viewport: a one-line j/k step stays inside the 60px
  // bottom-snap zone, so re-anchoring here would yank a live pane back down.
  if (_scrollMode) return;
  requestAnimationFrame(() => {
    output.scrollTop = _bottomScrollTop();
  });
}

function _bottomScrollTop() {
  const el = _bottomContentLine();
  return el ? el.offsetTop + el.offsetHeight - output.clientHeight
            : output.scrollHeight;
}

// ── selecting text in #output ─────────────────────────────────────────────
// #output is a scroller inside a page that doesn't scroll, with the header
// above it and the input bar below. On an ordinary page the document itself
// scrolls, and Chrome's selection handles behave; here two things go wrong
// when a handle is dragged toward either edge (easy: a line is ~36 device
// px), both seen on real Android Chrome (the mobile/tests/android harness;
// headless Chromium has no selection handles):
// - Chrome autoscrolls #output to follow the handle, and in a nested
//   scroller that's a jump straight to the end, not a glide, so the
//   selection runs to the bottom (or top) of the scrollback at once.
// - Past the edge, Chrome puts that end on the header or the input bar,
//   and the selection takes in everything between in document order: all
//   the hidden scrollback above the visible lines, or everything below.
// So while a selection exists:
// - #output only scrolls under a finger (or its fling). A scroll with no
//   finger on #output is Chrome's handle autoscroll -- a handle drag sends
//   the page no touch events at all -- and is undone, along with the
//   selection change that came with it.
// - An end that moves outside the visible lines is pulled back to the
//   first/last visible line, and each further drag past the edge scrolls
//   one line, so a longer selection still works, at a pace a finger can
//   follow.
// - #output never scrolls into the blank rows tmux keeps below the cursor
//   (or past wherever it already was when the selection started).
const SEL_EDGE_STEP_MS = 120;
const OUTPUT_FLING_MS = 1500;
const OUTPUT_FLING_SETTLE_MS = 250;
let _selEnds = null;        // [anchorNode, anchorOffset, focusNode, focusOffset]
let _selLastMoved = null;   // 'anchor' | 'focus': the end being dragged
let _selEdgeStepAt = 0;
let _selScrollPos = null;   // the scrollTop #output may have while selecting
let _selScrollCap = null;   // and the furthest down it may go
let _outputTouching = false;
let _outputTouchUntil = 0;

output.addEventListener('touchstart', () => { _outputTouching = true; }, { passive: true });
for (const t of ['touchend', 'touchcancel']) {
  output.addEventListener(t, e => {
    if (e.touches.length) return;
    _outputTouching = false;
    _outputTouchUntil = performance.now() + OUTPUT_FLING_MS;
    _outputHoldFor(OUTPUT_GESTURE_GRACE_MS);
  }, { passive: true });
}

// A finger on #output holds the pane still (_outputFrozen), through a pan
// too: Chrome cancels the pointer once it takes a touch over for scrolling,
// but touch events last until the finger lifts. Once it lifts, a fling
// still moving the pane keeps the hold until it settles.
function _outputHoldFor(ms) {
  const until = performance.now() + ms;
  if (until <= _outputGestureUntil) return;
  _outputGestureUntil = until;
  setTimeout(_resyncIfThawed, ms + 20);
}
output.addEventListener('scroll', () => {
  if (!_outputTouching && performance.now() < _outputTouchUntil) {
    _outputHoldFor(OUTPUT_FLING_SETTLE_MS);
  }
}, { passive: true });

// Our own scrolls while selecting go through here, so the scroll listener
// doesn't take them for Chrome's.
function _selScrollTo(top) {
  output.scrollTop = top;
  _selScrollPos = output.scrollTop;
}

function _visibleLineSpans() {
  const kids = output.children;
  if (!kids.length) return null;
  const box = output.getBoundingClientRect();
  // Binary search: line spans are in visual order.
  const firstBelow = y => {
    let lo = 0, hi = kids.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (kids[mid].getBoundingClientRect().bottom > y) hi = mid; else lo = mid + 1;
    }
    return lo;
  };
  const first = Math.min(firstBelow(box.top + 2), kids.length - 1);
  const last = Math.max(first, firstBelow(box.bottom - 2) - 1);
  return [kids[first], kids[Math.min(last, kids.length - 1)]];
}

// -1 if (node, offset) is before span's content, 1 if after, 0 inside.
function _cmpToSpan(span, node, offset) {
  const r = document.createRange();
  r.selectNodeContents(span);
  try { return r.comparePoint(node, offset); } catch (_) { return 0; }
}

function _clampSelectionEnd(sel, which) {
  const node = which === 'focus' ? sel.focusNode : sel.anchorNode;
  const off = which === 'focus' ? sel.focusOffset : sel.anchorOffset;
  let vis = _visibleLineSpans();
  if (!vis || !node) return false;
  const step = performance.now() - _selEdgeStepAt > SEL_EDGE_STEP_MS;
  let to = null;
  if (_cmpToSpan(vis[0], node, off) < 0) {
    if (step && output.scrollTop > 0) {
      _selEdgeStepAt = performance.now();
      _selScrollTo(output.scrollTop - (vis[0].getBoundingClientRect().height || 16));
      vis = _visibleLineSpans();
    }
    to = [vis[0], 0];
  } else if (_cmpToSpan(vis[1], node, off) > 0) {
    const down = output.scrollTop + (vis[1].getBoundingClientRect().height || 16);
    if (step && down <= _selScrollCap) {
      _selEdgeStepAt = performance.now();
      _selScrollTo(down);
      vis = _visibleLineSpans();
    }
    to = [vis[1], vis[1].childNodes.length];
  }
  if (!to) return false;
  if (which === 'focus') sel.setBaseAndExtent(sel.anchorNode, sel.anchorOffset, to[0], to[1]);
  else sel.setBaseAndExtent(to[0], to[1], sel.focusNode, sel.focusOffset);
  return true;
}

function _outputFingerScroll() {
  return _outputTouching || performance.now() < _outputTouchUntil;
}

// Chrome scrolled #output on its own: following a dragged handle. Its jump
// can come from mid-screen and lands the dragged end somewhere arbitrary
// (seen: dragging up from mid-screen jumped to the last line), so undo the
// scroll and put the selection back as it was. If the dragged end was
// already on the first/last visible line, the drag is heading off that
// edge: step one line that way, so a longer selection still extends.
function _selUndoForeignScroll(sel) {
  _selScrollTo(_selScrollPos);
  if (!_selEnds) return;
  sel.setBaseAndExtent(..._selEnds);
  const node = _selLastMoved === 'anchor' ? _selEnds[0] : _selEnds[2];
  const el = node && (node.nodeType === Node.TEXT_NODE ? node.parentElement : node);
  const line = el && el.closest('#output > span');
  const vis = _visibleLineSpans();
  if (!line || !vis || performance.now() - _selEdgeStepAt <= SEL_EDGE_STEP_MS) return;
  if (line === vis[0] && output.scrollTop > 0) {
    _selEdgeStepAt = performance.now();
    _selScrollTo(output.scrollTop - (line.getBoundingClientRect().height || 16));
  } else if (line === vis[1]) {
    const down = output.scrollTop + (line.getBoundingClientRect().height || 16);
    if (down <= _selScrollCap) {
      _selEdgeStepAt = performance.now();
      _selScrollTo(down);
    }
  }
}

document.addEventListener('selectionchange', () => {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed
      || !(output.contains(sel.anchorNode) || output.contains(sel.focusNode))) {
    _selEnds = _selLastMoved = _selScrollPos = _selScrollCap = null;
    return;
  }
  // The selection change that comes with one of Chrome's jumps can arrive
  // before the scroll event does.
  if (_selScrollPos !== null && !_outputFingerScroll()
      && Math.abs(output.scrollTop - _selScrollPos) > 1) {
    _selUndoForeignScroll(sel);
    return;
  }
  // An end outside #output (on the header or the input bar) is never one a
  // person meant; pull it back whichever end Chrome says moved (crossing
  // the edge, it can report both, swapping base and extent).
  const outside = ['anchor', 'focus'].filter(w =>
    !output.contains(w === 'anchor' ? sel.anchorNode : sel.focusNode));
  if (outside.length) {
    _selLastMoved = outside[0];
    for (const w of outside) _clampSelectionEnd(sel, w);
    return;
  }
  const now = [sel.anchorNode, sel.anchorOffset, sel.focusNode, sel.focusOffset];
  const prev = _selEnds;
  _selEnds = now;
  // A brand-new selection (long press, double tap) is where the finger is;
  // only an end that moved on its own is a drag.
  if (!prev) {
    _selScrollPos = output.scrollTop;
    _selScrollCap = Math.max(_bottomScrollTop(), output.scrollTop);
    return;
  }
  const anchorMoved = prev[0] !== now[0] || prev[1] !== now[1];
  const focusMoved = prev[2] !== now[2] || prev[3] !== now[3];
  if (anchorMoved === focusMoved) return;
  _selLastMoved = focusMoved ? 'focus' : 'anchor';
  _clampSelectionEnd(sel, _selLastMoved);
});

output.addEventListener('scroll', () => {
  if (_selScrollPos === null || !_hasOutputSelection()) return;
  if (!_outputFingerScroll()) {
    if (Math.abs(output.scrollTop - _selScrollPos) > 1) {
      _selUndoForeignScroll(window.getSelection());
    }
    return;
  }
  if (output.scrollTop > _selScrollCap + 1) _selScrollTo(_selScrollCap);
  else _selScrollPos = output.scrollTop;
}, { passive: true });

// The #output child "scrolled to bottom" should land on. capture-pane keeps
// trailing blank screen rows (tmux_cursor()'s doc comment in _daemon.py) --
// real content whenever the pane is taller than what it's currently showing
// (any short command in a tall terminal), but nothing worth scrolling past:
// scrolling to the buffer's literal end pushes the actual prompt/output off
// the top of the viewport, which is the "I have to scroll up to find my
// output" bug this fixes. Walk back over trailing blank lines to the last
// one with real content -- but never further than the cursor's own line
// (same cursor_from_end mapping _ghostSync() uses): a full-screen app (vim,
// htop) can legitimately draw a status/command line below where the cursor
// sits, and that must stay in view same as before.
function _bottomContentLine() {
  const kids = output.children;
  if (!kids.length) return null;
  if (!_lines) return kids[kids.length - 1];   // no snapshot model; old behavior

  // #output always mirrors _lines 1:1, except a capture ending in '\n'
  // (the common case) splits into one extra empty trailing element that
  // is not a captured row at all -- just split()'s artifact of the final
  // newline, carrying no line of its own in the DOM.
  const endsNl = output.textContent.endsWith('\n');
  let last = kids.length - 1 - (endsNl ? 1 : 0);
  if (last < 0) return kids[kids.length - 1];

  while (last > 0 && _lines[last] === '') last--;

  if (Number.isInteger(_cursorFromEnd) && _cursorFromEnd >= 0) {
    const realLast = kids.length - 1 - (endsNl ? 1 : 0);
    const cursorIdx = realLast - _cursorFromEnd;
    if (cursorIdx > last && cursorIdx <= realLast) last = cursorIdx;
  }
  return kids[last];
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

// Schemes allowed in an OSC 8 hyperlink target. Deliberately an allowlist,
// not a blocklist: pane content is whatever a remote command chose to
// print, so a scheme this doesn't recognize (javascript:, data:, vscode:,
// file: -- anything that could act rather than just navigate) renders as
// plain text instead of a clickable target. http/https covers the vast
// majority of real hyperlink-emitting tools (ls --hyperlink, git, ripgrep);
// mailto is the other common one (git blame/log author addresses).
const _SAFE_HYPERLINK_SCHEMES = /^(https?|mailto):/i;

function _sanitizeHyperlinkUrl(url) {
  return _SAFE_HYPERLINK_SCHEMES.test(url) ? url : null;
}

// Convert ANSI SGR escape codes and OSC 8 hyperlinks to HTML.
// Handles: 16/256/truecolor fg+bg, bold, italic, underline, OSC 8 links.
// Other sequences discarded.
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
  let linkHref = null; // current OSC 8 target, or null when not inside a link
  let spanCss = null, spanLinkHref = null, out = '';

  function css() {
    const p = [];
    if (fg) p.push(`color:${fg}`);
    if (bg) p.push(`background:${bg}`);
    if (bold) p.push('font-weight:bold');
    if (italic) p.push('font-style:italic');
    if (ul) p.push('text-decoration:underline');
    return p.join(';');
  }
  function esc(s) {
    // Also escapes " (harmless in text content, and required wherever esc()
    // feeds the href="..." attribute below -- an unescaped quote in a URL
    // would otherwise close the attribute early and let the rest of the
    // URL string inject arbitrary markup).
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function emit(s) {
    if (!s) return;
    // The <a> wrapper and the <span> style are independent axes (a link's
    // text can still change color mid-link), so each is opened/closed on
    // its own change -- but a span must close before its enclosing <a>
    // does, so a link-boundary change always closes the span first.
    if (linkHref !== spanLinkHref) {
      if (spanCss !== null) { out += '</span>'; spanCss = null; }
      if (spanLinkHref !== null) out += '</a>';
      if (linkHref !== null) {
        // title shows the real destination on long-press/hover: OSC 8 lets the
        // link text say one host while the href goes to another.
        out += `<a href="${esc(linkHref)}" title="${esc(linkHref)}" target="_blank" rel="noopener noreferrer">`;
      }
      spanLinkHref = linkHref;
    }
    const c = css();
    if (c !== spanCss) {
      if (spanCss !== null) out += '</span>';
      if (c) out += `<span style="${c}">`;
      spanCss = c || null;
    }
    out += esc(s);
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
  // OSC 8 body looks like "8;params;URI" -- an empty URI (just "8;;") is
  // the spec's own close marker. params (e.g. id=NNN, used by some
  // emitters to associate a link split across lines) is unused here: this
  // renders each captured OSC 8 span independently, which needs no such
  // grouping.
  function osc8(body) {
    const rest = body.slice(2); // drop leading "8"
    const semi = rest.indexOf(';');
    if (semi === -1) return;
    const uri = rest.slice(semi + 1);
    linkHref = uri ? _sanitizeHyperlinkUrl(uri) : null;
  }
  const TOK = /([^\x1b]+)|\x1b(?:\[([0-9;]*)([A-Za-z])|\]([^\x07\x1b]*)(?:\x07|\x1b\\)|(.))/g;
  for (const m of text.matchAll(TOK)) {
    if (m[1])              emit(m[1]);
    else if (m[3] === 'm') sgr(m[2] ? m[2].split(';').map(Number) : [0]);
    else if (m[4] !== undefined && m[4].charAt(0) === '8') osc8(m[4]);
  }
  if (spanCss !== null) out += '</span>';
  if (spanLinkHref !== null) out += '</a>';
  return out;
}

// One span per capture line, so a patch re-renders only its lines and never
// touches the nodes (or an in-progress text selection) of untouched ones.
// The newline lives inside the span: output.textContent reads back as the
// exact capture, which the ghost anchor depends on (it counts newlines).
// Per-line ansiToHtml resets SGR state at each line, which is safe because
// tmux capture-pane -e re-emits the full attribute set at the start of every
// line and resets at its end (verified on tmux 3.7b): capture lines are
// self-contained, no color leaks across them.
function _renderLine(line, withNewline) {
  const span = document.createElement('span');
  span.innerHTML = ansiToHtml(line) + (withNewline ? '\n' : '');
  return span;
}

function renderOutput(text, scrollToBottom) {
  output.className = '';
  _paneLoading = false;
  _lines = text.split('\n');
  const frag = document.createDocumentFragment();
  for (let i = 0; i < _lines.length; i++) {
    frag.appendChild(_renderLine(_lines[i], i < _lines.length - 1));
  }
  // Child replacement drops the composition ghost; re-attach before the
  // scroll so scrollHeight includes it.
  output.replaceChildren(frag);
  _ghostSync();
  _ttsAfterRender();
  if (scrollToBottom) scrollOutputToBottom();
}

// A re-render while scrolled up must leave the same lines under the
// reader's eyes. The capture is a window of the pane's last N lines, so on
// a busy pane lines drop off the top as new ones arrive, and a kept
// scrollTop would slide the text up under them. Chrome's scroll anchoring
// already holds the view (tests/android checks it); this is for browsers
// without it, and a no-op where anchoring did its job. Note the first
// visible line (and the two after it, to tell repeats apart) before the
// render; after it, find that line again -- at hint if the caller knows
// where it went, else the nearest match at or above its old index, since
// lines only leave from the top -- and scroll it back to where it was.
function _outputPlace(atBottom) {
  if (atBottom || !_lines || _paneLoading) return null;
  const vis = _visibleLineSpans();
  if (!vis) return null;
  const idx = Array.prototype.indexOf.call(output.children, vis[0]);
  if (idx < 0 || idx >= _lines.length) return null;
  return { idx, key: _lines.slice(idx, idx + 3).join('\n'),
           top: vis[0].getBoundingClientRect().top - output.getBoundingClientRect().top };
}

function _restoreOutputPlace(place, hint) {
  if (!place || !_lines) return;
  const at = i => i >= 0 && i < _lines.length
    && _lines.slice(i, i + 3).join('\n') === place.key;
  let i = Number.isInteger(hint) && at(hint) ? hint : -1;
  for (let j = Math.min(place.idx, _lines.length - 1); i < 0 && j >= 0; j--) {
    if (at(j)) i = j;
  }
  const el = i >= 0 && output.children[i];
  if (!el) return;
  const moved = el.getBoundingClientRect().top - output.getBoundingClientRect().top - place.top;
  if (Math.abs(moved) >= 1) output.scrollTop += moved;
}

// Apply daemon line-diff ops to _lines and the matching #output spans.
// Returns false on any shape/bounds mismatch, before touching either, so the
// caller can resubscribe for a fresh snapshot instead of guessing.
function applyPatch(ops) {
  for (const op of ops) {
    if (!op || !Number.isInteger(op.start) || !Number.isInteger(op.end)
        || op.start < 0 || op.end < op.start || op.end > _lines.length
        || (op.op !== 'delete' && !Array.isArray(op.lines))) return false;
  }
  // Ghost out first: its splitText fragments merge back and its climb-out
  // case (a direct #output child) would break the child index = line index
  // mapping the splices below rely on.
  _ghostRemove();
  // Ops are ascending and non-overlapping, so only the final op can reach
  // the old tail; note it now, before the splices move the end.
  const oldLen = _lines.length;
  const tailTouched = ops.length > 0
    && ops[ops.length - 1].end === oldLen;
  // Ops carry old-array indices in ascending start order; applying from the
  // end keeps every earlier op's indices valid.
  for (let k = ops.length - 1; k >= 0; k--) {
    const op = ops[k];
    const lines = op.lines || [];
    // A pure append after the old tail: difflib emits an insert with
    // start === end === old length when the trailing '' is absorbed into a
    // longer equal block (['a',''] -> ['a','','x','']). The old tail span,
    // rendered without '\n', survives mid-buffer, so give it one while
    // children indices are still old-array indices; the tailTouched
    // re-render below only fixes the new last element.
    if (k === ops.length - 1 && op.start === op.end && op.end === oldLen
        && op.start > 0) {
      output.replaceChild(_renderLine(_lines[op.start - 1], true),
                          output.children[op.start - 1]);
    }
    _lines.splice(op.start, op.end - op.start, ...lines);
    for (let i = op.end - 1; i >= op.start; i--) output.children[i].remove();
    const anchor = output.children[op.start] || null;
    for (const line of lines) output.insertBefore(_renderLine(line, true), anchor);
  }
  // Inserted spans always carry '\n'; when the ops touched the tail the
  // element now in last position has one too many (an inserted final line,
  // or a delete promoting a mid-buffer span). An untouched tail keeps its
  // ends aligned (equal suffix) and its selection endpoints, so re-render
  // only when the ops reached it: textContent comparison would strip SGR
  // escapes and mismatch every styled final line.
  const lastEl = output.lastElementChild;
  if (tailTouched && lastEl) {
    output.replaceChild(_renderLine(_lines[_lines.length - 1], false), lastEl);
  }
  _ghostSync();
  _ttsAfterRender();
  return true;
}

// ── send keys ─────────────────────────────────────────────────────────────
function activeInput() { return kbdMode === 2 ? pwdInput : cmdInput; }

// Pending-send tracking: a send_keys transmitted but not yet confirmed (or
// denied) by the daemon. Only the single most recent send is tracked here --
// this is recovery for the reported case (one long prompt typed and
// submitted on a slow/flaky link, no confirmation for seconds to minutes,
// sometimes silently dropped), not a general offline queue. Recovery is
// always a manual, explicit tap on the restore button, never automatic: on
// a link this uncertain there's no way to tell "lost" apart from "still in
// flight," and silently replaying could double-execute a command that
// actually did land the first time.
let _pendingSend = null; // { reqId, text, inp } or null
let _pendingSendId = 0;
let _pendingSendTimer = null;
const _PENDING_SEND_TIMEOUT_MS = 12000;

function _clearPendingSend() {
  _pendingSend = null;
  clearTimeout(_pendingSendTimer);
  _pendingSendTimer = null;
  pendingSendBanner.style.display = 'none';
}

function _showPendingSendBanner() {
  if (!_pendingSend) return;
  pendingSendBanner.style.display = 'flex';
}

function _restorePendingSend() {
  if (!_pendingSend) return;
  const { text, inp } = _pendingSend;
  // Only restore into an empty box -- don't clobber anything the user has
  // typed since.
  if (!inp.value) {
    inp.value = text;
    if (inp === cmdInput) {
      cmdInput.style.height = 'auto';
      cmdInput.style.height = Math.min(cmdInput.scrollHeight, 160) + 'px';
    }
    inp.focus();
  }
  _clearPendingSend();
}
pendingSendRestore.addEventListener('click', _restorePendingSend);

function sendKeys() {
  const inp  = activeInput();
  const keys = inp.value;
  if (!currentPane) return;
  // Streaming may already have delivered a prefix of the box to the pane,
  // including a draft left over from hiding the bar mid-composition; a Send
  // must reconcile the pane to the box, never resend the prefix. The
  // reconcile runs before the emptiness check: a fully deleted box still
  // BSpaces the stale streamed draft off the prompt. The full streaming
  // diff runs so an edited draft BSpaces the stale streamed suffix before
  // the tail goes out; a bare Enter then commits, and after the reconcile
  // an emptied or fully streamed draft sends just that Enter.
  //
  // Not tracked as a pending-send: the text itself already reached the pane
  // character by character as it streamed (same link reliability as any
  // other message), so only this bare Enter is actually outstanding. If it
  // gets lost, the typed text is still visible sitting in the pane's
  // prompt -- a visible, self-evident cue to just press Enter again --
  // unlike the fully-invisible dropped submission the ack/recovery
  // machinery below exists for. Restoring "keys" into the box on a lost
  // Enter would also just duplicate already-delivered text.
  if (inp === cmdInput && _streamed) {
    streamDirectInput(inp);
    send({ type: 'send_keys', pane_id: currentPane, keys: 'Enter', enter: false, literal: false });
    _streamed = '';
  } else if (!keys) {
    // Empty box: Send is a bare Enter, for answering "1/2/3", yes/no and
    // "press Enter" prompts without typing a character first. Not from the
    // password box, where a stray tap would submit an empty password.
    // Untracked, like the streamed-draft Enter above.
    if (inp !== cmdInput) return;
    send({ type: 'send_keys', pane_id: currentPane, keys: 'Enter', enter: false, literal: false });
    return;
  } else {
    const reqId = `s${++_pendingSendId}`;
    _pendingSend = { reqId, text: keys, inp };
    clearTimeout(_pendingSendTimer);
    _pendingSendTimer = setTimeout(_showPendingSendBanner, _PENDING_SEND_TIMEOUT_MS);
    send({ type: 'send_keys', pane_id: currentPane, keys, enter: true, req_id: reqId });
    _streamed = '';
  }
  inp.value = '';
  if (inp === cmdInput) {
    // '' not 'auto': an inline height would override the direct-mode
    // collapsed-sliver CSS; without the bar both compute the same.
    cmdInput.style.height = '';
  }
  // A Send tap mid-composition empties the box; the ghost must follow.
  _ghostSync();
}

// ── events ─────────────────────────────────────────────────────────────────
xyzLabel.addEventListener('click', () => send({ type: 'list_sessions' }));
cmdInput.addEventListener('keydown', e => {
  // The Enter that commits an IME composition arrives here with
  // isComposing=true (or as Android keyCode 229); it must not reach the pane.
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    // In direct mode Enter consumes the streamed text but _streamed keeps
    // recording it: resetting would re-stream the word onto the fresh
    // prompt. Known edge (like the hide-bar one in setKeybarVisible): a
    // post-Enter autocorrect rewrite BSpaces the new prompt instead.
    keybarVisible() ? sendNamedKey('Enter') : sendKeys();
  } else if (e.key === 'Backspace' && keybarVisible() && !cmdInput.value) {
    e.preventDefault();
    sendNamedKey('BSpace');
  }
});
cmdInput.addEventListener('input', () => {
  // No auto-grow while the box is invisible in direct mode: a second row
  // would shrink the pane and shift the ghost the user is watching.
  if (keybarVisible()) return;
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

// ── keyboard mode popup ($_ / Aa / **) ─────────────────────────────────────
// 0 = terminal ($_)   no spell-check, no autocorrect
// 1 = text     (Aa)   spell-check + autocorrect on
// 2 = password (**)   type="password" input — keyboard does not learn text
// Tapping the button opens a 3-way popup (like the ⎋ button) rather than
// cycling on tap: with three real choices a popup is faster to land on the
// one you want and shows what the other two are, instead of tap-tap-tap
// past states you don't want to confirm each is the right one.
let kbdMode = 0;
const kbdModePopupButtons = {
  0: document.getElementById('kbdmode-popup-terminal'),
  1: document.getElementById('kbdmode-popup-text'),
  2: document.getElementById('kbdmode-popup-password'),
};

function applyKbdMode() {
  const inPwd = kbdMode === 2;
  cmdInput.style.display = inPwd ? 'none' : '';
  pwdInput.style.display = inPwd ? 'block' : 'none';
  // Drives the direct-mode row collapse in the CSS: while the key bar is on
  // the input row leaves the layout so the pane gets its space, except in
  // password mode, whose visible row the collapse rules exempt via this
  // class. Every keybar/mode transition funnels through here.
  document.body.classList.toggle('kbd-pwd', inPwd);

  // Direct mode needs raw keystrokes: under spell-check Gboard composes
  // plain typing and only commits whole words, so force terminal attributes
  // while the key bar is visible; hiding it restores the user's mode.
  const direct = keybarVisible();

  if (kbdMode === 1) {
    cmdInput.setAttribute('spellcheck', direct ? 'false' : 'true');
    cmdInput.setAttribute('autocorrect', direct ? 'off' : 'on');
    cmdInput.setAttribute('autocapitalize', direct ? 'none' : 'sentences');
    btnKbdMode.textContent = 'Aa';
    btnKbdMode.title = 'Text mode: tap to choose a mode';
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
    btnKbdMode.title = 'Terminal mode: tap to choose a mode';
    btnKbdMode.style.color = '';
  } else {
    btnKbdMode.textContent = '**';
    btnKbdMode.title = 'Password mode: tap to choose a mode';
    btnKbdMode.style.color = 'var(--accent)';
  }
  for (const [mode, btn] of Object.entries(kbdModePopupButtons)) {
    btn.classList.toggle('current', Number(mode) === kbdMode);
  }
  // Non-password direct mode hides the row's button column, so the bar
  // carries a mirror of the mode button; keep the two in sync here.
  keybarKbdMode.textContent = btnKbdMode.textContent;
  keybarKbdMode.title = btnKbdMode.title;
  keybarKbdMode.style.color = btnKbdMode.style.color;
  // Every keybar/mode transition funnels through here, so this one call
  // covers show (attach), hide (remove, ahead of the blur+refocus dance so
  // the ghost never flashes) and the switch to password mode (remove: pwd
  // stays a visible box and never ghosts).
  _ghostSync();
  scrollOutputToBottom();
}

function setKbdMode(mode) {
  // Tapping the already-active mode in the popup is a no-op: without this
  // guard the wrap default below would clobber a manual Wrap toggle.
  if (mode === kbdMode) return;
  kbdMode = mode;
  if (currentPane) _saveKbdMode(currentPane, kbdMode);
  // Aa defaults to wrapped text, Terminal/Password to unwrapped -- applied
  // once here, on the mode switch itself, not forced on every render. A
  // manual Wrap toggle afterward (escape popup) still sticks until the
  // keyboard mode is changed again.
  wrapOn = (kbdMode === 1);
  if (currentPane) {
    _saveWrap(currentPane, wrapOn);
    _requestSubscribe(currentPane);
  }
  applyWrap();
  applyKbdMode();
  // blur + refocus so Android keyboard re-evaluates input type/spellcheck
  const inp = activeInput();
  inp.blur();
  setTimeout(() => inp.focus(), 50);
}

function showKbdModePopup(anchor = btnKbdMode) {
  hideEscapePopup();
  const rect = anchor.getBoundingClientRect();
  kbdModePopup.style.display = 'flex';
  kbdModePopup.style.right   = (window.innerWidth - rect.right) + 'px';
  kbdModePopup.style.bottom  = (window.innerHeight - rect.top + 8) + 'px';
}

function hideKbdModePopup() {
  kbdModePopup.style.display = 'none';
}

btnKbdMode.addEventListener('click', e => {
  e.stopPropagation();
  kbdModePopup.style.display === 'none' ? showKbdModePopup() : hideKbdModePopup();
});
// Same popup from the key bar, anchored to the bar's own button: the row's
// button is display:none while the row is collapsed, so its rect is unusable
// as an anchor there. pointerdown preventDefault keeps focus in the text box
// like the other bar buttons.
keybarKbdMode.addEventListener('click', e => {
  e.stopPropagation();
  kbdModePopup.style.display === 'none' ? showKbdModePopup(keybarKbdMode) : hideKbdModePopup();
});
keybarKbdMode.addEventListener('pointerdown', e => e.preventDefault());

kbdModePopupButtons[0].addEventListener('click', () => { setKbdMode(0); hideKbdModePopup(); });
kbdModePopupButtons[1].addEventListener('click', () => { setKbdMode(1); hideKbdModePopup(); });
kbdModePopupButtons[2].addEventListener('click', () => { setKbdMode(2); hideKbdModePopup(); });

// ── escape / ctrl-c popup ─────────────────────────────────────────────────
function showEscapePopup() {
  hideKbdModePopup();
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
// persisted state -- not driven by kbdMode -- but choosing a keyboard mode
// sets a sensible default on the switch (Aa on, Terminal/Password off) so
// picking Aa wraps immediately without a separate manual step; a Wrap
// toggle after that still sticks until the keyboard mode is changed again.
let wrapOn = false;
const escapePopupWrap = document.getElementById('escape-popup-wrap');
const keybarWrap = document.getElementById('keybar-wrap');

function applyWrap() {
  output.style.whiteSpace = wrapOn ? 'pre-wrap' : 'pre';
  // pre-wrap alone only breaks at existing whitespace; one unbroken run
  // with none (a long URL, hash, or path -- exactly what a nonce login
  // link looks like) stays one long line, stretching #output's scrollable
  // width to fit it and leaving a wide dead strip of horizontal scroll
  // behind every other, already-wrapped line. break-word breaks such a
  // run only when it would otherwise overflow, leaving normal wrapping
  // alone everywhere else. Irrelevant (and left at the default) off wrap:
  // 'pre' never wraps at all regardless of this.
  output.style.overflowWrap = wrapOn ? 'break-word' : '';
  // Checkmark prefix like the context picker: on-state must not rely on
  // color alone.
  escapePopupWrap.textContent = (wrapOn ? '✓ ' : '⤶ ') + 'Wrap';
  escapePopupWrap.style.color = wrapOn ? 'var(--accent)' : '';
  // The wrap toggle also lives on the key bar: the ⎋ popup is unreachable
  // while non-password direct mode collapses the input row.
  keybarWrap.textContent = wrapOn ? '✓⤶' : '⤶';
  keybarWrap.title = (wrapOn ? 'Line wrap on' : 'Line wrap off') + ': tap to toggle';
  keybarWrap.style.color = wrapOn ? 'var(--accent)' : '';
}

escapePopupWrap.addEventListener('click', () => {
  wrapOn = !wrapOn;
  if (currentPane) {
    _saveWrap(currentPane, wrapOn);
    // Resubscribe so the snapshot is re-captured with the new join flag.
    _requestSubscribe(currentPane);
  }
  applyWrap();
  scrollOutputToBottom();
  hideEscapePopup();
});
keybarWrap.addEventListener('click', () => escapePopupWrap.click());
keybarWrap.addEventListener('pointerdown', e => e.preventDefault());

document.addEventListener('click', () => { hideEscapePopup(); hideKbdModePopup(); });
document.addEventListener('touchstart', e => {
  if (!escapePopup.contains(e.target) && e.target !== btnEscape) hideEscapePopup();
  if (!kbdModePopup.contains(e.target) && e.target !== btnKbdMode
      && e.target !== keybarKbdMode) hideKbdModePopup();
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
// The direct-mode text box is invisible (see body.keybar-on #cmd in the
// CSS), so it carries no placeholder while the bar is on; the password box
// stays visible and keeps its keyboard-privacy hint.
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
  // Hiding the bar keeps the streamed record: a draft mid-composition stays
  // in the box with its streamed prefix already in the pane, and the trim
  // in sendKeys delivers only the un-streamed tail on the next Send.
  // navigateTo and the sends themselves reset the record.
  cmdInput.placeholder = show ? '' : _cmdPlaceholder;
  pwdInput.placeholder = show ? _directPwdPlaceholder : _pwdPlaceholder;
  // Re-apply so direct mode forces terminal input attributes and hiding
  // the bar restores the user's mode. Also scrolls output to bottom.
  applyKbdMode();
  // Like btnKbdMode: Android keyboards only re-evaluate input attributes on
  // refocus, so toggling the bar while the box is focused needs the same
  // blur + refocus dance for the forced attributes to take effect.
  // Entering direct mode focuses unconditionally: the input row is gone,
  // so the soft keyboard and the ghost caret are the only signs typing is
  // live, and they must appear without a second tap.
  const inp = activeInput();
  const wasFocused = document.activeElement === inp;
  if (wasFocused) inp.blur();
  if (show) {
    // The auto-grow inline height would override the collapsed sliver's
    // CSS height; clear it (direct-mode drains reset it to '' as well).
    cmdInput.style.height = '';
    setTimeout(() => inp.focus(), 50);
  } else if (wasFocused) {
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

// Whole-box drain, now the password-box path only: #cmd streams per input
// event via streamDirectInput below.
function drainDirectInput(inp) {
  const text = inp.value;
  if (!text) return;
  inp.value = '';
  if (inp === cmdInput) cmdInput.style.height = '';
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
      if (rest) {
        send({ type: 'send_keys', pane_id: currentPane, keys: rest, enter: false, literal: true });
        if (inp === cmdInput) _predPush(rest);
      }
      return;
    }
  }
  send({ type: 'send_keys', pane_id: currentPane, keys: text, enter: false, literal: true });
  // Never track the password box: _pending would hold the secret in plain
  // text, and the ghost is hidden in that mode anyway.
  if (inp === cmdInput) _predPush(text);
}

// ── direct-mode composition streaming ──────────────────────────────────────
// Composing keyboards (Gboard, swipe) mutate a draft in the box instead of
// emitting discrete characters. Streaming diffs the box against what already
// reached the pane on every input event: BSpace over the stale suffix, then
// the new tail as literal text, so the pane tracks the draft character by
// character like a real terminal. The box is never touched mid-composition
// (clearing it corrupts the IME draft); compositionend reconciles the
// committed text and clears. Non-composing keyboards degrade to the old
// one-event-one-char drain: the box is empty before each event, so the diff
// is exactly the new text and no BSpace is ever sent.
// Accepted hazard: the BSpaces are real terminal backspaces. Line editors
// delete on them, but vi normal mode and some TUIs treat backspace as
// cursor-left, so an autocorrect rewrite can garble state there. Direct
// mode always streams; there is no toggle.
let _streamed = '';

// Common prefix length of two code-point arrays (callers spread strings so
// no UTF-16 surrogate half is ever compared), shared by the streaming diff and
// the ghost's un-streamed-tail computation so the two cannot drift apart.
// Code points, not UTF-16 units: a rewrite must never backspace through
// half a surrogate pair (same reason the sticky-Ctrl gate splits this way).
function _cpCommonPrefix(a, b) {
  let common = 0;
  while (common < a.length && common < b.length && a[common] === b[common]) common++;
  return common;
}

function streamDirectInput(inp) {
  if (!currentPane) return;
  const value = inp.value;
  if (value === _streamed) return;
  const sent = [..._streamed];
  const cur  = [...value];
  const common = _cpCommonPrefix(sent, cur);
  const nBs = sent.length - common;
  if (nBs > 0) {
    // Rewrite BSpaces first cancel still-pending predictions (the newest
    // chars, never displayed as deleted); BSpaces beyond that erase echoed
    // text, which the ghost cannot represent, so drop the rest and let the
    // server frame show the deletion.
    if (nBs >= _pending.length) _predClear(false);
    else _pending.length -= nBs;
  }
  _predSquelch = true;
  for (let i = sent.length; i > common; i--) {
    send({ type: 'send_keys', pane_id: currentPane, keys: 'BSpace', enter: false, literal: false });
  }
  _predSquelch = false;
  let tail = cur.slice(common).join('');
  // Sticky Ctrl consumes the first un-streamed character, same combining
  // rule as drainDirectInput; the rest streams as literal text. The consumed
  // character still counts as streamed, so a later rewrite over it sends one
  // BSpace, the closest approximation available.
  if (tail && _ctrlArmed) {
    setCtrlArmed(false);
    const first = [...tail][0];
    if (/^[a-z@\[\\\]^_ ]$/i.test(first)) {
      const key = first === ' ' ? 'C-Space' : 'C-' + first.toLowerCase();
      send({ type: 'send_keys', pane_id: currentPane, keys: key, enter: false, literal: false });
      tail = tail.slice(first.length);
    }
  }
  if (tail) {
    send({ type: 'send_keys', pane_id: currentPane, keys: tail, enter: false, literal: true });
    _predPush(tail);
  }
  _streamed = value;
}

// ── predictive local echo ───────────────────────────────────────────────────
// Streamed characters are invisible until the daemon's capture echoes them
// back, a full round trip (~80-100ms on a 36ms-RTT tailnet). Track each
// streamed printable in _pending and mirror it through the composition
// ghost at the cursor cell, so typing renders immediately; the ghost's dim
// dotted-underline style doubles as the provisional cue. Server truth
// always wins: a prediction is dropped the moment the text left of the
// caret ends with it (the echo arrived), or after _PRED_TTL if it never
// does. Display is gated on _predOk, set by the first confirmed echo and
// cleared by an expiry, so a pane that does not echo (vim normal mode, a
// password prompt inside the shell) shows at most one wrong burst and then
// stays quiet until echo provably works again.
let _pending = [];        // {ch, ts} per streamed code point, oldest first
let _predOk = false;      // predictions render only after one confirmed echo
let _predSquelch = false; // rewrite BSpaces already reconciled _pending
let _predTimer = null;
const _PRED_TTL = 1200;   // ms before an unconfirmed prediction is abandoned

function _predClear(dropConfidence) {
  _pending = [];
  if (dropConfidence) _predOk = false;
  if (_predTimer) { clearTimeout(_predTimer); _predTimer = null; }
}

function _predPush(text) {
  const now = Date.now();
  for (const ch of text) {
    // A control character's effect on the line is not an append; anything
    // already pending is now unverifiable.
    if (ch < ' ') { _predClear(false); return; }
    _pending.push({ ch, ts: now });
  }
  _predArm();
}

// Expiry cannot ride on a server message (the failure case is exactly that
// no echo message comes), so it needs its own timer.
function _predArm() {
  if (_predTimer) clearTimeout(_predTimer);
  _predTimer = setTimeout(_ghostSync, _PRED_TTL + 50);
}

function _predConfirm(before) {
  // The longest prefix of _pending that `before` (text left of the caret)
  // now ends with is echo that came back: drop it, and let any hit license
  // rendering the rest. Longest-first so a repeated character confirms as
  // many copies as actually echoed.
  for (let k = _pending.length; k > 0; k--) {
    if (before.endsWith(_pending.slice(0, k).map(p => p.ch).join(''))) {
      _pending.splice(0, k);
      _predOk = true;
      break;
    }
  }
}

// Reconcile against `before`, then fill the ghost with the surviving
// predictions ahead of the un-streamed tail.
function _ghostFill(span, before, tail) {
  _predConfirm(before);
  const pred = _predOk ? _pending.map(p => p.ch).join('') : '';
  span.textContent = pred + tail;
  if (_pending.length) _predArm();
}

// ── direct-mode composition ghost ──────────────────────────────────────────
// While the key bar is on, the text box is out of flow and invisible (the
// body.keybar-on rules in the CSS) and its un-streamed tail is mirrored
// into the pane as ghost text. With streaming this is normally empty
// (characters reach the pane as they are typed); it shows text only for a
// draft that predates streaming, e.g. box content left from before the key
// bar was turned on. The span
// attaches even with no tail: its CSS ::after is the block caret that marks
// where typing lands (filled while the box is focused, hollow when the
// keyboard was dismissed), so the pane is the only prompt. Rendering is
// strictly read-only; reading inp.value mid-composition is safe (only
// clearing it is not). The anchor is the pane's cursor cell (daemon-sent
// cursor_from_end and cursor_x); without the fields, or with wrap on, it
// degrades to the end of the last line, exact at a shell prompt,
// approximate in full-screen TUIs.
function _ghostRemove() {
  const el = document.getElementById('compose-ghost');
  if (!el) return;
  const prev = el.previousSibling;
  const next = el.nextSibling;
  el.remove();
  // Re-join the text node that splitText cut around the span. Without this
  // every cursor-only sync leaves one more fragment behind (splitText on
  // static content mints an extra text node per message) and the TreeWalker
  // scans grow until the next full re-render. Merging adjacent text nodes is
  // serialization-neutral, the same argument as splitting them, so the
  // cache-strip guarantee only tightens. Targeted to the span's siblings:
  // no whole-tree normalize per render. Accepted trade: a selection
  // endpoint inside the merged node can collapse (a mid-drag cursor-only
  // sync across the anchor), rarer than the fragment leak this prevents.
  if (prev && next
      && prev.nodeType === Node.TEXT_NODE && next.nodeType === Node.TEXT_NODE) {
    prev.textContent += next.textContent;
    next.remove();
  }
}

function _ghostSync() {
  _ghostRemove();
  // No ghost in password mode (the pwd box stays a visible input), in
  // scroll mode (the viewport is browsing history, not the prompt), or
  // without a pane to anchor to.
  if (!keybarVisible() || kbdMode === 2 || _scrollMode || !currentPane) return;
  // Not over the loading placeholder either: composing during a cache-miss
  // pane switch would glue the ghost to "loading..." until the snapshot.
  if (_paneLoading) return;
  // Streaming already delivered the box's prefix to the pane, so the ghost
  // mirrors only the un-streamed tail (value minus the streamed prefix,
  // diffed by code point). Normally empty: it shows text only when nothing
  // could stream, e.g. with no pane at the time of typing.
  // Predictions that outlived the round trip are wrong: a pane that does
  // not echo, or a dropped frame. Drop them and stop rendering predictions
  // until an echo confirms again.
  if (_pending.length && Date.now() - _pending[0].ts > _PRED_TTL) {
    _predClear(true);
  }
  const full = [...cmdInput.value];
  const sent = [..._streamed];
  const text = full.slice(_cpCommonPrefix(sent, full)).join('');
  const span = document.createElement('span');
  span.id = 'compose-ghost';
  // Cursor-accurate anchor: the daemon maps #{cursor_y} against
  // #{pane_height} into cursor_from_end, the cursor's capture line as a
  // from-the-end index, so the ghost lands on the real cursor line even in
  // full-screen TUIs whose cursor sits mid-screen. With wrap on the daemon
  // re-maps the cursor into joined-capture coordinates (cm_join_cursor), so
  // the same anchor math applies; a daemon that cannot map (or predates the
  // mapping) omits the fields and this falls back to end-of-buffer.
  // cursor_x places the ghost at the cursor's column within that line: TUIs
  // that pad the cursor line to the pane width (a bordered input box) put
  // line-end at the right border while typing lands mid-line. A mid-line
  // insert shifts the rest of the line right by the ghost's width while it
  // exists; with streaming the tail is normally empty, so that is the caret
  // block only, accepted.
  if (Number.isInteger(_cursorFromEnd) && _cursorFromEnd >= 0
      && Number.isInteger(_cursorX) && _cursorX >= 0) {
    const all = output.textContent;
    // Offset of the end of the target line: step back one newline per
    // from-the-end index, starting before the final line-closing newline.
    let end = all.length;
    if (all.endsWith('\n')) end--;
    let ok = all.length > 0;
    for (let i = 0; ok && i < _cursorFromEnd; i++) {
      // end === 0 guard: lastIndexOf clamps fromIndex -1 to 0, so a leading
      // newline would match forever instead of falling through to the
      // end-of-buffer anchor (unreachable while the daemon clamps, but the
      // client stays self-contained).
      const nl = end > 0 ? all.lastIndexOf('\n', end - 1) : -1;
      if (nl === -1) ok = false;
      else end = nl;
    }
    if (ok) {
      // Insertion offset: line start plus cursor_x clamped to the line's
      // length, counted in code points (spread) so the split never lands
      // inside a surrogate pair. cursor_x is a terminal CELL column, so the
      // caret drifts one cell right past each double-width character or tab
      // before it; accepted like the other approximations here.
      // Guard end 0: lastIndexOf clamps a -1 fromIndex to 0, so a leading
      // newline would match itself and put lineStart past end.
      const lineStart = end > 0 ? all.lastIndexOf('\n', end - 1) + 1 : 0;
      const cps = [...all.slice(lineStart, end)];
      const at = lineStart + cps.slice(0, Math.min(_cursorX, cps.length)).join('').length;
      let node = null, start = 0;
      const w = document.createTreeWalker(output, NodeFilter.SHOW_TEXT);
      while (w.nextNode()) {
        const n = w.currentNode;
        if (start + n.textContent.length >= at) { node = n; break; }
        start += n.textContent.length;
      }
      if (node) {
        _ghostFill(span, all.slice(0, at), text);
        // A mid-buffer insert cannot climb out of a styled ANSI span without
        // moving the anchor, so an SGR-underline merging into the ghost's
        // dotted cue is accepted here (the end-of-buffer fallback climbs).
        const tail = node.splitText(at - start);
        tail.parentNode.insertBefore(span, tail);
        return;
      }
    }
    // Capture shorter than the index or no text nodes: fall through.
  }
  // End-of-buffer fallback: insert after the last rendered character but
  // before any trailing newlines, so the ghost sits at the end of the last
  // line rather than on a line of its own. Splitting a text node is
  // serialization-neutral: adjacent text nodes read back as the same
  // innerHTML, so once the ghost is removed (navigateTo strips it before
  // every cache save) nothing of this leaks into _paneCache.
  // Last text node with a non-newline character: every rendered line is its
  // own span, so the trailing blank screen rows are bare "\n" nodes and the
  // plain last-node walk would park the ghost on the bottom blank row
  // instead of after the prompt (visible in wrap mode, where this fallback
  // is the only anchor). A join capture preserves the prompt's trailing
  // space, so landing after it puts the ghost exactly at the caret cell.
  let last = null;
  const walker = document.createTreeWalker(output, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    if (/[^\n]/.test(walker.currentNode.textContent)) last = walker.currentNode;
  }
  if (!last) {
    _ghostFill(span, '', text);
    output.appendChild(span);
    return;
  }
  // The fallback caret sits after the last rendered character, so the text
  // left of it is everything up to the trailing newlines.
  _ghostFill(span, output.textContent.replace(/\n+$/, ''), text);
  const m = last.textContent.match(/\n+$/);
  if (m) {
    const tail = last.splitText(last.textContent.length - m[0].length);
    tail.parentNode.insertBefore(span, tail);
  } else {
    // Climb out of styled ANSI spans: text-decoration propagates into
    // children and cannot be reset from a descendant, so an SGR-underlined
    // ancestor would merge its underline into the ghost's dotted one.
    // Stop at the line span's child, not the line span itself: inserting
    // inside the line, before its trailing "\n" text node, keeps a
    // mid-buffer anchor (a styled prompt with blank rows below it, the
    // wrap-mode norm) on the prompt's line; inserting after the whole line
    // span would land past that newline and start the next line.
    let anchor = last;
    while (anchor.parentNode !== output
           && anchor.parentNode.parentNode !== output) {
      anchor = anchor.parentNode;
    }
    anchor.parentNode.insertBefore(span, anchor.nextSibling);
  }
}

// The ghost caret's fill tracks the box's focus (see #compose-ghost::after
// in the CSS): filled means keystrokes are live, hollow means tap the pane
// to bring the keyboard back. A body class so it is pure CSS from here on.
cmdInput.addEventListener('focus', () => document.body.classList.add('cmd-focused'));
cmdInput.addEventListener('blur',  () => document.body.classList.remove('cmd-focused'));

// Direct mode has no visible box to tap, so any blur (tapping the pane to
// look at it) would leave keystrokes with no target: nothing drains, no
// ghost, dead input. Tapping the pane focuses the hidden input instead,
// like tapping a real terminal. Skipped in scroll mode and while the user
// is selecting text to copy. Android's back gesture dismisses the keyboard
// without blurring the box, and focus() on an already-focused element will
// not re-open it; when the visual viewport is back to full height (no
// keyboard) a focus bounce re-summons it.
// The focus waits out the double-tap window: opening the keyboard on the
// first tap of a double tap shrinks #output under the second one, so the
// word select misses. A second tap restarts the wait, and if it selected
// a word there is a selection by then and the keyboard stays down.
const PANE_TAP_FOCUS_MS = 350;
let _paneTapFocusTimer = 0;
output.addEventListener('click', () => {
  clearTimeout(_paneTapFocusTimer);
  _paneTapFocusTimer = setTimeout(_paneTapFocus, PANE_TAP_FOCUS_MS);
});
function _paneTapFocus() {
  if (!keybarVisible() || _scrollMode) return;
  if (window.getSelection && String(window.getSelection())) return;
  const inp = activeInput();
  if (document.activeElement !== inp) { inp.focus(); return; }
  const vv = window.visualViewport;
  if (!vv || vv.height >= window.innerHeight - 50) {
    inp.blur();
    setTimeout(() => inp.focus(), 50);
  }
}

// Pointers held on #output freeze re-renders (_outputFrozen). Up and cancel
// listen on the document: a drag that ends outside #output still ends.
// A lost pointerup must not freeze the pane for good: a new primary pointer
// means nothing else is down, and leaving the app ends any gesture.
output.addEventListener('pointerdown', e => {
  if (e.isPrimary) _outputPointers.clear();
  _outputPointers.add(e.pointerId);
});
document.addEventListener('pointerup', _outputPointerUp, true);
document.addEventListener('pointercancel', _outputPointerUp, true);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { _outputPointers.clear(); _outputTouching = false; }
});

// IME guard: mobile keyboards fire input events mid-composition, and the
// box must never be cleared then (that corrupts the IME draft). Streaming
// sidesteps the guard for #cmd: every input event diffs the box against the
// streamed record without touching the box; only outside a composition
// (and on compositionend, the one safe point) is the box cleared, which
// keeps the empty-box Backspace keydown path and the beforeinput fallback
// live. The password box never streams and keeps the old drain-per-event
// flow. The ghost syncs after either path.
for (const inp of [cmdInput, pwdInput]) {
  inp.addEventListener('input', e => {
    if (keybarVisible()) {
      if (inp === cmdInput) {
        streamDirectInput(inp);
        if (!e.isComposing) {
          inp.value = '';
          _streamed = '';
          cmdInput.style.height = '';
        }
      } else if (!e.isComposing) {
        drainDirectInput(inp);
      }
    }
    _ghostSync();
  });
  inp.addEventListener('compositionend', () => {
    if (keybarVisible()) {
      if (inp === cmdInput) {
        // Reconcile the committed text against the streamed record: usually
        // a no-op, or a short BSpace-and-resend on an autocorrect rewrite.
        streamDirectInput(inp);
        inp.value = '';
        _streamed = '';
        cmdInput.style.height = '';
      } else {
        drainDirectInput(inp);
      }
    }
    _ghostSync();
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

// ── read aloud (triple tap, opt-in in Settings) ────────────────────────────
// Triple tap a spot in the output and the block it belongs to is read aloud
// with the browser's speechSynthesis (the phone's own on-device voice), one
// sentence at a time, with the sentence being read highlighted. Another
// triple tap, the Stop pill, switching context or leaving the app stops it.
//
// A block runs from the nearest break above the tapped line to the next
// break below it. Breaks:
//   - a shell prompt line (starts a block: the command and its output);
//   - Claude Code's ⏺ (a response or tool call) and "> " (your message);
//   - rules, box edges, Claude's input box and its "✻ Thinking…" status
//     line (never part of a block).
// Before speaking, terminal decoration is dropped: box-drawing, markers,
// Claude's ⎿ tool output (a tool call reads as its one-line summary), and
// lines that are mostly symbols (diffs, code, tables). Lines a program
// wrapped at the screen width are joined back into sentences.
//
// The parsing helpers below are pure (strings in, strings out) so the e2e
// tests can check exactly what gets spoken.
const TTS_ENABLED_KEY = 'tts-enabled';
const TTS_RATE_KEY    = 'tts-rate';
const TTS_RATE_MIN = 0.8, TTS_RATE_MAX = 2.0, TTS_RATE_DEFAULT = 1.2;
const TTS_MAX_SENTENCES = 60;
const TTS_SUPPORTED = 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window;

let ttsEnabled = TTS_SUPPORTED && localStorage.getItem(TTS_ENABLED_KEY) === '1';
let ttsRate = parseFloat(localStorage.getItem(TTS_RATE_KEY));
if (!(ttsRate >= TTS_RATE_MIN && ttsRate <= TTS_RATE_MAX)) ttsRate = TTS_RATE_DEFAULT;

function ttsPlain(raw) {
  return String(raw)
    .replace(/\x1b\][^\x07\x1b]*(\x07|\x1b\\)/g, '')   // OSC
    .replace(/\x1b\[[0-?]*[ -\/]*[@-~]/g, '')          // CSI
    .replace(/\x1b[@-_]/g, '')                         // other escapes
    .replace(/[\x00-\x08\x0b-\x1f\x7f]/g, '');
}

var _TTS_SHELLS = new Set(['bash', 'zsh', 'sh', 'dash', 'fish', 'ksh', 'tcsh', 'csh', 'mksh']);
// Learned per pane from the cursor line while a shell waits for input:
// { stem, term }, e.g. { stem: 'kirkland@host', term: '$' }.
var _ttsPrompts = new Map();

function _ttsLearnPrompt() {
  const p = currentPaneObj();
  if (!p || !_lines || !_TTS_SHELLS.has(p.command)) return;
  if (!Number.isInteger(_cursorFromEnd) || !Number.isInteger(_cursorX)) return;
  const realLast = _lines.length - 1 - (_lines[_lines.length - 1] === '' ? 1 : 0);
  const line = _lines[realLast - _cursorFromEnd];
  if (line === undefined) return;
  const prompt = [...ttsPlain(line)].slice(0, _cursorX).join('').trimEnd();
  const term = prompt.slice(-1);
  if (!prompt || !'$#%>❯➜»λ'.includes(term)) return;
  const stem = prompt.split(/[:\s]/)[0];
  if (stem.length >= 2 && stem !== term) _ttsPrompts.set(p.id, { stem, term });
}

// The command typed at a prompt line, or null if the line is no prompt.
function ttsPromptCommand(line, prompt) {
  if (prompt && line.startsWith(prompt.stem)) {
    const i = line.indexOf(prompt.term + ' ', prompt.stem.length);
    if (i >= 0) return line.slice(i + 2).trim();
    if (line.trimEnd().endsWith(prompt.term)) return '';
  }
  let m = line.match(/^[\w.-]+@[\w.-]+[:\s][^$#%]*[$#%](?: (.*)|$)/);
  if (m) return (m[1] || '').trim();
  m = line.match(/^\s*[❯➜λ»](?:\s+\S+\s+[❯$%#]|\s)?(?: (.*)|$)/);
  if (m) return (m[1] || '').trim();
  return null;
}

function _ttsIsBlockStart(line, prompt) {
  return /^\s*[⏺●]\s/.test(line) || /^\s*>\s\S/.test(line)
    || ttsPromptCommand(line, prompt) !== null;
}

function _ttsIsBoundary(line) {
  if (/^\s*[╭╰]/.test(line) || /^\s*│\s*>/.test(line)) return true;
  if (/^\s*[✻✳✶✢✽·*]\s+\S.*(…|\.\.\.)/.test(line)) return true;
  const t = line.trim();
  return t.length >= 3 && /^[─━═╌┄\-_=]+$/.test(t);
}

// [start, end) of the block holding line idx, or null on a break line.
function ttsFindBlock(lines, idx, prompt) {
  if (idx < 0 || idx >= lines.length || _ttsIsBoundary(lines[idx])) return null;
  let start = 0;
  for (let i = idx; i >= 0; i--) {
    if (_ttsIsBlockStart(lines[i], prompt)) { start = i; break; }
    if (_ttsIsBoundary(lines[i])) { start = i + 1; break; }
  }
  let end = lines.length;
  for (let j = idx + 1; j < lines.length; j++) {
    if (_ttsIsBlockStart(lines[j], prompt) || _ttsIsBoundary(lines[j])) { end = j; break; }
  }
  return { start, end };
}

// The sentences to speak for lines [start, end): [{ text, lines: [idx...] }].
function ttsSentences(lines, start, end, prompt) {
  const tool = (lines[start] || '').match(/^\s*[⏺●]\s+([A-Za-z][\w ]*)\((.*)\)\s*$/);
  if (tool) {
    const args = tool[2].length > 100 ? tool[2].slice(0, 100) + '…' : tool[2];
    const name = tool[1].trim();
    return [{ text: args ? `${name}: ${args}` : name, lines: [start] }];
  }
  const paras = [];
  let para = [];
  const flush = () => { if (para.length) paras.push(para); para = []; };
  let inToolOutput = false;
  let skipped = 0;
  for (let i = start; i < end; i++) {
    let t = lines[i];
    if (/^\s*⎿/.test(t)) { inToolOutput = true; flush(); continue; }
    if (inToolOutput && /^\s{4,}\S/.test(t)) continue;
    inToolOutput = false;
    const tableRow = /[│┃║]/.test(t);
    const cmd = ttsPromptCommand(t, prompt);
    if (cmd !== null) {
      flush();
      if (cmd) paras.push([{ text: cmd, line: i }]);
      continue;
    }
    t = t.replace(/^\s*[⏺●]\s/, '').replace(/^\s*>\s/, '')
         .replace(/[│┃║╭╮╰╯├┤┬┴┼─━═]/g, ' ').replace(/\s+/g, ' ').trim();
    if (!t) { flush(); continue; }
    const letters = (t.match(/\p{L}/gu) || []).length;
    const visible = t.replace(/\s/g, '').length;
    if (letters === 0 || letters / visible < 0.5) { skipped++; flush(); continue; }
    if (tableRow || /^([-*•◦▪]|\d+[.)])\s/.test(t)) {
      flush();
      t = t.replace(/^[-*•◦▪]\s/, '');
    }
    para.push({ text: t, line: i });
    if (tableRow) flush();          // one table row per paragraph
  }
  flush();

  const out = [];
  for (const p of paras) {
    let joined = '';
    const spans = [];
    for (const { text, line } of p) {
      if (joined) joined += ' ';
      spans.push({ from: joined.length, to: joined.length + text.length, line });
      joined += text;
    }
    // A sentence ends at . ! or ? followed by a space and a capital or
    // digit, so "e.g. this" and "1.2" stay whole.
    const ends = /[.!?]["')\]]*\s+(?=[A-Z0-9"'(\[])/g;
    let from = 0, m;
    const cut = to => {
      const text = joined.slice(from, to).trim();
      if (text) {
        const lns = spans.filter(sp => sp.from < to && sp.to > from).map(sp => sp.line);
        out.push({ text, lines: lns });
      }
      from = to;
    };
    while ((m = ends.exec(joined))) cut(m.index + m[0].length);
    cut(joined.length);
  }
  if (out.length > TTS_MAX_SENTENCES) {
    const rest = new Set(out.slice(TTS_MAX_SENTENCES).flatMap(x => x.lines)).size;
    out.length = TTS_MAX_SENTENCES;
    out.push({ text: `And ${rest} more lines.`, lines: [] });
  }
  return out;
}

// ── speaking ──
// var, not let/const: renderOutput and navigateTo call into this section,
// and either can run before the script reaches it.
var ttsStopBtn = document.getElementById('tts-stop');
var _tts = null;     // { gen, items, i, start, origStart, raw } while reading
var _ttsGen = 0;
var _ttsNoteTimer = 0;

function ttsSpeaking() { return !!_tts; }

function ttsStop() {
  if (!ttsStopBtn || (!_tts && ttsStopBtn.style.display === 'none')) return;
  _ttsGen++;
  _tts = null;
  try { speechSynthesis.cancel(); } catch (_) {}
  _ttsPaint();
  ttsStopBtn.style.display = 'none';
}

function _ttsNote(text) {
  ttsStopBtn.textContent = text;
  ttsStopBtn.style.display = '';
  clearTimeout(_ttsNoteTimer);
  _ttsNoteTimer = setTimeout(() => { if (!_tts) ttsStopBtn.style.display = 'none'; }, 1500);
}

function _ttsVoice() {
  const voices = speechSynthesis.getVoices() || [];
  const lang = (navigator.language || 'en').toLowerCase();
  const base = lang.split('-')[0];
  const ok = v => v.lang && v.lang.toLowerCase().replace('_', '-');
  return voices.find(v => ok(v) === lang && v.localService)
    || voices.find(v => ok(v) === lang)
    || voices.find(v => ok(v) && ok(v).startsWith(base) && v.localService)
    || voices.find(v => ok(v) && ok(v).startsWith(base))
    || null;
}

function ttsReadAt(lineIdx) {
  ttsStop();
  if (!_lines || !currentPane) return;
  const plain = _lines.map(ttsPlain);
  const prompt = _ttsPrompts.get(currentPane) || null;
  const block = ttsFindBlock(plain, lineIdx, prompt);
  const items = block ? ttsSentences(plain, block.start, block.end, prompt) : [];
  if (!items.length) { _ttsNote('Nothing to read here'); return; }
  const gen = ++_ttsGen;
  _tts = { gen, items, i: 0, start: block.start, origStart: block.start,
           raw: _lines.slice(block.start, block.end) };
  ttsStopBtn.textContent = '■ Stop reading';
  ttsStopBtn.style.display = '';
  try { speechSynthesis.cancel(); speechSynthesis.resume(); } catch (_) {}
  _ttsSpeakNext(gen);
}

function _ttsSpeakNext(gen) {
  if (!_tts || _tts.gen !== gen) return;
  if (_tts.i >= _tts.items.length) { ttsStop(); return; }
  const item = _tts.items[_tts.i];
  const u = new SpeechSynthesisUtterance(item.text);
  u.rate = ttsRate;
  u.lang = navigator.language || 'en-US';
  const v = _ttsVoice();
  if (v) u.voice = v;
  const next = () => {
    if (!_tts || _tts.gen !== gen) return;
    _tts.i++;
    _ttsSpeakNext(gen);
  };
  u.onend = next;
  u.onerror = next;
  _ttsPaint(true);
  speechSynthesis.speak(u);
}

// Where the block is now: renders and patches move lines (output scrolling
// off the top, a spinner line inserted), so find the block's lines again,
// nearest to where they were. null when they are gone.
function _ttsLocate() {
  if (!_tts || !_lines) return null;
  const n = _tts.raw.length;
  const at = i => { for (let k = 0; k < n; k++) if (_lines[i + k] !== _tts.raw[k]) return false; return true; };
  if (at(_tts.start)) return _tts.start;
  let best = null;
  for (let i = 0; i + n <= _lines.length; i++) {
    if (at(i) && (best === null || Math.abs(i - _tts.start) < Math.abs(best - _tts.start))) best = i;
  }
  if (best !== null) _tts.start = best;
  return best;
}

function _ttsPaint(scroll) {
  for (const el of output.querySelectorAll('.tts-block, .tts-reading')) {
    el.classList.remove('tts-block', 'tts-reading');
  }
  if (!_tts) return;
  const base = _ttsLocate();
  if (base === null) return;
  const kids = output.children;
  // Item line indices were taken when reading started; shift them with it.
  const shift = base - _tts.origStart;
  for (let k = 0; k < _tts.raw.length; k++) kids[base + k]?.classList.add('tts-block');
  const item = _tts.items[_tts.i];
  let first = null;
  for (const ln of (item ? item.lines : [])) {
    const el = kids[ln + shift];
    if (el) { el.classList.add('tts-reading'); first = first || el; }
  }
  if (scroll && first && !_scrollMode) first.scrollIntoView({ block: 'nearest' });
}

function _ttsAfterRender() {
  if (!_ttsPrompts) return;
  _ttsLearnPrompt();
  if (_tts) _ttsPaint();
}

ttsStopBtn.addEventListener('click', ttsStop);
document.addEventListener('visibilitychange', () => { if (document.hidden) ttsStop(); });

// ── triple tap ──
// Three taps within TTS_TAP_GAP_MS of each other, none moving more than
// TTS_TAP_SLOP px from the first. The line is taken from the first tap:
// the first tap can open the keyboard (direct mode), which moves the text
// under the finger before the third tap lands.
const TTS_TAP_GAP_MS = 400, TTS_TAP_SLOP = 16, TTS_TAP_HOLD_MS = 350;
let _ttsTaps = [];
let _ttsDown = null;

function _ttsLineAt(target, y) {
  const kids = output.children;
  let el = target && target.closest ? target.closest('#output > span') : null;
  if (el && el.id === 'compose-ghost') el = null;
  if (el) return Array.prototype.indexOf.call(kids, el);
  for (let i = 0; i < kids.length; i++) {
    const r = kids[i].getBoundingClientRect();
    if (y >= r.top && y < r.bottom) return i;
  }
  return -1;
}

output.addEventListener('pointerdown', e => {
  if (!ttsEnabled || !e.isPrimary) return;
  _ttsDown = { x: e.clientX, y: e.clientY, t: performance.now(),
               line: _ttsLineAt(e.target, e.clientY) };
});
output.addEventListener('pointercancel', () => { _ttsDown = null; _ttsTaps = []; });
output.addEventListener('pointerup', e => {
  const d = _ttsDown;
  _ttsDown = null;
  if (!ttsEnabled || !d || !e.isPrimary) return;
  const now = performance.now();
  if (now - d.t > TTS_TAP_HOLD_MS || Math.hypot(e.clientX - d.x, e.clientY - d.y) > TTS_TAP_SLOP) {
    _ttsTaps = [];
    return;
  }
  const last = _ttsTaps[_ttsTaps.length - 1];
  const first = _ttsTaps[0];
  if (!last || now - last.t > TTS_TAP_GAP_MS
      || Math.hypot(d.x - first.x, d.y - first.y) > TTS_TAP_SLOP) {
    _ttsTaps = [];
  }
  _ttsTaps.push({ x: d.x, y: d.y, t: now, line: d.line });
  if (_ttsTaps.length < 3) return;
  const line = _ttsTaps[0].line;
  _ttsTaps = [];
  try { window.getSelection().removeAllRanges(); } catch (_) {}
  if (ttsSpeaking()) ttsStop();
  else ttsReadAt(line);
});
// Desktop triple click would select a line first. Double click still
// selects a word: that is how a copy starts.
output.addEventListener('mousedown', e => {
  if (ttsEnabled && e.detail > 2) e.preventDefault();
});

// ── double swipe: same as the ‹ › buttons ────────────────────────────────
// A single horizontal swipe used to navigate, and was removed because it
// fired during ordinary scrolling. Two things keep this one out of the way:
// - It takes two quick swipes in the same direction.
// - A swipe that scrolled #output sideways doesn't count. In Terminal mode
//   (wrap off) long lines make #output scroll horizontally, so there a swipe
//   only counts once the content is at that edge (or has nothing to
//   scroll); in Aa mode (wrap on) there is normally nothing to scroll.
// Swipe left is next (like ›), swipe right is previous (like ‹).
const SWIPE_MIN_DX    = 50;   // px of horizontal travel
const SWIPE_RATIO     = 2;    // horizontal travel / vertical travel
const SWIPE_MAX_MS    = 500;  // a flick, not a slow drag or text selection
const SWIPE_DOUBLE_MS = 700;  // max gap between the two swipes

let _swipe = null;        // the touch in progress
let _swipeArmed = null;   // { dir, t } after a first qualifying swipe

output.addEventListener('touchstart', e => {
  if (e.touches.length !== 1) { _swipe = null; return; }
  const t = e.touches[0];
  _swipe = { x: t.clientX, y: t.clientY, t: performance.now(),
             scrollLeft: output.scrollLeft, multi: false };
}, { passive: true });

output.addEventListener('touchmove', e => {
  if (_swipe && e.touches.length !== 1) _swipe.multi = true;
}, { passive: true });

output.addEventListener('touchcancel', () => { _swipe = null; }, { passive: true });

output.addEventListener('touchend', e => {
  const sw = _swipe;
  _swipe = null;
  if (!sw || sw.multi || e.touches.length) return;
  const t = e.changedTouches[0];
  const dx = t.clientX - sw.x;
  const dy = t.clientY - sw.y;
  const now = performance.now();
  const isSwipe = Math.abs(dx) >= SWIPE_MIN_DX
    && Math.abs(dx) >= Math.abs(dy) * SWIPE_RATIO
    && now - sw.t <= SWIPE_MAX_MS
    && Math.abs(output.scrollLeft - sw.scrollLeft) < 1
    && !String(window.getSelection() || '');
  if (!isSwipe) return;
  const dir = dx < 0 ? 1 : -1;
  if (_swipeArmed && _swipeArmed.dir === dir && sw.t - _swipeArmed.t <= SWIPE_DOUBLE_MS) {
    _swipeArmed = null;
    navigateRelative(dir);
  } else {
    _swipeArmed = { dir, t: now };
    _flashSwipeHint(dir);
  }
}, { passive: true });

// A faint chevron on the side the next swipe will bring in, so the first
// swipe visibly registered.
const swipeHint = document.getElementById('swipe-hint');
let _swipeHintTimer = 0;
function _flashSwipeHint(dir) {
  swipeHint.textContent = dir > 0 ? '›' : '‹';
  swipeHint.className = dir > 0 ? 'show right' : 'show left';
  clearTimeout(_swipeHintTimer);
  _swipeHintTimer = setTimeout(() => { swipeHint.className = ''; }, SWIPE_DOUBLE_MS);
}

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
  _updateRenameButton();

  if (_ctxLevel === 'sessions') {
    _ctxAddTitle('Sessions');
    for (const s of sessions) {
      if (!liveWindowsInSession(s).length) continue;
      const btn = _ctxAddRow(sessionLabel(s), s.id === currentSessionId, () => {
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
    _ctxAddTitle(`Session ${sessionLabel(s)}`);
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
    _ctxAddBackRow(`Session ${sessionLabel(s)}`, () => {
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

// The rename button names whatever the open level lists: the current
// window, or the current pane. Sessions are numbers, so there is nothing to
// rename at that level.
function _updateRenameButton() {
  const what = _ctxLevel === 'windows' ? 'window' : _ctxLevel === 'panes' ? 'pane' : null;
  ctxRenameOpen.style.display = what ? '' : 'none';
  if (what) ctxRenameOpen.textContent = `✎  Rename current ${what}`;
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
// Renames go to tmux; the daemon answers with a fresh topology, and the
// local copy is updated right away so the header doesn't lag a round trip.
let _pendingRename = null; // { kind: 'window' | 'pane', id }

function showRenameForm() {
  if (!currentPane) return;
  const w = currentWindow();
  const p = currentPaneObj();
  if (_ctxLevel === 'windows' && w) {
    _pendingRename = { kind: 'window', id: w.id };
    ctxRenameLabel.textContent = 'Rename this window (empty = automatic name):';
    ctxRenameInput.value = w.name;
  } else if (_ctxLevel === 'panes' && p) {
    _pendingRename = { kind: 'pane', id: p.id };
    ctxRenameLabel.textContent = 'Name this pane (empty = no name):';
    ctxRenameInput.value = p.named ? p.title : '';
  } else {
    return;
  }
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
  if (!_pendingRename) return;
  const name = ctxRenameInput.value.trim();
  const { kind, id } = _pendingRename;
  if (kind === 'window') {
    if (_BAD_WINDOW_NAME.test(name)) {
      ctxRenameLabel.textContent = 'Window names can\'t contain : . @ or %';
      return;
    }
    send({ type: 'rename_window', window_id: id, name });
    const w = currentWindow();
    if (w && w.id === id && name) w.name = name;
  } else {
    send({ type: 'rename_pane', pane_id: id, name });
    const p = currentPaneObj();
    if (p && p.id === id) { p.named = !!name; p.title = name; }
  }
  _pendingRename = null;
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

// Round-trip latency via a dedicated ping/pong (not list_sessions — that
// queries tmux, adding noise to a number meant to reflect network time).
// Only one ping is ever in flight: the daemon's pong carries no correlation
// id, so a second concurrent ping would be indistinguishable from the
// first's reply. The on-demand popup measurement and the periodic
// connection-health monitor below share this one slot — a caller that
// arrives while a ping is already outstanding doesn't send a duplicate, it
// just also gets notified when that same pong comes back.
let _pendingPings = [];  // resolvers waiting on the next pong
let _pingSentAt = null;  // performance.now() the in-flight ping was sent, null = none in flight

function _requestPing(timeoutMs) {
  return new Promise(resolve => {
    let settled = false;
    const done = rtt => { if (!settled) { settled = true; resolve(rtt); } };
    _pendingPings.push(done);
    if (_pendingPings.length === 1) {
      _pingSentAt = performance.now();
      send({ type: 'ping' });
    }
    setTimeout(() => {
      const idx = _pendingPings.indexOf(done);
      if (idx !== -1) _pendingPings.splice(idx, 1);
      done(null);
    }, timeoutMs);
  });
}

// Resolves to null if no pong arrives within LATENCY_TIMEOUT_MS. Well past
// the connection's own stall threshold: a real link (e.g. plane wifi, or a
// connection still working through a backed-up send queue) can be connected
// but take tens of seconds to round-trip, and the popup should show that
// number instead of "timed out".
const LATENCY_TIMEOUT_MS = 60000;
function measureLatency() {
  return _requestPing(LATENCY_TIMEOUT_MS);
}

// ── connection-health indicator (top-bar checkmark) ─────────────────────────
// The checkmark used to mean only "the browser's WebSocket object reports
// OPEN" -- true even when the underlying link is so degraded that nothing
// gets through in any reasonable time, or even fully dead (TCP doesn't
// always notice a vanished path quickly). Reported: a solid green check
// shown throughout a connection that was, in practice, unusable. Ping on a
// steady cadence and grade the checkmark by real round-trip time instead of
// socket state alone -- green means "verified fast, recently", not just
// "technically still connected".
const HEALTH_PING_INTERVAL_MS = 5000;
const HEALTH_GREEN_MS   = 2000;  // under this: green
const HEALTH_STALLED_MS = 15000; // no response by this long: treat like disconnected
let _healthPingTimer = null;
let _healthTickTimer = null;

function _gradeHealth(rttOrNull) {
  if (rttOrNull === null || rttOrNull >= HEALTH_STALLED_MS) {
    setStatus(rttOrNull === null ? 'no response — connection may be lost'
                                  : `no response for ${Math.round(rttOrNull / 1000)}s`, 'error');
  } else if (rttOrNull >= HEALTH_GREEN_MS) {
    setStatus(`slow connection (${rttOrNull}ms)`, 'degraded');
  } else {
    setStatus(`connected (${rttOrNull}ms)`, 'connected');
  }
}

async function _healthPing() {
  const rtt = await _requestPing(HEALTH_STALLED_MS + 2000);
  if (!ws || ws.readyState !== WebSocket.OPEN) return; // onclose owns the indicator now
  _gradeHealth(rtt);
}

function _healthTick() {
  // Escalate the indicator *while* a slow ping is still outstanding rather
  // than only once it finally resolves -- otherwise a stalling link keeps
  // showing the last-known-good color for up to HEALTH_STALLED_MS.
  if (_pingSentAt === null) return;
  const elapsed = performance.now() - _pingSentAt;
  if (elapsed >= HEALTH_GREEN_MS) _gradeHealth(elapsed);
}

function startHealthMonitor() {
  stopHealthMonitor();
  _healthPing();
  _healthPingTimer = setInterval(_healthPing, HEALTH_PING_INTERVAL_MS);
  _healthTickTimer = setInterval(_healthTick, 1000);
}
function stopHealthMonitor() {
  if (_healthPingTimer) { clearInterval(_healthPingTimer); _healthPingTimer = null; }
  if (_healthTickTimer) { clearInterval(_healthTickTimer); _healthTickTimer = null; }
  _pingSentAt = null;
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
  // Take the code out of the URL bar and history as soon as it has been
  // read, whether or not pairing then succeeds; and only auto-submit a
  // fragment that is exactly a code, so a stray link cannot spend a guess.
  const fragment = window.location.hash.slice(1) || '';
  if (window.location.hash) {
    history.replaceState(null, '', window.location.pathname + window.location.search);
  }
  const autoCode = /^\d{6}$/.test(fragment) ? fragment : '';
  if (autoCode) {
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

  // Not connIndicator.classList.contains('connected') -- the indicator now
  // also has a 'degraded' class for a slow-but-open connection, which is
  // still connected for this popup's purposes (it should measure and show
  // the real latency, not claim there's nothing to measure).
  const connected = !!ws && ws.readyState === WebSocket.OPEN;
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

// ── about popup (tap the logo or "Trustmux" wordmark) ──────────────────────
function showAboutPopup() {
  const rect = logoLink.getBoundingClientRect();
  aboutPopup.style.display = 'block';
  aboutPopup.style.top  = (rect.bottom + 8) + 'px';
  aboutPopup.style.left = rect.left + 'px';
}
function hideAboutPopup() {
  aboutPopup.style.display = 'none';
}
function toggleAboutPopup(e) {
  e.stopPropagation();
  aboutPopup.style.display === 'none' ? showAboutPopup() : hideAboutPopup();
}
logoLink.addEventListener('click', toggleAboutPopup);
document.addEventListener('click', () => hideAboutPopup());
document.addEventListener('touchstart', e => {
  if (!aboutPopup.contains(e.target) && e.target !== logoLink && !logoLink.contains(e.target)) hideAboutPopup();
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
// Every other entry here is a local-fonts-only hint: it stays selectable
// even when the device can't actually render it (see fontResolves and
// renderSettings; hiding an unresolved entry made selection a one-way
// door), annotated "(not installed)" rather than silently falling back. The
// list mixes desktop staples with the monospace families Android ships
// (Droid Sans Mono, Cutive Mono, and OEM extras). 'DejaVu Sans Mono' is the
// one exception: it is bundled (fonts/, @font-face in index.html), not a
// device hint, so it carries no probe and is always available -- it is
// also what 'System' now falls through to ahead of the browser's own
// unpredictable last-resort substitute (see --font's comment).
const FONT_FAMILIES = [
  { label: 'System',         value: "'SF Mono', 'Fira Code', 'Cascadia Code', 'DejaVu Sans Mono', monospace" },
  { label: 'DejaVu Sans Mono', value: "'DejaVu Sans Mono', monospace" },
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
  // _sendSubscribeNow (via _requestSubscribe) carries join on every
  // resubscribe: without it the daemon would capture this pane unjoined,
  // and with wrap on the re-render would hard-break long lines at the tmux
  // pane width until the next pane switch.
  if (currentPane) _requestSubscribe(currentPane);
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
  renderTtsSettings();
}

const settingsTtsToggle = document.getElementById('settings-tts-toggle');
const settingsTtsRate   = document.getElementById('settings-tts-rate-row');
const settingsTtsValue  = document.getElementById('settings-tts-rate-value');

function renderTtsSettings() {
  if (!TTS_SUPPORTED) {
    settingsTtsToggle.textContent = 'Read aloud: not supported in this browser';
    settingsTtsToggle.disabled = true;
    settingsTtsRate.style.display = 'none';
    return;
  }
  settingsTtsToggle.className = 'ctx-btn' + (ttsEnabled ? ' ctx-current' : '');
  settingsTtsToggle.textContent = (ttsEnabled ? '✓ ' : '') + 'Triple tap reads aloud';
  settingsTtsRate.style.display = ttsEnabled ? '' : 'none';
  settingsTtsValue.textContent = ttsRate.toFixed(1) + '×';
}

settingsTtsToggle.addEventListener('click', () => {
  ttsEnabled = !ttsEnabled;
  localStorage.setItem(TTS_ENABLED_KEY, ttsEnabled ? '1' : '0');
  if (!ttsEnabled) ttsStop();
  renderTtsSettings();
});

function stepTtsRate(delta) {
  const next = Math.round(Math.min(TTS_RATE_MAX, Math.max(TTS_RATE_MIN, ttsRate + delta)) * 10) / 10;
  if (next === ttsRate) return;
  ttsRate = next;
  localStorage.setItem(TTS_RATE_KEY, String(next));
  renderTtsSettings();
}
document.getElementById('settings-tts-slower').addEventListener('click', () => stepTtsRate(-0.1));
document.getElementById('settings-tts-faster').addEventListener('click', () => stepTtsRate(0.1));

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
  // Hide the composition ghost: while browsing history it would masquerade
  // as pane content.
  _ghostSync();
}
function exitScrollMode() {
  _scrollMode = false;
  _hideKbdChip();
  // Entry blurred the input, so q/Esc would strand focus on body and the
  // next keystrokes would go nowhere. Scroll mode is only reachable via a
  // hardware Ctrl+B, so refocusing cannot pop a soft keyboard.
  activeInput().focus();
  _ghostSync();
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
let _vvAppliedH = -1;
let _vvAppliedTop = -1;
function syncViewportToKeyboard() {
  const vv = window.visualViewport;
  if (!vv) return;
  // Some keyboards report a visualViewport height that oscillates by about
  // a pixel per keystroke; writing every reading through re-lays-out #app
  // and the whole screen shifts with it. Round, and skip writes within 1px
  // of the last applied value (2px hysteresis against the applied value, so
  // a slow slide still lands within 1px). Keyboard open/close moves the
  // height by hundreds of pixels and always passes.
  const h = Math.round(vv.height);
  const top = Math.round(vv.offsetTop);
  if (Math.abs(h - _vvAppliedH) > 1 || Math.abs(top - _vvAppliedTop) > 1) {
    _vvAppliedH = h;
    _vvAppliedTop = top;
    appEl.style.height = h + 'px';
    appEl.style.top = top + 'px';
  }
  // The ghost caret fills on actual keyboard visibility, not focus:
  // Android's back gesture dismisses the keyboard without blurring the box,
  // so focus alone would keep a filled caret over dead keystrokes. Same
  // threshold as the pane-tap re-summon check.
  document.body.classList.toggle('kbd-open', vv.height < window.innerHeight - 50);
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
