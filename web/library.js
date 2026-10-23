'use strict';
const readingLabels = {want: '想读', reading: '在读', finished: '已读', paused: '搁置'};
let localPage = 1, localStatus = '', localResult = null, localSequence = 0;
let currentBook = null, importTarget = null, coverTarget = null, libraryWorking = false, searchTimer, detailDirty = false, detailSequence = 0;

function applyWarmth(value) {
  const warmth = Number(value);
  const level = Number.isFinite(warmth) ? Math.min(100, Math.max(0, warmth)) : 50;
  document.documentElement.style.setProperty('--warmth', level + '%');
  $('warmth').value = level;
  $('warmth-value').textContent = level + '%';
}
try { applyWarmth(localStorage.getItem('desk-warmth') ?? 50); } catch { applyWarmth(50); }
$('warmth').oninput = () => {
  applyWarmth($('warmth').value);
  try { localStorage.setItem('desk-warmth', $('warmth').value); } catch { /* Storage may be disabled; the slider still works. */ }
};
function renderStats(stats) {
  if (!stats) return;
  $('stats-year').textContent = stats.year + ' 年';
  for (const key of ['finished_year', 'finished', 'reading', 'notes']) $('stats-' + key.replace('_', '-')).textContent = stats[key];
}

function changeView(view, status = '') {
  if (busy) return;
  localStatus = status;
  $('local-view').hidden = view !== 'library';
  $('device-view').hidden = !['device', 'notes'].includes(view);
  $('reading-view').hidden = view !== 'reading';
  $('moaan-view').hidden = view !== 'moaan';
  $('library-actions').hidden = view !== 'library';
  $('device-actions').hidden = view === 'library' || view === 'moaan';
  $('device-view').classList.toggle('notes-only', view === 'notes');
  $('view-title').textContent = view === 'library' ? (readingLabels[status] || '我的图书馆') : view === 'moaan' ? '墨案设备' : view === 'reading' ? '阅读日历' : view === 'notes' ? '阅读摘录' : 'Kindle 设备';
  $('view-eyebrow').textContent = view === 'library' ? '个人图书馆' : view === 'reading' || view === 'notes' ? '阅读记录' : '设备连接';
  $('view-subtitle').textContent = view === 'library' ? (status ? '这个书架共有 ' + (localResult?.counts?.[status] || 0) + ' 本' : '想读的、在读的、读完的，都在这里。') : view === 'moaan' ? '墨案迷你阅，通过 MTP 连接你的本地图书馆。' : view === 'reading' ? '让每天的阅读，慢慢留下颜色。' : view === 'notes' ? '从 Kindle 导入的高亮、笔记和书签。' : '只在本机读取设备，不修改 Kindle 固件。';
  document.querySelectorAll('.nav-item').forEach(button => {
    const active = button.dataset.view === view && (view !== 'library' || button.dataset.status === status);
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
  if (view === 'library') { localPage = 1; loadLibrary(); }
  else if (view === 'moaan') loadMoaan();
  else { load(); if (view === 'reading') loadReading(); }
}
document.querySelectorAll('.nav-item').forEach(button => button.onclick = () => changeView(button.dataset.view, button.dataset.status || ''));

async function loadLibrary() {
  const sequence = ++localSequence;
  $('local-prev').disabled = true;
  $('local-next').disabled = true;
  const query = new URLSearchParams({q: $('local-search').value.trim(), status: localStatus, tag: $('local-tag').value, page: localPage, sort: $('local-sort').value, rating: $('local-rating').value});
  try {
    const result = await request('/api/library?' + query);
    if (sequence !== localSequence) return;
    localResult = result;
    renderStats(result.stats);
    localPage = result.page;
    const total = Object.values(result.counts).reduce((a, b) => a + b, 0);
    $('local-total').textContent = total;
    for (const key of Object.keys(readingLabels)) $('count-' + key).textContent = result.counts[key] || 0;
    const tag = $('local-tag').value;
    const tags = tag && !result.tags.includes(tag) ? [...result.tags, tag] : result.tags;
    $('local-tag').replaceChildren(new Option('全部标签', ''), ...tags.map(t => new Option(t, t)));
    $('local-tag').value = tag;
    $('local-summary').textContent = result.total + ' 本图书';
    $('local-books').replaceChildren();
    for (const book of result.books) {
      const button = element('button', 'local-book');
      button.type = 'button'; button.onclick = () => openBook(book.id);
      button.setAttribute('aria-label', '查看 ' + book.title);
      const cover = element('div', 'local-cover cover-' + (parseInt(book.id.slice(0, 2), 16) % 5));
      cover.setAttribute('aria-hidden', 'true');
      if (book.cover) {
        const img = element('img');
        img.src = '/api/cover?book=' + encodeURIComponent(book.id) + '&v=' + encodeURIComponent(book.cover_version || '');
        img.alt = ''; img.loading = 'lazy'; img.decoding = 'async';
        img.onerror = () => img.remove();
        cover.append(img);
      }
      cover.append(element('span', (book.cover ? 'cover-author visually-hidden' : 'cover-author'), book.author || '私人藏书'), element('span', (book.cover ? 'cover-title visually-hidden' : 'cover-title'), book.title), element('span', (book.cover ? 'cover-rule visually-hidden' : 'cover-rule'), ''));
      const formats = book.formats.join(' / ');
      const info = element('div', 'local-book-info');
      info.append(element('h3', '', book.title), element('p', 'local-author', book.author || '作者待补充'));
      const meta = element('div', 'local-meta');
      meta.append(element('span', 'reading-badge ' + book.status, readingLabels[book.status]), element('span', '', formats));
      if (book.rating) meta.append(element('span', 'book-rating', '★'.repeat(book.rating)));
      info.append(meta);
      if (book.status === 'reading') {
        const label = element('p', 'book-progress-label', (book.progress || 0) + '%' + (book.current_page ? ' / 第 ' + book.current_page + ' 页' : ''));
        const progress = element('progress', 'book-progress');
        progress.max = 100; progress.value = book.progress || 0;
        progress.setAttribute('aria-label', book.title + ' 阅读进度');
        info.append(label, progress);
      }
      if (book.tags.length) info.append(element('p', 'local-tags', book.tags.slice(0, 3).map(t => '#' + t).join('  ')));
      button.append(cover, info); $('local-books').append(button);
    }
    $('local-empty').hidden = result.total !== 0;
    const empty = $('local-empty');
    empty.querySelector('h2').textContent = total ? '这个书架还没有匹配的图书' : '你的图书馆，从第一本书开始';
    empty.querySelector('p').textContent = total ? '试试其他关键词或标签，也可以导入新书。' : '导入 EPUB、PDF 或 Kindle 图书文件。原文件会复制到本地书库，不会移动或修改你的源文件。';
    $('empty-import').textContent = total ? '导入图书' : '导入第一本书';
    $('local-page').textContent = result.page + ' / ' + result.pages;
    $('local-pagination').hidden = result.pages <= 1;
    $('local-prev').disabled = result.page <= 1;
    $('local-next').disabled = result.page >= result.pages;
  } catch (error) {
    if (sequence === localSequence) { $('local-summary').textContent = '书库读取失败'; toast(error.message, true); }
  } finally {
    if (sequence === localSequence) {
      $('local-prev').disabled = !localResult || localResult.page <= 1;
      $('local-next').disabled = !localResult || localResult.page >= localResult.pages;
    }
  }
}
$('local-search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { localPage = 1; loadLibrary(); }, 250); };
$('local-tag').onchange = () => { localPage = 1; loadLibrary(); };
$('local-sort').onchange = () => { localPage = 1; loadLibrary(); };
$('local-rating').onchange = () => { localPage = 1; loadLibrary(); };
$('local-prev').onclick = () => { localPage--; loadLibrary(); };
$('local-next').onclick = () => { localPage++; loadLibrary(); };

function setLibraryWorking(value) {
  libraryWorking = value; busy = value;
  render();
  $('local-import').disabled = busy || !state;
  $('empty-import').disabled = busy || !state;
  $('book-save').disabled = value;
  $('add-format').disabled = value;
  $('book-close').disabled = value;
  $('book-delete').disabled = value;
  for (const id of ['delete-confirm', 'delete-cancel', 'delete-files']) $(id).disabled = value;
  $('book-form').querySelectorAll('input,select,textarea').forEach(el => el.disabled = value);
  if (currentBook) renderFiles();
}
function chooseLocalFiles(bookId = null) {
  if (busy || !state) return;
  importTarget = bookId;
  $('local-file-input').click();
}
$('local-import').onclick = () => chooseLocalFiles();
$('empty-import').onclick = () => chooseLocalFiles();
$('add-format').onclick = () => chooseLocalFiles(currentBook.id);
$('local-file-input').onchange = async event => {
  const files = [...event.target.files], target = importTarget;
  event.target.value = '';
  if (!files.length) return;
  setLibraryWorking(true);
  const progress = target ? $('detail-message') : $('local-progress');
  progress.hidden = false;
  let added = 0, duplicates = 0;
  const failures = [], warnings = [];
  try {
    for (const [index, file] of files.entries()) {
      progress.textContent = `正在导入 ${index + 1}/${files.length}：${file.name}`;
      try {
        if (!file.size || file.size > 200 * 1024 * 1024) throw new Error('文件为空或超过 200 MiB。');
        const query = new URLSearchParams({name: file.name});
        if (target) query.set('book', target);
        const result = await request('/api/library/import?' + query, file);
        if (result.duplicate) duplicates++; else added++;
        if (result.warning) warnings.push(file.name + '：' + result.warning);
        if (target) currentBook = result.book;
      } catch (error) { failures.push(file.name + '：' + error.message); }
    }
    const summary = `导入完成：新增 ${added} 个文件，重复 ${duplicates} 个，失败 ${failures.length} 个。原文件已保留。`;
    progress.textContent = [summary, ...failures, ...warnings].join('\n');
    toast(summary, failures.length > 0);
  } finally {
    setLibraryWorking(false);
    localPage = 1; await loadLibrary();
  }
};

async function openBook(id) {
  if (busy) return;
  const sequence = ++detailSequence;
  try {
    const [, book] = await Promise.all([load(), request('/api/library/book?id=' + encodeURIComponent(id))]);
    if (sequence !== detailSequence) return;
    currentBook = book;
    $('book-detail-title').textContent = book.title;
    $('detail-byline').textContent = [book.author || '作者待补充', [...new Set(book.files.map(f => f.format))].join(' / '), readingLabels[book.status]].filter(Boolean).join(' · ');
    const cover = $('detail-cover-img');
    cover.hidden = !book.cover;
    cover.onerror = () => { cover.hidden = true; $('detail-cover-fallback').hidden = false; };
    if (book.cover) { cover.src = '/api/cover?book=' + encodeURIComponent(book.id) + '&v=' + encodeURIComponent(book.cover_version || ''); cover.alt = book.title + ' 封面'; }
    else { cover.removeAttribute('src'); cover.alt = ''; }
    $('detail-cover-fallback').hidden = !!book.cover;
    $('detail-id').value = book.id;
    for (const key of ['title', 'author', 'description', 'review', 'status']) $('detail-' + key).value = book[key];
    $('detail-tags').value = book.tags.join(', ');
    for (const key of ['progress', 'current_page', 'rating']) $('detail-' + key).value = book[key] || 0;
    $('detail-progress-value').textContent = (book.progress || 0) + '%';
    $('detail-message').textContent = '';
    detailDirty = false;
    renderFiles();
    if (!$('book-dialog').open) $('book-dialog').showModal();
  } catch (error) { if (sequence === detailSequence) toast(error.message, true); }
}
$('detail-progress').oninput = () => { $('detail-progress-value').textContent = $('detail-progress').value + '%'; };
$('detail-status').onchange = () => {
  detailDirty = true;
  if ($('detail-status').value === 'finished') {
    $('detail-progress').value = 100;
    $('detail-progress-value').textContent = '100%';
  }
};
$('book-form').oninput = event => { if (event.target.id !== 'cover-file-input') detailDirty = true; };
function canCloseBook() { return !libraryWorking && (!detailDirty || confirm('书籍信息尚未保存，确定放弃修改吗？')); }
$('book-close').onclick = () => { if (canCloseBook()) $('book-dialog').close(); };
$('book-dialog').oncancel = event => { if (!canCloseBook()) event.preventDefault(); };
$('book-form').onsubmit = async event => {
  event.preventDefault(); if (libraryWorking) return;
  const payload = {id: currentBook.id, tags: $('detail-tags').value.split(/[,，]/).map(t => t.trim()).filter(Boolean)};
  for (const key of ['title', 'author', 'description', 'review', 'status']) payload[key] = $('detail-' + key).value;
  for (const key of ['progress', 'current_page', 'rating']) payload[key] = Number($('detail-' + key).value);
  setLibraryWorking(true);
  try {
    currentBook = await request('/api/library/update', JSON.stringify(payload));
    detailDirty = false;
    $('book-detail-title').textContent = currentBook.title;
    $('detail-progress').value = currentBook.progress;
    $('detail-progress-value').textContent = currentBook.progress + '%';
    $('detail-message').textContent = '已保存到本地。';
    await loadLibrary();
  } catch (error) { $('detail-message').textContent = error.message; toast(error.message, true); }
  finally { setLibraryWorking(false); }
};

$('book-delete').onclick = () => {
  if (libraryWorking || !currentBook) return;
  $('delete-book-name').textContent = currentBook.title;
  $('delete-files').checked = false;
  $('delete-error').textContent = '';
  $('delete-dialog').showModal();
  $('delete-cancel').focus();
};
$('delete-cancel').onclick = () => { if (!libraryWorking) $('delete-dialog').close(); };
$('delete-dialog').oncancel = event => { if (libraryWorking) event.preventDefault(); };
$('delete-form').onsubmit = async event => {
  event.preventDefault();
  if (libraryWorking || !currentBook) return;
  const payload = {id: currentBook.id, delete_files: $('delete-files').checked};
  setLibraryWorking(true);
  try {
    const result = await request('/api/library/delete', JSON.stringify(payload));
    ++detailSequence;
    currentBook = null; detailDirty = false;
    $('delete-dialog').close(); $('book-dialog').close();
    toast(result.message, result.cleanup_failed?.length > 0);
    await loadLibrary();
    $('local-import').focus();
  } catch (error) { $('delete-error').textContent = error.message; }
  finally { setLibraryWorking(false); }
};

function renderFiles() {
  if (!currentBook || !state) return;
  fillFolders('local-send-folder', 'Kindle Desk（默认目录）');
  $('local-send-folder').disabled = libraryWorking || !state.connected;
  $('detail-files').replaceChildren();
  for (const file of currentBook.files) {
    const row = element('div', 'detail-file');
    const info = element('div');
    info.append(element('strong', '', file.name), element('p', 'small muted', file.format + ' · ' + size(file.size) + (file.generated_from ? ' · 转换副本' : ' · 书库文件') + (file.covered_file ? ' · 有历史补封面版本' : '')));
    if (file.sent_at) info.append(element('p', 'small muted', '上次发送 ' + when(file.sent_at) + ' · ' + file.sent_path + '（历史记录，非实时同步）'));
    const actions = element('div', 'detail-file-actions');
    const download = element('a', 'button secondary', '下载');
    download.href = '/api/library/file?id=' + encodeURIComponent(file.id);
    const send = element('button', 'button blue', file.format === 'EPUB' ? '转换并发送' : '发送 Kindle');
    send.type = 'button';
    const supported = ['EPUB', 'PDF', 'AZW3', 'AZW', 'MOBI', 'TXT'].includes(file.format);
    send.disabled = libraryWorking || !state.connected || !supported;
    send.title = !supported ? '此格式目前仅支持本地保存和下载。' : !state.connected ? '请先连接并挂载 Kindle。' : '发送完成后，请安全弹出设备。';
    send.onclick = () => sendLocalFile(file.id);
    actions.append(download, send);
    const moaanSend = element('button', 'button secondary', '发送墨案');
    moaanSend.type = 'button';
    const moaan = typeof moaanState === 'undefined' ? null : moaanState;
    moaanSend.disabled = libraryWorking || !moaan?.connected || !moaan.folder || !['EPUB', 'PDF', 'TXT'].includes(file.format);
    moaanSend.title = '先打开墨案设备页并选择书籍目录；支持 EPUB、PDF、TXT，EPUB 不转换。';
    moaanSend.onclick = () => sendMoaanFile(file.id);
    actions.append(moaanSend);
    if (['EPUB', 'AZW3', 'AZW', 'MOBI', 'TXT'].includes(file.format)) {
      const coverActions = element('div', 'file-cover-actions');
      const upload = element('button', 'text-button', '上传封面'), generate = element('button', 'text-button', '生成文字封面');
      upload.type = generate.type = 'button';
      upload.disabled = generate.disabled = libraryWorking;
      upload.onclick = () => {
        if (libraryWorking || !coverReady()) return;
        coverTarget = {file: file.id, book: currentBook.id}; $('cover-file-input').click();
      };
      generate.onclick = () => { if (!libraryWorking && coverReady()) applyBookCover(file.id); };
      coverActions.append(upload, generate); info.append(coverActions);
    }
    row.append(info, actions); $('detail-files').append(row);
  }
}
function coverReady() {
  if (detailDirty) { $('detail-message').textContent = '请先保存书名、作者等修改，再补封面。'; return false; }
  return true;
}
$('cover-file-input').onchange = async event => {
  const file = event.target.files[0], target = coverTarget;
  event.target.value = ''; coverTarget = null;
  if (!file || !target || libraryWorking || currentBook?.id !== target.book || !coverReady()) return;
  if (!file.size || file.size > 8 * 1024 * 1024) { toast('封面为空或超过 8 MiB。', true); return; }
  await applyBookCover(target.file, file);
};
async function applyBookCover(id, image = null) {
  if (libraryWorking || !currentBook || !coverReady()) return;
  const source = currentBook.files.find(file => file.id === id);
  if (['TXT', 'AZW'].includes(source?.format) && !confirm(source.format + ' 补封面需要转换，将把这条书库文件替换为 AZW3，不再保留书库中的旧格式。下载源文件不动，确认继续？')) return;
  setLibraryWorking(true);
  $('detail-message').textContent = '正在更新书库封面，校验成功后替换文件，不新增版本…';
  try {
    const query = new URLSearchParams({id});
    if (!image) query.set('generate', '1');
    const result = await request('/api/library/cover?' + query, image || '');
    currentBook = result.book;
    $('detail-cover-img').src = '/api/cover?book=' + encodeURIComponent(currentBook.id) + '&v=' + encodeURIComponent(currentBook.cover_version || '');
    $('detail-cover-img').hidden = !currentBook.cover;
    $('detail-cover-fallback').hidden = !!currentBook.cover;
    $('detail-message').textContent = result.message;
    toast(result.message);
    await loadLibrary();
  } catch (error) { $('detail-message').textContent = error.message; toast(error.message, true); }
  finally { setLibraryWorking(false); }
}
async function sendLocalFile(id) {
  const folder = $('local-send-folder').value;
  const payload = {id};
  if (folder !== '*') payload.folder = folder;
  setLibraryWorking(true);
  $('detail-message').textContent = '正在处理并发送，请勿断开设备。EPUB 转换可能需要一些时间…';
  try {
    const result = await request('/api/library/send', JSON.stringify(payload));
    $('detail-message').textContent = result.message;
    currentBook = await request('/api/library/book?id=' + encodeURIComponent(currentBook.id));
    toast(result.message);
    await load(); await loadLibrary();
  } catch (error) { $('detail-message').textContent = error.message; toast(error.message, true); }
  finally { setLibraryWorking(false); }
}
window.addEventListener('desk-state', () => {
  $('local-import').disabled = busy;
  $('empty-import').disabled = busy;
  if ($('book-dialog').open) renderFiles();
  if (state?.notes) $('stats-notes').textContent = state.notes.length;
});
// "/" jumps to the search box, like most desktop libraries; ignored inside inputs and open dialogs.
document.addEventListener('keydown', event => {
  if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey) return;
  if ($('local-view').hidden || $('book-dialog').open || ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
  event.preventDefault();
  $('local-search').focus();
});
loadLibrary();
