const $ = id => document.getElementById(id);
let movies = [], state = {status:'stopped', movie_id:null, position_seconds:0}, seeking = false;
async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'The device could not complete the request.');
  return data;
}
function fmt(s) {
  s = Math.max(0, Math.floor(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0');
}
function runtime(m) {
  if (!m.duration_seconds) return '';
  const h = Math.floor(m.duration_seconds / 3600), mm = Math.floor(m.duration_seconds % 3600 / 60);
  return h ? h + 'h ' + mm + 'm' : mm + 'm';
}
const meta = m => [m.year, runtime(m)].filter(Boolean).join(' · ');
const thumb = m => '/api/movies/' + m.id + '/thumbnail';
function poster(m) {
  const img = document.createElement('img'); img.src = thumb(m); img.alt = ''; img.loading = 'lazy';
  img.addEventListener('error', () => img.remove()); return img;
}
async function command(opcode, argument) {
  try {
    await api('/api/command', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({opcode, argument})});
    $('toast').textContent = ''; await status();
  } catch (error) {$('toast').textContent = error.message;}
}
function render() {
  const q = $('search').value.trim().toLowerCase(), grid = $('grid');
  const shown = movies.filter(m => !q || m.title.toLowerCase().includes(q));
  grid.replaceChildren();
  if (!shown.length) {
    const d = document.createElement('div'); d.className = 'empty';
    d.textContent = movies.length ? 'No movies match your search.' : 'No movies yet. Add movies to the device to get started.';
    grid.append(d); return;
  }
  for (const m of shown) {
    const card = document.createElement('button'); card.className = 'card' + (m.id === state.movie_id && state.status !== 'stopped' ? ' now' : '');
    const p = document.createElement('div'); p.className = 'poster'; p.textContent = m.title; p.append(poster(m));
    const cap = document.createElement('div'); cap.className = 'cap'; cap.textContent = m.title;
    if (meta(m)) {const s = document.createElement('small'); s.textContent = meta(m); cap.append(s);}
    card.append(p, cap);
    // Watched partway before: a progress bar along the bottom of the poster
    // (live for the movie playing now, saved position for the rest).
    const pos = m.id === state.movie_id && state.status !== 'stopped' ? state.position_seconds : m.position_seconds;
    if (pos > 0 && m.duration_seconds) {
      const bar = document.createElement('div'); bar.className = 'progress';
      const fill = document.createElement('i'); fill.style.width = Math.min(100, pos / m.duration_seconds * 100) + '%';
      bar.append(fill); card.append(bar);
    }
    card.addEventListener('click', () => openSheet(m)); grid.append(card);
  }
}
function openSheet(m) {
  const panel = $('panel'); panel.replaceChildren();
  const img = poster(m); panel.append(img);
  const body = document.createElement('div'); body.className = 'body';
  const h = document.createElement('h3'); h.textContent = m.title;
  const mt = document.createElement('div'); mt.className = 'meta'; mt.textContent = meta(m);
  body.append(h, mt);
  if (m.description) {const p = document.createElement('p'); p.textContent = m.description; body.append(p);}
  const row = document.createElement('div'); row.className = 'row';
  const play = document.createElement('button'); play.className = 'btn'; play.textContent = '▶ Play on device';
  play.addEventListener('click', () => {closeSheet(); command('select_movie', m.id);});
  const close = document.createElement('button'); close.className = 'btn grey'; close.textContent = 'Close';
  close.addEventListener('click', closeSheet);
  row.append(play, close); body.append(row); panel.append(body); $('sheet').classList.remove('hidden', 'top');
}
function closeSheet() {$('sheet').classList.add('hidden'); $('sheet').classList.remove('top'); clearInterval(settingsTimer);}
$('sheet').addEventListener('click', e => {if (e.target === $('sheet')) closeSheet();});
$('search').addEventListener('input', render);
$('toggle').addEventListener('click', () => command(state.status === 'playing' ? 'pause' : 'play'));
$('stop').addEventListener('click', () => command('stop'));
$('back').addEventListener('click', () => command('seek', Math.max(0, Math.floor(state.position_seconds) - 15)));
$('fwd').addEventListener('click', () => command('seek', Math.floor(state.position_seconds) + 15));
$('seek').addEventListener('input', () => {seeking = true;});
$('seek').addEventListener('change', () => {seeking = false; command('seek', Number($('seek').value));});
// While a movie plays we assume it keeps playing: tick the elapsed time up
// every second locally, and stop that ticker as soon as a poll returns (the
// device's own position replaces it, and the ticker restarts from there).
let ticker = null;
function stopTicker() {clearInterval(ticker); ticker = null;}
function showTime(m) {
  $('barTitle').textContent = m.title;
  const bt = document.createElement('small');
  bt.textContent = (state.status === 'paused' ? 'Paused · ' : '') + fmt(state.position_seconds) + (m.duration_seconds ? ' / ' + fmt(m.duration_seconds) : '');
  $('barTitle').append(bt);
  if (!seeking) $('seek').value = state.position_seconds;
}
function startTicker(m) {
  stopTicker();
  if (state.status !== 'playing' || document.hidden) return;
  ticker = setInterval(() => {
    if (document.hidden) {stopTicker(); return;}
    state.position_seconds += 1;
    if (m.duration_seconds) state.position_seconds = Math.min(state.position_seconds, m.duration_seconds);
    showTime(m);
  }, 1000);
}
async function status() {
  try {
    const previous = state.syncing_movie_title, previousSync = state.syncing_movie_title, previousMovie = state.status === 'stopped' ? null : state.movie_id;
    state = await api('/api/status'); $('conn').textContent = state.thermal_note || 'Connected';
    stopTicker();
    // A download started or finished: reload so the list is current. Same
    // when the movie playing changes or stops, so its saved position (the progress bar under it) is current.
    const playingMovie = state.status === 'stopped' ? null : state.movie_id;
    if (previous !== undefined && (previousSync !== state.syncing_movie_title
        || previousMovie !== playingMovie)) await load();
    const m = movies.find(x => x.id === state.movie_id), active = m && state.status !== 'stopped';
    // Top-right spinner while the device downloads; tapping it opens the
    // activity panel (downloads, their queue and the home server's progress).
    const busyLabel = 'Downloading ' + (state.syncing_movie_title || '');
    $('busy').classList.toggle('hidden', !state.syncing_movie_title);
    $('busy').title = busyLabel; $('busy').setAttribute('aria-label', busyLabel);
    $('bar').classList.toggle('hidden', !active); $('hero').classList.toggle('hidden', !active);
    if (active) {
      $('heroTitle').textContent = m.title;
      $('toggle').textContent = state.status === 'playing' ? '❚❚' : '▶';
      $('heroImg').src = thumb(m);
      $('seek').max = m.duration_seconds || 0; $('seek').classList.toggle('hidden', !m.duration_seconds);
      showTime(m); startTicker(m);
    }
    render();
  } catch (error) {$('conn').textContent = 'Waiting for device…';}
}
async function load() {
  try {movies = await api('/api/movies');} catch (error) {$('grid').textContent = 'Could not load movies. Reload this page to try again.'; return;}
  render();
}
// Poll gently: not at all while the tab is hidden, and less often while a
// movie plays, so the page never competes with playback on the device.
function pollStatus() {
  // The device says how often to ask: slower while it plays.
  const delay = Math.min(30, Math.max(2, state.poll_seconds || 3)) * 1000;
  setTimeout(async () => {
    if (!document.hidden) await status();
    pollStatus();
  }, delay);
}
document.addEventListener('visibilitychange', () => {if (!document.hidden) status();});
load().then(status); pollStatus();

// ---- Settings (gear): Controls, Logs and Info tabs ----
let settingsTimer = null, settingsTab = 'controls';
const dur = s => {
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return (d ? d + 'd ' : '') + (h || d ? h + 'h ' : '') + m + 'm';
};
function section(title, rows) {
  const box = document.createElement('div'); box.className = 'info';
  const h = document.createElement('h4'); h.textContent = title;
  const dl = document.createElement('dl');
  for (const [name, value, tone] of rows) {
    if (value === null || value === undefined || value === '') continue;
    const dt = document.createElement('dt'); dt.textContent = name;
    const dd = document.createElement('dd'); dd.textContent = value; if (tone) dd.className = tone;
    dl.append(dt, dd);
  }
  box.append(h, dl); return box;
}
function tabRow(items, current, pick, className) {
  const tabs = document.createElement('div'); tabs.className = className;
  for (const [key, label] of items) {
    const tab = document.createElement('button'); tab.className = 'btn small' + (key === current ? '' : ' grey'); tab.textContent = label;
    tab.addEventListener('click', () => pick(key)); tabs.append(tab);
  }
  return tabs;
}
function openSettings(tab) {
  if (typeof tab === 'string') settingsTab = tab;
  clearInterval(settingsTimer);
  const panel = $('panel'); panel.replaceChildren();
  const body = document.createElement('div'); body.className = 'body';
  const h = document.createElement('h3'); h.textContent = 'Device settings';
  const tabs = tabRow([['controls', 'Controls'], ['logs', 'Logs'], ['info', 'Info']], settingsTab, openSettings, 'tabs settings-tabs');
  const content = document.createElement('div');
  const close = document.createElement('button'); close.className = 'btn grey'; close.textContent = 'Close';
  close.addEventListener('click', closeSheet);
  const row = document.createElement('div'); row.className = 'row';
  if (settingsTab === 'logs') buildLogs(content, row);
  else if (settingsTab === 'info') buildInfo(content);
  else buildControls(content, row);
  row.append(close); body.append(h, tabs, content, row); panel.append(body);
  // Settings drops down from the top; movie details still rise from the bottom.
  $('sheet').classList.remove('hidden'); $('sheet').classList.add('top');
}

// Controls: Wi-Fi networks, update, reboot and shut down.
function buildControls(content, row) {
  const note = document.createElement('p'); note.className = 'meta';
  const wifi = document.createElement('div'); wifi.className = 'info';
  const wh = document.createElement('h4'); wh.textContent = 'Saved Wi-Fi networks';
  const saved = document.createElement('p'); saved.className = 'meta'; saved.textContent = 'Loading…';
  const ssid = document.createElement('input'); ssid.placeholder = 'Network name (e.g. your iPhone)'; ssid.autocapitalize = 'off'; ssid.autocomplete = 'off';
  const pass = document.createElement('input'); pass.type = 'password'; pass.placeholder = 'Password (blank for an open network)'; pass.autocomplete = 'off';
  const add = document.createElement('button'); add.className = 'btn grey small'; add.textContent = 'Save network';
  const loadSaved = async () => {
    try {const r = await api('/api/wifi/networks'); saved.textContent = r.networks.length ? r.networks.join(', ') : 'None saved';}
    catch (error) {saved.textContent = error.message;}
  };
  add.addEventListener('click', async () => {
    if (!ssid.value) {note.textContent = 'Enter the network name.'; return;}
    try {
      await api('/api/wifi/networks', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ssid: ssid.value, password: pass.value})});
      note.textContent = 'Saved. Turn that network on, then tap Find Wi-Fi networks.';
      ssid.value = ''; pass.value = ''; loadSaved();
    } catch (error) {note.textContent = error.message;}
  });
  wifi.append(wh, saved, ssid, pass, add); loadSaved();
  const search = document.createElement('button'); search.className = 'btn grey'; search.textContent = 'Find Wi-Fi networks';
  search.addEventListener('click', async () => {
    if (!confirm('Search for saved Wi-Fi networks for 30 seconds? The MagicBoxie Player hotspot turns off meanwhile, so this page disconnects. If no network is found the hotspot comes back - reconnect to it then.')) return;
    try {
      await api('/api/wifi/search', {method:'POST'});
      note.textContent = 'Searching for networks for 30 seconds… the hotspot is off. It returns if none is found.';
    } catch (error) {note.textContent = error.message;}
  });
  const update = document.createElement('button'); update.className = 'btn grey'; update.textContent = 'Update now';
  update.addEventListener('click', async () => {
    if (!confirm('Check for an update and install it now? If there is one, the player restarts and any movie resumes afterwards. Needs an internet connection.')) return;
    try {
      await api('/api/update', {method:'POST'});
      note.textContent = 'Checking for an update… if there is one, the player restarts to install it. Progress is under Info and the Updates log.';
    } catch (error) {note.textContent = error.message;}
  });
  const reboot = document.createElement('button'); reboot.className = 'btn danger'; reboot.textContent = 'Reboot device';
  reboot.addEventListener('click', async () => {
    if (!confirm('Reboot the device? Playback stops and the device is unavailable for about a minute.')) return;
    try {
      await api('/api/reboot', {method:'POST'});
      note.textContent = 'Rebooting… this page reconnects when the device is back.';
    } catch (error) {note.textContent = error.message;}
  });
  const shutdown = document.createElement('button'); shutdown.className = 'btn danger'; shutdown.textContent = 'Shut down';
  shutdown.addEventListener('click', async () => {
    if (!confirm('Shut down the device? It stays off until it is unplugged and plugged back in.')) return;
    try {
      await api('/api/shutdown', {method:'POST'});
      note.textContent = 'Shutting down… wait for the activity light to stop before unplugging.';
    } catch (error) {note.textContent = error.message;}
  });
  content.append(wifi, note); row.append(search, update, reboot, shutdown);
}

