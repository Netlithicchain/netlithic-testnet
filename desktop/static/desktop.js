/* Desktop extras: the Start/Stop helping button. Sends only the ADDRESS to the
 * local app (127.0.0.1); the secret key never leaves the wallet page's memory. */
(function () {
  "use strict";
  function $(id) { return document.getElementById(id); }
  var earning = false;
  function api() { return window.pywebview && window.pywebview.api; }
  // Native Save dialog / clipboard from the desktop window. Only the encrypted
  // backup text (or the address) is passed; the secret key never is.
  function hook() {
    if (!api()) return;
    window.NetlithicSaveBackup = function (text, name) { return api().save_backup(text, name); };
    window.NetlithicCopy = function (text) { return api().copy_text(text); };
    window.NetlithicNotify = function (title, body) { return api().notify(title, body); };
  }
  window.addEventListener("pywebviewready", hook);
  hook();
  async function call(path, body) {
    var opts = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
    var r = await fetch("/api/earn/" + path, opts);
    var d = {}; try { d = await r.json(); } catch (e) { /* ignore */ }
    if (!r.ok) throw new Error(typeof d.error === "string" ? d.error : "Something went wrong.");
    return d;
  }
  function paint(s) {
    earning = !!s.earning;
    var b = $("e-toggle");
    b.textContent = earning ? "Stop helping" : "Start helping";
    b.className = earning ? "big earn-btn on" : "big earn-btn";
    b.disabled = !earning && !s.synced;
    $("e-sync").textContent = s.sync;
    $("e-blocks").textContent = String(s.blocks_found);
    var st = s.stats || {};
    $("e-today").textContent = String(st.today || "0 minutes");
    $("e-total").textContent = String(st.total || "0 minutes");
    $("e-blocks-all").textContent = String(st.blocks_found_total || 0);
    // points and the next reward share come from the wallet's own account check
    var pts = $("h-points"), nxt = $("h-next-payout");
    $("e-points").textContent = pts && pts.textContent !== "…" ? pts.textContent : "0";
    $("e-next").textContent = nxt ? nxt.textContent : "";
    $("e-note").textContent = earning ? (s.message || "Helping. You can leave this window open in the background.") : (s.message || "");
  }
  async function poll() { try { paint(await call("status")); } catch (e) { $("e-sync").textContent = "Your node isn't answering yet..."; } }
  document.addEventListener("DOMContentLoaded", function () {
    $("e-toggle").addEventListener("click", async function () {
      try {
        var addr = $("h-address").textContent.trim();
        paint(await call(earning ? "stop" : "start", earning ? {} : { address: addr }));
      } catch (e) { $("e-note").textContent = e.message; }
    });
    // explorer links open in the normal browser
    ["h-history", "s-track"].forEach(function (id) { var a = $(id); if (a) { a.target = "_blank"; a.rel = "noopener"; } });
    $("b-lock").addEventListener("click", function () { if (earning) call("stop", {}).then(paint, function () {}); });
    poll(); setInterval(poll, 3000);
  });
})();
