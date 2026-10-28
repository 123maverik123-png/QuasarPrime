/*  «YouTube на колонке»: превью, очередь и история для панели «Станция».
 *
 *  Встроенного мини-плеера здесь больше нет — и включить его нельзя: IFrame API
 *  YouTube, <iframe>, громкость и перемотка в браузере удалены. Звук всегда
 *  играет сама Станция: карточка управляет мостом quasard-youtube
 *  (127.0.0.1:8099 через прокси панели /api/yt-bridge) и показывает то, что он
 *  отдаёт — обложку ролика, позицию, паузу.
 *
 *  Что делает модуль:
 *    • «На колонке» — включить ссылку/запрос прямо сейчас;
 *    • «В очередь» — поставить следующим; очередь играет сама: когда мост
 *      сообщает state=finished и трек доигран до конца, уходит следующий запрос;
 *    • история — что уже играло на колонке (заполняется по данным моста,
 *      поэтому туда попадают и треки, включённые голосом, а не только из панели).
 *
 *  Очередь и история живут в localStorage панели (yt.queue / yt.history) и
 *  переживают перезагрузку страницы. Модуль самостоятельный: разметка карточки
 *  #yt-card должна быть в index.html, внутренние хелперы панели не нужны.
 */
(() => {
  'use strict';

  const card = document.getElementById('yt-card');
  if (!card) return; // карточки на странице нет — модуль не нужен

  // ---------- утилиты ----------
  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = t => {
    t = Math.max(0, Math.floor(Number(t) || 0));
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : String(m)) + ':' + String(s).padStart(2, '0');
  };
  const clock = ts => {
    const d = new Date(Number(ts) || 0), now = new Date();
    const hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    if (d.toDateString() === now.toDateString()) return hm;
    return String(d.getDate()).padStart(2, '0') + '.' + String(d.getMonth() + 1).padStart(2, '0') + ' ' + hm;
  };
  const short = e => {
    const m = String((e && e.message) || e || 'ошибка');
    return m.length > 90 ? m.slice(0, 90) + '…' : m;
  };

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { } },
    json(k, dflt) { try { const v = JSON.parse(store.get(k)); return v == null ? dflt : v; } catch { return dflt; } },
    setJson(k, v) { store.set(k, JSON.stringify(v)); },
  };
  const pref = k => (window.PanelContext && PanelContext.storageKey ? PanelContext.storageKey(k) : k);
  const K = { queue: pref('yt.queue'), hist: pref('yt.history'), last: pref('yt.last') };
  const apiUrl = p => (window.PanelContext && PanelContext.url) ? PanelContext.url(p) : p;
  // toast() объявлен в общем скрипте панели: typeof безопасен, если его там нет
  const say = (msg, err) => { try { if (typeof toast === 'function') toast(msg, !!err); } catch { } };

  // 2 с, а не 3: паузу кольцом на колонке панель узнаёт из опроса, и с
  // запасом на обнаружение (мост ждёт 1.5 с тишины) тайминг останавливается
  // через 3–4 с, а не через 6.
  const MAX_QUEUE = 100, MAX_HIST = 40, MAX_QUERY = 500, POLL_MS = 2000;

  // Следы старого встроенного плеера (ссылка, позиция, громкость, «недавние»)
  // больше не нужны — вычищаем, чтобы не копились в localStorage.
  ['yt.video', 'yt.vol', 'yt.muted', 'yt.recent'].forEach(k => { try { localStorage.removeItem(k); } catch { } });
  try {
    Object.keys(localStorage).filter(k => k.indexOf('yt.pos.') === 0)
      .forEach(k => { try { localStorage.removeItem(k); } catch { } });
  } catch { }

  // ---------- элементы карточки ----------
  const prevBox = $('yt-prev'), prevImg = $('yt-prev-img');
  const veilEl = $('yt-ov'), veilTitle = $('yt-ov-title'), veilText = $('yt-ov-text');
  const badge = $('yt-badge'), openLink = $('yt-open');
  const titleEl = $('yt-title'), subEl = $('yt-sub');
  const timeBox = $('yt-time'), posEl = $('yt-pos'), durEl = $('yt-dur'), seekInput = $('yt-seek');
  const ctlBox = $('yt-ctl'), toggleBtn = $('yt-toggle'), stopBtn = $('yt-stop');
  const nextBtn = $('yt-next'), searchLink = $('yt-col-search');
  const form = $('yt-form'), urlInput = $('yt-url'), sendBtn = $('yt-send'), addBtn = $('yt-add');
  const qList = $('yt-queue'), qCount = $('yt-q-count'), qClear = $('yt-q-clear');
  const hList = $('yt-history'), hCount = $('yt-h-count'), hClear = $('yt-h-clear');

  if (!form || !urlInput || !qList || !hList || !seekInput) return; // разметка не та — молча выходим

  // ---------- состояние ----------
  let queue = readList(K.queue);
  let hist = readList(K.hist);
  let stream = null;        // последний stream из /api/yt-bridge
  let prevState = '';       // состояние предыдущего опроса — чтобы не запускать «дальше» дважды
  let prevKey = '';
  let histKey = '';         // какой трек уже записан в историю
  let recvAt = 0;           // когда пришёл последний статус (для досчёта позиции)
  let busy = false;
  let polling = false;
  let failures = 0;         // подряд неудачных опросов: один сбой не должен гасить карточку
  let scrubbing = false;    // тянем полосу — опросы не должны перебивать выбранную позицию
  let seekTimer = 0;        // отложенная отправка: клавиши и быстрый drag не шлют запрос на каждый шаг
  let holdUntil = 0;        // пока идёт перемотка, показываем выбранную позицию, а не старую
  let holdPos = 0;
  let holdKey = '';
  const badThumb = new Set();

  function readList(key) {
    const a = store.json(key, []);
    return Array.isArray(a) ? a.filter(x => x && typeof x === 'object' && typeof x.query === 'string') : [];
  }

  const keyOf = st => st ? [st.video_id || '', st.title || '', st.query || ''].join('|') : '';
  const idle = () => !stream || stream.state === 'finished';

  // ---------- мост ----------
  async function call(path, payload) {
    const opts = {
      credentials: 'same-origin',
      cache: 'no-store',
      method: payload ? 'POST' : 'GET',
    };
    if (payload) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(payload);
    }
    const r = await fetch(apiUrl('/api/yt-bridge' + (path || '')), opts);
    const j = await r.json().catch(() => null);
    if (!r.ok || !j || !j.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
    return j;
  }

  // ---------- разбор ссылки ----------
  function parseId(input) {
    const s = String(input || '').trim();
    if (!s) return '';
    if (/^[A-Za-z0-9_-]{11}$/.test(s)) return s;
    const m = s.match(/(?:youtu\.be\/|[?&]v=|\/embed\/|\/shorts\/|\/live\/|\/v\/)([A-Za-z0-9_-]{11})/);
    return m ? m[1] : '';
  }

  // Ссылку оставляем как есть (в ней может быть плейлист), одинокий id
  // и короткую ссылку приводим к youtu.be — так их понимает мост.
  function toQuery(raw) {
    const s = String(raw || '').trim();
    if (!s) return '';
    if (/^https?:\/\//i.test(s)) return s;
    const id = parseId(s);
    return id ? 'https://youtu.be/' + id : s;
  }

  // ---------- отрисовка: текущий трек ----------
  function setBadge(text, kind) {
    if (!badge) return;
    badge.hidden = !text;
    badge.textContent = text || '';
    badge.className = 'badge' + (kind ? ' ' + kind : '');
  }

  function stateBadge(st) {
    switch (st && st.state) {
      case 'streaming': return ['играет', 'ok'];
      case 'paused': return [st.paused_by === 'station' ? 'пауза на колонке' : 'пауза', ''];
      case 'waiting_station': return ['ждёт колонку', ''];
      case 'finished': return ['закончилось', ''];
      default: return ['', ''];
    }
  }

  function displayPos(st) {
    if (holdUntil && Date.now() < holdUntil) return holdPos;
    const base = (Number(st && st.played_ms) || 0) / 1000;
    const dur = Number(st && st.duration) || 0;
    if (st && st.state === 'streaming' && recvAt) {
      const pos = base + (Date.now() - recvAt) / 1000;
      return dur > 0 ? Math.min(dur, pos) : pos;
    }
    return base;
  }

  function stateLine(st) {
    const dur = Number(st && st.duration) || 0;
    if (!(dur > 0)) return '';
    return fmt(displayPos(st)) + ' / ' + fmt(dur);
  }

  function render(st) {
    const vid = st && st.video_id ? String(st.video_id) : '';

    if (vid && !badThumb.has(vid)) {
      const src = 'https://i.ytimg.com/vi/' + encodeURIComponent(vid) + '/hqdefault.jpg';
      if (prevImg.getAttribute('src') !== src) prevImg.src = src;
      prevBox.href = 'https://www.youtube.com/watch?v=' + encodeURIComponent(vid);
      prevBox.hidden = false;
      veilEl.hidden = true;
    } else {
      prevBox.hidden = true;
      veilEl.hidden = false;
      veilEl.classList.remove('bad');
      if (st) {
        veilTitle.textContent = st.title || 'На колонке';
        veilText.textContent = [st.artist, stateLine(st)].filter(Boolean).join(' · ') || 'Трек играет на колонке';
      } else {
        veilTitle.textContent = 'Ничего не играет';
        veilText.textContent = 'Вставьте ссылку или запрос — трек заиграет на колонке, а обложка появится здесь.';
      }
    }

    if (!st) {
      setBadge('');
      titleEl.textContent = 'Ничего не играет';
      subEl.textContent = 'Готов принять ссылку или запрос';
      timeBox.hidden = true;
      seekInput.disabled = true;
      ctlBox.hidden = true;
      openLink.hidden = true;
      nextBtn.disabled = !queue.length;
      return;
    }

    const kind = stateBadge(st);
    setBadge(kind[0], kind[1]);
    titleEl.textContent = st.title || 'Без названия';
    subEl.textContent = [st.artist, stateLine(st)].filter(Boolean).join(' · ') || 'YouTube';

    const dur = Number(st.duration) || 0;
    timeBox.hidden = !(dur > 0);
    seekInput.disabled = !(dur > 0);
    if (dur > 0) {
      const max = Math.max(1, Math.floor(dur));
      if (seekInput.max !== String(max)) seekInput.max = String(max);
      const pos = displayPos(st);
      if (!scrubbing) seekInput.value = String(Math.min(max, Math.round(pos)));
      posEl.textContent = fmt(scrubbing ? Number(seekInput.value) : pos);
      durEl.textContent = fmt(dur);
    }

    ctlBox.hidden = false;
    const playing = st.state === 'streaming';
    toggleBtn.innerHTML = '<svg width="22" height="22" aria-hidden="true"><use href="#' +
      (playing ? 'i-pause' : 'i-play') + '"/></svg>';
    toggleBtn.title = playing ? 'Пауза' : 'Продолжить';
    toggleBtn.setAttribute('aria-label', toggleBtn.title);

    openLink.hidden = !vid;
    if (vid) openLink.href = 'https://www.youtube.com/watch?v=' + encodeURIComponent(vid);
    // по ссылке искать бессмысленно — ищем по названию и исполнителю
    const searchFor = st.query && !/^https?:\/\//i.test(st.query)
      ? st.query : [st.title, st.artist].filter(Boolean).join(' ');
    searchLink.href = 'https://www.youtube.com/results?search_query=' + encodeURIComponent(searchFor);
    nextBtn.disabled = !queue.length;
  }

  function showOffline(err) {
    stream = null;
    prevState = ''; prevKey = ''; histKey = '';
    holdUntil = 0;
    prevBox.hidden = true;
    veilEl.hidden = false;
    veilEl.classList.add('bad');
    veilTitle.textContent = 'Мост YouTube не отвечает';
    veilText.textContent = 'Панель не может управлять колонкой: запустите quasard-youtube (127.0.0.1:8099).';
    setBadge('нет связи', 'bad');
    titleEl.textContent = 'Ничего не играет';
    subEl.textContent = short(err);
    timeBox.hidden = true;
    seekInput.disabled = true;
    ctlBox.hidden = true;
    openLink.hidden = true;
    nextBtn.disabled = !queue.length;
  }

  // ---------- очередь ----------
  function saveQueue() { store.setJson(K.queue, queue); }

  function newItem(query, videoId, title, artist) {
    return {
      uid: 'q' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      query,
      videoId: videoId || '',
      title: title || '',
      artist: artist || '',
      error: '',
      at: Date.now(),
    };
  }

  const findItem = uid => queue.findIndex(x => x.uid === uid);

  function removeItem(uid) {
    const i = findItem(uid);
    if (i < 0) return;
    queue.splice(i, 1);
    saveQueue(); renderQueue();
  }

  function markError(uid, msg) {
    const i = findItem(uid);
    if (i < 0) return;
    queue[i].error = msg;
    saveQueue(); renderQueue();
  }

  function renderQueue() {
    qCount.textContent = queue.length ? String(queue.length) : '';
    qClear.hidden = !queue.length;
    nextBtn.disabled = !queue.length;
    if (!queue.length) {
      qList.innerHTML = '<div class="yt-empty">Пусто. Добавьте ссылку или запрос — трек заиграет сам, ' +
        'когда закончится текущий.</div>';
      return;
    }
    qList.innerHTML = queue.map((it, i) => {
      const thumb = it.videoId
        ? '<img src="https://i.ytimg.com/vi/' + encodeURIComponent(it.videoId) + '/default.jpg" alt="" loading="lazy">'
        : '<span class="ph yt-ph"><svg width="16" height="16" aria-hidden="true"><use href="#i-yt"/></svg></span>';
      const sub = it.error ? 'не включилось: ' + it.error
        : it.artist || (it.videoId ? 'YouTube' : 'поиск на YouTube');
      return '<div class="trow' + (it.error ? ' err' : '') + '" title="' + esc(it.title || it.query) + '">' +
        thumb +
        '<div class="tt"><b>' + esc(it.title || it.query) + '</b><span>' + esc(sub) + '</span></div>' +
        '<button class="cbtn" type="button" data-act="play" data-i="' + i + '" title="Включить сейчас" aria-label="Включить сейчас">' +
        '<svg width="16" height="16" aria-hidden="true"><use href="#i-play"/></svg></button>' +
        '<button class="cbtn" type="button" data-act="del" data-i="' + i + '" title="Убрать из очереди" aria-label="Убрать из очереди">' +
        '<svg width="16" height="16" aria-hidden="true"><use href="#i-trash"/></svg></button>' +
        '</div>';
    }).join('');
  }

  function enqueue(rec, opts) {
    opts = opts || {};
    const query = String((rec && rec.query) || '').trim();
    const videoId = (rec && rec.videoId) || parseId(query);
    if (!query) return null;
    if (query.length > MAX_QUERY) { say('Слишком длинный запрос: до ' + MAX_QUERY + ' символов', true); return null; }
    if (queue.some(x => x.query === query || (videoId && x.videoId === videoId))) {
      if (!opts.quiet) say('Уже в очереди');
      return null;
    }
    const item = newItem(query, videoId, rec && rec.title, rec && rec.artist);
    queue.push(item);
    if (queue.length > MAX_QUEUE) queue = queue.slice(-MAX_QUEUE);
    saveQueue(); renderQueue();
    if (!opts.quiet) say('В очереди: ' + queue.length);
    if (idle()) playQuery(item.query, { fromQueue: item.uid, silent: true });
    return item;
  }

  function renderHistory() {
    hCount.textContent = hist.length ? String(hist.length) : '';
    hClear.hidden = !hist.length;
    if (!hist.length) {
      hList.innerHTML = '<div class="yt-empty">Здесь появится всё, что играло на колонке — в том числе включённое голосом.</div>';
      return;
    }
    hList.innerHTML = hist.map((it, i) => {
      const thumb = it.videoId
        ? '<img src="https://i.ytimg.com/vi/' + encodeURIComponent(it.videoId) + '/default.jpg" alt="" loading="lazy">'
        : '<span class="ph yt-ph"><svg width="16" height="16" aria-hidden="true"><use href="#i-yt"/></svg></span>';
      const sub = [it.artist, clock(it.at)].filter(Boolean).join(' · ');
      return '<div class="trow" title="' + esc(it.title) + '">' +
        thumb +
        '<div class="tt"><b>' + esc(it.title || it.query) + '</b><span>' + esc(sub) + '</span></div>' +
        '<button class="cbtn" type="button" data-act="again" data-i="' + i + '" title="В очередь" aria-label="Поставить в очередь">' +
        '<svg width="16" height="16" aria-hidden="true"><use href="#i-plus"/></svg></button>' +
        '</div>';
    }).join('');
  }

  function saveHistory() {
    hist = hist.slice(0, MAX_HIST);
    store.setJson(K.hist, hist);
  }

  function noteHistory(st) {
    if (!st || !st.title) return;
    const finished = st.state === 'finished';
    if (finished && !(Number(st.played_ms) > 1500)) return; // мгновенный стоп — не история
    const k = keyOf(st);
    if (k === histKey) return;
    histKey = k;
    const vid = st.video_id || '';
    const at = Date.now();
    const same = x => x && x.title === st.title && (x.videoId || '') === vid;
    if (same(hist[0])) {
      // тот же трек: повтор или перезагрузка панели — освежаем время, не дублируем
      hist[0].at = at;
    } else if (finished && hist.some(same)) {
      // завершённый поток, который уже есть в истории (панель открыли позже)
    } else {
      hist.unshift({
        query: st.query || '',
        videoId: vid,
        title: String(st.title),
        artist: st.artist ? String(st.artist) : '',
        at,
      });
    }
    saveHistory(); renderHistory();
  }

  // ---------- действия ----------
  function setBusy(on) {
    sendBtn.disabled = !!on;
    addBtn.disabled = !!on;
  }

  function needUrl() {
    say('Вставьте ссылку или запрос', true);
    try { urlInput.focus(); } catch { }
  }

  async function playQuery(raw, opts) {
    opts = opts || {};
    const query = toQuery(raw);
    if (!query) { needUrl(); return false; }
    if (query.length > MAX_QUERY) { say('Слишком длинный запрос: до ' + MAX_QUERY + ' символов', true); return false; }
    if (busy) { if (!opts.silent) say('Подождите: запрос уже отправляется'); return false; }

    busy = true;
    setBusy(true);
    setBadge(opts.silent ? 'следующий…' : 'на колонку…');
    try {
      await call('/cmd', { action: 'play', query });
      store.set(K.last, query);
      holdUntil = 0;   // новый трек — старая перемотка больше не важна
      if (opts.fromQueue) removeItem(opts.fromQueue);
      say(opts.silent ? 'Дальше: включил на колонке' : 'Играю на колонке');
      setBadge('');
      [400, 1200, 2500, 5000, 9000].forEach(ms => setTimeout(poll, ms));
      return true;
    } catch (e) {
      const msg = short(e);
      setBadge('ошибка', 'bad');
      say('Колонка не приняла: ' + msg, true);
      if (opts.fromQueue) markError(opts.fromQueue, msg);
      return false;
    } finally {
      busy = false;
      setBusy(false);
    }
  }

  function maybeAdvance(before, st) {
    if (busy || !queue.length) return;
    if (st.state !== 'finished' || st.error) return;
    // тот же самый «finished» уже обрабатывали — не запускаем по кругу
    if (before.state === 'finished' && before.key === keyOf(st)) return;
    const dur = Number(st.duration) || 0;
    const played = Number(st.played_ms) || 0;
    if (!(dur > 0 && played >= (dur - 5) * 1000)) return; // это не конец трека, а стоп/обрыв
    const item = queue[0];
    playQuery(item.query, { fromQueue: item.uid, silent: true });
  }

  // ---------- опрос моста ----------
  function applyStatus(st) {
    const before = { state: prevState, key: prevKey };
    stream = st;
    recvAt = Date.now();
    if (holdUntil && st) {
      // Перемотка доехала: новый поток уже играет с выбранного места.
      const at = (Number(st.played_ms) || 0) / 1000;
      if (keyOf(st) !== holdKey || Math.abs(at - holdPos) <= 8) holdUntil = 0;
    }
    if (st) {
      noteHistory(st);
      render(st);
      prevState = st.state || '';
      prevKey = keyOf(st);
      maybeAdvance(before, st);
    } else {
      holdUntil = 0;
      histKey = '';
      prevState = ''; prevKey = '';
      render(null);
    }
  }

  async function poll() {
    // Опрашиваем и в фоновой вкладке: очередь должна переключаться, даже когда
    // панель не на виду (браузер всё равно притормозит таймер до раза в минуту).
    if (polling) return;
    polling = true;
    let j = null, err = null;
    try { j = await call(''); } catch (e) { err = e; }
    polling = false;
    if (err) {
      failures += 1;
      if (failures >= 2) showOffline(err);   // одиночный таймаут — ещё не «мост упал»
      return;
    }
    failures = 0;
    const st = j && j.stream && typeof j.stream === 'object' && j.stream.title ? j.stream : null;
    applyStatus(st);
  }

  // Досчитываем позицию между опросами, чтобы полоса не дёргалась раз в 3 с.
  function tick() {
    if (document.hidden || !stream || stream.state !== 'streaming' || scrubbing) return;
    const dur = Number(stream.duration) || 0;
    if (!(dur > 0)) return;
    const pos = displayPos(stream);
    posEl.textContent = fmt(pos);
    durEl.textContent = fmt(dur);
    if (!seekInput.disabled) seekInput.value = String(Math.min(Math.floor(dur), Math.round(pos)));
  }

  // ---------- события ----------
  form.onsubmit = e => {
    e.preventDefault();
    const raw = urlInput.value.trim();
    if (!raw) { needUrl(); return; }
    playQuery(raw);
  };

  addBtn.onclick = () => {
    const raw = urlInput.value.trim();
    if (!raw) { needUrl(); return; }
    const query = toQuery(raw);
    if (!query) { needUrl(); return; }
    enqueue({ query, videoId: parseId(query) });
  };

  toggleBtn.onclick = async () => {
    if (!stream || busy) return;
    const wasPlaying = stream.state === 'streaming';
    toggleBtn.disabled = true;
    try {
      await call('/cmd', { action: wasPlaying ? 'pause' : 'resume' });
      say(wasPlaying ? 'Пауза' : 'Продолжаю');
      setTimeout(poll, 400);
      setTimeout(poll, 2000);
    } catch (e) {
      say('Не получилось: ' + short(e), true);
    } finally {
      toggleBtn.disabled = false;
    }
  };

  stopBtn.onclick = async () => {
    if (busy) return;
    stopBtn.disabled = true;
    try {
      await call('/cmd', { action: 'stop' });
      say('Остановил');
      setTimeout(poll, 400);
    } catch (e) {
      say('Не получилось: ' + short(e), true);
    } finally {
      stopBtn.disabled = false;
    }
  };

  nextBtn.onclick = () => {
    if (!queue.length) { say('Очередь пуста'); return; }
    const item = queue[0];
    playQuery(item.query, { fromQueue: item.uid, silent: true });
  };

  // ---------- перемотка ----------
  // Мост не умеет «прыгать» внутри уже отданного потока: он собирает тот же
  // трек заново с нужной секунды (ffmpeg -ss) и отдаёт Станции как новый поток
  // с offset_ms. Поэтому после запроса позиция какое-то время держится на
  // выбранном месте (hold), пока не придёт новый статус.
  async function seekTo(seconds) {
    if (!stream || busy) return;
    const dur = Number(stream.duration) || 0;
    if (!(dur > 0)) return;
    const target = Math.max(0, Math.min(Math.round(seconds), Math.max(0, Math.floor(dur) - 3)));
    if (!holdUntil && Math.abs(target - displayPos(stream)) < 2) return; // уже здесь
    busy = true;
    setBusy(true);
    setBadge('перемотка…');
    try {
      const r = await call('/cmd', { action: 'seek', position_ms: target * 1000 });
      if (r.seeked === false) throw new Error(r.reason || 'мост не смог перемотать');
      holdPos = (Number(r.position_ms) || target * 1000) / 1000;
      holdUntil = Date.now() + 60000;
      holdKey = keyOf(stream);
      say('Перемотал на ' + fmt(holdPos));
      setBadge('');
      [300, 800, 1800, 3500, 6000].forEach(ms => setTimeout(poll, ms));
    } catch (e) {
      holdUntil = 0;
      setBadge('ошибка', 'bad');
      say('Не удалось перемотать: ' + short(e), true);
      poll();
    } finally {
      busy = false;
      setBusy(false);
    }
  }

  function scheduleSeek(seconds) {
    if (seekTimer) clearTimeout(seekTimer);
    seekTimer = setTimeout(() => { seekTimer = 0; seekTo(seconds); }, 350);
  }

  seekInput.addEventListener('pointerdown', () => { scrubbing = true; });
  seekInput.addEventListener('input', () => {
    scrubbing = true;
    posEl.textContent = fmt(Number(seekInput.value) || 0);
  });
  seekInput.addEventListener('change', () => {
    const target = Number(seekInput.value) || 0;
    scrubbing = false;
    // сразу держим выбранную позицию, чтобы ближайший опрос не отыграл полосу назад
    if (stream && Number(stream.duration) > 0) {
      holdPos = target;
      holdUntil = Date.now() + 60000;
      holdKey = keyOf(stream);
    }
    posEl.textContent = fmt(target);
    scheduleSeek(target);
  });

  qClear.onclick = () => {
    if (!queue.length) return;
    if (!confirm('Очистить очередь (' + queue.length + ')?')) return;
    queue = [];
    saveQueue(); renderQueue();
    say('Очередь очищена');
  };

  hClear.onclick = () => {
    if (!hist.length) return;
    if (!confirm('Очистить историю (' + hist.length + ')?')) return;
    hist = [];
    saveHistory(); renderHistory();
    say('История очищена');
  };

  qList.addEventListener('click', e => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    const i = Number(b.dataset.i);
    const it = queue[i];
    if (!it) return;
    if (b.dataset.act === 'play') playQuery(it.query, { fromQueue: it.uid });
    else if (b.dataset.act === 'del') { queue.splice(i, 1); saveQueue(); renderQueue(); }
  });

  hList.addEventListener('click', e => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    const it = hist[Number(b.dataset.i)];
    if (it) enqueue({ query: it.query, videoId: it.videoId, title: it.title, artist: it.artist });
  });

  // Битые обложки (ролик удалён / i.ytimg.com недоступен) — показываем заглушку.
  function thumbFailed(img) {
    if (!img || img.tagName !== 'IMG') return;
    img.style.visibility = 'hidden';
    if (img === prevImg) {
      const vid = stream && stream.video_id ? String(stream.video_id) : '';
      if (vid) badThumb.add(vid);
      render(stream || null);
    }
  }
  qList.addEventListener('error', e => thumbFailed(e.target), true);
  hList.addEventListener('error', e => thumbFailed(e.target), true);
  prevImg.addEventListener('error', () => thumbFailed(prevImg));

  // ---------- старт ----------
  const last = store.get(K.last) || '';
  if (last) urlInput.value = last;
  renderQueue();
  renderHistory();
  render(null);
  poll();
  setInterval(poll, POLL_MS);
  setInterval(tick, 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

  // Вторая вкладка панели: очередь и история у неё те же (localStorage),
  // поэтому подхватываем изменения, а не живём своей копией.
  if (window.addEventListener) {
    window.addEventListener('storage', e => {
      if (e.key === K.queue) { queue = readList(K.queue); renderQueue(); }
      else if (e.key === K.hist) { hist = readList(K.hist); renderHistory(); }
    });
  }
})();
