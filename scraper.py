"""
Leták scraper - vytahuje produkty a ceny z letáků obchodů (Albert, Lidl, Kaufland)
a uloží je do SQLite databáze.

DVĚ STRATEGIE EXTRAKCE (viz funkce níže):
1. Z PDF letáku (preferované, levnější, přesnější) - pokud obchod nabízí
   leták ke stažení jako PDF (Lidl to dělá), vytáhneme z něj text pomocí
   pdftotext a strukturu mu dá AI textový model (levný, žádné vision tokeny).
2. Z obrázků webu (fallback) - pro obchody bez PDF letáku stáhneme obrázky
   stránek letáku z webu a použijeme AI vision model.

Spouštět jednou denně / týdně (letáky se měnit ~1x týdně).

POUŽITÍ:
    export OPENROUTER_API_KEY="sk-or-..."
    python scraper.py

KONFIGURACE:
    Uprav OBCHODY níže - každý obchod má vlastní extrakční funkci.
"""

import os
import json
import base64
import sqlite3
import time
import re
from datetime import datetime, timezone
from io import BytesIO
import requests
from PIL import Image

# ====================== KONFIGURACE ======================

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Levný vision model - dobrý poměr cena/přesnost pro čtení letáků z obrázků.
VISION_MODEL = "google/gemini-2.0-flash-001"

# Levný TEXTOVÝ model - pro letáky dostupné jako PDF s textovou vrstvou
TEXT_MODEL = "google/gemini-2.0-flash-001"

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "ceny.db")

# Max šířka obrázku v px před odesláním do AI
MAX_IMAGE_WIDTH = 1400

# ====================== PROMPTY PRO AI ======================

TEXT_EXTRACTION_PROMPT = """Jsi expert na čtení reklamních letáků z českých supermarketů.

Níže je text vytažený z jedné strany PDF letáku nástrojem pdftotext. Text může
být "rozsypaný" - čísla a popisky nejsou nutně ve stejném pořadí jako vizuálně
na letáku, protože extrakce nezachovává přesné prostorové rozmístění.

Tvým úkolem je z tohoto textu rekonstruovat VŠECHNY produkty s jejich cenami.

Typický vzor na letáku: název produktu + hmotnost/objem balení + sleva (%) +
přeškrtnutá původní cena + nová akční cena (velkým písmem) + případně cena za
měrnou jednotku (Kč/kg, Kč/l...) jako pomocný údaj.

Pro každý produkt vrať:
- nazev: název produktu (např. "PILOS Balkánský sýr")
- cena: konečná/akční cena jako číslo s desetinnou tečkou (např. "29.90" -> 29.90,
  "89.90" -> 89.90). Pokud vidíš víc čísel u produktu, ber tu zvýrazněnou/finální
  cenu za celé balení, NE cenu za měrnou jednotku (tu s "Kč/kg" nebo "1 kg =").
- jednotka: hmotnost/objem/počet balení (např. "100g", "3x200g", "kus", "30 kusů")
- puvodni_cena: přeškrtnutá cena před slevou, pokud je uvedená, jinak null
- akce: true pokud je u produktu vidět "%", "Ušetřete", přeškrtnutá cena, nebo
  podobný slevový prvek, jinak false

Vrať VÝHRADNĚ validní JSON (žádný text okolo, žádné markdown bloky):
{"produkty": [{"nazev": "...", "cena": 0.0, "jednotka": "...", "puvodni_cena": null, "akce": false}]}

Ignoruj texty jako "Více na www...", "Nabídka zboží platí od...", loga, právní
poznámky o tisku - to nejsou produkty. Pokud si u nějakého čísla nejsi jistý, ke
kterému produktu patří, raději produkt vynech, než abys hádal špatně.

"""