// Info: device details, refreshed every 5 seconds while the tab is open.
function renderInfo(body, info) {
  body.replaceChildren();
  const ips = (info.addresses || []).map(a => a.address + ' (' + a.interface + ')').join(', ') || info.ip_address;
  const conns = (info.connections || []).map(c => c.name + ' · ' + c.type).join(', ');
  const mem = info.memory_mb ? (info.memory_mb.total - info.memory_mb.available) + ' / ' + info.memory_mb.total + ' MB used' : null;
  const disk = d => d ? d.free + ' GB free of ' + d.total + ' GB' : null;
  const temp = info.cpu_temperature_celsius;
  const power = info.under_voltage ? ['Power', 'Under-voltage now', 'bad'] : info.throttled ? ['Power', 'CPU throttled', 'warn']
    : info.under_voltage === false ? ['Power', 'OK', 'good'] : ['Power', null];
  body.append(
    section('Network', [
      ['Hostname', info.hostname], ['Address on network', info.mdns_name],
      ['IP address', ips], ['Connection', conns],
      ['Internet', info.internet_reachable ? 'Reachable' : 'Not reachable', info.internet_reachable ? 'good' : 'warn'],
    ]),
    section('Device', [
      ['Model', info.model], ['Uptime', info.uptime_seconds != null ? dur(info.uptime_seconds) : null],
      ['CPU temperature', temp != null ? temp.toFixed(1) + ' °C' : null, temp > 75 ? 'bad' : temp > 65 ? 'warn' : ''],
      power, ['Load average', info.load_average ? info.load_average.join(' · ') : null],
      ['Memory', mem], ['Movie storage', disk(info.disk_movies_gb)], ['System storage', disk(info.disk_system_gb)],
    ]),
    section('Playback', [
      ['Status', info.playback_status], ['Movies', info.movie_count],
      ['Activity', info.activity || info.update_status],
      ['Keyboard', info.keyboards && info.keyboards.length ? info.keyboards.join(', ') : 'None detected'],
    ]),
    section('Software', [
      ['Version', info.software ? (info.software.version ? info.software.version + ' · ' : '') + info.software.commit : 'Unknown'],
      ['Latest change date', info.software ? info.software.date : 'Unknown'],
      ['Latest change', info.software && info.software.subject], ['API version', info.api_version],
    ]),
  );
}
function buildInfo(content) {
  content.textContent = 'Loading…';
  const refresh = async () => {
    try {renderInfo(content, await api('/api/info'));}
    catch (error) {content.textContent = 'Could not load device details: ' + error.message;}
  };
  refresh(); settingsTimer = setInterval(() => {if (!document.hidden) refresh();}, 5000);
}

