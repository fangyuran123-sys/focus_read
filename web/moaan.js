'use strict';
let moaanState = null, moaanSequence = 0, moaanWorking = false, moaanSelected = [], moaanFolderTarget = null;

function renderMoaan() {
  if (!moaanState) return;
  const available = moaanState.connected && !moaanWorking && !busy;
  $('moaan-connection').textContent = moaanState.connected ? '已连接' : '未连接';
  $('moaan-connection').className = 'status-pill' + (moaanState.connected ? '' : ' offline');
  $('moaan-dot').style.background = moaanState.connected ? 'var(--green)' : 'var(--muted)';
  $('moaan-connection-detail').textContent = moaanState.connected ? 'MTP 连接正常' : '离线浏览 / 上次快照';
  $('moaan-screen-title').textContent = moaanState.books[0]?.title || '你的下一本书';
  $('moaan-screen-count').textContent = moaanState.books.length + ' 本';
  $('moaan-book-count').textContent = moaanState.books.length;
  $('moaan-folder-count').textContent = moaanState.folders.length;
  $('moaan-library-count').textContent = moaanState.books.length + ' 本';
  const storage = moaanState.storage;
  $('moaan-storage-free').textContent = storage ? (storage.free / 1073741824).toFixed(2) : '—';
  $('moaan-storage-total').textContent = storage ? '共 ' + (storage.total / 1073741824).toFixed(2) + ' GiB' : '设备未提供容量';
  $('moaan-storage-bar').style.width = storage ? (storage.used / storage.total * 100) + '%' : '0%';
  $('moaan-new-folder').disabled = !available;
  $('moaan-dropzone').disabled = !available || !moaanState.folder;
  $('moaan-send').disabled = !available || !moaanState.folder || !moaanSelected.length;
  $('moaan-cancel-files').disabled = moaanWorking;
  $('moaan-send-path').textContent = moaanState.folder ? '目标：墨案 / ' + moaanState.folder : '先在设备书架选择目标目录，例如 Books';
  $('moaan-transfer-history').replaceChildren();
  for (const item of moaanState.transfers || []) {
    const row = element('div', 'transfer-row'), label = element('div');
    label.append(element('strong', '', item.name), element('small', '', when(item.created) + ' / ' + item.folder));
    row.append(label, element('span', 'done', '已校验')); $('moaan-transfer-history').append(row);
  }
  if (!moaanState.transfers?.length) $('moaan-transfer-history').append(element('p', 'small muted', '还没有墨案传输记录。单本最大 200 MiB。'));
  $('moaan-summary').textContent = (moaanState.connected ? 'MTP 已连接' : '未连接，显示上次书架') + ' / 当前目录 ' + moaanState.books.length + ' 本';
  $('moaan-path').textContent = '内部存储' + (moaanState.folder ? ' / ' + moaanState.folder : '');
  $('moaan-last-scan').textContent = '上次读取 ' + when(moaanState.last_scan);
  $('moaan-reading').textContent = moaanState.reading_message;
  $('moaan-up').disabled = !available || !moaanState.folder;
  $('moaan-refresh').disabled = moaanWorking || busy;
  $('moaan-folders').replaceChildren();
  for (const folder of moaanState.folders) {
    const button = element('button', 'button secondary', '▱ ' + folder.name);
    button.disabled = !available || busy;
    button.onclick = () => loadMoaan(folder.path);
    $('moaan-folders').append(button);
  }
  $('moaan-books').replaceChildren();
  for (const book of moaanState.books) {
    const row = element('article', 'moaan-book');
    const info = element('div');
    info.append(element('h3', '', book.title), element('p', 'small muted', book.format + ' / ' + size(book.size)));
    const actions = element('div', 'moaan-book-actions');
    const download = element('a', 'button secondary', '下载原文件');
    download.setAttribute('aria-disabled', String(!available));
    if (available) download.href = '/api/moaan/file?id=' + encodeURIComponent(book.id);
    const importBook = element('button', 'button blue', '导入本地书库');
    importBook.disabled = !available || busy || book.size > 200 * 1024 * 1024 || !book.size;
    importBook.onclick = () => importMoaanBook(book.id);
    actions.append(download, importBook); row.append(info, actions); $('moaan-books').append(row);
  }
  if (!moaanState.books.length) $('moaan-books').append(element('p', 'reading-history', '此目录没有可识别的书籍，请选择子目录。'));
}
async function loadMoaan(folder = null) {
  if (busy || moaanWorking) return;
  const sequence = ++moaanSequence;
  moaanWorking = true;
  $('moaan-refresh').disabled = true;
  $('moaan-error').hidden = true;
  try {
    const current = await request('/api/moaan');
    if (sequence !== moaanSequence) return;
    moaanState = current;
    if (current.connected) {
      const target = folder ?? current.folder;
      try { moaanState = await request('/api/moaan/scan', JSON.stringify({folder: target})); }
      catch (error) {
        if (!target) throw error;
        moaanState = await request('/api/moaan/scan', JSON.stringify({folder: ''}));
        $('moaan-error').textContent = '上次目录不可用，已返回内部存储：' + error.message;
        $('moaan-error').hidden = false;
      }
    }
  } catch (error) {
    $('moaan-error').textContent = error.message;
    $('moaan-error').hidden = false;
  } finally {
    moaanWorking = false;
    $('moaan-refresh').disabled = busy;
    renderMoaan();
  }
}
function setMoaanWorking(value) {
  moaanWorking = value;
  setLibraryWorking(value);
  renderMoaan();
}
$('moaan-refresh').onclick = () => loadMoaan();
$('moaan-up').onclick = () => loadMoaan(moaanState.folder.split('/').slice(0, -1).join('/'));
async function importMoaanBook(id) {
  if (busy || moaanWorking) return;
  setMoaanWorking(true);
  try {
    const result = await request('/api/moaan/import', JSON.stringify({id}));
    toast((result.duplicate ? '本地已有此文件，未重复导入。' : '已导入本地书库，墨案原文件未修改。') + (result.warning || ''));
    await loadLibrary();
  } catch (error) { toast(error.message, true); }
  finally { setMoaanWorking(false); }
}
async function sendMoaanFile(id) {
  if (busy || moaanWorking || !moaanState?.connected || !moaanState.folder) return;
  if (!confirm('发送到墨案 / ' + moaanState.folder + '？EPUB 直接发送，不转换；已有文件不会覆盖。')) return;
  const payload = {id, folder: moaanState.folder, device_id: moaanState.device_id};
  setMoaanWorking(true);
  $('detail-message').textContent = '正在发送到墨案并校验文件，请勿断开设备…';
  try {
    const result = await request('/api/moaan/send', JSON.stringify(payload));
    $('detail-message').textContent = result.message;
    toast(result.message);
    currentBook = await request('/api/library/book?id=' + encodeURIComponent(currentBook.id));
  } catch (error) { $('detail-message').textContent = error.message; toast(error.message, true); }
  finally { setMoaanWorking(false); }
}

