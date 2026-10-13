/* Vyhledávač spojů – webové rozhraní.
 *
 * Server posílá přes Server-Sent Events celý stav hledání po každé změně
 * (dopravce začal, poslal mezistav, skončil, selhal). Všechno, co jde
 * udělat bez nového hledání – řazení, druhy dopravy, přestupy, dopravci,
 * "jen se známou cenou", "skrýt dražší i pomalejší", měna – se počítá
 * tady v prohlížeči, takže přepnutí je okamžité.
 */
(() => {
  'use strict';

  // ---- pomůcky -------------------------------------------------------------
  const $ = sel => document.querySelector(sel);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const icon = (name, size = 16, cls = '') => {
    const body = (window.ICONS || {})[name];
    if (!body) return `<span style="display:block;width:${size}px;height:${size}px"></span>`;
    return `<svg class="i ${cls}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
  };
  const pl = (n, one, few, many) => n === 1 ? one : (n >= 2 && n <= 4 ? few : many);
  const minutes = iso => new Date(iso).getTime() / 60000;
  const hhmm = iso => iso ? iso.slice(11, 16) : '';
  const dayOf = iso => iso ? iso.slice(0, 10) : '';
  const dur = m => { m = Math.round(m); const h = Math.floor(m / 60), r = m % 60; return h ? `${h} h ${r} min` : `${r} min`; };
  const DAYS = ['neděle', 'pondělí', 'úterý', 'středa', 'čtvrtek', 'pátek', 'sobota'];
  const store = {
    get(k, d) { try { const v = localStorage.getItem('vs.' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem('vs.' + k, JSON.stringify(v)); } catch (e) { /* bez úložiště to jde taky */ } },
  };

  // Druhy dopravy: vlak / autobus / MHD. Pěšky se nepočítá jako prostředek.
  const MODE = {
    train: { icon: 'train-front', bg: 'var(--color-text)', fg: 'var(--color-bg)', group: 'train', name: 'Vlak' },
    bus: { icon: 'bus', bg: 'var(--mode-bus)', fg: 'var(--mode-on)', group: 'bus', name: 'Autobus' },
    metro: { icon: 'train-front-tunnel', bg: 'var(--mode-city)', fg: 'var(--mode-on)', group: 'city', name: 'Metro' },
    tram: { icon: 'tram-front', bg: 'var(--mode-city)', fg: 'var(--mode-on)', group: 'city', name: 'Tramvaj' },
    ferry: { icon: 'ship', bg: 'var(--mode-city)', fg: 'var(--mode-on)', group: 'city', name: 'Loď' },
    unknown: { icon: 'train-front', bg: 'var(--color-text)', fg: 'var(--color-bg)', group: 'train', name: 'Spoj' },
  };
  const modeOf = leg => MODE[leg.mode] || MODE.unknown;
  const ALL_GROUPS = ['train', 'bus', 'city'];
  const RISKY_TRANSFER = 3;   // min – kratší přestup označíme jako riskantní

  // Druh místa v našeptávači.
  const PLACE_KIND = {
    c: { icon: 'map-pin', name: 'obec' },
    t: { icon: 'train-front', name: 'vlak' },
    b: { icon: 'bus', name: 'autobus' },
  };
  const zonesText = z => !z || !z.length ? '' : (z.length === 1 ? `zóna ${z[0]}` : `zóny ${z.join(', ')}`);
  // "Jihomoravský kraj · IDS JMK · zóna 100" – nebo stát u zahraničí.
  // Přesahy: místo patří i do jiného systému ("také IDESKA zóna 932").
  const alsoText = p => Object.entries((p && p.systems) || {}).map(([s, z]) => `také ${s}${z.length ? ' ' + z.join(', ') : ''}`).join(' · ');
  const placeMeta = p => p ? (p.country ? [p.country, p.local] : [p.kraj, p.ids, zonesText(p.zones), alsoText(p)]).filter(Boolean).join(' · ') : '';

  // ---- stav ----------------------------------------------------------------
  const MAX_PAX = 9;
  function loadPax() {
    const saved = store.get('pax', null);
    if (Array.isArray(saved) && saved.length) {
      return saved.slice(0, MAX_PAX).map(p => ({
        age: Math.max(0, Math.min(120, parseInt(p.age, 10) || 0)), isic: !!p.isic, skFree: !!p.skFree,
      }));
    }
    return [{ age: store.get('age', 26), isic: store.get('isic', false), skFree: store.get('skFree', false) }];
  }
  // Předplatné IDS: [{pax: index cestujícího | 'all', system: 'PID', zones: 'P, 0, B'}]
  function loadPasses() {
    const saved = store.get('passes', []);
    return Array.isArray(saved) ? saved.filter(e => e && typeof e.system === 'string').slice(0, 20).map(e => ({
      pax: e.pax === 'all' ? 'all' : Math.max(0, parseInt(e.pax, 10) || 0),
      system: e.system, zones: String(e.zones || ''),
    })) : [];
  }
  const today = new Date();
  const pad = n => String(n).padStart(2, '0');
  const S = {
    meta: null,
    providersOn: null,            // Set jmen – nastaví se po načtení meta
    hacksOn: new Set(store.get('hacks', ['split_ticket', 'ids_ticket', 'idos_complete', 'obb_partial', 'hub_transfer', 'cd_europe_border', 'cd_europe_beyond', 'regional_pass'])),
    // Cestující: [{age, isic, skFree}] – starší verze ukládala jednoho do kořene.
    pax: loadPax(),
    passes: loadPasses(),
    pass: false,                  // rozbalený panel Cestující
    modes: [...ALL_GROUPS],
    adv: false,
    maxT: 'any',
    direct: false,
    buffer: 0,
    onlyPriced: store.get('onlyPriced', false),
    pareto: store.get('pareto', true),
    sameDep: store.get('sameDep', true),   // skrýt horší spoje se stejným odjezdem
    sort: store.get('sort', 'earliest'),
    off: new Set(),               // vypnutí dopravci (podle jména na úseku)
    open: null,
    stopsOpen: new Set(),
    cur: store.get('cur', 'CZK'),
    data: null,                   // poslední snímek ze serveru
    searching: false,
    sid: null,
    es: null,
    clientError: '',
  };

  // Hack s jízdenkami IDS přibyl později – zapnout ho jednou i těm, kdo už
  // mají výběr hacků uložený.
  if (!store.get('idsHackOffered', false)) {
    S.hacksOn.add('ids_ticket');
    store.set('idsHackOffered', true);
    store.set('hacks', [...S.hacksOn]);
  }
  // Přestupy v Praze/Brně, Včasná Evropa a regionální jízdenky – také jednou zapnout.
  if (!store.get('euroHacksOffered3', false)) {
    ['idos_complete', 'obb_partial', 'hub_transfer', 'cd_europe_border', 'cd_europe_beyond', 'regional_pass'].forEach(h => S.hacksOn.add(h));
    store.set('euroHacksOffered3', true);
    store.set('hacks', [...S.hacksOn]);
  }

  // ---- formulář ------------------------------------------------------------
  const form = $('#search-form');
  const fFrom = $('#f-from'), fTo = $('#f-to'), fDate = $('#f-date'), fTime = $('#f-time');

  function initForm() {
    const last = store.get('last', null);
    fFrom.value = last ? last.from : 'Praha';
    fTo.value = last ? last.to : 'Brno';
    fDate.value = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`;
    fTime.value = `${pad(today.getHours())}:${pad(today.getMinutes())}`;
    document.querySelectorAll('.sf-label[data-icon]').forEach(el => el.insertAdjacentHTML('afterbegin', icon(el.dataset.icon, 13)));
    $('#swap-btn').innerHTML = icon('arrow-left-right', 18);
    $('#submit-btn').insertAdjacentHTML('beforeend', icon('arrow-right', 16));
    suggester(fFrom);
    suggester(fTo);
  }

  function remember(from, to) {
    const history = store.get('history', []).filter(n => n !== from && n !== to);
    store.set('history', [from, to, ...history].slice(0, 20));
    store.set('last', { from, to });
  }

  // ---- našeptávač zastávek ---------------------------------------------------
  // Jeden seznam pod aktivním polem. Leží mimo formulář (ten má overflow:
  // hidden), pozice se počítá z buňky pole.
  const sugBox = document.createElement('div');
  sugBox.className = 'suggest';
  sugBox.id = 'suggest-list';
  sugBox.setAttribute('role', 'listbox');
  sugBox.hidden = true;
  document.body.appendChild(sugBox);
  const sug = { input: null, items: [], active: -1, seq: 0, timer: null };

  function highlight(name, q) {
    // Zvýraznit začátky slov, které odpovídají dotazu (bez ohledu na diakritiku).
    const fold = t => t.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
    const words = fold(q).split(/[^a-z0-9]+/).filter(Boolean);
    if (!words.length) return esc(name);
    const folded = fold(name);
    const marks = new Array(name.length).fill(false);
    words.forEach(w => {
      const re = new RegExp(`(^|[^a-z0-9])(${w})`, 'g');   // slova jsou jen [a-z0-9]
      let m;
      while ((m = re.exec(folded))) { const start = m.index + m[1].length; for (let i = start; i < start + w.length; i++) marks[i] = true; }
    });
    let out = '', on = false;
    for (let i = 0; i < name.length; i++) {
      if (marks[i] !== on) { out += marks[i] ? '<mark>' : '</mark>'; on = marks[i]; }
      out += esc(name[i]);
    }
    return out + (on ? '</mark>' : '');
  }

  function placeSub(p) {
    const parts = [p.country && p.kind === 'c' ? 'město' : PLACE_KIND[p.kind] ? PLACE_KIND[p.kind].name : ''];
    if (p.obec && p.obec !== p.name) parts.push(p.obec);
    if (p.okres && p.kind === 'c' && p.okres !== p.name && p.kraj !== 'Hlavní město Praha') parts.push(`okres ${p.okres}`);
    parts.push(placeMeta(p));
    return parts.filter(Boolean).join(' · ');
  }

  function renderSuggest() {
    const { input, items, active } = sug;
    if (!input || !items.length || document.activeElement !== input) { closeSuggest(); return; }
    const q = input.value;
    sugBox.innerHTML = items.map((p, i) => {
      if (p.history) {
        return `<div class="sg-item" role="option" id="sg-${i}" data-i="${i}" aria-selected="${i === active}">
          <span class="sg-icon">${icon('rotate-ccw', 16)}</span><span class="sg-text"><span class="sg-name">${esc(p.name)}</span><span class="sg-sub">nedávno hledané</span></span></div>`;
      }
      const k = PLACE_KIND[p.kind] || PLACE_KIND.c;
      return `<div class="sg-item" role="option" id="sg-${i}" data-i="${i}" aria-selected="${i === active}">
        <span class="sg-icon">${icon(k.icon, 16)}</span>
        <span class="sg-text"><span class="sg-name">${highlight(p.name, q)}</span><span class="sg-sub">${esc(placeSub(p))}</span></span></div>`;
    }).join('');
    const cell = input.closest('.sf-cell') || input;
    const r = cell.getBoundingClientRect();
    sugBox.style.left = `${r.left + window.scrollX}px`;
    sugBox.style.top = `${r.bottom + window.scrollY}px`;
    sugBox.style.width = `${Math.max(r.width, 320)}px`;
    sugBox.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    if (active >= 0) input.setAttribute('aria-activedescendant', `sg-${active}`);
    else input.removeAttribute('aria-activedescendant');
  }

  function closeSuggest() {
    sugBox.hidden = true;
    if (sug.input) { sug.input.setAttribute('aria-expanded', 'false'); sug.input.removeAttribute('aria-activedescendant'); }
    sug.items = []; sug.active = -1;
  }

  async function loadSuggest(input) {
    const q = input.value.trim();
    const seq = ++sug.seq;
    if (!q) {
      sug.items = store.get('history', []).slice(0, 8).map(name => ({ name, history: true }));
      sug.active = -1; sug.input = input;
      return renderSuggest();
    }
    try {
      const res = await fetch(`/api/stations?q=${encodeURIComponent(q)}&limit=10`);
      const data = await res.json();
      if (seq !== sug.seq || sug.input !== input) return;      // mezitím psal dál
      sug.items = data.places || [];
      sug.active = -1;
      renderSuggest();
    } catch (e) { /* bez našeptávače se dá hledat taky */ }
  }

  function pickSuggest(i) {
    const p = sug.items[i];
    if (!p || !sug.input) return;
    sug.input.value = p.name;
    const next = sug.input === fFrom ? fTo : fDate;
    closeSuggest();
    next.focus();
  }

  function suggester(input) {
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-controls', 'suggest-list');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('spellcheck', 'false');
    input.addEventListener('input', () => {
      sug.input = input;
      clearTimeout(sug.timer);
      sug.timer = setTimeout(() => loadSuggest(input), 110);
    });
    input.addEventListener('focus', () => { sug.input = input; loadSuggest(input); });
    input.addEventListener('blur', () => setTimeout(() => { if (document.activeElement !== input) closeSuggest(); }, 120));
    input.addEventListener('keydown', ev => {
      if (sugBox.hidden || sug.input !== input) {
        if (ev.key === 'ArrowDown') { loadSuggest(input); ev.preventDefault(); }
        return;
      }
      const n = sug.items.length;
      if (ev.key === 'ArrowDown') { sug.active = (sug.active + 1) % n; renderSuggest(); ev.preventDefault(); }
      else if (ev.key === 'ArrowUp') { sug.active = sug.active <= 0 ? n - 1 : sug.active - 1; renderSuggest(); ev.preventDefault(); }
      else if (ev.key === 'Enter' && sug.active >= 0) { pickSuggest(sug.active); ev.preventDefault(); }
      else if (ev.key === 'Escape') { closeSuggest(); ev.preventDefault(); }
      else if (ev.key === 'Tab' && sug.active >= 0) { input.value = sug.items[sug.active].name; closeSuggest(); }
    });
  }

  // mousedown místo click – jinak pole ztratí fokus dřív, než se výběr zapíše
  sugBox.addEventListener('mousedown', ev => {
    const item = ev.target.closest('.sg-item');
    if (!item) return;
    ev.preventDefault();
    pickSuggest(Number(item.dataset.i));
  });
  window.addEventListener('resize', () => { if (!sugBox.hidden) renderSuggest(); });

  // ---- hledání -------------------------------------------------------------
  async function startSearch(ev) {
    if (ev) ev.preventDefault();
    if (!S.providersOn) return toast('Načítám nastavení, zkuste to za okamžik.');
    const providers = [...S.providersOn];
    if (!providers.length) return toast('Vyberte alespoň jeden zdroj dat.');
    const body = {
      from: fFrom.value.trim(), to: fTo.value.trim(), date: fDate.value, time: fTime.value,
      passengers: S.pax.map((p, i) => ({ age: Number(p.age), isic: p.isic, skFree: p.skFree, passes: passesFor(i) })),
      providers, hacks: [...S.hacksOn],
    };
    if (!body.from || !body.to) return toast('Vyplňte odkud a kam.');
    closeStream();
    S.clientError = '';
    S.searching = true;
    S.open = null;
    S.stopsOpen.clear();
    S.off.clear();
    S.data = {
      done: false, query: { from: body.from, to: body.to, departure: `${body.date}T${body.time}` },
      providers: providers.map(name => ({ name, status: 'running', detail: 'hledám…' })),
      problems: [], journeys: [],
    };
    render();
    let res;
    try {
      res = await fetch('/api/search', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    } catch (e) {
      return failClient('Server vyhledávače neodpovídá. Běží ještě okno s programem?');
    }
    const json = await res.json().catch(() => ({}));
    if (!res.ok) return failClient(json.error || 'Hledání se nepodařilo spustit.');
    remember(body.from, body.to);
    S.sid = json.id;
    const es = new EventSource(`/api/search/${json.id}/events`);
    S.es = es;
    es.onmessage = msg => {
      const data = JSON.parse(msg.data);
      if (data.id !== S.sid) return;
      S.data = data;
      if (data.done) { S.searching = false; closeStream(); }
      render();
    };
    es.onerror = () => {
      if (S.es !== es) return;
      closeStream();
      if (S.searching) failClient('Spojení se serverem se přerušilo. Zobrazuji, co stihlo dorazit.');
    };
  }

  function failClient(message) {
    S.searching = false;
    S.clientError = message;
    if (S.data) S.data.done = true;
    render();
  }

  function closeStream() {
    if (S.es) { S.es.close(); S.es = null; }
  }

  async function cancelSearch() {
    if (!S.sid) return;
    try { await fetch(`/api/search/${S.sid}/cancel`, { method: 'POST' }); } catch (e) { /* server stejně skončí */ }
  }

  // ---- odvozená data spoje --------------------------------------------------
  function enrich(j) {
    // HAFAS vkládá i nulové "přesuny" v rámci jedné stanice – nic neříkají
    // a schovaly by box s přestupem mezi vlaky.
    const legs = j.legs.filter(l => !(l.mode === 'walk' && l.from === l.to && minutes(l.arr) - minutes(l.dep) < 1));
    j = { ...j, legs };
    const vehicles = legs.filter(l => l.mode !== 'walk');
    const waits = [];
    for (let i = 1; i < vehicles.length; i++) {
      const prev = vehicles[i - 1], next = vehicles[i];
      waits.push({ at: next.from, gap: Math.round(minutes(next.dep) - minutes(prev.arr)), prev, next });
    }
    return {
      ...j, vehicles, waits,
      depM: minutes(j.dep), arrM: minutes(j.arr),
      realTransfers: Math.max(0, vehicles.length - 1),
      minWait: waits.length ? Math.min(...waits.map(w => w.gap)) : Infinity,
      groups: [...new Set(vehicles.map(v => modeOf(v).group))],
      carriers: [...new Set(vehicles.map(v => v.carrier || j.provider))],
      walkMin: j.legs.filter(l => l.mode === 'walk').reduce((s, l) => s + minutes(l.arr) - minutes(l.dep), 0),
    };
  }

  const priceKey = j => j.price == null ? Infinity : j.price;
  const SORTERS = {
    fastest: (a, b) => a.minutes - b.minutes || a.depM - b.depM,
    fewest: (a, b) => a.realTransfers - b.realTransfers || a.minutes - b.minutes,
    cheapest: (a, b) => priceKey(a) - priceKey(b) || a.depM - b.depM,
    earliest: (a, b) => a.depM - b.depM || a.minutes - b.minutes,
  };

  function visibleJourneys() {
    const all = (S.data ? S.data.journeys : []).map(enrich);
    return all
      .filter(j => j.groups.every(g => S.modes.includes(g)))
      .filter(j => !S.direct || j.realTransfers === 0)
      .filter(j => S.maxT === 'any' || j.realTransfers <= Number(S.maxT))
      .filter(j => j.realTransfers === 0 || j.minWait >= S.buffer)
      .filter(j => !j.carriers.some(c => S.off.has(c)))
      .filter(j => !S.onlyPriced || j.price != null)
      .filter(j => !S.pareto || !j.dominated)
      .filter(sameDepartureFilter())
      .sort(SORTERS[S.sort]);
  }

  // Spoje se stejným odjezdem: nechat jen ty, které za něco stojí.
  //  - přijede později a není levnější -> pryč (levnější pozdější zůstane),
  //  - stejný příjezd i cena -> zůstane ten s méně přestupy,
  //  - pořád shoda -> náhodně jeden (náhoda je pevná po dobu načtené stránky,
  //    ať výběr při každém překreslení neposkakuje).
  // Neznámou cenu nelze porovnat, takový spoj se kvůli ceně neskrývá.
  const TIE_SALT = Math.random().toString(36).slice(2);
  function tieRank(id) {
    let h = 2166136261;
    for (const c of TIE_SALT + id) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }

  function beats(b, a) {
    // Je spoj b (se stejným odjezdem) lepší než a natolik, že a skryjeme?
    const known = a.price != null && b.price != null;
    if (b.arrM < a.arrM) return known && b.price <= a.price;
    if (b.arrM > a.arrM) return false;
    // stejný příjezd
    if (known && b.price !== a.price) return b.price < a.price;
    if (!known && (a.price != null || b.price != null)) return false;
    if (b.realTransfers !== a.realTransfers) return b.realTransfers < a.realTransfers;
    return tieRank(b.id) < tieRank(a.id);
  }

  function sameDepartureFilter() {
    if (!S.sameDep) return () => true;
    return (j, _, list) => !list.some(o => o !== j && o.depM === j.depM && beats(o, j));
  }

  // ---- peníze --------------------------------------------------------------
  function money(czk) {
    if (czk == null) return '';
    if (S.cur === 'EUR') {
      const rate = (S.meta && S.meta.eurRate) || 25;
      return (czk / rate).toFixed(2).replace('.', ',') + ' €';
    }
    return Math.round(czk).toLocaleString('cs-CZ') + ' Kč';
  }

  function priceText(j) {
    if (j.price != null) return money(j.price);
    return { sold_out: 'Vyprodáno', not_bookable: 'Nelze koupit online' }[j.fareStatus] || 'Cena neznámá';
  }

  function originalFare(j) {
    // Cena v cizí měně: ukážeme i originál, ať je vidět, co dopravce účtuje.
    const fare = j.fares.find(f => f.czk != null && Math.abs(f.czk - j.price) < 0.5) || j.fares[0];
    if (!fare || fare.currency === 'CZK' || fare.currency === S.cur) return '';
    return `${fare.amount.toFixed(2).replace('.', ',')} ${fare.currency === 'EUR' ? '€' : fare.currency}`;
  }

  // ---- vykreslení ----------------------------------------------------------
  function render() {
    renderHeader();
    renderModes();
    renderPass();
    renderAdv();
    renderResultsHead();
    renderProviders();
    renderProblems();
    const list = S.data ? visibleJourneys() : [];
    renderTabs(list);
    renderChips();
    renderList(list);
    renderEmpty(list);
    renderDiag();
    $('#loader').hidden = !S.searching;
    $('#submit-btn').disabled = S.searching;
    $('#submit-btn span').textContent = S.searching ? 'Hledám…' : 'Hledat spojení';
    $('#cancel-btn').hidden = !S.searching;
  }

  function renderHeader() {
    const dark = document.documentElement.dataset.theme === 'dark'
      || (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
    $('#theme-btn').innerHTML = icon(dark ? 'sun' : 'moon', 18);
    document.querySelectorAll('#cur-seg input').forEach(i => { i.checked = i.value === S.cur; });
  }

  function renderModes() {
    const all = S.modes.length === ALL_GROUPS.length;
    const pill = (key, label, ic, on) => `<button type="button" class="pill" data-act="mode" data-key="${key}" aria-pressed="${on}">${ic ? icon(ic, 16) : ''}${label}</button>`;
    // Jako v návrhu: odznak počítá jen aktivní filtry, ne nastavení cestujícího.
    const advCount = (S.maxT !== 'any') + S.direct + (S.buffer > 0) + S.onlyPriced;
    $('#modes').innerHTML = `<span class="row-label">Doprava</span>`
      + pill('all', 'Vše', null, all)
      + pill('train', 'Vlaky', 'train-front', !all && S.modes.includes('train'))
      + pill('bus', 'Autobusy', 'bus', !all && S.modes.includes('bus'))
      + pill('city', 'MHD · tram · metro', 'tram-front', !all && S.modes.includes('city'))
      + `<span class="vsep"></span>`
      + `<button class="btn btn-secondary adv-btn" type="button" data-act="pass" aria-expanded="${S.pass}">${icon('user', 16)}<span>${esc(passengerSummary())}</span>${icon(S.pass ? 'chevron-up' : 'chevron-down', 16)}</button>`
      + `<button class="btn btn-secondary adv-btn" type="button" data-act="adv" aria-expanded="${S.adv}">${icon('sliders-horizontal', 16)}<span>Pokročilé možnosti</span>`
      + (advCount ? `<span class="tag tag-accent">${advCount}</span>` : '') + icon(S.adv ? 'chevron-up' : 'chevron-down', 16) + `</button>`;
  }

  function seg(name, current, options) {
    return `<div class="seg">${options.map(([v, label]) =>
      `<label class="seg-opt"><input type="radio" name="${name}" value="${v}" data-act="seg" data-key="${name}" ${String(current) === String(v) ? 'checked' : ''}>${label}</label>`).join('')}</div>`;
  }
  function sw(key, on, label) {
    return `<button type="button" class="switch" role="switch" aria-checked="${on}" data-act="switch" data-key="${key}"><span class="track"><span class="knob"></span></span><span>${label}</span></button>`;
  }

  // Kategorie cestujícího – stejná pravidla jako Passenger.category na serveru.
  function passengerCategory(p) {
    const age = Number(p.age);
    if (age < 6) return 'dítě do 6 let';
    if (age < 15) return 'dítě 6–14 let';
    if (age < 18) return 'mladistvý 15–17 let';
    if (age >= 65) return 'senior 65+';
    if (p.isic && age < 26) return 'student s ISIC';
    return 'dospělý';
  }

  // Předplatné zóny cestujícího pro server: {"PID": "P, 0, B, 1"}.
  function passesFor(i) {
    const out = {};
    S.passes.filter(e => (e.pax === 'all' || e.pax === i) && e.zones.trim()).forEach(e => {
      out[e.system] = out[e.system] ? `${out[e.system]}, ${e.zones}` : e.zones;
    });
    return out;
  }
  const activePasses = () => S.passes.filter(e => e.zones.trim() && (e.pax === 'all' || e.pax < S.pax.length));

  function passengerSummary() {
    const extra = activePasses().length ? ` · předplatné ${[...new Set(activePasses().map(e => e.system))].join(', ')}` : '';
    return passengerSummaryBase() + extra;
  }

  function passengerSummaryBase() {
    if (S.pax.length === 1) {
      const p = S.pax[0];
      const parts = [`${p.age} let`, passengerCategory(p)];
      if (p.skFree) parts.push('SK zdarma');
      return parts.join(' · ');
    }
    const counts = new Map();
    S.pax.forEach(p => { const c = passengerCategory(p); counts.set(c, (counts.get(c) || 0) + 1); });
    const n = S.pax.length;
    return `${n} ${n < 5 ? 'cestující' : 'cestujících'} · ` + [...counts].map(([c, k]) => (k > 1 ? `${k}× ` : '') + c).join(', ');
  }

  function paxSwitch(act, i, on, label) {
    return `<button type="button" class="switch" role="switch" aria-checked="${on}" data-act="${act}" data-key="${i}"><span class="track"><span class="knob"></span></span><span>${label}</span></button>`;
  }

  function renderPass() {
    const box = $('#pass');
    if (!S.pass) { box.innerHTML = ''; return; }
    const isicNote = S.pax.some(p => p.isic && Number(p.age) >= 26)
      ? '<span class="adv-hint warn">ISIC dává slevu jen do 26 let – ceny budou pro dospělého.</span>' : '';
    const skNote = S.pax.some(p => p.skFree) && S.providersOn && !S.providersOn.has('ZSSK')
      ? '<span class="adv-hint warn">Bezplatnou přepravu uplatní jen ZSSK – zapněte ji ve Zdrojích dat.</span>' : '';
    const rows = S.pax.map((p, i) => `<div class="pax-row">
        <span class="pax-n">${i + 1}.</span>
        <label class="adv-age"><input type="number" min="0" max="120" value="${esc(p.age)}" data-act="age" data-key="${i}" aria-label="Věk cestujícího ${i + 1}"> let</label>
        <span class="pax-cat">${esc(passengerCategory(p))}</span>
        ${paxSwitch('pax-isic', i, p.isic, 'ISIC')}
        ${paxSwitch('pax-sk', i, p.skFree, 'SK 100%')}
        ${S.pax.length > 1 ? `<button type="button" class="btn btn-ghost pax-del" data-act="pax-del" data-key="${i}" aria-label="Odebrat cestujícího ${i + 1}">${icon('x', 16)}</button>` : ''}
      </div>`).join('');
    box.innerHTML = `<div class="adv-grid">
      <div class="adv-cell pax-cell"><span class="adv-title">Cestující</span>
        ${rows}
        ${S.pax.length < MAX_PAX ? `<button type="button" class="btn btn-secondary pax-add" data-act="pax-add">${icon('plus', 16)}<span>Přidat cestujícího</span></button>` : ''}
        <span class="adv-hint">Podle věku dopravci volí tarif: dítě, mladistvý, junior/student, dospělý, senior. Cena je za celou skupinu.</span>${isicNote}${skNote}</div>
      <div class="adv-cell"><span class="adv-title">Slevy</span>
        <span class="adv-hint"><strong>ISIC</strong> – studentské jízdné do 26 let: RegioJet Student 18–25, ČD Junior se žákovským průkazem, FlixBus a OneTicket student, ZSSK karta ISIC, studentské jízdné integrovaných systémů.</span>
        <span class="adv-hint"><strong>SK 100%</strong> – bezplatná přeprava ve vlacích ZSSK pro registrované žáky a studenty do 26 let a seniory 62+. Ostatní dopravci ji neznají.</span>
        <span class="adv-hint">ZSSK a ÖBB umí cenu jen pro jednoho cestujícího; ÖBB vždy pro dospělého bez slevy.</span></div>
    </div>`;
  }

  function renderAdv() {
    const box = $('#adv');
    if (!S.adv) { box.innerHTML = ''; return; }
    const hacks = (S.meta ? S.meta.hacks : []).map(h => sw('hack:' + h.name, S.hacksOn.has(h.name), esc(h.label))).join('');
    box.innerHTML = `<div class="adv-grid">
      <div class="adv-cell"><span class="adv-title">Maximální počet přestupů</span>${seg('maxT', S.maxT, [['any', 'Libovolně'], ['0', '0'], ['1', '1'], ['2', '2']])}</div>
      <div class="adv-cell"><span class="adv-title">Pouze přímá spojení</span>${sw('direct', S.direct, S.direct ? 'Zapnuto' : 'Vypnuto')}</div>
      <div class="adv-cell"><span class="adv-title">Minimální čas na přestup</span>${seg('buffer', S.buffer, [[0, 'Standardní'], [3, '+3 min'], [5, '+5 min'], [10, '+10 min']])}<span class="adv-hint">Skryje spojení s kratším přestupem.</span></div>
      <div class="adv-cell"><span class="adv-title">Travel hacky</span>${hacks || '<span class="adv-hint">Žádné nejsou k dispozici.</span>'}
        <span class="adv-hint">Hledají levnější kombinace jízdenek; platí od příštího hledání.</span></div>
      <div class="adv-cell pax-cell"><span class="adv-title">Zobrazení</span>
        ${sw('onlyPriced', S.onlyPriced, 'Jen spoje se známou cenou')}
        ${sw('sameDep', S.sameDep, 'Stejný odjezd: skrýt pozdější a dražší')}
        ${sw('pareto', S.pareto, 'Skrýt dražší i pomalejší')}</div>
      ${renderPassesCell()}
    </div>`;
  }

  function renderPassesCell() {
    const systems = S.meta && S.meta.idsSystems ? S.meta.idsSystems : [];
    if (!systems.length) return '';
    const example = name => (systems.find(x => x.name === name) || {}).example || '';
    const opt = (value, label, on) => `<option value="${esc(value)}"${on ? ' selected' : ''}>${esc(label)}</option>`;
    const rows = S.passes.map((e, i) => {
      const who = S.pax.length > 1
        ? `<select data-act="pass-pax" data-key="${i}" aria-label="Komu předplatné patří">${opt('all', 'všichni', e.pax === 'all')}${S.pax.map((p, k) => opt(k, `${k + 1}. cestující (${p.age} let)`, e.pax === k)).join('')}</select>` : '';
      return `<div class="pax-row">
        ${who}
        <select data-act="pass-sys" data-key="${i}" aria-label="Integrovaný systém">${systems.map(x => opt(x.name, x.name, x.name === e.system)).join('')}</select>
        <label class="pass-zones">zóny <input type="text" value="${esc(e.zones)}" placeholder="např. ${esc(example(e.system))}" data-act="pass-zones" data-key="${i}" aria-label="Předplacené zóny (oddělte čárkou, * = celá síť)" autocomplete="off"></label>
        <button type="button" class="btn btn-ghost pax-del" data-act="pass-del" data-key="${i}" aria-label="Odebrat předplatné">${icon('x', 16)}</button>
      </div>`;
    }).join('');
    return `<div class="adv-cell wide-cell"><span class="adv-title">Předplatné jízdenky IDS</span>
      ${rows}
      <button type="button" class="btn btn-secondary pax-add" data-act="pass-add">${icon('plus', 16)}<span>Přidat předplatné</span></button>
      <span class="adv-hint">Zóny, na které máte časovou jízdenku (kupón). Jízdenky IDS pak počítají jen s doplatkem za zóny mimo ni – uvnitř předplatného je cesta zdarma. Více zón oddělte čárkou, <strong>*</strong> = celá síť. Kód zóny vidíte v našeptávači u zastávky (např. „IDS JMK · zóny 100, 101“).</span></div>`;
  }

  function renderResultsHead() {
    const head = $('#results-head');
    if (!S.data) {
      head.innerHTML = `<h2>Kam to bude?</h2><div class="meta">Zadejte trasu a čas – hledám u všech vybraných dopravců najednou a výsledky ukazuji, jak přicházejí.</div>`;
      return;
    }
    const q = S.data.query;
    const d = new Date(dayOf(q.departure) + 'T12:00');
    const dateT = isNaN(d) ? '' : `${DAYS[d.getDay()]} ${d.getDate()}. ${d.getMonth() + 1}. ${d.getFullYear()}`;
    const n = visibleJourneys().length, total = S.data.journeys.length;
    const hidden = total - n;
    head.innerHTML = `<h2>${esc(q.from)} → ${esc(q.to)}</h2>
      <div class="meta">${dateT} · odjezd od ${hhmm(q.departure)} · ${n} ${pl(n, 'spojení', 'spojení', 'spojení')}${hidden > 0 ? ` (${hidden} skryto filtry)` : ''}${S.searching ? ' · hledám dál…' : ''}</div>`;
  }

  function providerStatus(name) {
    const st = S.data && S.data.providers.find(p => p.name === name);
    return st || null;
  }

  function renderProviders() {
    if (!S.meta) { $('#providers').innerHTML = ''; return; }
    const dotColor = st => ({
      ok: 'var(--color-text)', running: 'var(--color-accent)', error: 'var(--color-accent)', blocked: 'var(--color-accent-700)',
      empty: 'var(--color-neutral-500)', off: 'var(--color-neutral-400)',
    })[st] || 'var(--color-neutral-400)';
    const chip = p => {
      const on = S.providersOn.has(p.name);
      const st = providerStatus(p.name);
      const status = st ? st.status : (on ? 'idle' : 'off');
      let detail = st ? st.detail : (on ? 'připraveno' : 'vypnuto');
      if (st && st.status === 'error') detail = 'chyba';
      const notes = [];
      if (p.browser) notes.push(S.meta.playwright ? 'prohlížeč' : 'chybí prohlížeč');
      if (!p.prices) notes.push('bez cen');
      const symbol = st && (st.status === 'error' || st.status === 'blocked') ? icon('triangle-alert', 12) : `<span class="dot ${status === 'running' ? 'running' : ''}" style="background:${dotColor(status)}"></span>`;
      return `<button type="button" class="prov" data-act="provider" data-key="${esc(p.name)}" aria-pressed="${on}" title="${esc(p.label)}">${symbol}<strong>${esc(p.name)}</strong>${esc(detail)}${notes.length ? `<span class="note">· ${notes.join(' · ')}</span>` : ''}</button>`;
    };
    // Dopravci po oblastech; klik na název oblasti zapne/vypne celou skupinu.
    const regions = S.meta.regions || [{ key: '', label: '' }];
    const groups = regions.map(r => {
      const list = S.meta.providers.filter(p => (p.region || '') === r.key);
      if (!list.length) return '';
      const onCount = list.filter(p => S.providersOn.has(p.name)).length;
      const state = onCount === list.length ? 'true' : onCount ? 'mixed' : 'false';
      return `<div class="prov-group" role="group" aria-label="${esc(r.label)}"><button type="button" class="prov-region" data-act="prov-region" data-key="${esc(r.key)}" aria-pressed="${state}" title="${state === 'true' ? 'Vypnout' : 'Zapnout'} všechny dopravce: ${esc(r.label)}">${esc(r.label)}<span class="count">${onCount}/${list.length}</span></button><div class="prov-chips">${list.map(chip).join('')}</div></div>`;
    }).join('');
    $('#providers').innerHTML = `<div class="prov-head"><span class="row-label">Zdroje dat</span><span class="prov-links"><button class="btn btn-ghost" type="button" data-act="prov-fast">rychlé</button><button class="btn btn-ghost" type="button" data-act="prov-all">vše</button></span></div>${groups}`;
  }

  // Diagnostika: jaká verze serveru běží, které zdroje zná a jak dopadlo hledání.
  function diagText() {
    const lines = [];
    const m = S.meta;
    if (!m) return 'Nastavení ze serveru se nenačetlo – server neběží nebo neodpovídá.';
    const v = m.version || {};
    lines.push(v.fingerprint
      ? `Verze serveru: větev ${v.branch || '?'}, commit ${v.commit || '?'}, otisk ${v.fingerprint}, spuštěn ${v.started || '?'}`
      : 'Verze serveru: neznámá – běží starší verze programu (před přidáním diagnostiky). Zavřete všechna okna aplikace a spusťte ji znovu.');
    if (S.staleServer) lines.push('POZOR: server byl mezitím vyměněn za jinou verzi – obnovte stránku (F5).');
    if (v.log) lines.push(`Záznam serveru: ${v.log}`);
    const names = m.providers.map(p => p.name);
    lines.push(`Zdroje, které server zná: ${names.join(', ')}`);
    const on = S.providersOn ? [...S.providersOn] : [];
    lines.push(`Zapnuté zdroje: ${on.join(', ') || '–'}`);
    const off = names.filter(n => !on.includes(n));
    if (off.length) lines.push(`Vypnuté zdroje: ${off.join(', ')} (zapnete kliknutím na čip v řádku „Zdroje dat“)`);
    lines.push(`Zapnuté hacky: ${S.hacksOn ? [...S.hacksOn].join(', ') || '–' : '–'}`);
    const errors = Object.entries(m.loadErrors || {});
    lines.push(errors.length ? `Moduly, které se nenačetly:\n${errors.map(([k, e]) => `  ${k}: ${e}`).join('\n')}` : 'Všechny moduly se načetly.');
    lines.push(`Prohlížeč pro ČD/ZSSK (Playwright): ${m.playwright ? 'ano' : 'ne'}`);
    if (S.data) {
      const q = S.data.query || {};
      lines.push('', `Poslední hledání: ${q.from} → ${q.to}, ${q.departure}${S.data.done ? '' : ' (běží)'}`);
      for (const p of S.data.providers || []) {
        lines.push(`  ${p.name}: ${p.status}${p.detail ? ` – ${p.detail}` : ''}${p.elapsed != null ? ` (${p.elapsed} s)` : ''}`);
      }
      for (const p of S.data.problems || []) lines.push(`  ! ${p.provider}: ${p.message}${p.raw ? ` [${p.raw}]` : ''}`);
      lines.push(`  Spojení ze serveru: ${(S.data.journeys || []).length}`);
      if ((S.data.trace || []).length) {
        lines.push('', 'Jak program přemýšlel (lomy jízdenek, dotazy na dopravce):');
        for (const t of S.data.trace) lines.push(`  ${t}`);
      }
    }
    if (S.clientError) lines.push(`Chyba v prohlížeči: ${S.clientError}`);
    return lines.join('\n');
  }

  function renderDiag() {
    const body = $('#diag-body');
    if (!body || !$('#diag').open) return;
    body.innerHTML = `<pre>${esc(diagText())}</pre><button class="btn btn-ghost" type="button" data-act="diag-copy">kopírovat</button>`;
  }

  function renderProblems() {
    const box = $('#problems');
    const problems = [...(S.data ? S.data.problems : [])];
    if (S.clientError) problems.unshift({ provider: 'Aplikace', message: S.clientError, kind: 'error' });
    if (!problems.length) { box.innerHTML = ''; return; }
    const ic = { error: 'triangle-alert', blocked: 'ban', soft: 'info', not_served: 'circle-slash' };
    box.innerHTML = `<div class="problems"><h3>Co se nepovedlo</h3>${problems.map(p => `
      <div class="problem ${esc(p.kind)}"><span class="p-icon">${icon(ic[p.kind] || 'info', 14)}</span>
        <div><strong>${esc(p.provider)}</strong>${esc(p.message)}</div>
        ${p.canDisable && S.providersOn && S.providersOn.has(p.provider) ? `<button class="btn btn-ghost" type="button" data-act="disable" data-key="${esc(p.provider)}">vypnout</button>` : '<span></span>'}
      </div>`).join('')}</div>`;
  }

  function renderTabs(list) {
    if (!S.data) { $('#tabs').innerHTML = ''; $('#tabs').hidden = true; return; }
    $('#tabs').hidden = false;
    const best = k => list.length ? [...list].sort(SORTERS[k])[0] : null;
    const bf = best('fastest'), bt = best('fewest'), bc = best('cheapest'), be = best('earliest');
    const tabs = [
      ['fastest', 'Nejrychlejší', bf ? dur(bf.minutes) : '—'],
      ['fewest', 'Nejméně přestupů', bt ? (bt.realTransfers ? pl(bt.realTransfers, '1 přestup', `${bt.realTransfers} přestupy`, `${bt.realTransfers} přestupů`) : 'Přímé') : '—'],
      ['cheapest', 'Nejlevnější', bc && bc.price != null ? money(bc.price) : '—'],
      ['earliest', 'Nejdřívější odjezd', be ? hhmm(be.dep) : '—'],
    ];
    $('#tabs').innerHTML = tabs.map(([k, label, val]) =>
      `<button type="button" role="tab" class="sort-tab" aria-selected="${S.sort === k}" data-act="sort" data-key="${k}"><span class="t-label">${label}</span><span class="t-val">${esc(val)}</span></button>`).join('');
  }

  function renderChips() {
    const box = $('#chips');
    if (!S.data || !S.data.journeys.length) { box.innerHTML = ''; return; }
    const carriers = new Map();
    S.data.journeys.map(enrich).forEach(j => j.vehicles.forEach(v => {
      const name = v.carrier || j.provider;
      if (!carriers.has(name)) carriers.set(name, modeOf(v).icon);
    }));
    const chips = [...carriers].sort((a, b) => a[0].localeCompare(b[0], 'cs')).map(([name, ic]) =>
      `<button type="button" class="chip" data-act="carrier" data-key="${esc(name)}" aria-pressed="${!S.off.has(name)}">${icon(ic, 14)}${esc(name)}</button>`).join('');
    const active = [];
    if (S.modes.length !== ALL_GROUPS.length) active.push(['modes', 'Doprava: ' + S.modes.map(m => ({ train: 'vlaky', bus: 'autobusy', city: 'MHD' })[m]).join(', ')]);
    if (S.direct) active.push(['direct', 'Jen přímá']);
    if (S.maxT !== 'any') active.push(['maxT', `Max. ${S.maxT} ${pl(Number(S.maxT), 'přestup', 'přestupy', 'přestupů')}`]);
    if (S.buffer) active.push(['buffer', `Přestup ≥ ${S.buffer} min`]);
    if (S.onlyPriced) active.push(['onlyPriced', 'Jen se známou cenou']);
    const any = active.length || S.off.size;
    box.innerHTML = `<span class="row-label">Dopravci</span>${chips}`
      + active.map(([k, label]) => `<button type="button" class="tag tag-accent chip-clear" data-act="clear" data-key="${k}">${esc(label)}${icon('x', 13)}</button>`).join('')
      + (any ? `<button class="btn btn-ghost" type="button" data-act="reset" style="font-size:13px">Zrušit filtry</button>` : '')
      + `<span class="vsep"></span><div class="legend"><span><i style="background:var(--color-text)"></i>Vlak</span><span><i style="background:var(--mode-bus)"></i>Autobus</span><span><i style="background:var(--mode-city)"></i>MHD</span><span><i class="walk"></i>Pěšky / přesun</span></div>`;
  }

  // ---- karta spoje ---------------------------------------------------------
  function legLabel(leg) { return leg.line || modeOf(leg).name; }

  function routeStrip(j) {
    const parts = [];
    if (j.hack) parts.push(`<span class="badge tag-accent">${icon('lightbulb', 14)}Travel hack</span>`);
    j.vehicles.forEach((v, i) => {
      const m = modeOf(v);
      const badge = `<span class="badge" style="background:${m.bg};color:${m.fg}" title="${esc(legLabel(v))}: ${esc(v.from)} → ${esc(v.to)}">${icon(m.icon, 14)}${esc(legLabel(v))}</span>`;
      if (i === 0) { parts.push(badge); return; }
      const w = j.waits[i - 1];
      const risk = w.gap < RISKY_TRANSFER;
      // přestup a další spoj drží pohromadě – při zalomení řádku se nerozdělí
      parts.push(`<span class="strip-step"><span class="strip-x ${risk ? 'risk' : ''}" title="Přestup ${w.gap} min v ${esc(w.at)}${risk ? ' – riziko' : ''}">${icon('arrow-right', 13)}<span>${esc(shortStation(w.at))} · ${w.gap} min</span>${icon('arrow-right', 13)}</span>${badge}</span>`);
    });
    return `<div class="strip">${parts.join('')}</div>`;
  }

  // „Veselí n.Lužnicí“ zůstane, „Brno, ÚAN Zvonařka (stan. 5)“ -> „Brno, ÚAN Zvonařka“.
  function shortStation(name) { return String(name || '').replace(/\s*\([^)]*\)\s*$/, ''); }

  function journeyCard(j) {
    const isOpen = S.open === j.id;
    const first = j.vehicles[0] || j.legs[0];
    const days = Math.round((new Date(dayOf(j.arr)) - new Date(dayOf(j.dep))) / 86400000);
    // Jeden řádek: čím se jede a kde se přestupuje. Podrobnosti (časy,
    // zastávky) jsou v rozbaleném detailu – tady by se jen opakovaly.
    const strip = routeStrip(j);
    const reservation = j.legs.some(l => l.reservation) ? `<span class="tag tag-neutral">povinná místenka</span>` : '';
    const platform = first && first.stops[0] && first.stops[0].platform ? `<span class="muted">${esc(first.from)}: nástupiště ${esc(first.stops[0].platform)}</span>` : '';
    const seats = (j.fares.find(f => f.seats != null) || {}).seats;
    const subParts = [esc(j.provider)];
    const orig = j.price != null ? originalFare(j) : '';
    if (orig) subParts.push(orig);
    if (seats != null && seats <= 6) subParts.push(pl(seats, 'zbývá 1 místo', `zbývají ${seats} místa`, `zbývá ${seats} míst`));
    const stillPricing = j.price == null && S.searching && (providerStatus(j.provider) || {}).status === 'running';
    const buyLabel = j.price != null ? 'Koupit' : 'U dopravce';

    return `<article class="jr ${isOpen ? 'open' : ''}" data-id="${esc(j.id)}">
      <div class="jr-head" role="button" tabindex="0" data-act="toggle" data-key="${esc(j.id)}" aria-expanded="${isOpen}">
        <div class="jr-times">
          <div class="jr-clock"><span>${hhmm(j.dep)}</span><span class="dash">–</span><span>${hhmm(j.arr)}</span>${days > 0 ? `<sup>+${days}</sup>` : ''}</div>
          <div class="jr-dur"><strong>${dur(j.minutes)}</strong><span class="muted">${j.realTransfers ? pl(j.realTransfers, '1 přestup', `${j.realTransfers} přestupy`, `${j.realTransfers} přestupů`) : 'Přímé'}</span></div>
          <div class="jr-stations">${esc(j.legs[0].from)} → ${esc(j.legs[j.legs.length - 1].to)}</div>
        </div>
        <div class="jr-mid">${strip}${reservation || platform ? `<div class="jr-info">${reservation}${platform}</div>` : ''}</div>
        <div class="jr-price">
          <div><div class="amount ${j.price == null ? 'unknown' : ''}">${esc(priceText(j))}</div>
            <div class="sub">${stillPricing ? `<span class="pending">${icon('loader-circle', 12, 'spin')}zjišťuji cenu…</span>` : subParts.join(' · ')}</div></div>
          <div class="jr-actions">
            <button class="btn btn-secondary" type="button" data-act="toggle" data-key="${esc(j.id)}">Detail${icon(isOpen ? 'chevron-up' : 'chevron-down', 16)}</button>
            ${j.link ? `<button class="btn btn-primary" type="button" data-act="buy" data-key="${esc(j.id)}">${buyLabel}</button>` : ''}
          </div>
        </div>
      </div>
      ${isOpen ? journeyDetail(j) : ''}
    </article>`;
  }

  // Kraj · integrovaný systém · zóna stanice (z databáze zastávek).
  function placeInfo(p) {
    const text = placeMeta(p);
    return text ? `<span class="place-info" title="${esc(p.idsName || p.country || '')}">${icon('map-pin', 12)}${esc(text)}</span>` : '';
  }

  function journeyDetail(j) {
    const steps = [];
    j.legs.forEach((leg, i) => {
      if (leg.mode === 'walk') {
        steps.push(`<div class="step"><div class="muted" style="padding:12px 0;font-size:13px">${hhmm(leg.dep)}</div><div class="walk-line"><span></span></div>
          <div class="walk-body"><span class="what">${icon('footprints', 16)}Přesun · ${dur(minutes(leg.arr) - minutes(leg.dep))}</span><span class="route">${esc(leg.from)} → ${esc(leg.to)}</span></div></div>`);
      } else {
        const m = modeOf(leg);
        const key = j.id + '#' + i;
        const mid = leg.stops.slice(1, -1);
        const on = S.stopsOpen.has(key);
        const startPlat = leg.stops[0] && leg.stops[0].platform ? `nástupiště ${esc(leg.stops[0].platform)}` : '';
        const endStop = leg.stops[leg.stops.length - 1];
        const endPlat = endStop && endStop.platform && leg.stops.length > 1 ? `nástupiště ${esc(endStop.platform)}` : '';
        steps.push(`<div class="step">
          <div class="step-time"><strong>${hhmm(leg.dep)}</strong></div>
          <div class="rail"><span class="node" style="border-color:${m.bg}"></span><span class="bar" style="background:${m.bg}"></span><span class="node" style="border-color:${m.bg}"></span></div>
          <div class="step-body">
            <div class="step-place"><strong>${esc(leg.from)}</strong><span>${startPlat}</span>${placeInfo(leg.fromInfo)}</div>
            <div class="step-line"><span class="badge" style="background:${m.bg};color:${m.fg}">${icon(m.icon, 14)}${esc(legLabel(leg))}</span>
              <span class="meta">${esc(leg.carrier || j.provider)} · ${dur(minutes(leg.arr) - minutes(leg.dep))}</span>
              ${leg.reservation ? '<span class="tag tag-neutral">povinná místenka</span>' : ''}</div>
            ${mid.length ? `<button type="button" class="btn btn-ghost stops-btn" data-act="stops" data-key="${esc(key)}">${icon(on ? 'chevron-up' : 'chevron-down', 14)}${on ? 'Skrýt nácestné zastávky' : `Zobrazit ${mid.length} ${pl(mid.length, 'nácestnou zastávku', 'nácestné zastávky', 'nácestných zastávek')}`}</button>` : ''}
            ${on ? `<div class="stops">${mid.map(s => `<div><span>${hhmm(s.arr || s.dep)}</span><span>${esc(s.name)}</span></div>`).join('')}</div>` : ''}
          </div>
          <div class="step-arr"><strong>${hhmm(leg.arr)}</strong></div>
          <div class="step-place" style="padding-bottom:6px"><strong>${esc(leg.to)}</strong><span>${endPlat}</span>${placeInfo(leg.toInfo)}</div>
        </div>`);
      }
      const next = j.legs[i + 1];
      if (next) {
        const gap = Math.round(minutes(next.dep) - minutes(leg.arr));
        if (gap > 0 && leg.mode !== 'walk' && next.mode !== 'walk') {
          const risk = gap < RISKY_TRANSFER;
          steps.push(`<div class="step"><div></div><div class="wait-line"><span></span></div>
            <div class="wait-box ${risk ? 'risk' : ''}"><span class="what">${icon(risk ? 'triangle-alert' : 'clock', 15)}Přestup ${gap} min · ${esc(next.from)}</span>
            ${risk ? `<span class="hint">Krátký přestup – při zpoždění hrozí, že ${esc(legLabel(next))} ujede.</span>` : ''}</div></div>`);
        }
      }
    });

    const fare = j.fares[0];
    const rows = [
      ['Doba jízdy', dur(j.minutes)],
      ['Přestupy', j.realTransfers ? String(j.realTransfers) : 'Přímé'],
      ['Zdroj dat', j.provider],
      ['Cena', priceText(j) + (originalFare(j) ? ` (${originalFare(j)})` : '')],
    ];
    // "od" je u ÖBB jen předložka k ceně ("od 22 €"), jako tarif nic neříká.
    const fareLabel = fare && fare.label !== 'od' ? fare.label : '';
    if (fare && (fare.tariff || fareLabel)) rows.push(['Tarif', [fareLabel, fare.tariff].filter(Boolean).join(' · ')]);
    if (j.walkMin) rows.push(['Přesuny pěšky', dur(j.walkMin)]);
    const rides = j.legs.filter(l => l.mode !== 'walk');
    const startInfo = (rides[0] || j.legs[0]).fromInfo, endInfo = (rides[rides.length - 1] || j.legs[j.legs.length - 1]).toInfo;
    const zoneOf = p => p && p.zones && p.zones.length ? `${p.ids} ${p.zones.join(', ')}` : '';
    if (zoneOf(startInfo) || zoneOf(endInfo)) rows.push(['Tarifní zóny', `${zoneOf(startInfo) || '?'} → ${zoneOf(endInfo) || '?'}`]);
    const segments = j.segments.length ? `<div class="segments"><div class="sec-title" style="margin:0">Jízdenky ke koupi</div>${j.segments.map((s, i) =>
      `<div>${i + 1}. ${esc(s.from)} → ${esc(s.to)} · ${esc(s.provider)}: <strong>${esc(s.priceLabel)}</strong>${s.link ? ` · <a href="${esc(s.link)}" target="_blank" rel="noopener">koupit</a>` : ''}</div>`).join('')}</div>` : '';
    const notes = j.notes.map(n => `<p class="note-warn">${esc(n)}</p>`).join('');
    return `<div class="jr-detail">
      <div class="itin"><div class="sec-title">Itinerář</div>${steps.join('')}</div>
      <aside class="summary"><div class="sec-title" style="margin:0">Souhrn</div>
        <div class="rows">${rows.map(([k, v]) => `<div class="row"><span>${k}</span><strong>${esc(v)}</strong></div>`).join('')}</div>
        ${notes}${segments}
        ${j.price == null && j.fareStatus === 'unknown' ? '<p>Cenu tento zdroj neuvádí – ověřte ji u dopravce.</p>' : ''}
        ${j.link ? `<button class="btn btn-primary buy" type="button" data-act="buy" data-key="${esc(j.id)}"><span>${j.price != null ? 'Koupit u ' : 'Otevřít u '}${esc(j.provider)}</span>${icon('arrow-up-right', 18)}</button>` : ''}
      </aside>
    </div>`;
  }

  function renderList(list) {
    $('#list').innerHTML = list.map(journeyCard).join('');
    $('#list').style.opacity = S.searching && !list.length ? '.4' : '1';
  }

  function renderEmpty(list) {
    const box = $('#empty');
    if (!S.data || list.length) { box.innerHTML = ''; return; }
    if (S.searching) {
      box.innerHTML = `<div class="empty"><h3>Hledám…</h3><p>Výsledky se zobrazí, jakmile odpoví první dopravce. Pomalejší zdroje (prohlížeč) dobíhají postupně.</p></div>`;
      return;
    }
    const total = S.data.journeys.length;
    if (total) {
      box.innerHTML = `<div class="empty"><h3>Žádné spojení neodpovídá filtrům</h3><p>Nalezeno ${total} spojení, ale všechna skrývají zvolené filtry. Zkuste povolit další druhy dopravy, dopravce nebo delší přestupy.</p><button class="btn btn-secondary" type="button" data-act="reset-all">Zrušit filtry</button></div>`;
      return;
    }
    const failed = S.data.problems.filter(p => p.kind === 'error' || p.kind === 'blocked').map(p => p.provider);
    const notServed = S.data.problems.filter(p => p.kind === 'not_served').map(p => p.provider);
    let text = 'Žádné spoje pro zadané parametry. Zkuste jiný čas nebo víc zdrojů dat.';
    if (failed.length) text = `Nepodařilo se načíst: ${[...new Set(failed)].join(', ')}. Zkuste jiné zdroje dat, nebo hledání opakujte.`;
    else if (notServed.length) text = `${[...new Set(notServed)].join(', ')} do této destinace nejezdí. Zapněte další zdroje dat.`;
    box.innerHTML = `<div class="empty"><h3>Nic jsem nenašel</h3><p>${esc(text)}</p></div>`;
  }

  // ---- toast ---------------------------------------------------------------
  let toastTimer = null;
  function toast(text, ic = 'info') {
    const el = $('#toast');
    el.innerHTML = icon(ic, 18) + `<span>${esc(text)}</span>`;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 2800);
  }

  // ---- akce ----------------------------------------------------------------
  function persistOptions() {
    if (S.providersOn) store.set('providers', [...S.providersOn]);
    store.set('hacks', [...S.hacksOn]);
    store.set('pax', S.pax);
    store.set('passes', S.passes);
    store.set('onlyPriced', S.onlyPriced);
    store.set('pareto', S.pareto);
    store.set('sameDep', S.sameDep);
    store.set('sort', S.sort);
    store.set('cur', S.cur);
  }

  function findJourney(id) {
    return S.data && S.data.journeys.find(j => j.id === id);
  }

  const ACTIONS = {
    mode(key) {
      if (key === 'all') { S.modes = [...ALL_GROUPS]; return; }
      let m = S.modes.length === ALL_GROUPS.length ? [key] : (S.modes.includes(key) ? S.modes.filter(x => x !== key) : [...S.modes, key]);
      S.modes = m.length ? m : [...ALL_GROUPS];
    },
    adv() { S.adv = !S.adv; if (S.adv) S.pass = false; },
    pass() { S.pass = !S.pass; if (S.pass) S.adv = false; },
    switch(key) {
      if (key.startsWith('hack:')) {
        const name = key.slice(5);
        S.hacksOn.has(name) ? S.hacksOn.delete(name) : S.hacksOn.add(name);
      } else { S[key] = !S[key]; }
    },
    sort(key) { S.sort = key; },
    carrier(key) { S.off.has(key) ? S.off.delete(key) : S.off.add(key); },
    clear(key) {
      if (key === 'modes') S.modes = [...ALL_GROUPS];
      else if (key === 'maxT') S.maxT = 'any';
      else if (key === 'buffer') S.buffer = 0;
      else S[key] = false;
    },
    reset() { S.modes = [...ALL_GROUPS]; S.maxT = 'any'; S.direct = false; S.buffer = 0; S.onlyPriced = false; S.off.clear(); },
    'reset-all'() { ACTIONS.reset(); S.pareto = false; S.sameDep = false; },
    'pax-add'() { if (S.pax.length < MAX_PAX) S.pax.push({ age: 26, isic: false, skFree: false }); },
    'pax-del'(key) {
      const k = Number(key);
      if (S.pax.length <= 1) return;
      S.pax.splice(k, 1);
      S.passes = S.passes.filter(e => e.pax !== k).map(e => (e.pax !== 'all' && e.pax > k ? { ...e, pax: e.pax - 1 } : e));
    },
    'pass-add'() {
      const systems = S.meta && S.meta.idsSystems ? S.meta.idsSystems : [];
      if (systems.length && S.passes.length < 20) S.passes.push({ pax: S.pax.length > 1 ? 'all' : 0, system: systems.some(x => x.name === 'PID') ? 'PID' : systems[0].name, zones: '' });
    },
    'pass-del'(key) { S.passes.splice(Number(key), 1); },
    'pax-isic'(key) { const p = S.pax[Number(key)]; if (p) p.isic = !p.isic; },
    'pax-sk'(key) { const p = S.pax[Number(key)]; if (p) p.skFree = !p.skFree; },
    provider(key) { S.providersOn.has(key) ? S.providersOn.delete(key) : S.providersOn.add(key); },
    'prov-fast'() { S.providersOn = new Set(S.meta.providers.filter(p => !p.browser).map(p => p.name)); },
    'prov-region'(key) {
      const list = S.meta.providers.filter(p => (p.region || '') === key).map(p => p.name);
      const allOn = list.every(n => S.providersOn.has(n));
      list.forEach(n => allOn ? S.providersOn.delete(n) : S.providersOn.add(n));
    },
    'prov-all'() { S.providersOn = new Set(S.meta.providers.map(p => p.name)); },
    disable(key) { S.providersOn.delete(key); toast(`${key} je vypnutý pro další hledání.`); },
    toggle(key) { S.open = S.open === key ? null : key; },
    'diag-copy'() {
      navigator.clipboard.writeText(diagText()).then(() => toast('Diagnostika je ve schránce.'), () => toast('Kopírování se nepovedlo – označte text ručně.'));
    },
    stops(key) { S.stopsOpen.has(key) ? S.stopsOpen.delete(key) : S.stopsOpen.add(key); },
    buy(key) {
      const j = findJourney(key);
      if (!j || !j.link) return;
      window.open(j.link, '_blank', 'noopener');
      toast(`Otevírám web dopravce ${j.provider}…`, 'arrow-up-right');
    },
  };

  document.addEventListener('click', ev => {
    const el = ev.target.closest('[data-act]');
    if (!el || el.tagName === 'INPUT') return;
    const act = ACTIONS[el.dataset.act];
    if (!act) return;
    ev.stopPropagation();
    act(el.dataset.key);
    persistOptions();
    render();
  });
  document.addEventListener('keydown', ev => {
    if ((ev.key === 'Enter' || ev.key === ' ') && ev.target.matches('.jr-head')) {
      ev.preventDefault();
      ACTIONS.toggle(ev.target.dataset.key);
      render();
    }
  });
  document.addEventListener('change', ev => {
    const el = ev.target;
    if (el.dataset.act === 'seg') {
      S[el.dataset.key] = el.dataset.key === 'buffer' ? Number(el.value) : el.value;
    } else if (el.dataset.act === 'age') {
      const v = Math.max(0, Math.min(120, parseInt(el.value, 10) || 0));
      const p = S.pax[Number(el.dataset.key) || 0];
      if (p) p.age = v;
    } else if (el.dataset.act === 'pass-pax' || el.dataset.act === 'pass-sys' || el.dataset.act === 'pass-zones') {
      const e = S.passes[Number(el.dataset.key)];
      if (!e) return;
      if (el.dataset.act === 'pass-pax') e.pax = el.value === 'all' ? 'all' : Number(el.value);
      else if (el.dataset.act === 'pass-sys') e.system = el.value;
      else e.zones = el.value.slice(0, 200);
    } else if (el.name === 'cur') {
      S.cur = el.value;
    } else return;
    persistOptions();
    render();
  });

  form.addEventListener('submit', startSearch);
  $('#cancel-btn').addEventListener('click', cancelSearch);
  $('#swap-btn').addEventListener('click', () => { const a = fFrom.value; fFrom.value = fTo.value; fTo.value = a; });
  $('#theme-btn').addEventListener('click', () => {
    const root = document.documentElement;
    const dark = root.dataset.theme === 'dark' || (!root.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
    root.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('vs.theme', root.dataset.theme); } catch (e) { /* jen pohodlí */ }
    render();
  });

  // Server se sám ukončí, když se prohlížeč delší dobu neozve.
  // Ping zároveň hlídá verzi: když spouštěč vymění server za novější (po
  // aktualizaci), stránka se sama obnoví, ať nezůstane u starého kódu.
  setInterval(() => {
    fetch('/api/ping').then(r => r.json()).then(r => {
      const mine = S.meta && S.meta.version && S.meta.version.fingerprint;
      if (!mine || !r.version || r.version === mine) return;
      if (!S.searching) location.reload();
      else if (!S.staleServer) { S.staleServer = true; toast('Program byl aktualizován – po hledání obnovte stránku (F5).'); render(); }
    }).catch(() => {});
  }, 20000);
  $('#diag').addEventListener('toggle', renderDiag);

  // ---- start ---------------------------------------------------------------
  initForm();
  render();
  fetch('/api/meta').then(r => r.json()).then(meta => {
    S.meta = meta;
    const saved = store.get('providers', null);
    const names = meta.providers.map(p => p.name);
    S.providersOn = new Set(saved ? saved.filter(n => names.includes(n)) : meta.providers.filter(p => p.default).map(p => p.name));
    // Nově přidaní dopravci (např. Bileto, Arriva) se zapnou i uloženému výběru.
    // (Starší verze si „známé“ dopravce uložila dřív, než zapnula PKP, Leo Express
    // a IDS – proto nový klíč a jednorázové zapnutí všech dopravců přidaných od té doby.)
    const known = store.get('providersKnown2', null);
    const added = ['Bileto', 'Arriva', 'PKP', 'MÁV', 'DB', 'NS', 'Trenitalia', 'OUIGO', 'SNCF', 'Vy', 'DSB', 'SJ', 'BDŽ', 'CP', 'Srbijavoz', 'SBB', 'Renfe', 'Eurostar',
      'EuropeanSleeper', 'CFR', 'SŽ', 'HŽPP', 'LeoExpress', 'IDS', 'Plánovač'];
    if (saved) meta.providers.filter(p => p.default && (Array.isArray(known) ? !known.includes(p.name) : added.includes(p.name)))
      .forEach(p => S.providersOn.add(p.name));
    store.set('providersKnown2', names);
    persistOptions();
    render();
  }).catch(() => failClient('Nepodařilo se načíst nastavení ze serveru.'));
})();
