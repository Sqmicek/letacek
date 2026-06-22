"""
API server - vystavuje data z databáze (naplněné scraper.py) jako REST API
pro frontend appku (nakup-porovnavac).

POUŽITÍ (lokálně):
    pip install -r requirements.txt
    uvicorn api:app --reload --port 8000

ENDPOINTY:
    GET /produkty                  - všechny aktuální produkty, volitelně ?obchod=albert
    GET /hledat?q=mleko            - fulltextové hledání podle názvu produktu
    GET /obchody                   - seznam obchodů a kdy byly naposledy zaktualizovány
    GET /porovnat?produkty=mleko,rohliky  - porovnání cen víc produktů najednou
"""

import os
import sqlite3
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "ceny.db")

app = FastAPI(title="Porovnávač cen letáků - API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/")
def root():
    return {"status": "ok", "message": "Porovnávač cen API běží. Viz /docs pro endpointy."}


@app.get("/produkty")
def produkty(obchod: Optional[str] = None):
    conn = get_conn()
    try:
        if obchod:
            rows = conn.execute(
                "SELECT * FROM produkty WHERE obchod = ? ORDER BY nazev", (obchod,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM produkty ORDER BY obchod, nazev").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/hledat")
def hledat(q: str = Query(..., min_length=1, description="Hledaný text v názvu produktu")):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM produkty WHERE nazev LIKE ? ORDER BY cena ASC",
            (f"%{q}%",),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/obchody")
def obchody():
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT obchod, MAX(scraped_at) as posledni_update, COUNT(*) as pocet_produktu
               FROM produkty GROUP BY obchod"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/porovnat")
def porovnat(produkty: str = Query(..., description="Čárkou oddělené hledané výrazy, např. mleko,rohliky")):
    """Pro každý zadaný výraz vrátí nejlevnější nalezenou shodu v každém obchodě."""
    conn = get_conn()
    try:
        vysledek = {}
        for vyraz in [p.strip() for p in produkty.split(",") if p.strip()]:
            rows = conn.execute(
                "SELECT * FROM produkty WHERE nazev LIKE ? ORDER BY cena ASC",
                (f"%{vyraz}%",),
            ).fetchall()
            vysledek[vyraz] = [dict(r) for r in rows]
        return vysledek
    finally:
        conn.close()


@app.get("/zdravi")
def zdravi():
    """Health check pro hosting (Railway apod.)."""
    if not os.path.exists(DB_PATH):
        raise HTTPException(status_code=503, detail="Databáze ještě neexistuje - spusť scraper.py")
    return {"status": "ok"}
