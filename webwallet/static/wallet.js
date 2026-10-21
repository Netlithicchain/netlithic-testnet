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
  var key = null, address = null, info = null, pending = null, freshBlob = null;

  function $(id) { return document.getElementById(id); }
  function show(view) {
    ["v-start", "v-create", "v-backup", "v-restore", "v-unlock", "v-home"].forEach(function (v) { $(v).hidden = v !== view; });
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

  async function openWallet(seed) {
    key = C.keyFromSeed(seed);
    address = await C.addressOf(key.pubkey);
    $("h-address").textContent = address;
    show("v-home");
    refresh();
  }
  async function refresh() {
    try {
      info = await api("info");
      var acct = await api("account?address=" + encodeURIComponent(address));
      $("h-balance").textContent = C.fromBaseUnits(acct.balance);
      $("h-points").textContent = acct.points;
    } catch (e) { say(e.message, true); }
  }
  function download(blob) {
    var a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([JSON.stringify(blob, null, 2)], { type: "application/json" }));
    a.download = "netlithic-wallet-backup-" + blob.address.slice(0, 8) + ".json";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
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
    $("b-download").addEventListener("click", function () { if (freshBlob) download(freshBlob); });
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
      await openWallet(seed);
      say("Welcome back! Your wallet is ready.");
    }));

    $("b-unlock").addEventListener("click", guard(async function () {
      var seed = await decryptSeed(JSON.parse(localStorage.getItem(STORE)), $("u-pass").value);
      $("u-pass").value = "";
      await openWallet(seed);
    }));
    $("b-forget").addEventListener("click", function () {
      if (confirm("Remove this wallet from this device? You can only get it back with your backup code or backup file.")) {
        localStorage.removeItem(STORE); key = null; show("v-start");
      }
    });
    $("b-lock").addEventListener("click", function () { key = null; address = null; show("v-unlock"); });
    $("b-refresh").addEventListener("click", guard(refresh));
    $("b-copy").addEventListener("click", function () {
      navigator.clipboard.writeText(address).then(function () { say("Address copied."); }, function () { say("Couldn't copy. Select the address and copy it by hand.", true); });
    });
    $("b-backup-again").addEventListener("click", guard(async function () {
      var raw = localStorage.getItem(STORE); if (raw) download(JSON.parse(raw));
    }));

    $("b-drip").addEventListener("click", guard(async function () {
      var r = await api("drip", { address: address });
      say("On the way: " + String(r.amount_nlt || "") + " free test NLT. It shows up after the next block (about 10 minutes).");
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
      $("s-summary").textContent = "You're about to send " + C.fromBaseUnits(amount) + " test NLT to " + to +
        ". The fee is " + C.fromBaseUnits(info.min_fee) + " test NLT, so " + C.fromBaseUnits(amount + info.min_fee) +
        " leaves your wallet in total. Transfers can't be undone.";
      $("s-confirm").hidden = false;
    }));
    $("b-cancel").addEventListener("click", function () { pending = null; $("s-confirm").hidden = true; });
    $("b-send").addEventListener("click", guard(async function () {
      if (!pending) return;
      var tx = pending; pending = null; $("s-confirm").hidden = true;
      await api("send", { tx: tx });
      $("s-to").value = $("s-amount").value = "";
      say("Sent! It will show up after the next block (about 10 minutes).");
    }));

    if (!window.crypto || !crypto.subtle) { say("This browser is too old for the wallet. Please use a recent Chrome, Firefox, Safari or Edge.", true); return; }
    show(localStorage.getItem(STORE) ? "v-unlock" : "v-start");
  });
})();
