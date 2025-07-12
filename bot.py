import telebot
import schedule
import time
import requests
import pytz
import json
import os
from bs4 import BeautifulSoup
from datetime import datetime
import threading
import queue
from functools import wraps


# Initialize bot with channel support
bot = telebot.TeleBot('7790004997:AAHEgbPAKCi4H-oQraVnV9LeJxMjribHXD4', parse_mode=None)

# --- Notification Queue for Reliable Delivery ---
class NotificationTask:
    def __init__(self, chat_id, text, parse_mode=None, disable_web_page_preview=None, retries=0, reply_markup=None):
        self.chat_id = chat_id
        self.text = text
        self.parse_mode = parse_mode
        self.disable_web_page_preview = disable_web_page_preview
        self.retries = retries
        self.reply_markup = reply_markup


import json as _json
import math

class NotificationQueue:
    def __init__(self, max_retries=7, retry_delay=7, max_queue_size=100000, rate_limit_per_sec=18, queue_file='notification_queue.json'):
        self.queue = queue.Queue(maxsize=max_queue_size)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.running = False
        self.lock = threading.Lock()
        self.failed = []  # For logging failed notifications
        self.rate_limit_per_sec = rate_limit_per_sec
        self.last_send_time = 0
        self.queue_file = queue_file
        self._load_queue_from_file()

    def start(self):
        if not self.running:
            self.running = True
            t = threading.Thread(target=self.worker, daemon=True)
            t.start()

    def stop(self):
        self.running = False

    def put(self, task: NotificationTask):
        try:
            self.queue.put_nowait(task)
            self._save_queue_to_file()
        except queue.Full:
            print(f"[NotificationQueue] Queue is full! Dropping notification for {task.chat_id}")

    def worker(self):
        while self.running:
            try:
                task = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._rate_limit()
                sent = self._send(task)
                if not sent and task.retries < self.max_retries:
                    # Exponential backoff for retries
                    task.retries += 1
                    delay = self.retry_delay * (2 ** (task.retries - 1))
                    print(f"[NotificationQueue] Will retry for {task.chat_id} after {delay} sec (attempt {task.retries})")
                    threading.Timer(delay, lambda: self.put(task)).start()
                self._save_queue_to_file()
            except Exception as e:
                print(f"[NotificationQueue] Unexpected error: {e}")
            finally:
                self.queue.task_done()

    def _rate_limit(self):
        now = time.time()
        elapsed = now - self.last_send_time
        min_interval = 1.0 / self.rate_limit_per_sec
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self.last_send_time = time.time()

    def _send(self, task: NotificationTask):
        try:
            sent = bot.send_message(
                task.chat_id,
                task.text,
                parse_mode=task.parse_mode,
                disable_web_page_preview=task.disable_web_page_preview,
                reply_markup=task.reply_markup,
                timeout=30
            )
            return True
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            print(f"[NotificationQueue] Network error for {task.chat_id}, retry {task.retries+1}/{self.max_retries}: {e}")
            return False
        except telebot.apihelper.ApiException as e:
            print(f"[NotificationQueue] Telegram API error for {task.chat_id}: {e}")
            if "chat not found" in str(e).lower() or "forbidden" in str(e).lower():
                with self.lock:
                    if task.chat_id in enabled_groups:
                        del enabled_groups[task.chat_id]
                        save_state()
                return True  # Не надо больше пытаться
            return False
        except Exception as e:
            print(f"[NotificationQueue] Unexpected error for {task.chat_id}: {e}")
            return False
        # Если дошли сюда — неудача
        return False

    def _save_queue_to_file(self):
        # Сохраняем очередь на диск для восстановления после рестарта
        try:
            with self.lock:
                tasks = list(self.queue.queue)
                data = [
                    {
                        'chat_id': t.chat_id,
                        'text': t.text,
                        'parse_mode': t.parse_mode,
                        'disable_web_page_preview': t.disable_web_page_preview,
                        'retries': t.retries,
                        'reply_markup': None  # reply_markup не сериализуем, если надо — доработать
                    } for t in tasks
                ]
                with open(self.queue_file, 'w', encoding='utf-8') as f:
                    _json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            print(f"[NotificationQueue] Failed to save queue: {e}")

    def _load_queue_from_file(self):
        # Восстанавливаем очередь из файла
        try:
            if os.path.exists(self.queue_file):
                with open(self.queue_file, 'r', encoding='utf-8') as f:
                    data = _json.load(f)
                    for t in data:
                        self.queue.put_nowait(NotificationTask(
                            chat_id=t['chat_id'],
                            text=t['text'],
                            parse_mode=t.get('parse_mode'),
                            disable_web_page_preview=t.get('disable_web_page_preview'),
                            retries=t.get('retries', 0),
                            reply_markup=None
                        ))
                print(f"[NotificationQueue] Restored {self.queue.qsize()} tasks from file")
        except Exception as e:
            print(f"[NotificationQueue] Failed to load queue: {e}")

# Global notification queue instance
notification_queue = NotificationQueue(max_retries=7, retry_delay=7)
notification_queue.start()


# Global variables
latest_stock_data = {}

subscribed_users = set()  # Stores user IDs subscribed to updates
watched_items = {}  # Stores items users are watching: {user_id: {item_name: last_quantity}}
enabled_groups = {}  # Stores group IDs and their last stock message ID: {group_id: last_message_id}
user_languages = {}  # Stores user language preferences: {user_id: 'EN' or 'RU'}
new_users = set()  # Stores user IDs that haven't selected language yet

# --- Global message tracking for auto-clean ---
all_bot_messages = {}  # {chat_id: [message_id, ...]}



# --- Weather Watch ---
weather_watch_channels = set()  # chat_ids of channels/groups watching weather
last_weather_sent = {}  # {chat_id: last_weather_id}
# Для автоочистки сообщений в каналах
weather_sent_messages = {}  # {chat_id: [message_id, ...]}

