(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var TOKEN_RE = /^[A-Za-z0-9_-]{43}$/;
  var TK = 'eum_qr_t', AK = 'eum_qr_a';
  var scanned = false, token = null, expiresAt = 0, tick = 0, poll = 0, busy = false;

  function ss(k, v) {
    try { if (v === undefined) return sessionStorage.getItem(k); if (v === null) sessionStorage.removeItem(k); else sessionStorage.setItem(k, v); } catch (e) {}
    return null;
  }
  function show(id) {
    ['stLoading', 'stMsg', 'stAuth', 'stOk'].forEach(function (s) { $(s).hidden = s !== id; });
  }
  function wipe() { $('pName').textContent = ''; $('pMeta').textContent = ''; $('pList').textContent = ''; }
  function stopTimers() { clearInterval(tick); clearInterval(poll); tick = poll = 0; }

  function msg(icon, title, desc, retry) {
    stopTimers(); wipe();
    $('demoBanner').hidden = true;
    $('msgIcon').textContent = icon; $('msgTitle').textContent = title; $('msgDesc').textContent = desc;
    $('msgBtn').hidden = !retry;
    show('stMsg');
  }

  // 토큰은 fragment(#t=...)로만 들어온다. 읽은 뒤 주소창에서 지우고 탭 세션에만 보관한다.
  function readToken() {
    var m = /(?:^#|&)t=([^&]+)/.exec(location.hash || '');
    if (m) {
      ss(TK, m[1]);
      try { history.replaceState(null, '', location.pathname); } catch (e) {}
    }
    var t = ss(TK);
    return t && TOKEN_RE.test(t) ? t : null;
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;       // 입력값은 항상 textContent로만 넣는다
    return e;
  }
  function fmt(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return '-';
    return d.toLocaleString('ko-KR', { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
  }
  function row(label, node) {
    var r = el('div', 'row'), dt = el('dt', null, label), dd = el('dd');
    dd.appendChild(node); r.appendChild(dt); r.appendChild(dd);
    return r;
  }
  // null = 미입력, [] = '없음'으로 입력, 그 외 = 항목 목록
  function listNode(v) {
    if (v === null || v === undefined) return el('span', 'empty', '미입력');
    if (!v.length) return el('span', 'empty none', '없음');
    var w = el('div', 'tags');
    v.forEach(function (s) { w.appendChild(el('span', 'tag', s)); });
    return w;
  }
  function render(d) {
    var p = d.patient;
    $('demoBanner').hidden = !d.demo;
    $('pName').textContent = p.name;
    var meta = $('pMeta'); meta.textContent = '';
    meta.appendChild(el('span', 'chip' + (p.age == null ? ' none' : ''), p.age == null ? '나이 미입력' : p.age + '세'));
    meta.appendChild(el('span', 'chip' + (!p.gender ? ' none' : ''), p.gender ? (p.gender==='남'?'남성':p.gender==='여'?'여성':p.gender) : '성별 미입력'));
    meta.appendChild(el('span', 'chip blood' + (!p.blood_type ? ' none' : ''), p.blood_type ? '혈액형 ' + p.blood_type : '혈액형 미입력'));
    var list = $('pList'); list.textContent = '';
    list.appendChild(row('기저질환', listNode(p.conditions)));
    list.appendChild(row('복용 중인 약', listNode(p.medications)));
    list.appendChild(row('알레르기', listNode(p.allergies)));
    var h = p.hospital, hn;
    if (!h || (!h.name && !h.department)) hn = el('span', 'empty', '미입력');
    else hn = el('span', null, [h.name, h.department].filter(Boolean).join(' · '));
    list.appendChild(row('기존 이용 병원', hn));
    $('fCreated').textContent = fmt(d.created_at);
    $('fExpires').textContent = fmt(d.expires_at);
    // 서버 시계 기준으로 남은 시간을 계산해 기기 시계 오차를 피한다
    expiresAt = Date.now() + (new Date(d.expires_at) - new Date(d.server_time));
    show('stOk');
    if (!scanned) {            // NFC 태그처럼 처음 한 번만 스캔 연출
      scanned = true;
      var b = document.body; b.classList.add('scan');
      setTimeout(function () { b.classList.remove('scan'); }, 1700);
    }
    stopTimers();
    var upd = function () {
      var s = Math.max(0, Math.round((expiresAt - Date.now()) / 1000));
      $('fLeft').textContent = s >= 60 ? '(' + Math.ceil(s / 60) + '분 남음)' : '(' + s + '초 남음)';
      if (s <= 0) load(true);
    };
    upd(); tick = setInterval(upd, 1000);
    poll = setInterval(function () { load(true); }, 20000);       // 폐기를 빠르게 반영
  }

  function load(quiet) {
    if (busy) return; busy = true;
    if (!quiet) show('stLoading');
    var h = { 'X-Share-Token': token, 'Accept': 'application/json' };
    var a = ss(AK); if (a) h['Authorization'] = 'Bearer ' + a;
    var timer = new Promise(function (_, rej) { setTimeout(function () { rej(new Error('timeout')); }, 15000); });
    Promise.race([
      fetch('/api/qr-share', { method: 'GET', headers: h, cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer' }),
      timer
    ]).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (j) { return { s: res.status, j: j }; });
    }).then(function (r) {
      busy = false;
      if (r.s === 200 && r.j.patient) return render(r.j);
      var e = r.j && r.j.error;
      if (r.s === 401) { stopTimers(); wipe(); ss(AK, null); show('stAuth'); $('authInput').focus(); return; }
      if (e === 'expired') return msg('⏱', '만료된 QR이에요', '유효 시간이 지나 환자 정보가 삭제되었어요. 환자(보호자)에게 새 QR을 요청해 주세요.');
      if (e === 'revoked') return msg('⛔', '폐기된 QR이에요', '환자 측에서 공유를 중단했어요. 새 QR이 필요하면 다시 요청해 주세요.');
      if (e === 'invalid_link' || r.s === 400 || r.s === 404) return msg('?', '잘못된 링크예요', 'QR을 다시 스캔하거나 새 QR을 요청해 주세요.');
      if (r.s === 429) return msg('…', '잠시 후 다시 시도해 주세요', '요청이 너무 많아요.', true);
      if (e === 'auth_not_configured') return msg('!', '서버 설정이 필요해요', '조회 권한 설정이 되어 있지 않아 정보를 표시할 수 없어요.');
      return msg('!', '불러오지 못했어요', '잠시 후 다시 시도해 주세요.', true);
    }).catch(function () {
      busy = false;
      if (quiet && !$('stOk').hidden) return;                    // 이미 표시 중이면 일시 오류로 화면을 지우지 않음
      msg('!', '연결할 수 없어요', '네트워크 상태를 확인하고 다시 시도해 주세요.', true);
    });
  }

  $('msgBtn').addEventListener('click', function () { load(false); });
  $('authForm').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var v = $('authInput').value.trim(); if (!v) return;
    ss(AK, v); $('authInput').value = ''; $('authErr').hidden = true;
    load(false);
  });
  document.addEventListener('visibilitychange', function () { if (!document.hidden && token && !$('stOk').hidden) load(true); });

  token = readToken();
  if (!token) msg('?', '잘못된 링크예요', 'QR을 다시 스캔하거나 새 QR을 요청해 주세요.');
  else load(false);
})();
