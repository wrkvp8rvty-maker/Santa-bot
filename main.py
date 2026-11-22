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
        connection = psycopg2.connect(DATABASE_URL, keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5)
        return connection, connection.cursor()
    except Exception as e:
        print(f"❌ Ошибка подключения к БД: {e}")
        return None, None

conn, cur = db_connect()

if conn:
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (vk_id INTEGER PRIMARY KEY, address TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS games (id SERIAL PRIMARY KEY, creator_id INTEGER, peer_id INTEGER, status TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS participants (game_id INTEGER, user_id INTEGER, target_id INTEGER, PRIMARY KEY (game_id, user_id));
    CREATE TABLE IF NOT EXISTS chat_messages (id SERIAL PRIMARY KEY, peer_id INTEGER, user_id INTEGER, user_name TEXT, text TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
    """)
    conn.commit()

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

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def send(peer_id, text, attachment=None):
    try:
        vk.messages.send(peer_id=peer_id, message=text, attachment=attachment, random_id=int(time.time() * 1000))
    except Exception as e:
        print(f"Ошибка отправки: {e}")

def get_latest_game(peer_id):
    cur.execute("SELECT id, creator_id, status FROM games WHERE peer_id = %s ORDER BY id DESC LIMIT 1", (peer_id,))
    row = cur.fetchone()
    return row if row else (None, None, None)

def send_target_info(user_id, target_id):
    cur.execute("SELECT address FROM users WHERE vk_id=%s", (target_id,))
    w = cur.fetchone()
    address = (w[0] if w and w[0] else "❌ Не указан адрес.")
    try:
        send(user_id, f"🎅 Твой подопечный: {get_name(target_id)}\nСсылка: vk.com/id{target_id}\n📍 Адрес: {address}\n📦 QR-код кидай мне в ЛС!")
    except Exception as e:
        print(f"Не удалось отправить сообщение {user_id}: {e}")

def extract_vk_id(text):
    m1 = re.search(r'\[id(\d+)\|', text)
    m2 = re.search(r'@id(\d+)', text)
    m3 = re.search(r'vk\.com/id(\d+)', text)
    m4 = re.search(r'vk\.com/([a-zA-Z0-9_.]+)', text)
    m5 = re.search(r'\b(\d{5,})\b', text)
    
    if m1: return int(m1.group(1))
    if m2: return int(m2.group(1))
    if m3: return int(m3.group(1))
    if m5: return int(m5.group(1))
    if m4:
        try:
            res = vk.users.get(user_ids=m4.group(1))
            if res: return res[0]['id']
        except: pass
    return None

# --- КОМАНДЫ ---
def cmd_start(peer_id):
    send(peer_id, "Привет! Я ELF. 🤖\n\n"
                  "🎅 ТАЙНЫЙ САНТА:\n"
                  "/организатор — создать игру\n"
                  "/вступить [адрес ПВЗ] — записаться\n"
                  "/участники — список участников\n"
                  "/жеребьевка — провести жеребьевку\n"
                  "/добор [ссылка] — вписать опоздавших\n"
                  "/отмена — отменить жеребьевку\n"
                  "/игра — статус игры\n"
                  "/удалить — сбросить игру\n"
                  "/правила — инструкция\n\n"
                  "📦 QR-КОД: отправь мне в ЛС фото QR-кода.\n\n"
                  "📊 СВОДКА: /сводка")

def cmd_rules(peer_id):
    send(peer_id, "📜 ПРАВИЛА:\n1️⃣ /вступить [адрес ПВЗ]\n2️⃣ Ждем /жеребьевка от орга\n3️⃣ Отправляем подарок\n4️⃣ QR-код кидаем боту в ЛС (не в чат!)")

def cmd_set_organizer(peer_id, user_id):
    gid, creator_id, status = get_latest_game(peer_id)
    if gid and status == 'open':
        if creator_id == user_id: return send(peer_id, "Ты уже организатор.")
        else: return send(peer_id, f"❌ Игра уже есть. Орг: {get_name(creator_id)}")
    
    cur.execute("INSERT INTO games (creator_id, peer_id) VALUES (%s, %s)", (user_id, peer_id))
    conn.commit()
    send(peer_id, f"👑 {get_name(user_id)} — организатор! Пишите /вступить [адрес]")

def cmd_join(peer_id, user_id, text):
    gid, creator_id, status = get_latest_game(peer_id)
    
    if not gid: 
        return send(peer_id, "🤷‍♂️ Нет игры. Ждите /организатор.")
    
    address = re.sub(r'^/вступить\s*', '', text, flags=re.IGNORECASE).strip(" :,-")
    if not address: 
        return send(peer_id, "⚠️ Укажи адрес: /вступить: г. Москва, Озон Ленина 1")
    
    cur.execute("INSERT INTO users (vk_id, address) VALUES (%s, %s) ON CONFLICT (vk_id) DO UPDATE SET address = EXCLUDED.address", (user_id, address))
    
    cur.execute("SELECT target_id FROM participants WHERE game_id=%s AND user_id=%s", (gid, user_id))
    row = cur.fetchone()
    
    if row:
        conn.commit()
        if row[0] is None and status == 'closed':
            return send(peer_id, "Ты уже записан как опоздавший! Ждем, пока организатор напишет /добор.")
        return send(peer_id, "Ты уже в игре! Адрес обновлен.")
    
    cur.execute("INSERT INTO participants (game_id, user_id) VALUES (%s, %s)", (gid, user_id))
    conn.commit()
    
    if status == 'closed':
        send(peer_id, f"✅ {get_name(user_id)}, ты добавлен в список опоздавших!\nОрганизатору нужно написать /добор, чтобы вписать тебя.")
    else:
        cur.execute("SELECT COUNT(*) FROM participants WHERE game_id=%s", (gid,))
        send(peer_id, f"✅ {get_name(user_id)} в игре! Всего: {cur.fetchone()[0]}")

def cmd_show_participants(peer_id):
    gid, creator_id, status = get_latest_game(peer_id)
    if not gid: return send(peer_id, "В чате нет игр.")
    
    cur.execute("SELECT user_id FROM participants WHERE game_id=%s ORDER BY user_id", (gid,))
    users = [r[0] for r in cur.fetchall()]
    
    if not users:
        return send(peer_id, "В игре пока нет участников. Напишите /вступить [адрес].")
    
    msg = f"📋 Участники игры #{gid} ({len(users)} чел.):\n"
    for i, u in enumerate(users, 1):
        is_org = " 👑 (Орг)" if u == creator_id else ""
        msg += f"{i}. {get_name(u)}{is_org}\n"
        
    send(peer_id, msg)

def cmd_add_latecomers(peer_id, user_id, text):
    gid, creator_id, status = get_latest_game(peer_id)
    if not gid: return send(peer_id, "Нет игры.")
    if creator_id != user_id: return send(peer_id, "❌ Только организатор может делать добор.")
    if status == 'open': return send(peer_id, "Игра еще открыта. Просто напишите /жеребьевка.")

    cur.execute("SELECT user_id FROM participants WHERE game_id=%s AND target_id IS NULL", (gid,))
    latecomers = [r[0] for r in cur.fetchall()]
    
    if not latecomers:
        return send(peer_id, "Никто не опоздал. Все уже распределены!")

    lucky_id = extract_vk_id(text)
    
    if lucky_id:
        cur.execute("SELECT target_id FROM participants WHERE game_id=%s AND user_id=%s", (gid, lucky_id))
        row = cur.fetchone()
        
        if not row:
            return send(peer_id, "❌ Этот человек не участвует в игре.")
        if row[0] is None:
            return send(peer_id, "❌ У этого человека еще нет подопечного (он сам опоздавший).")
        if lucky_id in latecomers:
            return send(peer_id, "❌ Нельзя выбрать опоздавшего счастливчиком.")
            
        old_target = row[0]
        late_user = latecomers[0]
        
        cur.execute("UPDATE participants SET target_id=%s WHERE game_id=%s AND user_id=%s", (late_user, gid, lucky_id))
        cur.execute("UPDATE participants SET target_id=%s WHERE game_id=%s AND user_id=%s", (old_target, gid, late_user))
        conn.commit()
        
        send_target_info(lucky_id, late_user)
        send_target_info(late_user, old_target)
        
        send(peer_id, f"✅ Добор завершен вручную!\n👤 {get_name(lucky_id)} теперь дарит опоздавшему {get_name(late_user)}.\n🎁 А {get_name(late_user)} дарит тому, кому дарил {get_name(lucky_id)}.")
        
    else:
        cur.execute("SELECT user_id, target_id FROM participants WHERE game_id=%s AND target_id IS NOT NULL", (gid,))
        existing_pairs = cur.fetchall()
        
        if len(existing_pairs) < len(latecomers):
            return send(peer_id, "❌ Опоздавших больше, чем старых участников. Придется делать полную /отмена и /жеребьевка.")

        random.shuffle(existing_pairs)
        
        for i, late_user in enumerate(latecomers):
            old_user, old_target = existing_pairs[i]
            
            cur.execute("UPDATE participants SET target_id=%s WHERE game_id=%s AND user_id=%s", (late_user, gid, old_user))
            cur.execute("UPDATE participants SET target_id=%s WHERE game_id=%s AND user_id=%s", (old_target, gid, late_user))
            conn.commit()
            
            send_target_info(old_user, late_user)
            send_target_info(late_user, old_target)
        
        send(peer_id, f"✅ Случайный добор завершен! Вписали {len(latecomers)} опоздавших.")

def cmd_cancel_draw(peer_id, user_id):
    gid, creator_id, status = get_latest_game(peer_id)
    if not gid: return send(peer_id, "🤷‍♂️ В этом чате нет игр.")
    if creator_id != user_id: return send(peer_id, "❌ Только организатор может отменить жеребьевку.")
    if status == 'open': return send(peer_id, "Игра и так открыта.")
    
    cur.execute("UPDATE games SET status='open' WHERE id=%s", (gid,))
    cur.execute("UPDATE participants SET target_id=NULL WHERE game_id=%s", (gid,))
    conn.commit()
    send(peer_id, "🔄 Жеребьевка полностью отменена! Игра открыта заново.")

def cmd_draw(peer_id, user_id):
    gid, creator_id, status = get_latest_game(peer_id)
    if not gid: return send(peer_id, "Нет игры.")
    if status == 'closed': return send(peer_id, "Жеребьевка уже проведена. Используйте /добор или /отмена.")
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
        try:
            send_target_info(u, t)
            success += 1
        except: pass
    send(peer_id, f"✅ Жеребьевка проведена! Отправлено ({success} из {len(users)}).")

def cmd_show_game(peer_id):
    gid, creator_id, status = get_latest_game(peer_id)
    if not gid: return send(peer_id, "В чате нет игр.")
    
    cur.execute("SELECT COUNT(*) FROM participants WHERE game_id=%s", (gid,))
    count = cur.fetchone()[0]
    
    status_text = "Открыта (ждем участников)" if status == 'open' else "Закрыта (жеребьевка проведена)"
    send(peer_id, f"🎮 Игра #{gid}\n👑 Орг: {get_name(creator_id)}\n👥 Участников: {count}\n📌 Статус: {status_text}")

def cmd_delete_game(peer_id, user_id):
    gid, creator_id, status = get_latest_game(peer_id)
    if not gid: return send(peer_id, "Нет игр для удаления.")
    if creator_id != user_id: return send(peer_id, "❌ Только орг может удалить.")
    
    cur.execute("DELETE FROM participants WHERE game_id=%s", (gid,))
    cur.execute("DELETE FROM games WHERE id=%s", (gid,))
    conn.commit()
    send(peer_id, "🗑 Игра полностью удалена!")

# --- ОСНОВНОЙ ЦИКЛ ---
print("Бот запущен (Neon Postgres)...")
while True:
    try:
        if not conn or conn.closed:
            print("🔄 Переподключение к базе данных...")
            conn, cur = db_connect()
            if not conn: time.sleep(5); continue

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

            # Обработка фото (QR-коды)
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
                elif clean_cmd in ["/отмена", "/otmena"]: cmd_cancel_draw(peer_id, user_id)
                elif clean_cmd in ["/добор", "/dobor"]: cmd_add_latecomers(peer_id, user_id, text)
                elif clean_cmd in ["/участники", "/spisok", "/список"]: cmd_show_participants(peer_id)
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
