"""
Leták scraper - bez závislosti na pdftotext/pdfinfo (používá pypdf).
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

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TEXT_MODEL = "google/gemini-2.0-flash-001"
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "ceny.db")

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

TEXT_EXTRACTION_PROMPT = """Jsi expert na čtení reklamních letáků z českých supermarketů.
Níže je text vytažený z jedné strany PDF letáku. Text může být "rozsypaný".
Tvým úkolem je z tohoto textu rekonstruovat VŠECHNY produkty s jejich cenami.
Pro každý produkt vrať:
- nazev: název produktu
- cena: konečná/akční cena jako číslo s desetinnou tečkou
- jednotka: hmotnost/objem/počet balení
- puvodni_cena: přeškrtnutá cena před slevou, pokud je uvedená, jinak null
- akce: true pokud je produkt zlevněný, jinak false
Vrať VÝHRADNĚ validní JSON (žádný text okolo, žádné markdown bloky):
{"produkty": [{"nazev": "...", "cena": 0.0, "jednotka": "...", "puvodni_cena": null, "akce": false}]}
Ignoruj texty jako URL, právní poznámky, loga. Pokud si nejsi jistý, raději vynech.
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
            (obchod, p.get("nazev"), p.get("cena"), p.get("jednotka"),
             p.get("puvodni_cena"), 1 if p.get("akce") else 0,
             platnost_od, platnost_do, now, zdroj_url),
        )
    conn.commit()

def smazat_stara_data(conn, obchod):
    conn.execute("DELETE FROM produkty WHERE obchod = ?", (obchod,))
    conn.commit()

# ====================== PDF EXTRAKCE (přes pypdf, bez pdftotext) ======================

def extrahovat_text_z_pdf(pdf_path: str) -> dict:
    """Vytáhne text z PDF pomocí pypdf — nevyžaduje žádné systémové nástroje."""
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            print("  CHYBA: pypdf není nainstalováno. Přidej 'pypdf' do requirements.txt")
            return {}

    vysledek = {}
    try:
        reader = PdfReader(pdf_path)
        for i, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            vysledek[i] = text
        print(f"  Extrahováno {len(vysledek)} stran z PDF")
    except Exception as e:
        print(f"  CHYBA při čtení PDF: {e}")
    return vysledek

def extrahovat_produkty_z_textu(text: str, max_retries=3) -> list:
    if not text.strip():
        return []
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": TEXT_MODEL,
        "messages": [{"role": "user", "content": f"{TEXT_EXTRACTION_PROMPT}\n\n---TEXT---\n{text}"}],
        "temperature": 0,
        "max_tokens": 4000,
    }
    for attempt in range(max_retries):
        try:
            resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            return json.loads(content).get("produkty", [])
        except Exception as e:
            print(f"  [pokus {attempt+1}/{max_retries}] chyba: {e}")
            time.sleep(2 ** attempt)
    return []

