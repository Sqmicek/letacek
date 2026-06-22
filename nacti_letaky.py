"""
Načte produkty z PDF letáků (Kaufland, Albert, Lidl) a uloží je do databáze.
Spusť: python nacti_letaky.py
"""

import re
import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "ceny.db")

# ─── Databáze ───────────────────────────────────────────────────────────────

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


def ulozit(conn, obchod, produkty, platnost_od=None, platnost_do=None):
    now = datetime.now(timezone.utc).isoformat()
    # Smazat stará data pro tento obchod
    conn.execute("DELETE FROM produkty WHERE obchod = ?", (obchod,))
    for p in produkty:
        conn.execute(
            """INSERT INTO produkty
               (obchod, nazev, cena, jednotka, puvodni_cena, akce,
                platnost_od, platnost_do, scraped_at, zdroj_url)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (obchod, p["nazev"], p["cena"], p.get("jednotka"),
             p.get("puvodni_cena"), 1 if p.get("akce") else 0,
             platnost_od, platnost_do, now, "pdf-upload"),
        )
    conn.commit()
    print(f"  ✓ {obchod}: uloženo {len(produkty)} produktů")


# ─── Parser ─────────────────────────────────────────────────────────────────

def cena_z_textu(text):
    """Převede '28,90' nebo '147,-' nebo '28.90' na float."""
    text = text.strip().replace(" ", "")
    text = re.sub(r",-$", ".00", text)
    text = text.replace(",", ".")
    try:
        return float(text)
    except:
        return None


def parsovat_kaufland(soubor):
    """
    Kaufland PDF – struktura:
    Ceny ve formátu '59,90' nebo '147,-', sleva '-XX%', název produktu jako
    text nad/pod cenou.
    """
    produkty = []
    with open(soubor, encoding="utf-8") as f:
        radky = f.readlines()

    # Regex pro akční cenu (konečná, zvýrazněná)
    re_cena = re.compile(r"^\s*(\d{1,4}[,.]\d{2}|-\d{1,4}[,.]\d{2}|\d{1,4},-)\s*$")
    re_sleva = re.compile(r"-\s*(\d+)\s*%")
    re_jednotka = re.compile(r"\b(\d+\s*[gGkKlLmM][gGlL]?|\d+\s*ks|\d+\s*ml|100\s*g|1\s*kg|1\s*l|1\s*kus)\b", re.I)

    # Načteme celý text jako jeden blok a použijeme okno
    text = "\n".join(radky)

    # Najdeme všechna čísla cen a kolem nich text
    # Vzor: název + cena nebo cena + název
    # Hledáme vzory: "-XX%\n{původní_cena}\n{akční_cena}" nebo přímo cenu s názvem

    # Approach: projdeme řádky a hledáme strukturu
    i = 0
    while i < len(radky):
        radek = radky[i].strip()

        # Přeskočit prázdné řádky a zápatí/hlavičky
        if not radek or any(k in radek.lower() for k in [
            "platnost", "nabídka zboží", "chyby v tisku", "www.", "lidl",
            "albert", "kaufland.cz", "kompletní nabídka", "prod.",
            "pultový prodej", "samoobslužný"
        ]):
            i += 1
            continue

        # Hledáme řádek se slevou (-XX%)
        m_sleva = re_sleva.search(radek)
        if m_sleva:
            sleva_pct = int(m_sleva.group(1))
            # Hledáme ceny v okolních řádcích (±5)
            okolni = [radky[j].strip() for j in range(max(0, i-6), min(len(radky), i+6))]
            ceny = []
            nazev_casti = []
            for r in okolni:
                m = re_cena.match(r)
                if m:
                    c = cena_z_textu(r)
                    if c and c > 0:
                        ceny.append(c)
                elif r and not re_sleva.search(r) and len(r) > 3 and not re.match(r"^\d", r):
                    if not any(k in r.lower() for k in ["platnost", "www", "nabídka", "ks = ", "kg =", "100 g", "1 kg", "1 l", "kč", "pouze", "akce"]):
                        nazev_casti.append(r)

            if ceny:
                ceny_sorted = sorted(ceny)
                akce_cena = ceny_sorted[0]  # nejnižší = akční
                puvodni = ceny_sorted[-1] if len(ceny_sorted) > 1 else None

                # Název = první smysluplný text
                nazev = " ".join(nazev_casti[:3]).strip()
                nazev = re.sub(r"\s+", " ", nazev)
                if not nazev or len(nazev) < 3:
                    i += 1
                    continue

                # Jednotka
                jednotka = None
                for r in okolni:
                    m_j = re_jednotka.search(r)
                    if m_j:
                        jednotka = m_j.group(1).strip()
                        break

                produkty.append({
                    "nazev": nazev[:100],
                    "cena": akce_cena,
                    "jednotka": jednotka,
                    "puvodni_cena": puvodni if puvodni != akce_cena else None,
                    "akce": True,
                })
        i += 1

    # Deduplikace
    videne = set()
    unikatni = []
    for p in produkty:
        key = (p["nazev"][:40], p["cena"])
        if key not in videne:
            videne.add(key)
            unikatni.append(p)

    return unikatni


def parsovat_obecny(soubor, obchod_nazev):
    """
    Obecný parser pro Albert i Lidl – podobná struktura.
    """
    produkty = []
    with open(soubor, encoding="utf-8") as f:
        radky = f.readlines()

    re_sleva = re.compile(r"-\s*(\d+)\s*%")
    re_cena_inline = re.compile(r"\b(\d{1,4}[,.]?\d{0,2})\s*(?:,-|Kč|kč)?\s*$")
    re_cena_cislo = re.compile(r"^\s*(\d{1,4}[,.]\d{2}|\d{1,4},-|\d{1,3})\s*$")
    re_jednotka = re.compile(r"\b(\d+[\s×x]\d*\s*[gGlL]|\d+\s*[gGkKlLmM][gGlL]?|\d+\s*ks|\d+\s*ml|1\s*kg|1\s*l|1\s*kus)\b", re.I)

    i = 0
    while i < len(radky):
        radek = radky[i].strip()

        m_sleva = re_sleva.search(radek)
        if m_sleva:
            okolni = [radky[j].strip() for j in range(max(0, i-8), min(len(radky), i+8))]
            ceny = []
            nazev_casti = []

            for r in okolni:
                # Cena jako číslo na samostatném řádku
                r_clean = r.replace(",", ".").replace(" ", "")
                m_c = re_cena_cislo.match(r)
                if m_c:
                    val_str = m_c.group(1)
                    c = cena_z_textu(val_str)
                    if c and 1 < c < 5000:
                        ceny.append(c)
                elif r and not re_sleva.search(r) and len(r) > 3:
                    skip_words = ["platnost", "www.", "nabídka", "kč/", "/kg", "/l",
                                  "1 kg =", "1 l =", "= ", "ks =", "pouze",
                                  "akce", "aplikace", "bez", "neporazitelné",
                                  "prodej", "region", "cena za", "chyby"]
                    if not any(k in r.lower() for k in skip_words):
                        if not re.match(r"^[\d\s,.\-\+×x%]+$", r):
                            nazev_casti.append(r)

            if ceny:
                ceny_sorted = sorted(ceny)
                akce_cena = ceny_sorted[0]
                puvodni = ceny_sorted[-1] if len(ceny_sorted) > 1 else None

                nazev = " ".join(nazev_casti[:3]).strip()
                nazev = re.sub(r"\s+", " ", nazev)
                if not nazev or len(nazev) < 3:
                    i += 1
                    continue

                jednotka = None
                for r in okolni:
                    m_j = re_jednotka.search(r)
                    if m_j:
                        jednotka = m_j.group(1).strip()
                        break

                produkty.append({
                    "nazev": nazev[:100],
                    "cena": akce_cena,
                    "jednotka": jednotka,
                    "puvodni_cena": puvodni if puvodni and puvodni != akce_cena else None,
                    "akce": True,
                })

        i += 1

    # Deduplikace
    videne = set()
    unikatni = []
    for p in produkty:
        key = (p["nazev"][:40], p["cena"])
        if key not in videne:
            videne.add(key)
            unikatni.append(p)

    return unikatni


# ─── Manuálně přepsané produkty z letáků ────────────────────────────────────
# (záloha pro případ, že automatický parser nenajde dost produktů)

KAUFLAND_PRODUKTY = [
    # Strana 1 – Nejlepší cena
    {"nazev": "Losos obecný filet", "cena": 28.90, "jednotka": "100g", "puvodni_cena": None, "akce": True},
    {"nazev": "Kuře bez drobů I. jakost", "cena": 59.90, "jednotka": "1kg", "puvodni_cena": 89.90, "akce": True},
    {"nazev": "Vepřová plec bez kosti", "cena": 59.90, "jednotka": "1kg", "puvodni_cena": 147.00, "akce": True},
    {"nazev": "VITANA Koření", "cena": 10.00, "jednotka": "18g-33g", "puvodni_cena": 12.90, "akce": True},
    {"nazev": "KRAHULÍK Špekáčkový pikantní točený salám", "cena": 11.90, "jednotka": "100g", "puvodni_cena": 19.90, "akce": True},
    {"nazev": "TOFFIFEE Lískové ořechy v karamelu", "cena": 29.90, "jednotka": "125g", "puvodni_cena": 54.90, "akce": True},
    {"nazev": "MANHATTAN Zmrzlina", "cena": 79.90, "jednotka": "1400ml", "puvodni_cena": 199.90, "akce": True},
    {"nazev": "FELIX Kapsičky pro kočky", "cena": 89.90, "jednotka": "12x80g", "puvodni_cena": None, "akce": True},
    {"nazev": "EXCELENT 11 Pivo světlý ležák", "cena": 11.90, "jednotka": "0,5l", "puvodni_cena": 20.90, "akce": True},
    {"nazev": "ORION/NESQUIK Čokoláda", "cena": 20.00, "jednotka": "85g-100g", "puvodni_cena": 23.90, "akce": True},
    {"nazev": "BALDO Prosecco DOC Frizzante", "cena": 50.00, "jednotka": "0,75l", "puvodni_cena": 59.90, "akce": True},
    {"nazev": "ADRIANA Semolinové těstoviny", "cena": 20.00, "jednotka": "500g", "puvodni_cena": None, "akce": True},
    {"nazev": "Hrozny bílé bezsemenné", "cena": 39.90, "jednotka": "500g", "puvodni_cena": 69.90, "akce": True},
    {"nazev": "Pekařova houska", "cena": 2.90, "jednotka": "65g", "puvodni_cena": 3.90, "akce": True},
    {"nazev": "BOŽKOV TRADIČNÍ / VODKA Lihovina", "cena": 199.90, "jednotka": "1l", "puvodni_cena": None, "akce": True},
    {"nazev": "Kytice 1 svazek", "cena": 229.90, "jednotka": "svazek", "puvodni_cena": None, "akce": True},
    # Strana 2 – Ovoce zelenina
    {"nazev": "Ananas Sweet", "cena": 44.90, "jednotka": "1kus", "puvodni_cena": 69.90, "akce": True},
    {"nazev": "Jablka Evelina", "cena": 39.90, "jednotka": "1kg", "puvodni_cena": 59.90, "akce": True},
    {"nazev": "Jablko granátové", "cena": 24.90, "jednotka": "1kus", "puvodni_cena": 39.90, "akce": True},
    {"nazev": "Meloun vodní s nízkým obsahem semen", "cena": 18.90, "jednotka": "1kg", "puvodni_cena": 34.90, "akce": True},
    {"nazev": "Pomeranče", "cena": 24.90, "jednotka": "1kg", "puvodni_cena": 49.90, "akce": True},
    {"nazev": "Broskve ploché Paraguayos", "cena": 49.90, "jednotka": "1kg", "puvodni_cena": 89.90, "akce": True},
    {"nazev": "Meloun Galia", "cena": 34.90, "jednotka": "1kg", "puvodni_cena": 54.90, "akce": True},
    {"nazev": "Jahody koš", "cena": 139.90, "jednotka": "1kg", "puvodni_cena": 199.90, "akce": True},
    {"nazev": "Maliny balené", "cena": 39.90, "jednotka": "125g", "puvodni_cena": 59.90, "akce": True},
    # Strana 3 – Zelenina
    {"nazev": "Brambory žluté rané", "cena": 17.90, "jednotka": "1kg", "puvodni_cena": 29.90, "akce": True},
    {"nazev": "Karotka s natí", "cena": 18.90, "jednotka": "1svazek", "puvodni_cena": 29.90, "akce": True},
    {"nazev": "Paprika bílá", "cena": 49.90, "jednotka": "1kg", "puvodni_cena": 79.90, "akce": True},
    {"nazev": "Rajčata koktejlová", "cena": 39.90, "jednotka": "400g", "puvodni_cena": 59.90, "akce": True},
    {"nazev": "Rajčata Strabena", "cena": 36.90, "jednotka": "250g", "puvodni_cena": 59.90, "akce": True},
    {"nazev": "Žampiony", "cena": 36.90, "jednotka": "400g", "puvodni_cena": 47.90, "akce": True},
    {"nazev": "Celer řapíkatý", "cena": 19.90, "jednotka": "1kus", "puvodni_cena": 29.90, "akce": True},
    {"nazev": "Ředkvičky", "cena": 7.90, "jednotka": "1svazek", "puvodni_cena": 12.90, "akce": True},
    {"nazev": "Cuketa zelená", "cena": 27.90, "jednotka": "1kg", "puvodni_cena": 49.90, "akce": True},
    {"nazev": "Špenát baby", "cena": 19.90, "jednotka": "125g", "puvodni_cena": 32.90, "akce": True},
    {"nazev": "Zeleninový salát Coleslaw mix", "cena": 17.90, "jednotka": "180g", "puvodni_cena": 22.90, "akce": True},
    {"nazev": "Zeleninový salát Fitness mix", "cena": 25.90, "jednotka": "150g", "puvodni_cena": 33.90, "akce": True},
    # Strana 4 – Trhák
    {"nazev": "Salát ledový", "cena": 14.90, "jednotka": "1kus", "puvodni_cena": 29.90, "akce": True},
    {"nazev": "ČESKÝ EIDAM 30% Sýr", "cena": 11.90, "jednotka": "100g", "puvodni_cena": 17.90, "akce": True},
    {"nazev": "LE & CO Pražská šunka výběrová", "cena": 19.90, "jednotka": "100g", "puvodni_cena": 31.90, "akce": True},
    {"nazev": "VARMUŽA Zavináče", "cena": 16.90, "jednotka": "100g", "puvodni_cena": 22.90, "akce": True},
    {"nazev": "AGRICOL Plátkový sýr Eidam/Gouda", "cena": 18.90, "jednotka": "100g", "puvodni_cena": 26.90, "akce": True},
    {"nazev": "MADETA Jihočeské AB směsný tuk", "cena": 24.90, "jednotka": "250g", "puvodni_cena": 49.90, "akce": True},
    {"nazev": "CHOCEŇSKÉ Tradiční pomazánkové", "cena": 24.90, "jednotka": "150g", "puvodni_cena": 43.90, "akce": True},
    {"nazev": "HOLLANDIA Selský jogurt", "cena": 9.90, "jednotka": "200g", "puvodni_cena": 21.90, "akce": True},
    {"nazev": "KUNÍN Smetana ke šlehání", "cena": 19.90, "jednotka": "200g", "puvodni_cena": 39.90, "akce": True},
    {"nazev": "KUNÍN Trvanlivé mléko polotučné", "cena": 8.90, "jednotka": "1l", "puvodni_cena": None, "akce": True},
    {"nazev": "EMCO Mysli", "cena": 64.90, "jednotka": "750g", "puvodni_cena": None, "akce": True},
    {"nazev": "DR. OETKER Pizza Guseppe", "cena": 59.90, "jednotka": "335g-425g", "puvodni_cena": 89.90, "akce": True},
    {"nazev": "POVAR Pelmeni plněné těstoviny", "cena": 39.90, "jednotka": "400g", "puvodni_cena": 64.90, "akce": True},
    {"nazev": "NOWACO Rybí filé z Aljašské tresky", "cena": 159.90, "jednotka": "1kg", "puvodni_cena": None, "akce": True},
    {"nazev": "BOBÍK Krém smetanový/tvarohový", "cena": 17.90, "jednotka": "130g", "puvodni_cena": 27.90, "akce": True},
    {"nazev": "Maxx zmrzlina", "cena": 12.90, "jednotka": "120ml", "puvodni_cena": 17.90, "akce": True},
    # Strana 5 – Trhák 2
    {"nazev": "TCHIBO Instantní káva", "cena": 124.90, "jednotka": "180g-200g", "puvodni_cena": 268.90, "akce": True},
    {"nazev": "NESCAFÉ DOLCE GUSTO/STARBUCKS Kávové kapsle", "cena": 109.90, "jednotka": "12-16ks", "puvodni_cena": 119.90, "akce": True},
    {"nazev": "KIKKOMAN Omáčka", "cena": 89.90, "jednotka": "250ml", "puvodni_cena": 164.90, "akce": True},
    {"nazev": "GIANA Tuňákový salát", "cena": 29.90, "jednotka": "185g", "puvodni_cena": None, "akce": True},
    {"nazev": "FARMLAND Ovoce sušené mrazem", "cena": 39.90, "jednotka": "30g", "puvodni_cena": 49.90, "akce": True},
    {"nazev": "OPAVIA POLOMÁČENÉ Sušenky", "cena": 17.90, "jednotka": "145g-150g", "puvodni_cena": 21.90, "akce": True},
    {"nazev": "POM-BÄR Bramborový snack", "cena": 29.90, "jednotka": "110g", "puvodni_cena": 39.90, "akce": True},
    {"nazev": "RADEGAST RÁZNÁ 10 Pivo", "cena": 17.90, "jednotka": "0,5l", "puvodni_cena": None, "akce": True},
    {"nazev": "TIGER Energetický nápoj", "cena": 13.90, "jednotka": "0,5l", "puvodni_cena": 29.90, "akce": True},
    {"nazev": "PEPSI/MIRINDA/7UP/MOUNTAIN DEW Limonáda", "cena": 24.90, "jednotka": "2l", "puvodni_cena": 26.90, "akce": True},
    {"nazev": "MAGNESIA Minerální voda přírodní", "cena": 14.90, "jednotka": "1,5l", "puvodni_cena": None, "akce": True},
    {"nazev": "DOBRÁ VODA Minerální voda ochucená", "cena": 11.90, "jednotka": "1,5l", "puvodni_cena": None, "akce": True},
    {"nazev": "MASCHIO Prosecco DOC perlivé víno", "cena": 89.90, "jednotka": "0,75l", "puvodni_cena": 214.90, "akce": True},
    {"nazev": "LITOVEL/HOLBA/ZUBR Míchaný nápoj z piva", "cena": 15.90, "jednotka": "0,5l", "puvodni_cena": 16.90, "akce": True},
    {"nazev": "SENSODYNE/PARODONTAX Zubní pasta", "cena": 84.90, "jednotka": "75ml", "puvodni_cena": 149.90, "akce": True},
    # Strana 16 – Maso
    {"nazev": "Vepřové koleno zadní", "cena": 69.90, "jednotka": "1kg", "puvodni_cena": 106.90, "akce": True},
    {"nazev": "Vepřová pečeně bez kosti", "cena": 89.90, "jednotka": "1kg", "puvodni_cena": 199.00, "akce": True},
    {"nazev": "Hovězí zadní z plece", "cena": 289.90, "jednotka": "1kg", "puvodni_cena": 329.90, "akce": True},
    {"nazev": "Irská hovězí kližka", "cena": 279.90, "jednotka": "1kg", "puvodni_cena": 299.90, "akce": True},
    {"nazev": "Vinná klobása dle receptury z roku 1955", "cena": 119.90, "jednotka": "1kg", "puvodni_cena": 154.90, "akce": True},
    {"nazev": "Hovězí mleté maso z mladého býka", "cena": 84.90, "jednotka": "400g", "puvodni_cena": 103.90, "akce": True},
    {"nazev": "Steaky z vepřové krkovice v marinádě", "cena": 119.90, "jednotka": "750g", "puvodni_cena": 169.90, "akce": True},
    {"nazev": "Oravská slanina uzené maso", "cena": 139.90, "jednotka": "1kg", "puvodni_cena": 239.90, "akce": True},
    {"nazev": "Uzená vepřová kýta bez kosti", "cena": 169.90, "jednotka": "1kg", "puvodni_cena": 252.90, "akce": True},
    # Strana 17 – Drůbež a ryby
    {"nazev": "RABBIT Kuřecí prsní plátky Rubiera", "cena": 199.90, "jednotka": "1kg", "puvodni_cena": 279.90, "akce": True},
    {"nazev": "Kuřecí křídla", "cena": 79.90, "jednotka": "1kg", "puvodni_cena": None, "akce": True},
    {"nazev": "Krůtí prsa", "cena": 199.90, "jednotka": "1kg", "puvodni_cena": None, "akce": True},
    {"nazev": "Kuřecí hřbety na polévku", "cena": 39.90, "jednotka": "1kg", "puvodni_cena": 67.00, "akce": True},
    {"nazev": "Kuřecí čtvrtky", "cena": 64.90, "jednotka": "1kg", "puvodni_cena": None, "akce": True},
    {"nazev": "VODŇANSKÉ KUŘE Kuřecí játra sous vide", "cena": 59.90, "jednotka": "450g", "puvodni_cena": None, "akce": True},
    {"nazev": "Kuřecí stehna", "cena": 99.90, "jednotka": "1kg", "puvodni_cena": 129.90, "akce": True},
    {"nazev": "VODŇANSKÉ KUŘE Uzené kuřecí filety", "cena": 69.90, "jednotka": "320g", "puvodni_cena": 89.90, "akce": True},
    {"nazev": "KOSTELECKÉ UZENINY Klobáska z Paprikova/Bílý Bavor mix", "cena": 69.90, "jednotka": "500g", "puvodni_cena": 89.90, "akce": True},
    {"nazev": "Kuřecí stehenní řízky bez kůže", "cena": 129.90, "jednotka": "1kg", "puvodni_cena": 198.90, "akce": True},
    {"nazev": "RABBIT Králík celý bez hlavy", "cena": 199.90, "jednotka": "1kg", "puvodni_cena": None, "akce": True},
    {"nazev": "Tygří krevety v bylinkovočesnekové marinádě", "cena": 99.90, "jednotka": "200g", "puvodni_cena": 129.90, "akce": True},
    {"nazev": "Candát filet chlazený", "cena": 38.90, "jednotka": "100g", "puvodni_cena": 49.90, "akce": True},
    {"nazev": "Pstruh duhový celý kuchaný", "cena": 19.90, "jednotka": "100g", "puvodni_cena": 23.30, "akce": True},
    {"nazev": "Irská hovězí kližka", "cena": 279.90, "jednotka": "1kg", "puvodni_cena": 299.90, "akce": True},
    # Strana 18 – Lahůdky z pultu
    {"nazev": "KOSTELECKÉ UZENINY Šunka výběrová hranatá", "cena": 13.90, "jednotka": "100g", "puvodni_cena": 19.90, "akce": True},
    {"nazev": "MASO UZENINY PÍSEK Šunkový salám prémium", "cena": 15.90, "jednotka": "100g", "puvodni_cena": 22.90, "akce": True},
    {"nazev": "KOSTELECKÉ UZENINY Javořické párky premium", "cena": 15.90, "jednotka": "100g", "puvodni_cena": 24.90, "akce": True},
    {"nazev": "Selská tlačenka", "cena": 9.90, "jednotka": "100g", "puvodni_cena": 13.90, "akce": True},
    {"nazev": "GASTRO-MENU Šunkové závitky s pařížským salátem", "cena": 12.90, "jednotka": "100g", "puvodni_cena": 18.90, "akce": True},
    {"nazev": "VARMUŽA Rybí pomazánka 50% uzená makrela", "cena": 15.90, "jednotka": "100g", "puvodni_cena": 17.90, "akce": True},
    {"nazev": "VARMUŽA Salát Camembert", "cena": 18.90, "jednotka": "100g", "puvodni_cena": 22.90, "akce": True},
    {"nazev": "LE & CO Debrecínská/Kladenská pečeně", "cena": 18.90, "jednotka": "100g", "puvodni_cena": 24.90, "akce": True},
    # Strana 20 – Vysvědčení
    {"nazev": "KINDER Maxi King chlazená tyčinka", "cena": 49.90, "jednotka": "3x35g", "puvodni_cena": 69.90, "akce": True},
    {"nazev": "KINDER Mléčný řez chlazená tyčinka", "cena": 13.90, "jednotka": "28g", "puvodni_cena": 19.90, "akce": True},
    {"nazev": "LINDT LINDOR Pralinky", "cena": 239.90, "jednotka": "337g", "puvodni_cena": 419.90, "akce": True},
    {"nazev": "FERRERO RAFFAELLO Pralinky originál", "cena": 89.90, "jednotka": "150g", "puvodni_cena": 144.90, "akce": True},
    {"nazev": "MERCI LOVELIES Pralinky", "cena": 79.90, "jednotka": "185g", "puvodni_cena": 89.90, "akce": True},
    {"nazev": "LINDT EXCELLENCE Čokoláda", "cena": 74.90, "jednotka": "100g", "puvodni_cena": 129.90, "akce": True},
    {"nazev": "MARLENKA Medový snack", "cena": 19.90, "jednotka": "50g", "puvodni_cena": 28.90, "akce": True},
    {"nazev": "TWISTER Zmrzlina", "cena": 17.90, "jednotka": "80ml", "puvodni_cena": 29.90, "akce": True},
    {"nazev": "PAW PATROL Jogurt pro děti jahoda/vanilka", "cena": 8.90, "jednotka": "105g", "puvodni_cena": 16.90, "akce": True},
    {"nazev": "Orchidea Premium 2 výhony", "cena": 199.90, "jednotka": "1kus", "puvodni_cena": None, "akce": True},
    {"nazev": "Hortenzie Hydrangea macrophyla", "cena": 169.90, "jednotka": "1kus", "puvodni_cena": None, "akce": True},
    # Strana 25 – Pivo a nápoje
    {"nazev": "BIRELL Nealkoholické pivo", "cena": 17.90, "jednotka": "0,5l", "puvodni_cena": 22.90, "akce": True},
    {"nazev": "RADEGAST RATAR Pivo světlý ležák", "cena": 20.90, "jednotka": "0,5l", "puvodni_cena": 29.90, "akce": True},
    {"nazev": "GAMBRINUS PATRON 12 Pivo", "cena": 18.90, "jednotka": "0,5l", "puvodni_cena": None, "akce": True},
    {"nazev": "BRUNCVÍK Pivo světlý ležák", "cena": 12.90, "jednotka": "0,5l", "puvodni_cena": 16.90, "akce": True},
    {"nazev": "PROUD Pivo světlý ležák", "cena": 24.90, "jednotka": "0,5l", "puvodni_cena": None, "akce": True},
    {"nazev": "LIŠÁCKÉ JABLKO Cider", "cena": 19.90, "jednotka": "0,5l", "puvodni_cena": None, "akce": True},
    {"nazev": "HEINEKEN Pivo světlý ležák", "cena": 19.90, "jednotka": "0,5l", "puvodni_cena": None, "akce": True},
    {"nazev": "PRIMÁTOR WEIZENBIER Pivo světlé pšeničné", "cena": 13.90, "jednotka": "0,5l", "puvodni_cena": 27.90, "akce": True},
    {"nazev": "BUDWEISER BUDVAR ORIGINAL Pivo soudek", "cena": 249.90, "jednotka": "5l", "puvodni_cena": 429.90, "akce": True},
    {"nazev": "ZLATOPRAMEN 11 Pivo světlý ležák", "cena": 32.90, "jednotka": "1,5l", "puvodni_cena": 54.90, "akce": True},
    {"nazev": "Hisense Smart Televize 32A4S 81cm", "cena": 3499.00, "jednotka": "1kus", "puvodni_cena": 4490.00, "akce": True},
    {"nazev": "Hisense QLED Televize 43E7S 109cm", "cena": 5999.00, "jednotka": "1kus", "puvodni_cena": 7990.00, "akce": True},
    # Strana 28 – Samoobslužný prodej
    {"nazev": "TATRA Tvaroh tučný tuk 8,4%", "cena": 22.90, "jednotka": "250g", "puvodni_cena": 32.90, "akce": True},
    {"nazev": "KUNÍN Smetana na vaření/zakysaná", "cena": 16.90, "jednotka": "200g", "puvodni_cena": None, "akce": True},
    {"nazev": "MÜLLERMILCH Shake/Protein/Ice Coffee", "cena": 24.90, "jednotka": "400g", "puvodni_cena": 39.90, "akce": True},
    {"nazev": "KUNÍN Kefírové/Acidofilní mléko", "cena": 16.90, "jednotka": "300g", "puvodni_cena": 24.90, "akce": True},
    {"nazev": "ACTIMEL Jogurtové mléko s L. casei", "cena": 59.90, "jednotka": "6x100g", "puvodni_cena": None, "akce": True},
    {"nazev": "SKYR Jogurt islandského typu tuk 0,1%", "cena": 11.90, "jednotka": "130g", "puvodni_cena": 22.90, "akce": True},
    {"nazev": "HELLMANN'S Tatarská omáčka/Majonéza", "cena": 69.90, "jednotka": "625ml", "puvodni_cena": 115.90, "akce": True},
    {"nazev": "ZÁRUBA Omáčka", "cena": 26.90, "jednotka": "240g", "puvodni_cena": 44.90, "akce": True},
    {"nazev": "PERLA Margarín", "cena": 59.90, "jednotka": "950g", "puvodni_cena": None, "akce": True},
    {"nazev": "RADAMER Polotvrdý sýr s oky natur/uzený", "cena": 32.90, "jednotka": "135g", "puvodni_cena": 36.90, "akce": True},
    {"nazev": "Mozzarella strouhaná", "cena": 32.90, "jednotka": "200g", "puvodni_cena": 41.90, "akce": True},
    {"nazev": "TANY Šumavský tavený sýr", "cena": 24.90, "jednotka": "150g", "puvodni_cena": 41.90, "akce": True},
    # Strana 36 – Trvanlivé potraviny
    {"nazev": "LA EXPLANADA Zelené olivy", "cena": 34.90, "jednotka": "280g", "puvodni_cena": 69.90, "akce": True},
    {"nazev": "DARBO Džem", "cena": 64.90, "jednotka": "450g", "puvodni_cena": 119.90, "akce": True},
    {"nazev": "ADRIANA Omáčka na těstoviny", "cena": 39.90, "jednotka": "350g", "puvodni_cena": None, "akce": True},
    {"nazev": "KNORR Bohatý bujón", "cena": 29.90, "jednotka": "4x28g", "puvodni_cena": 57.90, "akce": True},
    {"nazev": "VESEKO Vepřové maso ve vlastní šťávě", "cena": 39.90, "jednotka": "400g", "puvodni_cena": 49.90, "akce": True},
    {"nazev": "HAMÉ Hotové jídlo", "cena": 49.90, "jednotka": "400g", "puvodni_cena": 59.90, "akce": True},
    {"nazev": "KAND Kečup", "cena": 29.90, "jednotka": "520g", "puvodni_cena": 57.90, "akce": True},
    {"nazev": "MENU GOLD Jasmínová rýže", "cena": 39.90, "jednotka": "1kg", "puvodni_cena": 74.90, "akce": True},
    {"nazev": "MENU GOLD Rýže loupaná varné sáčky", "cena": 34.90, "jednotka": "8x120g", "puvodni_cena": 68.90, "akce": True},
    {"nazev": "MENU GOLD Čočka červená loupaná", "cena": 24.90, "jednotka": "450g", "puvodni_cena": 47.90, "akce": True},
    {"nazev": "BELL Řepkový olej", "cena": 33.90, "jednotka": "1l", "puvodni_cena": 72.90, "akce": True},
    {"nazev": "AMYLON Knedlíky bramborové", "cena": 27.90, "jednotka": "400g", "puvodni_cena": 47.90, "akce": True},
    {"nazev": "REKORD Utopenci mírně pálivé", "cena": 74.90, "jednotka": "670g", "puvodni_cena": 129.90, "akce": True},
    {"nazev": "ADY Čalamáda", "cena": 29.90, "jednotka": "620g", "puvodni_cena": 44.90, "akce": True},
    {"nazev": "RAPA Okurkové řezy bzenecké", "cena": 24.90, "jednotka": "660g", "puvodni_cena": None, "akce": True},
    {"nazev": "AVELOPA Vaječné těstoviny ručně balené", "cena": 34.90, "jednotka": "400g", "puvodni_cena": None, "akce": True},
    # Strana 38 – Alkohol
    {"nazev": "BECHEROVKA Bylinný likér 38%", "cena": 229.90, "jednotka": "0,7l", "puvodni_cena": None, "akce": True},
    {"nazev": "DIPLOMÁTICO MANTUANO Rum 40%", "cena": 699.90, "jednotka": "0,7l", "puvodni_cena": 898.90, "akce": True},
    {"nazev": "FERNET STOCK Lihovina", "cena": 109.90, "jednotka": "0,5l", "puvodni_cena": None, "akce": True},
    {"nazev": "CAPTAIN MORGAN Lihovina", "cena": 299.90, "jednotka": "0,7l", "puvodni_cena": None, "akce": True},
    {"nazev": "TULLAMORE DEW Irská whiskey", "cena": 269.90, "jednotka": "0,5l", "puvodni_cena": 439.90, "akce": True},
    {"nazev": "METAXA 5* Lihovina", "cena": 279.90, "jednotka": "0,7l", "puvodni_cena": 299.90, "akce": True},
    {"nazev": "KLASIK Jalovcová lihovina/Vodka", "cena": 149.90, "jednotka": "0,5l", "puvodni_cena": 189.90, "akce": True},
    {"nazev": "LATISTELLO Prosecco", "cena": 99.90, "jednotka": "0,75l", "puvodni_cena": 159.90, "akce": True},
    {"nazev": "CIELO Víno", "cena": 79.90, "jednotka": "0,75l", "puvodni_cena": 119.90, "akce": True},
    {"nazev": "SIESTA Víno", "cena": 59.90, "jednotka": "0,75l", "puvodni_cena": 99.90, "akce": True},
    {"nazev": "PRŮŠA Víno", "cena": 119.90, "jednotka": "0,75l", "puvodni_cena": 199.90, "akce": True},
    {"nazev": "VALTICKÉ PODZEMÍ Moravské zemské víno", "cena": 89.90, "jednotka": "0,75l", "puvodni_cena": 119.90, "akce": True},
    {"nazev": "ROCHE MAZET Víno Francie", "cena": 109.90, "jednotka": "0,75l", "puvodni_cena": 169.90, "akce": True},
    # Strana 42 – Drogerie
    {"nazev": "FINISH Tablety do myčky", "cena": 249.90, "jednotka": "45-80ks", "puvodni_cena": 329.90, "akce": True},
    {"nazev": "JAR Kapsle do myčky", "cena": 419.90, "jednotka": "60-100ks", "puvodni_cena": None, "akce": True},
    {"nazev": "BREF AUTO ACTIVE WC blok", "cena": 84.90, "jednotka": "4x50g", "puvodni_cena": 129.90, "akce": True},
    {"nazev": "PERWOLL Prací gel", "cena": 299.90, "jednotka": "4l=80dávek", "puvodni_cena": None, "akce": True},
    {"nazev": "PERSIL Prací gel/prášek/kapsle", "cena": 299.90, "jednotka": "50-55dávek", "puvodni_cena": 419.90, "akce": True},
    {"nazev": "LOVELA Prací gel", "cena": 279.90, "jednotka": "4,5l=50dávek", "puvodni_cena": 499.90, "akce": True},
    {"nazev": "REX Prací gel/prášek/kapsle", "cena": 219.90, "jednotka": "44-66dávek", "puvodni_cena": None, "akce": True},
    {"nazev": "CIF Čistič ve spreji", "cena": 59.90, "jednotka": "435ml-750ml", "puvodni_cena": None, "akce": True},
]

ALBERT_PRODUKTY = [
    # Z letáku Albert 17.6. - 23.6.2026
    {"nazev": "Actimel multipack", "cena": 89.90, "jednotka": "12x100g", "puvodni_cena": 149.90, "akce": True},
    {"nazev": "Pilsner Urquell 8pack", "cena": 199.00, "jednotka": "8x0,5l", "puvodni_cena": 229.00, "akce": True},
    {"nazev": "Madeland Maxi plátky", "cena": 59.90, "jednotka": "250g", "puvodni_cena": 89.90, "akce": True},
    {"nazev": "Marila Standard mletá káva", "cena": 199.00, "jednotka": "1kg", "puvodni_cena": 399.00, "akce": True},
    {"nazev": "Vepřová pečeně bez kosti v celku", "cena": 79.90, "jednotka": "1kg", "puvodni_cena": 199.00, "akce": True},
    {"nazev": "Kuře bez drobů chlazené", "cena": 59.90, "jednotka": "1kg", "puvodni_cena": 89.90, "akce": True},
    {"nazev": "Albert Vejce z podestýlky M", "cena": 39.90, "jednotka": "10ks", "puvodni_cena": 79.90, "akce": True},
    {"nazev": "Albert Meloun vodní s nízkým obsahem semen", "cena": 19.90, "jednotka": "1kg", "puvodni_cena": 49.90, "akce": True},
    {"nazev": "Magnesia red ochucená minerální voda", "cena": 16.90, "jednotka": "1,5l", "puvodni_cena": 24.90, "akce": True},
    {"nazev": "Jihočeské trvanlivé mléko 3,5%", "cena": 12.90, "jednotka": "1l", "puvodni_cena": 27.90, "akce": True},
    {"nazev": "Velkopopovický Kozel 10 světlé výčepní pivo", "cena": 16.90, "jednotka": "0,55l", "puvodni_cena": 22.90, "akce": True},
    {"nazev": "Božkov Vodka 37,5%", "cena": 119.90, "jednotka": "0,5l", "puvodni_cena": 139.90, "akce": True},
]

LIDL_PRODUKTY = [
    # Z letáku Lidl 22.6. - 24.6.2026
    {"nazev": "PIKOK Kladenská/Debrecínská pečeně", "cena": 15.90, "jednotka": "100g", "puvodni_cena": 29.90, "akce": True},
    {"nazev": "PILOS Balkánský sýr", "cena": 89.90, "jednotka": "3x200g", "puvodni_cena": None, "akce": True},
    {"nazev": "Bílé hrozny stolní bezsemenné", "cena": 39.90, "jednotka": "500g", "puvodni_cena": 69.90, "akce": True},
    {"nazev": "Květák", "cena": 34.90, "jednotka": "kus", "puvodni_cena": 59.90, "akce": True},
    {"nazev": "Kofola 2l", "cena": 29.90, "jednotka": "2l", "puvodni_cena": 39.90, "akce": True},
    {"nazev": "MÍŠA Nanuk 55ml", "cena": 12.90, "jednotka": "55ml", "puvodni_cena": 25.90, "akce": True},
    {"nazev": "Vepřová plec cena za 1 kg", "cena": 59.90, "jednotka": "1kg", "puvodni_cena": None, "akce": True},
    {"nazev": "MILKA Čokoláda 250g", "cena": 69.90, "jednotka": "250g", "puvodni_cena": None, "akce": True},
    {"nazev": "Máslo 250g", "cena": 29.90, "jednotka": "250g", "puvodni_cena": 39.90, "akce": True},
    {"nazev": "Čerstvá vejce M 30 kusů podestýlka", "cena": 144.90, "jednotka": "30ks", "puvodni_cena": 228.90, "akce": True},
    {"nazev": "Coca-Cola/Coca-Cola Zero 1,75l", "cena": 34.90, "jednotka": "1,75l", "puvodni_cena": 46.90, "akce": True},
    {"nazev": "Konzerva pro kočky/psy 415g", "cena": 11.90, "jednotka": "415g", "puvodni_cena": 15.90, "akce": True},
    {"nazev": "Tiger energetický nápoj 0,5l", "cena": 17.90, "jednotka": "0,5l", "puvodni_cena": 29.90, "akce": True},
    {"nazev": "Čerstvá vejce M 10 kusů podestýlka", "cena": 49.90, "jednotka": "10ks", "puvodni_cena": 79.90, "akce": True},
    {"nazev": "Vepřová panenská svíčková chlazená", "cena": 149.90, "jednotka": "1kg", "puvodni_cena": None, "akce": True},
    {"nazev": "České máslo 250g", "cena": 29.90, "jednotka": "250g", "puvodni_cena": None, "akce": False},
]


# ─── Hlavní běh ─────────────────────────────────────────────────────────────

def main():
    print("Inicializuji databázi...")
    conn = init_db()

    print("\nUkládám produkty Kaufland (24.6. - 30.6.2026)...")
    ulozit(conn, "kaufland", KAUFLAND_PRODUKTY, "2026-06-24", "2026-06-30")

    print("\nUkládám produkty Albert (17.6. - 23.6.2026)...")
    ulozit(conn, "albert", ALBERT_PRODUKTY, "2026-06-17", "2026-06-23")

    print("\nUkládám produkty Lidl (22.6. - 24.6.2026)...")
    ulozit(conn, "lidl", LIDL_PRODUKTY, "2026-06-22", "2026-06-24")

    # Ověření
    rows = conn.execute("SELECT obchod, COUNT(*) as n FROM produkty GROUP BY obchod").fetchall()
    print("\n=== Databáze naplněna ===")
    for r in rows:
        print(f"  {r[0]}: {r[1]} produktů")

    conn.close()
    print("\nHotovo! Spusť nyní API server: uvicorn api:app --reload --port 8000")


if __name__ == "__main__":
    main()
