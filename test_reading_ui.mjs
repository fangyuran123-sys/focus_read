import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function setup() {
  const elements = new Map(), listeners = new Map();
  const element = (tag, cls = '', text = '') => ({
    tag, className: cls, textContent: text, children: [], dataset: {}, style: {}, attrs: {}, value: '', hidden: true,
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute(key, value) { this.attrs[key] = value; },
    querySelectorAll(tag) { return this.children.filter(child => child.tag === tag); },
    querySelector() { return null; },
    focus() { this.onfocus?.(); },
  });
  const $ = id => { if (!elements.has(id)) elements.set(id, element('div')); return elements.get(id); };
  let handler = async () => ({});
  const context = vm.createContext({
    $, element, Option: function(text, value) { return {text, value}; }, when: String,
    window: {addEventListener: (name, fn) => listeners.set(name, fn)}, state: {last_scan: 1},
    request: (...args) => handler(...args),
  });
  vm.runInContext(fs.readFileSync(new URL('./web/reading.js', import.meta.url), 'utf8'), context);
  return {$, context, run: source => vm.runInContext(source, context), handle: fn => { handler = fn; }, listeners};
}
const result = year => ({year, years: [2026, 2024], today: '2026-10-22', days: {'2024-02-29': 1800},
  active_days: 1, seconds: 1800, sessions: 1, synced_at: 1, first_date: '2024-02-29', last_date: '2024-02-29'});

test('calendar includes leap day, aligns Monday, and handles 54-week years', () => {
  const ui = setup();
  const dates = ui.run('readingCalendar(2024)');
  assert.equal(dates.filter(Boolean).length, 366);
  assert.equal(dates[0], '2024-01-01'); // Monday
  assert.ok(dates.includes('2024-02-29'));
  const long = ui.run('readingCalendar(2012)');
  assert.equal(long[6], '2012-01-01'); // Sunday
  assert.equal(Math.ceil(long.length / 7), 54);
  assert.equal(ui.run('readingLevel(0)'), 0);
  assert.equal(ui.run('readingLevel(899)'), 1);
  assert.equal(ui.run('readingLevel(900)'), 2);
  assert.equal(ui.run('readingLevel(1800)'), 3);
  assert.equal(ui.run('readingLevel(3600)'), 4);
});

test('day labels, click and arrow keys expose the same duration without coloring unknown dates', () => {
  const ui = setup();
  ui.context.fixture = result(2024);
  ui.run('renderReading(fixture)');
  const buttons = ui.$('reading-heatmap').querySelectorAll('button');
  assert.equal(buttons.length, 366);
  const leap = buttons.find(button => button.dataset.date === '2024-02-29');
  assert.equal(leap.attrs['aria-label'], '2024-02-29 / 30 分');
  assert.equal(leap.className, 'heatmap-cell heat-level-3');
  leap.onclick();
  assert.equal(ui.$('reading-day').textContent, '2024-02-29 / 30 分');
  assert.equal(leap.tabIndex, 0);
  leap.onkeydown({key: 'ArrowDown', preventDefault() {}});
  assert.equal(ui.$('reading-day').textContent, '2024-03-01 / 无已导入会话');
  assert.equal(buttons.filter(button => button.tabIndex === 0).length, 1);
  assert.equal(ui.run('readingDuration(3660)'), '1 小时 1 分');
});

test('empty state and stale responses preserve the most recent year selection', async () => {
  const ui = setup(), pending = [];
  ui.handle(() => new Promise(resolve => pending.push(resolve)));
  const first = ui.run('loadReading()');
  const second = ui.run('loadReading()');
  pending[1](result(2024)); await second;
  pending[0](result(2026)); await first;
  assert.equal(ui.$('reading-year').value, '2024');
  ui.context.fixture = {...result(2026), days: {}, active_days: 0, seconds: 0, sessions: 0, first_date: null, synced_at: null};
  ui.run('renderReading(fixture)');
  assert.equal(ui.$('reading-empty').hidden, false);
  assert.match(ui.$('reading-empty').textContent, /连接 Kindle/);
  assert.equal(ui.$('reading-heatmap').querySelectorAll('button').length, 365);
  ui.handle(async () => { throw new Error('offline'); });
  await ui.run('loadReading()');
  assert.equal(ui.$('reading-error').hidden, false);
  assert.match(ui.$('reading-error').textContent, /offline/);
});

test('state polling only reloads after a new scan and when calendar is visible', () => {
  const ui = setup();
  let calls = 0;
  ui.handle(async () => { calls++; return result(2026); });
  ui.listeners.get('desk-state')();
  assert.equal(calls, 0);
  ui.$('reading-view').hidden = false;
  ui.listeners.get('desk-state')(); ui.listeners.get('desk-state')();
  assert.equal(calls, 1);
  ui.run('state.last_scan = 2'); ui.listeners.get('desk-state')();
  assert.equal(calls, 2);
});
