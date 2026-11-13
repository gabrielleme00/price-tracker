import os
import sqlite3
import asyncio
import time
import logging
import sys
from typing import Optional, Dict, List
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import requests

from utils.browser_config import get_browser_context
from utils.page_handler import setup_page_handlers
from utils.captcha_monitor import with_captcha_check, CaptchaDetected

DEBUG_SAVE_DEBUG = True
DB_PATH = "data/tracker.db"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("price-tracker")

app = FastAPI(title="Self-Hosted Price Tracker")

os.makedirs("static", exist_ok=True)
os.makedirs("data", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            store TEXT NOT NULL,
            url TEXT NOT NULL,
            image_url TEXT DEFAULT '',
            target_price REAL NOT NULL,
            active BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    try:
        cursor.execute("ALTER TABLE products ADD COLUMN image_url TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            price REAL,
            status TEXT,
            sizes TEXT DEFAULT '',
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        )
    """)
    # Migração: adiciona coluna sizes em bancos antigos
    try:
        cursor.execute("ALTER TABLE price_history ADD COLUMN sizes TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


init_db()


class ProductCreate(BaseModel):
    name: str
    store: str
    url: str
    target_price: float


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    target_price: Optional[float] = None
    active: Optional[bool] = None


def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Token ou Chat ID do Telegram não configurados.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=20)
            if resp.status_code == 200:
                logger.info("Notificação enviada com sucesso para o Telegram.")
                return
            else:
                logger.error(f"Erro ao enviar Telegram (Tentativa {attempt}/{max_retries}): {resp.text}")
        except Exception as e:
            logger.error(f"Exceção ao enviar notificação Telegram (Tentativa {attempt}/{max_retries}): {e}")

        if attempt < max_retries:
            time.sleep(3)


def process_image_url(image_url: str) -> str:
    """Converte URL de thumbnail para imagem em tamanho real."""
    if not image_url:
        return ""
    base_url = image_url.split('_thumbnail_')[0]
    if base_url.startswith('//'):
        base_url = 'https:' + base_url
    if not any(base_url.endswith(ext) for ext in ('.jpg', '.png', '.jpeg', '.webp')):
        base_url += '.jpg'
    return base_url


def _utc_iso(ts: Optional[str]) -> Optional[str]:
    """
    SQLite CURRENT_TIMESTAMP returns 'YYYY-MM-DD HH:MM:SS' (UTC, no tz marker).
    The browser interprets that string as LOCAL time and shifts it incorrectly.
    We normalize to ISO 8601 with a 'Z' suffix so JS converts UTC → local properly.
    """
    if not ts:
        return ts
    # Already has a tz marker / ISO format? Leave it alone.
    if "T" in ts or ts.endswith("Z") or "+" in ts[10:]:
        return ts
    return ts.replace(" ", "T") + "Z"


@with_captcha_check
def navigate_to_product(page, url: str, delay: int = 2):
    """Navega para a URL verificando se houve redirecionamento para captcha."""
    page.goto(url)
    time.sleep(delay)


@with_captcha_check
def scrape_single_shein_product(page, prod: Dict) -> Dict:
    """Extrai o preço, a primeira foto e os tamanhos disponíveis de 1 URL da SHEIN."""
    retry_count = 0
    max_retries = 3
    url = prod["url"]

    while retry_count < max_retries:
        try:
            navigate_to_product(page, url)

            page.wait_for_selector(
                ".product-intro__head-sku-text, .product-intro__head-name",
                timeout=10000,
                state="visible"
            )

            price_element = page.locator(".productPrice__main").first
            raw_price = price_element.text_content().strip() if price_element else None

            if not raw_price:
                raise ValueError("Elemento '.productPrice__main' vazio ou não encontrado.")

            cleaned = raw_price.replace("R$", "").replace(".", "").replace(",", ".").strip()
            price = float(cleaned)

            image_url = ""
            try:
                page.wait_for_selector('.crop-image-container', timeout=5000)
                container = page.locator('.crop-image-container').first
                if container:
                    src = container.get_attribute('data-before-crop-src')
                    if src:
                        image_url = process_image_url(src)
            except Exception as e:
                logger.warning(f"Não foi possível extrair a imagem: {e}")

            # ---- Extração dos tamanhos disponíveis ----
            sizes: List[str] = []
            try:
                page.wait_for_selector(
                    '.product-intro__size-radio[aria-disabled="false"]',
                    timeout=3500,
                    state="visible"
                )
                size_nodes = page.locator('.product-intro__size-radio[aria-disabled="false"]').all()
                for node in size_nodes:
                    try:
                        txt = (node.text_content() or "").strip()
                        txt = " ".join(txt.split())
                        if txt and txt not in sizes:
                            sizes.append(txt)
                    except Exception:
                        continue
                logger.info(f"Tamanhos extraídos para '{prod['name']}': {sizes or 'nenhum'}")
            except Exception as e:
                logger.warning(f"Não foi possível extrair tamanhos de '{prod['name']}': {e}")

            return {"price": price, "image_url": image_url, "sizes": sizes}

        except CaptchaDetected:
            raise

        except Exception as e:
            retry_count += 1
            if retry_count >= max_retries:
                if DEBUG_SAVE_DEBUG:
                    try:
                        page.screenshot(path=f"/app/data/debug_product_{prod['id']}.png", full_page=True)
                        with open(f"/app/data/debug_product_{prod['id']}.html", "w", encoding="utf-8") as f:
                            f.write(page.content())
                    except Exception as debug_err:
                        logger.error(f"Erro ao salvar debug: {debug_err}")
                raise Exception(f"Falhou após {max_retries} tentativas: {str(e)}")

            logger.warning(f"Tentativa {retry_count}/{max_retries} falhou para '{prod['name']}': {e}")
            time.sleep(2 * retry_count)


def run_sync_batch_scrape(products: List[Dict]) -> List[str]:
    """Executa a varredura síncrona (1 URL por vez) reutilizando a mesma sessão do browser."""
    digest_lines = []
    price_dropped_any = False
    playwright, browser, context = get_browser_context()

    try:
        page = context.new_page()
        setup_page_handlers(page)

        logger.info("Aquecendo sessão na página inicial da SHEIN...")
        navigate_to_product(page, "https://shein.com")

        for prod in products:
            logger.info(f"Verificando [{prod['id']}] {prod['name']} -> {prod['url']}")

            last_recorded_price = _get_last_price(prod["id"])
            price = None
            image_url = ""
            sizes: List[str] = []
            status = "ok"

            try:
                result = scrape_single_shein_product(page, prod)
                price = result["price"]
                image_url = result["image_url"]
                sizes = result.get("sizes", [])
                logger.info(
                    f"Preço extraído para '{prod['name']}': R$ {price:.2f} | "
                    f"Tamanhos: {sizes or '—'} | Imagem: {image_url}"
                )
                time.sleep(2)

            except CaptchaDetected:
                status = "error: captcha_detected"
                logger.error(f"Captcha não resolvido em '{prod['name']}'. Interrompendo ciclo atual.")
                digest_lines.append(f"🛑 <b>{prod['name']}</b> - Bloqueado por Captcha.")
                _save_scrape_result(prod["id"], None, "", status)
                break

            except Exception as e:
                status = f"error: {str(e)[:50]}"
                logger.error(f"Erro ao raspar '{prod['name']}': {e}")
                digest_lines.append(f"⚠️ <b>{prod['name']}</b> - Falha ao extrair preço.")
                _save_scrape_result(prod["id"], None, "", status)
                continue

            _save_scrape_result(prod["id"], price, image_url, status, sizes)

            is_price_lower = last_recorded_price is None or price < last_recorded_price
            if is_price_lower:
                price_dropped_any = True

            is_target_reached = price <= prod["target_price"]

            prefix = "🚨 " if is_target_reached else ("📉 " if is_price_lower else "▪️ ")
            drop_notice = " <i>(Preço Abaixou!)</i>" if is_price_lower and last_recorded_price is not None else ""

            sizes_display = ", ".join(sizes) if sizes else "—"

            product_msg = (
                f"{prefix} <a href=\"{prod['url']}\"><b>{prod['name']}</b></a> ({prod['store']})\n"
                f"   💰 Preço: <b>R$ {price:.2f}</b>{drop_notice}\n"
                f"   🎯 Alvo: R$ {prod['target_price']:.2f}\n"
                f"   📏 Tamanhos: {sizes_display}"
            )
            digest_lines.append(product_msg)

    except CaptchaDetected:
        logger.error("Captcha detectado logo na página inicial (warm-up).")
        digest_lines.insert(0, "🛑 <b>SHEIN</b>: Bloqueio de Captcha na inicialização.")
    except Exception as e:
        logger.error(f"Erro crítico no navegador: {e}")
    finally:
        try:
            context.close()
            browser.close()
            playwright.stop()
        except Exception:
            pass

    if price_dropped_any:
        header = "🔥 <b>PRECINHO: Queda de preço detectada!</b> \n\n📊 <b>Relatório:</b>"
    else:
        header = "📊 <b>Relatório dos Precinhos</b>"

    digest_lines.insert(0, header)
    return digest_lines


def _get_last_price(product_id: int) -> Optional[float]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT price FROM price_history WHERE product_id = ? AND price IS NOT NULL ORDER BY scraped_at DESC LIMIT 1",
        (product_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def _save_scrape_result(
    product_id: int,
    price: Optional[float],
    image_url: str,
    status: str,
    sizes: Optional[List[str]] = None,
):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if image_url:
        cursor.execute("UPDATE products SET image_url = ? WHERE id = ?", (image_url, product_id))
    sizes_str = ",".join(sizes) if sizes else ""
    cursor.execute(
        "INSERT INTO price_history (product_id, price, status, sizes) VALUES (?, ?, ?, ?)",
        (product_id, price, status, sizes_str)
    )
    conn.commit()
    conn.close()


async def run_scraping_job():
    logger.info("--- Iniciando ciclo de varredura de preços ---")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM products WHERE active = 1")
    products = [dict(row) for row in cursor.fetchall()]
    conn.close()

    if not products:
        logger.info("Nenhum produto ativo cadastrado.")
        return

    digest_lines = await asyncio.to_thread(run_sync_batch_scrape, products)

    if len(digest_lines) > 1:
        send_telegram("\n\n".join(digest_lines))


scheduler = AsyncIOScheduler()
scheduler.add_job(
    run_scraping_job,
    CronTrigger(hour="*/3", minute=0),
    id="scraping_cron_job",
    replace_existing=True
)


@app.on_event("startup")
async def startup_event():
    scheduler.start()
    logger.info("Scheduler iniciado com sucesso.")


@app.get("/")
def read_index():
    return FileResponse("static/index.html")


@app.get("/api/products")
def get_products():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.*,
        (SELECT price FROM price_history WHERE product_id = p.id ORDER BY scraped_at DESC LIMIT 1) as last_price,
        (SELECT status FROM price_history WHERE product_id = p.id ORDER BY scraped_at DESC LIMIT 1) as last_status,
        (SELECT sizes FROM price_history WHERE product_id = p.id ORDER BY scraped_at DESC LIMIT 1) as last_sizes,
        (SELECT scraped_at FROM price_history WHERE product_id = p.id ORDER BY scraped_at DESC LIMIT 1) as last_scraped
        FROM products p
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    for r in rows:
        r["last_scraped"] = _utc_iso(r.get("last_scraped"))
    return rows


@app.post("/api/products")
def add_product(prod: ProductCreate):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO products (name, store, url, target_price) VALUES (?, ?, ?, ?)",
        (prod.name, prod.store, prod.url, prod.target_price)
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
    cursor.execute(
        "SELECT price, status, sizes, scraped_at FROM price_history "
        "WHERE product_id = ? ORDER BY scraped_at ASC LIMIT 100",
        (product_id,)
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    for r in rows:
        r["scraped_at"] = _utc_iso(r.get("scraped_at"))
    return rows


@app.post("/api/scrape-now")
async def scrape_now():
    logger.info("Requisição manual recebida no endpoint /api/scrape-now")
    asyncio.create_task(run_scraping_job())
    return {"status": "Scrape job triggered in background"}