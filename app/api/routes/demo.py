"""Página de demo del widget, servida same-origin desde el propio backend.

Permite probar la interfaz y el asistente en vivo SIN configurar Shopify:
- GET /demo        -> HTML con el widget flotante (mismo diseño que el App Block).
- GET /demo/chat   -> SSE (reusa rag.answer_query_stream). Same-origin => sin CORS.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services import rag

router = APIRouter(tags=["demo"])

PRIMARY = "#ef2c8f"

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tropical Glitz — Asistente (demo)</title>
<style>
 *{box-sizing:border-box} body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;background:#0e0e12;color:#fff}
 .hero{min-height:100vh;background:linear-gradient(160deg,#1a1024,#0e0e12);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px}
 .logo{font-weight:800;font-size:28px;letter-spacing:1px} .logo span{color:__P__}
 .hero p{opacity:.7;max-width:520px;line-height:1.5}
 .badge{margin-top:18px;font-size:13px;background:rgba(239,44,143,.15);color:__P__;border:1px solid __P__;border-radius:20px;padding:6px 14px}
 /* widget */
 .tg-launch{position:fixed;right:20px;bottom:20px;width:56px;height:56px;border:0;border-radius:50%;background:__P__;box-shadow:0 6px 20px rgba(0,0,0,.35);cursor:pointer;display:flex;align-items:center;justify-content:center;z-index:50}
 .tg-panel{position:fixed;right:20px;bottom:88px;width:380px;max-width:calc(100vw - 32px);height:560px;max-height:calc(100vh - 120px);background:#fff;color:#111;border-radius:18px;box-shadow:0 12px 40px rgba(0,0,0,.4);display:flex;flex-direction:column;overflow:hidden;z-index:50}
 .tg-head{background:__P__;color:#fff;padding:16px;display:flex;justify-content:space-between;align-items:flex-start}
 .tg-title{font-weight:700;font-size:15px}.tg-sub{font-size:12px;opacity:.9}
 .tg-close{background:transparent;border:0;color:#fff;font-size:22px;cursor:pointer}
 .tg-msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px;background:#faf7f9}
 .tg-msg{max-width:85%;padding:10px 12px;border-radius:14px;font-size:14px;line-height:1.45;white-space:pre-wrap;word-wrap:break-word}
 .tg-a{background:#f0eef0;align-self:flex-start;border-bottom-left-radius:4px}
 .tg-u{background:__P__;color:#fff;align-self:flex-end;border-bottom-right-radius:4px}
 .tg-msg a{color:__P__} .tg-msg strong{font-weight:700}
 .tg-typing{display:flex;gap:4px}.tg-typing span{width:6px;height:6px;border-radius:50%;background:#bbb;animation:tgb 1s infinite}.tg-typing span:nth-child(2){animation-delay:.2s}.tg-typing span:nth-child(3){animation-delay:.4s}@keyframes tgb{0%,60%,100%{opacity:.3}30%{opacity:1}}
 .tg-chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 14px 6px}
 .tg-chip{border:1px solid __P__;color:__P__;background:#fff;border-radius:16px;padding:6px 12px;font-size:13px;cursor:pointer}
 .tg-form{display:flex;gap:8px;padding:12px;border-top:1px solid #eee}
 .tg-input{flex:1;border:1px solid #ddd;border-radius:20px;padding:10px 14px;font-size:14px;outline:none}
 .tg-send{width:40px;height:40px;border:0;border-radius:50%;background:__P__;color:#fff;cursor:pointer}
</style></head><body>
<div class="hero">
  <div class="logo">TROPICAL <span>GLITZ</span></div>
  <p>Demo del asistente. Pulsa el botón rosa abajo a la derecha y pregúntale lo que un cliente preguntaría: cuánta pintura para un carro, candy sobre plata, envíos, etc.</p>
  <div class="badge">Backend en vivo · Gemini + Claude · 169 FAQs</div>
</div>
<button class="tg-launch" aria-label="Abrir chat">__ICON__</button>
<section class="tg-panel" role="dialog" aria-label="Tropical Glitz AI Support" hidden>
  <header class="tg-head"><div><div class="tg-title">Tropical Glitz AI Support</div><div class="tg-sub">Here to help you shop</div></div><button class="tg-close" aria-label="Cerrar">&times;</button></header>
  <div class="tg-msgs" aria-live="polite"></div>
  <div class="tg-chips"></div>
  <form class="tg-form"><input class="tg-input" type="text" placeholder="Type anything here..." maxlength="1000" autocomplete="off"><button class="tg-send" aria-label="Enviar">&#10148;</button></form>
</section>
<script>
 const $=s=>document.querySelector(s);
 const launch=$('.tg-launch'),panel=$('.tg-panel'),msgs=$('.tg-msgs'),chips=$('.tg-chips'),form=$('.tg-form'),input=$('.tg-input');
 const CHIPS=["How much paint do I need for a car?","Candy red over silver base?","Do you ship to Hawaii?","What flake sizes do you have?"];
 let greeted=false,es=null;
 function open(){panel.hidden=false;input.focus();if(!greeted){greeted=true;add('a','Welcome back! How may I be of service to you today?');renderChips();}}
 function close(){panel.hidden=true;}
 launch.onclick=()=>panel.hidden?open():close(); $('.tg-close').onclick=close;
 function renderChips(){chips.innerHTML='';CHIPS.forEach(c=>{const b=document.createElement('button');b.className='tg-chip';b.type='button';b.textContent=c;b.onclick=()=>send(c);chips.appendChild(b);});}
 function fmt(t){return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>').replace(/\\[([^\\]]+)\\]\\((https?:[^)]+)\\)/g,'<a href="$2" target="_blank">$1</a>');}
 function add(role,text){const el=document.createElement('div');el.className='tg-msg '+(role==='u'?'tg-u':'tg-a');el.innerHTML=role==='a'?fmt(text):text;msgs.appendChild(el);msgs.scrollTop=msgs.scrollHeight;return el;}
 function typing(){const t=document.createElement('div');t.className='tg-msg tg-a tg-typing';t.innerHTML='<span></span><span></span><span></span>';msgs.appendChild(t);msgs.scrollTop=msgs.scrollHeight;return t;}
 function send(text){text=(text||input.value).trim();if(!text)return;input.value='';chips.innerHTML='';add('u',text);const bubble=add('a','');bubble.style.display='none';const tp=typing();let raw='';if(es)es.close();
   es=new EventSource('/demo/chat?message='+encodeURIComponent(text));
   es.onmessage=e=>{let d;try{d=JSON.parse(e.data)}catch(_){return}
     if(d.type==='token'){tp.remove();bubble.style.display='';raw+=d.text;bubble.innerHTML=fmt(raw);}
     else if(d.type==='message'){tp.remove();bubble.style.display='';raw=d.text;bubble.innerHTML=fmt(raw);}
     else if(d.type==='done'){tp.remove();es.close();es=null;}
     msgs.scrollTop=msgs.scrollHeight;};
   es.onerror=()=>{tp.remove();if(!raw){bubble.style.display='';bubble.textContent='Sorry, something went wrong. Try again.';}if(es){es.close();es=null;}};}
 form.onsubmit=e=>{e.preventDefault();send();};
</script></body></html>"""


@router.get("/demo", response_class=HTMLResponse)
async def demo_page() -> HTMLResponse:
    icon = ('<svg width="26" height="26" viewBox="0 0 24 24" fill="white">'
            '<path d="M12 3C6.5 3 2 6.9 2 11.7c0 2.2 1 4.2 2.6 5.7L4 21l4.1-1.6c1.2.4 2.5.6 '
            '3.9.6 5.5 0 10-3.9 10-8.7S17.5 3 12 3z"/></svg>')
    html = _PAGE.replace("__P__", PRIMARY).replace("__ICON__", icon)
    return HTMLResponse(html)


@router.get("/demo/chat")
async def demo_chat(request: Request, session: AsyncSession = Depends(get_session)):
    message = (request.query_params.get("message") or "").strip()

    async def event_stream():
        if not message:
            yield 'data: {"type":"done"}\n\n'
            return
        async for chunk in rag.answer_query_stream(session, message):
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
