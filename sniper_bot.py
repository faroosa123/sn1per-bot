import os
import asyncio
import requests
import sqlite3
import re
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

# --- DATABASE LOGIC ---
def init_db():
    conn = sqlite3.connect('sniper.db')
    conn.execute('CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS prices (symbol TEXT PRIMARY KEY, last_price TEXT)')
    conn.commit()
    conn.close()

def is_new(item_id):
    conn = sqlite3.connect('sniper.db')
    exists = conn.execute('SELECT 1 FROM seen WHERE id=?', (item_id,)).fetchone()
    if not exists:
        conn.execute('INSERT INTO seen VALUES (?)', (item_id,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def price_has_changed(symbol, current_price):
    conn = sqlite3.connect('sniper.db')
    row = conn.execute('SELECT last_price FROM prices WHERE symbol=?', (symbol,)).fetchone()
    if row is None or row[0] != str(current_price):
        conn.execute('INSERT OR REPLACE INTO prices VALUES (?, ?)', (symbol, str(current_price)))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

async def get_ai_summary(raw_data, type="news"):
    """Uses Gemini to create those smart summaries you asked for."""
    if type == "news":
        prompt = f"Summarize this news article into 2 short, punchy sentences. Use emojis. Format: [Headline] \n [Summary]. DATA: {raw_data}"
    else:
        prompt = f"Beautify this data for Telegram. Bold the important parts. DATA: {raw_data}"
    
    try:
        response = await ai_model.generate_content_async(prompt)
        return response.text
    except:
        return f"🚨 New Update Found!\n{raw_data[:100]}..."

# --- ENGINES ---
async def news_engine(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    url = f"https://newsapi.org/v2/everything?q={job.data['q']}&sortBy=publishedAt&apiKey={NEWS_KEY}"
    try:
        r = requests.get(url).json()
        articles = r.get('articles', [])
        for art in articles[:3]: # Check top 3 latest
            if is_new(art['url']):
                raw_info = f"Title: {art['title']} Content: {art['description']}"
                summary = await get_ai_summary(raw_info, "news")
                message = f"{summary}\n\n🔗 [Read More]({art['url']})"
                await context.bot.send_message(chat_id=job.chat_id, text=message, parse_mode="Markdown")
    except Exception as e: print(f"News Error: {e}")

async def crypto_engine(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    headers = {"Authorization": f"Bearer {FREE_CRYPTO_KEY}"}
    for sym in job.data['coins'].split(','):
        sym = sym.strip().upper()
        try:
            r = requests.get(f"https://api.freecryptoapi.com/v1/getData?symbol={sym}", headers=headers).json()
            data = r[0] if isinstance(r, list) else r
            curr_price = data.get('price')
            if price_has_changed(sym, curr_price):
                msg = f"Symbol: {sym}, Price: {curr_price}, Change: {data.get('change_24h')}%"
                pretty = await get_ai_summary(msg, "crypto")
                await context.bot.send_message(chat_id=job.chat_id, text=pretty, parse_mode="Markdown")
        except: continue

async def fb_engine(context: ContextTypes.DEFAULT_TYPE):
    from playwright.async_api import async_playwright
    job = context.job
    try:
        geo = requests.get(f"https://nominatim.openstreetmap.org/search?q={job.data['city']}&format=json&limit=1", headers={'User-Agent':'Sniper'}).json()
        lat, lon = geo[0]['lat'], geo[0]['lon']
        async with async_playwright() as p:
            browser = await p.chromium.launch_persistent_context(user_data_dir="./fb_session", headless=True)
            page = await browser.new_page()
            url = f"https://www.facebook.com/marketplace/search/?query={job.data['item']}&latitude={lat}&longitude={lon}&radius_in_km={job.data['radius']}"
            await page.goto(url)
            await asyncio.sleep(10)
            listings = await page.locator('div[style*="max-width:381px"]').all()
            for item in listings[:3]:
                raw = await item.inner_text()
                link_el = await item.locator('a').first()
                href = "https://facebook.com" + await link_el.get_attribute('href')
                if is_new(href):
                    pretty = await get_ai_summary(raw, "crypto") # Reuse styler
                    await context.bot.send_message(chat_id=job.chat_id, text=f"{pretty}\n🔗 {href}", parse_mode="Markdown")
            await browser.close()
    except Exception as e: print(f"FB Error: {e}")

# --- COMMAND HELPERS ---
def parse_time(t_str):
    m = re.match(r"(\d+)([hm])", str(t_str).lower())
    if not m: return 1
    v, u = int(m.group(1)), m.group(2)
    return v * 60 if u == 'h' else v

# --- TELEGRAM COMMANDS ---
async def fb_cmd(update, context):
    if len(context.args) < 4: return
    mins = parse_time(context.args[-1])
    # Handles multi-word items if typed correctly
    item = context.args[0] 
    context.job_queue.run_repeating(fb_engine, interval=mins*60, first=1, data={'item':item,'city':context.args[1],'radius':context.args[2]}, chat_id=update.effective_chat.id, name=f"fb_{update.effective_chat.id}")
    await update.message.reply_text(f"🎯 FB Sniper Active for {item}!")

async def cry_cmd(update, context):
    if len(context.args) < 2: return
    mins = parse_time(context.args[0])
    context.job_queue.run_repeating(crypto_engine, interval=mins*60, first=1, data={'coins':context.args[1]}, chat_id=update.effective_chat.id, name=f"cry_{update.effective_chat.id}")
    await update.message.reply_text(f"🚀 Crypto Sniper Active!")

async def news_cmd(update, context):
    if len(context.args) < 2: return
    mins = parse_time(context.args[0])
    context.job_queue.run_repeating(news_engine, interval=mins*60, first=1, data={'q':context.args[1]}, chat_id=update.effective_chat.id, name=f"news_{update.effective_chat.id}")
    await update.message.reply_text(f"📰 News Sniper Active for {context.args[1]}!")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    target = context.args[0].lower() if context.args else "all"
    job_map = {"facebook": f"fb_{cid}", "crypto": f"cry_{cid}", "news": f"news_{cid}"}
    
    removed = False
    if target == "all":
        for name in job_map.values():
            for j in context.job_queue.get_jobs_by_name(name): j.schedule_removal(); removed = True
    elif target in job_map:
        for j in context.job_queue.get_jobs_by_name(job_map[target]): j.schedule_removal(); removed = True

    await update.message.reply_text(f"🛑 Stopped {target}" if removed else "Nothing to stop.")

if __name__ == "__main__":
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("facebook", fb_cmd))
    app.add_handler(CommandHandler("crypto", cry_cmd))
    app.add_handler(CommandHandler("news", news_cmd))
    app.add_handler(CommandHandler("stop", stop))
    print("ULTIMATE SNIPER v2 ONLINE. 🥕")
    app.run_polling()