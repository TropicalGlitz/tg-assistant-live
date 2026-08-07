/* Tropical Glitz — AI Assistant widget (self-injecting, served by the backend).
   Loaded on the storefront with:
     <script src="https://tg-assistant-ie5p.onrender.com/widget.js" defer></script>
   Renders the chat, product cards with a size/variant selector, and adds to the
   cart via the storefront AJAX Cart API (/cart/add.js) — no token needed. */
(function () {
  var BACKEND = "https://tg-assistant-ie5p.onrender.com";

  // ID de sesión por navegador: agrupa las preguntas de un mismo visitante en el panel.
  var SID = (function () {
    try {
      var k = "tg_sid", v = localStorage.getItem(k);
      if (!v) { v = Date.now().toString(36) + Math.random().toString(36).slice(2, 8); localStorage.setItem(k, v); }
      return v;
    } catch (e) { return "anon"; }
  })();

  var LOGO = BACKEND + "/tg-logo.png";

  var CSS =
    '#tg-w *{box-sizing:border-box}' +
    '#tg-w{--tg:#ef2c8f;--tg2:#f3f3f3;--tgt:#1b1b1f;--tgm:#6b6b74;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}' +
    '#tg-launch{position:fixed;right:20px;bottom:20px;width:62px;height:60px;border-radius:20px 20px 20px 6px;border:0;background:#fff;cursor:pointer;box-shadow:0 10px 30px rgba(0,0,0,.22);display:grid;place-items:center;padding:0;z-index:2147483000;transition:transform .18s}' +
    '#tg-launch::after{content:"";position:absolute;left:13px;bottom:-6px;width:0;height:0;border:7px solid transparent;border-top-color:#fff;border-bottom:0}' +
    '#tg-launch img{width:46px;height:46px;object-fit:contain;border-radius:50%;display:block;pointer-events:none}' +
    '#tg-launch:hover{transform:scale(1.06)}' +
    '#tg-panel{position:fixed;right:20px;bottom:20px;width:min(400px,calc(100vw - 32px));height:min(640px,calc(100vh - 40px));background:#fff;color:var(--tgt);border-radius:18px;box-shadow:0 12px 40px rgba(0,0,0,.22);display:flex;flex-direction:column;overflow:hidden;z-index:2147483001;opacity:0;transform:translateY(12px) scale(.98);pointer-events:none;transition:opacity .2s,transform .2s}' +
    '#tg-panel[data-open="true"]{opacity:1;transform:none;pointer-events:auto}' +
    '@media (prefers-reduced-motion:reduce){#tg-panel,#tg-launch{transition:none}}' +
    '#tg-w .h{background:var(--tg);color:#fff;padding:16px 18px;display:flex;align-items:center;gap:12px}' +
    '#tg-w .av{width:40px;height:40px;border-radius:50%;background:#fff;display:grid;place-items:center;overflow:hidden}' +
    '#tg-w .av img{width:34px;height:34px;object-fit:contain}' +
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
    '#tg-w .cform{align-self:stretch;background:#fff;border:1px solid #eee;border-radius:14px;padding:12px;display:flex;flex-direction:column;gap:8px}' +
    '#tg-w .cform h4{margin:0;font-size:14px;font-weight:800}' +
    '#tg-w .cform .note{font-size:11px;color:var(--tgm);margin-top:-4px}' +
    '#tg-w .cform .row{display:flex;gap:8px}' +
    '#tg-w .cform input,#tg-w .cform textarea{width:100%;border:1px solid #e2e2e8;border-radius:10px;padding:9px 11px;font-size:13px;font-family:inherit;outline:none}' +
    '#tg-w .cform input:focus,#tg-w .cform textarea:focus{border-color:var(--tg)}' +
    '#tg-w .cform textarea{resize:vertical;min-height:64px}' +
    '#tg-w .cform .send{border:0;border-radius:999px;background:var(--tg);color:#fff;padding:10px;font-size:14px;font-weight:700;cursor:pointer}' +
    '#tg-w .cform .send[disabled]{opacity:.6;cursor:default}' +
    '#tg-w .cform .err{color:#d33;font-size:12px}' +
    '#tg-form{display:flex;gap:8px;padding:12px 14px;border-top:1px solid #eee;background:#fff}' +
    '#tg-form input{flex:1;border:1px solid #e2e2e8;border-radius:999px;padding:11px 16px;font-size:14px;outline:none}' +
    '#tg-form input:focus{border-color:var(--tg)}' +
    '#tg-form button{width:42px;height:42px;border-radius:50%;border:0;background:var(--tg);color:#fff;cursor:pointer;font-size:17px}' +
    '#tg-w .ft{text-align:center;font-size:10px;color:#b8b8c2;padding:6px}';

  var HTML =
    '<button id="tg-launch" aria-label="Open support chat" aria-expanded="false"><img alt="Chat" src="' + LOGO + '"></button>' +
    '<section id="tg-panel" role="dialog" aria-modal="false" aria-label="Tropical Glitz AI Support" data-open="false">' +
      '<div class="h"><div class="av"><img alt="" src="' + LOGO + '"></div><div><h2>Tropical Glitz AI Support</h2><span>Here to help you shop</span></div>' +
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

  var SHOP_DOMAIN = "7b297d-7a.myshopify.com", STORE_DOMAIN = "tropicalglitz.net";
  function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  // Convierte un subconjunto de markdown (enlaces y negrita) a HTML limpio, y
  // normaliza el dominio myshopify -> dominio propio para que el enlace sea corto.
  function mdToHtml(s) {
    s = esc(s);
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function (_m, t, u) {
      u = u.split(SHOP_DOMAIN).join(STORE_DOMAIN);
      return '<a href="' + u + '" target="_top" rel="noopener">' + t + "</a>";
    });
    // URLs "sueltas" (sin formato markdown) también se vuelven enlaces cortos.
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s)]+)/g, function (_m, pre, u) {
      var href = u.split(SHOP_DOMAIN).join(STORE_DOMAIN);
      var label = href.replace(/^https?:\/\//, "").replace(/\/$/, "");
      return pre + '<a href="' + href + '" target="_top" rel="noopener">' + label + "</a>";
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    return s;
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
    function isContactChip(t) { return /talk to a human|contact a representative|contact support/i.test(t); }
    function chips(list) { C.innerHTML = ""; (list || []).forEach(function (t) { var b = document.createElement("button"); b.className = "chip"; b.type = "button"; b.textContent = t; b.onclick = function () { if (isContactChip(t)) openContact(); else ask(t); }; C.appendChild(b); }); }

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
      if (!greeted) { greeted = true; add("m ai", "Welcome back! How may I be of service to you today?"); chips(["Any promotions?", "How much paint do I need?", "Talk to a human"]); }
    }
    function close() { P.dataset.open = "false"; L.setAttribute("aria-expanded", "false"); L.focus(); }

    // Formulario de contacto: el cliente deja sus datos + su pregunta y al enviar
    // se manda un correo a support@ con toda la conversación para que un humano responda.
    function openContact(prefill) {
      if (P.dataset.open !== "true") open();
      contactForm(prefill || "");
    }

    function contactForm(prefill) {
      var box = document.createElement("div");
      box.className = "cform";
      box.innerHTML =
        '<h4>Talk to a representative</h4>' +
        '<div class="note">Leave your details and our team will email you back — your chat is included automatically.</div>' +
        '<div class="row"><input id="cf-fn" placeholder="First name" autocomplete="given-name"><input id="cf-ln" placeholder="Last name" autocomplete="family-name"></div>' +
        '<input id="cf-ph" placeholder="Phone number" autocomplete="tel" inputmode="tel">' +
        '<input id="cf-em" type="email" placeholder="Email" autocomplete="email">' +
        '<textarea id="cf-msg" placeholder="How can we help?"></textarea>' +
        '<div class="err" id="cf-err" style="display:none"></div>' +
        '<button class="send" id="cf-send" type="button">Send</button>';
      log.appendChild(box);
      log.scrollTop = log.scrollHeight;
      if (prefill) box.querySelector("#cf-msg").value = prefill;
      box.querySelector("#cf-fn").focus();
      box.querySelector("#cf-send").onclick = function () { submitContact(box); };
    }

    function submitContact(box) {
      var send = box.querySelector("#cf-send");
      var errEl = box.querySelector("#cf-err");
      var fn = box.querySelector("#cf-fn").value.trim();
      var ln = box.querySelector("#cf-ln").value.trim();
      var ph = box.querySelector("#cf-ph").value.trim();
      var em = box.querySelector("#cf-em").value.trim();
      var msg = box.querySelector("#cf-msg").value.trim();
      function fail(t) { errEl.textContent = t; errEl.style.display = "block"; }
      errEl.style.display = "none";
      if (!fn) return fail("Please enter your first name.");
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(em)) return fail("Please enter a valid email.");
      if (!msg) return fail("Please tell us how we can help.");
      send.disabled = true; send.textContent = "Sending…";
      fetch(BACKEND + "/contact", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ first_name: fn, last_name: ln, phone: ph, email: em, message: msg, session_id: SID })
      })
        .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
        .then(function () {
          box.innerHTML = '<h4>Thanks, ' + esc(fn) + '!</h4><div class="note">We received your request along with your chat. A Tropical Glitz representative will email you at ' + esc(em) + ' shortly.</div>';
        })
        .catch(function () {
          send.disabled = false; send.textContent = "Send";
          fail("Sorry, we couldn't send that right now. Please email support@tropicalglitz.net or call 786-383-3013.");
        });
    }

    function ask(q) {
      if (!q || !q.trim()) return; C.innerHTML = ""; add("m u", q); T.value = "";
      var ty = add("ty"); ty.innerHTML = "<i></i><i></i><i></i>";
      fetch(BACKEND + "/chat", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ message: q, session_id: SID }) })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          ty.remove(); var ai = add("m ai", ""); var full = j.answer || ""; var words = full.split(" "); var i = 0;
          (function tick() {
            if (i < words.length) { i++; ai.innerHTML = mdToHtml(words.slice(0, i).join(" ")); log.scrollTop = log.scrollHeight; setTimeout(tick, 18); }
            else {
              ai.innerHTML = mdToHtml(full);
              renderProducts(j.products);
              chips(j.handoff ? ["Contact a representative", "Browse best sellers"] : ["Tell me more", "Any promotions?", "Talk to a human"]);
            }
          })();
        })
        .catch(function () { ty.remove(); add("m ai", "Sorry, something went wrong. Email support@tropicalglitz.net or call 786-383-3013."); });
    }
    L.onclick = function () { P.dataset.open === "true" ? close() : open(); };
    document.getElementById("tg-x").onclick = close;
    F.addEventListener("submit", function (e) { e.preventDefault(); ask(T.value); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && P.dataset.open === "true") close(); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
