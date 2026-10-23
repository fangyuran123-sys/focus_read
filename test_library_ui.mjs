// Dependency-free checks for library interactions, not a replacement for browser layout tests.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function setup() {
  const elements = new Map();
  const makeElement = () => ({
    value: '', textContent: '', disabled: false, hidden: false, open: false,
    children: [], classList: {toggle() {}},
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    setAttribute() {}, removeAttribute() {}, focus() {}, click() { this.clicked = true; },
    querySelectorAll() { return []; },
    querySelector() { return makeElement(); },
    showModal() { this.open = true; }, close() { this.open = false; },
  });
  const $ = id => {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  };
  let handler = async () => ({books: [], total: 0, page: 1, pages: 1, counts: {}, tags: []});
  const calls = [];
  const storage = new Map(), styles = new Map();
  const context = vm.createContext({
    $, element: makeElement, Option: function(text, value) { return {text, value}; },
    document: {querySelectorAll: () => [], addEventListener() {}, activeElement: null,
      documentElement: {style: {setProperty: (key, value) => styles.set(key, value)}}},
    location: {search: ''},
    localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value)},
    window: {addEventListener() {}},
    URLSearchParams, setTimeout, clearTimeout, state: {connected: false}, busy: false,
    render() {}, fillFolders() {}, toast() {}, load: async () => {}, size: String, when: String,
    request: async (path, body) => { calls.push({path, body}); return handler(path, body); },
  });
  vm.runInContext(fs.readFileSync(new URL('./web/library.js', import.meta.url), 'utf8'), context);
  return {$, calls, styles, storage, run: source => vm.runInContext(source, context), handle: fn => { handler = fn; }};
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const listing = (page = 1) => ({books: [], total: 38, page, pages: 2, counts: {want: 38}, tags: []});
const book = id => ({id, title: id, author: '', description: '', review: '', status: 'want', tags: [], files: []});

test('pagination disables while loading and ignores stale responses', async () => {
  const ui = setup(); await tick();
  const pending = [];
  ui.handle(() => new Promise(resolve => pending.push(resolve)));
  const first = ui.run('loadLibrary()');
  const second = ui.run('loadLibrary()');
  assert.equal(ui.$('local-next').disabled, true);
  pending[1](listing(2)); await second;
  pending[0](listing(1)); await first;
  assert.equal(ui.$('local-page').textContent, '2 / 2');
  assert.equal(ui.$('local-prev').disabled, false);
  assert.equal(ui.$('local-next').disabled, true);
});

test('removed active tag stays visible instead of silently clearing the filter', async () => {
  const ui = setup(); await tick();
  ui.$('local-tag').value = 'removed-tag';
  ui.handle(async () => listing());
  await ui.run('loadLibrary()');
  assert.equal(ui.$('local-tag').children.at(-1).value, 'removed-tag');
});

test('batch import continues past bad files and shows warnings without markup', async () => {
  const ui = setup(); await tick();
  ui.handle(async path => {
    if (path.startsWith('/api/library/import')) {
      const name = new URLSearchParams(path.split('?')[1]).get('name');
      if (name === 'bad.exe') throw new Error('不支持此格式');
      return {book: book('id'), duplicate: name === 'duplicate.txt', warning: name === 'good.epub' ? '元数据读取失败' : ''};
    }
    return listing();
  });
  await ui.$('local-file-input').onchange({target: {value: 'files', files: [
    {name: 'empty.txt', size: 0}, {name: 'bad.exe', size: 10},
    {name: 'good.epub', size: 10}, {name: 'duplicate.txt', size: 10},
  ]}});
  assert.match(ui.$('local-progress').textContent, /新增 1 个文件，重复 1 个，失败 2 个/);
  assert.match(ui.$('local-progress').textContent, /bad.exe：不支持此格式/);
  assert.match(ui.$('local-progress').textContent, /good.epub：元数据读取失败/);
  assert.equal(ui.run('busy'), false);
  assert.equal(ui.calls.filter(c => c.path.startsWith('/api/library/import')).length, 3);
});

test('warmth clamps invalid levels, stores choice and tolerates disabled storage', async () => {
  const ui = setup(); await tick();
  assert.equal(ui.styles.get('--warmth'), '50%');
  ui.$('warmth').value = '80'; ui.$('warmth').oninput();
  assert.equal(ui.styles.get('--warmth'), '80%');
  assert.equal(Number(ui.storage.get('desk-warmth')), 80);
  ui.run('applyWarmth(200)');
  assert.equal(ui.styles.get('--warmth'), '100%');
  ui.run('applyWarmth("bad")');
  assert.equal(ui.styles.get('--warmth'), '50%');
  ui.run('localStorage.setItem = () => { throw new Error("disabled"); }');
  assert.doesNotThrow(() => ui.$('warmth').oninput());
});