def save_weather_watch_state():
    try:
        with open('weather_watch.json', 'w', encoding='utf-8') as f:
            json.dump({
                'channels': list(weather_watch_channels),
                'last_weather_sent': last_weather_sent,
                'weather_sent_messages': weather_sent_messages
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving weather watch state: {e}")

# --- Save/load all_bot_messages ---
def save_all_bot_messages():
    try:
        with open('all_bot_messages.json', 'w', encoding='utf-8') as f:
            json.dump(all_bot_messages, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving all_bot_messages: {e}")

def load_all_bot_messages():
    global all_bot_messages
    try:
        if os.path.exists('all_bot_messages.json'):
            with open('all_bot_messages.json', 'r', encoding='utf-8') as f:
                all_bot_messages = json.load(f)
    except Exception as e:
        print(f"Error loading all_bot_messages: {e}")

def load_weather_watch_state():
    global weather_watch_channels, last_weather_sent, weather_sent_messages
    try:
        if os.path.exists('weather_watch.json'):
            with open('weather_watch.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                weather_watch_channels = set(data.get('channels', []))
                last_weather_sent = data.get('last_weather_sent', {})
                weather_sent_messages = data.get('weather_sent_messages', {})
    except Exception as e:
        print(f"Error loading weather watch state: {e}")

def format_weather_event_message(weather_data):
    # Форматирует уведомление о погоде/ивенте красиво
    icon = weather_data.get('icon', '')
    desc = weather_data.get('description', '')
    bonuses = weather_data.get('cropBonuses', '')
    mutations = weather_data.get('mutations', [])
    rarity = weather_data.get('rarity', '')
    effect = weather_data.get('effectDescription', '')
    weather_type = weather_data.get('weatherType', '')
    msg = f"<b>{icon} {weather_type}</b>\n"
    if desc:
        msg += f"<i>{desc}</i>\n"
    if bonuses:
        msg += f"<b>Бонусы:</b> <code>{bonuses}</code>\n"
    if mutations:
        msg += f"<b>Мутации:</b> <code>{'; '.join(mutations)}</code>\n"
    if rarity:
        msg += f"<b>Редкость:</b> <code>{rarity}</code>\n"
    if effect:
        msg += f"<b>Эффект:</b> <code>{effect}</code>\n"
    return msg.strip()

def weather_watch_worker():
    import requests
    while True:
        try:
            if not weather_watch_channels:
                time.sleep(30)
                continue
            resp = requests.get('https://growagardenstock.com/api/stock/weather')
            if resp.status_code == 200:
                data = resp.json()
                # Уникальный id события — используем updatedAt или endTime
                weather_id = str(data.get('updatedAt') or data.get('endTime') or data.get('description'))
                for chat_id in list(weather_watch_channels):
                    last_id = last_weather_sent.get(str(chat_id))
                    if weather_id != last_id:
                        # Новое событие — отправить
                        msg = format_weather_event_message(data)
                        # Отправляем и сохраняем message_id для автоочистки
                        try:
                            sent = bot.send_message(chat_id, msg, parse_mode="HTML", disable_web_page_preview=True)
                            # Сохраняем message_id
                            if str(chat_id) not in weather_sent_messages:
                                weather_sent_messages[str(chat_id)] = []
                            weather_sent_messages[str(chat_id)].append(sent.message_id)
                            # Оставляем только последние 30 сообщений (на всякий случай)
                            if len(weather_sent_messages[str(chat_id)]) > 30:
                                weather_sent_messages[str(chat_id)] = weather_sent_messages[str(chat_id)][-30:]
                        except Exception as e:
                            print(f"[WeatherWatch] Error sending weather message: {e}")
                        last_weather_sent[str(chat_id)] = weather_id
                        save_weather_watch_state()
            time.sleep(60)
        except Exception as e:
            print(f"[WeatherWatch] Error: {e}")
            time.sleep(30)

# Автоочистка сообщений от бота в каналах раз в час
def all_bot_messages_cleaner():
    while True:
        try:
            for chat_id, msg_ids in list(all_bot_messages.items()):
                for msg_id in list(msg_ids):
                    try:
                        bot.delete_message(int(chat_id), msg_id)
                    except Exception as e:
                        pass
                all_bot_messages[chat_id] = []
            save_all_bot_messages()
        except Exception as e:
            print(f"[AllBotMessagesCleaner] Error: {e}")
        time.sleep(3600)  # 1 час

# Запуск воркера отслеживания погоды
def start_weather_watch_thread():
    t = threading.Thread(target=weather_watch_worker, daemon=True)
    t.start()



load_weather_watch_state()
load_all_bot_messages()
start_weather_watch_thread()
# Запуск глобальной автоочистки всех сообщений бота в каналах
threading.Thread(target=all_bot_messages_cleaner, daemon=True).start()

REQUIRED_CHANNELS = [
    {'id': -1002779274447, 'link': 'https://t.me/GrowaGardenStockNewsCLARTY'}
]

def is_user_subscribed(user_id):
    for ch in REQUIRED_CHANNELS:
        try:
            member = bot.get_chat_member(ch['id'], user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                return False
        except Exception:
            return False
    return True

# Декоратор для проверки подписки

def require_subscription(func):
    @wraps(func)
    def wrapper(message, *args, **kwargs):
        user_id = message.from_user.id if hasattr(message, 'from_user') else None
        if user_id and not is_user_subscribed(user_id):
            text = (
                "❗ Для использования бота подпишитесь на каналы:\n"
                "• <a href='https://t.me/GrowaGardenStockNewsCLARTY'>@GrowaGardenStockNewsCLARTY</a>"
            )
            bot.reply_to(message, text, parse_mode="HTML", disable_web_page_preview=True)
            return
        return func(message, *args, **kwargs)
    return wrapper

# Function to save state
def save_state():
    """Save all state data to files"""
    try:
        # Save user languages with UTF-8 encoding
        with open('user_languages.json', 'w', encoding='utf-8') as f:
            json.dump({
                'languages': {str(k): v for k, v in user_languages.items()},
                'new_users': list(new_users)
            }, f, ensure_ascii=False, indent=2)
        
        # Save enabled groups with UTF-8 encoding
        with open('enabled_groups.json', 'w', encoding='utf-8') as f:
            json.dump({str(k): v for k, v in enabled_groups.items()}, f, ensure_ascii=False, indent=2)
        
        # Save watched items with UTF-8 encoding
        with open('watched_items.json', 'w', encoding='utf-8') as f:
            json.dump({str(k): v for k, v in watched_items.items()}, f, ensure_ascii=False, indent=2)
        
        # Save subscribed users
        save_subscribed_users()
            
        print("Successfully saved all state data")
    except Exception as e:
        print(f"Error saving state: {e}")

def save_subscribed_users():
    """Save subscribed users to file"""
    try:
        # Convert set to a dictionary with proper structure
        subscribers_data = {}
        for user_id in subscribed_users:
            # Convert each user_id to string for JSON compatibility
            subscribers_data[str(user_id)] = {
                'last_message': None  # Initialize with no last message
            }
        
        # Save the structured data
        with open('subscribers.json', 'w', encoding='utf-8') as f:
            json.dump({"subscribers": subscribers_data}, f, ensure_ascii=False, indent=2)
        print(f"Saved {len(subscribed_users)} subscribers to file")
    except Exception as e:
        print(f"Error saving subscribers: {e}")

def load_subscribed_users():
    """Load subscribed users from file"""
    global subscribed_users
    try:
        if os.path.exists('subscribers.json'):
            with open('subscribers.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Initialize as a set with user IDs
                subscribed_users = {int(user_id) for user_id in data.get("subscribers", {}).keys()}
            print(f"Loaded {len(subscribed_users)} subscribers")
        else:
            subscribed_users = set()
    except Exception as e:
        print(f"Error loading subscribers: {e}")
        subscribed_users = set()

def subscribe_user(user_id):
    """Subscribe a user and save the state"""
    global subscribed_users
    try:
        # Add user to the set
        subscribed_users.add(user_id)
        # Save the updated state
        save_subscribed_users()
        return True
    except Exception as e:
        print(f"Error subscribing user {user_id}: {e}")
        return False

def unsubscribe_user(user_id):
    """Unsubscribe a user and save the state"""
    global subscribed_users
    try:
        # Remove user from the set if they exist
        if user_id in subscribed_users:
            subscribed_users.remove(user_id)
            # Save the updated state
            save_subscribed_users()
            return True
    except Exception as e:
        print(f"Error unsubscribing user {user_id}: {e}")
    return False

def load_state():
    """Load all state data from files"""
    global user_languages, enabled_groups, watched_items, new_users, subscribed_users
    
    try:
        # Load user languages
        if os.path.exists('user_languages.json'):
            with open('user_languages.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                user_languages = {int(k): v for k, v in data.get('languages', {}).items()}
                new_users = set(data.get('new_users', []))
        
        # Load enabled groups
        if os.path.exists('enabled_groups.json'):
            with open('enabled_groups.json', 'r', encoding='utf-8') as f:
                enabled_groups = {int(k): v for k, v in json.load(f).items()}
        
        # Load watched items
        if os.path.exists('watched_items.json'):
            with open('watched_items.json', 'r', encoding='utf-8') as f:
                watched_items = {int(k): v for k, v in json.load(f).items()}
        
        # Load subscribed users
        load_subscribed_users()
                
        print("Successfully loaded all state data")
    except Exception as e:
        print(f"Error loading state: {e}")
        # Initialize empty state if loading fails
        user_languages = {}
        enabled_groups = {}
        watched_items = {}
        new_users = set()
        subscribed_users = set()

# Moscow timezone
moscow_tz = pytz.timezone('Europe/Moscow')

def get_stock_data(retry_count=3, retry_delay=5):
    for attempt in range(retry_count):
        try:
            url = 'https://growagardenvalues.com/stock/stocks.php'
            response = requests.get(url, timeout=30)
            
            if response.status_code != 200:
                print(f"Error: Server returned status code {response.status_code}")
                if attempt < retry_count - 1:
                    print(f"Retrying in {retry_delay} seconds... (Attempt {attempt + 1}/{retry_count})")
                    time.sleep(retry_delay)
                    continue
                return False
            
            soup = BeautifulSoup(response.text, 'html.parser')
            stock_info = {}
            
            # Get all stock sections
            sections = soup.find_all('section', class_='stock-section')
            
            for section in sections:
                section_id = section.get('id', '').replace('-section', '').title()
                # Пропускаем Event-Shop-Stock
                if section_id.lower() == 'event-shop-stock':
                    continue
                stock_info[section_id] = {}
                
                # Get all items in this section
                items = section.find_all('div', class_='stock-item')
                
                # Dictionary to keep track of item counts for duplicate names
                item_counts = {}
                
                for item in items:
                    name = item.find('div', class_='item-name')
                    quantity = item.find('div', class_='item-quantity')
                    
                    if name and quantity:
                        name = name.text.strip()
                        quantity_text = quantity.text.strip()
                        
                        # Handle special cases for weather section
                        if section_id == 'Weather':
                            if 'Active' in quantity_text:
                                stock_info[section_id][name] = 'Active'
                            else:
                                stock_info[section_id][name] = quantity_text
                        else:
                            # Remove 'x' from quantity and convert to number
                            quantity_value = quantity_text.replace('x', '').strip()
                            # Handle duplicate items by adding a number suffix
                            if name in item_counts:
                                item_counts[name] += 1
                                item_key = f"{name} #{item_counts[name]}"
                            else:
                                item_counts[name] = 1
                                item_key = name
                                
                            stock_info[section_id][item_key] = quantity_value
            
            # Add timestamp in Moscow timezone
            moscow_time = datetime.now(moscow_tz)
            stock_info['timestamp'] = moscow_time.strftime('%Y-%m-%d %H:%M:%S (MSK)')
            
            global latest_stock_data
            latest_stock_data = stock_info
            return True
        except Exception as e:
            print(f"Error fetching data (attempt {attempt + 1}/{retry_count}): {e}")
            if attempt < retry_count - 1:
                print(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                print("All retry attempts failed.")
                return False
    
    return False  # В случае, если все попытки не удались

def schedule_checker():
    while True:
        try:
            now = datetime.now()
            minutes_to_next = 5 - (now.minute % 5)
            if minutes_to_next == 5:
                minutes_to_next = 0
            seconds_to_next = minutes_to_next * 60 - now.second
            if seconds_to_next > 0:
                time.sleep(seconds_to_next)
            # Сбросить значения до получения нового стока!
            reset_watched_items_quantities()
            # Ждать 20 секунд после наступления 5-минутки
            time.sleep(20)
            if get_stock_data():
                check_and_notify_watched_items(latest_stock_data)
                send_stock_updates(skip_watched_items=True)
            else:
                print("Failed to get stock data in schedule_checker")
            time.sleep(250)
        except Exception as e:
            print(f"Error in schedule_checker: {e}")
            time.sleep(5)

def reset_watched_items_quantities():
    now = datetime.now()
    minute = now.minute
    # Сброс для обычных предметов только если minute кратен 5 (0,5,10,...,55), для яиц только если minute == 0 или 30
    for chat_id, watched in watched_items.items():
        for watched_item, item_data in watched.items():
            is_egg = item_data.get('is_egg', watched_item.endswith('-egg'))
            if is_egg:
                if minute in [0, 30]:
                    watched_items[chat_id][watched_item]['quantity'] = None
            else:
                if minute % 5 == 0:
                    watched_items[chat_id][watched_item]['quantity'] = None
    save_state()

def get_next_update_time(section_name, current_time):
    minute = current_time.minute
    if section_name in ['Seeds', 'Gears']:
        # Update every 5 minutes
        minutes_until_next = 5 - (minute % 5)
        return f"{minutes_until_next} min"
    elif section_name in ['Eggs', 'Event-Shop-Stock']:
        # Update every 30 minutes
        minutes_until_next = 30 - (minute % 30)
        return f"{minutes_until_next} min"
    return None

# Item emoji mappings
GEAR_EMOJIS = {
    'watering can': '💧',
    'trowel': '🏺',
    'recall wrench': '🔧',
    'basic sprinkler': '💦',
    'advanced sprinkler': '🌊',
    'godly sprinkler': '⚡',
    'lightning rod': '⚡',
    'master sprinkler': '🌊',
    'cleaning spray': '🧪',
    'favourite tool': '⭐',
    'harvest tool': '🌾',
    'friendship pot': '🪴'
}

SEED_EMOJIS = {
    'carrot': '🥕',
    'strawberry': '🍓',
    'blueberry': '🫐',
    'orange tulip': '🌷',
    'tomato': '🍅',
    'daffodil': '💐',
    'corn': '🌽',
    'watermelon': '🍉',
    'pumpkin': '🎃',
    'apple': '🍎',
    'bamboo': '🎋',
    'coconut': '🥥',
    'cactus': '🌵',
    'dragon fruit': '🐉',
    'mango': '🥭',
    'mushroom': '🍄',
    'grape': '🍇',
    'pepper': '🌶️',
    'cacao': '🍫',
    'beanstalk': '🌱',
    'ember lily': '🔥',
    'sugar apple': '🍎'
}

EGG_EMOJIS = {
    'common egg': '🥚',
    'uncommon egg': '🥚',
    'rare egg': '✨',
    'legendary egg': '🌟',
    'mythical egg': '🌈',
    'bug egg': '🐛'
}

# Weather emoji mappings
WEATHER_EMOJIS = {
    'rain': '🌧️',
    'thunderstorm': '⛈️',
    'frost': '❄️',
    'night': '🌙',
    'meteor shower': '☄️',
    'blood moon': '🌕',
    'bee swarm': '🐝',
    'working bee swarm': '👷🐝',
    'disco': '🪩',
    'jandel storm': '🐒',
    'sheckle rain': '💰',
    'chocolate rain': '🍫',
    'lazer storm': '🔫',
    'tornado': '🌪️',
    'black hole': '🌌',
    'sun god': '☀️',
    'floating jandel': '👻',
    'volcano': '🌋',
    'meteor strike': '💫'
}

def get_item_emoji(item_name, section):
    """Get emoji for an item based on its section"""
    item_name_lower = item_name.lower()
    
    if section == 'Gears':
        return GEAR_EMOJIS.get(item_name_lower, '❓')
    elif section == 'Seeds':
        return SEED_EMOJIS.get(item_name_lower, '❓')
    elif section == 'Eggs':
        return EGG_EMOJIS.get(item_name_lower, '❓')
    elif section == 'Cosmetics':
        return '🎨'
    elif section == 'Event-Shop-Stock':
        return '🐝'
    else:
        return '❓'

def get_weather_emoji(weather_name):
    """Get emoji for weather condition"""
    return WEATHER_EMOJIS.get(weather_name.lower(), '❓')

def format_stock_message(current_time=None):
    if not latest_stock_data:
        return "❌ No stock data available"
    
    sections_order = ['Seeds', 'Gears', 'Eggs', 'Weather', 'Event-Shop-Stock', 'Cosmetics']
    
    message = "🌿 Garden Values Stock Information 🌿\n"
    message += "━━━━━━━━━━━━━━━━━━━━━\n"
    
    for section in sections_order:
        if section in latest_stock_data:
            items = latest_stock_data[section]
            section_emoji = '🌱' if section == 'Seeds' else '🛠️' if section == 'Gears' else '🥚' if section == 'Eggs' else '🌤️' if section == 'Weather' else '🐝' if section == 'Event-Shop-Stock' else '🎨'
            message += f"\n{section_emoji} {section} {section_emoji}\n"
            message += "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"

            # Group items by base name
            grouped_items = {}
            for item_name, quantity in items.items():
                base_name = item_name.split(' #')[0]
                if base_name not in grouped_items:
                    grouped_items[base_name] = []
                grouped_items[base_name].append(quantity)
                
            for base_name, quantities in grouped_items.items():
                # Get appropriate emoji based on section
                if section == 'Weather':
                    item_emoji = get_weather_emoji(base_name)
                else:
                    item_emoji = get_item_emoji(base_name, section)
                
                if section == 'Weather':
                    if 'Active' in quantities:
                        message += f"{item_emoji} {base_name}: 🟢 Active\n"
                    else:
                        message += f"{item_emoji} {base_name}: {quantities[0]}\n"
                else:
                    if len(quantities) == 1:
                        message += f"{item_emoji} {base_name}: {quantities[0]}\n"
                    else:
                        message += f"{item_emoji} {base_name}:"
                        for i, qty in enumerate(quantities, 1):
                            message += f" {qty}"
                            if i < len(quantities):
                                message += ", "
                        message += "\n"
            
            if current_time:
                next_update = get_next_update_time(section, current_time)
                if next_update:
                    message += f"⏰ Next update in: {next_update}\n"
            
            message += "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
    
    moscow_time = datetime.now(moscow_tz).strftime('%Y-%m-%d %H:%M:%S (MSK)')
    message += f"\n⏰ Last updated: {moscow_time}"
    message += "\n━━━━━━━━━━━━━━━━━━━━━"
    
    return message

def format_watch_item_message(item_name, quantity, is_private=True):
    """Format a watch item notification message"""
    item_section = 'Eggs' if item_name.endswith('-egg') else (
        'Gears' if item_name.lower() in GEAR_EMOJIS else
        'Seeds' if item_name.lower() in SEED_EMOJIS else
        'Event-Shop-Stock' if 'event' in item_name.lower() else
        'Cosmetics' if 'cosmetic' in item_name.lower() else None
    )
    
    item_display_name = item_name.rstrip('-egg')
    item_emoji = get_item_emoji(item_display_name, item_section) if item_section else '❓'
    
    if is_private:
        return f"{item_emoji} {item_display_name} в стоке {item_emoji}\nКоличество: {quantity}"
    else:
        return (f"{item_emoji} {item_display_name} в стоке {item_emoji}\n"
                f"Количество: {quantity}\n\n"
                f"❤️- залутал, спасибо\n"
                f"⚡️- не успел\n\n"
                f"Включаем уведомления чтобы не пропускать сочную имбу🔔🔥")

def format_watch_items_notification(items_found, moscow_time):
    """Компактное уведомление для каналов/групп без предпросмотра"""
    lines = ["🚨 Редкий предмет! 🚨"]
    for item, qty in items_found:
        lines.append(f"⚡ {item} — {qty} шт.")
    # Ссылка без предпросмотра
    lines.append('<a href="https://t.me/StockBotVDLK_bot">🌈Бот по стокам🌈</a>')
    # Оборачиваем всё сообщение в <b>...</b>
    return f"<b>{chr(10).join(lines)}</b>"


def check_and_notify_watched_items(new_data):
    if not new_data:
        print("No data available for watched items check")
        return

    moscow_time = datetime.now(moscow_tz).strftime('%H:%M:%S')
    watched_items_copy = {k: v.copy() for k, v in watched_items.items()}

    for chat_id, watched in watched_items_copy.items():
        items_found = []
        is_private = str(chat_id)[0] != '-' if isinstance(chat_id, (int, str)) else False
        is_channel_or_group = not is_private

        now = datetime.now()
        minute = now.minute

        # Сброс количества: только в 5-минутки для обычных, только в 30-минутки для яиц
        for watched_item, item_data in watched.items():
            if not isinstance(item_data, dict):
                watched_items[chat_id][watched_item] = {
                    'quantity': item_data,
                    'is_egg': watched_item.endswith('-egg')
                }
                item_data = watched_items[chat_id][watched_item]

            is_egg = item_data.get('is_egg', watched_item.endswith('-egg'))

            if is_egg:
                if minute % 30 == 0:
                    watched_items[chat_id][watched_item] = {'quantity': None, 'is_egg': True}
            else:
                if minute % 5 == 0 and minute % 30 != 0:
                    watched_items[chat_id][watched_item] = {'quantity': None, 'is_egg': False}

        # Проверка появления предметов
        for section, items in new_data.items():
            if section != 'timestamp':
                for stock_item, quantity in items.items():
                    for watched_item, item_data in watched.items():
                        if not isinstance(item_data, dict):
                            continue
                        is_egg = item_data.get('is_egg', watched_item.endswith('-egg'))
                        last_quantity = item_data.get('quantity', None)
                        base_name = watched_item.rstrip('-egg').lower()
                        if base_name in stock_item.lower():
                            current_quantity = quantity
                            if last_quantity is None and current_quantity not in [None, '', '0']:
                                items_found.append((watched_item, current_quantity))
                                watched_items[chat_id][watched_item]['quantity'] = current_quantity
                            elif last_quantity not in [None, '', '0'] and str(current_quantity) != str(last_quantity):
                                items_found.append((watched_item, current_quantity))
                                watched_items[chat_id][watched_item]['quantity'] = current_quantity

        if items_found:
            seen = set()
            items_found = [x for x in items_found if not (x[0] in seen or seen.add(x[0]))]
            if is_channel_or_group:
                final_message = format_watch_items_notification(items_found, moscow_time)
                notification_queue.put(NotificationTask(
                    chat_id=chat_id,
                    text=final_message,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                ))
                save_state()
            else:
                final_message = "\n\n".join([
                    format_watch_item_message(item, qty, True) for item, qty in items_found
                ])
                notification_queue.put(NotificationTask(
                    chat_id=chat_id,
                    text=final_message
                ))
                save_state()


# Надёжная отправка сообщений через очередь

def send_message_with_retry(chat_id, text, parse_mode=None, disable_web_page_preview=None, reply_markup=None):
    # Отправляем через очередь, но message_id получаем только если напрямую через bot.send_message
    # Поэтому для отслеживания message_id — отправим напрямую, если нет очереди, иначе — через очередь и хукнем в NotificationQueue._send
    try:
        sent = bot.send_message(chat_id, text, parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview, reply_markup=reply_markup)
        # Сохраняем message_id для автоочистки
        chat_id_str = str(chat_id)
        if chat_id_str not in all_bot_messages:
            all_bot_messages[chat_id_str] = []
        all_bot_messages[chat_id_str].append(sent.message_id)
        # Оставляем только последние 100 сообщений на всякий случай
        if len(all_bot_messages[chat_id_str]) > 100:
            all_bot_messages[chat_id_str] = all_bot_messages[chat_id_str][-100:]
        save_all_bot_messages()
        return True
    except Exception as e:
        # Если не удалось напрямую — отправим через очередь (но message_id не получим)
        notification_queue.put(NotificationTask(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            reply_markup=reply_markup
        ))
        return True


def send_stock_updates(skip_watched_items=False):
    """Send stock updates to all groups and subscribed users (через очередь)"""
    global latest_stock_data
    current_time = datetime.now(moscow_tz)
    message = format_stock_message(current_time)

    print(f"Sending updates to {len(enabled_groups)} enabled groups/channels...")
    for chat_id in enabled_groups:
        send_message_with_retry(chat_id, message)

    print(f"Sending updates to {len(subscribed_users)} subscribed users...")
    for user_id in subscribed_users:
        send_message_with_retry(user_id, message)

    if not skip_watched_items and latest_stock_data:
        check_and_notify_watched_items(latest_stock_data)
    return True

# Language selection keyboard markup
def get_language_keyboard():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_RU"),
        telebot.types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_EN")
    )
    return markup

def get_start_keyboard():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("📊 Чек сток", callback_data="check_stock"))
    return markup

def get_stock_keyboard():
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("🔄 Обновить сток", callback_data="refresh_stock"),
        telebot.types.InlineKeyboardButton("⬅️ Назад", callback_data="back_to_start")
    )
    return markup

@bot.callback_query_handler(func=lambda call: call.data.startswith('lang_'))
def callback_language(call):
    user_id = call.from_user.id
    language = call.data.split('_')[1]
    user_languages[user_id] = language
    if user_id in new_users:
        new_users.remove(user_id)
    save_state()
    response = "✅ Language set to English" if language == 'EN' else "✅ Язык установлен на Русский"
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=response
    )
    show_start_message(call.message, language, with_keyboard=True)

# Модификация show_start_message для поддержки инлайн-клавиатуры

def show_start_message(message, language, with_keyboard=False):
    if language == 'EN':
        response = (
            "👋 Hello! I'm a Garden Values stock tracking bot. Available commands:\n\n"
            "📊 /check - check current stock\n"
            "🔔 /subscribe - subscribe to stock updates\n"
            "🔕 /unsubscribe - unsubscribe from updates\n"
            "👀 /checkitem <name> - track item appearance\n"
            "🚫 /uncheckitem <name> - stop tracking item\n"
            "🌍 /setlanguage - change language\n"
            "ℹ️ Stock updates occur:\n"
            "• Seeds and Gears - every 5 minutes\n"
            "• Eggs - every 30 minutes\n"
            "• Event Shop - every 30 minutes"
        )
    else:
        response = (
            "👋 Привет! Я бот для отслеживания стока Grow a Garden. Доступные команды:\n\n"
            "📊 /check - проверить текущий сток\n"
            "🔔 /subscribe - подписаться на обновления стока\n"
            "🔕 /unsubscribe - отписаться от обновлений\n"
            "👀 /checkitem <название> - отслеживать появление предмета\n"
            "🚫 /uncheckitem <название> - прекратить отслеживание предмета\n"
            "🌍 /setlanguage - изменить язык\n"
            "ℹ️ Обновления стока происходят:\n"
            "• Seeds и Gears - каждые 5 минут\n"
            "• Eggs - каждые 30 минут\n"
            "• Event Shop - каждые 30 минут"
        )
    if with_keyboard:
        bot.reply_to(message, response, reply_markup=get_start_keyboard())
    else:
        bot.reply_to(message, response)

@bot.callback_query_handler(func=lambda call: call.data == 'check_stock')
def callback_check_stock(call):
    current_time = datetime.now()
    if get_stock_data():
        response = format_stock_message(current_time)
    else:
        response = "❌ Ошибка: Не удалось получить данные о стоке. Попробуйте позже."
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=response,
        reply_markup=get_stock_keyboard()
    )

@bot.callback_query_handler(func=lambda call: call.data == 'refresh_stock')
def callback_refresh_stock(call):
    current_time = datetime.now()
    if get_stock_data():
        response = format_stock_message(current_time)
    else:
        response = "❌ Ошибка: Не удалось получить данные о стоке. Попробуйте позже."
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=response,
        reply_markup=get_stock_keyboard()
    )

@bot.callback_query_handler(func=lambda call: call.data == 'back_to_start')
def callback_back_to_start(call):
    user_id = call.from_user.id
    language = user_languages.get(user_id, 'RU')
    show_start_message(call.message, language, with_keyboard=True)
    try:
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None
        )
    except:
        pass

@bot.message_handler(commands=['start'])
@require_subscription
def start_command(message):
    user_id = message.from_user.id
    if user_id not in user_languages:
        new_users.add(user_id)
        bot.reply_to(
            message,
            "🌍 Please select your language / Пожалуйста, выберите язык:",
            reply_markup=get_language_keyboard()
        )
    else:
        show_start_message(message, user_languages[user_id], with_keyboard=True)

@bot.message_handler(commands=['subscribe'])
@require_subscription
def subscribe_command(message):
    user_id = message.from_user.id
    language = user_languages.get(user_id, 'RU')
    
    if user_id in subscribed_users:
        msg = "✋ You are already subscribed to stock updates!" if language == 'EN' else "✋ Вы уже подписаны на обновления стока!"
    else:
        if subscribe_user(user_id):
            msg = "✅ You will now receive stock updates automatically every 5 minutes!" if language == 'EN' else "✅ Теперь я буду автоматически отправлять сток каждые 5 минут!"
        else:
            msg = "❌ Failed to subscribe. Please try again later!" if language == 'EN' else "❌ Не удалось подписаться. Попробуйте позже!"
    sent = bot.send_message(message.chat.id, msg)
    # Сохраняем message_id для автоочистки
    chat_id_str = str(message.chat.id)
    if chat_id_str not in all_bot_messages:
        all_bot_messages[chat_id_str] = []
    all_bot_messages[chat_id_str].append(sent.message_id)
    if len(all_bot_messages[chat_id_str]) > 100:
        all_bot_messages[chat_id_str] = all_bot_messages[chat_id_str][-100:]
    save_all_bot_messages()

@bot.message_handler(commands=['unsubscribe'])
@require_subscription
def unsubscribe_command(message):
    user_id = message.from_user.id
    language = user_languages.get(user_id, 'RU')
    
    if user_id in subscribed_users:
        if unsubscribe_user(user_id):
            msg = "✅ You have successfully unsubscribed from stock updates!" if language == 'EN' else "✅ Вы успешно отписались от обновлений стока!"
        else:
            msg = "❌ Failed to unsubscribe. Please try again later!" if language == 'EN' else "❌ Не удалось отписаться. Попробуйте позже!"
    else:
        msg = "❌ You were not subscribed to updates!" if language == 'EN' else "❌ Вы не были подписаны на обновления!"
    sent = bot.send_message(message.chat.id, msg)
    chat_id_str = str(message.chat.id)
    if chat_id_str not in all_bot_messages:
        all_bot_messages[chat_id_str] = []
    all_bot_messages[chat_id_str].append(sent.message_id)
    if len(all_bot_messages[chat_id_str]) > 100:
        all_bot_messages[chat_id_str] = all_bot_messages[chat_id_str][-100:]
    save_all_bot_messages()

# Dictionary to track user's last stock check time
user_last_check = {}

def can_check_stock(user_id):
    """Check if user can request stock (4 second cooldown)"""
    current_time = time.time()
    if user_id in user_last_check:
        if current_time - user_last_check[user_id] < 4:
            return False
    user_last_check[user_id] = current_time
    return True

@bot.message_handler(func=lambda message: message.text and message.text.lower() in ['чек сток', 'чек'])
@require_subscription
def text_check_command(message):
    if message.chat.type not in ['group', 'supergroup']:
        return

    user_id = message.from_user.id
    
    # Check cooldown
    if not can_check_stock(user_id):
        return  # Silently ignore if on cooldown
    
    current_time = datetime.now()
    if get_stock_data():
        response = format_stock_message(current_time)
        send_message_with_retry(message.chat.id, response)
    else:
        bot.reply_to(message, "❌ Ошибка: Не удалось получить данные о стоке. Попробуйте позже.")

@bot.message_handler(commands=['checkitem'])
@require_subscription
def checkitem_command(message):
    chat_id = message.chat.id
    chat_type = message.chat.type
    is_private = chat_type == 'private'
    user_id = message.from_user.id if hasattr(message, 'from_user') else None
    language = user_languages.get(user_id, 'RU') if is_private else None
    
    # Get the command arguments
    if hasattr(message, 'text'):
        args = message.text.split(maxsplit=1)
    else:
        # For channel posts
        args = message.caption.split(maxsplit=1) if message.caption else [message.text]
    
    if len(args) < 2:
        msg = "❌ Please specify the item name after the command!" if language == 'EN' else "❌ Пожалуйста, укажите название предмета после команды!"
        bot.send_message(chat_id, msg)
        return
    
    item_name = args[1].strip()
    
    # Check permissions for group/channel
    if not is_private:
        if chat_type in ['group', 'supergroup']:
            try:
                user = bot.get_chat_member(chat_id, user_id)
                if user.status not in ['creator', 'administrator']:
                    bot.send_message(chat_id, "❌ Only administrators can use this command!")
                    return
            except Exception as e:
                print(f"Failed to check user permissions: {e}")
                bot.send_message(chat_id, "❌ Error checking administrator rights!")
                return
    
    # Add to watched items
    if chat_id not in watched_items:
        watched_items[chat_id] = {}
    
    # Check if it's an egg item by checking the -egg suffix
    is_egg = item_name.endswith('-egg')
    
    # Store item with metadata
    watched_items[chat_id][item_name] = {
        'quantity': None,
        'is_egg': is_egg
    }
    save_state()
    
    # Prepare notification message
    update_interval = "30 minutes" if is_egg else "5 minutes"
    
    if is_private:
        msg = (f"✅ You will receive notifications when item '{item_name.rstrip('-egg')}' appears in stock!\n" +
              f"🕒 Updates every {update_interval}") if language == 'EN' else \
              (f"✅ Вы будете получать уведомления о появлении предмета '{item_name.rstrip('-egg')}' в стоке!\n" +
              f"🕒 Обновление каждые {update_interval}")
    else:
        msg = (f"✅ This chat will receive notifications when '{item_name.rstrip('-egg')}' appears in stock!\n" +
              f"🕒 Updates every {update_interval}")
    
    bot.send_message(chat_id, msg)

@bot.message_handler(commands=['uncheckitem'])
@require_subscription
def uncheckitem_command(message):
    chat_id = message.chat.id
    chat_type = message.chat.type
    is_private = chat_type == 'private'
    user_id = message.from_user.id if hasattr(message, 'from_user') else None
    language = user_languages.get(user_id, 'RU') if is_private else None
    
    # Get the command arguments
    if hasattr(message, 'text'):
        args = message.text.split(maxsplit=1)
    else:
        # For channel posts
        args = message.caption.split(maxsplit=1) if message.caption else [message.text]
    
    if len(args) < 2:
        msg = "❌ Please specify the item name after the command!" if language == 'EN' else "❌ Пожалуйста, укажите название предмета после команды!"
        bot.send_message(chat_id, msg)
        return
    
    item_name = args[1].strip()
    
    # Check permissions for group/channel
    if not is_private:
        if chat_type in ['group', 'supergroup']:
            try:
                user = bot.get_chat_member(chat_id, user_id)
                if user.status not in ['creator', 'administrator']:
                    bot.send_message(chat_id, "❌ Only administrators can use this command!")
                    return
            except Exception as e:
                print(f"Failed to check user permissions: {e}")
                bot.send_message(chat_id, "❌ Error checking administrator rights!")
                return
    
    if chat_id not in watched_items or item_name not in watched_items[chat_id]:
        msg = f"❌ You are not tracking the item '{item_name}'!" if language == 'EN' else f"❌ Вы не отслеживаете предмет '{item_name}'!"
        bot.send_message(chat_id, msg)
        return
    
    del watched_items[chat_id][item_name]
    if not watched_items[chat_id]:
        del watched_items[chat_id]
    
    save_state()
    if is_private:
        msg = f"✅ Stopped tracking the item '{item_name}'!" if language == 'EN' else f"✅ Прекращено отслеживание предмета '{item_name}'!"
    else:
        msg = f"✅ This chat will no longer receive notifications for '{item_name}'!"
    
    bot.send_message(chat_id, msg)

@bot.message_handler(commands=['check'])
@require_subscription
def check_command(message):
    chat_id = message.chat.id
    chat_type = message.chat.type
    user_id = message.from_user.id
    language = user_languages.get(user_id, 'RU')
    current_time = datetime.now()
    
    if get_stock_data():
        response = format_stock_message(current_time)
        # Use send_message_with_retry to properly handle message deletion
        send_message_with_retry(chat_id, response)
    else:
        error_msg = "❌ Error: Could not fetch stock data. Please try again later." if language == 'EN' else "❌ Ошибка: Не удалось получить данные о стоке. Попробуйте позже."
        sent = bot.send_message(message.chat.id, error_msg)
        chat_id_str = str(message.chat.id)
        if chat_id_str not in all_bot_messages:
            all_bot_messages[chat_id_str] = []
        all_bot_messages[chat_id_str].append(sent.message_id)
        if len(all_bot_messages[chat_id_str]) > 100:
            all_bot_messages[chat_id_str] = all_bot_messages[chat_id_str][-100:]
        save_all_bot_messages()

# Add channel post handler for check command
@bot.channel_post_handler(commands=['check'])
def channel_check_command(message):
    check_command(message)

@bot.message_handler(commands=['setlanguage'])
@require_subscription
def set_language_command(message):
    user_id = message.from_user.id
    sent = bot.send_message(
        message.chat.id,
        "🌍 Please select your language / Пожалуйста, выберите язык:",
        reply_markup=get_language_keyboard()
    )
    chat_id_str = str(message.chat.id)
    if chat_id_str not in all_bot_messages:
        all_bot_messages[chat_id_str] = []
    all_bot_messages[chat_id_str].append(sent.message_id)
    if len(all_bot_messages[chat_id_str]) > 100:
        all_bot_messages[chat_id_str] = all_bot_messages[chat_id_str][-100:]
    save_all_bot_messages()

@bot.message_handler(commands=['enablegroupstock'])
def enablegroupstock_command(message):
    chat_id = message.chat.id
    chat_type = message.chat.type
    
    try:
        if chat_type == 'private':
            if chat_id not in subscribed_users:
                subscribed_users.add(chat_id)
                save_state()
                bot.reply_to(message, "✅ You have been subscribed to stock updates!")
            else:
                bot.reply_to(message, "✋ You are already subscribed to stock updates!")
            return
            
        if chat_type not in ['group', 'supergroup', 'channel']:
            bot.send_message(chat_id, "❌ This command only works in groups and channels!")
            return

        if chat_id in enabled_groups:
            bot.send_message(chat_id, "✋ Stock updates are already enabled!")
            return
        
        try:
            bot_member = bot.get_chat_member(chat_id, bot.get_me().id)
            if bot_member.status != 'administrator':
                bot.send_message(chat_id, "❌ Bot must be an administrator!")
                return
        except Exception as e:
            print(f"Failed to check bot permissions: {e}")
            return

        if chat_type in ['group', 'supergroup']:
            try:
                user = bot.get_chat_member(chat_id, message.from_user.id)
                if user.status not in ['creator', 'administrator']:
                    bot.send_message(chat_id, "❌ Only administrators can enable stock updates!")
                    return
            except Exception as e:
                print(f"Failed to check user permissions: {e}")
                bot.send_message(chat_id, "❌ Error checking administrator rights!")
                return

        # Try sending a test message
        try:
            test_msg = "🔄 Checking message sending permissions..."
            sent = bot.send_message(chat_id, test_msg)
            bot.delete_message(chat_id, sent.message_id)
        except Exception as e:
            print(f"Failed to send test message: {e}")
            bot.send_message(chat_id, "❌ Failed to send test message. Check bot permissions!")
            return

        # Enable updates for the chat
        enabled_groups[chat_id] = {'last_message': None}
        save_state()
        
        success_msg = "✅ Stock updates will now be sent automatically every 5 minutes!"
        bot.send_message(chat_id, success_msg)
        
        # Send first update
        if get_stock_data():
            current_time = datetime.now()
            message = format_stock_message(current_time)
            send_message_with_retry(chat_id, message)
        else:
            bot.send_message(chat_id, "❌ Failed to get stock data. Next attempt in 5 minutes.")

    except Exception as e:
        print(f"Error in enablegroupstock for {chat_id}: {e}")
        bot.send_message(chat_id, "❌ An error occurred! Please try again later or contact administrator.")

@bot.message_handler(commands=['disablegroupstock'])
def disablegroupstock_command(message):
    chat_id = message.chat.id
    chat_type = message.chat.type
    
    try:
        if chat_type not in ['group', 'supergroup', 'channel']:
            bot.send_message(chat_id, "❌ This command only works in groups and channels!")
            return

        if chat_id not in enabled_groups:
            bot.send_message(chat_id, "❌ Stock updates are not enabled in this chat!")
            return

        if chat_type in ['group', 'supergroup']:
            try:
                user = bot.get_chat_member(chat_id, message.from_user.id)
                if user.status not in ['creator', 'administrator']:
                    bot.send_message(chat_id, "❌ Only administrators can disable stock updates!")
                    return
            except Exception as e:
                print(f"Failed to check user permissions: {e}")
                bot.send_message(chat_id, "❌ Error checking administrator rights!")
                return

        # Delete last stock message if it exists
        if isinstance(enabled_groups[chat_id], dict) and 'last_message' in enabled_groups[chat_id]:
            try:
                bot.delete_message(chat_id, enabled_groups[chat_id]['last_message'])
            except:
                pass

        # Disable updates for the chat
        del enabled_groups[chat_id]
        save_state()
        bot.send_message(chat_id, "✅ Stock updates have been disabled!")

    except Exception as e:
        print(f"Error in disablegroupstock for {chat_id}: {e}")
        bot.send_message(chat_id, "❌ An error occurred! Please try again later or contact administrator.")

# Add channel post handler for commands
# Add channel post handler for commands
@bot.channel_post_handler(commands=['enablegroupstock', 'disablegroupstock', 'checkitem', 'uncheckitem', 'check', 'checkweath'])
def channel_command_handler(message):
    try:
        # For channels, we work directly with chat_id
        chat_id = message.chat.id
        command = message.text.split()[0].lower()
        
        if len(message.text.split()) > 1:
            args = message.text.split(maxsplit=1)[1]
        else:
            args = None

        if command == '/checkweath':
            # Включить отслеживание погоды для этого канала
            if chat_id not in weather_watch_channels:
                weather_watch_channels.add(chat_id)
                save_weather_watch_state()
                bot.send_message(chat_id, "✅ Теперь этот канал будет получать уведомления о погоде и ивентах!")
            else:
                bot.send_message(chat_id, "✋ Этот канал уже отслеживает погоду!")
            return

        if command == '/enablegroupstock':
            # Create a simplified message object for the handler
            message.from_user = None
            enablegroupstock_command(message)
        elif command == '/disablegroupstock':
            # For channels, we don't need to check admin rights
            if chat_id not in enabled_groups:
                bot.send_message(chat_id, "❌ Stock updates are not enabled in this channel!")
                return
            
            # Delete last stock message if it exists
            if isinstance(enabled_groups[chat_id], dict) and 'last_message' in enabled_groups[chat_id]:
                try:
                    bot.delete_message(chat_id, enabled_groups[chat_id]['last_message'])
                except:
                    pass

            del enabled_groups[chat_id]
            save_state()
            bot.send_message(chat_id, "✅ Stock updates have been disabled!")

        elif command == '/checkitem':
            if not args:
                bot.send_message(chat_id, "❌ Please specify the item name after the command!")
                return
                
            item_name = args.strip()
            
            if chat_id not in watched_items:
                watched_items[chat_id] = {}
            
            # Check if it's an egg item by checking the -egg suffix
            is_egg = item_name.endswith('-egg')
            
            # Store item with metadata
            watched_items[chat_id][item_name] = {
                'quantity': None,
                'is_egg': is_egg
            }
            save_state()
            
            # Prepare notification message with update interval
            update_interval = "30 минут" if is_egg else "5 минут"
            msg = (f"✅ Канал будет получать уведомления о появлении предмета '{item_name.rstrip('-egg')}' в стоке!\n" +
                  f"🕒 Обновление каждые {update_interval}")
            
            bot.send_message(chat_id, msg)

        elif command == '/uncheckitem':
            if not args:
                bot.send_message(chat_id, "❌ Please specify the item name after the command!")
                return
                
            item_name = args.strip()
            
            if chat_id not in watched_items or item_name not in watched_items[chat_id]:
                bot.send_message(chat_id, f"❌ Канал не отслеживает предмет '{item_name}'!")
                return
            
            del watched_items[chat_id][item_name]
            if not watched_items[chat_id]:
                del watched_items[chat_id]
            
            save_state()
            bot.send_message(chat_id, f"✅ Канал больше не будет получать уведомления о предмете '{item_name}'!")

        elif command == '/check':
            current_time = datetime.now()
            if get_stock_data():
                response = format_stock_message(current_time)
                send_message_with_retry(chat_id, response)
            else:
                bot.send_message(chat_id, "❌ Error: Could not fetch stock data. Please try again later.")
                
    except Exception as e:
        print(f"Error in channel command handler: {e}")
        bot.send_message(chat_id, "❌ An error occurred while processing the command.")

ALERT_ADMINS = {'@Ymler_clarity', '@Vilitraika', '@DikoreT_Garant'}

@bot.message_handler(commands=['doalertbot'])
def doalertbot_command(message):
    user = message.from_user
    username = f"@{user.username}" if user.username else None
    if username not in ALERT_ADMINS:
        bot.reply_to(message, "❌ У вас нет прав для этой команды.")
        return
    # Получаем текст после команды
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        bot.reply_to(message, "❌ Укажите текст для рассылки после команды!")
        return
    alert_text = args[1].strip()
    # Рассылка всем пользователям из user_languages
    count = 0
    for user_id in user_languages.keys():
        try:
            bot.send_message(user_id, alert_text)
            count += 1
            time.sleep(0.1)
        except Exception as e:
            print(f"Ошибка отправки алерта пользователю {user_id}: {e}")
    bot.reply_to(message, f"✅ Сообщение отправлено {count} пользователям.")

def run_bot():
    # Load saved state
    load_state()
    
    # Schedule stock updates every 5 minutes
    schedule.every(5).minutes.do(send_stock_updates)
    
    # Start the schedule checker in a separate thread
    schedule_thread = threading.Thread(target=schedule_checker)
    schedule_thread.daemon = True
    schedule_thread.start()
    
    while True:
        try:
            print("Starting bot polling...")
            bot.polling(none_stop=True, timeout=60)
        except requests.exceptions.ReadTimeout:
            print("Timeout occurred, restarting polling...")
            time.sleep(5)
            continue
        except requests.exceptions.ConnectionError:
            print("Connection error occurred, waiting 15 seconds before retry...")
            time.sleep(15)
            continue
        except Exception as e:
            print(f"Unexpected error occurred: {e}")
            time.sleep(15)
            continue

if __name__ == '__main__':
    run_bot()