// Logs: this boot's Player, Wi-Fi and Update logs (times are seconds since boot).
let logSource = 'wifi';
function buildLogs(content, row) {
  const sources = tabRow([['player', 'Player'], ['wifi', 'Wi-Fi'], ['update', 'Updates']], logSource,
    source => {logSource = source; openSettings('logs');}, 'tabs log-sources');
  const info = document.createElement('div');
  const out = document.createElement('pre'); out.className = 'logs'; out.textContent = 'Loading…';
  const refreshLogs = async () => {
    try {
      const data = await api('/api/logs?source=' + logSource);
      info.replaceChildren();
      if (data.source === 'wifi') {
        const saved = data.saved_networks ? data.saved_networks.join(', ') || 'None' : 'Could not read';
        const profiles = (data.wifi_profiles || []).map(p => p.name + (p.autoconnect ? '' : ' (no autoconnect)')).join(', ') || 'None';
        const visible = (data.in_range || []).map(n => n.ssid + ' (' + n.signal + '%)').join(', ') || 'None seen (no scan while the hotspot is on)';
        info.append(section('Wi-Fi', [['Saved networks', saved], ['NetworkManager profiles', profiles],
          ['Wi-Fi adapter', data.adapter], ['In range', visible]]));
      }
      out.textContent = data.journal; out.scrollTop = out.scrollHeight;
    } catch (error) {out.textContent = 'Could not load logs: ' + error.message;}
  };
  const refresh = document.createElement('button'); refresh.className = 'btn'; refresh.textContent = 'Refresh';
  refresh.addEventListener('click', refreshLogs);
  const copy = document.createElement('button'); copy.className = 'btn grey'; copy.textContent = 'Copy';
  copy.addEventListener('click', async () => {
    try {await navigator.clipboard.writeText(out.textContent); copy.textContent = 'Copied';}
    catch {const range = document.createRange(); range.selectNodeContents(out); getSelection().removeAllRanges(); getSelection().addRange(range); copy.textContent = 'Selected';}
  });
  content.append(sources, info, out); row.append(refresh, copy); refreshLogs();
}
$('gear').addEventListener('click', openSettings);
$('busy').addEventListener('click', openActivity);

