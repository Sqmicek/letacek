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
from datetime import datetime, timezone
from io import BytesIO

import requests
from PIL import Image

# ====================== KONFIGURACE ======================

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Levný vision model - dobrý poměr cena/přesnost pro čtení letáků z obrázků.
# Lze přepnout na jiný model dostupný na openrouter.ai/models (musí podporovat image input).
VISION_MODEL = "google/gemini-2.0-flash-001"

# Levný TEXTOVÝ model - pro letáky dostupné jako PDF s textovou vrstvou
# (např. Lidl) je tohle výrazně levnější než vision, protože nejde žádný obrázek.
TEXT_MODEL = "google/gemini-2.0-flash-001"

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "ceny.db")

# Max šířka obrázku v px před odesláním do AI (zmenšení = méně tokenů = levnější).
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
    """Před novým scrapem smaž stará data daného obchodu, ať appka nezobrazuje neplatné ceny."""
    conn.execute("DELETE FROM produkty WHERE obchod = ?", (obchod,))
    conn.commit()


# ====================== AI EXTRAKCE ======================

def zmensit_obrazek(image_bytes: bytes) -> bytes:
    """Zmenší obrázek na MAX_IMAGE_WIDTH, ať se šetří tokeny při volání AI."""
    img = Image.open(BytesIO(image_bytes))
    if img.width > MAX_IMAGE_WIDTH:
        ratio = MAX_IMAGE_WIDTH / img.width
        img = img.resize((MAX_IMAGE_WIDTH, int(img.height * ratio)))
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def extrahovat_produkty_z_obrazku(image_bytes: bytes, max_retries=3) -> list:
    """Pošle obrázek letáku do AI vision modelu a vrátí seznam produktů."""
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
            # Pojistka, pokud model přesto vrátí markdown bloky.
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
    """Pošle text strany letáku (z pdftotext) do levného textového AI modelu."""
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
    """
    Vytáhne text z daného rozsahu stran PDF letáku pomocí pdftotext (poppler-utils).
    Vrací dict {cislo_strany: text}.

    Vyžaduje nainstalovaný balíček poppler-utils (na Debian/Ubuntu: apt install poppler-utils;
    na Railway/Docker prostředí bývá dostupný, případně doplň do build kroku).
    """
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


# ====================== STAHOVÁNÍ LETÁKŮ PER OBCHOD ======================
#
# DŮLEŽITÉ: Tyhle funkce musíš dopracovat podle reálné struktury webu obchodu.
# Weby často letáky vykreslují přes JS prohlížeč (flipbook), takže přímý
# requests.get() na HTML stránku nemusí stačit - níže je kostra a komentáře
# k tomu, co zkontrolovat.

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def stahnout_obrazek(url: str) -> bytes:
    resp = requests.get(url, headers=HEADERS_BROWSER, timeout=30)
    resp.raise_for_status()
    return resp.content


import re
import json as _json


def _najdi_json_v_html(html: str, klice_k_hledani) -> list:
    """
    Společná pomocná funkce: flipbook prohlížeče letáků (Albert, Lidl, Kaufland
    leaflets.kaufland.com) typicky embeddují JSON s daty o stránkách letáku
    přímo do HTML stránky (ve <script> tagu, často jako window.__INITIAL_STATE__,
    __NEXT_DATA__, nebo podobně), protože samotné renderování dělá JS až v prohlížeči.

    Tahle funkce v HTML najde JSON bloky a zkusí v nich najít pole obsahující
    URL obrázků (typicky klíče jako "image", "imageUrl", "src", "url" + přípona
    .jpg/.png/.webp).
    """
    obrazky = []
    # Najdi všechny "velké" JSON-like bloky v <script> tagách.
    bloky = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    for blok in bloky:
        # Hrubá heuristika - hledej JSON objekty/pole obsahující klíčová slova.
        if not any(k in blok for k in klice_k_hledani):
            continue
        # Najdi URL adresy obrázků přímo regexem (robustnější než parsovat celý JS).
        nalezene = re.findall(r'https?://[^\s"\'\\]+\.(?:jpg|jpeg|png|webp)', blok)
        obrazky.extend(nalezene)
    # Odstranit duplicity, zachovat pořadí.
    viděno = set()
    unikatni = []
    for url in obrazky:
        if url not in viděno:
            viděno.add(url)
            unikatni.append(url)
    return unikatni


