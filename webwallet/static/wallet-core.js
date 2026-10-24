/* Netlithic web wallet core: key handling, canonical JSON and tx signing.
 * Must produce byte-for-byte the same signed message as wallet.py:
 *   canonical({"payload": payload, "pubkey": pubkey})  (sorted keys, no spaces)
 * Works in the browser (window.NetlithicCore) and in Node.js (module.exports) for tests.
 * Signing uses the vendored tweetnacl (public domain, v1.0.3), so keys are
 * derived the same way in every browser. No network access here. */
(function (root) {
  "use strict";
  var nacl = (typeof module !== "undefined" && module.exports) ? require("./nacl-fast.min.js") : root.nacl;
  var DECIMALS = 8;
  var BASE = 100000000n;

  function hex(bytes) {
    var s = "";
    for (var i = 0; i < bytes.length; i++) s += (bytes[i] < 16 ? "0" : "") + bytes[i].toString(16);
    return s;
  }
  function unhex(s) {
    if (typeof s !== "string" || s.length % 2 || !/^[0-9a-f]*$/.test(s)) throw new Error("bad hex");
    var out = new Uint8Array(s.length / 2);
    for (var i = 0; i < out.length; i++) out[i] = parseInt(s.substr(i * 2, 2), 16);
    return out;
  }
  function utf8(s) { return new TextEncoder().encode(s); }

  // Same as Python json.dumps(obj, sort_keys=True, separators=(",", ":")) for the
  // values a tx contains (ASCII strings, safe integers, the float timestamp).
  function canonical(v) {
    if (v === null) return "null";
    if (typeof v === "number") {
      if (!isFinite(v)) throw new Error("bad number");
      return JSON.stringify(v);
    }
    if (typeof v === "string" || typeof v === "boolean") return JSON.stringify(v);
    if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
    var keys = Object.keys(v).sort();
    return "{" + keys.map(function (k) { return JSON.stringify(k) + ":" + canonical(v[k]); }).join(",") + "}";
  }

  async function sha256Hex(text) {
    var c = (typeof crypto !== "undefined" && crypto.subtle) ? crypto : require("crypto").webcrypto;
    var d = await c.subtle.digest("SHA-256", utf8(text));
    return hex(new Uint8Array(d));
  }

  function keyFromSeed(seedHex) {
    var kp = nacl.sign.keyPair.fromSeed(unhex(seedHex));
    return { seed: seedHex, pubkey: hex(kp.publicKey), secretKey: kp.secretKey };
  }
  function newSeed() { return hex(nacl.randomBytes(32)); }

  async function addressOf(pubkeyHex) { return (await sha256Hex(pubkeyHex)).slice(0, 32); }

  // Backup code: 64 hex seed + 4 hex checksum, in 17 groups of 4.
  async function backupCode(seedHex) {
    var check = (await sha256Hex("netlithic-backup:" + seedHex)).slice(0, 4);
    return (seedHex + check).match(/.{4}/g).join("-");
  }
  async function seedFromBackupCode(code) {
    var s = String(code || "").toLowerCase().replace(/[^0-9a-f]/g, "");
    if (s.length !== 68) throw new Error("The backup code should have 17 groups of 4 letters/numbers.");
    var seed = s.slice(0, 64);
    if ((await sha256Hex("netlithic-backup:" + seed)).slice(0, 4) !== s.slice(64))
      throw new Error("That backup code has a typo. Please check each group.");
    return seed;
  }

  // "1.5" NLT -> 150000000 base units (exact, no floating point).
  function toBaseUnits(text) {
    var s = String(text || "").trim();
    if (!/^\d{1,10}(\.\d{1,8})?$/.test(s)) throw new Error("Enter an amount like 5 or 2.5");
    var parts = s.split(".");
    var frac = ((parts[1] || "") + "00000000").slice(0, DECIMALS);
    var v = BigInt(parts[0]) * BASE + BigInt(frac);
    if (v <= 0n) throw new Error("The amount must be more than zero.");
    if (v > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("That amount is too large.");
    return Number(v);
  }
  function fromBaseUnits(n) {
    var v = BigInt(n);
    var neg = v < 0n; if (neg) v = -v;
    var whole = v / BASE, frac = (v % BASE).toString().padStart(DECIMALS, "0").replace(/0+$/, "");
    return (neg ? "-" : "") + whole.toString() + (frac ? "." + frac : "");
  }

  // Float timestamp with a fractional part, so JS and Python print it identically.
  function txTimestamp(ms) {
    var t = Math.floor(ms === undefined ? Date.now() : ms) / 1000;
    if (Number.isInteger(t)) t += 0.001;
    return t;
  }

  async function signTransfer(key, opts) {
    var sender = await addressOf(key.pubkey);
    var recipient = String(opts.recipient || "").trim().toLowerCase();
    if (!/^[0-9a-f]{32}$/.test(recipient)) throw new Error("That address doesn't look right. It should be 32 letters/numbers (0-9, a-f).");
    if (recipient === sender) throw new Error("You can't send coins to your own address.");
    ["amount", "nonce", "fee"].forEach(function (k) {
      if (!Number.isSafeInteger(opts[k]) || opts[k] < (k === "fee" ? 0 : 1)) throw new Error("bad " + k);
    });
    if (typeof opts.chain_id !== "string" || !opts.chain_id) throw new Error("bad chain_id");
    var payload = {
      sender: sender, recipient: recipient, amount: opts.amount, nonce: opts.nonce,
      timestamp: opts.timestamp !== undefined ? opts.timestamp : txTimestamp(),
      fee: opts.fee, chain_id: opts.chain_id
    };
    var msg = utf8(canonical({ payload: payload, pubkey: key.pubkey }));
    var sig = hex(nacl.sign.detached(msg, key.secretKey));
    return { payload: payload, pubkey: key.pubkey, sig: sig };
  }

  // ---- "On the way" tracking (pure, so it can be tested in Node) ----
  // tracked: { txId: {dir:"in"|"out", other, amount, state:"pending"|"arrived", since, done} }
  // acct: /api/account answer with pending_in / pending_out from the node mempool.
  // A transfer that leaves the mempool is shown as "Arrived" for ARRIVED_MS.
  var ARRIVED_MS = 10 * 60 * 1000, LOCAL_GRACE_MS = 60 * 1000;
  function trackTransfers(tracked, acct, now) {
    tracked = tracked || {};
    if (acct && acct.pending_known) {
      var seen = {};
      (acct.pending_in || []).forEach(function (t) {
        seen[t.tx_id] = 1;
        var old = tracked[t.tx_id];
        tracked[t.tx_id] = { dir: "in", other: t.from, amount: t.amount, state: "pending", since: old ? old.since : now };
      });
      (acct.pending_out || []).forEach(function (t) {
        seen[t.tx_id] = 1;
        var old = tracked[t.tx_id];
        tracked[t.tx_id] = { dir: "out", other: t.to, amount: t.amount, fee: t.fee || 0, state: "pending", since: old ? old.since : now };
      });
      Object.keys(tracked).forEach(function (id) {
        var t = tracked[id];
        if (t.state !== "pending" || seen[id]) return;
        if (t.local && now - t.since < LOCAL_GRACE_MS) return;  // just sent; the node may not list it yet
        t.state = "arrived"; t.done = now;
      });
    }
    Object.keys(tracked).forEach(function (id) {
      if (tracked[id].state === "arrived" && now - tracked[id].done > ARRIVED_MS) delete tracked[id];
    });
    return tracked;
  }
  function shortAddr(a) { a = String(a || ""); return a.length > 12 ? a.slice(0, 4) + "…" + a.slice(-4) : a; }
  function describeTransfer(t) {
    var amt = fromBaseUnits(t.amount) + " test NLT";
    if (t.state === "arrived") return t.dir === "in" ? "Received " + amt + " from " + shortAddr(t.other) : "Sent " + amt + " to " + shortAddr(t.other);
    return t.dir === "in" ? "Receiving " + amt + " from " + shortAddr(t.other) : "Sending " + amt + " to " + shortAddr(t.other);
  }
  // "Pending: +5 / -2" line. Never changes the confirmed balance.
  function pendingTotals(tracked) {
    var inn = 0, out = 0;
    Object.keys(tracked || {}).forEach(function (id) {
      var t = tracked[id]; if (t.state !== "pending") return;
      if (t.dir === "in") inn += t.amount; else out += t.amount + (t.fee || 0);
    });
    return { in: inn, out: out };
  }


  // ---- Activity history (built from /api/history). Plain words; test coins have no value.
  function whoIs(addr, nameOf) {
    var n = typeof nameOf === "function" ? nameOf(addr) : null;
    return n ? n : shortAddr(addr);
  }
  function describeActivity(it, nameOf) {
    var amt = fromBaseUnits(it.amount) + " test NLT";
    var waiting = it.pending ? " (on the way)" : "";
    switch (it.kind) {
      case "received": return "Received " + amt + " from " + whoIs(it.from, nameOf) + waiting;
      case "sent": return "Sent " + amt + " to " + whoIs(it.to, nameOf) + waiting;
      case "reward_share": return "Reward share: " + amt + " (unlocks bit by bit)";
      case "mined": return "Mined a block: " + amt;
      case "allocation": return "Starting coins: " + amt;
      default: return "Activity: " + amt;
    }
  }
  function activityKey(it) {
    return [it.kind, it.height || 0, it.tx_id || "", it.amount, it.epoch || ""].join(":");
  }
  // Items that just arrived (coins received, reward shares) and weren't seen before.
  // The first look only fills `seen`, so opening the wallet never shows old news.
  function newArrivals(seen, items, first) {
    var fresh = [];
    (items || []).forEach(function (it) {
      if (it.pending) return;
      var k = activityKey(it);
      if (seen[k]) return;
      seen[k] = 1;
      if (!first && (it.kind === "received" || it.kind === "reward_share")) fresh.push(it);
    });
    return fresh;
  }
  function notifyText(it, nameOf) {
    if (it.kind === "reward_share") return "A reward share of " + fromBaseUnits(it.amount) + " test NLT landed in your wallet. Test coins have no value.";
    return "You received " + fromBaseUnits(it.amount) + " test NLT from " + whoIs(it.from, nameOf) + ". Test coins have no value.";
  }
  function whenText(ts, nowMs) {
    if (!Number.isFinite(ts) || ts <= 0) return "";
    var d = new Date(ts * 1000), now = new Date(nowMs || Date.now());
    var t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    if (d.toDateString() === now.toDateString()) return "Today " + t;
    var y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return "Yesterday " + t;
    return d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + t;
  }

  // ---- Address book. Kept only on this device (localStorage), never sent anywhere.
  var MAX_CONTACTS = 200, ADDR = /^[0-9a-f]{32}$/;
  function cleanName(n) {
    n = String(n == null ? "" : n).replace(/[\u0000-\u001f\u007f<>]/g, "").replace(/\s+/g, " ").trim();
    return n.slice(0, 40);
  }
  function parseContacts(raw) {
    var list; try { list = JSON.parse(raw || "[]"); } catch (e) { return []; }
    if (!Array.isArray(list)) return [];
    var out = [], seen = {};
    list.forEach(function (c) {
      if (!c || typeof c !== "object") return;
      var a = String(c.address || "").toLowerCase(), n = cleanName(c.name);
      if (!ADDR.test(a) || !n || seen[a] || out.length >= MAX_CONTACTS) return;
      seen[a] = 1; out.push({ name: n, address: a });
    });
    return out;
  }
  function addContact(list, name, addr) {
    var n = cleanName(name), a = String(addr || "").trim().toLowerCase();
    if (!n) throw new Error("Please type a name.");
    if (!ADDR.test(a)) throw new Error("That address doesn't look right. It should be 32 letters and numbers (0-9, a-f).");
    var out = (list || []).filter(function (c) { return c.address !== a; });
    if (out.length >= MAX_CONTACTS) throw new Error("Your contact list is full.");
    out.push({ name: n, address: a });
    out.sort(function (x, y) { return x.name.toLowerCase() < y.name.toLowerCase() ? -1 : 1; });
    return out;
  }
  function removeContact(list, addr) { return (list || []).filter(function (c) { return c.address !== addr; }); }
  function contactName(list, addr) {
    for (var i = 0; i < (list || []).length; i++) if (list[i].address === addr) return list[i].name;
    return null;
  }

  // "Stay unlocked for 15 minutes": pure timing rules (used by wallet.js, tested in Node).
  // A session lasts 15 minutes after the last activity, and never longer.
  var STAY_MS = 15 * 60 * 1000;
  function stayUntil(nowMs) { return Math.floor(nowMs) + STAY_MS; }
  function staySessionValid(rec, nowMs) {
    if (!rec || typeof rec !== "object") return false;
    var e = rec.expires;
    if (!Number.isFinite(e) || e <= nowMs || e > nowMs + STAY_MS) return false;  // expired, or tampered beyond the cap
    return typeof rec.id === "string" && /^[0-9a-f]{32}$/.test(rec.id) &&
      typeof rec.iv === "string" && /^[0-9a-f]{24}$/.test(rec.iv) &&
      typeof rec.ct === "string" && /^[0-9a-f]+$/.test(rec.ct) && typeof rec.address === "string";
  }
  function stayLine(expiresMs) {
    var t = new Date(expiresMs).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    return "Stays unlocked in this tab until " + t + ".";
  }

  var api = { describeActivity: describeActivity, activityKey: activityKey, newArrivals: newArrivals, notifyText: notifyText, whenText: whenText, cleanName: cleanName, parseContacts: parseContacts, addContact: addContact, removeContact: removeContact, contactName: contactName, STAY_MS: STAY_MS, stayUntil: stayUntil, staySessionValid: staySessionValid, stayLine: stayLine, trackTransfers: trackTransfers, describeTransfer: describeTransfer, pendingTotals: pendingTotals, shortAddr: shortAddr, canonical: canonical, keyFromSeed: keyFromSeed, newSeed: newSeed, addressOf: addressOf,
    backupCode: backupCode, seedFromBackupCode: seedFromBackupCode, toBaseUnits: toBaseUnits,
    fromBaseUnits: fromBaseUnits, txTimestamp: txTimestamp, signTransfer: signTransfer, hex: hex, unhex: unhex };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.NetlithicCore = api;
})(this);
