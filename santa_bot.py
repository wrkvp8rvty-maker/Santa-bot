import os
import sqlite3
import random
import vk_api
import requests
import json
import uuid
import time
import urllib3
import re
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
CREATE TABLE IF NOT EXISTS users (
    vk_id INTEGER PRIMARY KEY, 
    address TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT, 
    creator_id INTEGER, 
    peer_id INTEGER, 
    status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS participants (
    game_id INTEGER, 
    user_id INTEGER, 
    target_id INTEGER, 
    PRIMARY KEY (game_id, user_id)
);
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

def send(peer_id, text, attachment=None):
    try:
        vk.messages.send(
            peer_id=peer_id, 
            message=text, 
            attachment=attachment,
            random_id=int(time.time() * 1000)
        )
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
    cur.execute("SELECT id, creator_id FROM games WHERE peer_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (peer_id,))
    row = cur.fetchone()
    return row if row else (None, None)

# --- ФУНКЦИИ ВЫЖИМКИ GIGACHAT ---
def get_gigachat_token():
    global GIGACHAT_TOKEN, TOKEN_EXPIRES_AT
    if GIGACHAT_TOKEN and time.time() * 1000 < TOKEN_EXPIRES_AT - 60000:
        return GIGACHAT_TOKEN

    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    payload = 'scope=GIGACHAT_API_PERS'
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept': 'application/json',
        'RqUID': str(uuid.uuid4()),
        'Authorization': f'Basic {GIGACHAT_AUTH_KEY}'
    }
    
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
    
    system_prompt = (
        "Ты — профессиональный ведущий новостей. Твоя задача — сделать краткий, но информативный дайджест (сводку) общения в чате. "
        "Раздели свой ответ на логические блоки:\n"
        "📰 ГЛАВНАЯ ТЕМА: О чем вообще идет речь.\n"
        "⚡ ЧТО ОБСУЖДАЛИ: Конкретные факты, планы или вопросы.\n"
        "👥 КТО УЧАСТВОВАЛ: Кто именно что предлагал или спрашивал.\n\n"
        "ВАЖНЫЕ ПРАВИЛА:\n"
        "1. Используй ТОЛЬКО имена из диалога. Не путай их.\n"
        "2. Пиши четко, по фактам.\n"
        "3. ЗАПРЕЩЕНО использовать символы Markdown (звездочки, решетки)."
    )
    
    payload = json.dumps({
        "model": "GigaChat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Вот лог сообщений из чата:\n\n{chat_text}\n\nСделай новостную сводку."}
        ],
        "temperature": 0.4
    })
    
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Authorization': f'Bearer {token}'}

    try:
        response = requests.post(url, headers=headers, data=payload, verify=False, timeout=60)
        response.raise_for_status()
        summary = response.json()['choices'][0]['message']['content']
        return summary.replace("**", "").replace("*", "")
    except Exception as e:
        print(f"Ошибка GigaChat API: {e}")
        return "🤖 Нейросеть не смогла обработать запрос."

# --- КОМАНДЫ ---
def cmd_start(peer_id):
    send(peer_id, "Привет! Я ELF. 🤖 Вот что я умею:\n\n"
                  "🎅 ТАЙНЫЙ САНТА:\n"
                  "/организатор — создать игру\n"
                  "/вступить [адрес ПВЗ] — записаться в игру\n"
                  "/жеребьевка — распределить подарки\n"
                  "/игра — статус активной игры\n"
                  "/удалить — сбросить игру\n"
                  "/правила — инструкция для участников\n\n"
                  "📦 ОТПРАВКА QR-КОДА:\n"
                  "Просто отправь мне в ЛС фото QR-кода, и я перешлю его твоему подопечному!\n\n"
                  "📊 НОВОСТНАЯ СВОДКА:\n"
                  "/сводка — сделаю выпуск новостей по чату.")