def ziskat_stranky_letaku_albert() -> list:
    """
    Albert (albert.cz/aktualni-letaky) - stránka obsahuje vícero letáků
    (Supermarket, Hypermarket...), každý jako embed flipbook widgetu.

    Strategie: stáhneme hlavní stránku, najdeme v ní URL obrázků stránek
    letáku (embeddované v JSON/JS na stránce). Pokud Albert používá
    externí flipbook službu (běžné u retailu - např. Flipp, Mironet),
    najdeme URL do iframe a stáhneme JSON přímo odtamtud.
    """
    url = "https://www.albert.cz/aktualni-letaky"
    try:
        resp = requests.get(url, headers=HEADERS_BROWSER, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  nepodařilo se stáhnout {url}: {e}")
        return []

    html = resp.text

    # 1) Zkusit najít přímo obrázky stránek letáku v hlavním HTML.
    obrazky = _najdi_json_v_html(html, ["leaflet", "flyer", "letak", "page"])
    if obrazky:
        return obrazky

    # 2) Pokud leták běží přes iframe na externí flipbook službu, najdi tu URL.
    iframe_match = re.search(r'<iframe[^>]+src="([^"]+)"', html)
    if iframe_match:
        iframe_url = iframe_match.group(1)
        if iframe_url.startswith("/"):
            iframe_url = "https://www.albert.cz" + iframe_url
        try:
            iframe_resp = requests.get(iframe_url, headers=HEADERS_BROWSER, timeout=30)
            iframe_resp.raise_for_status()
            return _najdi_json_v_html(iframe_resp.text, ["leaflet", "flyer", "page"])
        except requests.RequestException as e:
            print(f"  nepodařilo se stáhnout iframe {iframe_url}: {e}")

    print("  Albert: nenalezeny žádné obrázky letáku - struktura webu se "
          "pravděpodobně liší od předpokladu, nutná manuální kontrola.")
    return []


def zpracovat_lidl_pdf(conn, pdf_path: str, platnost_od: str = None, platnost_do: str = None):
    """
    Zpracuje Lidl leták z PDF souboru (stáhnutého manuálně z lidl.cz/c/akcni-letak/s10008644
    nebo automatizovaně - viz ziskat_pdf_url_lidl níže).

    Tohle je SPOLEHLIVĚJŠÍ cesta než scraping obrázků z webu: Lidl letáky mají
    v PDF zachovanou textovou vrstvu (potvrzeno na reálném letáku), takže
    pdftotext + levný textový AI model dá přesnější a levnější výsledek než
    posílání obrázků do vision modelu.

    Použití:
        zpracovat_lidl_pdf(conn, "/cesta/k/letaku.pdf", "2026-06-22", "2026-06-24")
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF leták nenalezen: {pdf_path}")

    import subprocess
    info = subprocess.run(["pdfinfo", pdf_path], capture_output=True, text=True, timeout=15)
    pocet_stran = None
    for radek in info.stdout.splitlines():
        if radek.startswith("Pages:"):
            pocet_stran = int(radek.split(":")[1].strip())
    if not pocet_stran:
        raise RuntimeError(f"Nepodařilo se zjistit počet stran PDF: {pdf_path}")

    print(f"  Lidl PDF: {pocet_stran} stran celkem")
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

    smazat_stara_data(conn, "lidl")
    ulozit_produkty(conn, "lidl", vsechny_produkty, zdroj_url=pdf_path,
                     platnost_od=platnost_od, platnost_do=platnost_do)
    return vsechny_produkty


def ziskat_pdf_url_lidl() -> str:
    """
    Pokus o automatické zjištění URL aktuálního PDF letáku ze stránky
    https://www.lidl.cz/c/akcni-letak/s10008644

    Lidl tuhle stránku aktualizuje pravidelně s odkazy na aktuální PDF letáky
    (potvrzeno: stránka obsahuje texty typu "Akční leták OD PONDĚLÍ..." s
    odkazy). Pokud se struktura webu změní, funkce vrátí None a je potřeba
    PDF stáhnout manuálně (viz README).
    """
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

    print("  Lidl: nenalezen žádný .pdf odkaz na stránce letáků - "
          "nutné stáhnout PDF manuálně.")
    return None


def ziskat_pdf_url_albert() -> str:
    """
    Pokus o automatické zjištění URL aktuálního PDF letáku Alberta.
    Albert nabízí letáky ke stažení jako PDF (ověřeno - soubor
    Albert_akcni_letak.pdf byl úspěšně zpracován).

    Hledá .pdf odkaz na stránce https://www.albert.cz/aktualni-letaky.
    Pokud se struktura webu změní nebo PDF odkaz není v HTML (JS render),
    vrátí None - pak nastav ALBERT_PDF_CESTA manuálně.
    """
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

    # Zkus hledat relativní PDF odkaz
    rel_pdf = re.findall(r'href=["\']([^"\']+\.pdf)["\']', resp.text)
    if rel_pdf:
        return "https://www.albert.cz" + rel_pdf[0] if rel_pdf[0].startswith("/") else rel_pdf[0]

    print("  Albert: nenalezen žádný .pdf odkaz - nutné stáhnout PDF manuálně.")
    return None


def zpracovat_albert_pdf(conn, pdf_path: str, platnost_od: str = None, platnost_do: str = None):
    """
    Zpracuje Albert leták z PDF souboru.
    Stejný přístup jako u Lidlu - pdftotext + levný textový AI model.

    Ověřeno na reálném letáku Albert_akcni_letak.pdf.
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF leták nenalezen: {pdf_path}")

    import subprocess
    info = subprocess.run(["pdfinfo", pdf_path], capture_output=True, text=True, timeout=15)
    pocet_stran = None
    for radek in info.stdout.splitlines():
        if radek.startswith("Pages:"):
            pocet_stran = int(radek.split(":")[1].strip())
    if not pocet_stran:
        raise RuntimeError(f"Nepodařilo se zjistit počet stran PDF: {pdf_path}")

    print(f"  Albert PDF: {pocet_stran} stran celkem")
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

    smazat_stara_data(conn, "albert")
    ulozit_produkty(conn, "albert", vsechny_produkty, zdroj_url=pdf_path,
                     platnost_od=platnost_od, platnost_do=platnost_do)
    return vsechny_produkty


def ziskat_pdf_url_kaufland() -> str:
    """
    Pokus o automatické zjištění URL aktuálního PDF letáku Kauflandu.
    Kaufland nabízí letáky ke stažení jako PDF s textovou vrstvou
    (ověřeno: produkty, ceny i slevy jsou čitelné pdftotext nástrojem).

    Hledá .pdf odkaz na stránce https://www.kaufland.cz/akcni-nabidky/letaky.html.
    Pokud se struktura webu změní nebo PDF není přímo v HTML (JS render),
    vrátí None - pak nastav KAUFLAND_PDF_CESTA manuálně.
    """
    url = "https://www.kaufland.cz/akcni-nabidky/letaky.html"
    try:
        resp = requests.get(url, headers=HEADERS_BROWSER, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  nepodařilo se stáhnout {url}: {e}")
        return None

    pdf_urls = re.findall(r'https?://[^\s"\'\\]+\.pdf', resp.text)
    if pdf_urls:
        return pdf_urls[0]

    # Zkus hledat relativní PDF odkaz
    rel_pdf = re.findall(r'href=["\']([^"\']+\.pdf)["\']', resp.text)
    if rel_pdf:
        return "https://www.kaufland.cz" + rel_pdf[0] if rel_pdf[0].startswith("/") else rel_pdf[0]

    print("  Kaufland: nenalezen žádný .pdf odkaz - nutné stáhnout PDF manuálně.")
    return None


def zpracovat_kaufland_pdf(conn, pdf_path: str, platnost_od: str = None, platnost_do: str = None):
    """
    Zpracuje Kaufland leták z PDF souboru.
    Stejný přístup jako u Lidlu - pdftotext + levný textový AI model.

    Kaufland PDF letáky mají textovou vrstvu (ověřeno) - produkty, ceny i slevy
    jsou extrahovatelné bez vision modelu.
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF leták nenalezen: {pdf_path}")

    import subprocess
    info = subprocess.run(["pdfinfo", pdf_path], capture_output=True, text=True, timeout=15)
    pocet_stran = None
    for radek in info.stdout.splitlines():
        if radek.startswith("Pages:"):
            pocet_stran = int(radek.split(":")[1].strip())
    if not pocet_stran:
        raise RuntimeError(f"Nepodařilo se zjistit počet stran PDF: {pdf_path}")

    print(f"  Kaufland PDF: {pocet_stran} stran celkem")
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

    smazat_stara_data(conn, "kaufland")
    ulozit_produkty(conn, "kaufland", vsechny_produkty, zdroj_url=pdf_path,
                     platnost_od=platnost_od, platnost_do=platnost_do)
    return vsechny_produkty


def ziskat_stranky_letaku_kaufland() -> list:
    """
    Kaufland (leaflets.kaufland.com) - URL letáku má tvar
    .../CZ_cs_KDZ_3410_CZ25-LFT/view/flyer/page/N

    Stejná flipbook logika jako u Lidlu - tahle leták-platforma
    (leaflets.kaufland.com) bývá společná pro víc retailerů ve střední
    Evropě, takže když se podaří rozparsovat jednu, stejný kód může
    fungovat i pro další obchody na téže platformě.
    """
    # ID letáku se měnit každý týden - zjistitelné z hlavní stránky
    # https://www.kaufland.cz/akcni-nabidky/letaky.html (lze doplnit
    # automatické zjištění aktuálního ID, zatím nastaveno staticky).
    zakladni_url = ("https://leaflets.kaufland.com/cz-CZ/CZ_cs_KDZ_3410_CZ25-LFT"
                     "/view/flyer/page/{strana}")

    obrazky = []
    strana = 1
    max_stran = 40

    while strana <= max_stran:
        url = zakladni_url.format(strana=strana)
        try:
            resp = requests.get(url, headers=HEADERS_BROWSER, timeout=30)
        except requests.RequestException as e:
            print(f"  Kaufland strana {strana}: chyba požadavku ({e}), končím")
            break

        if resp.status_code == 404:
            break
        resp.raise_for_status()

        nalezene = _najdi_json_v_html(resp.text, ["flyer", "page", "image"])
        if not nalezene:
            if strana == 1:
                print("  Kaufland: nenalezeny žádné obrázky na straně 1 - "
                      "struktura webu se liší od předpokladu, nutná manuální kontrola.")
                break
            else:
                break

        obrazky.extend(nalezene)
        strana += 1

    return obrazky


OBCHODY_VISION = {
    # Vision fallback - použije se jen pokud PDF není k dispozici.
    # Albert a Kaufland nyní mají ověřené PDF letáky s textovou vrstvou,
    # takže vision cesta slouží pouze jako nouzový záložní plán.
}

# Cesty k PDF - nastav env var nebo nech prázdné pro automatické zjištění URL.
LIDL_PDF_CESTA = os.environ.get("LIDL_PDF_CESTA")          # cesta k manuálně staženému PDF
ALBERT_PDF_CESTA = os.environ.get("ALBERT_PDF_CESTA")      # cesta k manuálně staženému PDF
KAUFLAND_PDF_CESTA = os.environ.get("KAUFLAND_PDF_CESTA")  # cesta k manuálně staženému PDF


# ====================== HLAVNÍ BĚH ======================

def zpracovat_obchod_vision(conn, nazev_obchodu, ziskat_stranky_fn):
    print(f"\n=== {nazev_obchodu.upper()} (vision) ===")
    started = datetime.now(timezone.utc).isoformat()

    try:
        urls_stranek = ziskat_stranky_fn()
        if not urls_stranek:
            conn.execute(
                "INSERT INTO scrape_log (obchod, spusteno_at, pocet_produktu, status, chyba) VALUES (?, ?, ?, ?, ?)",
                (nazev_obchodu, started, 0, "skipped", "ziskat_stranky funkce nevrátila žádné URL"),
            )
            conn.commit()
            return

        vsechny_produkty = []
        for i, url in enumerate(urls_stranek, 1):
            print(f"  strana {i}/{len(urls_stranek)}: {url}")
            try:
                img_bytes = stahnout_obrazek(url)
            except requests.RequestException as e:
                print(f"    nepodařilo se stáhnout obrázek: {e}")
                continue

            produkty = extrahovat_produkty_z_obrazku(img_bytes)
            print(f"    nalezeno {len(produkty)} produktů")
            vsechny_produkty.extend(produkty)
            time.sleep(1)  # ohleduplnost k serveru obchodu i k OpenRouter rate limitům

        smazat_stara_data(conn, nazev_obchodu)
        ulozit_produkty(conn, nazev_obchodu, vsechny_produkty, zdroj_url=",".join(urls_stranek))

        conn.execute(
            "INSERT INTO scrape_log (obchod, spusteno_at, pocet_produktu, status, chyba) VALUES (?, ?, ?, ?, ?)",
            (nazev_obchodu, started, len(vsechny_produkty), "ok", None),
        )
        conn.commit()
        print(f"  HOTOVO: {len(vsechny_produkty)} produktů uloženo.")

    except Exception as e:
        conn.execute(
            "INSERT INTO scrape_log (obchod, spusteno_at, pocet_produktu, status, chyba) VALUES (?, ?, ?, ?, ?)",
            (nazev_obchodu, started, 0, "error", str(e)),
        )
        conn.commit()
        print(f"  CHYBA u {nazev_obchodu}: {e}")


def zpracovat_lidl(conn, pdf_path=None):
    print("\n=== LIDL (PDF) ===")
    started = datetime.now(timezone.utc).isoformat()

    cesta = pdf_path or LIDL_PDF_CESTA
    if not cesta:
        cesta = ziskat_pdf_url_lidl()
        if cesta and cesta.startswith("http"):
            # Stáhnout do dočasného souboru.
            lokalni_cesta = "/tmp/lidl_letak.pdf"
            try:
                resp = requests.get(cesta, headers=HEADERS_BROWSER, timeout=60)
                resp.raise_for_status()
                with open(lokalni_cesta, "wb") as f:
                    f.write(resp.content)
                cesta = lokalni_cesta
            except requests.RequestException as e:
                print(f"  nepodařilo se stáhnout PDF: {e}")
                cesta = None

    if not cesta:
        conn.execute(
            "INSERT INTO scrape_log (obchod, spusteno_at, pocet_produktu, status, chyba) VALUES (?, ?, ?, ?, ?)",
            ("lidl", started, 0, "skipped",
             "Žádná cesta k PDF - nastav LIDL_PDF_CESTA nebo doplň automatické zjištění."),
        )
        conn.commit()
        return

    try:
        produkty = zpracovat_lidl_pdf(conn, cesta)
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


def _stahnout_pdf_do_tmp(url: str, nazev: str) -> str | None:
    """Stáhne PDF z URL do /tmp a vrátí lokální cestu."""
    lokalni_cesta = f"/tmp/{nazev}_letak.pdf"
    try:
        resp = requests.get(url, headers=HEADERS_BROWSER, timeout=60)
        resp.raise_for_status()
        with open(lokalni_cesta, "wb") as f:
            f.write(resp.content)
        return lokalni_cesta
    except requests.RequestException as e:
        print(f"  nepodařilo se stáhnout PDF z {url}: {e}")
        return None


def zpracovat_albert(conn, pdf_path=None):
    print("\n=== ALBERT (PDF) ===")
    started = datetime.now(timezone.utc).isoformat()

    cesta = pdf_path or ALBERT_PDF_CESTA
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
        produkty = zpracovat_albert_pdf(conn, cesta)
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


def zpracovat_kaufland(conn, pdf_path=None):
    print("\n=== KAUFLAND (PDF) ===")
    started = datetime.now(timezone.utc).isoformat()

    cesta = pdf_path or KAUFLAND_PDF_CESTA
    if not cesta:
        url = ziskat_pdf_url_kaufland()
        if url and url.startswith("http"):
            cesta = _stahnout_pdf_do_tmp(url, "kaufland")

    if not cesta:
        conn.execute(
            "INSERT INTO scrape_log (obchod, spusteno_at, pocet_produktu, status, chyba) VALUES (?, ?, ?, ?, ?)",
            ("kaufland", started, 0, "skipped",
             "Žádná cesta k PDF - nastav KAUFLAND_PDF_CESTA nebo doplň automatické zjištění."),
        )
        conn.commit()
        return

    try:
        produkty = zpracovat_kaufland_pdf(conn, cesta)
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


def main():
    if not OPENROUTER_API_KEY:
        raise SystemExit(
            "Chybí proměnná prostředí OPENROUTER_API_KEY.\n"
            "Nastav ji: export OPENROUTER_API_KEY='sk-or-...'"
        )

    conn = init_db()

    # Všechny tři obchody běží přes PDF → pdftotext → textový AI model.
    # Vision fallback (OBCHODY_VISION) je prázdný - PDF přístup je spolehlivější a levnější.
    zpracovat_lidl(conn)
    zpracovat_albert(conn)
    zpracovat_kaufland(conn)

    # Případný vision fallback pro obchody bez PDF (momentálně žádné):
    for nazev_obchodu, ziskat_stranky_fn in OBCHODY_VISION.items():
        zpracovat_obchod_vision(conn, nazev_obchodu, ziskat_stranky_fn)

    conn.close()
    print("\nVšechny obchody zpracovány.")


if __name__ == "__main__":
    main()
