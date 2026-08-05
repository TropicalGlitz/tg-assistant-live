/* Tropical Glitz — AI Assistant widget (self-injecting, served by the backend).
   Loaded on the storefront with:
     <script src="https://tg-assistant-ie5p.onrender.com/widget.js" defer></script>
   Renders the chat, product cards with a size/variant selector, and adds to the
   cart via the storefront AJAX Cart API (/cart/add.js) — no token needed. */
(function () {
  var BACKEND = "https://tg-assistant-ie5p.onrender.com";

  var CSS =
    '#tg-w *{box-sizing:border-box}' +
    '#tg-w{--tg:#ef2c8f;--tg2:#f3f3f3;--tgt:#1b1b1f;--tgm:#6b6b74;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}' +
    '#tg-launch{position:fixed;right:20px;bottom:20px;width:60px;height:60px;border-radius:50%;border:0;background:var(--tg);color:#fff;cursor:pointer;box-shadow:0 12px 40px rgba(0,0,0,.18);display:grid;place-items:center;font-size:26px;z-index:2147483000;transition:transform .18s}' +
    '#tg-launch:hover{transform:scale(1.06)}' +
    '#tg-panel{position:fixed;right:20px;bottom:20px;width:min(400px,calc(100vw - 32px));height:min(640px,calc(100vh - 40px));background:#fff;color:var(--tgt);border-radius:18px;box-shadow:0 12px 40px rgba(0,0,0,.22);display:flex;flex-direction:column;overflow:hidden;z-index:2147483001;opacity:0;transform:translateY(12px) scale(.98);pointer-events:none;transition:opacity .2s,transform .2s}' +
    '#tg-panel[data-open="true"]{opacity:1;transform:none;pointer-events:auto}' +
    '@media (prefers-reduced-motion:reduce){#tg-panel,#tg-launch{transition:none}}' +
    '#tg-w .h{background:var(--tg);color:#fff;padding:16px 18px;display:flex;align-items:center;gap:12px}' +
    '#tg-w .av{width:40px;height:40px;border-radius:50%;background:#ffffff33;display:grid;place-items:center;font-size:22px}' +
    '#tg-w .h h2{margin:0;font-size:15px;font-weight:800}#tg-w .h span{font-size:12px;opacity:.9}' +
    '#tg-w .x{margin-left:auto;background:0;border:0;color:#fff;font-size:20px;cursor:pointer;padding:6px;line-height:1}' +
    '#tg-log{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px;background:#fafafa}' +
    '#tg-w .m{max-width:85%;padding:10px 13px;border-radius:14px;font-size:14px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word}' +
    '#tg-w .m.ai{background:var(--tg2);align-self:flex-start;border-bottom-left-radius:4px}' +
    '#tg-w .m.u{background:var(--tg);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}' +
    '#tg-w .src{font-size:11px;color:var(--tgm);align-self:flex-start;margin-top:-4px}' +
    '#tg-w .ty{align-self:flex-start;display:inline-flex;gap:4px;padding:12px 14px;background:var(--tg2);border-radius:14px}' +
    '#tg-w .ty i{width:7px;height:7px;border-radius:50%;background:#bcbcc6;animation:tgb 1s infinite ease-in-out}' +
    '#tg-w .ty i:nth-child(2){animation-delay:.15s}#tg-w .ty i:nth-child(3){animation-delay:.3s}' +
    '@keyframes tgb{0%,80%,100%{transform:scale(.6);opacity:.5}40%{transform:scale(1);opacity:1}}' +
    '#tg-w .cards{align-self:stretch;display:flex;flex-direction:column;gap:10px}' +
    '#tg-w .card{display:flex;gap:10px;padding:10px;border:1px solid #eee;border-radius:14px;background:#fff}' +
    '#tg-w .card img{width:58px;height:58px;border-radius:10px;object-fit:cover;flex:none;background:#f3f3f3}' +
    '#tg-w .card .info{flex:1;min-width:0;display:flex;flex-direction:column;gap:6px}' +
    '#tg-w .card .nm{font-size:13px;font-weight:700;line-height:1.25}' +
    '#tg-w .card a.nm{color:var(--tgt);text-decoration:none}' +
    '#tg-w .card a.nm:hover{color:var(--tg)}' +
    '#tg-w .card .pr{font-size:13px;font-weight:800;color:var(--tg)}' +
    '#tg-w .card select{width:100%;border:1px solid #e2e2e8;border-radius:10px;padding:7px 9px;font-size:12px;background:#fff;color:var(--tgt)}' +
    '#tg-w .card .buy{border:0;border-radius:999px;background:var(--tg);color:#fff;padding:8px 12px;font-size:13px;font-weight:700;cursor:pointer}' +
    '#tg-w .card .buy[disabled]{opacity:.6;cursor:default}' +
    '#tg-w .card .buy.ok{background:#12b76a}' +
    '#tg-w .card .vc{font-size:11px;color:var(--tg);text-decoration:none;align-self:flex-start}' +
    '#tg-chips{display:flex;flex-wrap:wrap;gap:8px;padding:0 16px 8px}' +
    '#tg-w .chip{border:1px solid var(--tg);color:var(--tg);background:#fff;border-radius:999px;padding:7px 13px;font-size:13px;cursor:pointer}' +
    '#tg-form{display:flex;gap:8px;padding:12px 14px;border-top:1px solid #eee;background:#fff}' +
    '#tg-form input{flex:1;border:1px solid #e2e2e8;border-radius:999px;padding:11px 16px;font-size:14px;outline:none}' +
    '#tg-form input:focus{border-color:var(--tg)}' +
    '#tg-form button{width:42px;height:42px;border-radius:50%;border:0;background:var(--tg);color:#fff;cursor:pointer;font-size:17px}' +
    '#tg-w .ft{text-align:center;font-size:10px;color:#b8b8c2;padding:6px}';

  var HTML =
    '<button id="tg-launch" aria-label="Open support chat" aria-expanded="false">🌴</button>' +
    '<section id="tg-panel" role="dialog" aria-modal="false" aria-label="Tropical Glitz AI Support" data-open="false">' +
      '<div class="h"><div class="av">🌴</div><div><h2>Tropical Glitz AI Support</h2><span>Here to help you shop</span></div>' +
      '<button class="x" id="tg-x" aria-label="Close">✕</button></div>' +
      '<div id="tg-log" aria-live="polite"></div>' +
      '<div id="tg-chips"></div>' +
      '<form id="tg-form" autocomplete="off"><input id="tg-t" type="text" placeholder="Type anything here..." aria-label="Message"><button type="submit" aria-label="Send">➤</button></form>' +
      '<div class="ft">Powered by Tropical Glitz AI</div>' +
    '</section>';

  function money(n) {
    var v = Number(n);
    if (isNaN(v)) return "";
    return "$" + v.toFixed(2);
  }

  function boot() {
    if (document.getElementById("tg-w")) return;
    var style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);
    var wrap = document.createElement("div");
    wrap.id = "tg-w";
    wrap.innerHTML = HTML;
    document.body.appendChild(wrap);

    var P = document.getElementById("tg-panel"),
        L = document.getElementById("tg-launch"),
        log = document.getElementById("tg-log"),
        C = document.getElementById("tg-chips"),
        F = document.getElementById("tg-form"),
        T = document.getElementById("tg-t");
    var greeted = false;

    function add(c, t) { var d = document.createElement("div"); d.className = c; if (t != null) d.textContent = t; log.appendChild(d); log.scrollTop = log.scrollHeight; return d; }
    function chips(list) { C.innerHTML = ""; (list || []).forEach(function (t) { var b = document.createElement("button"); b.className = "chip"; b.type = "button"; b.textContent = t; b.onclick = function () { ask(t); }; C.appendChild(b); }); }

    function addToCart(variantId, btn) {
      if (!variantId) return;
      btn.disabled = true;
      var original = btn.textContent;
      btn.textContent = "Adding…";
      fetch("/cart/add.js", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: [{ id: Number(variantId), quantity: 1 }] })
      })
        .then(function (r) { if (!r.ok) throw new Error("cart"); return r.json(); })
        .then(function () {
          btn.textContent = "✓ Added";
          btn.classList.add("ok");
          document.dispatchEvent(new CustomEvent("tg:cart-updated"));
          var card = btn.closest(".card");
          if (card && !card.querySelector(".vc")) {
            var vc = document.createElement("a");
            vc.className = "vc"; vc.href = "/cart"; vc.textContent = "View cart →";
            btn.parentNode.appendChild(vc);
          }
          setTimeout(function () { btn.textContent = original; btn.classList.remove("ok"); btn.disabled = false; }, 2500);
        })
        .catch(function () {
          btn.textContent = "Try again";
          btn.disabled = false;
        });
    }

    function renderProducts(list) {
      if (!list || !list.length) return;
      var box = document.createElement("div");
      box.className = "cards";
      list.forEach(function (p) {
        var variants = (p.variants || []).filter(function (v) { return v.id; });
        if (!variants.length) return;
        var card = document.createElement("div");
        card.className = "card";

        if (p.image) {
          var img = document.createElement("img");
          img.src = p.image; img.alt = p.title || ""; img.loading = "lazy";
          card.appendChild(img);
        }
        var info = document.createElement("div");
        info.className = "info";

        var nm = document.createElement(p.url ? "a" : "div");
        nm.className = "nm";
        nm.textContent = p.title || "";
        if (p.url) { nm.href = p.url; nm.target = "_top"; }
        info.appendChild(nm);

        var pr = document.createElement("div");
        pr.className = "pr";
        pr.textContent = money(variants[0].price);
        info.appendChild(pr);

        var sel = null;
        if (variants.length > 1) {
          sel = document.createElement("select");
          variants.forEach(function (v, i) {
            var o = document.createElement("option");
            o.value = v.id;
            o.textContent = (v.title || "Option") + " — " + money(v.price) + (v.available ? "" : " (sold out)");
            o.disabled = !v.available;
            o.setAttribute("data-price", v.price);
            if (i === 0) o.selected = true;
            sel.appendChild(o);
          });
          sel.onchange = function () {
            var opt = sel.options[sel.selectedIndex];
            pr.textContent = money(opt.getAttribute("data-price"));
          };
          info.appendChild(sel);
        }

        var buy = document.createElement("button");
        buy.className = "buy"; buy.type = "button"; buy.textContent = "Add to cart";
        buy.onclick = function () {
          var vid = sel ? sel.value : variants[0].id;
          addToCart(vid, buy);
        };
        info.appendChild(buy);

        card.appendChild(info);
        box.appendChild(card);
      });
      if (box.children.length) { log.appendChild(box); log.scrollTop = log.scrollHeight; }
    }

    function open() {
      P.dataset.open = "true"; L.setAttribute("aria-expanded", "true"); T.focus();
      if (!greeted) { greeted = true; add("m ai", "Welcome back! How may I be of service to you today?"); chips(["Any promotions?", "Not sure what to choose", "How much paint do I need?"]); }
    }
    function close() { P.dataset.open = "false"; L.setAttribute("aria-expanded", "false"); L.focus(); }

    function ask(q) {
      if (!q || !q.trim()) return; C.innerHTML = ""; add("m u", q); T.value = "";
      var ty = add("ty"); ty.innerHTML = "<i></i><i></i><i></i>";
      fetch(BACKEND + "/chat", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ message: q }) })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          ty.remove(); var ai = add("m ai", ""); var words = (j.answer || "").split(" "); var i = 0;
          (function tick() {
            if (i < words.length) { ai.textContent += (i ? " " : "") + words[i++]; log.scrollTop = log.scrollHeight; setTimeout(tick, 18); }
            else {
              renderProducts(j.products);
              if (j.sources && j.sources.length) { var s = add("src"); s.textContent = "Sources: " + j.sources.map(function (x) { return x.source + ":" + (x.ref || "").slice(0, 26); }).join(" · "); }
              chips(j.handoff ? ["Contact support", "Browse best sellers"] : ["Tell me more", "Any promotions?"]);
            }
          })();
        })
        .catch(function () { ty.remove(); add("m ai", "Sorry, something went wrong. Email tropicalglitz@gmail.com or call 786-383-3013."); });
    }
    L.onclick = function () { P.dataset.open === "true" ? close() : open(); };
    document.getElementById("tg-x").onclick = close;
    F.addEventListener("submit", function (e) { e.preventDefault(); ask(T.value); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && P.dataset.open === "true") close(); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
