/* Tropical Glitz AI Assistant — production widget (Theme App Extension).
   - Chat con backend vía App Proxy SSE + cart-context.
   - Proactividad: 13 triggers detectados en el navegador → /apps/proactive.
   Sin dependencias. Estado mínimo en cookie de 1ª parte (returning). */
(function () {
  const root = document.getElementById("tg-assistant-root");
  if (!root) return;
  const cfg = {
    proxy: root.dataset.proxy || "/apps/assistant",
    proactiveUrl: (root.dataset.proxy || "/apps/assistant").replace(/\/assistant$/, "/proactive"),
    primary: root.dataset.primary || "#ef2c8f",
    title: root.dataset.title || "Tropical Glitz AI Support",
    subtitle: root.dataset.subtitle || "Here to help you shop",
    greeting: root.dataset.greeting || "Welcome back! How may I be of service to you today?",
    hasPurchased: root.dataset.hasPurchased === "1",
  };
  document.documentElement.style.setProperty("--tg-primary", cfg.primary);

  const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  root.insertAdjacentHTML("beforeend", `
    <button class="tg-launcher" id="tgL" aria-label="Open support chat" aria-expanded="false">🌴</button>
    <section class="tg-panel" id="tgP" role="dialog" aria-modal="false" aria-label="${esc(cfg.title)}" data-open="false">
      <header class="tg-header"><div class="tg-avatar">🌴</div>
        <div><h2>${esc(cfg.title)}</h2><span>${esc(cfg.subtitle)}</span></div>
        <button class="tg-close" id="tgX" aria-label="Close chat">✕</button></header>
      <div class="tg-log" id="tgLog" aria-live="polite"></div>
      <div class="tg-chips" id="tgC"></div>
      <form class="tg-input" id="tgF" autocomplete="off">
        <input id="tgT" type="text" placeholder="Type anything here..." aria-label="Message" />
        <button class="tg-send" type="submit" aria-label="Send">➤</button>
      </form>
      <div class="tg-foot">Powered by Tropical Glitz AI</div>
    </section>`);

  const P = document.getElementById("tgP"), L = document.getElementById("tgL"),
    log = document.getElementById("tgLog"), C = document.getElementById("tgC"),
    F = document.getElementById("tgF"), T = document.getElementById("tgT");
  let greeted = false, proactiveFired = false;

  const add = (cls, txt) => { const d = document.createElement("div"); d.className = cls; if (txt != null) d.textContent = txt; log.appendChild(d); log.scrollTop = log.scrollHeight; return d; };
  const chips = (list) => { C.innerHTML = ""; (list || []).forEach((t) => { const b = document.createElement("button"); b.className = "tg-chip"; b.type = "button"; b.textContent = t; b.onclick = () => (t.toLowerCase().includes("checkout") ? (location.href = "/checkout") : ask(t)); C.appendChild(b); }); };
  const productCard = (p) => { if (!p) return; const a = document.createElement("a"); a.className = "tg-msg ai tg-product"; a.href = p.url; a.textContent = `🛒 ${p.title} — ${p.currency || ""} ${p.price_min}`; log.appendChild(a); };

  function openPanel() { P.dataset.open = "true"; L.setAttribute("aria-expanded", "true"); T.focus();
    if (!greeted) { greeted = true; add("tg-msg ai", cfg.greeting); chips(["Any promotions?", "I'm not sure what to choose"]); } }
  function closePanel() { P.dataset.open = "false"; L.setAttribute("aria-expanded", "false"); L.focus(); }

  async function cartData() {
    try { const r = await fetch("/cart.js"); const c = await r.json();
      return { titles: (c.items || []).map((i) => i.product_title).slice(0, 5), total: (c.total_price || 0) / 100, count: c.item_count || 0 }; }
    catch (e) { return { titles: [], total: 0, count: 0 }; }
  }

  async function ask(q) {
    if (!q.trim()) return; C.innerHTML = "";
    add("tg-msg user", q); T.value = "";
    const typing = add("tg-typing"); typing.innerHTML = "<i></i><i></i><i></i>";
    try {
      const cart = await cartData();
      const ctx = cart.titles.length ? "cart:" + cart.titles.join(",") : "";
      const url = cfg.proxy + "?message=" + encodeURIComponent(q) + (ctx ? "&context=" + encodeURIComponent(ctx) : "");
      const res = await fetch(url); const reader = res.body.getReader(); const dec = new TextDecoder();
      typing.remove(); const ai = add("tg-msg ai", ""); let buf = "";
      for (;;) { const { value, done } = await reader.read(); if (done) break; buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n"); buf = parts.pop();
        for (const p of parts) { const ln = p.split("\n").find((l) => l.startsWith("data:")); if (!ln) continue;
          const e = JSON.parse(ln.slice(5).trim());
          if (e.type === "token" || e.type === "message") { ai.textContent += e.text; log.scrollTop = log.scrollHeight; }
          if (e.type === "done") { if (e.sources && e.sources.length) { const s = add("tg-sources");
            s.textContent = "Sources: " + e.sources.map((x) => x.source + ":" + (x.ref || "").slice(0, 26)).join(" · "); }
            if (e.handoff) chips(["Contact support", "Browse best sellers"]); } } }
    } catch (err) { typing.remove(); add("tg-msg ai", "Sorry, something went wrong. Email tropicalglitz@gmail.com or call 786-383-3013."); }
    finally { T.focus(); }
  }

  // ---------- Proactividad (13 triggers) ----------
  function pageType() {
    const p = location.pathname;
    if (p === "/" || p === "") return "home";
    if (p.includes("/products/")) return "product";
    if (p.includes("/collections/")) return "collection";
    if (p.startsWith("/cart")) return "cart";
    if (p.includes("/checkout")) return "checkout";
    return "other";
  }
  function isReturning() {
    const seen = document.cookie.includes("tg_seen=1");
    if (!seen) document.cookie = "tg_seen=1; max-age=2592000; path=/; samesite=lax";
    return seen;
  }

  async function fireProactive(signal) {
    if (proactiveFired || P.dataset.open === "true") return;
    proactiveFired = true;
    const cart = await cartData();
    const q = new URLSearchParams({
      page_type: pageType(), signal: signal || "idle",
      is_returning: isReturning() ? "1" : "0",
      has_purchased: cfg.hasPurchased ? "1" : "0",
      cart: cart.titles.join("|"), cart_total: String(cart.total),
    });
    // Producto / stock desde el objeto nativo de Shopify (fallback a data-attrs del tema)
    try {
      const meta = (window.ShopifyAnalytics && window.ShopifyAnalytics.meta) || {};
      if (meta.product) {
        q.set("product_title", meta.product.title || "");
        const anyAvail = (meta.product.variants || []).some((v) => v.available !== false);
        q.set("in_stock", anyAvail ? "1" : "0");
      }
    } catch (e) { /* noop */ }
    const prod = document.querySelector('[data-tg-product-title]');
    if (prod && !q.get("product_title")) { q.set("product_title", prod.dataset.tgProductTitle);
      q.set("in_stock", prod.dataset.tgInStock === "0" ? "0" : "1"); }
    try {
      const res = await fetch(cfg.proactiveUrl + "?" + q.toString());
      if (res.status !== 200) return;
      const data = await res.json();
      openPanel(); greeted = true;
      add("tg-msg ai", data.message);
      (data.products || []).forEach(productCard);
      chips(data.chips);
    } catch (e) { /* silencioso */ }
  }

  // Disparadores de señal
  let idleTimer = setTimeout(() => fireProactive(pageType() === "cart" ? "cart_view_idle" : "idle"), 12000);
  const resetIdle = () => { clearTimeout(idleTimer); idleTimer = setTimeout(() => fireProactive("idle"), 20000); };
  ["click", "keydown", "scroll", "mousemove"].forEach((ev) => window.addEventListener(ev, resetIdle, { passive: true }));
  // exit-intent (mouse sale por arriba)
  document.addEventListener("mouseout", (e) => { if (e.clientY <= 0) fireProactive("exit_intent"); });
  // add-to-cart: intercepta el POST a /cart/add
  const of = window.fetch;
  window.fetch = function (u, o) { const url = (u && u.url) || String(u);
    if (/\/cart\/add/.test(url)) { setTimeout(() => { proactiveFired = false; fireProactive("add_to_cart"); }, 600); }
    return of.apply(this, arguments); };
  document.addEventListener("submit", (e) => { const a = (e.target && e.target.action) || "";
    if (/\/cart\/add/.test(a)) setTimeout(() => { proactiveFired = false; fireProactive("add_to_cart"); }, 700); });

  L.onclick = () => (P.dataset.open === "true" ? closePanel() : openPanel());
  document.getElementById("tgX").onclick = closePanel;
  F.addEventListener("submit", (e) => { e.preventDefault(); ask(T.value); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && P.dataset.open === "true") closePanel(); });
})();
