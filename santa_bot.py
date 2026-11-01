import os
import sqlite3
import vk_api
import requests
import json
import uuid
import time
import urllib3
import re
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

# Отключаем предупреждения SSL для GigaChat
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TOKEN = os.environ.get("VK_TOKEN")
GROUP_ID = "242810690"
GIGACHAT_AUTH_KEY = os.environ.get("GIGACHAT_AUTH_KEY")

# Глобальные переменные для токена GigaChat
GIGACHAT_TOKEN = None
TOKEN_EXPIRES_AT = 0

# --- БАЗА ДАННЫХ ---
conn = sqlite3.connect("santa.db")
cur = conn.cursor()
cur.executescript("""
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
        vk.messages.send(peer_id=peer_id, message=text, random_id=int(time.time() * 1000))
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
    if not token: return "❌ Не удалось авторизоваться в GigaChat. Проверьте ключ."

    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    
    # ВОТ ТУТ МЫ ИЗМЕНИЛИ ПРОМПТ
    system_prompt = (
        "Ты — участник этого чата, который только что вернулся и прочитал всё пропущенное. "
        "Перескажи кратко и в разговорной форме (как в жизни, другу), о чем тут говорили. "
        "Обязательно упоминай имена участников, которые что-то обсуждали, предлагали или задавали вопросы. "
        "Не используй списки, маркеры, звездочки и официальный тон. "
        "Пиши сплошным текстом, легко и непринужденно. Главное — передать суть и кто что сказал."
    )

    payload = json.dumps({
        "model": "GigaChat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Вот диалог:\n\n{chat_text}"}
        ],
        "temperature": 0.8 # Чуть повысили температуру для живости
    })
    
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Authorization': f'Bearer {token}'
    }

    try:
        response = requests.post(url, headers=headers, data=payload, verify=False, timeout=60)
        response.raise_for_status()
        summary = response.json()['choices'][0]['message']['content']
        # На всякий случай чистим от случайных звездочек
        return summary.replace("**", "").replace("*", "")
    except Exception as e:
        print(f"Ошибка GigaChat API: {e}")
        return "🤖 Нейросеть не смогла обработать запрос. Попробуйте позже."

# --- КОМАНДЫ ---
def cmd_start(peer_id):
    send(peer_id, "Привет! Я ELF. 🤖\n"
                  "Если вы тут много болтали, а вы всё пропустили — просто напишите слово «сводка».\n"
                  "Я прочитаю последние 100 сообщений и перескажу всё самое важное в двух словах!")

# --- ОСНОВНОЙ ЦИКЛ ---
print("Бот запущен (режим: живая сводка)...")
for event in longpoll.listen():
    if event.type != VkBotEventType.MESSAGE_NEW: continue
    
    msg = event.object.message
    peer_id = msg['peer_id']
    user_id = msg['from_id']
    text = msg['text'].strip()
    
    # Очистка от упоминаний в беседе
    if peer_id >= 2000000000:
        text = text.replace(f"[club{GROUP_ID}|", "").replace("]", "").replace(f"@club{GROUP_ID}", "").strip()

    # --- СОХРАНЯЕМ СООБЩЕНИЕ В БАЗУ ---
    if peer_id >= 2000000000 and user_id > 0:
        user_name = get_name(user_id)
        cur.execute("INSERT INTO chat_messages (peer_id, user_id, user_name, text) VALUES (?, ?, ?, ?)",
                    (peer_id, user_id, user_name, text))
        conn.commit()

    low = text.lower()

    # --- ЛОГИКА ВЫЖИМКИ (Только "сводка") ---
    clean_low = re.sub(r'[^\w\s]', '', low).strip()

    if clean_low == "сводка":
        if peer_id < 2000000000:
            send(peer_id, "Сводку можно делать только в общем чате (беседе)!")
        else:
            send(peer_id, "⏳ Так, сейчас гляну, что вы тут написали...")
            cur.execute("SELECT user_name, text FROM chat_messages WHERE peer_id = ? ORDER BY id DESC LIMIT 100", (peer_id,))
            rows = cur.fetchall()
            if not rows:
                send(peer_id, "❌ Тут пока пусто, даже читать нечего. Напишите что-нибудь!")
            else:
                history_text = "\n".join([f"{name}: {txt}" for name, txt in reversed(rows)])
                summary = ask_ai_summary(history_text)
                send(peer_id, f"🗣 В общем, вот что я понял:\n\n{summary}")

    # --- ПРИВЕТСТВИЕ ---
    elif any(w in low for w in ["привет", "помощь", "меню", "начать", "start"]):
        cmd_start(peer_id)