def cmd_rules(peer_id):
    rules_text = (
        "📜 ПРАВИЛА ИГРЫ «ТАЙНЫЙ САНТА» 📜\n\n"
        "1️⃣ ВСТУПЛЕНИЕ В ИГРУ\n"
        "Напишите в чат: /вступить [ваш адрес ПВЗ]\n"
        "Пример: /вступить: г. Москва, ул. Ленина 1, ПВЗ Озон.\n"
        "Важно: без адреса бот не запишет вас в игру!\n\n"
        "2️⃣ ЖЕРЕБЬЕВКА\n"
        "Когда все соберутся, организатор пишет /жеребьевка.\n"
        "Каждому в личные сообщения (ЛС) придет имя, ссылка и адрес вашего подопечного.\n\n"
        "3️⃣ ОТПРАВКА ПОДАРКА\n"
        "Купите подарок и отправьте его через ПВЗ на адрес подопечного.\n"
        "Получив в приложении доставки QR-код для выдачи посылки...\n\n"
        "4️⃣ КУДА ОТПРАВЛЯТЬ QR-КОД?\n"
        "❗️ НЕ отправляйте QR-код в общий чат! Это испортит сюрприз.\n"
        "Отправьте фото (скриншот) QR-кода боту в ЛИЧНЫЕ СООБЩЕНИЯ.\n"
        "Бот автоматически перешлет его вашему подопечному, сохранив полную анонимность!"
    )
    send(peer_id, rules_text)

def cmd_set_organizer(peer_id, user_id):
    gid, creator_id = get_active_game(peer_id)
    if gid:
        if creator_id == user_id:
            return send(peer_id, "Ты уже являешься организатором этой игры.")
        else:
            return send(peer_id, f"❌ В этом чате уже есть активная игра. Организатор: {get_name(creator_id)}")
    
    cur.execute("INSERT INTO games (creator_id, peer_id) VALUES (?, ?)", (user_id, peer_id))
    conn.commit()
    send(peer_id, f"👑 {get_name(user_id)} назначен организатором!\n"
                  f"Теперь участники могут вступать в игру командой:\n"
                  f"/вступить [ваш адрес ПВЗ]")

def cmd_join(peer_id, user_id, text):
    gid, creator_id = get_active_game(peer_id)
    
    if not gid:
        return send(peer_id, "🤷‍♂️ В чате нет активной игры. Дождитесь, пока организатор напишет /организатор.")

    # Отрезаем саму команду /вступить, чтобы получить чистый адрес
    address = re.sub(r'^/вступить\s*', '', text, flags=re.IGNORECASE).strip(" :,-")
            
    if not address:
        return send(peer_id, "⚠️ Чтобы вступить в игру, обязательно укажи адрес ПВЗ.\nПример: «/вступить: г. Москва, ул. Ленина 1, Озон»")

    cur.execute("INSERT INTO users (vk_id, address) VALUES (?, ?) ON CONFLICT(vk_id) DO UPDATE SET address=excluded.address", (user_id, address))
    
    cur.execute("SELECT 1 FROM participants WHERE game_id=? AND user_id=?", (gid, user_id))
    if cur.fetchone(): 
        conn.commit()
        return send(peer_id, "Ты уже в игре! 🎉 (Адрес ПВЗ обновлен)")
    
    cur.execute("INSERT INTO participants (game_id, user_id) VALUES (?, ?)", (gid, user_id))
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM participants WHERE game_id=?", (gid,))
    send(peer_id, f"✅ {get_name(user_id)}, ты в игре! Всего участников: {cur.fetchone()[0]}.\n"
                  f"📍 Адрес ПВЗ сохранен.")

def cmd_draw(peer_id, user_id):
    gid, creator_id = get_active_game(peer_id)
    if not gid: return send(peer_id, "Нет активной игры.")
    
    if creator_id != user_id: return send(peer_id, "❌ Только организатор игры может запустить жеребьевку.")
    
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
        cur.execute("SELECT address FROM users WHERE vk_id=?", (t,))
        w = cur.fetchone()
        address = (w[0] if w and w[0] else "❌ Участник не указал адрес! Свяжитесь с ним.")
        
        try:
            send(u, f"🎅 Твой подопечный: {get_name(t)}\nСсылка: vk.com/id{t}\n\n"
                    f"📍 Адрес ПВЗ для отправки:\n{address}\n\n"
                    f"📦 Когда отправишь подарок, отправь мне в ЛС фото QR-кода, и я перешлю его получателю!")
            success += 1
        except: pass
        
    send(peer_id, f"✅ Жеребьевка проведена! Результаты отправлены в ЛС ({success} из {len(users)}).")

def cmd_show_game(peer_id):
    gid, creator_id = get_active_game(peer_id)
    if not gid:
        return send(peer_id, "В чате нет активной игры.")
    
    cur.execute("SELECT COUNT(*) FROM participants WHERE game_id=?", (gid,))
    count = cur.fetchone()[0]
    
    send(peer_id, f"🎮 Активная игра\n"
                  f"👑 Организатор: {get_name(creator_id)}\n"
                  f"👥 Участников: {count}\n\n"
                  f"Чтобы вступить, напишите /вступить [адрес ПВЗ].")

