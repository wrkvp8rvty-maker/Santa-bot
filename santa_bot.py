import os
import sqlite3
import random
import vk_api
import requests
import json
import uuid
import time
import urllib3
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TOKEN = os.environ.get("VK_TOKEN")
GROUP_ID = "242810690"
GIGACHAT_AUTH_KEY = os.environ.get("GIGACHAT_AUTH_KEY")

GIGACHAT_TOKEN = None
TOKEN_EXPIRES_AT = 0

# --- БАЗА ДАННЫХ ---
conn = sqlite3.connect("santa.db")
cur = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (vk_id INTEGER PRIMARY KEY, wishlist TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS games (id INTEGER PRIMARY KEY AUTOINCREMENT, creator_id INTEGER, peer_id INTEGER, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS participants (game_id INTEGER, user_id INTEGER, target_id INTEGER, PRIMARY KEY (game_id, user_id));
-- Новая таблица для хранения истории сообщений
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id INTEGER,
    user_id INTEGER,
    user_name TEXT,
    text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""")
conn.commit()

# --- VK ---
vk_session = vk_api.VkApi(token=TOKEN)
vk = vk_session.get_api()
longpoll = VkBotLongPoll(vk_session, GROUP_ID)

names_cache = {}

def send(peer_id, text):
    try:
        vk.messages.send(peer_id=peer_id, message=text, random_id=random.randint(1, 2**31 - 1))
    except Exception as e:
        print(f"Ошибка отправки: {e}")

def get_name(uid):
    if uid in names_cache:
        return names_cache[uid]
    try:
        u = vk.users.get(user_ids=uid)[0]
        name = f"{u['first_name']} {u['last_name']}"
        names_cache[uid] = name
        return name
    except Exception:
        return f"id{uid}"

def get_active_game(peer_id):
    cur.execute("SELECT id FROM games WHERE peer_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (peer_id,))
    row = cur.fetchone()
    return row[0] if row else None

# --- ВЫЖИМКА (из БД) ---
def get_gigachat_token():
    global GIGACHAT_TOKEN, TOKEN_EXPIRES_AT
    if GIGACHAT_TOKEN and time.time() * 1000 < TOKEN_EXPIRES_AT - 60000:
        return GIGACHAT_TOKEN
    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    payload = 'scope=GIGACHAT_API_PERS'
    headers = {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json', 'RqUID': str(uuid.uuid4()), 'Authorization': f'Basic {GIGACHAT_AUTH_KEY}'}
    try:
        response = requests.post(url, headers=headers, data=payload, verify=False)
        response.raise_for_status()
        data = response.json()
        GIGACHAT_TOKEN = data['access_token']
        TOKEN_EXPIRES_AT = data['expires_at']
        return GIGACHAT_TOKEN
    except Exception as e:
        print(f"Ошибка авторизации GigaChat: {e}")
        return None

def ask_ai_summary(chat_text):
    if not GIGACHAT_AUTH_KEY: return "❌ Ошибка: Не настроен ключ GIGACHAT_AUTH_KEY."
    if not chat_text: return "❌ В чате слишком мало сообщений для анализа."
    token = get_gigachat_token()
    if not token: return "❌ Не удалось авторизоваться в GigaChat."
    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    payload = json.dumps({"model": "GigaChat", "messages": [{"role": "system", "content": "Ты — полезный ассистент. Сделай краткую выжимку диалога. Выдели главные темы, решения и задачи. Пиши кратко и структурированно."}, {"role": "user", "content": f"Вот диалог:\n\n{chat_text}"}], "temperature": 0.7})
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    try:
        response = requests.post(url, headers=headers, data=payload, verify=False, timeout=60)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f"Ошибка GigaChat API: {e}")
        return "🤖 Нейросеть не смогла обработать запрос. Попробуйте позже."

# --- КОМАНДЫ САНТЫ (без изменений) ---
def cmd_start(peer_id):
    send(peer_id, "Привет! Я умею две вещи:\n🎅 Тайный Санта (Создать, Участвую, Хочу, Список, Жеребьевка)\n📊 Выжимка чата (напиши «че тут?»)")

def cmd_create(peer_id, user_id):
    active = get_active_game(peer_id)
    if active: return send(peer_id, f"⚠️ В чате уже есть игра #{active}.")
    cur.execute("INSERT INTO games (creator_id, peer_id) VALUES (?, ?)", (user_id, peer_id))
    conn.commit()
    send(peer_id, "✅ Игра создана! Пишите «Участвую».")

def cmd_join(peer_id, user_id):
    gid = get_active_game(peer_id)
    if not gid: return send(peer_id, "🤷‍♂️ Сначала напишите «Создать».")
    cur.execute("SELECT 1 FROM participants WHERE game_id=? AND user_id=?", (gid, user_id))
    if cur.fetchone(): return send(peer_id, "Ты уже в игре! 🎉")
    cur.execute("INSERT INTO participants (game_id, user_id) VALUES (?, ?)", (gid, user_id))
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM participants WHERE game_id=?", (gid,))
    send(peer_id, f"✅ {get_name(user_id)}, ты в игре! Всего участников: {cur.fetchone()[0]}")

def cmd_list(peer_id):
    gid = get_active_game(peer_id)
    if not gid: return send(peer_id, "Нет активной игры.")
    cur.execute("SELECT user_id FROM participants WHERE game_id=?", (gid,))
    users = [get_name(r[0]) for r in cur.fetchall()]
    if not users: return send(peer_id, "Пока никто не записался.")
    send(peer_id, f"📋 Участники ({len(users)}):\n" + "\n".join([f"{i}. {n}" for i, n in enumerate(users, 1)]))

def cmd_wish(peer_id, user_id, text):
    cur.execute("INSERT INTO users (vk_id, wishlist) VALUES (?, ?) ON CONFLICT(vk_id) DO UPDATE SET wishlist=excluded.wishlist", (user_id, text))
    conn.commit()
    send(peer_id, "🎁 Пожелания сохранены!")

def cmd_draw(peer_id, user_id):
    gid = get_active_game(peer_id)
    if not gid: return send(peer_id, "Нет активной игры.")
    cur.execute("SELECT creator_id FROM games WHERE id=?", (gid,))
    if cur.fetchone()[0] != user_id: return send(peer_id, "❌ Только создатель игры может запустить жеребьевку.")
    cur.execute("SELECT user_id FROM participants WHERE game_id=?", (gid,))
    users = [r[0] for r in cur.fetchall()]
    if len(users) < 2: return send(peer_id, "❌ Нужно минимум 2 участника.")
    shuffled = users[:]
    for _ in range(200):
        random.shuffle(shuffled)
        if all(a != b for a, b in zip(users, shuffled)): break
    else: return send(peer_id, "❌ Ошибка жеребьевки.")
    for u, t in zip(users, shuffled):
        cur.execute("UPDATE participants SET target_id=? WHERE game_id=? AND user_id=?", (t, gid, u))
    cur.execute("UPDATE games SET status='closed' WHERE id=?", (gid,))
    conn.commit()
    success = 0
    for u, t in zip(users, shuffled):
        cur.execute("SELECT wishlist FROM users WHERE vk_id=?", (t,))
        w = cur.fetchone()
        wish = (w[0] if w and w[0] else "Не указаны")
        try:
            send(u, f"🎅 Твой подопечный: {get_name(t)}\nСсылка: vk.com/id{t}\n🎁 Пожелания: {wish}")
            success += 1
        except: pass
    send(peer_id, f"✅ Жеребьевка проведена! Результаты в ЛС ({success} из {len(users)}).")

# --- ОСНОВНОЙ ЦИКЛ ---
print("Бот запущен...")
for event in longpoll.listen():
    if event.type != VkBotEventType.MESSAGE_NEW: continue
    msg = event.object.message
    peer_id = msg['peer_id']
    user_id = msg['from_id']
    text = msg['text'].strip()
    
    # Очистка от упоминаний
    if peer_id >= 2000000000:
        text = text.replace(f"[club{GROUP_ID}|", "").replace("]", "").replace(f"@club{GROUP_ID}", "").strip()

    # --- СОХРАНЯЕМ СООБЩЕНИЕ В БАЗУ (если это беседа и не от бота) ---
    if peer_id >= 2000000000 and user_id > 0:
        user_name = get_name(user_id)
        cur.execute("INSERT INTO chat_messages (peer_id, user_id, user_name, text) VALUES (?, ?, ?, ?)",
                    (peer_id, user_id, user_name, text))
        conn.commit()

    low = text.lower()

    # --- ЛОГИКА ВЫЖИМКИ (теперь из БД) ---
    if any(w in low for w in ["че тут", "что тут", "выжимка", "сводка", "summary"]):
        if peer_id < 2000000000:
            send(peer_id, "Выжимку можно делать только в общем чате (беседе)!")
        else:
            send(peer_id, "⏳ Анализирую последние сообщения...")
            # Берем 100 последних сообщений из базы
            cur.execute("SELECT user_name, text FROM chat_messages WHERE peer_id = ? ORDER BY id DESC LIMIT 100", (peer_id,))
            rows = cur.fetchall()
            if not rows:
                send(peer_id, "❌ В этом чате пока нет сохраненных сообщений. Напишите что-нибудь!")
            else:
                # Разворачиваем, чтобы было в хронологическом порядке
                history_text = "\n".join([f"{name}: {txt}" for name, txt in reversed(rows)])
                summary = ask_ai_summary(history_text)
                send(peer_id, f"📊 **Выжимка чата:**\n\n{summary}")

    # --- ОСТАЛЬНЫЕ КОМАНДЫ ---
    elif any(w in low for w in ["привет", "помощь", "меню", "начать", "start"]):
        cmd_start(peer_id)
    elif any(w in low for w in ["создать", "новая игра"]):
        cmd_create(peer_id, user_id)
    elif any(w in low for w in ["участвую", "запиши", "я в деле", "я в игре"]):
        cmd_join(peer_id, user_id)
    elif any(w in low for w in ["список", "участники"]):
        cmd_list(peer_id)
    elif any(w in low for w in ["жеребьевка", "жеребьёвка", "распределяй"]):
        cmd_draw(peer_id, user_id)
    elif any(w in low for w in ["хочу в подарок", "желание", "вишлист"]):
        clean = text
        for p in ["хочу в подарок", "желание", "вишлист"]:
            if p in clean.lower(): clean = clean[clean.lower().find(p)+len(p):].strip()
        if clean: cmd_wish(peer_id, user_id, clean)
        else: send(peer_id, "Напиши, что хочешь. Пример: «Хочу в подарок наушники».")
