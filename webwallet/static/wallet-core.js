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

  var api = { canonical: canonical, keyFromSeed: keyFromSeed, newSeed: newSeed, addressOf: addressOf,
    backupCode: backupCode, seedFromBackupCode: seedFromBackupCode, toBaseUnits: toBaseUnits,
    fromBaseUnits: fromBaseUnits, txTimestamp: txTimestamp, signTransfer: signTransfer, hex: hex, unhex: unhex };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.NetlithicCore = api;
})(this);