def cmd_delete_game(peer_id, user_id):
    gid, creator_id = get_active_game(peer_id)
    if not gid:
        return send(peer_id, "Нет активной игры для удаления.")
    
    if creator_id != user_id:
        return send(peer_id, "❌ Только организатор может удалить игру.")
        
    cur.execute("DELETE FROM participants WHERE game_id=?", (gid,))
    cur.execute("DELETE FROM games WHERE id=?", (gid,))
    conn.commit()
    send(peer_id, "🗑 Игра удалена! Чтобы начать новую, напишите /организатор.")

# --- ОСНОВНОЙ ЦИКЛ ---
print("Бот запущен...")
for event in longpoll.listen():
    if event.type != VkBotEventType.MESSAGE_NEW: continue
    
    msg = event.object.message
    peer_id = msg['peer_id']
    user_id = msg['from_id']
    text = msg['text'].strip()
    
    if peer_id >= 2000000000:
        text = text.replace(f"[club{GROUP_ID}|", "").replace("]", "").replace(f"@club{GROUP_ID}", "").strip()

    # --- СОХРАНЯЕМ СООБЩЕНИЕ В БАЗУ ---
    if peer_id >= 2000000000 and user_id > 0:
        user_name = get_name(user_id)
        cur.execute("INSERT INTO chat_messages (peer_id, user_id, user_name, text) VALUES (?, ?, ?, ?)",
                    (peer_id, user_id, user_name, text))
        conn.commit()

    low = text.lower()

    # ==========================================
    # 1. ОБРАБОТКА ФОТО (QR-КОДЫ В ЛИЧКУ)
    # ==========================================
    if 'attachments' in msg and peer_id < 2000000000:
        for attach in msg['attachments']:
            if attach['type'] == 'photo':
                cur.execute("""
                    SELECT target_id FROM participants 
                    WHERE user_id = ? AND target_id IS NOT NULL 
                    ORDER BY game_id DESC LIMIT 1
                """, (user_id,))
                row = cur.fetchone()
                
                if row:
                    target_id = row[0]
                    photo = attach['photo']
                    attach_str = f"photo{photo['owner_id']}_{photo['id']}"
                    if 'access_key' in photo:
                        attach_str += f"_{photo['access_key']}"
                    
                    try:
                        send(target_id, "🎁 Твой Тайный Санта отправил тебе подарок!\n"
                                        "📦 Вот QR-код для получения посылки:", attachment=attach_str)
                        send(peer_id, f"✅ Успешно! QR-код отправлен твоему подопечному ({get_name(target_id)}).")
                    except Exception as e:
                        send(peer_id, "❌ Не удалось отправить сообщение подопечному. Возможно, у него закрыты личные сообщения.")
                else:
                    send(peer_id, "❌ Я не нашел, кому ты даришь подарок. Кажется, ты не участвовал в жеребьевке.")
                break

    # ==========================================
    # 2. ТЕКСТОВЫЕ КОМАНДЫ (ТОЛЬКО С СЛЭШЕМ)
    # ==========================================
    if low.startswith("/"):
        # Очищаем от знаков препинания, чтобы /сводка? тоже работала
        clean_cmd = re.sub(r'[^\w\s/]', '', low).split()[0]

        if clean_cmd in ["/start", "/помощь", "/меню"]:
            cmd_start(peer_id)
            
        elif clean_cmd == "/сводка":
            if peer_id < 2000000000:
                send(peer_id, "Сводку можно делать только в общем чате!")
            else:
                send(peer_id, "🎙 Готовлю выпуск новостей...")
                cur.execute("SELECT user_name, text FROM chat_messages WHERE peer_id = ? ORDER BY id DESC LIMIT 100", (peer_id,))
                rows = cur.fetchall()
                if not rows:
                    send(peer_id, "❌ Тут пока пусто!")
                else:
                    history_text = "\n".join([f"{name}: {txt}" for name, txt in reversed(rows)])
                    summary = ask_ai_summary(history_text)
                    send(peer_id, f"📺 ВЕЧЕРНИЙ ВЫПУСК:\n\n{summary}")
                    
        elif clean_cmd == "/правила":
            cmd_rules(peer_id)
            
        elif clean_cmd == "/организатор":
            cmd_set_organizer(peer_id, user_id)
            
        elif clean_cmd == "/игра":
            cmd_show_game(peer_id)
            
        elif clean_cmd == "/удалить":
            cmd_delete_game(peer_id, user_id)
            
        elif clean_cmd == "/жеребьевка":
            cmd_draw(peer_id, user_id)
            
        elif clean_cmd == "/вступить":
            cmd_join(peer_id, user_id, text)