function chooseMoaanFiles(files) {
  if (busy || moaanWorking) return;
  moaanSelected = [...files];
  $('moaan-pending').hidden = !moaanSelected.length;
  $('moaan-pending-list').replaceChildren(...moaanSelected.map(file => element('p', 'pending-file', file.name + ' / ' + size(file.size))));
  $('moaan-transfer-state').hidden = true;
  renderMoaan();
}
$('moaan-dropzone').onclick = () => { if (!$('moaan-dropzone').disabled) $('moaan-file-input').click(); };
$('moaan-file-input').onchange = event => { chooseMoaanFiles(event.target.files); event.target.value = ''; };
for (const type of ['dragenter', 'dragover']) $('moaan-dropzone').addEventListener(type, event => {
  event.preventDefault();
  if (!$('moaan-dropzone').disabled) $('moaan-dropzone').classList.add('dragging');
});
for (const type of ['dragleave', 'drop']) $('moaan-dropzone').addEventListener(type, event => {
  event.preventDefault(); $('moaan-dropzone').classList.remove('dragging');
  if (type === 'drop' && !$('moaan-dropzone').disabled) chooseMoaanFiles(event.dataTransfer.files);
});
$('moaan-cancel-files').onclick = () => chooseMoaanFiles([]);
$('moaan-send').onclick = async () => {
  if (busy || moaanWorking || !moaanSelected.length || !moaanState?.connected || !moaanState.folder) return;
  const folder = moaanState.folder, deviceId = moaanState.device_id;
  const failures = []; let completed = 0;
  setMoaanWorking(true);
  $('moaan-transfer-state').hidden = false;
  try {
    for (const file of moaanSelected) {
      $('moaan-transfer-state').textContent = '正在发送到墨案 / ' + folder + '：' + file.name;
      try {
        if (!file.size || file.size > 200 * 1024 * 1024) throw new Error('文件为空或超过 200 MiB。');
        if (!/\.(epub|pdf|txt)$/i.test(file.name)) throw new Error('支持 EPUB、PDF、TXT。');
        const query = new URLSearchParams({name: file.name, folder, device_id: deviceId});
        await request('/api/moaan/upload?' + query, file);
        completed++;
      } catch (error) { failures.push(file.name + '：' + error.message); }
    }
    const message = '发送完成：成功 ' + completed + ' 本，失败 ' + failures.length + ' 本。';
    $('moaan-transfer-state').textContent = [message, ...failures].join('\n');
    toast(message, failures.length > 0);
    moaanSelected = []; $('moaan-pending').hidden = true;
  } finally {
    setMoaanWorking(false);
    await loadMoaan(folder);
  }
};
$('moaan-new-folder').onclick = () => {
  if (busy || moaanWorking || !moaanState?.connected) return;
  moaanFolderTarget = {parent: moaanState.folder, device_id: moaanState.device_id};
  $('moaan-folder-parent').textContent = '墨案 / ' + (moaanState.folder || '内部存储');
  $('moaan-folder-name').value = '';
  $('moaan-folder-error').hidden = true;
  $('moaan-folder-dialog').showModal(); $('moaan-folder-name').focus();
};
$('moaan-folder-cancel').onclick = () => { if (!moaanWorking) $('moaan-folder-dialog').close(); };
$('moaan-folder-dialog').oncancel = event => { if (moaanWorking) event.preventDefault(); };
$('moaan-folder-form').onsubmit = async event => {
  event.preventDefault();
  if (busy || moaanWorking || !moaanFolderTarget) return;
  const payload = {...moaanFolderTarget, name: $('moaan-folder-name').value.trim()};
  setMoaanWorking(true);
  for (const id of ['moaan-folder-cancel', 'moaan-folder-confirm', 'moaan-folder-name']) $(id).disabled = true;
  try {
    moaanState = await request('/api/moaan/folders', JSON.stringify(payload));
    $('moaan-folder-dialog').close(); toast('墨案分类已创建。');
  } catch (error) { $('moaan-folder-error').textContent = error.message; $('moaan-folder-error').hidden = false; }
  finally {
    for (const id of ['moaan-folder-cancel', 'moaan-folder-confirm', 'moaan-folder-name']) $(id).disabled = false;
    setMoaanWorking(false);
  }
};
