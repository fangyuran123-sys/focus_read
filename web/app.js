'use strict';
const $ = id => document.getElementById(id);
let state = null, selected = [], busy = false, toastTimer;
const size = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
const when = n => n ? new Date(n * 1000).toLocaleString('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '尚未读取';
function element(tag, cls, text) { const el = document.createElement(tag); if (cls) el.className = cls; if (text != null) el.textContent = text; return el; }
function toast(message, error=false) { clearTimeout(toastTimer); $('toast').textContent=message; $('toast').className='toast'+(error?' error':''); $('toast').hidden=false; toastTimer=setTimeout(()=>{$('toast').hidden=true;},6500); }
async function request(path, body, binary=false) {
  const options = body === undefined ? {} : {method:'POST',headers:{'X-Desk-Token':state?.token||''},body};
  const response = await fetch(path, options);
  if (!response.ok) { const info=await response.json(); throw new Error(info.error || '操作失败，请重试。'); }
  return binary ? response.blob() : response.json();
}
function renderNotes() {
  if(!state) return;
  const query=$('search').value.trim().toLocaleLowerCase(), book=$('book-filter').value;
  const notes=state.notes.filter(n=>(!book||n.title===book)&&(!$('only-new').checked||n.new)&&(!query||(n.title+' '+n.text).toLocaleLowerCase().includes(query)));
  const list=$('notes-list'); list.replaceChildren();
  if(!notes.length) {
    const empty=element('div','empty-state'); empty.append(element('div','empty-book','❞'));
    empty.append(element('h3','',state.notes.length?'没有匹配的摘录':'让读过的句子，留在这里'));
    const detail=state.notes.length?'试试其他关键词，或取消筛选。':state.connected?'这次扫描还没有找到高亮或笔记。安全弹出 Kindle，在书中添加一条高亮，再连接并刷新。':'连接 Kindle 并点击刷新，或导入已有的 My Clippings.txt。';
    empty.append(element('p','',detail));
    if(!state.notes.length) {const btn=element('button','text-button empty-action','导入 My Clippings.txt'); btn.onclick=()=>$('clippings-input').click(); empty.append(btn);}
    list.append(empty); return;
  }
  for(const note of notes){
    const article=element('article','note-item');
    article.append(element('p','note-text'+(note.pending?' note-pending':''),note.pending?'已读取标注位置，正文尚未解析。':note.text||'书签'));
    const meta=element('div','note-meta');
    if(note.new) meta.append(element('span','new','新'));
    meta.append(element('span','',note.title),element('span','',note.location));
    if(note.kind==='note')meta.append(element('span','','笔记'));
    article.append(meta); list.append(article);
  }
}
function render() {
  $('connection').textContent=state.connected?'已连接':'未连接'; $('connection').className='status-pill'+(state.connected?'':' offline');
  $('connection-detail').textContent=state.connected?'USB 磁盘 · 连接正常':'离线浏览 · 本地缓存';
  $('device-dot').style.background=state.connected?'var(--green)':'#b4b8c1';
  $('storage-free').textContent=state.storage?(state.storage.free/1073741824).toFixed(2):'—';
  $('storage-total').textContent=state.storage?'共 '+(state.storage.total/1073741824).toFixed(2)+' GiB':'连接后读取';
  $('storage-bar').style.width=state.storage?(100*state.storage.used/state.storage.total)+'%':'0%';
  $('book-count').textContent=state.books.length; $('note-count').textContent=state.notes.length;
  $('last-scan').textContent=when(state.last_scan); $('last-scan').dateTime=state.last_scan?new Date(state.last_scan*1000).toISOString():'';
  const first=state.books[0];
  $('screen-title').textContent=first?first.title.split(' = ')[0].split(' (')[0]:'你的下一本书';
  $('screen-subtitle').textContent=first?'每次翻页，都是新的发现':'传入一本书，开始阅读';
  $('screen-count').textContent=state.books.length+' 本';
  const fresh=state.notes.filter(n=>n.new).length;
  $('notes-summary').textContent=state.notes.length+' 条'+(fresh?' · 新增 '+fresh:'');
  $('export').disabled=!state.notes.length||busy;
  $('export-time').textContent=state.last_export?'上次导出 '+when(state.last_export):'尚未导出';
  const pending=state.notes.filter(n=>n.pending).length;
  $('source-label').textContent=pending?pending+' 条标注正文待解析':'从 Kindle 读取，本地保存';
  $('warnings').hidden=!state.warnings?.length; $('warnings').textContent=(state.warnings||[]).join(' ');
  const previous=$('book-filter').value; $('book-filter').replaceChildren(new Option('全部书籍',''));
  for(const title of [...new Set(state.notes.map(n=>n.title))])$('book-filter').add(new Option(title,title));
  $('book-filter').value=[...$('book-filter').options].some(o=>o.value===previous)?previous:'';
  $('converter-status').textContent=state.converter?'EPUB 会在本地转换为 AZW3':'EPUB 需要安装 calibre；可直接传 PDF / AZW3';
  $('library-count').textContent=state.books.length+' 本'; $('library-status').textContent=state.connected?'本次读取':'离线缓存';
  $('books-list').replaceChildren();
  if(!state.books.length)$('books-list').append(element('p','small muted','还没有读取到书籍。连接 Kindle 后刷新，或从左侧传入。'));
  for(const book of state.books){
    const row=element('article','book-row'), cover=element('div','book-cover',book.title.slice(0,5)), info=element('div','book-info');
    cover.setAttribute('aria-hidden','true'); const title=element('h3','book-title',book.title); title.title=book.title;
    info.append(title,element('p','book-meta',book.format+' · '+size(book.size)));
    const backup=element('a','book-backup','备份原文件 ↗'); backup.href='/api/book?id='+encodeURIComponent(book.id); backup.setAttribute('aria-label','备份 '+book.title); backup.setAttribute('aria-disabled',String(!state.connected));
    if(!state.connected)backup.removeAttribute('href');
    info.append(backup); row.append(cover,info); $('books-list').append(row);
  }
  $('transfer-history').replaceChildren();
  for(const item of state.transfers){
    const row=element('div','transfer-row'), label=element('div'); label.append(element('strong','',item.name),element('small','',when(item.created)+' · '+size(item.size)));
    row.append(label,element('span','done','已复制')); $('transfer-history').append(row);
  }
  if(!state.transfers.length)$('transfer-history').append(element('p','small muted','还没有传输记录。支持单本最大 200 MB。'));
  $('send').disabled=!state.connected||busy; renderNotes();
  $('eject').disabled=!state.connected||busy||state.can_eject===false;
  $('eject').title=state.can_eject===false?'容器模式：停止容器后，在宿主机文件管理器安全弹出 Kindle。':'';
}
async function load(scan=false,notify=false){
  try{let next=await request(scan?'/api/scan':'/api/state',scan?'':undefined);if(!scan&&state&&!state.connected&&next.connected)next=await request('/api/scan','');state=next;render();if(notify)toast(state.connected?'读取完成。'+(state.added?'新增 '+state.added+' 条摘录。':''):'Kindle 未连接，正在显示本地缓存。');}
  catch(error){toast(error.message,true);}
}
$('refresh').onclick=async()=>{if(busy)return; $('refresh').disabled=true; await load(true,true);$('refresh').disabled=false;};
$('eject').onclick=async()=>{if(busy)return;busy=true;render();try{const result=await request('/api/eject','');toast(result.message);}catch(error){toast(error.message,true);}finally{busy=false;await load();}};
['search','book-filter','only-new'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',renderNotes));
$('import-notes').onclick=()=>$('clippings-input').click();
$('clippings-input').onchange=async event=>{const file=event.target.files[0];if(!file)return;try{const result=await request('/api/import?name='+encodeURIComponent(file.name),file);toast('导入完成：新增 '+result.added+' 条'+(result.skipped?'，'+result.skipped+' 条未识别':'')+'。');await load();}catch(error){toast(error.message,true);}event.target.value='';};
$('export').onclick=async()=>{try{const blob=await request('/api/export','',true),url=URL.createObjectURL(blob),a=element('a');a.href=url;a.download='Kindle摘录.zip';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);toast('已导出 Markdown 压缩包，一本书一个文件。');await load();}catch(error){toast(error.message,true);}};
function choose(files){if(busy)return; selected=[...files];$('pending').hidden=!selected.length;$('pending-list').replaceChildren();for(const file of selected)$('pending-list').append(element('div','pending-file',file.name+' · '+size(file.size)));$('transfer-state').hidden=true;}
$('dropzone').onclick=()=>$('book-input').click();$('book-input').onchange=event=>{choose(event.target.files);event.target.value='';};
for(const type of ['dragenter','dragover'])$('dropzone').addEventListener(type,event=>{event.preventDefault();$('dropzone').classList.add('dragging');});
for(const type of ['dragleave','drop'])$('dropzone').addEventListener(type,event=>{event.preventDefault();$('dropzone').classList.remove('dragging');if(type==='drop')choose(event.dataTransfer.files);});
$('cancel-files').onclick=()=>choose([]);
$('send').onclick=async()=>{
  if(busy||!selected.length)return; busy=true; render(); $('dropzone').disabled=true; $('cancel-files').disabled=true;
  $('transfer-state').hidden=false; let completed=0;
  try{for(const file of selected){$('transfer-state').textContent='正在处理 '+file.name+'…'+(file.name.toLowerCase().endsWith('.epub')?' EPUB 转换可能需要一些时间。':'');const result=await request('/api/upload?name='+encodeURIComponent(file.name),file);completed++;$('transfer-state').textContent=result.message;}selected=[];$('pending').hidden=true;toast(completed+' 本书已复制到 Kindle。请安全弹出后查看。');}
  catch(error){selected=selected.slice(completed);$('pending-list').replaceChildren();for(const file of selected)$('pending-list').append(element('div','pending-file',file.name+' · '+size(file.size)));$('transfer-state').textContent=error.message;toast(error.message,true);}
  finally{busy=false;$('dropzone').disabled=false;$('cancel-files').disabled=false;await load();}
};
load();
setInterval(()=>{if(!busy&&!document.hidden)load();},15000);
