"""
Vlastní HTML stránka pro /docs — nahrazuje výchozí Swagger UI.

Vizuálně identická s nakup-porovnavac.tsx, ale ceny a produkty se ŽIVĚ
natahují z reálného API tohoto serveru (/porovnat, /obchody) — tedy
ze skutečných dat nascrapovaných z letáků pomocí scraper.py, ne z
natvrdo zadaných ukázkových čísel.

Vizuální metadata obchodů (barva, logo, odhad adresy/vzdálenosti) jsou
v JS proměnné STORE_META — scraper tohle nesbírá, takže to zůstává
natvrdo zadané a klidně si to uprav podle skutečné polohy svých prodejen.

  - tmavý navy gradient header
  - taby: 📝 Seznam · 📊 Ceny · ✅ Výsledek
  - AI input s voláním Anthropic API (parsování seznamu, ne cen)
  - rychlé přidání chipů
  - bar-chart porovnání cen (živá data z /porovnat)
  - optimalizované výsledky s trasou a úsporou (živá data)

Použití ve FastAPI:
    from docs_page import DOCS_HTML
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    app = FastAPI(docs_url=None)

    @app.get("/docs", include_in_schema=False)
    async def custom_docs():
        return HTMLResponse(DOCS_HTML)
"""

DOCS_HTML = r"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NejlevnějšíNákup — Porovnávač cen letáků</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Inter',system-ui,sans-serif;background:#f0f4f8;color:#1a1a2e;min-height:100vh;}