// ---- Activity (loading icon): downloads, transcodes and what is queued ----
let activityTimer = null;
const mb = b => b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : Math.round(b / 1e6) + ' MB';
const REMOTE_STATUS = {pending: 'Waiting', probing: 'Checking the file', needs_transcode: 'Waiting to transcode', transcoding: 'Transcoding'};
function activityItem(title, detail, percent) {
  const row = document.createElement('div'); row.className = 'upl';
  const label = document.createElement('div'); label.textContent = title + (detail ? ' — ' + detail : '');
  row.append(label);
  if (percent != null) {
    const bar = document.createElement('div'); bar.className = 'bar'; const fill = document.createElement('i');
    fill.style.width = Math.max(0, Math.min(100, percent)) + '%'; bar.append(fill); row.append(bar);
  }
  return row;
}
function activityGroup(body, title, rows, empty) {
  if (!rows.length && !empty) return;
  const box = document.createElement('div'); box.className = 'info';
  const h = document.createElement('h4'); h.textContent = title; box.append(h);
  if (rows.length) box.append(...rows);
  else {const p = document.createElement('p'); p.className = 'meta'; p.textContent = empty; box.append(p);}
  body.append(box);
}
function renderActivity(a) {
  const body = $('activityBody'); body.replaceChildren();
  if (a.paused_for_playback) {
    const p = document.createElement('p'); p.className = 'meta';
    p.textContent = 'A movie is playing: downloads carry on, slowed so playback stays smooth.'; body.append(p);
  }
  const d = a.downloading;
  const dlPct = d && d.bytes_total ? d.bytes_done / d.bytes_total * 100 : null;
  const dlDetail = d ? (d.bytes_total ? mb(d.bytes_done) + ' of ' + mb(d.bytes_total) + ' (' + Math.floor(dlPct) + '%)'
    : d.bytes_done ? mb(d.bytes_done) : 'starting') : '';
  activityGroup(body, 'Downloading from home server', d ? [activityItem(d.title, dlDetail, dlPct ?? 0)] : [], 'Nothing downloading');
  activityGroup(body, 'Download queue', a.download_queue.map(t => activityItem(t, 'waiting')));
  const home = a.home_server;
  if (home) {
    const rows = home.preparing.map(m => activityItem(m.title,
      (REMOTE_STATUS[m.status] || m.status) + (m.progress_percent != null && m.status === 'transcoding' ? ' ' + Math.floor(m.progress_percent) + '%' : ''),
      m.status === 'transcoding' ? m.progress_percent ?? 0 : null));
    activityGroup(body, 'Transcoding on home server', rows,
      home.reachable ? 'Nothing being prepared' : 'Home server not reachable. It is checked when the player is on home Wi-Fi.');
  }
}
async function refreshActivity() {
  if (!$('activityBody')) {clearInterval(activityTimer); return;}  // sheet now shows something else
  try {renderActivity(await api('/api/activity'));}
  catch (error) {$('activityBody').textContent = 'Could not load activity: ' + error.message;}
}
function openActivity() {
  clearInterval(settingsTimer);
  const panel = $('panel'); panel.replaceChildren();
  const body = document.createElement('div'); body.className = 'body';
  const h = document.createElement('h3'); h.textContent = 'Downloads';
  const details = document.createElement('div'); details.id = 'activityBody'; details.textContent = 'Loading…';
  const row = document.createElement('div'); row.className = 'row';
  const close = document.createElement('button'); close.className = 'btn grey'; close.textContent = 'Close';
  close.addEventListener('click', closeSheet);
  row.append(close); body.append(h, details, row); panel.append(body);
  $('sheet').classList.remove('hidden');
  refreshActivity(); clearInterval(activityTimer); activityTimer = setInterval(() => {if (!document.hidden) refreshActivity();}, 3000);
}

