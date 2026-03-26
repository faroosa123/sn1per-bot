import os, asyncio, requests, sqlite3, re, logging
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- LOGGING ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CREDENTIALS ---
TELEGRAM_TOKEN = "8632300395:AAFn17SlsVan4GqkEfXKZx0QqmzCwa7zMg8"
GEMINI_KEY = "AIzaSyAzR1d31UpVbkoHMAdckTJw9gR5UOOPJ5s"
NEWS_KEY = "f202eef0c1dc46dbad354b752a8b534b"
FREE_CRYPTO_KEY = "xepv32wtw9xgksv1bsga"

genai.configure(api_key=GEMINI_KEY)
ai_model = genai.GenerativeModel('gemini-1.5-flash')

# --- DATABASE LOGIC ---
def init_db():
    conn = sqlite3.connect('sniper.db')
    conn.execute('CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS prices (symbol TEXT PRIMARY KEY, last_price TEXT)')
    conn.commit(); conn.close()

def is_new(item_id):
    conn = sqlite3.connect('sniper.db')
    exists = conn.execute('SELECT 1 FROM seen WHERE id=?', (item_id,)).fetchone()
    if not exists:
        conn.execute('INSERT INTO seen VALUES (?)', (item_id,))
        conn.commit(); conn.close(); return True
    conn.close(); return False

def price_has_changed(symbol, current_price):
    conn = sqlite3.connect('sniper.db')
    row = conn.execute('SELECT last_price FROM prices WHERE symbol=?', (symbol,)).fetchone()
    if row is None or row[0] != str(current_price):
        conn.execute('INSERT OR REPLACE INTO prices VALUES (?, ?)', (symbol, str(current_price)))
        conn.commit(); conn.close(); return True
    conn.close(); return False

async def ai_gen(text, mode="news"):
    prompt = f"Summarize this {mode} into 2 punchy English sentences with emojis. Focus on the core facts. DATA: {text}"
    try:
        response = await ai_model.generate_content_async(prompt)
        return response.text
    except: return f"📍 **{mode.upper()} Update**\n{text[:100]}..."

# --- ENGINES ---
async def fb_engine(context: ContextTypes.DEFAULT_TYPE):
    from playwright.async_api import async_playwright
    job = context.job
    try:
        geo = requests.get(f"https://nominatim.openstreetmap.org/search?q={job.data['city']}&format=json&limit=1", headers={'User-Agent':'Sniper'}).json()
        lat, lon = geo[0]['lat'], geo[0]['lon']
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            url = f"https://www.facebook.com/marketplace/search/?query={job.data['item']}&latitude={lat}&longitude={lon}&radius_in_km={job.data['radius']}"
            await page.goto(url); await asyncio.sleep(10)
            listings = await page.locator('div[style*="max-width:381px"]').all()
            print(f"DEBUG: Found {len(listings)} FB items for {job.data['item']}")
            for item in listings[:3]:
                raw = await item.inner_text()
                link_el = await item.locator('a').first()
                href = "https://facebook.com" + await link_el.get_attribute('href')
                if is_new(href):
                    pretty = await ai_gen(raw, "Marketplace")
                    await context.bot.send_message(chat_id=job.chat_id, text=f"{pretty}\n🔗 {href}", parse_mode="Markdown")
            await browser.close()
    except Exception as e: logger.error(f"FB Error: {e}")

