import os
import sqlite3
import random
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

TOKEN = os.environ.get("VK_TOKEN")
GROUP_ID = "242810690" # ID твоего сообщества

# ---------- База данных ----------
conn = sqlite3.connect("santa.db")
cur = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (
    vk_id INTEGER PRIMARY KEY,
    wishlist TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id INTEGER,
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

# Отправка сообщения (теперь принимает peer_id, чтобы отвечать и в чат, и в личку)
def send(peer_id, text):
    vk.messages.send(peer_id=peer_id, message=text,
                     random_id=random.randint(1, 2**31 - 1))

def get_name(uid):
    try:
        u = vk.users.get(user_ids=uid)[0]
        return f"{u['first_name']} {u['last_name']}"
    except Exception:
        return f"id{uid}"

# ---------- Команды ----------
def cmd_start(peer_id):
    send(peer_id, "Привет! Я бот «Тайный Санта» 🎅\n\n"
              "Команды:\n"
              "/create — создать игру\n"
              "/join <id> — присоединиться к игре\n"
              "/wish <текст> — задать пожелания\n"
              "/my — мои игры\n"
              "/draw <id> — жеребьёвка (только организатор)\n\n"
              "В беседах пишите команды с упоминанием бота: @club242810690 /create")

def cmd_create(peer_id, user_id):
    cur.execute("INSERT INTO games (creator_id) VALUES (?)", (user_id,))
    gid = cur.lastrowid
    conn.commit()
    send(peer_id, f"✅ Игра #{gid} создана!\n"
              f"Пригласи друзей: /join {gid}\n"
              f"Когда все зайдут — запусти жеребьёвку: /draw {gid}")

def cmd_join(peer_id, user_id, gid):
    cur.execute("SELECT status FROM games WHERE id=?", (gid,))
    row = cur.fetchone()
    if not row:
        return send(peer_id, "❌ Игра не найдена.")
    if row[0] != 'open':
        return send(peer_id, "❌ Игра уже закрыта (жеребьёвка проведена).")
    cur.execute("INSERT OR IGNORE INTO participants (game_id, user_id) VALUES (?, ?)",
                (gid, user_id))
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM participants WHERE game_id=?", (gid,))
    cnt = cur.fetchone()[0]
    send(peer_id, f"✅ Ты в игре #{gid}! Всего участников: {cnt}")

def cmd_wish(peer_id, user_id, text):
    cur.execute(
        "INSERT INTO users (vk_id, wishlist) VALUES (?, ?) "
        "ON CONFLICT(vk_id) DO UPDATE SET wishlist=excluded.wishlist",
        (user_id, text)
    )
    conn.commit()
    send(peer_id, "🎁 Пожелания сохранены.")

def cmd_my(peer_id, user_id):
    cur.execute("""
        SELECT g.id, g.status, g.creator_id
        FROM games g JOIN participants p ON p.game_id = g.id
        WHERE p.user_id = ?
    """, (user_id,))
    rows = cur.fetchall()
    if not rows:
        return send(peer_id, "Ты пока не участвуешь ни в одной игре.")
    msg = "Твои игры:\n"
    for gid, status, creator in rows:
        role = "организатор" if creator == user_id else "участник"
        msg += f"#{gid} — {status}, {role}\n"
    send(peer_id, msg)

def cmd_draw(peer_id, user_id, gid):
    cur.execute("SELECT creator_id, status FROM games WHERE id=?", (gid,))
    row = cur.fetchone()
    if not row:
        return send(peer_id, "❌ Игра не найдена.")
    if row[0] != user_id:
        return send(peer_id, "❌ Только организатор может запустить жеребьёвку.")
    if row[1] != 'open':
        return send(peer_id, "❌ Жеребьёвка уже проведена.")

    cur.execute("SELECT user_id FROM participants WHERE game_id=?", (gid,))
    users = [r[0] for r in cur.fetchall()]
    if len(users) < 2:
        return send(peer_id, "❌ Нужно минимум 2 участника.")

    shuffled = users[:]
    for _ in range(200):
        random.shuffle(shuffled)
        if all(a != b for a, b in zip(users, shuffled)):
            break
    else:
        return send(peer_id, "❌ Не удалось провести жеребьёвку, попробуй ещё раз.")

    for u, t in zip(users, shuffled):
        cur.execute("UPDATE participants SET target_id=? WHERE game_id=? AND user_id=?",
                    (t, gid, u))
    cur.execute("UPDATE games SET status='closed' WHERE id=?", (gid,))
    conn.commit()

    # Отправляем результаты В ЛИЧКУ каждому участнику (peer_id = u)
    for u, t in zip(users, shuffled):
        cur.execute("SELECT wishlist FROM users WHERE vk_id=?", (t,))
        w = cur.fetchone()
        wish = (w[0] if w and w[0] else "(не указаны)")
        send(u, f"🎅 Твой подопечный: {get_name(t)} (vk.com/id{t})\n"
                f"Пожелания: {wish}")

    send(peer_id, f"✅ Жеребьёвка игры #{gid} проведена! Участники получили сообщения в личку.")

# ---------- Основной цикл ----------
print("Бот запущен...")
for event in longpoll.listen():
    if event.type != VkBotEventType.MESSAGE_NEW:
        continue
    
    # Получаем данные из сообщения
    msg = event.object.message
    peer_id = msg['peer_id']   # Куда отвечать (в чат или в личку)
    user_id = msg['from_id']   # Кто написал (для базы данных)
    text = msg['text'].strip()

    # Если это беседа (peer_id больше 2 млрд), очищаем текст от упоминания бота
    if peer_id >= 2000000000:
        # Убираем упоминание вида [club242810690|@santa] из текста
        text = text.replace(f"[club{GROUP_ID}|", "").replace("]", "").strip()

    low = text.lower()

    if low.startswith("/start"):
        cmd_start(peer_id)
    elif low.startswith("/create"):
        cmd_create(peer_id, user_id)
    elif low.startswith("/join"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            send(peer_id, "Использование: /join <id_игры>")
        else:
            cmd_join(peer_id, user_id, int(parts[1]))
    elif low.startswith("/wish"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send(peer_id, "Использование: /wish <текст пожеланий>")
        else:
            cmd_wish(peer_id, user_id, parts[1])
    elif low.startswith("/my"):
        cmd_my(peer_id, user_id)
    elif low.startswith("/draw"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            send(peer_id, "Использование: /draw <id_игры>")
        else:
            cmd_draw(peer_id, user_id, int(parts[1]))