EXTRACTION_PROMPT = """Jsi expert na čtení reklamních letáků z českých supermarketů.

Na obrázku je strana letáku obchodu. Tvým úkolem je vytáhnout VŠECHNY produkty
a jejich ceny, které na letáku vidíš.

Pro každý produkt vrať:
- nazev: celý název produktu tak, jak je na letáku (např. "Pilsner Urquell 8pack 8×0,5l")
- cena: konečná/akční cena jako číslo (desetinná čísla s tečkou, např. 199.00). Pokud je cena
  uvedená jako "229,-" je to 229.00. Pokud vidíš cenu za jednotku (např. "28,63 Kč/0,5l"),
  použij celkovou cenu balení, ne cenu za jednotku.
- jednotka: jednotka/množství balení (např. "250g", "1l", "8×0,5l", "1ks", "1kg")
- puvodni_cena: pokud je vedle vidět přeškrtnutá/původní cena, ulož ji jako číslo, jinak null
- akce: true pokud je produkt zlevněný (má slevovou bublinu, %, přeškrtnutou cenu), jinak false

Vrať VÝHRADNĚ validní JSON ve tvaru (žádný text okolo, žádné markdown bloky):
{"produkty": [{"nazev": "...", "cena": 0.0, "jednotka": "...", "puvodni_cena": null, "akce": false}]}

Pokud na obrázku nepoznáš žádný produkt nebo cenu, vrať {"produkty": []}.
Nepřidávej žádné produkty, které na obrázku nejsou - nehádej, nedoplňuj.

"""

# ====================== DATABÁZE ======================

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS produkty (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            obchod TEXT NOT NULL,
            nazev TEXT NOT NULL,
            cena REAL NOT NULL,
            jednotka TEXT,
            puvodni_cena REAL,
            akce INTEGER DEFAULT 0,
            platnost_od TEXT,
            platnost_do TEXT,
            scraped_at TEXT NOT NULL,
            zdroj_url TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scrape_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            obchod TEXT NOT NULL,
            spusteno_at TEXT NOT NULL,
            pocet_produktu INTEGER,
            status TEXT,
            chyba TEXT
        )
    """)
    conn.commit()
    return conn

def ulozit_produkty(conn, obchod, produkty, zdroj_url, platnost_od=None, platnost_do=None):
    now = datetime.now(timezone.utc).isoformat()
    for p in produkty:
        conn.execute(
            """INSERT INTO produkty
               (obchod, nazev, cena, jednotka, puvodni_cena, akce, platnost_od, platnost_do, scraped_at, zdroj_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                obchod,
                p.get("nazev"),
                p.get("cena"),
                p.get("jednotka"),
                p.get("puvodni_cena"),
                1 if p.get("akce") else 0,
                platnost_od,
                platnost_do,
                now,
                zdroj_url,
            ),
        )
    conn.commit()

def smazat_stara_data(conn, obchod):
    """Před novým scrapem smaž stará data daného obchodu."""
    conn.execute("DELETE FROM produkty WHERE obchod = ?", (obchod,))
    conn.commit()

# ====================== AI EXTRAKCE ======================

def zmensit_obrazek(image_bytes: bytes) -> bytes:
    img = Image.open(BytesIO(image_bytes))
    if img.width > MAX_IMAGE_WIDTH:
        ratio = MAX_IMAGE_WIDTH / img.width
        img = img.resize((MAX_IMAGE_WIDTH, int(img.height * ratio)))
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    return buf.getvalue()

def extrahovat_produkty_z_obrazku(image_bytes: bytes, max_retries=3) -> list:
    image_bytes = zmensit_obrazek(image_bytes)
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": EXTRACTION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": 4000,
    }
    for attempt in range(max_retries):
        try:
            resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            text = text.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            parsed = json.loads(text)
            return parsed.get("produkty", [])
        except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"  [pokus {attempt + 1}/{max_retries}] chyba při extrakci: {e}")
            time.sleep(2 ** attempt)
    print("  AI extrakce selhala po všech pokusech, vracím prázdný seznam.")
    return []

def extrahovat_produkty_z_textu(text: str, max_retries=3) -> list:
    if not text.strip():
        return []
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": TEXT_MODEL,
        "messages": [
            {
                "role": "user",
                "content": f"{TEXT_EXTRACTION_PROMPT}\n\n---TEXT STRANY LETÁKU---\n{text}",
            }
        ],
        "temperature": 0,
        "max_tokens": 4000,
    }
    for attempt in range(max_retries):
        try:
            resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            parsed = json.loads(content)
            return parsed.get("produkty", [])
        except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError) as e:
            print(f"  [pokus {attempt + 1}/{max_retries}] chyba při textové extrakci: {e}")
            time.sleep(2 ** attempt)
    print("  Textová AI extrakce selhala po všech pokusech, vracím prázdný seznam.")
    return []

