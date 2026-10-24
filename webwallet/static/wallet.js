/* Netlithic web wallet UI. The secret key is generated here, encrypted with the
 * user's password (PBKDF2-SHA256 600k + AES-GCM) and stored only in this
 * browser's localStorage. It is never sent anywhere. Only signed transfers,
 * addresses and faucet requests go to this page's own server (/api/...).
 * All text is written with textContent (never raw HTML). */
(function () {
  "use strict";
  var C = window.NetlithicCore;
  var STORE = "netlithic.wallet.v1";
  var ITER = 600000;
  var tracked = {}, autoTimer = null, key = null, address = null, info = null, pending = null, freshBlob = null;

  // ---- "Stay unlocked for 15 minutes" (opt-in, this tab only) -------------
  // sessionStorage (this tab only, gone when the tab closes) holds the seed
  // ENCRYPTED with a random AES-GCM key. That key is created non-extractable and
  // kept in IndexedDB, so its raw bytes can't be read back out by script, and
  // neither storage ever holds the seed in plain text. Lock, "Remove wallet",
  // or 15 minutes without activity wipe both.
  var SESSION = "netlithic.stay.v1", IDB = "netlithic-stay", IDB_STORE = "keys";
  var stayTimer = null, lastBump = 0;
  function idb() {
    return new Promise(function (ok, bad) {
      var r = indexedDB.open(IDB, 1);
      r.onupgradeneeded = function () { r.result.createObjectStore(IDB_STORE); };
      r.onsuccess = function () { ok(r.result); }; r.onerror = function () { bad(r.error); };
    });
  }
  async function idbDo(mode, fn) {
    var db = await idb();
    try {
      return await new Promise(function (ok, bad) {
        var tx = db.transaction(IDB_STORE, mode), req = fn(tx.objectStore(IDB_STORE));
        tx.oncomplete = function () { ok(req && req.result); }; tx.onerror = function () { bad(tx.error); };
      });
    } finally { db.close(); }
  }
  function readStay() { try { return JSON.parse(sessionStorage.getItem(SESSION)); } catch (e) { return null; } }
  async function clearStay() {
    var rec = readStay();
    try { sessionStorage.removeItem(SESSION); } catch (e) { /* ignore */ }
    if (stayTimer) { clearInterval(stayTimer); stayTimer = null; }
    paintStay(null);
    // Remove this tab's key, plus any left behind by tabs that were closed.
    try { await idbDo("readwrite", function (st) { return st.clear(); }); } catch (e) { /* ignore */ }
    return rec;
  }
  async function startStay(seed, addr) {
    var k = await crypto.subtle.generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
    var id = C.hex(rand(16)), iv = rand(12);
    var ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv: iv, additionalData: new TextEncoder().encode(addr) },
      k, new TextEncoder().encode(seed));
    await idbDo("readwrite", function (st) { return st.put(k, id); });
    var rec = { id: id, iv: C.hex(iv), ct: C.hex(new Uint8Array(ct)), address: addr, expires: C.stayUntil(Date.now()) };
    sessionStorage.setItem(SESSION, JSON.stringify(rec));
    watchStay(); paintStay(rec);
  }
  async function resumeStay() {
    var rec = readStay();
    if (!C.staySessionValid(rec, Date.now())) { if (rec) await clearStay(); return null; }
    var k = await idbDo("readonly", function (st) { return st.get(rec.id); });
    if (!k) { await clearStay(); return null; }
    try {
      var pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: C.unhex(rec.iv), additionalData: new TextEncoder().encode(rec.address) },
        k, C.unhex(rec.ct));
      var seed = new TextDecoder().decode(pt);
      if (!/^[0-9a-f]{64}$/.test(seed)) throw new Error("bad");
      bumpStay(true); watchStay();
      return seed;
    } catch (e) { await clearStay(); return null; }
  }
  // Activity slides the end time forward, at most 15 minutes from now.
  function bumpStay(force) {
    var rec = readStay(); if (!rec) return;
    var now = Date.now();
    if (!force && now - lastBump < 30000) return;
    if (!C.staySessionValid(rec, now)) { lockNow(); return; }
    lastBump = now; rec.expires = C.stayUntil(now);
    sessionStorage.setItem(SESSION, JSON.stringify(rec)); paintStay(rec);
  }
  function watchStay() {
    if (stayTimer) return;
    stayTimer = setInterval(function () { if (!C.staySessionValid(readStay(), Date.now())) lockNow(); }, 10000);
  }
  function paintStay(rec) {
    var box = $("h-stay"); if (!box) return;
    box.hidden = !rec;
    $("h-stay-text").textContent = rec ? C.stayLine(rec.expires) + " " : "";
  }
  function lockNow() { clearStay(); say(""); key = null; address = null; tracked = {}; activity = []; waiting = []; show("v-unlock"); }

  function $(id) { return document.getElementById(id); }
  // ---- In-wallet Back/Forward (History API). Each view or step is one history entry. ----
  var VIEWS = ["v-start", "v-create", "v-backup", "v-save", "v-restore", "v-unlock", "v-home"];
  var curView = null, curSub = null, navReady = false, navQuiet = false, leaveArmed = false;
  function navState() { return { nl: 1, v: curView, sub: curSub }; }
  function navPush(replace) {
    if (!navReady || navQuiet) return;
    leaveArmed = false;
    try { history[replace ? "replaceState" : "pushState"](navState(), ""); } catch (e) { /* ignore */ }
  }
  function show(view, replace) {
    VIEWS.forEach(function (v) { $(v).hidden = v !== view; });
    var changed = view !== curView || curSub !== null;
    curView = view; curSub = null;
    if (changed) navPush(replace);
  }
  function openSub(sub) { if (sub !== curSub) { curSub = sub; navPush(false); } }
  // Close a step the user opened: step back through history so Back/Forward stay in order.
  function closeSub(sub) {
    if (curSub === sub && history.state && history.state.nl && history.state.sub === sub) { history.back(); return; }
    curSub = null; applySub(); navPush(true);
  }
  function helpBoxes() { return Array.prototype.slice.call(document.querySelectorAll("details")); }
  function applySub() {
    if (curSub !== "review") { pending = null; $("s-confirm").hidden = true; }
    else if (!pending) { curSub = null; $("s-confirm").hidden = true; }
    var qr = curSub === "qr" && curView === "v-home" && !!address;
    if (qr) drawQr(shareLink());
    $("h-qr-box").hidden = !qr; $("b-qr").textContent = qr ? "Hide QR code" : "Show QR code";
    helpBoxes().forEach(function (d, i) { d.open = curSub === "help:" + i; });
  }
  function allowedView(v) {
    if (VIEWS.indexOf(v) < 0) v = null;
    if (key) return v === "v-home" || v === "v-save" ? v : "v-home";
    if (v === "v-home" || v === "v-save" || !v) return localStorage.getItem(STORE) ? "v-unlock" : "v-start";
    if (v === "v-unlock" && !localStorage.getItem(STORE)) return "v-start";
    return v;
  }
  function onPop(ev) {
    var s = ev.state;
    if (!s || !s.nl) return;
    if (s.base) {
      if (leaveArmed) { history.back(); return; }
      try { history.pushState(navState(), ""); } catch (e) { /* ignore */ }
      leaveArmed = true;
      say("You're at the start of the wallet. Press Back again to leave this page.");
      return;
    }
    leaveArmed = false;
    var v = allowedView(s.v);
    navQuiet = true;
    VIEWS.forEach(function (x) { $(x).hidden = x !== v; });
    curView = v; curSub = v === s.v ? (s.sub || null) : null;
    applySub();
    navQuiet = false;
    if (v !== s.v || curSub !== (s.sub || null)) { try { history.replaceState(navState(), ""); } catch (e) { /* ignore */ } }
  }
  function startNav() {
    if (navReady) return;
    try {
      history.replaceState({ nl: 1, base: true }, "");
      history.pushState(navState(), "");
      navReady = true;
      window.addEventListener("popstate", onPop);
      helpBoxes().forEach(function (d, i) {
        d.addEventListener("toggle", function () {
          var me = "help:" + i;
          if (d.open && curSub !== me) openSub(me);
          else if (!d.open && curSub === me) closeSub(me);
        });
      });
    } catch (e) { /* history not available: views still work, Back just leaves */ }
  }
  function say(text, isErr) {
    var m = $("msg"); m.textContent = text; m.hidden = !text; m.className = isErr ? "msg err" : "msg";
  }
  function rand(n) { return crypto.getRandomValues(new Uint8Array(n)); }

  async function deriveKey(password, salt) {
    var base = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey({ name: "PBKDF2", hash: "SHA-256", salt: salt, iterations: ITER },
      base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
  }
  async function encryptSeed(seedHex, password, addr) {
    var salt = rand(16), iv = rand(12);
    var k = await deriveKey(password, salt);
    var ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv: iv, additionalData: new TextEncoder().encode(addr) },
      k, new TextEncoder().encode(seedHex));
    return { format: "netlithic-wallet-backup", version: 1, address: addr, kdf: "PBKDF2-SHA256",
      iterations: ITER, salt: C.hex(salt), iv: C.hex(iv), ciphertext: C.hex(new Uint8Array(ct)) };
  }
  async function decryptSeed(blob, password) {
    if (!blob || blob.format !== "netlithic-wallet-backup" || blob.version !== 1) throw new Error("That isn't a Netlithic backup file.");
    var iter = blob.iterations;
    if (!Number.isInteger(iter) || iter < 100000 || iter > 10000000) throw new Error("That backup file looks damaged.");
    var base = await crypto.subtle.importKey("raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveKey"]);
    var k = await crypto.subtle.deriveKey({ name: "PBKDF2", hash: "SHA-256", salt: C.unhex(blob.salt), iterations: iter },
      base, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
    var pt;
    try {
      pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: C.unhex(blob.iv), additionalData: new TextEncoder().encode(String(blob.address)) },
        k, C.unhex(blob.ciphertext));
    } catch (e) { throw new Error("Wrong password."); }
    var seed = new TextDecoder().decode(pt);
    if (!/^[0-9a-f]{64}$/.test(seed)) throw new Error("That backup file looks damaged.");
    return seed;
  }

  async function api(path, body) {
    var opts = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
    var r = await fetch("/api/" + path, opts);
    var data = {};
    try { data = await r.json(); } catch (e) { /* ignore */ }
    if (!r.ok) throw new Error(typeof data.error === "string" ? data.error : "The network didn't answer. Try again in a minute.");
    return data;
  }

  // ---- share links: /send?to=<address> only pre-fills the recipient, never sends.
  var ADDR_RE = /^[0-9a-f]{32}$/;
  var shareTo = (function () {
    try {
      if (location.pathname !== "/send") return null;
      var to = String(new URLSearchParams(location.search).get("to") || "").trim().toLowerCase();
      return ADDR_RE.test(to) ? to : null;
    } catch (e) { return null; }
  })();
  function shareBase() {
    var b = info && typeof info.share_base === "string" ? info.share_base : "";
    if (/^https:\/\/[A-Za-z0-9.-]+(:[0-9]{1,5})?(\/[A-Za-z0-9._~\/-]*)?$/.test(b)) return b.replace(/\/+$/, "");
    return location.origin;
  }
  function shareLink() { return shareBase() + "/send?to=" + address; }
  function shareText() { return "Send me test NLT on Netlithic (test coins have no value): " + address; }
  function applyShareTo() {
    if (!shareTo) return;
    if (shareTo === address) { say("That share link is your own address, so there's nothing to fill in."); }
    else { $("s-to").value = shareTo; $("s-amount").value = ""; $("s-prefill").hidden = false; }
    shareTo = null;
    try { history.replaceState(history.state, "", "/"); } catch (e) {}
  }
  function drawQr(text) {
    if (typeof window.qrcode !== "function") throw new Error("QR code isn't available.");
    var q = window.qrcode(0, "M"); q.addData(text); q.make();
    var n = q.getModuleCount(), cv = $("h-qr"), quiet = 4, cell = Math.max(2, Math.floor(232 / (n + quiet * 2)));
    cv.width = cv.height = cell * (n + quiet * 2);
    var g = cv.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, cv.width, cv.height); g.fillStyle = "#000";
    for (var r = 0; r < n; r++) for (var c = 0; c < n; c++) if (q.isDark(r, c)) g.fillRect((c + quiet) * cell, (r + quiet) * cell, cell, cell);
  }

  // ---- Backup check. "Confirmed" means a backup file or code was opened here and
  // gave back THIS address. Only that flag (no key) is stored.
  var CONFIRM = "netlithic.backup.confirmed.v1.";
  function backupConfirmed(addr) { try { return localStorage.getItem(CONFIRM + addr) === "yes"; } catch (e) { return false; } }
  function paintNag() { $("h-backup-nag").hidden = !address || backupConfirmed(address); }
  async function checkBackup() {
    var res = $("k-result"), file = $("k-file").files[0], code = $("k-code").value.trim(), seed;
    res.hidden = true;
    if (file) {
      if (file.size > 10000) throw new Error("That isn't a Netlithic backup file.");
      var blob; try { blob = JSON.parse(await file.text()); } catch (e) { throw new Error("That isn't a Netlithic backup file."); }
      seed = await decryptSeed(blob, $("k-pass").value);
    } else if (code) {
      seed = await C.seedFromBackupCode(code);
    } else throw new Error("Pick your backup file (and type its password), or type your backup code.");
    var got = await C.addressOf(C.keyFromSeed(seed).pubkey);
    seed = null; $("k-pass").value = $("k-code").value = ""; $("k-file").value = "";
    res.hidden = false;
    if (got !== address) {
      res.className = "small bad";
      res.textContent = "That backup belongs to a different wallet (" + C.shortAddr(got) + "), not this one. Save a new backup file in Step 1 and try again.";
      return;
    }
    localStorage.setItem(CONFIRM + address, "yes");
    res.className = "small ok";
    res.textContent = "It works! This backup brings back your wallet. Keep it safe.";
    paintNag();
  }
  async function printBackup(seed) {
    $("p-address").textContent = address;
    $("p-code").textContent = await C.backupCode(seed);
    $("p-date").textContent = "Made on " + new Date().toLocaleDateString();
    var wipe = function () { $("p-code").textContent = ""; window.removeEventListener("afterprint", wipe); };
    window.addEventListener("afterprint", wipe);
    window.print();
    setTimeout(wipe, 60000);
  }

  // ---- Contacts (this device only) ----------------------------------------
  var CONTACTS = "netlithic.contacts.v1";
  function contacts() { try { return C.parseContacts(localStorage.getItem(CONTACTS)); } catch (e) { return []; } }
  function nameOf(a) { return C.contactName(contacts(), a); }
  function paintContacts() {
    var list = contacts(), ul = $("h-contacts"), sel = $("s-contact");
    ul.textContent = "";
    while (sel.options.length > 1) sel.remove(1);
    list.forEach(function (c) {
      var li = document.createElement("li"), who = document.createElement("span"), ad = document.createElement("span");
      who.className = "who"; who.textContent = c.name; ad.className = "addr"; ad.textContent = c.address; who.appendChild(ad);
      var send = document.createElement("button"); send.className = "plain"; send.textContent = "Send";
      send.addEventListener("click", function () { $("s-to").value = c.address; $("s-amount").focus(); });
      var del = document.createElement("button"); del.className = "plain danger"; del.textContent = "Remove";
      del.addEventListener("click", function () {
        if (!confirm("Remove " + c.name + " from your contacts?")) return;
        localStorage.setItem(CONTACTS, JSON.stringify(C.removeContact(contacts(), c.address))); paintContacts(); paintActivity();
      });
      li.appendChild(who); li.appendChild(send); li.appendChild(del); ul.appendChild(li);
      var o = document.createElement("option"); o.value = c.address; o.textContent = c.name + " (" + C.shortAddr(c.address) + ")"; sel.appendChild(o);
    });
    $("s-contact-row").hidden = list.length === 0;
  }

  // ---- Activity history ---------------------------------------------------
  var activity = [], waiting = [], nextOffset = null, seenAct = {}, firstLook = true;
  async function loadActivity(more) {
    var off = more ? nextOffset : 0;
    if (more && off == null) return;
    var h = await api("history?address=" + encodeURIComponent(address) + "&offset=" + off + "&limit=20");
    var items = Array.isArray(h.items) ? h.items : [];
    if (more) activity = activity.concat(items);
    else {
      // keep pages already loaded with "Show older"; refresh only the newest page
      var keep = activity.slice(20);
      activity = items.concat(keep.filter(function (k) { return !items.some(function (i) { return C.activityKey(i) === C.activityKey(k); }); }));
      waiting = (Array.isArray(h.pending) ? h.pending : []).map(function (p) { p.pending = true; return p; });
      var fresh = C.newArrivals(seenAct, items, firstLook); firstLook = false;
      fresh.forEach(function (it) { notify(C.notifyText(it, nameOf)); });
    }
    if (!more || nextOffset == null || h.next_offset == null || h.next_offset > nextOffset) nextOffset = h.next_offset;
    paintActivity();
  }
  function paintActivity() {
    var ul = $("h-activity"); ul.textContent = "";
    waiting.concat(activity).forEach(function (it) {
      var li = document.createElement("li");
      li.className = it.pending ? "wait" : (it.kind === "sent" ? "out" : (it.kind === "reward_share" || it.kind === "mined" ? "reward" : "in"));
      var dot = document.createElement("span"); dot.className = "dot";
      var txt = document.createElement("span"); txt.textContent = C.describeActivity(it, nameOf);
      var when = document.createElement("span"); when.className = "when";
      when.textContent = it.pending ? "On the way" : C.whenText(it.timestamp, Date.now());
      li.appendChild(dot); li.appendChild(txt); li.appendChild(when); ul.appendChild(li);
    });
    $("h-activity-empty").hidden = activity.length + waiting.length > 0;
    $("b-more").hidden = nextOffset == null;
  }

  // ---- Notifications (opt-in, off by default) -----------------------------
  var NOTIFY = "netlithic.notify.v1";
  function notifyOn() { try { return localStorage.getItem(NOTIFY) === "on"; } catch (e) { return false; } }
  function paintNotify() {
    var on = notifyOn();
    $("b-notify").textContent = on ? "Turn notifications off" : "Tell me when coins arrive";
    $("h-notify-text").textContent = on
      ? "On: you'll get a heads-up when test coins arrive or a reward share lands (while the wallet is open)."
      : "Want a heads-up when test coins arrive or a reward share lands? This is off unless you turn it on.";
  }
  function toast(text) {
    var t = $("toast"); t.textContent = text; t.hidden = false;
    clearTimeout(toast.timer); toast.timer = setTimeout(function () { t.hidden = true; }, 8000);
  }
  function notify(text) {
    if (!notifyOn()) return;
    // Desktop app: native notification if the computer allows it, else in-app.
    if (typeof window.NetlithicNotify === "function") {
      Promise.resolve(window.NetlithicNotify("Netlithic (test network)", text)).then(function (ok) { if (!ok) toast(text); }, function () { toast(text); });
      return;
    }
    if (window.Notification && Notification.permission === "granted" && document.hidden) {
      try { new Notification("Netlithic (test network)", { body: text, icon: "/logo.png" }); return; } catch (e) { /* fall through */ }
    }
    toast(text);
  }
  async function toggleNotify() {
    if (notifyOn()) { localStorage.removeItem(NOTIFY); paintNotify(); say("Notifications are off."); return; }
    if (!confirm("Turn on notifications? You'll get a short heads-up when test coins arrive or a reward share lands. You can turn them off here any time.")) return;
    if (typeof window.NetlithicNotify !== "function" && window.Notification && Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch (e) { /* ignore */ }
    }
    localStorage.setItem(NOTIFY, "on"); paintNotify();
    var blocked = typeof window.NetlithicNotify !== "function" && (!window.Notification || Notification.permission !== "granted");
    say(blocked ? "Notifications are on. Your browser didn't allow pop-ups, so they'll show inside this page." : "Notifications are on.");
  }

  async function openWallet(seed) {
    key = C.keyFromSeed(seed);
    address = await C.addressOf(key.pubkey);
    $("h-address").textContent = address;
    show("v-home");
    applyShareTo();
    activity = []; waiting = []; nextOffset = null; seenAct = {}; firstLook = true;
    paintNag(); paintContacts(); paintNotify(); paintActivity();
    refresh();
    // Keep the balance live: re-check every 15 seconds while the wallet is open.
    if (!autoTimer) autoTimer = setInterval(function () {
      if (address && !$("v-home").hidden && !document.hidden) refresh(true);
    }, 15000);
  }
  // Explorer links: only same-site paths (the server refuses anything else).
  function setLink(id, href) {
    var a = $(id);
    if (href && /^\/[A-Za-z0-9._~\/%-]*$/.test(href)) { a.href = href; a.hidden = false; }
    else { a.removeAttribute("href"); a.hidden = true; }
  }
  async function refresh(quiet) {
    try {
      info = await api("info");
      var acct = await api("account?address=" + encodeURIComponent(address));
      $("h-balance").textContent = C.fromBaseUnits(acct.balance);
      $("h-points").textContent = acct.points;
      tracked = C.trackTransfers(tracked, acct, Date.now());
      paintMoving();
      var p = acct.payouts || {};
      $("h-pts-epoch").textContent = String(p.points_this_epoch || "0");
      $("h-earned").textContent = String(p.earned_nlt || "0");
      $("h-vesting").textContent = String(p.vesting_nlt || "0");
      $("h-claimable").textContent = String(p.claimable_nlt || "0");
      $("h-next-payout").textContent = Number.isInteger(p.blocks_until_next_payout)
        ? "Next reward share in " + p.blocks_until_next_payout + " blocks." : "";
      setLink("h-history", info.explorer ? info.explorer + "/address/" + encodeURIComponent(address) : null);
    } catch (e) { if (!quiet) say(e.message, true); }
    try { await loadActivity(false); } catch (e) { /* history is optional; the balance above still works */ }
  }
  function paintMoving() {
    var ids = Object.keys(tracked).sort(function (a, b) {
      var x = tracked[a], y = tracked[b];
      if (x.state !== y.state) return x.state === "pending" ? -1 : 1;
      return y.since - x.since;
    });
    var ul = $("h-moving"); ul.textContent = "";
    ids.forEach(function (id) {
      var t = tracked[id], li = document.createElement("li");
      li.className = t.state === "pending" ? "pending " + t.dir : "arrived " + t.dir;
      var dot = document.createElement("span"); dot.className = "dot";
      var txt = document.createElement("span"); txt.textContent = C.describeTransfer(t);
      var tag = document.createElement("span"); tag.className = "tag";
      tag.textContent = t.state === "pending" ? "On the way" : "Arrived";
      li.appendChild(dot); li.appendChild(txt); li.appendChild(tag); ul.appendChild(li);
    });
    $("h-moving-card").hidden = ids.length === 0;
    var tot = C.pendingTotals(tracked), parts = [];
    if (tot.in) parts.push("+" + C.fromBaseUnits(tot.in));
    if (tot.out) parts.push("−" + C.fromBaseUnits(tot.out));
    var pt = $("h-pending-total");
    pt.textContent = parts.length ? "Pending: " + parts.join(" / ") + " test NLT (not in your balance yet)" : "";
    pt.hidden = !parts.length;
  }
  function download(blob) {
    var name = "netlithic-wallet-backup-" + blob.address.slice(0, 8) + ".json";
    // Desktop app: the window can't do browser downloads, so it provides a native
    // Save dialog. Only the already-encrypted backup is handed over, never the key.
    if (typeof window.NetlithicSaveBackup === "function") {
      return Promise.resolve(window.NetlithicSaveBackup(JSON.stringify(blob, null, 2), name)).then(function (r) {
        if (r && r.ok) say("Backup file saved: " + String(r.path || name));
        else if (r && r.error) say(String(r.error), true);
      });
    }
    var a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(blob, null, 2)], { type: "application/json" }));
    a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
    return Promise.resolve();
  }
  function copyText(text) {
    if (typeof window.NetlithicCopy === "function") return Promise.resolve(window.NetlithicCopy(text));
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
    return Promise.reject(new Error("no clipboard"));
  }
  function checkPass(p, p2) {
    if (p.length < 10) throw new Error("Please use a password of at least 10 characters.");
    if (p2 !== undefined && p !== p2) throw new Error("The two passwords don't match.");
  }
  function guard(fn) {
    return async function () {
      var btn = this; if (btn) btn.disabled = true;
      say("");
      try { await fn(); } catch (e) { say(e.message || "Something went wrong.", true); }
      finally { if (btn && btn.id !== "b-backup-done") btn.disabled = false; }
    };
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".b-back").forEach(function (b) { b.addEventListener("click", function () { say(""); show("v-start"); }); });
    $("b-create").addEventListener("click", function () { show("v-create"); });
    $("b-show-restore").addEventListener("click", function () { show("v-restore"); });

    $("b-create-go").addEventListener("click", guard(async function () {
      var p = $("c-pass").value; checkPass(p, $("c-pass2").value);
      var seed = C.newSeed();
      var k = C.keyFromSeed(seed), addr = await C.addressOf(k.pubkey);
      freshBlob = await encryptSeed(seed, p, addr);
      localStorage.setItem(STORE, JSON.stringify(freshBlob));
      $("c-pass").value = $("c-pass2").value = "";
      $("backup-code").textContent = await C.backupCode(seed);
      $("c-wrote").checked = false; $("b-backup-done").disabled = true;
      key = k; address = addr;
      show("v-backup");
    }));
    $("c-wrote").addEventListener("change", function () { $("b-backup-done").disabled = !this.checked; });
    $("b-download").addEventListener("click", guard(async function () { if (freshBlob) await download(freshBlob); }));
    $("b-print-new").addEventListener("click", guard(async function () { if (key) await printBackup(key.seed); }));
    $("b-backup-start").addEventListener("click", function () { say(""); $("k-result").hidden = true; show("v-save"); });
    $("b-backup-steps").addEventListener("click", function () { say(""); $("k-result").hidden = true; show("v-save"); });
    $("b-save-file").addEventListener("click", guard(async function () {
      var raw = localStorage.getItem(STORE); if (raw) await download(JSON.parse(raw));
    }));
    $("b-save-print").addEventListener("click", guard(async function () { if (key) await printBackup(key.seed); }));
    $("b-check").addEventListener("click", guard(checkBackup));
    $("b-save-close").addEventListener("click", function () { say(""); show("v-home"); paintNag(); });
    $("b-more").addEventListener("click", guard(async function () { await loadActivity(true); }));
    $("s-contact").addEventListener("change", function () { if (this.value) { $("s-to").value = this.value; $("s-prefill").hidden = true; } this.value = ""; });
    $("b-contact-add").addEventListener("click", guard(async function () {
      var a = $("n-addr").value.trim().toLowerCase();
      localStorage.setItem(CONTACTS, JSON.stringify(C.addContact(contacts(), $("n-name").value, a)));
      $("n-name").value = $("n-addr").value = ""; paintContacts(); paintActivity();
      say("Contact saved on this device.");
    }));
    $("b-prefill-save").addEventListener("click", function () {
      $("n-addr").value = $("s-to").value.trim().toLowerCase(); $("n-name").focus();
      $("h-contacts-card").scrollIntoView({ block: "center" });
      say("Type a name for this address, then press Save contact.");
    });
    $("b-notify").addEventListener("click", guard(toggleNotify));
    $("b-backup-done").addEventListener("click", function () {
      $("backup-code").textContent = ""; freshBlob = null; openWallet(key.seed);
    });

    $("b-restore-go").addEventListener("click", guard(async function () {
      var p = $("r-pass").value, code = $("r-code").value.trim(), file = $("r-file").files[0], seed;
      if (!p) throw new Error("Please enter a password.");
      if (code) { checkPass(p); seed = await C.seedFromBackupCode(code); }
      else if (file) {
        if (file.size > 10000) throw new Error("That isn't a Netlithic backup file.");
        var blob; try { blob = JSON.parse(await file.text()); } catch (e) { throw new Error("That isn't a Netlithic backup file."); }
        seed = await decryptSeed(blob, p);
      } else throw new Error("Type your backup code or pick your backup file.");
      var k = C.keyFromSeed(seed), addr = await C.addressOf(k.pubkey);
      localStorage.setItem(STORE, JSON.stringify(await encryptSeed(seed, p, addr)));
      $("r-pass").value = $("r-code").value = "";
      localStorage.setItem(CONFIRM + addr, "yes");  // the backup just worked, so it counts as checked
      await openWallet(seed);
      say("Welcome back! Your wallet is ready.");
    }));

    $("b-unlock").addEventListener("click", guard(async function () {
      var blob = JSON.parse(localStorage.getItem(STORE));
      var seed = await decryptSeed(blob, $("u-pass").value);
      var stay = $("u-stay") && $("u-stay").checked;
      $("u-pass").value = ""; if ($("u-stay")) $("u-stay").checked = false;
      await clearStay();
      if (stay) {
        try { await startStay(seed, String(blob.address)); }
        catch (e) { await clearStay(); say("Couldn't keep it unlocked in this browser, so it will lock on refresh.", true); }
      }
      await openWallet(seed);
    }));
    $("b-forget").addEventListener("click", function () {
      if (confirm("Remove this wallet from this device? You can only get it back with your backup code or backup file.")) {
        clearStay(); localStorage.removeItem(STORE); key = null; address = null; tracked = {}; show("v-start");
      }
    });
    $("b-lock").addEventListener("click", lockNow);
    if ($("b-lock-now")) $("b-lock-now").addEventListener("click", lockNow);
    ["click", "keydown"].forEach(function (ev) { document.addEventListener(ev, function () { if (key) bumpStay(false); }, true); });
    $("b-refresh").addEventListener("click", guard(refresh));
    $("b-copy").addEventListener("click", function () {
      copyText(address).then(function (r) { if (r === false) throw new Error("copy failed"); }).then(function () { say("Address copied."); }, function () { say("Couldn't copy. Select the address and copy it by hand.", true); });
    });
    $("b-share").addEventListener("click", guard(async function () {
      if (!address) return;
      if (!info) { try { info = await api("info"); } catch (e) {} }
      var link = shareLink();
      if (navigator.share) {
        try { await navigator.share({ title: "My Netlithic address", text: shareText(), url: link }); return; }
        catch (e) { if (e && e.name === "AbortError") return; }
      }
      var r = await copyText(link);
      if (r === false) throw new Error("Couldn't copy. Use Copy address instead.");
      say("Link copied. Paste it to whoever is sending you test coins.");
    }));
    $("b-qr").addEventListener("click", guard(async function () {
      var box = $("h-qr-box");
      if (!box.hidden) { closeSub("qr"); return; }
      if (!info) { try { info = await api("info"); } catch (e) {} }
      drawQr(shareLink()); box.hidden = false; $("b-qr").textContent = "Hide QR code";
      openSub("qr");
    }));
    $("s-to").addEventListener("input", function () { $("s-prefill").hidden = true; });
    $("b-backup-again").addEventListener("click", guard(async function () {
      var raw = localStorage.getItem(STORE); if (raw) await download(JSON.parse(raw));
    }));

    $("b-drip").addEventListener("click", guard(async function () {
      var r = await api("drip", { address: address });
      say("On the way: " + String(r.amount_nlt || "") + " free test NLT. It will show up below in a moment and arrive with the next block.");
      setTimeout(function () { refresh(true); }, 2000);
    }));

    $("b-review").addEventListener("click", guard(async function () {
      $("s-confirm").hidden = true; pending = null;
      info = await api("info");
      var to = $("s-to").value.trim().toLowerCase();
      var amount = C.toBaseUnits($("s-amount").value);
      var acct = await api("account?address=" + encodeURIComponent(address));
      if (amount + info.min_fee > acct.balance) throw new Error("Not enough coins. You have " + C.fromBaseUnits(acct.balance) + " test NLT, and the fee is " + C.fromBaseUnits(info.min_fee) + ".");
      var tx = await C.signTransfer(key, { recipient: to, amount: amount, nonce: acct.next_nonce, fee: info.min_fee, chain_id: info.chain_id });
      pending = tx;
      var nm = nameOf(to);
      $("s-summary").textContent = "You're about to send " + C.fromBaseUnits(amount) + " test NLT to " + (nm ? nm + " (" + to + ")" : to) +
        ". The fee is " + C.fromBaseUnits(info.min_fee) + " test NLT, so " + C.fromBaseUnits(amount + info.min_fee) +
        " leaves your wallet in total. Transfers can't be undone.";
      $("s-confirm").hidden = false;
      openSub("review");
    }));
    $("b-cancel").addEventListener("click", function () { closeSub("review"); });
    $("b-send").addEventListener("click", guard(async function () {
      if (!pending) return;
      var tx = pending; pending = null; $("s-confirm").hidden = true;
      if (curSub === "review") { curSub = null; navPush(true); }
      var res = await api("send", { tx: tx });
      $("s-to").value = $("s-amount").value = ""; $("s-prefill").hidden = true;
      var tid = String(res.tx_id || "").toLowerCase();
      if (/^[0-9a-f]{64}$/.test(tid)) {
        tracked[tid] = { dir: "out", other: tx.payload.recipient, amount: tx.payload.amount, fee: tx.payload.fee,
          state: "pending", since: Date.now(), local: true };
        paintMoving();
      }
      say("Sent! It's on the way and will arrive with the next block.");
      setTimeout(function () { refresh(true); }, 2000);
      refresh(true);
      setLink("s-track", info && info.explorer && /^[0-9a-f]{64}$/.test(tid) ? info.explorer + "/tx/" + tid : null);
    }));

    if (!window.crypto || !crypto.subtle) { say("This browser is too old for the wallet. Please use a recent Chrome, Firefox, Safari or Edge.", true); return; }
    if (!localStorage.getItem(STORE)) { clearStay(); show("v-start"); startNav(); return; }
    show("v-unlock"); startNav();
    if (readStay() && window.indexedDB) {
      resumeStay().then(function (seed) { if (seed) openWallet(seed); }, function () { clearStay(); });
    }
  });
})();
