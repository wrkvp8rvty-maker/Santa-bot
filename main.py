import os
import random
import vk_api
import requests
import json
import uuid
import time
import urllib3
import re
import psycopg2
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TOKEN = os.environ.get("VK_TOKEN")
GROUP_ID = "242810690"
GIGACHAT_AUTH_KEY = os.environ.get("GIGACHAT_AUTH_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    print("⚠️ ВНИМАНИЕ: Не задана переменная DATABASE_URL!")
    exit(1)

# --- ФУНКЦИЯ ПОДКЛЮЧЕНИЯ К БАЗЕ ---
def db_connect():
    try:
        # Добавляем keepalives, чтобы соединение не засыпало
        connection = psycopg2.connect(
            DATABASE_URL, 
            keepalives=1, 
            keepalives_idle=30, 
            keepalives_interval=10, 
            keepalives_count=5
        )
        return connection, connection.cursor()
    except Exception as e:
        print(f"❌ Ошибка подключения к БД: {e}")
        return None, None

# Инициализация подключения
conn, cur = db_connect()

if conn:
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (vk_id INTEGER PRIMARY KEY, address TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS games (id SERIAL PRIMARY KEY, creator_id INTEGER, peer_id INTEGER, status TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS participants (game_id INTEGER, user_id INTEGER, target_id INTEGER, PRIMARY KEY (game_id, user_id));
    CREATE TABLE IF NOT EXISTS chat_messages (id SERIAL PRIMARY KEY, peer_id INTEGER, user_id INTEGER, user_name TEXT, text TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    """)
    conn.commit()

# --- VK И GIGACHAT ---
names_cache = {}
GIGACHAT_TOKEN = None
TOKEN_EXPIRES_AT = 0

def get_name(uid):
    if uid in names_cache: return names_cache[uid]
    try:
        u = vk.users.get(user_ids=uid)[0]
        name = f"{u['first_name']} {u['last_name']}"
        names_cache[uid] = name
        return name
    except Exception:
        return f"id{uid}"

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
        print(f"Ошибка GigaChat: {e}")
        return None

def ask_ai_summary(chat_text):
    if not GIGACHAT_AUTH_KEY: return "❌ Ошибка GigaChat."
    if not chat_text: return "❌ Мало сообщений."
    token = get_gigachat_token()
    if not token: return "❌ Нет токена GigaChat."
    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    system_prompt = ("Ты — ведущий новостей. Сделай краткий дайджест. Раздели на: 📰 ГЛАВНАЯ ТЕМА, ⚡ ЧТО ОБСУЖДАЛИ, 👥 КТО УЧАСТВОВАЛ. Используй ТОЛЬКО имена из диалога. Без Markdown.")
    payload = json.dumps({"model": "GigaChat", "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Лог:\n\n{chat_text}\n\nСводка:"}], "temperature": 0.4})
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Authorization': f'Bearer {token}'}
    try:
        response = requests.post(url, headers=headers, data=payload, verify=False, timeout=60)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content'].replace("**", "").replace("*", "")
    except Exception as e:
        print(f"Ошибка GigaChat API: {e}")
        return "🤖 Ошибка нейросети."

# --- КОМАНДЫ ---
def send(peer_id, text, attachment=None):
    try:
        vk.messages.send(peer_id=peer_id, message=text, attachment=attachment, random_id=int(time.time() * 1000))
    except Exception as e:
        print(f"Ошибка отправки: {e}")

def get_active_game(peer_id):
    cur.execute("SELECT id, creator_id FROM games WHERE peer_id = %s AND status = 'open' ORDER BY id DESC LIMIT 1", (peer_id,))
    row = cur.fetchone()
    return row if row else (None, None)

def cmd_start(peer_id):
    send(peer_id, "Привет! Я ELF. 🤖\n\n🎅 ТАЙНЫЙ САНТА:\n/organizator — создать игру\n/vstupit [адрес ПВЗ] — записаться\n/zherebevka — распределить подарки\n/igra — статус игры\n/udalit — сбросить игру\n/pravila — инструкция\n\n📦 QR-КОД: отправь мне в ЛС фото QR-кода.\n\n📊 СВОДКА: /svodka")

def cmd_rules(peer_id):
    send(peer_id, "📜 ПРАВИЛА:\n1️⃣ /вступить [адрес ПВЗ]\n2️⃣ Ждем /жеребьевка от орга\n3️⃣ Отправляем подарок\n4️⃣ QR-код кидаем боту в ЛС (не в чат!)")

def cmd_set_organizer(peer_id, user_id):
    gid, creator_id = get_active_game(peer_id)
    if gid:
        if creator_id == user_id: return send(peer_id, "Ты уже организатор.")
        else: return send(peer_id, f"❌ Игра уже есть. Орг: {get_name(creator_id)}")
    
    cur.execute("INSERT INTO games (creator_id, peer_id) VALUES (%s, %s)", (user_id, peer_id))
    conn.commit()
    send(peer_id, f"👑 {get_name(user_id)} — организатор! Пишите /вступить [адрес]")

def cmd_join(peer_id, user_id, text):
    gid, creator_id = get_active_game(peer_id)
    if not gid: return send(peer_id, "🤷‍♂️ Нет игры. Ждите /организатор.")
    
    address = re.sub(r'^/вступить\s*', '', text, flags=re.IGNORECASE).strip(" :,-")
    if not address: return send(peer_id, "⚠️ Укажи адрес: /вступить: г. Москва, Озон Ленина 1")
    
    cur.execute("INSERT INTO users (vk_id, address) VALUES (%s, %s) ON CONFLICT (vk_id) DO UPDATE SET address = EXCLUDED.address", (user_id, address))
    
    cur.execute("SELECT 1 FROM participants WHERE game_id=%s AND user_id=%s", (gid, user_id))
    if cur.fetchone(): 
        conn.commit()
        return send(peer_id, "Ты уже в игре! Адрес обновлен.")
    
    cur.execute("INSERT INTO participants (game_id, user_id) VALUES (%s, %s)", (gid, user_id))
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM participants WHERE game_id=%s", (gid,))
    send(peer_id, f"✅ {get_name(user_id)} в игре! Всего: {cur.fetchone()[0]}")

def cmd_draw(peer_id, user_id):
    gid, creator_id = get_active_game(peer_id)
    if not gid: return send(peer_id, "Нет игры.")
    if creator_id != user_id: return send(peer_id, "❌ Только орг может запустить жеребьевку.")
    
    cur.execute("SELECT user_id FROM participants WHERE game_id=%s", (gid,))
    users = [r[0] for r in cur.fetchall()]
    if len(users) < 2: return send(peer_id, "❌ Нужно 2+ участника.")

    shuffled = users[:]
    for _ in range(200):
        random.shuffle(shuffled)
        if all(a != b for a, b in zip(users, shuffled)): break
    else: return send(peer_id, "❌ Ошибка жеребьевки.")

    for u, t in zip(users, shuffled):
        cur.execute("UPDATE participants SET target_id=%s WHERE game_id=%s AND user_id=%s", (t, gid, u))
    cur.execute("UPDATE games SET status='closed' WHERE id=%s", (gid,))
    conn.commit()

    success = 0
    for u, t in zip(users, shuffled):
        cur.execute("SELECT address FROM users WHERE vk_id=%s", (t,))
        w = cur.fetchone()
        address = (w[0] if w and w[0] else "❌ Не указан адрес.")
        try:
            send(u, f"🎅 Твой подопечный: {get_name(t)}\nСсылка: vk.com/id{t}\n📍 Адрес: {address}\n📦 QR-код кидай мне в ЛС!")
            success += 1
        except: pass
    send(peer_id, f"✅ Жеребьевка проведена! Отправлено ({success} из {len(users)}).")

def cmd_show_game(peer_id):
    gid, creator_id = get_active_game(peer_id)
    if not gid: return send(peer_id, "В чате нет игры.")
    cur.execute("SELECT COUNT(*) FROM participants WHERE game_id=%s", (gid,))
    send(peer_id, f"🎮 Игра активна.\n👑 Орг: {get_name(creator_id)}\n👥 Участников: {cur.fetchone()[0]}")

def cmd_delete_game(peer_id, user_id):
    gid, creator_id = get_active_game(peer_id)
    if not gid: return send(peer_id, "Нет игры.")
    if creator_id != user_id: return send(peer_id, "❌ Только орг может удалить.")
    cur.execute("DELETE FROM participants WHERE game_id=%s", (gid,))
    cur.execute("DELETE FROM games WHERE id=%s", (gid,))
    conn.commit()
    send(peer_id, "🗑 Игра удалена!")

# --- ОСНОВНОЙ ЦИКЛ С ЗАЩИТОЙ ОТ РАЗРЫВА БД ---
print("Бот запущен (Neon Postgres)...")
while True:
    try:
        # Если соединение с БД потеряно или не установлено, подключаемся заново
        if not conn or conn.closed:
            print("🔄 Переподключение к базе данных...")
            conn, cur = db_connect()
            if not conn:
                time.sleep(5)
                continue

        # Инициализируем VK (делаем это внутри цикла, чтобы при сбое пересоздать)
        vk_session = vk_api.VkApi(token=TOKEN)
        vk = vk_session.get_api()
        longpoll = VkBotLongPoll(vk_session, GROUP_ID)

        for event in longpoll.listen():
            if event.type != VkBotEventType.MESSAGE_NEW: continue
            
            msg = event.object.message
            peer_id = msg['peer_id']
            user_id = msg['from_id']
            text = msg['text'].strip()
            
            if peer_id >= 2000000000:
                text = text.replace(f"[club{GROUP_ID}|", "").replace("]", "").replace(f"@club{GROUP_ID}", "").strip()

            if peer_id >= 2000000000 and user_id > 0:
                user_name = get_name(user_id)
                cur.execute("INSERT INTO chat_messages (peer_id, user_id, user_name, text) VALUES (%s, %s, %s, %s)", (peer_id, user_id, user_name, text))
                conn.commit()

            low = text.lower()

            if 'attachments' in msg and peer_id < 2000000000:
                for attach in msg['attachments']:
                    if attach['type'] == 'photo':
                        cur.execute("SELECT target_id FROM participants WHERE user_id = %s AND target_id IS NOT NULL ORDER BY game_id DESC LIMIT 1", (user_id,))
                        row = cur.fetchone()
                        if row:
                            target_id = row[0]
                            photo = attach['photo']
                            attach_str = f"photo{photo['owner_id']}_{photo['id']}"
                            if 'access_key' in photo: attach_str += f"_{photo['access_key']}"
                            try:
                                send(target_id, "🎁 Твой Тайный Санта отправил QR-код:", attachment=attach_str)
                                send(peer_id, f"✅ QR отправлен ({get_name(target_id)})")
                            except: send(peer_id, "❌ У него закрыты ЛС.")
                        else: send(peer_id, "❌ Ты не участвовал в жеребьевке.")
                        break

            if low.startswith("/"):
                clean_cmd = re.sub(r'[^\w\s/]', '', low).split()[0]

                if clean_cmd in ["/start", "/помощь", "/меню"]: cmd_start(peer_id)
                elif clean_cmd in ["/сводка", "/svodka"]:
                    if peer_id < 2000000000: send(peer_id, "Только в чате!")
                    else:
                        send(peer_id, "🎙 Готовлю сводку...")
                        cur.execute("SELECT user_name, text FROM chat_messages WHERE peer_id = %s ORDER BY id DESC LIMIT 100", (peer_id,))
                        rows = cur.fetchall()
                        if not rows: send(peer_id, "❌ Пусто.")
                        else:
                            history = "\n".join([f"{n}: {t}" for n, t in reversed(rows)])
                            send(peer_id, f"📺 ВЫПУСК:\n\n{ask_ai_summary(history)}")
                elif clean_cmd in ["/правила", "/pravila"]: cmd_rules(peer_id)
                elif clean_cmd in ["/организатор", "/organizator"]: cmd_set_organizer(peer_id, user_id)
                elif clean_cmd in ["/игра", "/igra"]: cmd_show_game(peer_id)
                elif clean_cmd in ["/удалить", "/udalit"]: cmd_delete_game(peer_id, user_id)
                elif clean_cmd in ["/жеребьевка", "/zherebevka"]: cmd_draw(peer_id, user_id)
                elif clean_cmd in ["/вступить", "/vstupit"]: cmd_join(peer_id, user_id, text)

    except psycopg2.OperationalError as e:
        print(f"⚠️ Соединение с БД потеряно: {e}. Переподключаюсь...")
        conn = None
        time.sleep(3)
    except Exception as e:
        print(f"⚠️ Ошибка в главном цикле: {e}. Перезапуск через 5 секунд...")
        time.sleep(5)
