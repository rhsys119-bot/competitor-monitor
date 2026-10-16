(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const READ_KEY = 'competitor-monitor.read.v1';
  const PAGE_SIZE = 100;
  const state = {view: 'all', company: 'all', topic: 'all', query: '', channelQuery: '', includeOlder: false, items: [], sources: [], total: 0, olderCount: 0, selected: null, status: null, windowStart: null, now: null, loading: false, loaded: false, sourcesLoaded: false, sourceLoading: false, baselineCount: null, pendingCount: 0, firstEmptyRefreshPending: true, feedError: false, read: new Set()};
  let debounceTimer, toastTimer, snapshotLoading = false, snapshotBytes = 0;
  const POLL_MS = 5 * 60 * 1000;
  Object.assign(state, {allItems: [], pendingSnapshot: null, knownIds: new Set(), publishedAt: null, lastFetch: 0});
  let readStorageAvailable = true;
  try { const saved = JSON.parse(localStorage.getItem(READ_KEY) || '[]'); if (Array.isArray(saved)) state.read = new Set(saved.filter((id) => typeof id === 'string').slice(-50000)); } catch (_) { readStorageAvailable = false; }
  const escapeHTML = (value) => String(value == null ? '' : value).replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  function safeURL(value) { try { const url = new URL(String(value)); return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''; } catch (_) { return ''; } }
  function dateObject(value) { if (!value) return null; const date = new Date(value); return Number.isNaN(date.getTime()) ? null : date; }
  function formatDate(value, short = false) { const date = dateObject(value); if (!date) return '게시일 확인 중'; return new Intl.DateTimeFormat('ko-KR', {timeZone: 'Asia/Seoul', ...(short ? {} : {year: 'numeric'}), month: '2-digit', day: '2-digit'}).format(date).replace(/\.\s*$/, '').replace(/\.\s*/g, '.'); }
  function formatTime(value) { const date = dateObject(value); if (!date) return '아직 없음'; return new Intl.DateTimeFormat('ko-KR', {timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false}).format(date); }
  function bytes(value) { const n = Number(value); if (!Number.isFinite(n) || n < 0) return '확인 중'; if (n < 1024) return `${n.toLocaleString('ko-KR')} B`; if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`; if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`; return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`; }
  function count(value) { return Math.max(0, Number(value) || 0).toLocaleString('ko-KR'); }
  function announce(message) { $('announcement').textContent = message; }
  function toast(message) { clearTimeout(toastTimer); $('toast').textContent = message; $('toast').hidden = false; toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3500); }
  function persistRead() { try { localStorage.setItem(READ_KEY, JSON.stringify([...state.read].slice(-50000))); } catch (_) { readStorageAvailable = false; document.querySelector('.read-note').textContent = '브라우저 저장공간을 사용할 수 없어 읽음 상태는 이번 화면에서만 유지됩니다.'; announce('브라우저 저장공간을 사용할 수 없어 이번 화면에서만 읽음 상태를 유지합니다.'); } }
  function isOlder(item) { const published = dateObject(item.published_at), start = dateObject(state.windowStart); return Boolean(published && start && published < start); }
  function visibleItems() { return state.items.filter((item) => state.view !== 'unread' || !state.read.has(String(item.id))); }
  function synchronizeSelection() { const list = visibleItems(); if (!list.some((item) => String(item.id) === state.selected)) state.selected = list.length ? String(list[0].id) : null; }
  function companyAvatar(company) { const name = company === 'LS' ? 'LS' : 'TYM'; return `<span class="company-avatar${name === 'LS' ? ' ls' : ''}" aria-hidden="true">${name}</span>`; }
  function sourcesOf(item) { const result = Array.isArray(item.sources) ? item.sources.filter((source) => safeURL(source.url)) : []; if (result.length) return result; return safeURL(item.url) ? [{name: item.source_name || '공식 원문', url: item.url, published_at: item.published_at}] : []; }
  function renderControls() { document.querySelectorAll('[data-view]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.view === state.view))); document.querySelectorAll('[data-company]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.company === state.company))); const directory = state.view === 'channels'; $('feed-section').hidden = directory; $('directory-section').hidden = !directory; $('topic-wrap').hidden = directory; $('topic').value = state.topic; $('search').value = directory ? state.channelQuery : state.query; $('search').placeholder = directory ? '공식채널 검색' : '과거 소식까지 검색'; $('search-label').textContent = directory ? '공식채널 검색' : '과거 소식까지 검색'; $('search-hint').hidden = directory; renderPending(); }
  function renderPending() { $('new-items').hidden = !state.pendingCount || state.view === 'channels'; $('new-items-text').textContent = `새 소식 ${count(state.pendingCount)}건이 도착했습니다.`; }
  function renderFeed() {
    synchronizeSelection();
    const list = visibleItems();
    const scope = state.query.trim() ? '저장된 전체 기간 검색' : state.includeOlder ? '이전 소식 포함' : `최근 2주${state.windowStart && state.now ? ` · ${formatDate(state.windowStart, true)}–${formatDate(state.now, true)}` : ''}`;
    const shown = state.view === 'unread' ? `불러온 ${count(state.items.length)}건 중 미확인 ${count(list.length)}건` : state.items.length < state.total ? `${count(state.total)}건 중 ${count(list.length)}건` : `${count(list.length)}건`;
    $('scope').textContent = state.loading && !state.loaded ? '소식을 불러오고 있습니다.' : `${scope} · ${shown} · 게시일 최신순`;
    $('item-list').setAttribute('aria-busy', String(state.loading));
    $('mark-all').disabled = state.loading || !list.some((item) => !state.read.has(String(item.id)));
    $('load-more').hidden = state.items.length >= state.total || !state.loaded;
    $('load-more').disabled = state.loading;
    $('load-more').textContent = state.loading ? '불러오는 중…' : `소식 더 보기 · ${count(state.total - state.items.length)}건 남음`;
    $('older-toggle').hidden = Boolean(state.query.trim()) || (!state.olderCount && !state.includeOlder) || !state.loaded;
    $('older-toggle').disabled = state.loading;
    $('older-toggle').textContent = state.includeOlder ? '이전 소식 접기' : `이전 소식 펼쳐보기 · ${count(state.olderCount)}건`;
    $('older-toggle').setAttribute('aria-expanded', String(state.includeOlder));
    if (state.loading && !state.loaded) { renderDetail(); return; }
    if (!list.length) {
      let title = '조건에 맞는 소식이 없습니다.', description = '회사나 주제를 바꾸거나 다른 검색어로 찾아보세요.';
      if (!state.feedError && !state.query.trim() && state.company === 'all' && state.topic === 'all' && !state.total) {
        if (!state.publishedAt && !state.feedError) { title = '첫 소식을 확인하고 있어요.'; description = '공식채널 확인이 끝나면 실제 수집한 소식이 여기에 표시됩니다.'; }
        else if (state.olderCount && !state.includeOlder) { title = '최근 2주에 등록된 소식이 없습니다.'; description = '이전 소식을 펼치거나 공식채널에서 직접 확인해 보세요.'; }
        else { title = '아직 수집된 소식이 없습니다.'; description = '자동 수집 결과가 여기에 표시됩니다. 공식채널에서도 직접 확인할 수 있어요.'; }
      }
      if (state.view === 'unread' && state.items.length) { title = '표시할 미확인 소식이 없습니다.'; description = state.items.length < state.total ? '불러온 소식을 모두 확인했습니다. 더 보기를 눌러 다음 소식도 확인하세요.' : '전체 소식 탭에서는 읽은 소식도 다시 볼 수 있습니다.'; }
      if (state.feedError) { title = '소식을 불러오지 못했습니다.'; description = '인터넷 연결을 확인한 뒤 최신 결과를 다시 불러와 주세요.'; }
      $('item-list').innerHTML = `<div class="empty-state"><span class="empty-mark" aria-hidden="true">○</span><h2>${escapeHTML(title)}</h2><p>${escapeHTML(description)}</p>${!state.feedError && !state.items.length ? '<button type="button" class="button" data-view="channels">공식채널 보기 <span aria-hidden="true">↗</span></button>' : ''}</div>`;
    } else {
      $('item-list').innerHTML = list.map((item) => { const read = state.read.has(String(item.id)); const sources = sourcesOf(item); return `<article class="content-card" data-selected="${state.selected === String(item.id)}"><button type="button" class="content-button" data-open="${escapeHTML(item.id)}" aria-pressed="${state.selected === String(item.id)}" aria-label="${escapeHTML(item.title)} 미리보기"><span class="content-meta">${companyAvatar(item.company)}<span class="company-name">${escapeHTML(item.company)}</span><span class="meta-divider" aria-hidden="true"></span><span>${escapeHTML(item.platform || item.source_name || '공식채널')}</span><span class="topic-tag">${escapeHTML(item.topic || '기타')}</span>${read ? '<span class="read-label">읽음</span>' : '<span class="unread-dot">미확인</span>'}</span><span class="content-title">${escapeHTML(item.title || '제목 없음')}</span><span class="content-bottom"><time datetime="${escapeHTML(item.published_at || '')}">${formatDate(item.published_at)} 게시</time>${isOlder(item) ? '<span class="old-badge">2주 이전</span>' : ''}${sources.length > 1 ? `<span>공식 출처 ${count(sources.length)}개</span>` : ''}<span class="content-preview-arrow" aria-hidden="true">↗</span></span></button></article>`; }).join('');
    }
    renderDetail();
  }
  function renderDetail() {
    const item = state.items.find((entry) => String(entry.id) === state.selected);
    if (!item) { $('detail-panel').innerHTML = '<div class="detail-placeholder"><span class="preview-symbol" aria-hidden="true">↗</span><h2>소식을 선택해 주세요</h2><p>목록에서 선택하면 내용과<br>공식 원문 링크를 볼 수 있습니다.</p></div>'; return; }
    const read = state.read.has(String(item.id)), sources = sourcesOf(item), original = safeURL(item.url) || (sources[0] && safeURL(sources[0].url));
    $('detail-panel').innerHTML = `<div class="detail-label"><span>컨텐츠 미리보기</span><span class="read-label">${read ? '읽음' : '아직 확인하지 않은 소식'}</span></div><button type="button" id="back-to-list" class="text-button detail-back">← 목록으로 돌아가기</button><div class="content-meta">${companyAvatar(item.company)}<span class="company-name">${escapeHTML(item.company)}</span><span class="topic-tag">${escapeHTML(item.topic || '기타')}</span><span>${formatDate(item.published_at)}</span></div><h2>${escapeHTML(item.title || '제목 없음')}</h2><p class="detail-summary">${escapeHTML(item.summary || '상세 내용은 공식 원문에서 확인할 수 있습니다.')}</p>${item.summary ? '<p class="summary-caption">자세한 내용은 원문에서 확인하세요.</p>' : ''}<hr class="detail-separator"><p class="source-heading">${sources.length > 1 ? '같은 소식의 공식 출처' : '공식 출처'}</p>${sources.map((source) => `<div class="original-source"><a href="${escapeHTML(safeURL(source.url))}" target="_blank" rel="noopener noreferrer">${escapeHTML(source.name || item.source_name || '원문')} ↗</a><span>${source.published_at ? formatDate(source.published_at) : ''}</span></div>`).join('')}<div class="detail-actions">${original ? `<a class="button button-primary" href="${escapeHTML(original)}" target="_blank" rel="noopener noreferrer">원문 보기 <span aria-hidden="true">↗</span></a>` : ''}<button type="button" class="button" data-read="${escapeHTML(item.id)}">${read ? '미확인으로 되돌리기' : '읽음 처리'}</button></div>`;
  }
  function updateWindow() {
    const now = new Date();
    const kstToday = new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit'}).formatToParts(now);
    const part = (name) => kstToday.find((entry) => entry.type === name).value;
    const midnight = new Date(`${part('year')}-${part('month')}-${part('day')}T00:00:00+09:00`);
    state.windowStart = new Date(midnight.getTime() - 13 * 86400000).toISOString();
    state.now = now.toISOString();
  }
  async function loadItems(append = false, preserveSelection = false) {
    updateWindow();
    const oldSelected = state.selected;
    const q = state.query.trim().toLocaleLowerCase('ko-KR');
    const matching = state.allItems.filter((item) => (state.company === 'all' || item.company === state.company) && (state.topic === 'all' || item.topic === state.topic) && (!q || `${item.title || ''} ${item.summary || ''} ${item.source_name || ''} ${item.company || ''} ${(item.sources || []).map((source) => source.name || '').join(' ')}`.toLocaleLowerCase('ko-KR').includes(q)));
    state.olderCount = matching.filter(isOlder).length;
    const filtered = matching.filter((item) => q || state.includeOlder || !isOlder(item));
    const limit = append ? state.items.length + PAGE_SIZE : preserveSelection ? Math.max(PAGE_SIZE, state.items.length) : PAGE_SIZE;
    state.items = filtered.slice(0, limit);
    state.total = filtered.length;
    state.loaded = true;
    state.loading = false;
    state.selected = preserveSelection ? oldSelected : null;
    renderFeed();
    announce(`${state.items.length}건의 소식을 불러왔습니다.`);
  }
  function applySnapshot(data, preserveSelection = false) {
    state.allItems = data.items.filter((item) => item && item.id != null && dateObject(item.published_at) && dateObject(item.published_at) <= new Date()).sort((a, b) => dateObject(b.published_at) - dateObject(a.published_at) || String(a.id).localeCompare(String(b.id)));
    state.knownIds = new Set(state.allItems.map((item) => String(item.id)));
    state.pendingSnapshot = null;
    state.pendingCount = 0;
    loadItems(false, preserveSelection);
    renderPending();
  }
  async function loadSnapshot(silent = false) {
    if (snapshotLoading) return;
    snapshotLoading = true;
    state.lastFetch = Date.now();
    $('refresh-button').disabled = true;
    $('refresh-button').textContent = '불러오는 중…';
    try {
      const response = await fetch(`./data/news.json?t=${Date.now()}`, {cache: 'no-store', credentials: 'same-origin', headers: {'Accept': 'application/json'}});
      if (!response.ok) throw new Error(`요청 실패 (${response.status})`);
      const raw = await response.text();
      const data = JSON.parse(raw);
      if (data.schema_version !== 1 || !Array.isArray(data.items) || !Array.isArray(data.sources) || !dateObject(data.generated_at)) throw new Error('응답 형식 오류');
      if (state.publishedAt && dateObject(data.generated_at) < dateObject(state.publishedAt)) return;
      snapshotBytes = new Blob([raw]).size;
      state.sources = data.sources.filter((source) => source && typeof source === 'object');
      state.sourcesLoaded = true;
      state.publishedAt = data.generated_at;
      state.status = {...(data.status || {}), item_count: data.items.length, source_count: state.sources.length, auto_count: state.sources.filter((source) => source.mode === 'auto').length};
      state.feedError = false;
      ['feed-error', 'status-error', 'source-error'].forEach((id) => { $(id).hidden = true; });
      const incoming = data.items.filter((item) => item && item.id != null && !state.knownIds.has(String(item.id)));
      if (silent && state.loaded && incoming.length) { state.pendingSnapshot = data; state.pendingCount = incoming.length; loadItems(false, true); renderPending(); }
      else applySnapshot(data, silent);
      renderSources();
      renderStatus();
      if (!silent && state.loaded) toast('게시된 최신 수집 결과를 불러왔습니다.');
    } catch (_) {
      $('connection').dataset.state = 'error';
      $('connection-text').textContent = '결과 확인 필요';
      $('status-error').hidden = false;
      $('status-error-text').textContent = state.loaded ? '최신 결과를 불러오지 못했습니다. 마지막으로 불러온 소식은 계속 볼 수 있습니다.' : '수집 결과를 불러오지 못했습니다. 인터넷 연결을 확인하거나 잠시 후 다시 불러와 주세요.';
      if (!state.loaded) { state.feedError = true; state.loaded = true; renderFeed(); }
    } finally {
      snapshotLoading = false;
      $('refresh-button').disabled = false;
      $('refresh-button').textContent = '최신 결과 불러오기';
    }
  }
  function sourceStatus(source) {
    const status = source.status || 'idle';
    const labels = {idle: '첫 수집 전', collecting: '최근 확인 중', ok: '정상', error: '오류', manual: '직접 확인', dormant: '휴면'};
    return {label: labels[status] || '상태 확인 중', css: ['ok', 'error', 'dormant'].includes(status) ? status : ''};
  }
  function renderSources() {
    const q = state.channelQuery.trim().toLocaleLowerCase('ko-KR');
    const list = state.sources.filter((source) => (state.company === 'all' || source.company === state.company) && `${source.name} ${source.platform} ${source.region} ${source.company}`.toLocaleLowerCase('ko-KR').includes(q));
    $('channel-count').textContent = count(state.sources.length); $('channel-count').hidden = !state.sourcesLoaded;
    const autoCount = list.filter((source) => source.mode === 'auto').length;
    $('directory-scope').textContent = state.sourcesLoaded ? `공식채널 ${count(list.length)}개 · 자동 수집 ${count(autoCount)}개 · 직접 확인 ${count(list.length - autoCount)}개` : '공식채널을 불러오고 있습니다.';
    $('directory').setAttribute('aria-busy', String(state.sourceLoading));
    if (!list.length) { $('directory').innerHTML = state.sourcesLoaded ? `<div class="empty-state"><h2>${state.sources.length ? '조건에 맞는 공식채널이 없습니다.' : '등록된 채널이 없습니다.'}</h2><p>${state.sources.length ? '회사나 검색어를 바꿔 주세요.' : '채널 설정을 확인해 주세요.'}</p></div>` : '<div class="loading-card"><span class="loading-line"></span><span class="loading-line long"></span><span class="loading-line short"></span></div>'; return; }
    const companies = [...new Set(list.map((source) => source.company))].sort((a, b) => (a === 'TYM' ? -1 : b === 'TYM' ? 1 : String(a).localeCompare(String(b))));
    $('directory').innerHTML = companies.map((company) => {
      const group = list.filter((source) => source.company === company);
      const regions = [...new Set(group.map((source) => source.region || '국내'))].sort((a, b) => (a === '국내' ? -1 : b === '국내' ? 1 : a.localeCompare(b)));
      return `<section class="company-directory"><h2 class="company-heading">${companyAvatar(company)}${escapeHTML(company === 'LS' ? 'LS엠트론 · LS트랙터' : company)}<span>${count(group.length)}개 채널</span></h2>${regions.map((region) => `<h3 class="region-heading">${escapeHTML(region)}</h3>${group.filter((source) => (source.region || '국내') === region).map((source) => {
        const status = sourceStatus(source), url = safeURL(source.url), manual = source.mode !== 'auto';
        const message = source.message || (manual ? '공식채널에서 새 게시물을 직접 확인해 주세요.' : source.status === 'dormant' ? '최근 6개월간 새 소식이 없어 수집을 쉬고 있습니다.' : source.status === 'idle' ? '첫 수집을 기다리고 있습니다.' : '주기적으로 새 소식을 확인합니다.');
        return `<article class="channel-card"><div class="channel-top"><div>${url ? `<a class="channel-title" href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(source.name)}<span class="external-arrow" aria-hidden="true">↗</span></a>` : `<span class="channel-title">${escapeHTML(source.name)}</span>`}<span class="channel-platform">${escapeHTML(source.platform || '홈페이지')}</span></div><div class="channel-statuses"><span class="channel-badge${manual ? '' : ' auto'}">${manual ? '직접 확인' : '자동 수집'}</span>${status.label !== '직접 확인' ? `<span class="channel-badge ${status.css}">${status.label}</span>` : ''}</div></div><p class="channel-message${source.status === 'error' ? ' error' : ''}">${escapeHTML(message)}</p><div class="channel-history"><span>최근 게시 ${source.last_item_at ? formatDate(source.last_item_at) : '확인 전'}</span>${!manual ? `<span>최근 성공 ${source.last_success ? formatTime(source.last_success) : '아직 없음'}</span><span>저장 ${count(source.item_count)}건</span>` : ''}</div></article>`;
      }).join('')}`).join('')}</section>`;
    }).join('');
  }
  async function loadSources() { if (!state.sourcesLoaded) await loadSnapshot(); else renderSources(); }
  function renderStatus() {
    const status = state.status; if (!status) return;
    const lastRun = status.last_run && typeof status.last_run === 'object' ? status.last_run.finished_at || status.last_run.started_at : status.last_run;
    const successTimes = state.sources.filter((source) => source.mode === 'auto').map((source) => source.last_success).filter((value) => dateObject(value)).sort((a, b) => dateObject(b) - dateObject(a));
    const latestSuccess = successTimes[0];
    const errors = state.sources.filter((source) => source.status === 'error').length;
    const stale = !dateObject(lastRun) || Date.now() - dateObject(lastRun).getTime() > 6 * 3600000;
    $('connection').dataset.state = stale || errors ? 'error' : 'ok';
    $('connection-text').textContent = stale ? '수집 시각 확인 필요' : errors ? `일부 채널 확인 필요 · ${count(errors)}개` : '수집 결과 최신';
    $('last-run').textContent = formatTime(lastRun);
    $('item-count').textContent = `${count(status.item_count)}건`;
    $('source-count').textContent = `${count(status.source_count)}개 · 자동 ${count(status.auto_count)}개`;
    $('next-run').textContent = '매시간 · 예약 실행';
    $('last-success').textContent = latestSuccess ? formatTime(latestSuccess) : '아직 없음';
    $('storage-size').textContent = bytes(snapshotBytes);
    $('current-source').textContent = formatTime(state.publishedAt);
    $('status-description').textContent = `${stale ? '최근 수집 기록이 6시간 이상 갱신되지 않았습니다. 운영자가 자동 실행 기록을 확인해 주세요. ' : ''}${errors ? `자동 수집 오류 ${count(errors)}개 채널이 있습니다. 공식채널 탭에서 확인하세요. ` : ''}PC가 꺼져 있어도 매시간 자동으로 확인합니다. 예약 실행은 지연될 수 있습니다. 이 페이지는 공개된 최근 수집 결과를 표시하며, 새 수집을 직접 시작하지 않습니다.`;
    $('refresh-button').hidden = false;
    $('refresh-button').disabled = snapshotLoading;
    $('refresh-button').textContent = snapshotLoading ? '불러오는 중…' : '최신 결과 불러오기';
  }
  async function pollStatus() {
    if (Date.now() - state.lastFetch >= POLL_MS) await loadSnapshot(true);
  }
  function changeView(view) { if (!['all', 'unread', 'channels'].includes(view)) return; const needsReload = state.view === 'channels' || Boolean(debounceTimer); clearTimeout(debounceTimer); debounceTimer = null; state.view = view; renderControls(); if (view === 'channels') { renderSources(); if (!state.sourcesLoaded) loadSources(); } else if (needsReload) loadItems(); else { renderFeed(); if (!state.loaded && !state.loading) loadItems(); } }
  document.addEventListener('click', async (event) => {
    const button = event.target.closest('button'); if (!button || button.disabled) return;
    if (button.dataset.view) { changeView(button.dataset.view); return; }
    if (button.dataset.company) { clearTimeout(debounceTimer); debounceTimer = null; state.company = button.dataset.company; renderControls(); if (state.view === 'channels') renderSources(); else loadItems(); return; }
    if (button.dataset.open) { state.selected = button.dataset.open; renderFeed(); const opened = [...document.querySelectorAll('[data-open]')].find((entry) => entry.dataset.open === state.selected); if (opened) opened.focus({preventScroll: true}); if (window.matchMedia('(max-width: 760px)').matches) $('detail-panel').scrollIntoView({behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start'}); return; }
    if (button.dataset.read) { const id = button.dataset.read; const wasRead = state.read.has(id); if (wasRead) state.read.delete(id); else state.read.add(id); persistRead(); renderFeed(); toast(wasRead ? '미확인으로 되돌렸습니다.' : '읽음 처리했습니다. 전체 소식에서 다시 볼 수 있습니다.'); return; }
    if (button.id === 'older-toggle') { state.includeOlder = !state.includeOlder; await loadItems(); announce(state.includeOlder ? '이전 소식까지 펼쳤습니다.' : '최근 2주 소식만 표시합니다.'); }
    else if (button.id === 'back-to-list') { const opened = [...document.querySelectorAll('[data-open]')].find((entry) => entry.dataset.open === state.selected); if (opened) { opened.scrollIntoView({behavior: 'auto', block: 'center'}); opened.focus({preventScroll: true}); } }
    else if (button.id === 'load-more') loadItems(true);
    else if (button.id === 'mark-all') { const unread = visibleItems().filter((item) => !state.read.has(String(item.id))); unread.forEach((item) => state.read.add(String(item.id))); persistRead(); renderFeed(); toast(`표시된 ${unread.length}건을 읽음 처리했습니다.`); }
    else if (button.id === 'show-new-items') { if (state.pendingSnapshot) applySnapshot(state.pendingSnapshot); }
    else if (button.id === 'refresh-button') loadSnapshot();
    else if (button.id === 'retry-feed') loadSnapshot();
    else if (button.id === 'retry-sources') loadSnapshot();
    else if (button.id === 'retry-status') loadSnapshot();
  });
  $('search').addEventListener('input', (event) => { clearTimeout(debounceTimer); debounceTimer = null; if (state.view === 'channels') { state.channelQuery = event.target.value; renderSources(); } else { state.query = event.target.value; debounceTimer = setTimeout(() => { debounceTimer = null; loadItems(); }, 300); } });
  $('search').addEventListener('keydown', (event) => { if (event.key === 'Enter' && state.view !== 'channels') { clearTimeout(debounceTimer); debounceTimer = null; loadItems(); } });
  $('topic').addEventListener('change', (event) => { clearTimeout(debounceTimer); debounceTimer = null; state.topic = event.target.value; loadItems(); });
  window.addEventListener('storage', (event) => { if (event.key !== READ_KEY) return; try { const saved = JSON.parse(event.newValue || '[]'); if (Array.isArray(saved)) { state.read = new Set(saved.filter((id) => typeof id === 'string')); renderFeed(); } } catch (_) {} });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) pollStatus(); });
  renderControls();
  loadSnapshot();
  setInterval(() => { if (!document.hidden) pollStatus(); }, POLL_MS);
  if (!readStorageAvailable) { document.querySelector('.read-note').textContent = '읽음 처리는 선택입니다. 브라우저 저장공간을 사용할 수 없어 이번 화면에서만 유지됩니다.'; }
})();
