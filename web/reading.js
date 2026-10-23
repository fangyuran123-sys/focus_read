'use strict';
let readingSequence = 0, readingScan = null;

function readingDuration(seconds) {
  if (!seconds) return '无已导入会话';
  if (seconds < 60) return Math.round(seconds) + ' 秒';
  const minutes = Math.floor(seconds / 60);
  return minutes >= 60 ? Math.floor(minutes / 60) + ' 小时 ' + minutes % 60 + ' 分' : minutes + ' 分';
}
function readingLevel(seconds) {
  return !seconds ? 0 : seconds < 900 ? 1 : seconds < 1800 ? 2 : seconds < 3600 ? 3 : 4;
}
function readingCalendar(year) {
  // UTC is only calendar arithmetic here; activity dates are already grouped by the server's local timezone.
  const first = new Date(Date.UTC(year, 0, 1)), last = new Date(Date.UTC(year, 11, 31));
  const cells = Array((first.getUTCDay() + 6) % 7).fill(null);
  for (let date = first; date <= last; date = new Date(date.getTime() + 86400000)) cells.push(date.toISOString().slice(0, 10));
  return cells;
}
function renderReading(result) {
  const year = String(result.year);
  $('reading-year').replaceChildren(...result.years.map(y => new Option(y + ' 年', y)));
  $('reading-year').value = year;
  $('reading-summary').textContent = year + ' 年 / ' + result.active_days + ' 天有会话 / ' + readingDuration(result.seconds);
  $('reading-empty').hidden = result.sessions !== 0;
  $('reading-empty').textContent = result.first_date ? '所选年份没有已导入的会话，可切换年份查看。' : '还没有可用会话。连接 Kindle 后点击“刷新设备”。设备缓存可能已被清理，无法补回没有保留的历史。';
  $('reading-history').textContent = result.synced_at ? '最近导入：' + when(result.synced_at) + (result.first_date ? '；已保留记录范围：' + result.first_date + ' 至 ' + result.last_date : '；缓存中没有已结束会话') : '尚未导入设备阅读会话。';
  const grid = $('reading-heatmap'), months = $('heatmap-months');
  grid.replaceChildren(); months.replaceChildren();
  let selected = result.today.startsWith(year + '-') ? result.today : Object.keys(result.days).sort().at(-1) || year + '-01-01';
  const choose = button => {
    grid.querySelectorAll('button').forEach(cell => {
      const active = cell === button;
      cell.setAttribute('aria-pressed', String(active)); cell.tabIndex = active ? 0 : -1;
    });
    selected = button.dataset.date;
    $('reading-day').textContent = selected + ' / ' + readingDuration(result.days[selected] || 0);
  };
  const cells = readingCalendar(result.year), columns = Math.ceil(cells.length / 7);
  for (let week = 0; week < columns; week++) {
    const dates = cells.slice(week * 7, week * 7 + 7).filter(Boolean);
    const month = dates.find(date => date.endsWith('-01'));
    months.append(element('span', '', month ? Number(month.slice(5, 7)) + '月' : ''));
  }
  grid.style.gridTemplateColumns = 'repeat(' + columns + ', var(--heat-cell))';
  months.style.gridTemplateColumns = 'repeat(' + columns + ', var(--heat-cell))';
  for (const date of cells) {
    if (!date) { grid.append(element('span', 'heatmap-pad')); continue; }
    const seconds = result.days[date] || 0;
    const button = element('button', 'heatmap-cell heat-level-' + readingLevel(seconds));
    button.type = 'button'; button.dataset.date = date;
    button.title = date + ' / ' + readingDuration(seconds);
    button.setAttribute('aria-label', button.title);
    button.setAttribute('aria-pressed', String(date === selected));
    button.tabIndex = date === selected ? 0 : -1;
    button.onclick = () => choose(button);
    button.onfocus = () => choose(button);
    button.onkeydown = event => {
      const step = {ArrowUp: -1, ArrowDown: 1, ArrowLeft: -7, ArrowRight: 7}[event.key];
      if (!step) return;
      event.preventDefault();
      const buttons = [...grid.querySelectorAll('button')], index = buttons.indexOf(button);
      buttons[Math.min(buttons.length - 1, Math.max(0, index + step))].focus();
    };
    grid.append(button);
  }
  $('reading-day').textContent = selected + ' / ' + readingDuration(result.days[selected] || 0);
  const selectedCell = grid.querySelector('[data-date="' + selected + '"]');
  if (selectedCell) {
    const scroll = $('heatmap-months').parentElement;
    scroll.scrollLeft = selectedCell.offsetLeft - scroll.offsetLeft - scroll.clientWidth / 2;
  }
}
async function loadReading() {
  const sequence = ++readingSequence;
  $('reading-error').hidden = true;
  const year = $('reading-year').value;
  try {
    const result = await request('/api/reading' + (year ? '?year=' + encodeURIComponent(year) : ''));
    if (sequence === readingSequence) renderReading(result);
  } catch (error) {
    if (sequence !== readingSequence) return;
    $('reading-error').textContent = '阅读日历读取失败：' + error.message + ' 请重新打开此页重试。';
    $('reading-error').hidden = false;
  }
}
$('reading-year').onchange = loadReading;
window.addEventListener('desk-state', () => {
  if ($('reading-view').hidden || !state || readingScan === state.last_scan) return;
  readingScan = state.last_scan;
  loadReading();
});