def extrahovat_text_ze_stran_pdf(pdf_path: str, prvni_strana: int, posledni_strana: int) -> dict:
    import subprocess
    vysledek = {}
    for strana in range(prvni_strana, posledni_strana + 1):
        try:
            proc = subprocess.run(
                ["pdftotext", "-layout", "-f", str(strana), "-l", str(strana), pdf_path, "-"],
                capture_output=True, text=True, timeout=30,
            )
            vysledek[strana] = proc.stdout
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            print(f"  nepodařilo se extrahovat text ze strany {strana}: {e}")
            vysledek[strana] = ""
    return vysledek

# ====================== STAHOVÁNÍ LETÁKŮ ======================

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

def stahnout_obrazek(url: str) -> bytes:
    resp = requests.get(url, headers=HEADERS_BROWSER, timeout=30)
    resp.raise_for_status()
    return resp.content

def _stahnout_pdf_do_tmp(url: str, nazev: str) -> str | None:
    lokalni_cesta = f"/tmp/{nazev}_letak.pdf"
    try:
        print(f"  Stahuji PDF z {url} ...")
        resp = requests.get(url, headers=HEADERS_BROWSER, timeout=120, stream=True)
        resp.raise_for_status()
        with open(lokalni_cesta, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"  PDF uloženo: {lokalni_cesta} ({os.path.getsize(lokalni_cesta)//1024} KB)")
        return lokalni_cesta
    except requests.RequestException as e:
        print(f"  nepodařilo se stáhnout PDF z {url}: {e}")
        return None

def _zpracovat_pdf_obchod(conn, nazev_obchodu, pdf_path, platnost_od=None, platnost_do=None):
    """Společná logika zpracování PDF pro všechny obchody."""
    import subprocess
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF leták nenalezen: {pdf_path}")

    info = subprocess.run(["pdfinfo", pdf_path], capture_output=True, text=True, timeout=15)
    pocet_stran = None
    for radek in info.stdout.splitlines():
        if radek.startswith("Pages:"):
            pocet_stran = int(radek.split(":")[1].strip())

    if not pocet_stran:
        raise RuntimeError(f"Nepodařilo se zjistit počet stran PDF: {pdf_path}")

    print(f"  {nazev_obchodu.upper()} PDF: {pocet_stran} stran celkem")
    texty_stran = extrahovat_text_ze_stran_pdf(pdf_path, 1, pocet_stran)

    vsechny_produkty = []
    for strana, text in texty_stran.items():
        if not text.strip():
            print(f"  strana {strana}: prázdná, přeskakuji")
            continue
        produkty = extrahovat_produkty_z_textu(text)
        print(f"  strana {strana}: nalezeno {len(produkty)} produktů")
        vsechny_produkty.extend(produkty)
        time.sleep(0.5)

    smazat_stara_data(conn, nazev_obchodu)
    ulozit_produkty(conn, nazev_obchodu, vsechny_produkty, zdroj_url=pdf_path,
                    platnost_od=platnost_od, platnost_do=platnost_do)
    return vsechny_produkty

# ====================== KAUFLAND ======================

