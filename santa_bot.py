import os
import sqlite3
import random
import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType

# Токен будет браться из переменной окружения VK_TOKEN на хостинге
TOKEN = os.environ.get("VK_TOKEN")

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
    print("ОШИБКА: Не найден токен VK_TOKEN в переменных окружения!")
    exit(1)

vk_session = vk_api.VkApi(token=TOKEN)
vk = vk_session.get_api()
longpoll = VkLongPoll(vk_session)

def send(uid, text):
    vk.messages.send(user_id=uid, message=text,
                     random_id=random.randint(1, 2**31 - 1))

def get_name(uid):
    try:
        u = vk.users.get(user_ids=uid)[0]
        return f"{u['first_name']} {u['last_name']}"
    except Exception:
        return f"id{uid}"

# ---------- Команды ----------
def cmd_start(uid):
    send(uid, "Привет! Я бот «Тайный Санта» 🎅\n\n"
              "Команды:\n"
              "/create — создать игру\n"
              "/join <id> — присоединиться к игре\n"
              "/wish <текст> — задать пожелания\n"
              "/my — мои игры\n"
              "/draw <id> — жеребьёвка (только организатор)")

def cmd_create(uid):
    cur.execute("INSERT INTO games (creator_id) VALUES (?)", (uid,))
    gid = cur.lastrowid
    conn.commit()
    send(uid, f"✅ Игра #{gid} создана!\n"
              f"Пригласи друзей: /join {gid}\n"
              f"Когда все зайдут — запусти жеребьёвку: /draw {gid}")

def cmd_join(uid, gid):
    cur.execute("SELECT status FROM games WHERE id=?", (gid,))
    row = cur.fetchone()
    if not row:
        return send(uid, "❌ Игра не найдена.")
    if row[0] != 'open':
        return send(uid, "❌ Игра уже закрыта (жеребьёвка проведена).")
    cur.execute("INSERT OR IGNORE INTO participants (game_id, user_id) VALUES (?, ?)",
                (gid, uid))
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM participants WHERE game_id=?", (gid,))
    cnt = cur.fetchone()[0]
    send(uid, f"✅ Ты в игре #{gid}! Всего участников: {cnt}")

def cmd_wish(uid, text):
    cur.execute(
        "INSERT INTO users (vk_id, wishlist) VALUES (?, ?) "
        "ON CONFLICT(vk_id) DO UPDATE SET wishlist=excluded.wishlist",
        (uid, text)
    )
    conn.commit()
    send(uid, "🎁 Пожелания сохранены.")

def cmd_my(uid):
    cur.execute("""
        SELECT g.id, g.status, g.creator_id
        FROM games g JOIN participants p ON p.game_id = g.id
        WHERE p.user_id = ?
    """, (uid,))
    rows = cur.fetchall()
    if not rows:
        return send(uid, "Ты пока не участвуешь ни в одной игре.")
    msg = "Твои игры:\n"
    for gid, status, creator in rows:
        role = "организатор" if creator == uid else "участник"
        msg += f"#{gid} — {status}, {role}\n"
    send(uid, msg)

def cmd_draw(uid, gid):
    cur.execute("SELECT creator_id, status FROM games WHERE id=?", (gid,))
    row = cur.fetchone()
    if not row:
        return send(uid, "❌ Игра не найдена.")
    if row[0] != uid:
        return send(uid, "❌ Только организатор может запустить жеребьёвку.")
    if row[1] != 'open':
        return send(uid, "❌ Жеребьёвка уже проведена.")

    cur.execute("SELECT user_id FROM participants WHERE game_id=?", (gid,))
    users = [r[0] for r in cur.fetchall()]
    if len(users) < 2:
        return send(uid, "❌ Нужно минимум 2 участника.")

    # Перемешиваем, пока никто не попадёт сам на себя
    shuffled = users[:]
    for _ in range(200):
        random.shuffle(shuffled)
        if all(a != b for a, b in zip(users, shuffled)):
            break
    else:
        return send(uid, "❌ Не удалось провести жеребьёвку, попробуй ещё раз.")

    # users[i] дарит подарок shuffled[i]
    for u, t in zip(users, shuffled):
        cur.execute("UPDATE participants SET target_id=? WHERE game_id=? AND user_id=?",
                    (t, gid, u))
    cur.execute("UPDATE games SET status='closed' WHERE id=?", (gid,))
    conn.commit()

    # Рассылка
    for u, t in zip(users, shuffled):
        cur.execute("SELECT wishlist FROM users WHERE vk_id=?", (t,))
        w = cur.fetchone()
        wish = (w[0] if w and w[0] else "(не указаны)")
        send(u, f"🎅 Твой подопечный: {get_name(t)} (vk.com/id{t})\n"
                f"Пожелания: {wish}")

    send(uid, f"✅ Жеребьёвка игры #{gid} проведена! Участники получили сообщения.")

# ---------- Основной цикл ----------
print("Бот запущен...")
for event in longpoll.listen():
    if event.type != VkEventType.MESSAGE_NEW or not event.to_me:
        continue
    text = event.text.strip()
    low = text.lower()
    uid = event.user_id

    if low.startswith("/start"):
        cmd_start(uid)
    elif low.startswith("/create"):
        cmd_create(uid)
    elif low.startswith("/join"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            send(uid, "Использование: /join <id_игры>")
        else:
            cmd_join(uid, int(parts[1]))
    elif low.startswith("/wish"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send(uid, "Использование: /wish <текст пожеланий>")
        else:
            cmd_wish(uid, parts[1])
    elif low.startswith("/my"):
        cmd_my(uid)
    elif low.startswith("/draw"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            send(uid, "Использование: /draw <id_игры>")
        else:
            cmd_draw(uid, int(parts[1]))