test('progress and rating save as numbers, stats are unfiltered and filters reach API', async () => {
  const ui = setup(); await tick();
  const stored = {...book('first'), progress: 30, current_page: 42, rating: 4};
  ui.handle(async path => path.startsWith('/api/library/book') || path === '/api/library/update' ? stored :
    {...listing(), stats: {year: 2026, finished_year: 2, finished: 5, reading: 3, notes: 10}});
  await ui.run("openBook('first')");
  assert.equal(ui.$('detail-progress').value, 30);
  ui.$('detail-progress').value = '60';
  ui.$('detail-current_page').value = '90';
  ui.$('detail-rating').value = '5';
  ui.$('detail-tags').value = '';
  await ui.$('book-form').onsubmit({preventDefault() {}});
  const payload = JSON.parse(ui.calls.find(c => c.path === '/api/library/update').body);
  assert.equal(payload.progress, 60);
  assert.equal(payload.current_page, 90);
  assert.equal(payload.rating, 5);
  assert.equal(ui.$('stats-finished-year').textContent, 2);
  assert.equal(ui.$('stats-notes').textContent, 10);
  ui.$('local-rating').value = '4';
  ui.$('local-sort').value = 'rating';
  ui.$('local-rating').onchange(); await tick();
  assert.match(ui.calls.at(-1).path, /rating=4/);
  assert.match(ui.calls.at(-1).path, /sort=rating/);
  ui.$('detail-status').value = 'finished'; ui.$('detail-status').onchange();
  assert.equal(ui.$('detail-progress').value, 100);
});

test('delete is explicit, cancel is harmless, failures keep details, success refreshes', async () => {
  const ui = setup(); await tick();
  let failDelete = true;
  ui.handle(async path => {
    if (path.startsWith('/api/library/book')) return book('first');
    if (path === '/api/library/delete') {
      if (failDelete) throw new Error('权限不足');
      return {message: '已移出书库', cleanup_failed: []};
    }
    return listing();
  });
  await ui.run("openBook('first')");
  ui.$('book-delete').onclick();
  assert.equal(ui.$('delete-files').checked, false);
  ui.$('delete-cancel').onclick();
  assert.equal(ui.calls.some(c => c.path === '/api/library/delete'), false);
  ui.$('book-delete').onclick();
  await ui.$('delete-form').onsubmit({preventDefault() {}});
  assert.equal(ui.$('delete-error').textContent, '权限不足');
  assert.equal(ui.$('book-dialog').open, true);
  assert.equal(ui.$('delete-confirm').disabled, false);
  failDelete = false;
  ui.$('delete-files').checked = true;
  await ui.$('delete-form').onsubmit({preventDefault() {}});
  assert.equal(ui.$('book-dialog').open, false);
  assert.equal(ui.$('delete-dialog').open, false);
  assert.equal(ui.run('currentBook'), null);
  assert.equal(JSON.parse(ui.calls.filter(c => c.path === '/api/library/delete').at(-1).body).delete_files, true);
});

test('cover generation requires saved metadata, refreshes versioned image, and unlocks on error', async () => {
  const ui = setup(); await tick();
  const stored = {...book('first'), cover: true, cover_version: 'new-version'};
  ui.handle(async path => path.startsWith('/api/library/book') ? book('first') :
    path.startsWith('/api/library/cover?') ? {book: stored, message: '已生成'} : listing());
  await ui.run("openBook('first')");
  ui.run('detailDirty = true');
  await ui.run("applyBookCover('file')");
  assert.equal(ui.calls.some(c => c.path.startsWith('/api/library/cover')), false);
  assert.match(ui.$('detail-message').textContent, /请先保存/);
  ui.run('detailDirty = false');
  ui.$('book-form').oninput({target: {id: 'cover-file-input'}});
  assert.equal(ui.run('detailDirty'), false);
  await ui.run("applyBookCover('file')");
  const request = ui.calls.find(c => c.path.startsWith('/api/library/cover'));
  assert.match(request.path, /generate=1/);
  assert.equal(request.body, '');
  assert.match(ui.$('detail-cover-img').src, /v=new-version/);
  assert.equal(ui.$('detail-cover-img').hidden, false);
  assert.equal(ui.$('detail-message').textContent, '已生成');
  ui.handle(async () => { throw new Error('转换失败'); });
  await ui.run("applyBookCover('file')");
  assert.equal(ui.$('detail-message').textContent, '转换失败');
  assert.equal(ui.run('libraryWorking'), false);
});

test('cover upload is bounded and cannot apply a picker result to a different book', async () => {
  const ui = setup(); await tick();
  ui.run('currentBook = {id:"second"}; coverTarget = {file:"file",book:"first"}');
  const count = ui.calls.length;
  await ui.$('cover-file-input').onchange({target: {files: [{size: 10}], value: 'image'}});
  assert.equal(ui.calls.length, count);
  ui.run('coverTarget = {file:"file",book:"second"}');
  await ui.$('cover-file-input').onchange({target: {files: [{size: 9*1024*1024}], value: 'image'}});
  assert.equal(ui.calls.length, count);
});

test('last clicked book wins even if its detail request finishes first', async () => {
  const ui = setup(); await tick();
  const pending = new Map();
  ui.handle(path => new Promise(resolve => pending.set(path.split('id=')[1], resolve)));
  const first = ui.run("openBook('first')");
  const second = ui.run("openBook('second')");
  pending.get('second')(book('second')); await second;
  pending.get('first')(book('first')); await first;
  assert.equal(ui.$('book-detail-title').textContent, 'second');
  assert.equal(ui.$('book-dialog').open, true);
});