def _stahnout_pdf(url: str, nazev: str) -> str | None:
    cesta = f"/tmp/{nazev}_letak.pdf"
    try:
        print(f"  Stahuji PDF z {url} ...")
        resp = requests.get(url, headers=HEADERS_BROWSER, timeout=120, stream=True)
        resp.raise_for_status()
        with open(cesta, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        size = os.path.getsize(cesta)
        print(f"  PDF uloženo: {cesta} ({size//1024} KB)")
        if size < 5000:
            print(f"  VAROVÁNÍ: PDF je příliš malé ({size} B), pravděpodobně chybné")
            return None
        return cesta
    except Exception as e:
        print(f"  Nepodařilo se stáhnout PDF: {e}")
        return None

def _zpracovat_pdf(conn, nazev_obchodu, pdf_path):
    texty = extrahovat_text_z_pdf(pdf_path)
    if not texty:
        raise RuntimeError("Nepodařilo se extrahovat text z PDF")

    vsechny = []
    for strana, text in texty.items():
        if not text.strip():
            continue
        produkty = extrahovat_produkty_z_textu(text)
        print(f"  strana {strana}: {len(produkty)} produktů")
        vsechny.extend(produkty)
        time.sleep(0.5)

    smazat_stara_data(conn, nazev_obchodu)
    ulozit_produkty(conn, nazev_obchodu, vsechny, zdroj_url=pdf_path)
    return vsechny

# ====================== KAUFLAND ======================

def ziskat_pdf_url_kaufland() -> str | None:
    url = "https://prodejny.kaufland.cz/letak.html"
    try:
        resp = requests.get(url, headers=HEADERS_BROWSER, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  Kaufland: nepodařilo se stáhnout stránku: {e}")
        return None

    matches = re.findall(r'leaflets\.kaufland\.com/cz-CZ/([A-Za-z0-9_\-]+)', resp.text)
    if not matches:
        print("  Kaufland: nenalezeno ID letáku")
        return None

    letak_id = None
    for m in matches:
        if "KDZ" in m or "LFT" in m:
            letak_id = m
            break
    if not letak_id:
        letak_id = matches[0]

    pdf_url = f"https://leaflets.kaufland.com/cz-CZ/{letak_id}/pdf"
    print(f"  Kaufland PDF URL = {pdf_url}")
    return pdf_url

def zpracovat_kaufland(conn):
    print("\n=== KAUFLAND (PDF) ===")
    started = datetime.now(timezone.utc).isoformat()
    cesta = os.environ.get("KAUFLAND_PDF_CESTA")

    if not cesta:
        url = ziskat_pdf_url_kaufland()
        if url:
            cesta = _stahnout_pdf(url, "kaufland")

    if not cesta:
        conn.execute("INSERT INTO scrape_log VALUES (NULL,?,?,?,?,?)",
                     ("kaufland", started, 0, "skipped", "Nepodařilo se získat PDF"))
        conn.commit()
        return

    try:
        produkty = _zpracovat_pdf(conn, "kaufland", cesta)
        conn.execute("INSERT INTO scrape_log VALUES (NULL,?,?,?,?,?)",
                     ("kaufland", started, len(produkty), "ok", None))
        conn.commit()
        print(f"  HOTOVO: {len(produkty)} produktů")
    except Exception as e:
        conn.execute("INSERT INTO scrape_log VALUES (NULL,?,?,?,?,?)",
                     ("kaufland", started, 0, "error", str(e)))
        conn.commit()
        print(f"  CHYBA: {e}")

# ====================== LIDL ======================

def zpracovat_lidl(conn):
    print("\n=== LIDL (PDF) ===")
    started = datetime.now(timezone.utc).isoformat()
    cesta = os.environ.get("LIDL_PDF_CESTA")

    if not cesta:
        try:
            resp = requests.get("https://www.lidl.cz/c/akcni-letak/s10008644",
                                headers=HEADERS_BROWSER, timeout=30)
            pdf_urls = re.findall(r'https?://[^\s"\'\\]+\.pdf', resp.text)
            if pdf_urls:
                cesta = _stahnout_pdf(pdf_urls[0], "lidl")
        except Exception as e:
            print(f"  Lidl: chyba při hledání PDF: {e}")

    if not cesta:
        conn.execute("INSERT INTO scrape_log VALUES (NULL,?,?,?,?,?)",
                     ("lidl", started, 0, "skipped", "Nepodařilo se získat PDF"))
        conn.commit()
        return

    try:
        produkty = _zpracovat_pdf(conn, "lidl", cesta)
        conn.execute("INSERT INTO scrape_log VALUES (NULL,?,?,?,?,?)",
                     ("lidl", started, len(produkty), "ok", None))
        conn.commit()
        print(f"  HOTOVO: {len(produkty)} produktů")
    except Exception as e:
        conn.execute("INSERT INTO scrape_log VALUES (NULL,?,?,?,?,?)",
                     ("lidl", started, 0, "error", str(e)))
        conn.commit()
        print(f"  CHYBA: {e}")

# ====================== ALBERT ======================

def zpracovat_albert(conn):
    print("\n=== ALBERT (PDF) ===")
    started = datetime.now(timezone.utc).isoformat()
    cesta = os.environ.get("ALBERT_PDF_CESTA")

    if not cesta:
        try:
            resp = requests.get("https://www.albert.cz/aktualni-letaky",
                                headers=HEADERS_BROWSER, timeout=30)
            pdf_urls = re.findall(r'https?://[^\s"\'\\]+\.pdf', resp.text)
            if not pdf_urls:
                pdf_urls = ["https://www.albert.cz" + u for u in
                            re.findall(r'href=["\']([^"\']+\.pdf)["\']', resp.text)]
            if pdf_urls:
                cesta = _stahnout_pdf(pdf_urls[0], "albert")
        except Exception as e:
            print(f"  Albert: chyba při hledání PDF: {e}")

    if not cesta:
        conn.execute("INSERT INTO scrape_log VALUES (NULL,?,?,?,?,?)",
                     ("albert", started, 0, "skipped", "Nepodařilo se získat PDF"))
        conn.commit()
        return

    try:
        produkty = _zpracovat_pdf(conn, "albert", cesta)
        conn.execute("INSERT INTO scrape_log VALUES (NULL,?,?,?,?,?)",
                     ("albert", started, len(produkty), "ok", None))
        conn.commit()
        print(f"  HOTOVO: {len(produkty)} produktů")
    except Exception as e:
        conn.execute("INSERT INTO scrape_log VALUES (NULL,?,?,?,?,?)",
                     ("albert", started, 0, "error", str(e)))
        conn.commit()
        print(f"  CHYBA: {e}")

# ====================== HLAVNÍ BĚH ======================

def main():
    if not OPENROUTER_API_KEY:
        raise SystemExit("Chybí proměnná prostředí OPENROUTER_API_KEY.\nNastav ji: export OPENROUTER_API_KEY='sk-or-...'")
    conn = init_db()
    zpracovat_kaufland(conn)
    zpracovat_lidl(conn)
    zpracovat_albert(conn)
    conn.close()
    print("\nVšechny obchody zpracovány.")

if __name__ == "__main__":
    main()