async def crypto_engine(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    headers = {"Authorization": f"Bearer {FREE_CRYPTO_KEY}"}
    coins_to_check = job.data['coins']

    # --- MAGIC "ALL" LOGIC ---
    if coins_to_check.upper() == "ALL":
        try:
            r_list = requests.get("https://api.freecryptoapi.com/v1/getCryptoList", headers=headers).json()
            coins_to_check = ",".join([c['symbol'] for c in r_list[:50]]) # Top 50
        except:
            coins_to_check = "BTC,ETH,SOL,BNB,XRP,ADA,DOGE,TRX,TON,DOT"

    for sym in coins_to_check.split(','):
        sym = sym.strip().upper()
        try:
            r = requests.get(f"https://api.freecryptoapi.com/v1/getData?symbol={sym}", headers=headers).json()
            data = r[0] if isinstance(r, list) else r
            p = data.get('price')
            if price_has_changed(sym, p):
                msg = f"{sym} is ${p}. 24h Change: {data.get('change_24h')}%"
                pretty = await ai_gen(msg, "crypto")
                await context.bot.send_message(chat_id=job.chat_id, text=pretty, parse_mode="Markdown")
        except: continue

async def news_engine(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    # qInTitle ensures we don't get random "Salah" news if searching for "Qatar"
    url = f"https://newsapi.org/v2/everything?qInTitle={job.data['q']}&language=en&sortBy=publishedAt&apiKey={NEWS_KEY}"
    try:
        r = requests.get(url).json()
        for art in r.get('articles', [])[:3]:
            if is_new(art['url']):
                summary = await ai_gen(f"{art['title']} - {art['description']}", "news")
                await context.bot.send_message(chat_id=job.chat_id, text=f"{summary}\n🔗 {art['url']}", parse_mode="Markdown")
    except: pass

# --- COMMANDS ---
def parse_time(t):
    m = re.search(r'(\d+)([hm])', str(t).lower())
    if not m: return 1
    val, unit = int(m.group(1)), m.group(2)
    return val * 60 if unit == 'h' else val

async def fb_cmd(u, c):
    if len(c.args) < 4:
        await u.message.reply_text("❌ `/facebook \"Item Name\" City Radius Time`")
        return
    t = parse_time(c.args[-1])
    c.job_queue.run_repeating(fb_engine, interval=t*60, first=1, data={'item':c.args[0],'city':c.args[1],'radius':c.args[2]}, chat_id=u.effective_chat.id, name=f"FB_{u.effective_chat.id}")
    await u.message.reply_text(f"🎯 FB Sniper Active for {c.args[0]}!")

async def cry_cmd(u, c):
    if len(c.args) < 2:
        await u.message.reply_text("❌ `/crypto 1m BTC` or `/crypto 1m ALL`")
        return
    t_str = c.args[0]
    t = parse_time(t_str)
    job_name = f"CRYPTO_{u.effective_chat.id}"
    # Stop existing crypto job if running
    for j in c.job_queue.get_jobs_by_name(job_name): j.schedule_removal()
    
    c.job_queue.run_repeating(crypto_engine, interval=t*60, first=1, data={'coins':c.args[1]}, chat_id=u.effective_chat.id, name=job_name)
    await u.message.reply_text(f"🚀 Crypto Sniper Active (Every {t_str})")

async def news_cmd(u, c):
    if len(c.args) < 2: return
    t_str = c.args[0]
    t = parse_time(t_str)
    c.job_queue.run_repeating(news_engine, interval=t*60, first=1, data={'q':c.args[1]}, chat_id=u.effective_chat.id, name=f"NEWS_{u.effective_chat.id}")
    await u.message.reply_text(f"📰 News Sniper Active for {c.args[1]} (Every {t_str})")

async def status(u, c):
    cid = str(u.effective_chat.id)
    active = [j.name.split('_')[0] for j in c.job_queue.jobs() if cid in j.name]
    msg = "🎯 **Active Snipers:**\n" + "\n".join([f"• {n}" for n in active])
    await u.message.reply_text(msg if active else "📭 No active snipers.")

async def stop(u, c):
    cid = str(u.effective_chat.id)
    target = c.args[0].upper() if c.args else "ALL"
    removed = 0
    for j in c.job_queue.jobs():
        if cid in j.name and (target == "ALL" or target in j.name):
            j.schedule_removal(); removed += 1
    await u.message.reply_text(f"🛑 Stopped {removed} sniper(s).")

if __name__ == "__main__":
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("facebook", fb_cmd))
    app.add_handler(CommandHandler("crypto", cry_cmd))
    app.add_handler(CommandHandler("news", news_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop))
    print("ULTIMATE SNIPER v5 ONLINE. 🚀")
    app.run_polling()
