import os, asyncio, requests, sqlite3, re, logging
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- CREDENTIALS (Double check these!) ---
TELEGRAM_TOKEN = "8247420186:AAFH2UALqnWulQgQVgyQCaRoYsuaLggy5ko"
GEMINI_KEY = "AIzaSyAzR1d31UpVbkoHMAdckTJw9gR5UOOPJ5s"
NEWS_KEY = "f202eef0c1dc46dbad354b752a8b534b"
FREE_CRYPTO_KEY = "xepv32wtw9xgksv1bsga"

genai.configure(api_key=GEMINI_KEY)
ai_model = genai.GenerativeModel('gemini-1.5-flash')

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
    # STRICT INSTRUCTION TO GEMINI TO STAY IN ENGLISH
    prompt = f"Summarize this into 2 short sentences in ENGLISH only. Use emojis. DATA: {text}"
    try:
        response = await ai_model.generate_content_async(prompt)
        return response.text
    except: return f"📍 **{mode.upper()} Update**\n{text[:100]}..."

# --- ENGINES ---
async def fb_engine(context):
    from playwright.async_api import async_playwright
    job = context.job
    try:
        geo = requests.get(f"https://nominatim.openstreetmap.org/search?q={job.data['city']}&format=json&limit=1", headers={'User-Agent':'SniperBot'}).json()
        lat, lon = geo[0]['lat'], geo[0]['lon']
        async with async_playwright() as p:
            # Added User Agent to stop FB from blocking the bot
            browser = await p.chromium.launch(headless=True)
            context_browser = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36")
            page = await context_browser.new_page()
            url = f"https://www.facebook.com/marketplace/search/?query={job.data['item']}&latitude={lat}&longitude={lon}&radius_in_km={job.data['radius']}"
            await page.goto(url, wait_until="networkidle"); await asyncio.sleep(8)
            
            listings = await page.locator('div[style*="max-width:381px"]').all()
            for item in listings[:3]:
                raw = await item.inner_text()
                link_el = await item.locator('a').first()
                href = "https://facebook.com" + await link_el.get_attribute('href')
                if is_new(href):
                    pretty = await ai_gen(raw, "Marketplace")
                    await context.bot.send_message(chat_id=job.chat_id, text=f"{pretty}\n🔗 {href}", parse_mode="Markdown")
            await browser.close()
    except Exception as e: print(f"FB Error: {e}")

async def crypto_engine(context):
    job = context.job
    headers = {"Authorization": f"Bearer {FREE_CRYPTO_KEY}"}
    coins = job.data['coins']
    if coins.upper() == "ALL":
        try:
            r_list = requests.get("https://api.freecryptoapi.com/v1/getCryptoList", headers=headers).json()
            coins = ",".join([c['symbol'] for c in r_list[:50]])
        except: coins = "BTC,ETH,SOL,BNB,XRP"

    for sym in coins.split(','):
        sym = sym.strip().upper()
        try:
            r = requests.get(f"https://api.freecryptoapi.com/v1/getData?symbol={sym}", headers=headers).json()
            data = r[0] if isinstance(r, list) else r
            p = data.get('price')
            if price_has_changed(sym, p):
                pretty = await ai_gen(f"{sym} is ${p}", "crypto")
                await context.bot.send_message(chat_id=job.chat_id, text=pretty, parse_mode="Markdown")
        except: continue

async def news_engine(context):
    job = context.job
    # FIXED: Added language=en and searchIn=title for precision
    url = f"https://newsapi.org/v2/everything?qInTitle={job.data['q']}&language=en&sortBy=publishedAt&apiKey={NEWS_KEY}"
    try:
        r = requests.get(url).json()
        for art in r.get('articles', [])[:3]:
            if is_new(art['url']):
                summary = await ai_gen(f"{art['title']} - {art['description']}", "news")
                await context.bot.send_message(chat_id=job.chat_id, text=f"{summary}\n🔗 {art['url']}", parse_mode="Markdown")
    except: pass

# --- HANDLERS ---
def p_time(t):
    m = re.search(r'(\d+)([hm])', str(t).lower())
    if not m: return 1
    return int(m.group(1)) * 60 if m.group(2) == 'h' else int(m.group(1))

async def fb_cmd(u, c):
    if len(c.args) < 4: return
    t = p_time(c.args[-1])
    c.job_queue.run_repeating(fb_engine, interval=t*60, first=1, data={'item':c.args[0],'city':c.args[1],'radius':c.args[2]}, chat_id=u.effective_chat.id, name=f"FB_{u.effective_chat.id}")
    await u.message.reply_text(f"🎯 FB Sniper Active for {c.args[0]}")

async def cry_cmd(u, c):
    if len(c.args) < 2: return
    t = p_time(c.args[0])
    c.job_queue.run_repeating(crypto_engine, interval=t*60, first=1, data={'coins':c.args[1]}, chat_id=u.effective_chat.id, name=f"CRYPTO_{u.effective_chat.id}")
    await u.message.reply_text("🚀 Crypto Sniper Active!")

async def news_cmd(u, c):
    if len(c.args) < 2: return
    t = p_time(c.args[0])
    c.job_queue.run_repeating(news_engine, interval=t*60, first=1, data={'q':c.args[1]}, chat_id=u.effective_chat.id, name=f"NEWS_{u.effective_chat.id}")
    await u.message.reply_text(f"📰 News Sniper Active for {c.args[1]}")

async def stop(u, c):
    cid = str(u.effective_chat.id)
    target = c.args[0].upper() if c.args else "ALL"
    jobs = c.job_queue.jobs()
    removed = 0
    for j in jobs:
        if cid in str(j.name) and (target == "ALL" or target in j.name):
            j.schedule_removal(); removed += 1
    await u.message.reply_text(f"🛑 Stopped {removed} sniper(s).")

if __name__ == "__main__":
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("facebook", fb_cmd))
    app.add_handler(CommandHandler("crypto", cry_cmd))
    app.add_handler(CommandHandler("news", news_cmd))
    app.add_handler(CommandHandler("stop", stop))
    print("ULTIMATE SNIPER ONLINE. 🚀")
    app.run_polling()