def ziskat_pdf_url_kaufland() -> str | None:
    """
    Automaticky zjistí URL aktuálního PDF letáku Kauflandu.

    Kaufland stránka prodejny (prodejny.kaufland.cz/letak.html) obsahuje
    odkazy na leaflets.kaufland.com s ID aktuálního letáku.
    PDF letáku je dostupné přes endpoint /pdf na leaflets.kaufland.com.

    Formát URL: https://leaflets.kaufland.com/cz-CZ/{ID}/pdf
    """
    url = "https://prodejny.kaufland.cz/letak.html"
    try:
        resp = requests.get(url, headers=HEADERS_BROWSER, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  Kaufland: nepodařilo se stáhnout {url}: {e}")
        return None

    html = resp.text

    # Hledej leaflets.kaufland.com URL s ID letáku - bereme první (aktuální potravinový leták)
    # Vzor: https://leaflets.kaufland.com/cz-CZ/CZ_cs_KDZ_XXXX_CXXX-LFT/ar/XXXX
    matches = re.findall(
        r'https://leaflets\.kaufland\.com/cz-CZ/([^/"\']+)/(?:ar|pdf)/\d+',
        html
    )

    if not matches:
        # Zkus také jednoduší pattern
        matches = re.findall(
            r'leaflets\.kaufland\.com/cz-CZ/([A-Za-z0-9_\-]+)',
            html
        )

    if not matches:
        print("  Kaufland: nepodařilo se najít ID letáku na stránce.")
        return None

    # Preferuj leták s "KDZ" v názvu (potravinový) nebo "LFT"
    letak_id = None
    for m in matches:
        if "KDZ" in m or "LFT" in m:
            letak_id = m.split("/")[0]  # odstranění případné části za lomítkem
            break

    if not letak_id:
        letak_id = matches[0].split("/")[0]

    pdf_url = f"https://leaflets.kaufland.com/cz-CZ/{letak_id}/pdf"
    print(f"  Kaufland: nalezeno ID letáku '{letak_id}'")
    print(f"  Kaufland: PDF URL = {pdf_url}")
    return pdf_url

def zpracovat_kaufland(conn, pdf_path=None):
    print("\n=== KAUFLAND (PDF) ===")
    started = datetime.now(timezone.utc).isoformat()

    cesta = pdf_path or os.environ.get("KAUFLAND_PDF_CESTA")

    if not cesta:
        pdf_url = ziskat_pdf_url_kaufland()
        if pdf_url:
            cesta = _stahnout_pdf_do_tmp(pdf_url, "kaufland")
        else:
            print("  Kaufland: automatické zjištění URL selhalo, zkus nastavit KAUFLAND_PDF_CESTA.")

    if not cesta:
        conn.execute(
            "INSERT INTO scrape_log (obchod, spusteno_at, pocet_produktu, status, chyba) VALUES (?, ?, ?, ?, ?)",
            ("kaufland", started, 0, "skipped",
             "Žádná cesta k PDF - nastav KAUFLAND_PDF_CESTA nebo zkontroluj dostupnost prodejny.kaufland.cz"),
        )
        conn.commit()
        return

    try:
        produkty = _zpracovat_pdf_obchod(conn, "kaufland", cesta)
        conn.execute(
            "INSERT INTO scrape_log (obchod, spusteno_at, pocet_produktu, status, chyba) VALUES (?, ?, ?, ?, ?)",
            ("kaufland", started, len(produkty), "ok", None),
        )
        conn.commit()
        print(f"  HOTOVO: {len(produkty)} produktů uloženo.")
    except Exception as e:
        conn.execute(
            "INSERT INTO scrape_log (obchod, spusteno_at, pocet_produktu, status, chyba) VALUES (?, ?, ?, ?, ?)",
            ("kaufland", started, 0, "error", str(e)),
        )
        conn.commit()
        print(f"  CHYBA u Kaufland: {e}")

# ====================== LIDL ======================

def ziskat_pdf_url_lidl() -> str | None:
    url = "https://www.lidl.cz/c/akcni-letak/s10008644"
    try:
        resp = requests.get(url, headers=HEADERS_BROWSER, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  nepodařilo se stáhnout {url}: {e}")
        return None
    pdf_urls = re.findall(r'https?://[^\s"\'\\]+\.pdf', resp.text)
    if pdf_urls:
        return pdf_urls[0]
    print("  Lidl: nenalezen žádný .pdf odkaz - nutné stáhnout PDF manuálně.")
    return None

def zpracovat_lidl(conn, pdf_path=None):
    print("\n=== LIDL (PDF) ===")
    started = datetime.now(timezone.utc).isoformat()

    cesta = pdf_path or os.environ.get("LIDL_PDF_CESTA")
    if not cesta:
        cesta = ziskat_pdf_url_lidl()
        if cesta and cesta.startswith("http"):
            cesta = _stahnout_pdf_do_tmp(cesta, "lidl")

    if not cesta:
        conn.execute(
            "INSERT INTO scrape_log (obchod, spusteno_at, pocet_produktu, status, chyba) VALUES (?, ?, ?, ?, ?)",
            ("lidl", started, 0, "skipped",
             "Žádná cesta k PDF - nastav LIDL_PDF_CESTA nebo doplň automatické zjištění."),
        )
        conn.commit()
        return

    try:
        produkty = _zpracovat_pdf_obchod(conn, "lidl", cesta)
        conn.execute(
            "INSERT INTO scrape_log (obchod, spusteno_at, pocet_produktu, status, chyba) VALUES (?, ?, ?, ?, ?)",
            ("lidl", started, len(produkty), "ok", None),
        )
        conn.commit()
        print(f"  HOTOVO: {len(produkty)} produktů uloženo.")
    except Exception as e:
        conn.execute(
            "INSERT INTO scrape_log (obchod, spusteno_at, pocet_produktu, status, chyba) VALUES (?, ?, ?, ?, ?)",
            ("lidl", started, 0, "error", str(e)),
        )
        conn.commit()
        print(f"  CHYBA u Lidl: {e}")

# ====================== ALBERT ======================

def ziskat_pdf_url_albert() -> str | None:
    url = "https://www.albert.cz/aktualni-letaky"
    try:
        resp = requests.get(url, headers=HEADERS_BROWSER, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  nepodařilo se stáhnout {url}: {e}")
        return None
    pdf_urls = re.findall(r'https?://[^\s"\'\\]+\.pdf', resp.text)
    if pdf_urls:
        return pdf_urls[0]
    rel_pdf = re.findall(r'href=["\']([^"\']+\.pdf)["\']', resp.text)
    if rel_pdf:
        return "https://www.albert.cz" + rel_pdf[0] if rel_pdf[0].startswith("/") else rel_pdf[0]
    print("  Albert: nenalezen žádný .pdf odkaz - nutné stáhnout PDF manuálně.")
    return None

def zpracovat_albert(conn, pdf_path=None):
    print("\n=== ALBERT (PDF) ===")
    started = datetime.now(timezone.utc).isoformat()

    cesta = pdf_path or os.environ.get("ALBERT_PDF_CESTA")
    if not cesta:
        url = ziskat_pdf_url_albert()
        if url and url.startswith("http"):
            cesta = _stahnout_pdf_do_tmp(url, "albert")

    if not cesta:
        conn.execute(
            "INSERT INTO scrape_log (obchod, spusteno_at, pocet_produktu, status, chyba) VALUES (?, ?, ?, ?, ?)",
            ("albert", started, 0, "skipped",
             "Žádná cesta k PDF - nastav ALBERT_PDF_CESTA nebo doplň automatické zjištění."),
        )
        conn.commit()
        return

    try:
        produkty = _zpracovat_pdf_obchod(conn, "albert", cesta)
        conn.execute(
            "INSERT INTO scrape_log (obchod, spusteno_at, pocet_produktu, status, chyba) VALUES (?, ?, ?, ?, ?)",
            ("albert", started, len(produkty), "ok", None),
        )
        conn.commit()
        print(f"  HOTOVO: {len(produkty)} produktů uloženo.")
    except Exception as e:
        conn.execute(
            "INSERT INTO scrape_log (obchod, spusteno_at, pocet_produktu, status, chyba) VALUES (?, ?, ?, ?, ?)",
            ("albert", started, 0, "error", str(e)),
        )
        conn.commit()
        print(f"  CHYBA u Albert: {e}")

# ====================== HLAVNÍ BĚH ======================

def main():
    if not OPENROUTER_API_KEY:
        raise SystemExit(
            "Chybí proměnná prostředí OPENROUTER_API_KEY.\n"
            "Nastav ji: export OPENROUTER_API_KEY='sk-or-...'"
        )

    conn = init_db()

    zpracovat_kaufland(conn)
    zpracovat_lidl(conn)
    zpracovat_albert(conn)

    conn.close()
    print("\nVšechny obchody zpracovány.")

if __name__ == "__main__":
    main()
