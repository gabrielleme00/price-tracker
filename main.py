import os
import sqlite3
import asyncio
import re
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncScheduler
from apscheduler.triggers.interval import IntervalTrigger
from playwright.async_api import async_playwright
import requests

app = FastAPI(title="Self-Hosted Price Tracker")

# Mount static frontend
os.makedirs("static", exist_ok=True)
os.makedirs("data", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

DB_PATH = "data/tracker.db"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            store TEXT NOT NULL,
            url TEXT NOT NULL,
            css_selector TEXT NOT NULL,
            target_price REAL NOT NULL,
            active BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            price REAL,
            status TEXT,
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()

init_db()

class ProductCreate(BaseModel):
    name: str
    store: str
    url: str
    css_selector: str
    target_price: float

class ProductUpdate(BaseModel):
    name: Optional[str] = None
    target_price: Optional[float] = None
    css_selector: Optional[str] = None
    active: Optional[bool] = None

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Telegram notification error: {e}")

def parse_price_string(price_str: str) -> float:
    cleaned = re.sub(r"[^\d,\.]", "", price_str)
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    return float(cleaned)

async def run_scraping_job():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products WHERE active = 1")
    products = cursor.fetchall()
    conn.close()

    if not products:
        return

    digest_lines = ["📊 *Scheduled Price Check Report*"]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        
        for prod in products:
            price = None
            status = "ok"
            try:
                page = await context.new_page()
                # Optimize: block heavy assets
                await page.route("**/*.{png,jpg,jpeg,gif,svg,css,font,woff,woff2}", lambda route: route.abort())
                
                await page.goto(prod["url"], wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_selector(prod["css_selector"], timeout=10000)
                price_text = await page.inner_text(prod["css_selector"])
                price = parse_price_string(price_text)
                await page.close()
            except Exception as e:
                status = f"error: {str(e)[:50]}"
                print(f"Failed scraping {prod['name']}: {e}")

            # Save to DB
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO price_history (product_id, price, status) VALUES (?, ?, ?)",
                (prod["id"], price, status)
            )
            conn.commit()
            conn.close()

            # Format digest line
            if price is not None:
                is_trigger = price <= prod["target_price"]
                prefix = "🚨 " if is_trigger else "▪️ "
                line = f"{prefix}*{prod['name']}* ({prod['store']}): R$ {price:.2f} (Alvo: R$ {prod['target_price']:.2f})"
                digest_lines.append(line)
            else:
                digest_lines.append(f"⚠️ *{prod['name']}*: Falha ao extrair preço.")
            
            await asyncio.sleep(5)  # Jitter between requests

        await browser.close()

    send_telegram("\n".join(digest_lines))

# Scheduler setup
scheduler = AsyncScheduler()
# Uncomment to enable automatic 3-hour interval:
# scheduler.add_job(run_scraping_job, IntervalTrigger(hours=3))

@app.on_event("startup")
async def startup_event():
    await scheduler.start()

# --- API Endpoints ---
@app.get("/")
def read_index():
    return {"message": "Price Tracker API is running. Access UI at /static/index.html"}

@app.get("/api/products")
def get_products():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        p.*, 
        (SELECT price FROM price_history WHERE product_id = p.id ORDER BY scraped_at DESC LIMIT 1) as last_price,
        (SELECT status FROM price_history WHERE product_id = p.id ORDER BY scraped_at DESC LIMIT 1) as last_status,
        (SELECT scraped_at FROM price_history WHERE product_id = p.id ORDER BY scraped_at DESC LIMIT 1) as last_scraped
        FROM products p
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

@app.post("/api/products")
def add_product(prod: ProductCreate):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO products (name, store, url, css_selector, target_price) VALUES (?, ?, ?, ?, ?)",
        (prod.name, prod.store, prod.url, prod.css_selector, prod.target_price)
    )
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.put("/api/products/{product_id}")
def update_product(product_id: int, prod: ProductUpdate):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    for field, val in prod.dict(exclude_unset=True).items():
        cursor.execute(f"UPDATE products SET {field} = ? WHERE id = ?", (val, product_id))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.delete("/api/products/{product_id}")
def delete_product(product_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM products WHERE id = ?", (product_id,))
    cursor.execute("DELETE FROM price_history WHERE product_id = ?", (product_id,))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.get("/api/history/{product_id}")
def get_history(product_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT price, status, scraped_at FROM price_history WHERE product_id = ? ORDER BY scraped_at ASC LIMIT 100", (product_id,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

@app.post("/api/scrape-now")
async def scrape_now():
    asyncio.create_task(run_scraping_job())
    return {"status": "Scrape job triggered in background"}