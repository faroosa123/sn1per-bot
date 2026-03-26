import os, asyncio, requests, sqlite3, re, logging
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- CREDENTIALS ---
TELEGRAM_TOKEN = "8632300395:AAFn17SlsVan4GqkEfXKZx0QqmzCwa7zMg8"
GEMINI_KEY = "AIzaSyAzR1d31UpVbkoHMAdckTJw9gR5UOOPJ5s"
NEWS_KEY = "f202eef0c1dc46dbad354b752a8b534b"
FREE_CRYPTO_KEY = "xepv32wtw9xgksv1bsga"

genai.configure(api_key=GEMINI_KEY)
ai_model = genai.GenerativeModel('gemini-1.5-flash')

# --- DATABASE ---
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
    prompt = f"Translate and summarize this {mode} into 2 short English sentences with emojis. Focus on facts. DATA: {text}"
    try:
        response = await ai_model.generate_content_async(prompt)
        return response.text
    except: return f"📍 **{mode.upper()} Update**\n{text[:100]}..."

# --- ENGINES ---
async def fb_engine(context):
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
    except Exception as e: print(f"FB Engine Error: {e}")

async def crypto_engine(context):
    job = context.job
    headers = {"Authorization": f"Bearer {FREE_CRYPTO_KEY}"}
    for sym in job.data['coins'].split(','):
        sym = sym.strip().upper()
        if sym == "ALL": continue
        try:
            r = requests.get(f"https://api.freecryptoapi.com/v1/getData?symbol={sym}", headers=headers).json()
            data = r[0] if isinstance(r, list) else r
            p = data.get('price')
            if price_has_changed(sym, p):
                pretty = await ai_gen(f"{sym} price is {p}", "crypto")
                await context.bot.send_message(chat_id=job.chat_id, text=pretty, parse_mode="Markdown")
        except: continue

async def news_engine(context):
    job = context.job
    # FIXED: Added 'qInTitle' to make searches way more accurate
    url = f"https://newsapi.org/v2/everything?qInTitle={job.data['q']}&language=en&sortBy=publishedAt&apiKey={NEWS_KEY}"
    try:
        r = requests.get(url).json()
        for art in r.get('articles', [])[:2]:
            if is_new(art['url']):
                summary = await ai_gen(f"{art['title']} - {art['description']}", "news")
                await context.bot.send_message(chat_id=job.chat_id, text=f"{summary}\n🔗 {art['url']}", parse_mode="Markdown")
    except: pass

# --- COMMANDS ---
async def status(update, context):
    cid = str(update.effective_chat.id)
    active = [j.name for j in context.job_queue.jobs() if cid in j.name]
    msg = "🎯 **Active Snipers:**\n" + "\n".join([f"• {n.split('_')[0]}" for n in active])
    await update.message.reply_text(msg if active else "📭 No active snipers.")

async def fb_cmd(u, c):
    if len(c.args) < 4: return
    t = int(re.search(r'\d+', c.args[-1]).group()) * (60 if 'h' in c.args[-1] else 1)
    c.job_queue.run_repeating(fb_engine, interval=t*60, first=1, data={'item':c.args[0],'city':c.args[1],'radius':c.args[2]}, chat_id=u.effective_chat.id, name=f"FB_{u.effective_chat.id}")
    await u.message.reply_text(f"🎯 FB Sniper started for {c.args[0]}")

async def cry_cmd(u, c):
    if not c.args or c.args[1].upper() == "ALL":
        await u.message.reply_text("❌ Specify coins like: `/crypto 1m BTC,ETH`")
        return
    t = int(re.search(r'\d+', c.args[0]).group()) * (60 if 'h' in c.args[0] else 1)
    c.job_queue.run_repeating(crypto_engine, interval=t*60, first=1, data={'coins':c.args[1]}, chat_id=u.effective_chat.id, name=f"CRYPTO_{u.effective_chat.id}")
    await u.message.reply_text(f"🚀 Crypto Sniper Active!")

async def news_cmd(u, c):
    if len(c.args) < 2: return
    t = int(re.search(r'\d+', c.args[0]).group()) * (60 if 'h' in c.args[0] else 1)
    c.job_queue.run_repeating(news_engine, interval=t*60, first=1, data={'q':c.args[1]}, chat_id=u.effective_chat.id, name=f"NEWS_{u.effective_chat.id}")
    await u.message.reply_text(f"📰 News Sniper Active for {c.args[1]}")

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
    app.add_handlers([CommandHandler("facebook", fb_cmd), CommandHandler("crypto", cry_cmd), CommandHandler("news", news_cmd), CommandHandler("status", status), CommandHandler("stop", stop)])
    print("SNIPER v4 READY. 🥕")
    app.run_polling()
