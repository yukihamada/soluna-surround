/* SOLUNA · SOUND — shared JA/EN switch.
 *
 * Default language is Japanese. The choice is remembered in localStorage
 * (soluna_lang) and can be forced per page with ?lang=ja|en, so it carries
 * across the audience page, FOH console, DJ page and the print/QP pages.
 *
 * Translation is dictionary based (Japanese source text -> English). It walks
 * text nodes and a few attributes, and re-applies on DOM mutations, so text
 * that JavaScript writes later is translated too. confirm()/prompt()/alert()
 * messages are translated as well. If a string is not in the dictionary it is
 * left untouched (never blanked), so nothing breaks when new Japanese copy is
 * added later.
 */
(function () {
  'use strict';

  var DICT = {
    /* ---- shared / brand ---- */
    '配信': 'LIVE',
    '接続': 'CONNECTED',
    '鳴っている': 'PLAYING',
    '準備OK': 'READY',
    '失敗': 'FAILED',
    '長さ': 'LEN',
    '合計': 'TOTAL',
    '幅': 'WIDTH',
    '周期(s)': 'PERIOD (s)',
    '距離 m': 'DIST m',
    '鳴': 'PLAY',
    '設定': 'SETUP',
    '消す': 'CLEAR',
    'ログ': 'LOG',
    '全ゾーン': 'ALL ZONES',

    /* ---- FOH console: top bar ---- */
    '🔇 全端末ミュート中 — ②のカードの UNMUTE で解除':
      '🔇 All devices muted — tap UNMUTE in card ② to restore',
    '📊 今夜の数字(STATS)': "📊 Tonight's numbers (STATS)",
    '🔑 管理トークンが未入力です。操作するには一番下の「8 設定」で入力してください。':
      '🔑 Admin token not entered. Fill it in under "8 SETUP" at the bottom to operate.',

    /* ---- 1 SHOW ---- */
    'ショーを進める': 'Run the show',
    'SHOW — セットリスト。各ステップ=曲+映像+ライトの束。夜はこのNEXTだけ押す':
      'SHOW — setlist. Each step = a bundle of song + video + light. On the night, just press NEXT.',
    '⏭ NEXT — 次のステップを一斉発火': '⏭ NEXT — fire the next step everywhere',
    '全端末・全Piスピーカーが同時に切り替わる': 'Every phone and Pi speaker switches at once',
    'セットリストを編集': 'Edit the setlist',
    '自動で次へ': 'Auto-advance',
    '(auto-advance — 曲が終わったら次のステップを自動発火)':
      '(auto-advance — fire the next step when the song ends)',
    'GAP 曲間 s': 'GAP between songs (s)',
    '曲の長さは追加時にブラウザで測って保存(':
      'Song length is measured in the browser and saved when added (',
    ')。長さが取れない/ループの曲では自動進行しない=手動NEXT。':
      '). If the length is unknown or the track loops, it will not auto-advance — press NEXT by hand.',
    '+ ステップ追加': '+ Add step',
    'APPLY — サーバへ保存': 'APPLY — save to server',
    '⏮ 先頭へ戻す': '⏮ Back to start',

    /* ---- 2 CUE ---- */
    '曲を鳴らす': 'Play a track',
    'CUE — 事前配布した音源を全端末で同時再生':
      'CUE — play a pre-distributed track on every phone at once',
    '音源を選ぶ': 'Pick a track',
    'PRELOAD(先に配る)': 'PRELOAD (hand out first)',
    'FIRE(一斉再生)': 'FIRE (play all together)',
    '読み込み中…(トークンを入れると一覧が出ます)':
      'Loading… (enter the token to see the list)',
    '⬆ 音源・映像をアップロード': '⬆ Upload audio / video',
    'mp3/aac/mp4/jpg — ブラウザから直接サーバへ届きます(コマンド不要)':
      'mp3/aac/mp4/jpg — uploads straight from the browser to the server (no commands)',
    'Audio URL(手入力可)': 'Audio URL (or type it)',
    '表示・映像(任意)': 'Display / video (optional)',
    '— NOW PLAYING の曲名/アーティスト、静止画、同期映像':
      '— NOW PLAYING title/artist, still image, synced video',
    'Title(端末に NOW PLAYING 表示)': 'Title (shown as NOW PLAYING on phones)',
    'Image URL(静止画=スポンサー/告知)': 'Image URL (still = sponsor / notice)',
    'Video URL(全端末+/screenで映像同期・音声併用時は映像側ミュート)':
      'Video URL (synced on all phones + /screen; video is muted when audio is used)',
    '📥 PRELOAD — 先に全端末へ配布(再生しない)':
      '📥 PRELOAD — distribute to all phones first (no playback)',
    '▶ FIRE — 全端末一斉再生': '▶ FIRE — play on every phone at once',
    'Lead秒後にサンプル一致で鳴り始める': 'Starts after Lead seconds, sample-accurate',
    '■ STOP — 再生を止める': '■ STOP — stop playback',
    'CUEもLIVEも即時無音・解除で位相そのまま復帰':
      'Mutes CUE and LIVE instantly; unmute restores the same phase',

    /* ---- 3 LIGHT ---- */
    '光らせる': 'Light it up',
    'LIGHT — 全端末の画面が同期カラーライトになる(帯域ゼロ)':
      'LIGHT — every phone screen becomes a synced color light (zero bandwidth)',
    'Zones(全ゾーン以外は選択したゾーンだけ点灯)':
      'Zones (unless ALL, only the selected zones light up)',
    '自動消灯(s)': 'Auto-off (s)',
    'LEDトーチ(iOS)': 'LED torch (iOS)',
    'ゾーン自動巡回': 'Zone auto-sweep',
    'OFF(手動)': 'OFF (manual)',
    'SWEEP(波が流れる)': 'SWEEP (a wave flows)',
    'CHASE(1ゾーン追いかけ)': 'CHASE (one zone chases)',
    'BOUNCE(往復)': 'BOUNCE (back and forth)',
    'RANDOM(ランダム)': 'RANDOM (random)',
    '— プリセット —': '— preset —',
    '現在の見た目を保存': 'Save the current look',
    '選択中のプリセットを削除': 'Delete the selected preset',
    'preview (端末同期の近似)': 'preview (approximate phone sync)',
    '💡 LIGHT GO — 全端末を光らせる': '💡 LIGHT GO — light up every phone',
    '■ DARK — 消灯': '■ DARK — lights off',

    /* ---- 4 ZONES & ALIGN ---- */
    'ゾーンと位相': 'Zones & alignment',
    'ZONES & ALIGN — ステージからの距離[m]→遅延を自動計算。🔊 でそのゾーンだけ試聴':
      'ZONES & ALIGN — distance from stage [m] → delay computed automatically. 🔊 auditions one zone',
    'APPLY ZONES — 全端末に配信': 'APPLY ZONES — push to all phones',
    '+ ゾーン': '+ Zone',
    'delay = 距離/343 + 15ms(Haas)。gain=ゾーン別音量補正(±12dB)。':
      'delay = distance/343 + 15ms (Haas). gain = per-zone level trim (±12dB).',
    'ハウスPAとの位相合わせ(全ゾーン一括トリム・即時反映)':
      'Align phase with the house PA (all-zone trim, applied live)',
    '手順: ハウスPAとグリッド両方でクリック/スネアを鳴らす → フラム(二度打ち感)が消えるまで ±5 → ±1ms で追い込む。+ = グリッドを遅らせる。':
      'How: play a click/snare on both the house PA and the grid → trim ±5 then ±1ms until the flam (double-hit) disappears. + = delay the grid.',
    'ステージ位置(GPS自動ゾーン)': 'Stage position (GPS auto-zone)',
    '— 登録すると来場者のゾーンが📍で自動選択される':
      '— once set, visitors get their zone auto-selected via 📍',
    '📍 現在地をステージに設定': '📍 Set current location as the stage',

    /* ---- 5 DEVICES ---- */
    '何台が本当に鳴っているか': 'How many are really playing',
    'DEVICES — 接続数ではなく端末の実状態。位置情報は受け取らない':
      'DEVICES — real device state, not just connection count. No location is received',
    '音が出ている': 'producing sound',
    'DL済・待機': 'downloaded · waiting',
    '接続のみ': 'connected only',
    'DL/デコード失敗': 'download/decode failed',
    '無音の可能性(ロック/サイレント)': 'maybe silent (lock / silent switch)',
    '充電案内': 'charge reminder',
    '旧client/未報告': 'old client / no report',

    /* ---- 6 NODES ---- */
    'Piスピーカー': 'Pi speaker',
    'Piスピーカー(箱)': 'Pi speaker (box)',
    'NODES — 5秒ごとに自己報告。ゾーン/位置/音量はここから割り当て=箱側は設定不要。⚠=15秒以上報告なし':
      'NODES — self-report every 5s. Assign zone/position/volume here = no setup on the box. ⚠ = no report for 15s+',
    'まだ箱からの報告がありません(Piを起動すると自動で並びます)':
      'No boxes reporting yet (start a Pi and they appear automatically)',

    /* ---- 7 STATE / STATS ---- */
    '控えと記録': 'Backup & records',
    'STATE / STATS — ホットスタンバイ用に控えを取る・予備機で IMPORT すれば端末は曲中復帰':
      'STATE / STATS — take a backup for hot standby; IMPORT on a spare and phones resume mid-song',
    '危険な操作': 'Dangerous actions',
    '— チャンネル削除': '— delete channel',
    'このチャンネルの保存状態を削除(接続中は不可)':
      'Delete this channel’s saved state (not while connected)',
    '✕ このチャンネルの保存状態を削除': '✕ Delete this channel’s saved state',

    /* ---- 8 SETUP ---- */
    'SETUP — トークン・チャンネル・印刷物・回線案内':
      'SETUP — token, channel, printouts, network guide',
    'この端末に記憶する': 'Remember on this device',
    'LOGOUT — トークンを消す': 'LOGOUT — clear the token',
    '🚩 ゾーン旗を印刷': '🚩 Print zone flags',
    '🎫 入場QRを印刷': '🎫 Print entrance QR',
    '⚙ この箱の設定': '⚙ This box’s setup',
    '🎧 観客ページ': '🎧 Audience page',
    '🖥 スクリーン': '🖥 Screen',
    '回線案内': 'Network guide',
    '— 前方ゾーンだけ会場Wi-Fi、それ以外はLTEのまま(無線のスケール解)':
      '— only front zones use venue Wi-Fi; the rest stay on LTE (the wireless scaling answer)',
    '会場Wi-Fi SSID(空=案内なし)': 'Venue Wi-Fi SSID (empty = no guide)',
    'Wi-Fi推奨ゾーン': 'Wi-Fi recommended zones',
    '回線案内を配信': 'Push network guide',
    '入場QRで音源を入口で先読み=開演時のDLゼロ。前方だけ会場Wi-Fi、後方はモバイル回線のままでOK。':
      'The entrance QR preloads the track at the door = zero download at showtime. Front zones on venue Wi-Fi, the rest on mobile data is fine.',

    /* ---- admin JS dynamic ---- */
    'AP: なし(サーバ箱は上流Wi-Fi/有線に乗っている、またはMac運用)':
      'AP: none (the server box is on upstream Wi-Fi/wired, or running on a Mac)',
    '音源がまだありません — まず mp3/aac を /api/upload か assets/ に置いてください →':
      'No tracks yet — first put an mp3/aac in /api/upload or assets/ →',
    '⚠ 先に「設定」で Admin token を入れてください':
      '⚠ Enter the Admin token under SETUP first',
    '全端末のキュー再生を停止します。OK?': 'Stop cue playback on all phones. OK?',
    '進行位置を先頭に戻します。OK?': 'Reset the progress to the start. OK?',
    'プリセット名(40文字以内)': 'Preset name (max 40 chars)',
    'プリセットを選択してください': 'Select a preset first',
    '全端末のライトを消灯します。OK?': 'Turn off the lights on all phones. OK?',
    '🔊 UNMUTE — ミュート解除': '🔊 UNMUTE — restore sound',
    '🔇 MUTE — 全端末を即時無音': '🔇 MUTE — silence all phones now',
    'このゾーンだけ試聴(ウォークテスト)': 'Audition this zone only (walk test)',

    /* ---- DJ ---- */
    'この端末が、会場全体の音源になる': 'This device becomes the sound of the whole floor',
    '音源': 'Source',
    'source — マイク/ライン入力か、この端末のファイル':
      'source — mic/line input, or a file on this device',
    '🎙 マイク / ライン': '🎙 Mic / Line',
    '🎵 ファイル': '🎵 File',
    '音源ファイル(この端末内)': 'Source file (on this device)',
    '入力デバイス(Mac: DJミキサーのUSB-IF等を選択)':
      'Input device (Mac: pick the DJ mixer’s USB-IF etc.)',
    '既定の入力': 'Default input',
    '接続先・チャンネル・トークン(通常は変えない)':
      'Server · channel · token (usually leave as is)',
    '空欄 = このホストに配信': 'empty = broadcast to this host',
    'TOKEN(本番サーバでは必須)': 'TOKEN (required on the production server)',
    '主催者から受け取る': 'get it from the organizer',
    '既存PAと融合(有線/低遅延 lead 0.08s) — 単体運用はOFFのまま':
      'Blend with the existing PA (wired / low-latency lead 0.08s) — leave OFF for standalone use',
    '▶ ON AIR — 配信をはじめる': '▶ ON AIR — start broadcasting',
    'start broadcasting to every phone and speaker':
      'start broadcasting to every phone and speaker',
    '■ OFF AIR — 配信を止める': '■ OFF AIR — stop broadcasting',
    '送信 frames': 'sent frames',
    'マイク入力はエコー/ノイズ処理を切った音楽向け設定。DJミキサーのREC OUTをイヤホン端子やUSB-IFでこの端末に入れれば、そのままフロア全体へ。聴く側は同じホストの':
      'Mic input uses music-friendly settings (echo/noise processing off). Feed the DJ mixer’s REC OUT into this device via the headphone jack or USB-IF and it goes straight to the whole floor. Listeners open',
    'を開く。': 'on the same host.',
    'ファイルを選んでください': 'Please choose a file',
    'このサーバは配信トークンが必要です。TOKEN欄に入力して(主催者から受け取る)、もう一度 ON AIR を押してください。':
      'This server requires a broadcast token. Enter it in the TOKEN field (get it from the organizer) and press ON AIR again.',
    '接続が切れました — 再接続します': 'Connection lost — reconnecting',

    /* ---- flags / print pages ---- */
    '入場': 'ENTRANCE',
    'いま読み取ると、中に入る前に音の準備が終わります。':
      'Scan now and your phone is ready before you go in.',
    'この旗の近くなら、ここを読み取って ▶ 。':
      'Near this flag? Scan here and tap ▶.',
    '音が出ない時: サイレントスイッチ OFF・音量アップ':
      'No sound? Silent switch off, volume up',
    'スマホでよみとる → あとは会場で ▶ を押すだけ · 位置情報は端末の中だけ':
      'Scan with your phone → then just tap ▶ inside · location stays on your device',
    '印刷は': 'Print from',
    'ゾーン旗': 'zone flags',
    '入場QR': 'entrance QR',
    'からA4で。印刷ダイアログで「背景のグラフィック」をON。':
      'on A4. Turn on "Background graphics" in the print dialog.',
    '印刷 / Print': 'Print',
    '🖨 印刷 / Print': '🖨 Print',
    '枚 · A4 縦 · 印刷ダイアログで「背景のグラフィック」をON':
      'sheets · A4 portrait · turn on "Background graphics" in the print dialog',
  };

  var LANG_KEY = 'soluna_lang';

  function norm(s) {
    return (s || '').replace(/\s+/g, ' ').trim();
  }

  function currentLang() {
    try {
      var q = new URLSearchParams(location.search).get('lang');
      if (q === 'ja' || q === 'en') return q;
    } catch (e) {}
    var saved = null;
    try { saved = localStorage.getItem(LANG_KEY); } catch (e) {}
    if (saved === 'ja' || saved === 'en') return saved;
    return 'ja';
  }

  function translateString(s) {
    var key = norm(s);
    if (!key) return s;
    var hit = DICT[key];
    if (!hit) return s;
    // keep the original leading/trailing whitespace so layout is unchanged
    var lead = s.match(/^\s*/)[0];
    var trail = s.match(/\s*$/)[0];
    return lead + hit + trail;
  }

  var ATTRS = ['placeholder', 'title', 'aria-label', 'alt'];

  function translateNode(node) {
    if (node.nodeType === 3) { // text
      var parent = node.parentNode;
      if (parent && /^(SCRIPT|STYLE|TEXTAREA)$/.test(parent.nodeName)) return;
      var t = translateString(node.nodeValue);
      if (t !== node.nodeValue) node.nodeValue = t;
      return;
    }
    if (node.nodeType !== 1) return;
    if (/^(SCRIPT|STYLE)$/.test(node.nodeName)) return;
    for (var i = 0; i < ATTRS.length; i++) {
      var a = ATTRS[i];
      if (node.hasAttribute && node.hasAttribute(a)) {
        var v = node.getAttribute(a);
        var nv = translateString(v);
        if (nv !== v) node.setAttribute(a, nv);
      }
    }
  }

  function walk(root) {
    if (!root) return;
    translateNode(root);
    var kids = root.childNodes || [];
    for (var i = 0; i < kids.length; i++) walk(kids[i]);
  }

  var observer = null;
  var lastLang = null;

  function applyLang(lang) {
    lang = lang === 'en' ? 'en' : 'ja';
    lastLang = lang;
    document.documentElement.lang = lang;
    try { localStorage.setItem(LANG_KEY, lang); } catch (e) {}
    if (observer) { observer.disconnect(); observer = null; }
    if (lang === 'en') {
      walk(document.body);
      observer = new MutationObserver(function (muts) {
        for (var i = 0; i < muts.length; i++) {
          var m = muts[i];
          if (m.type === 'characterData') translateNode(m.target);
          else if (m.type === 'attributes') translateNode(m.target);
          else for (var j = 0; j < m.addedNodes.length; j++) walk(m.addedNodes[j]);
        }
      });
      observer.observe(document.body, {
        childList: true, subtree: true, characterData: true,
        attributes: true, attributeFilter: ATTRS
      });
    }
    updateToggle();
  }

  function makeToggle() {
    if (document.getElementById('langToggle')) return;
    var b = document.createElement('button');
    b.id = 'langToggle';
    b.type = 'button';
    b.setAttribute('aria-label', 'Language / 言語');
    b.style.cssText = 'position:fixed;right:14px;bottom:14px;z-index:2147483000;' +
      'display:inline-flex;align-items:center;gap:6px;padding:9px 14px;border-radius:999px;' +
      'border:1px solid rgba(212,175,55,.5);background:rgba(20,12,16,.82);color:#f0d47a;' +
      'font:600 13px/1 "IBM Plex Mono",ui-monospace,monospace;letter-spacing:.06em;' +
      'cursor:pointer;backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);' +
      'box-shadow:0 6px 20px rgba(0,0,0,.35)';
    b.onclick = function () {
      applyLang(lastLang === 'en' ? 'ja' : 'en');
    };
    var st = document.createElement('style');
    st.textContent = '@media print{#langToggle{display:none!important}}';
    document.head.appendChild(st);
    document.body.appendChild(b);
  }

  function updateToggle() {
    var b = document.getElementById('langToggle');
    if (!b) return;
    b.textContent = lastLang === 'en' ? '🌐 日本語' : '🌐 English';
  }

  // Translate JS dialogs too (confirm/prompt/alert messages).
  ['confirm', 'prompt', 'alert'].forEach(function (fn) {
    var orig = window[fn];
    if (typeof orig !== 'function') return;
    window[fn] = function (msg) {
      var args = Array.prototype.slice.call(arguments);
      if (typeof msg === 'string') args[0] = translateString(msg);
      return orig.apply(window, args);
    };
  });

  // Expose for code that builds strings dynamically.
  window.SOLUNA_t = translateString;
  window.SOLUNA_lang = currentLang;
  window.SOLUNA_setLang = applyLang;

  function boot() {
    makeToggle();
    applyLang(currentLang());
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