// ---- Upload movies from this browser ----
function uploadOne(file) {
  const row = document.createElement('div'); row.className = 'upl';
  const label = document.createElement('div'); label.textContent = file.name + ' — waiting…';
  const bar = document.createElement('div'); bar.className = 'bar'; const fill = document.createElement('i'); bar.append(fill);
  row.append(label, bar); $('uploads').append(row);
  return new Promise(resolve => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/movies');
    xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
    xhr.setRequestHeader('X-Filename-Encoding', 'uri');
    const fail = message => {row.classList.add('err'); label.textContent = file.name + ' — ' + message; bar.remove(); resolve(false);};
    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      const pct = Math.floor(e.loaded / e.total * 100);
      label.textContent = file.name + ' — uploading ' + pct + '%'; fill.style.width = pct + '%';
    };
    xhr.onload = () => {
      if (xhr.status === 201) {label.textContent = file.name + ' — uploaded'; fill.style.width = '100%'; setTimeout(() => row.remove(), 4000); resolve(true); return;}
      let message = 'upload failed (' + xhr.status + ')';
      try {message = JSON.parse(xhr.responseText).error || message;} catch (e) {}
      fail(message);
    };
    xhr.onerror = () => fail('connection lost');
    xhr.send(file);
  });
}
async function uploadFiles(files) {
  for (const file of files) await uploadOne(file);
  await load(); await status();
}
$('upload').addEventListener('click', () => $('file').click());
$('file').addEventListener('change', () => {uploadFiles(Array.from($('file').files)); $('file').value = '';});
