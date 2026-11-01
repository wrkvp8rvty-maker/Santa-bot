import os
import sqlite3
import random
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

TOKEN = os.environ.get("VK_TOKEN")
GROUP_ID = "242810690"

# ---------- База данных ----------
conn = sqlite3.connect("santa.db")
cur = conn.cursor()
# Добавляем peer_id в таблицу games, чтобы игра привязывалась к конкретному чату
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (
    vk_id INTEGER PRIMARY KEY,
    wishlist TEXT DEFAULT ''
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
""")
conn.commit()

# ---------- VK ----------
if not TOKEN:
    print("ОШИБКА: Не найден токен VK_TOKEN!")
    exit(1)

vk_session = vk_api.VkApi(token=TOKEN)
vk = vk_session.get_api()
longpoll = VkBotLongPoll(vk_session, GROUP_ID)

def send(peer_id, text):
    vk.messages.send(peer_id=peer_id, message=text,
                     random_id=random.randint(1, 2**31 - 1))

def get_name(uid):
    try:
        u = vk.users.get(user_ids=uid)[0]
        return f"{u['first_name']} {u['last_name']}"
    except Exception:
        return f"id{uid}"

def get_active_game(peer_id):
    """Ищет активную игру в текущем чате"""
    cur.execute("SELECT id FROM games WHERE peer_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1", (peer_id,))
    row = cur.fetchone()
    return row[0] if row else None

# ---------- Команды ----------
def cmd_start(peer_id):
    send(peer_id, "Привет! Я бот «Тайный Санта» 🎅\n\n"
              "Просто пиши мне обычными словами:\n"
              "👉 «Создать» — чтобы начать игру\n"
              "👉 «Участвую» — чтобы записаться\n"
              "👉 «Хочу [текст]» — чтобы указать пожелания\n"
              "👉 «Список» — чтобы увидеть участников\n"
              "👉 «Жеребьевка» — чтобы распределить подарки (только для создателя)\n\n"
              "Все результаты придут в личные сообщения!")

def cmd_create(peer_id, user_id):
    active = get_active_game(peer_id)
    if active:
        return send(peer_id, f"⚠️ В этом чате уже есть активная игра #{active}. Если хотите начать новую, сначала проведите жеребьевку в текущей.")
    
    cur.execute("INSERT INTO games (creator_id, peer_id) VALUES (?, ?)", (user_id, peer_id))
    gid = cur.lastrowid
    conn.commit()
    send(peer_id, f"✅ Игра создана!\nТеперь все желающие могут написать «Участвую».")

def cmd_join(peer_id, user_id):
    gid = get_active_game(peer_id)
    if not gid:
        return send(peer_id, "🤷‍♂️ В этом чате пока нет активной игры. Напишите «Создать», чтобы начать!")
    
    cur.execute("SELECT 1 FROM participants WHERE game_id=? AND user_id=?", (gid, user_id))
    if cur.fetchone():
        return send(peer_id, f"Ты уже участвуешь в игре! 🎉")

    cur.execute("INSERT INTO participants (game_id, user_id) VALUES (?, ?)", (gid, user_id))
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM participants WHERE game_id=?", (gid,))
    cnt = cur.fetchone()[0]
    send(peer_id, f"✅ {get_name(user_id)}, ты в игре! Всего участников: {cnt}")

def cmd_list(peer_id):
    gid = get_active_game(peer_id)
    if not gid:
        return send(peer_id, "Сейчас нет активной игры.")
    
    cur.execute("SELECT user_id FROM participants WHERE game_id=?", (gid,))
    users = [get_name(r[0]) for r in cur.fetchall()]
    if not users:
        return send(peer_id, "В игре пока нет участников. Напишите «Участвую»!")
    
    msg = f"📋 Участники игры #{gid} ({len(users)} чел.):\n"
    for i, name in enumerate(users, 1):
        msg += f"{i}. {name}\n"
    send(peer_id, msg)

def cmd_wish(peer_id, user_id, text):
    cur.execute(
        "INSERT INTO users (vk_id, wishlist) VALUES (?, ?) "
        "ON CONFLICT(vk_id) DO UPDATE SET wishlist=excluded.wishlist",
        (user_id, text)
    )
    conn.commit()
    send(peer_id, "🎁 Твои пожелания сохранены! Твой Тайный Санта их увидит.")

def cmd_draw(peer_id, user_id):
    gid = get_active_game(peer_id)
    if not gid:
        return send(peer_id, "Нет активной игры для жеребьевки.")
    
    cur.execute("SELECT creator_id FROM games WHERE id=?", (gid,))
    creator_id = cur.fetchone()[0]
    
    if creator_id != user_id:
        return send(peer_id, "❌ Только тот, кто создал игру, может запустить жеребьевку.")

    cur.execute("SELECT user_id FROM participants WHERE game_id=?", (gid,))
    users = [r[0] for r in cur.fetchall()]
    
    if len(users) < 2:
        return send(peer_id, "❌ Нужно минимум 2 участника, чтобы провести жеребьевку.")

    shuffled = users[:]
    for _ in range(200):
        random.shuffle(shuffled)
        if all(a != b for a, b in zip(users, shuffled)):
            break
    else:
        return send(peer_id, "❌ Не удалось провести жеребьевку, попробуйте еще раз.")

    for u, t in zip(users, shuffled):
        cur.execute("UPDATE participants SET target_id=? WHERE game_id=? AND user_id=?",
                    (t, gid, u))
    cur.execute("UPDATE games SET status='closed' WHERE id=?", (gid,))
    conn.commit()

    # Рассылка в ЛС
    success_count = 0
    for u, t in zip(users, shuffled):
        cur.execute("SELECT wishlist FROM users WHERE vk_id=?", (t,))
        w = cur.fetchone()
        wish = (w[0] if w and w[0] else "Пожелания не указаны 🤷‍♂️")
        
        # Отправляем в ЛС (peer_id = u)
        try:
            send(u, f"🎅 Твой подопечный: {get_name(t)}\nСсылка: vk.com/id{t}\n\n🎁 Пожелания: {wish}")
            success_count += 1
        except Exception as e:
            print(f"Не удалось отправить сообщение пользователю {u}: {e}")

    send(peer_id, f"✅ Жеребьевка проведена! Результаты отправлены в личные сообщения ({success_count} из {len(users)}). Если кто-то не получил, возможно, у него закрыты ЛС.")

# ---------- Основной цикл ----------
print("Бот запущен...")
for event in longpoll.listen():
    if event.type != VkBotEventType.MESSAGE_NEW:
        continue
    
    msg = event.object.message
    peer_id = msg['peer_id']
    user_id = msg['from_id']
    text = msg['text'].strip()
    
    # Очистка от упоминания бота (если пишут через @)
    if peer_id >= 2000000000:
        text = text.replace(f"[club{GROUP_ID}|", "").replace("]", "").strip()
        # Убираем @club123, если написали просто текстом
        text = text.replace(f"@club{GROUP_ID}", "").strip()

    low = text.lower()

    # Умный парсинг команд
    if any(word in low for word in ["привет", "помощь", "меню", "начать", "start"]):
        cmd_start(peer_id)
    elif any(word in low for word in ["создать", "новая игра", "старт игру"]):
        cmd_create(peer_id, user_id)
    elif any(word in low for word in ["участвую", "запиши", "я в деле", "я в игре", "добавь"]):
        cmd_join(peer_id, user_id)
    elif any(word in low for word in ["список", "участники", "кто в игре"]):
        cmd_list(peer_id)
    elif any(word in low for word in ["жеребьевка", "жеребьёвка", "распределяй", "поехали"]):
        cmd_draw(peer_id, user_id)
    # Обработка пожеланий (ищем фразы "хочу в подарок", "желание" и т.д.)
    elif any(word in low for word in ["хочу в подарок", "желание", "вишлист", "подарите"]):
        # Отрезаем вступительные слова, оставляя только суть
        clean_text = text
        for phrase in ["хочу в подарок", "желание", "вишлист", "подарите", "я хочу"]:
            # Заменяем без учета регистра
            idx = clean_text.lower().find(phrase)
            if idx != -1:
                clean_text = clean_text[idx+len(phrase):].strip()
        
        if clean_text:
            cmd_wish(peer_id, user_id, clean_text)
        else:
            send(peer_id, "Напиши, что именно ты хочешь получить. Например: «Хочу в подарок наушники».")
    
    # Оставляем поддержку слэш-команд на случай, если кто-то привык
    elif low.startswith("/join"):
        cmd_join(peer_id, user_id)
    elif low.startswith("/wish"):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            cmd_wish(peer_id, user_id, parts[1])
