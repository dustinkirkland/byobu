const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { test } = require('node:test');
const vm = require('node:vm');

const source = readFileSync(`${__dirname}/../trustmux/static/app.js`, 'utf8');
const lockSource = source.slice(source.indexOf('const LOCK_IDLE_MS'), source.indexOf('const pairOverlay'));
const renderSource = source.slice(source.indexOf('function renderSettings()'), source.indexOf('function stepFontSize('));

function client(saved, enabled = 'true') {
  const storage = new Map([['lock-enabled', enabled], ['lock-cred-id', 'credential']]);
  if (saved !== undefined) storage.set('lock-hidden-ms', saved);
  const listeners = {};
  const timers = new Map();
  let now = 1000;
  let timerId = 0;
  function element() {
    return {
      style: {}, children: [], listeners: {},
      set innerHTML(value) { this.children = []; },
      appendChild(child) { this.children.push(child); },
      addEventListener(event, fn) { this.listeners[event] = fn; },
    };
  }
  const overlay = element();
  const context = vm.createContext({
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
    },
    document: {
      hidden: false,
      addEventListener: (event, fn) => { listeners[event] = fn; },
      getElementById: () => overlay,
      createElement: element,
    },
    Date: { now: () => now },
    setTimeout: (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; },
    clearTimeout: id => timers.delete(id),
    settingsLockList: element(), settingsThemeList: element(),
    settingsFontList: element(), settingsSizeValue: element(),
    THEME_CHOICES: [], FONT_FAMILIES: [], fontSize: 12,
  });
  vm.runInContext(lockSource + renderSource, context);
  return {
    context, storage, timers, overlay,
    run: code => vm.runInContext(code, context),
    away(ms) {
      context.document.hidden = true;
      listeners.visibilitychange();
      now += ms;
      context.document.hidden = false;
      listeners.visibilitychange();
    },
    activity: () => listeners.keydown(),
  };
}

test('background timeout locks at the boundary for every choice', () => {
  for (const ms of [30000, 60000, 120000, 300000]) {
    for (const delta of [-1, 0, 1]) {
      const app = client(String(ms));
      app.away(ms + delta);
      assert.equal(app.run('_isLocked'), delta >= 0);
    }
  }
});

test('missing and invalid saved choices fall back to 30 seconds', () => {
  for (const saved of [undefined, '', 'garbage', '0', '-1', 'Infinity', '30000ms', '3e4', '3000000']) {
    const app = client(saved);
    assert.equal(app.run('lockHiddenMs'), '30000');
    app.away(30000);
    assert.equal(app.run('_isLocked'), true);
  }
});

test('settings select, persist and restore each choice', () => {
  const app = client();
  app.run('renderSettings()');
  for (const [index, value] of ['30000', '60000', '120000', '300000'].entries()) {
    app.context.settingsLockList.children[index].listeners.click();
    assert.equal(app.storage.get('lock-hidden-ms'), value);
    assert.match(app.context.settingsLockList.children[index].textContent, /^✓ /);
    assert.equal(client(app.storage.get('lock-hidden-ms')).run('lockHiddenMs'), value);
    assert.equal(app.timers.size, 0);
  }
});

test('idle timer stays at five minutes and remains active in the background', () => {
  const app = client('300000');
  app.activity();
  const timer = [...app.timers.values()][0];
  assert.equal(timer.ms, 300000);
  app.away(299999);
  assert.equal(app.run('_isLocked'), false);
  assert.equal([...app.timers.values()][0], timer);
  timer.fn();
  assert.equal(app.run('_isLocked'), true);
});

test('background intervals do not accumulate and disabled lock stays off', () => {
  const app = client();
  app.away(20000);
  app.away(20000);
  assert.equal(app.run('_isLocked'), false);
  const disabled = client('30000', 'false');
  disabled.activity();
  disabled.away(300000);
  assert.equal(disabled.run('_isLocked'), false);
  assert.equal(disabled.timers.size, 0);
});
