# Porovnávač cen - scraper + API

## Aktuální stav

✅ **Lidl** - OVĚŘENO. PDF letáky mají funkční textovou vrstvu.
   `pdftotext` vytáhne text, levný AI textový model ho složí do JSON.

✅ **Albert** - OVĚŘENO. PDF leták úspěšně otestován (Albert_akcni_letak.pdf).
   Stejný přístup jako Lidl - pdftotext + textový AI model.

✅ **Kaufland** - OVĚŘENO (dle konverzace). PDF letáky mají textovou vrstvu
   s produkty, cenami i slevami. Stejný přístup jako Lidl a Albert.

Všechny tři obchody jdou přes: **PDF → pdftotext → AI textový model → SQLite**  
Žádné vision tokeny, žádné obrázky, žádné scrapování webové struktury.

## Jak to funguje

1. Scraper stáhne / načte PDF letáku (manuálně nebo automaticky z webu obchodu)
2. `pdftotext -layout` vytáhne text z každé strany
3. Text strany pošle do levného AI modelu (`TEXT_MODEL` v `scraper.py`),
   který ho složí do JSON: název, cena, jednotka, akce
4. Vše se uloží do SQLite (`data/ceny.db`)
5. `api.py` to vystaví appce přes REST

## Spuštění

```bash
pip install -r requirements.txt --break-system-packages

# Na Ubuntu/Debian nainstaluj poppler-utils (pro pdftotext):
sudo apt-get install poppler-utils

export OPENROUTER_API_KEY="sk-or-tvuj-klic"

# Varianta A - manuálně stažená PDF (nejspolehlivější):
export LIDL_PDF_CESTA="/cesta/k/lidl-letak.pdf"
export ALBERT_PDF_CESTA="/cesta/k/albert-letak.pdf"
export KAUFLAND_PDF_CESTA="/cesta/k/kaufland-letak.pdf"
python scraper.py

# Varianta B - scraper zkusí PDF najít sám na webu:
python scraper.py
```

```bash
uvicorn api:app --reload --port 8000   # spustí API na http://localhost:8000
```

Vyzkoušej: `http://localhost:8000/hledat?q=mleko`

## Jak stáhnout PDF leták

1. Otevři leták obchodu v prohlížeči na **počítači**
2. Hledej tlačítko "Stáhnout PDF" nebo "Ke stažení"
3. Pravým tlačítkem → "Uložit cíl odkazu jako"
4. Nastav cestu přes env var (LIDL_PDF_CESTA / ALBERT_PDF_CESTA / KAUFLAND_PDF_CESTA)

## Nasazení na Railway.app

1. Založ repo na GitHubu, nahraj tam tuhle složku
2. Na railway.app → New Project → Deploy from GitHub
3. V Railway nastav env proměnné:
   - `OPENROUTER_API_KEY`
   - (volitelně) `LIDL_PDF_CESTA`, `ALBERT_PDF_CESTA`, `KAUFLAND_PDF_CESTA`
4. Railway automaticky spustí `Procfile` → API poběží na veřejné URL
5. **Scraper se SAMO nespustí periodicky** - potřebuješ:
   - Railway "Cron Job" service (`python scraper.py` 1x denně/týdně), nebo
   - GitHub Actions scheduled workflow
6. **DŮLEŽITÉ**: `pdftotext` vyžaduje `poppler-utils` v runtime prostředí.
   Přidej `nixpacks.toml` nebo `Dockerfile` s `apt-get install poppler-utils`
   (napiš, pomůžu to nastavit).

## Náklady

| Položka | Cena |
|---|---|
| Railway hosting (API 24/7) | $0-5/měsíc (free tier obvykle stačí) |
| OpenRouter AI (textový model, bez vision) | ~$0.0005-0.002 za stránku letáku |
| 3 obchody × ~30-60 stran × 1x týdně | odhad $0.02-0.20/měsíc |

Tvých $5 na OpenRouter by mělo vydržet **mnoho měsíců**.

## Co zbývá dodělat

- [ ] Ověřit automatické zjišťování URL PDF pro Albert a Kaufland (funkce
      `ziskat_pdf_url_albert()` a `ziskat_pdf_url_kaufland()` jsou hotové,
      ale URL hádání nebylo live testováno - pokud selžou, stáhni PDF manuálně)
- [ ] Nastavit periodické spouštění scraperu (cron/GitHub Actions)
- [ ] Přidat `nixpacks.toml` pro Railway s `poppler-utils`
- [ ] Napojit appku `nakup-porovnavac.tsx` na API endpoint

## Napojení appky na API

```javascript
const res = await fetch("https://tvoje-api.up.railway.app/produkty");
const data = await res.json();
```

Tohle přepojení appky ti rád udělám hned, jak bude mít API reálná data - napiš.
