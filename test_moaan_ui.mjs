import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function setup() {
  const elements = new Map(), calls = [], notices = [];
  const element = (tag, cls = '', text = '') => ({
    tag, className: cls, textContent: text, children: [], disabled: false, value: '', hidden: true, open: false, attrs: {}, style: {},
    classList: {add() {}, remove() {}}, addEventListener() {}, focus() {},
    showModal() { this.open = true; }, close() { this.open = false; },
    append(...children) { this.children.push(...children); }, replaceChildren(...children) { this.children = children; },
    setAttribute(key, value) { this.attrs[key] = value; },
  });
  const $ = id => { if (!elements.has(id)) elements.set(id, element('div')); return elements.get(id); };
  let handler = async () => fixture(), allow = true;
  const context = vm.createContext({
    $, element, when: String, size: String, busy: false, currentBook: {id: 'local-book'},
    request: (path, body) => { calls.push({path, body}); return handler(path, body); },
    setLibraryWorking: value => { context.busy = value; }, loadLibrary: async () => {},
    toast: (message, error) => notices.push({message, error}), confirm: () => allow, URLSearchParams,
  });
  vm.runInContext(fs.readFileSync(new URL('./web/moaan.js', import.meta.url), 'utf8'), context);
  return {$, calls, notices, run: source => vm.runInContext(source, context), handle: fn => { handler = fn; }, confirm: value => { allow = value; }};
}
function fixture() {
  return {connected: true, device_id: 'device', folder: 'Books', last_scan: 1,
    books: [{id: 'book', title: '<literal title>', format: 'EPUB', size: 123}],
    folders: [{name: '文学', path: 'Books/文学'}], reading_message: '墨案暂不计入热力图。'};
}

test('MTP folder is explicit and offline cache disables device actions', async () => {
  const ui = setup();
  await ui.run('loadMoaan()');
  assert.equal(JSON.parse(ui.calls[1].body).folder, 'Books');
  assert.match(ui.$('moaan-summary').textContent, /已连接/);
  assert.equal(ui.$('moaan-books').children[0].children[0].children[0].textContent, '<literal title>');
  ui.$('moaan-folders').children[0].onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(JSON.parse(ui.calls.at(-1).body).folder, 'Books/文学');
  ui.handle(async () => ({...fixture(), connected: false}));
  ui.calls.length = 0;
  await ui.run('loadMoaan()');
  assert.equal(ui.calls.length, 1);
  assert.equal(ui.$('moaan-folders').children[0].disabled, true);
  const actions = ui.$('moaan-books').children[0].children[1].children;
  assert.equal(actions[0].attrs['aria-disabled'], 'true');
  assert.equal(actions[0].href, undefined);
  assert.equal(actions[1].disabled, true);
});

test('missing cached directory falls back to shared root with an explanation', async () => {
  const ui = setup();
  ui.handle(async (path, raw) => {
    if (path === '/api/moaan') return fixture();
    if (JSON.parse(raw).folder) throw new Error('directory missing');
    return {...fixture(), folder: ''};
  });
  await ui.run('loadMoaan()');
  assert.equal(JSON.parse(ui.calls.at(-1).body).folder, '');
  assert.equal(ui.$('moaan-error').hidden, false);
  assert.match(ui.$('moaan-error').textContent, /已返回内部存储/);
  assert.equal(ui.$('moaan-up').disabled, true);
});

test('import waits for completion and refreshes local library', async () => {
  const ui = setup();
  await ui.run('loadMoaan()');
  ui.handle(async () => ({duplicate: true, warning: ''}));
  await ui.run('importMoaanBook("book")');
  assert.equal(ui.calls.at(-1).path, '/api/moaan/import');
  assert.equal(JSON.parse(ui.calls.at(-1).body).id, 'book');
  assert.match(ui.notices.at(-1).message, /未重复导入/);
  assert.equal(ui.run('busy'), false);
});

test('dashboard shows real storage and the device preview without inventing reading totals', async () => {
  const ui = setup();
  ui.handle(async () => ({...fixture(), storage: {free: 1073741824, total: 2147483648, used: 1073741824}, transfers: [{name: 'Book.epub', folder: 'Books', created: 1}]}));
  await ui.run('loadMoaan()');
  assert.equal(ui.$('moaan-storage-free').textContent, '1.00');
  assert.equal(ui.$('moaan-storage-bar').style.width, '50%');
  assert.equal(ui.$('moaan-screen-title').textContent, '<literal title>');
  assert.equal(ui.$('moaan-transfer-history').children.length, 1);
  assert.equal(ui.$('moaan-new-folder').disabled, false);
  assert.equal(ui.$('moaan-dropzone').disabled, false);
  assert.equal(ui.$('moaan-book-count').textContent, 1);
});

test('batch upload continues after invalid files and pins the destination', async () => {
  const ui = setup(); await ui.run('loadMoaan()');
  ui.handle(async path => path.startsWith('/api/moaan/upload?') ? {message: 'sent'} : fixture());
  ui.run('chooseMoaanFiles([{name:"bad.exe",size:10},{name:"Book.epub",size:10},{name:"empty.txt",size:0}])');
  await ui.$('moaan-send').onclick();
  const uploads = ui.calls.filter(call => call.path.startsWith('/api/moaan/upload?'));
  assert.equal(uploads.length, 1);
  assert.match(uploads[0].path, /folder=Books/);
  assert.match(uploads[0].path, /device_id=device/);
  assert.match(ui.$('moaan-transfer-state').textContent, /成功 1 本，失败 2 本/);
  assert.match(ui.$('moaan-transfer-state').textContent, /bad.exe/);
  assert.equal(ui.run('busy'), false);
});

test('folder creation pins the original parent and shows errors without closing', async () => {
  const ui = setup(); await ui.run('loadMoaan()');
  ui.$('moaan-new-folder').onclick();
  assert.equal(ui.$('moaan-folder-dialog').open, true);
  ui.$('moaan-folder-name').value = '文学';
  ui.handle(async () => { throw new Error('name collision'); });
  await ui.$('moaan-folder-form').onsubmit({preventDefault() {}});
  assert.equal(ui.$('moaan-folder-dialog').open, true);
  assert.equal(ui.$('moaan-folder-error').textContent, 'name collision');
  assert.deepEqual(JSON.parse(ui.calls.at(-1).body), {parent: 'Books', device_id: 'device', name: '文学'});
  assert.equal(ui.$('moaan-folder-confirm').disabled, false);
  ui.handle(async () => fixture());
  await ui.$('moaan-folder-form').onsubmit({preventDefault() {}});
  assert.equal(ui.$('moaan-folder-dialog').open, false);
});

test('sending requires confirmation and keeps device identity/folder explicit; failures unlock controls', async () => {
  const ui = setup();
  await ui.run('loadMoaan()');
  ui.confirm(false);
  const before = ui.calls.length;
  await ui.run('sendMoaanFile("file")');
  assert.equal(ui.calls.length, before);
  ui.confirm(true);
  ui.handle(async path => {
    if (path === '/api/moaan/send') throw new Error('MTP disconnected');
    return {};
  });
  await ui.run('sendMoaanFile("file")');
  const request = ui.calls.at(-1);
  assert.equal(request.path, '/api/moaan/send');
  assert.deepEqual(JSON.parse(request.body), {id: 'file', folder: 'Books', device_id: 'device'});
  assert.equal(ui.$('detail-message').textContent, 'MTP disconnected');
  assert.equal(ui.run('busy'), false);
  assert.equal(ui.run('moaanWorking'), false);
});