.hdr{background:linear-gradient(135deg,#1a1a2e 0%,#16213e 60%,#0f3460 100%);padding:18px 16px 0;color:#fff;}
.hdr-inner{max-width:500px;margin:0 auto;}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:4px;}
.logo-icon{font-size:26px;}
.logo-t{font-size:20px;font-weight:800;letter-spacing:-0.5px;}
.logo-s{font-size:11px;color:#94a3b8;margin-top:-2px;}
.logo-fresh{font-size:10px;color:#64748b;margin-top:3px;}
.tabs{display:flex;margin-top:14px;gap:3px;}
.tab{flex:1;padding:9px 2px;border:none;background:transparent;color:#94a3b8;font-weight:400;font-size:11px;border-radius:8px 8px 0 0;cursor:pointer;font-family:inherit;}
.tab.active{background:#fff;color:#1a1a2e;font-weight:700;}

.content{max-width:500px;margin:0 auto;padding:14px 14px 40px;}

.card{background:#fff;border-radius:16px;padding:16px;margin-bottom:14px;box-shadow:0 2px 12px rgba(0,0,0,0.06);}
.card-ai{border:2px solid #e0e7ff;background:linear-gradient(135deg,#fafbff,#f0f4ff);}
.lbl{font-size:12px;font-weight:700;color:#64748b;margin-bottom:10px;text-transform:uppercase;letter-spacing:0.5px;}

textarea{width:100%;padding:10px 12px;border-radius:10px;border:2px solid #e0e7ff;font-size:14px;resize:none;outline:none;font-family:inherit;}
textarea:focus{border-color:#6366f1;}
.ai-hint{font-size:12px;color:#6366f1;margin-bottom:10px;font-style:italic;}
.ai-err{color:#ef4444;font-size:12px;margin-top:6px;}

button{font-family:inherit;cursor:pointer;}
.btn{border:none;border-radius:12px;font-weight:700;font-size:14px;padding:12px 20px;color:#fff;}
.btn-ai{background:linear-gradient(135deg,#6366f1,#4f46e5);width:100%;font-size:15px;margin-top:10px;box-shadow:0 4px 16px rgba(99,102,241,0.3);}
.btn-ai:disabled{background:#94a3b8;box-shadow:none;cursor:not-allowed;}
.btn-green{background:linear-gradient(135deg,#22c55e,#16a34a);width:100%;font-size:16px;box-shadow:0 4px 20px rgba(34,197,94,0.3);}
.btn-green:disabled{background:#94a3b8;box-shadow:none;cursor:not-allowed;}
.btn-white{background:#fff;color:#374151;border:2px solid #e2e8f0;width:100%;font-size:15px;}
.btn-navy{background:#1a1a2e;color:#fff;padding:10px 18px;border-radius:10px;font-size:13px;}

.chips{display:flex;flex-wrap:wrap;gap:7px;}
.chip{padding:6px 12px;border-radius:20px;border:2px solid #e2e8f0;background:#fff;color:#374151;font-size:13px;font-weight:600;cursor:pointer;}
.chip.on{border-color:#22c55e;background:#f0fdf4;color:#16a34a;cursor:default;}

.row{display:flex;align-items:center;gap:8px;padding:10px 0;border-bottom:1px solid #f1f5f9;}
.row:last-child{border-bottom:none;}
.qty-btn{width:28px;height:28px;border-radius:8px;border:2px solid #e2e8f0;background:#fff;font-weight:700;font-size:15px;}
.rm-btn{color:#ef4444;background:none;border:none;font-size:18px;}

.empty-state{text-align:center;padding:40px 0;color:#94a3b8;}
.empty-icon{font-size:44px;margin-bottom:10px;}

/* bar chart */
.bar-row{display:flex;align-items:center;gap:8px;margin-bottom:7px;}
.store-tag{width:62px;text-align:center;display:inline-block;padding:2px 7px;border-radius:20px;font-size:10px;font-weight:700;flex-shrink:0;}
.store-tag.lg{min-width:72px;font-size:12px;padding:5px 12px;}
.bar-track{flex:1;background:#f1f5f9;border-radius:20px;height:24px;overflow:hidden;}
.bar-fill{height:100%;border-radius:20px;display:flex;align-items:center;justify-content:flex-end;padding-right:8px;color:#fff;font-size:11px;font-weight:800;transition:width .3s;white-space:nowrap;}
.akce{font-size:9px;background:#fef2f2;color:#dc2626;padding:2px 5px;border-radius:8px;font-weight:700;flex-shrink:0;}
.from-price{font-size:12px;color:#22c55e;font-weight:700;background:#f0fdf4;padding:2px 10px;border-radius:20px;}
.prod-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;}
.prod-name{font-weight:700;font-size:15px;text-transform:capitalize;}

/* výsledky */
.savings-box{background:linear-gradient(135deg,#065f46,#047857);border-radius:16px;padding:20px;color:#fff;text-align:center;margin-bottom:14px;}
.savings-sub{font-size:12px;opacity:.8;margin-bottom:4px;}
.savings-amt{font-size:44px;font-weight:900;letter-spacing:-2px;}
.savings-info{font-size:13px;opacity:.8;margin-top:4px;}

.route-row{display:flex;align-items:center;flex-wrap:wrap;gap:6px;}
.route-pin{width:34px;height:34px;border-radius:50%;background:#f1f5f9;display:flex;align-items:center;justify-content:center;font-size:16px;}
.route-store-ball{width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:16px;border:2px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,0.15);}
.route-arrow{color:#cbd5e1;font-size:16px;}
.route-name{font-size:11px;font-weight:700;}
.route-dist{font-size:10px;color:#94a3b8;}

.store-card{background:#fff;border-radius:16px;overflow:hidden;margin-bottom:14px;box-shadow:0 2px 12px rgba(0,0,0,0.06);}
.store-hdr{padding:14px 16px;display:flex;justify-content:space-between;align-items:center;}
.store-hdr-left{display:flex;align-items:center;gap:8px;}
.store-emoji{font-size:22px;}
.store-hdr-name{font-weight:800;font-size:16px;}
.store-hdr-addr{font-size:12px;opacity:.8;}
.store-total{font-size:20px;font-weight:900;}
.store-count{font-size:11px;opacity:.8;text-align:right;}
.store-items{padding:6px 16px;}
.item-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #f8fafc;}
.item-row:last-child{border-bottom:none;}
.item-name{font-weight:600;font-size:14px;text-transform:capitalize;}
.item-desc{font-size:12px;color:#94a3b8;}
.item-price{font-weight:700;font-size:15px;text-align:right;}
.item-akce{font-size:10px;color:#dc2626;font-weight:700;}
</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-inner">
    <div class="logo">
      <span class="logo-icon">🛒</span>
      <div>
        <div class="logo-t">NejlevnějšíNákup</div>
        <div class="logo-s">Porovnání cen z letáků · AI rozpoznávání</div>
        <div class="logo-fresh" id="freshness">🕒 Načítám stav dat...</div>
      </div>
    </div>
    <div class="tabs">
      <button class="tab active" id="tab-btn-seznam" onclick="switchTab('seznam')">📝 Seznam</button>
      <button class="tab" id="tab-btn-porovnani" onclick="switchTab('porovnani')">📊 Ceny</button>
      <button class="tab" id="tab-btn-vysledky" onclick="switchTab('vysledky')">✅ Výsledek</button>
    </div>
  </div>
</div>

<div class="content">
  <div id="tab-seznam"></div>
  <div id="tab-porovnani" style="display:none"></div>
  <div id="tab-vysledky" style="display:none"></div>
</div>

<script>
// ---- konfigurace ----
// Vizuální metadata obchodů (barva/logo/odhad adresy a vzdálenosti).
// Ceny a názvy produktů jdou ŽIVĚ z API (/porovnat, /obchody) — tohle jsou
// jen kosmetické údaje, které scraper nesbírá. Pokud chceš přesnou adresu
// a vzdálenost své konkrétní pobočky, uprav je tady.
const STORE_META = {
  lidl:     {name:"Lidl",     color:"#FFD700", textColor:"#002f6c", logo:"🟡", vzdalenost:0.3,  adresa:"nejbližší pobočka"},
  kaufland: {name:"Kaufland", color:"#E30613", textColor:"#ffffff", logo:"🔴", vzdalenost:0.35, adresa:"nejbližší pobočka"},
  albert:   {name:"Albert",   color:"#004A99", textColor:"#ffffff", logo:"🔵", vzdalenost:0.8,  adresa:"nejbližší pobočka"},
};
function storeMeta(id){
  return STORE_META[id] || {name:id, color:"#94a3b8", textColor:"#ffffff", logo:"🏬", vzdalenost:9, adresa:""};
}

// Obecné hledané výrazy pro rychlé přidání a pro AI parsování.
// Jde o LIKE-hledání v reálných názvech produktů z letáků (např. "mléko"
// najde "Mléko Olma 1,5%" i "Mléko Kunín 1,5%" napříč obchody).
const VSECHNY = ["mléko","rohlíky","máslo","jogurt","pivo","prací prášek","chleba","sýr",
  "jablka","banány","kuře","rajčata","brambory","vejce","rýže","těstoviny"];

let seznam = [];
let vysledky = null;
let aiLoading = false;
let porovnaniLoading = false;
let porovnaniCache = null;       // poslední odpověď z /porovnat, ať se nevolá API znovu zbytečně
let porovnaniCacheKey = null;

// ---- utils ----
function esc(s){ return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

async function fetchPorovnani(terms){
  const key = terms.join(",");
  if(porovnaniCache && porovnaniCacheKey===key) return porovnaniCache;
  const res = await fetch('/porovnat?produkty=' + encodeURIComponent(key));
  if(!res.ok) throw new Error('API odpovědělo ' + res.status);
  const data = await res.json();
  porovnaniCache = data;
  porovnaniCacheKey = key;
  return data;
}

async function loadFreshness(){
  const el = document.getElementById('freshness');
  if(!el) return;
  try{
    const res = await fetch('/obchody');
    if(!res.ok) throw new Error('status ' + res.status);
    const data = await res.json();
    if(!data.length){
      el.textContent = '⚠️ Databáze je prázdná — spusť scraper.py';
      return;
    }
    const parts = data.map(o=>{
      const meta = storeMeta(o.obchod);
      const dt = new Date(o.posledni_update);
      const dny = Math.floor((Date.now()-dt.getTime())/86400000);
      const kdy = isNaN(dny) ? '?' : (dny<=0 ? 'dnes' : dny+'d zpět');
      return `${meta.name} ${kdy}`;
    });
    el.textContent = '🕒 ' + parts.join(' · ');
  } catch(e){
    el.textContent = '⚠️ Nepodařilo se ověřit stav dat z API';
  }
}

function optimalizuj(seznamItems, data){
  const obchody = {};
  seznamItems.forEach(({nazev,mnozstvi})=>{
    const matches = data[nazev] || [];
    if(!matches.length) return;
    const best = matches.reduce((a,b)=> a.cena<b.cena ? a : b);
    const id = best.obchod;
    if(!obchody[id]) obchody[id] = {obchod: storeMeta(id), id, produkty:[], celkem:0};
    obchody[id].produkty.push({produkt:nazev, nazev:best.nazev, cena:best.cena, akce:!!best.akce, jednotka:best.jednotka||'', mnozstvi});
    obchody[id].celkem += best.cena*mnozstvi;
  });
  return Object.entries(obchody).sort(([,a],[,b])=>a.obchod.vzdalenost-b.obchod.vzdalenost);
}

function celkovaCena(opt){ return opt.reduce((s,[,d])=>s+d.celkem,0); }

function nejdrazsi(seznamItems, data){
  return seznamItems.reduce((sum,{nazev,mnozstvi})=>{
    const matches = data[nazev]||[];
    if(!matches.length) return sum;
    const mx = matches.reduce((m,x)=>Math.max(m,x.cena),0);
    return sum + mx*mnozstvi;
  },0);
}

// ---- render ----
function render(){
  renderSeznam();
  renderPorovnani();
  renderVysledky();
}

function switchTab(name){
  ['seznam','porovnani','vysledky'].forEach(t=>{
    document.getElementById('tab-'+t).style.display = t===name?'':'none';
    document.getElementById('tab-btn-'+t).classList.toggle('active',t===name);
  });
  render();
}

function renderSeznam(){
  const el = document.getElementById('tab-seznam');
  let html = '';

  // AI card
  html += `<div class="card card-ai">
    <div class="lbl">✨ Napiš seznam přirozeně – AI to pochopí</div>
    <div class="ai-hint">Zkus: „kilo jablek, 2 mléka, prací prášek a nějaké to pivo"</div>
    <textarea id="ai-input" rows="3" placeholder="Napiš sem svůj nákupní seznam..." oninput="document.getElementById('ai-err').textContent=''">${''}</textarea>
    <div class="ai-err" id="ai-err"></div>
    <button class="btn btn-ai" id="ai-btn" onclick="parseAI()" ${aiLoading?'disabled':''}>
      ${aiLoading?'⏳ AI zpracovává...':'✨ Přidat pomocí AI'}
    </button>
  </div>`;

  // Rychlé přidání
  html += `<div class="card"><div class="lbl">Rychlé přidání</div><div class="chips">`;
  VSECHNY.forEach(p=>{
    const on = seznam.find(s=>s.nazev===p);
    html += `<button class="chip${on?' on':''}" onclick="${on?`odeberProdukt('${p}')`:`pridejProdukt('${p}')`}">${on?'✓ ':''}${esc(p)}</button>`;
  });
  html += `</div></div>`;

  // Aktuální seznam
  if(seznam.length>0){
    html += `<div class="card"><div class="lbl">Váš seznam (${seznam.length} položek)</div>`;
    seznam.forEach(({nazev,mnozstvi})=>{
      html += `<div class="row">
        <span style="flex:1;font-weight:600;font-size:15px;text-transform:capitalize">${esc(nazev)}</span>
        <div style="display:flex;align-items:center;gap:6px">
          <button class="qty-btn" onclick="zmenMnozstvi('${nazev}',-1)">−</button>
          <span style="width:22px;text-align:center;font-weight:700">${mnozstvi}</span>
          <button class="qty-btn" onclick="zmenMnozstvi('${nazev}',1)">+</button>
        </div>
        <button class="rm-btn" onclick="odeberProdukt('${nazev}')">✕</button>
      </div>`;
    });
    html += `</div>`;
  }

  // Akce
  if(seznam.length>0){
    html += `<button class="btn btn-green" id="porovnej-btn" onclick="porovnej()" ${porovnaniLoading?'disabled':''}>
      ${porovnaniLoading?'⏳ Hledám nejlepší ceny...':`🔍 Porovnat ${seznam.length} položek`}
    </button>`;
  } else {
    html += `<div class="empty-state"><div class="empty-icon">🛒</div><div style="font-weight:600">Prázdný seznam</div><div style="font-size:13px;margin-top:4px">Napiš seznam výše nebo přidej produkty tlačítky</div></div>`;
  }

  el.innerHTML = html;
}

async function renderPorovnani(){
  const el = document.getElementById('tab-porovnani');
  if(seznam.length===0){
    el.innerHTML=`<div class="empty-state"><div class="empty-icon">📊</div><div style="font-weight:600">Nejdřív přidej produkty do seznamu</div></div>`;
    return;
  }
  el.innerHTML = `<div class="empty-state"><div class="empty-icon">⏳</div><div style="font-weight:600">Načítám aktuální ceny z API...</div></div>`;

  const terms = seznam.map(p=>p.nazev);
  let data;
  try{
    data = await fetchPorovnani(terms);
  } catch(e){
    el.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><div style="font-weight:600">Nepodařilo se načíst ceny z API</div></div>`;
    return;
  }

  let html='';
  terms.forEach(nazev=>{
    const matches = data[nazev] || [];
    if(!matches.length){
      html += `<div class="card"><div class="prod-head"><span class="prod-name">${esc(nazev)}</span></div>
        <div style="color:#94a3b8;font-size:13px">Nenalezeno v žádném aktuálním letáku</div></div>`;
      return;
    }
    // nejlevnější nález pro každý obchod
    const byStore = {};
    matches.forEach(m=>{
      if(!byStore[m.obchod] || m.cena < byStore[m.obchod].cena) byStore[m.obchod] = m;
    });
    const entries = Object.entries(byStore)
      .map(([obchod,item])=>({obchod,item}))
      .sort((a,b)=>a.item.cena-b.item.cena);
    const max = entries[entries.length-1].item.cena;

    html += `<div class="card">
      <div class="prod-head">
        <span class="prod-name">${esc(nazev)}</span>
        ${entries[0]?`<span class="from-price">od ${entries[0].item.cena.toFixed(2)} Kč</span>`:''}
      </div>`;
    entries.forEach(({obchod,item},i)=>{
      const meta = storeMeta(obchod);
      const color = i===0?'#22c55e':(i===entries.length-1?'#ef4444':'#94a3b8');
      const w = Math.max((item.cena/max)*100,18);
      html += `<div class="bar-row">
        <span class="store-tag" style="background:${meta.color};color:${meta.textColor}">${esc(meta.name)}</span>
        <div class="bar-track"><div class="bar-fill" style="width:${w}%;background:${color}">${item.cena.toFixed(2)}</div></div>
        ${item.akce?'<span class="akce">AKCE</span>':''}
      </div>`;
    });
    html += `</div>`;
  });
  el.innerHTML = html;
}

function renderVysledky(){
  const el = document.getElementById('tab-vysledky');
  if(!vysledky){
    el.innerHTML=`<div class="empty-state"><div class="empty-icon">✅</div><div style="font-weight:600;margin-bottom:12px">Nejdřív porovnej ceny</div><button class="btn btn-navy" onclick="switchTab('seznam')">Zpět na seznam</button></div>`;
    return;
  }
  const optimum = vysledky.optimum;
  if(!optimum.length){
    el.innerHTML = `<div class="empty-state"><div class="empty-icon">😕</div><div style="font-weight:600;margin-bottom:12px">Žádný z produktů nebyl nalezen v aktuálních letácích</div><button class="btn btn-navy" onclick="switchTab('seznam')">Zpět na seznam</button></div>`;
    return;
  }
  const usetris = (vysledky.nejdrazsiCelkem-celkovaCena(optimum)).toFixed(2);
  let html='';

  // Úspora
  html += `<div class="savings-box">
    <div class="savings-sub">Ušetříš oproti nákupu vše nejdráže</div>
    <div class="savings-amt">${parseFloat(usetris)>0?usetris+' Kč':'0 Kč'}</div>
    <div class="savings-info">Zaplatíš: <strong>${celkovaCena(optimum).toFixed(2)} Kč</strong> · ${optimum.length} ${optimum.length===1?'obchod':optimum.length<5?'obchody':'obchodů'}</div>
  </div>`;

  // Trasa
  html += `<div class="card"><div class="lbl">🗺️ Doporučená trasa</div><div class="route-row">
    <div style="display:flex;align-items:center;gap:6px">
      <div class="route-pin">📍</div>
      <div style="font-size:11px;color:#94a3b8;font-weight:600">Vy</div>
    </div>`;
  optimum.forEach(([id,data])=>{
    html += `<div style="display:flex;align-items:center;gap:4px">
      <span class="route-arrow">→</span>
      <div style="display:flex;align-items:center;gap:6px">
        <div class="route-store-ball" style="background:${data.obchod.color}">${data.obchod.logo}</div>
        <div>
          <div class="route-name" style="color:${data.obchod.color}">${esc(data.obchod.name)}</div>
          <div class="route-dist">${data.obchod.vzdalenost} km</div>
        </div>
      </div>
    </div>`;
  });
  html += `</div></div>`;

  // Obchody
  optimum.forEach(([id,data])=>{
    html += `<div class="store-card">
      <div class="store-hdr" style="background:${data.obchod.color};color:${data.obchod.textColor}">
        <div class="store-hdr-left">
          <span class="store-emoji">${data.obchod.logo}</span>
          <div>
            <div class="store-hdr-name">${esc(data.obchod.name)}</div>
            <div class="store-hdr-addr">📍 ${esc(data.obchod.adresa)} · ${data.obchod.vzdalenost} km</div>
          </div>
        </div>
        <div>
          <div class="store-total">${data.celkem.toFixed(2)} Kč</div>
          <div class="store-count">${data.produkty.length} pol.</div>
        </div>
      </div>
      <div class="store-items">`;
    data.produkty.forEach(({produkt,nazev,cena,akce,jednotka,mnozstvi})=>{
      html += `<div class="item-row">
        <div>
          <div class="item-name">${esc(produkt)} ×${mnozstvi}</div>
          <div class="item-desc">${esc(nazev)} · ${esc(jednotka)}</div>
        </div>
        <div>
          <div class="item-price">${(cena*mnozstvi).toFixed(2)} Kč</div>
          ${akce?'<div class="item-akce">🏷️ AKCE</div>':''}
        </div>
      </div>`;
    });
    html += `</div></div>`;
  });

  html += `<button class="btn btn-white" onclick="novyNakup()">🔄 Nový nákup</button>`;
  el.innerHTML = html;
}

// ---- akce ----
function pridejProdukt(nazev){
  if(!seznam.find(p=>p.nazev===nazev)) seznam.push({nazev,mnozstvi:1});
  renderSeznam();
}
function odeberProdukt(nazev){
  seznam = seznam.filter(p=>p.nazev!==nazev);
  renderSeznam();
  renderPorovnani();
}
function zmenMnozstvi(nazev,delta){
  seznam = seznam.map(p=>p.nazev===nazev?{...p,mnozstvi:Math.max(1,p.mnozstvi+delta)}:p);
  renderSeznam();
}
function novyNakup(){
  seznam=[]; vysledky=null;
  switchTab('seznam');
}

async function porovnej(){
  porovnaniLoading=true;
  renderSeznam();
  const terms = seznam.map(p=>p.nazev);
  try{
    const data = await fetchPorovnani(terms);
    const optimum = optimalizuj(seznam, data);
    const nejdrazsiCelkem = nejdrazsi(seznam, data);
    vysledky = {optimum, nejdrazsiCelkem};
  } catch(e){
    porovnaniLoading=false;
    renderSeznam();
    const errEl = document.getElementById('ai-err');
    if(errEl) errEl.textContent = 'Nepodařilo se načíst ceny z API. Zkus to znovu.';
    return;
  }
  porovnaniLoading=false;
  switchTab('vysledky');
}

async function parseAI(){
  const input = document.getElementById('ai-input').value.trim();
  if(!input) return;
  aiLoading=true;
  renderSeznam();
  try{
    const systemPrompt = `Jsi pomocník pro nákupní seznam. Uživatel ti napíše seznam nákupu v přirozeném jazyce (česky). Tvým úkolem je extrahovat jednotlivé produkty a jejich množství.

Dostupné produkty v databázi (používej PŘESNĚ tyto názvy):
${VSECHNY.join(", ")}

Odpověz POUZE validním JSON polem, žádný text navíc, žádné markdown backticky. Formát:
[{"nazev": "mléko", "mnozstvi": 2}, {"nazev": "rohlíky", "mnozstvi": 4}]

Pokud produkt není v databázi, zkus ho namapovat na nejbližší dostupný. Pokud opravdu nic neodpovídá, vynech ho.
Pokud není uvedeno množství, použij 1.`;

    const res = await fetch("https://api.anthropic.com/v1/messages",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        model:"claude-sonnet-4-6",
        max_tokens:1000,
        system:systemPrompt,
        messages:[{role:"user",content:input}]
      })
    });
    const data = await res.json();
    const text = data.content?.map(b=>b.text||"").join("").trim();
    const clean = text.replace(/```json|```/g,"").trim();
    const parsed = JSON.parse(clean);
    parsed.forEach(({nazev,mnozstvi})=>{
      if(nazev&&VSECHNY.includes(nazev)){
        const ex=seznam.find(p=>p.nazev===nazev);
        if(ex) ex.mnozstvi+=mnozstvi||1;
        else seznam.push({nazev,mnozstvi:mnozstvi||1});
      }
    });
  } catch(e){
    setTimeout(()=>{const el=document.getElementById('ai-err');if(el)el.textContent='Nepodařilo se zpracovat seznam. Zkuste to znovu.';},0);
  }
  aiLoading=false;
  renderSeznam();
}

// init
render();
loadFreshness();
</script>
</body>
</html>
"""
