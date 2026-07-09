import os, json, logging, random, calendar
from zoneinfo import ZoneInfo
import uuid
import fcntl

# Railway крутится в UTC, а вся команда — по Москве. Чтобы метки времени
# в базе (чек-ины, разборы, отчёты) не съезжали на 3 часа, везде вместо
# datetime.now() используем эту функцию. Возвращает наивный datetime
# (без tzinfo) — специально, чтобы не ломать сравнения со старыми
# метками, которые уже сохранены в базе без часового пояса.
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
def now_msk():
    from datetime import datetime as _dt
    return _dt.now(MOSCOW_TZ).replace(tzinfo=None)
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
CORS(app)

BOT_TOKEN     = os.environ.get("BOT_TOKEN", "YOUR_TOKEN_HERE")
CHAT_ID       = os.environ.get("CHAT_ID", "YOUR_CHAT_ID")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://ваш-дашборд.com")
API_SECRET    = os.environ.get("API_SECRET", "as_secret_2026")  # вставь свой секрет в Railway
OWNER_ID      = os.environ.get("OWNER_ID", "6251390433")  # кому пересылать сообщения торговых и сводки

# Кто исключён из отчёта бота (но остаётся в дашборде)
EXCLUDED_FROM_REPORT = ["Бузина Яна"]

# Через сколько часов после разбора Валеры слать напоминание, если ТП не ответил
REMINDER_HOURS = float(os.environ.get("REMINDER_HOURS", "2"))

# Во сколько часов (по времени сервера) слать запрос геолокации агентам, через запятую
CHECKIN_HOURS = os.environ.get("CHECKIN_HOURS", "10,13,16,18")
# Через сколько минут после запроса считать чек-ин пропущенным
CHECKIN_TIMEOUT_MIN = int(os.environ.get("CHECKIN_TIMEOUT_MIN", "30"))
# Во сколько часов присылать владельцу итоговый свод за день (после последнего
# времени чек-ина + запас, чтобы все успели ответить)
DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "20"))

# Railway Volume — постоянное хранилище, подключено на /data.
# Если вдруг volume не смонтирован (например, локальный запуск), откатываемся на /tmp.
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _probe = DATA_DIR / ".write_probe"
    _probe.write_text("ok")
    _probe.unlink()
except Exception:
    logging.warning(f"/data недоступен ({DATA_DIR}), откатываюсь на /tmp — данные будут теряться при рестарте!")
    DATA_DIR = Path("/tmp")

DATA_FILE = DATA_DIR / "dash_data.json"
_LOCK_FILE = DATA_DIR / "dash_data.lock"

class db_lock:
    """Блокировка на файле — гарантирует, что два входящих запроса от
    Telegram (например, локация и почти сразу следом фото) не будут
    одновременно читать-менять-писать базу и не затрут изменения друг
    друга. Без этого гонка могла тихо стирать только что сохранённые
    точки чек-ина или очередь ожидания фото."""
    def __enter__(self):
        self._f = open(_LOCK_FILE, "w")
        fcntl.flock(self._f, fcntl.LOCK_EX)
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()

logging.basicConfig(level=logging.INFO)

# ── ХРАНИЛИЩЕ ────────────────────────────────────────────────

def load_db():
    db = {"months": {}, "privl": [], "privl_total": {},
          "pending_calls": {}, "tp_contacts": {}, "answered_calls": [],
          "checkins": {}, "checkin_requests": {}}
    if DATA_FILE.exists():
        try:
            db.update(json.loads(DATA_FILE.read_text()))
        except Exception:
            pass
    db.setdefault("pending_calls", {})
    db.setdefault("tp_contacts", {})
    db.setdefault("answered_calls", [])
    db.setdefault("checkins", {})          # {date: {tp_name: [{lat, lon, ts}, ...]}}
    db.setdefault("checkin_requests", {})  # {date: {tp_name: {"sent_at": iso, "responded": bool}}}
    db.setdefault("awaiting_photo", {})    # {user_id: [{"date":, "name":, "idx":}, ...]} — очередь ждущих фото
    db.setdefault("territories", {})       # {tp_name: [city1, city2, ...]} — закреплённые города
    db.setdefault("territory_requests", {})  # {request_id: {"tp_name":, "city":, "status":}}
    db.setdefault("inactive_tp", [])       # уволенные — скрыты из бота/территорий, но статистика остаётся
    db.setdefault("vacations", {})          # {tp_name: [{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, ...]}
    db.setdefault("tp_name_codes", {})     # {short_code: full_tp_name} — для callback_data кнопок
    return db

def save_db(db):
    DATA_FILE.write_text(json.dumps(db, ensure_ascii=False))

# ── AUTH ─────────────────────────────────────────────────────

def check_auth():
    secret = request.headers.get("X-API-Secret") or request.args.get("secret")
    return secret == API_SECRET

# ── СОПОСТАВЛЕНИЕ ИМЁН ТП ↔ TELEGRAM ────────────────────────

def clean_tp_name(name):
    """Убирает пометки типа '(Торговый New)' из имени ТП"""
    return name.split("(")[0].strip()

def match_tp_name(query_name, tp_keys):
    """Ищет имя ТП (query_name — то, что пришло из Telegram) среди списка
    полных имён ТП (tp_keys), той же логикой частичного совпадения,
    что уже используется в build_privl_callout."""
    query_name = (query_name or "").strip()
    if not query_name:
        return None
    q_first = query_name.split()[0].lower() if query_name.split() else ""
    for k in tp_keys:
        k_clean = clean_tp_name(k)
        k_first = k_clean.split()[0].lower() if k_clean.split() else ""
        if not q_first or not k_first:
            continue
        if q_first == k_first or q_first in k_clean.lower() or k_first in query_name.lower():
            return k
    return None

# ── ЧЕК-ИНЫ ГЕОЛОКАЦИИ ───────────────────────────────────────

import hashlib

def get_all_tp_names(db, include_inactive=False):
    """Список всех ТП за последний загруженный месяц — используем как
    источник истины 'кто вообще есть в команде', а не только тех, кого
    когда-либо вызывали на разбор. По умолчанию не включает уволенных
    (см. db['inactive_tp']) — их статистика по активациям никуда не
    девается, но в регистрации бота и территориях они больше не нужны."""
    months = db.get("months", {})
    if not months:
        return []
    last_key = sorted(months.keys())[-1]
    tp_list = months[last_key].get("tp", [])
    names = [t["name"] for t in tp_list]
    if not include_inactive:
        inactive = set(db.get("inactive_tp", []))
        names = [n for n in names if n not in inactive]
    return names

def tp_name_code(name):
    """Короткий стабильный код имени ТП — Telegram callback_data ограничен
    64 байтами, а полные кириллические ФИО с пометками в него не влезают."""
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:12]

def today_key():
    return now_msk().strftime("%Y-%m-%d")

def is_on_vacation(db, tp_name, date_str=None):
    """Проверяет, находится ли ТП в отпуске на указанную дату (по умолчанию
    сегодня) — по графику, заданному владельцем в '🏖️ Отпуска'."""
    date_str = date_str or today_key()
    ranges = db.get("vacations", {}).get(tp_name, [])
    for r in ranges:
        if r.get("start", "") <= date_str <= r.get("end", ""):
            return True
    return False

def send_registration_keyboard(chat_id, db):
    """Присылает агенту список ТП кнопками — чтобы он один раз явно
    указал, кто он, вместо ненадёжного автоматического угадывания по имени."""
    all_names = get_all_tp_names(db)
    if not all_names:
        send_message(
            "Пока не могу показать список — нет загруженных данных по ТП. Напишите руководителю.",
            chat_id=chat_id
        )
        return

    # Сохраняем код -> полное имя, чтобы потом расшифровать выбор
    code_map = db.setdefault("tp_name_codes", {})
    for name in all_names:
        code_map[tp_name_code(name)] = name
    save_db(db)

    buttons = [[{"text": clean_tp_name(name), "callback_data": f"reg:{tp_name_code(name)}"}] for name in all_names]
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "👋 Привет! Выбери своё имя из списка, чтобы бот знал, кто ты — это нужно для чек-инов по геолокации и разборов Валеры.",
        "reply_markup": {"inline_keyboard": buttons}
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        res = r.json()
        if not res.get("ok"):
            logging.error(f"send_registration_keyboard: Telegram отклонил запрос: {res}")
    except Exception as e:
        logging.error(f"send_registration_keyboard error: {e}")

def answer_callback_query(callback_query_id, text=""):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_query_id, "text": text}, timeout=10)
    except Exception as e:
        logging.error(f"answer_callback_query error: {e}")

def handle_registration_callback(callback_query):
    """Обрабатывает выбор агентом своего имени из списка — сохраняет
    железную привязку chat_id ↔ имя ТП, без угадывания."""
    data_str = callback_query.get("data", "")
    from_user = callback_query.get("from", {})
    user_id = from_user.get("id", "")
    username = from_user.get("username", "")
    cq_id = callback_query.get("id", "")

    if not data_str.startswith("reg:"):
        return
    code = data_str[len("reg:"):]

    db = load_db()
    tp_name = db.get("tp_name_codes", {}).get(code)
    if not tp_name:
        answer_callback_query(cq_id, "Список устарел, напиши /register ещё раз")
        return

    db.setdefault("tp_contacts", {})[tp_name] = {"id": user_id, "username": username}
    save_db(db)

    answer_callback_query(cq_id, "Готово!")
    send_message(
        f"✅ Записал: ты — <b>{clean_tp_name(tp_name)}</b>.\n\n"
        f"Теперь будешь получать запросы на чек-ин и разборы Валеры сюда.",
        chat_id=user_id
    )
    logging.info(f"Agent registered: {tp_name} -> {user_id}")

def send_location_keyboard(chat_id):
    """Просит агента отправить геолокацию — одна кнопка, без набора текста"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "📍 Валера просит отметиться — жми кнопку ниже, а следом пришли фото с точки (обязательно)",
        "reply_markup": {
            "keyboard": [[{"text": "📍 Отправить локацию", "request_location": True}]],
            "resize_keyboard": True,
            "one_time_keyboard": True
        }
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        res = r.json()
        if not res.get("ok"):
            logging.error(f"send_location_keyboard: Telegram отклонил запрос: {res}")
    except Exception as e:
        logging.error(f"send_location_keyboard error: {e}")

def request_all_checkins():
    """Рассылает запрос геолокации всем ТП, чей chat_id уже известен
    (агент хотя бы раз писал боту). Помечает отправку, чтобы позже
    можно было увидеть, кто не ответил."""
    db = load_db()
    contacts = db.get("tp_contacts", {})
    if not contacts:
        logging.info("request_all_checkins: нет известных контактов ТП")
        return 0

    date_key = today_key()
    reqs = db.setdefault("checkin_requests", {}).setdefault(date_key, {})
    now_iso = now_msk().isoformat()
    sent = 0
    for tp_name, contact in contacts.items():
        if is_on_vacation(db, tp_name, date_key):
            continue  # в отпуске — не дёргаем
        chat_id = contact.get("id")
        if not chat_id:
            continue
        send_location_keyboard(chat_id)
        reqs[tp_name] = {"sent_at": now_iso, "responded": False}
        sent += 1
    save_db(db)
    logging.info(f"Checkin requests sent to {sent} agents")
    return sent

def tp_name_for_user(user_id, db):
    """Ищет уже зарегистрированное (через явный выбор кнопкой) имя ТП по
    user_id — это авторитетный источник истины, в отличие от угадывания
    по тексту имени в Telegram-профиле (которое может быть на другом
    языке/в другом формате, чем ФИО в отчётах)."""
    contacts = db.get("tp_contacts", {})
    for name, contact in contacts.items():
        if str(contact.get("id", "")) == str(user_id):
            return name
    return None

YANDEX_GEOCODER_KEY = os.environ.get("YANDEX_GEOCODER_KEY", "3f602451-361b-4835-b80b-f2e0602fe739")

def _reverse_geocode_yandex(lat, lon, kind=None):
    """Обратное геокодирование через Яндекс — точнее для российских
    адресов, особенно внутри Москвы (собственные детальные карты).
    Важно: у Яндекса координаты в запросе идут в порядке lon,lat,
    в отличие от большинства других сервисов.
    kind='district' явно просит именно район/округ, а не ближайший
    адресный объект — иначе для точек у шоссе/эстакад Яндекс отдаёт
    саму трассу вместо жилого района."""
    try:
        params = {
            "apikey": YANDEX_GEOCODER_KEY,
            "format": "json",
            "geocode": f"{lon},{lat}",
            "results": 1,
        }
        if kind:
            params["kind"] = kind
        r = requests.get("https://geocode-maps.yandex.ru/1.x/", params=params, timeout=10)
        data = r.json()
        members = data.get("response", {}).get("GeoObjectCollection", {}).get("featureMember", [])
        if not members:
            return []
        components = members[0]["GeoObject"]["metaDataProperty"]["GeocoderMetaData"]["Address"]["Components"]
        by_kind = {}
        for c in components:
            k, name = c.get("kind"), c.get("name")
            if k and name:
                by_kind.setdefault(k, name)
        # Логируем сырые компоненты — если снова получим только "Москва",
        # в логах Railway будет видно ВСЁ, что реально прислал Яндекс,
        # вместо того чтобы гадать заново вслепую.
        logging.info(f"Yandex geocode raw components ({lat},{lon}, kind={kind}): {by_kind}")
        # От точного к общему: район → округ/area → город → область
        return [by_kind[k] for k in ("district", "area", "locality", "province") if k in by_kind]
    except Exception as e:
        logging.error(f"_reverse_geocode_yandex error: {e}")
        return []

def _reverse_geocode_osm(lat, lon, zoom=14):
    """Бесплатный обратный геокодер OpenStreetMap Nominatim — резервный
    вариант, если у Яндекса не получилось (сбой, лимит и т.п.). Возвращает
    набор возможных названий места, от самого точного к общему."""
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": zoom, "addressdetails": 1},
            headers={"User-Agent": "AS-Partner-Dashboard/1.0"},
            timeout=10
        )
        addr = r.json().get("address", {})
        candidates = [
            addr.get("city_district"), addr.get("suburb"), addr.get("borough"),
            addr.get("town"), addr.get("village"),
            addr.get("municipality"), addr.get("county"), addr.get("city"),
        ]
        return [c for c in candidates if c]
    except Exception as e:
        logging.error(f"_reverse_geocode_osm error: {e}")
        return []

def reverse_geocode_location(lat, lon, zoom=14):
    """Определяет место чек-ина — сначала пробуем геокодер Яндекса (точнее
    для российских адресов). Если он вернул только голое название города
    (например, у шоссе/эстакады — ближайший адресный объект это трасса,
    а не жилой район) — повторяем запрос, явно попросив район. Если и
    это не дало ничего конкретнее — дополнительно пробуем бесплатный
    OpenStreetMap, вдруг у него есть более детальные данные для этой
    точки. Возвращаем самый детальный результат из всех попыток."""
    candidates = _reverse_geocode_yandex(lat, lon)
    is_locality_only = len(candidates) == 1  # только город/область, без района/округа

    if is_locality_only:
        precise = _reverse_geocode_yandex(lat, lon, kind="district")
        if precise:
            candidates = precise
            is_locality_only = len(candidates) == 1

    if is_locality_only or not candidates:
        # Яндекс не смог (или дал только общее) — пробуем OSM как ещё один шанс
        osm_candidates = _reverse_geocode_osm(lat, lon, zoom=zoom)
        logging.info(f"OSM fallback candidates ({lat},{lon}): {osm_candidates}")
        if len(osm_candidates) > len(candidates):
            candidates = osm_candidates

    logging.info(f"Финальные кандидаты геокодинга ({lat},{lon}): {candidates}")
    return candidates

MOSCOW_OKRUGS = {
    "цао": "центральный административный округ",
    "сао": "северный административный округ",
    "свао": "северо-восточный административный округ",
    "вао": "восточный административный округ",
    "ювао": "юго-восточный административный округ",
    "юао": "южный административный округ",
    "юзао": "юго-западный административный округ",
    "зао": "западный административный округ",
    "сзао": "северо-западный административный округ",
    "зелао": "зеленоградский административный округ",
    "нао": "новомосковский административный округ",
    "тао": "троицкий административный округ",
}

def expand_okrug_aliases(names):
    """«ЗАО» и «Западный административный округ» — одно и то же место,
    но текстово не совпадают. Разворачиваем сокращения в обе стороны,
    чтобы сравнение видело эту связь."""
    expanded = set(names)
    for n in names:
        if n in MOSCOW_OKRUGS:
            expanded.add(MOSCOW_OKRUGS[n])
        for abbr, full in MOSCOW_OKRUGS.items():
            if n == full:
                expanded.add(abbr)
    return expanded

def send_territory_request_button(chat_id, request_id, place):
    """Кнопка «это моя территория» — агент может сам попросить закрепить
    за собой место, где его засекли вне зоны. Решение всё равно за
    руководителем (см. handle_territory_approval_callback)."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": f"Если «{place}» — правда твоя территория, можешь попросить руководителя закрепить её за тобой:",
        "reply_markup": {"inline_keyboard": [[
            {"text": "📍 Запросить закрепление территории", "callback_data": f"terrreq:{request_id}"}
        ]]}
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"send_territory_request_button error: {e}")

def handle_territory_request_callback(callback_query):
    """Агент нажал «запросить закрепление» — пересылаем владельцу с
    кнопками решения, ничего не меняем в территориях без его ответа."""
    data_str = callback_query.get("data", "")
    cq_id = callback_query.get("id", "")
    from_user = callback_query.get("from", {})
    user_id = from_user.get("id", "")
    request_id = data_str[len("terrreq:"):]

    db = load_db()
    req = db.get("territory_requests", {}).get(request_id)
    if not req or req.get("status") != "pending":
        answer_callback_query(cq_id, "Запрос уже обработан или устарел")
        return

    tp_name = req["tp_name"]
    city = req["city"]
    req["status"] = "awaiting_approval"
    save_db(db)

    answer_callback_query(cq_id, "Отправил руководителю на согласование")
    send_message(f"📨 Запрос по «{city}» отправлен руководителю, ждём решения.", chat_id=user_id)

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": OWNER_ID,
        "text": f"🗺️ <b>{clean_tp_name(tp_name)}</b> просит закрепить за собой «{city}» — добавить в его территорию?",
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [[
            {"text": "✅ Одобрить", "callback_data": f"terrapp:{request_id}:yes"},
            {"text": "❌ Отклонить", "callback_data": f"terrapp:{request_id}:no"}
        ]]}
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"handle_territory_request_callback owner notify error: {e}")

def handle_territory_approval_callback(callback_query):
    """Только владелец может одобрить/отклонить — при одобрении город
    сразу добавляется в территорию агента на дашборде."""
    data_str = callback_query.get("data", "")
    cq_id = callback_query.get("id", "")
    from_user = callback_query.get("from", {})
    approver_id = from_user.get("id", "")

    parts = data_str.split(":")
    if len(parts) != 3:
        return
    _, request_id, decision = parts

    if str(approver_id) != str(OWNER_ID):
        answer_callback_query(cq_id, "Решение по территориям принимает только руководитель")
        return

    db = load_db()
    req = db.get("territory_requests", {}).get(request_id)
    if not req:
        answer_callback_query(cq_id, "Запрос не найден (возможно, устарел)")
        return

    tp_name = req["tp_name"]
    city = req["city"]
    contact = db.get("tp_contacts", {}).get(tp_name)
    agent_chat_id = contact.get("id") if contact else None

    if decision == "yes":
        territories = db.setdefault("territories", {})
        lst = territories.setdefault(tp_name, [])
        if city not in lst:
            lst.append(city)
        req["status"] = "approved"
        answer_callback_query(cq_id, "Добавлено в территорию!")
        if agent_chat_id:
            send_message(f"✅ Руководитель подтвердил: «{city}» теперь твоя закреплённая территория.", chat_id=agent_chat_id)
    else:
        req["status"] = "rejected"
        answer_callback_query(cq_id, "Отклонено")
        if agent_chat_id:
            send_message(f"❌ Руководитель не подтвердил «{city}» как твою территорию.", chat_id=agent_chat_id)

    save_db(db)

def resolve_and_check_territory(db, tp_name, lat, lon, contact_user_id):
    """Определяет место чек-ина и, если агенту назначена территория,
    проверяет попадание по ЛЮБОМУ из кандидатов названий (район/город/
    округ). Возвращает (display_place, status), где status один из:
    'ok' — всё в порядке или территория не назначена;
    'violation' — реально вне закреплённой территории;
    'unknown' — геокодер не смог определить место даже со второй
    попытки; это СЕРАЯ ЗОНА — не считается ничьей виной, никому не
    прилетает никаких сообщений, дашборд просто покажет точку серым."""
    candidates = reverse_geocode_location(lat, lon, zoom=14)
    is_bare_moscow = len(candidates) == 1 and candidates[0].strip().lower() == "москва"

    display_place = candidates[0] if candidates else None

    if is_bare_moscow:
        # Серая зона: ни разбора, ни уведомлений — ни владельцу, ни агенту.
        # Дашборд отметит эту точку серым, чтобы было видно, что тут просто
        # не хватило данных, а не что-то скрывается.
        return display_place, "unknown"

    assigned = db.get("territories", {}).get(tp_name, [])
    if not candidates or not assigned:
        return display_place, "ok"  # территория не назначена — сверять не с чем, значит всё ок

    candidates_norm = expand_okrug_aliases([c.strip().lower() for c in candidates])
    assigned_norm = expand_okrug_aliases([c.strip().lower() for c in assigned if c.strip()])
    matches = any(
        cn == an or cn in an or an in cn
        for cn in candidates_norm for an in assigned_norm
    )
    if matches:
        return display_place, "ok"

    register_pending_calls(db, {"territory": [tp_name]})
    if contact_user_id:
        combined_text = (
            f"📍 Валера заметил, что ты в районе «{display_place}» — это не твоя закреплённая территория.\n\n"
            f"• Если это разовая ситуация (заезжал по делам, менял точку и т.п.) — "
            f"напиши объяснение прямо сюда, текстом, сейчас же.\n\n"
            f"• Если «{display_place}» на самом деле должна быть твоей постоянной территорией — "
            f"нажми кнопку ниже, чтобы запросить её закрепление у руководителя."
        )
        request_id = uuid.uuid4().hex[:10]
        db.setdefault("territory_requests", {})[request_id] = {
            "tp_name": tp_name, "city": display_place, "status": "pending"
        }
        reply_markup = {"inline_keyboard": [[
            {"text": "📍 Запросить закрепление территории", "callback_data": f"terrreq:{request_id}"}
        ]]}
        send_message(combined_text, chat_id=contact_user_id, reply_markup=reply_markup)
    return display_place, "violation"

def handle_checkin(msg, db):
    """Обрабатывает входящее сообщение с геолокацией от агента.
    Фото теперь обязательно — чек-ин считается завершённым только после
    того, как следом придёт фото (см. handle_checkin_photo)."""
    from_user = msg.get("from", {})
    user_id   = from_user.get("id", "")
    user_name = (from_user.get("first_name", "") + " " + from_user.get("last_name", "")).strip()
    loc       = msg["location"]
    date_key  = today_key()
    now_iso   = now_msk().isoformat()

    # Сначала — уже зарегистрированная явная привязка по user_id (надёжно).
    # Только если агент почему-то ещё не регистрировался — пробуем угадать
    # по имени из Telegram-профиля, как раньше.
    all_tp_names = get_all_tp_names(db)
    matched_key = tp_name_for_user(user_id, db) or match_tp_name(user_name, all_tp_names) or user_name

    point = {"lat": loc["latitude"], "lon": loc["longitude"], "ts": now_iso, "photo_file_id": None}

    # Проверяем территорию — не бросает исключение наружу, если геокодер недоступен
    city, territory_status = resolve_and_check_territory(db, matched_key, loc["latitude"], loc["longitude"], user_id)
    point["resolved_city"] = city
    point["territory_status"] = territory_status  # "ok" | "violation" | "unknown"
    point["outside_territory"] = (territory_status == "violation")  # для обратной совместимости с дашбордом

    day_points = db.setdefault("checkins", {}).setdefault(date_key, {})
    day_points.setdefault(matched_key, []).append(point)
    point_idx = len(day_points[matched_key]) - 1

    # Ждём фото следующим сообщением — чек-ин пока не считается отвеченным.
    # Очередь (список), а не одна запись — если агент отметится второй раз
    # до того, как пришлёт фото за первый, обе отметки не потеряются.
    db.setdefault("awaiting_photo", {}).setdefault(str(user_id), []).append({
        "date": date_key, "name": matched_key, "idx": point_idx
    })

    save_db(db)
    send_message(
        "📍 Локация принята! Теперь пришли, пожалуйста, фото с этой точки 📷 — без него чек-ин не считается выполненным.",
        chat_id=user_id,
        reply_markup={"remove_keyboard": True}
    )
    logging.info(f"Checkin location saved for {matched_key}, ждём фото")

def handle_checkin_photo(msg, db):
    """Обрабатывает фото, присланное следом за геолокацией — довершает чек-ин.
    Если у агента накопилось несколько ожидающих фото (отметился не раз
    подряд) — фото уходит на самую раннюю из них (FIFO), по порядку."""
    from_user = msg.get("from", {})
    user_id = str(from_user.get("id", ""))
    awaiting = db.get("awaiting_photo", {})
    queue = awaiting.get(user_id) or []
    if isinstance(queue, dict):
        queue = [queue]  # старый формат до обновления (одна запись, не список) — подстраховка

    if not queue:
        send_message(
            "📷 Фото получил, но сейчас не жду его от тебя — сначала нажми кнопку локации, если это был чек-ин.",
            chat_id=int(user_id) if user_id.isdigit() else user_id
        )
        return

    photo_sizes = msg.get("photo", [])
    if not photo_sizes:
        return
    file_id = photo_sizes[-1]["file_id"]  # последний элемент — самое большое разрешение

    pending_info = queue.pop(0)  # самая ранняя ожидающая отметка
    if not queue:
        del awaiting[user_id]

    date_key = pending_info["date"]
    name     = pending_info["name"]
    idx      = pending_info["idx"]

    try:
        db["checkins"][date_key][name][idx]["photo_file_id"] = file_id
    except (KeyError, IndexError):
        logging.error(f"handle_checkin_photo: не нашёл точку для {name}/{date_key}/{idx}")
        return

    # Только теперь, с фото, чек-ин считается выполненным
    reqs = db.get("checkin_requests", {}).get(date_key, {})
    if name in reqs:
        reqs[name]["responded"] = True

    save_db(db)
    send_message(
        "✅ Фото принято, чек-ин засчитан!",
        chat_id=int(user_id) if user_id.isdigit() else user_id,
        reply_markup={"remove_keyboard": True}
    )
    logging.info(f"Checkin photo saved for {name}")

def send_daily_checkin_summary():
    """Раз в день (после последнего времени чек-ина) — свод владельцу:
    кто сколько отметок с фото реально прошёл за сегодня, из скольких
    ожидалось. Сортировка — сначала худшие, чтобы сразу бросались в глаза."""
    try:
        db = load_db()
        date_key = today_key()
        contacts = db.get("tp_contacts", {})
        if not contacts:
            return  # некому докладывать — никто ещё не зарегистрирован

        checkins = db.get("checkins", {}).get(date_key, {})
        expected = len([h for h in CHECKIN_HOURS.split(",") if h.strip()]) or 1

        rows = []
        for tp_name in contacts.keys():
            points = checkins.get(tp_name, [])
            done = sum(1 for p in points if p.get("photo_file_id"))
            on_vac = is_on_vacation(db, tp_name, date_key)
            rows.append((tp_name, done, on_vac))
        rows.sort(key=lambda x: (x[2], x[1]))  # отпускники в конец, потом худшие сверху

        lines = [f"<b>📋 Итоги чек-инов за {date_key}</b>", ""]
        for tp_name, done, on_vac in rows:
            if on_vac:
                lines.append(f"🏖️ {clean_tp_name(tp_name)}: в отпуске")
                continue
            icon = "✅" if done >= expected else ("⚠️" if done > 0 else "❌")
            lines.append(f"{icon} {clean_tp_name(tp_name)}: {done}/{expected}")

        send_message("\n".join(lines), chat_id=OWNER_ID)
        logging.info(f"Daily checkin summary sent for {date_key}")
    except Exception as e:
        logging.error(f"send_daily_checkin_summary error: {e}")

def check_missed_checkins():
    """Раз в N минут смотрит, кто не ответил на сегодняшний запрос
    геолокации дольше CHECKIN_TIMEOUT_MIN, и один раз уведомляет владельца.
    Отдельно отмечает тех, кто прислал локацию, но забыл фото."""
    try:
        db = load_db()
        date_key = today_key()
        reqs = db.get("checkin_requests", {}).get(date_key, {})
        if not reqs:
            return
        now = now_msk()
        changed = False
        missed_none  = []  # вообще не ответили
        missed_photo = []  # локация есть, фото нет
        awaiting = db.get("awaiting_photo", {})
        names_awaiting_photo = {
            item["name"]
            for queue in awaiting.values()
            for item in (queue if isinstance(queue, list) else [queue])
            if item.get("date") == date_key
        }

        for tp_name, info in reqs.items():
            if info.get("responded") or info.get("missed_notified"):
                continue
            try:
                sent_at = datetime.fromisoformat(info["sent_at"])
            except Exception:
                continue
            if now - sent_at < timedelta(minutes=CHECKIN_TIMEOUT_MIN):
                continue
            display = clean_tp_name(tp_name)
            if tp_name in names_awaiting_photo:
                missed_photo.append(display)
            else:
                missed_none.append(display)
            info["missed_notified"] = True
            changed = True

        if missed_none or missed_photo:
            lines = ["⏰ <b>Не отметились по геолокации:</b>", ""]
            if missed_none:
                lines += [f"👤 {name}" for name in missed_none]
            if missed_photo:
                lines += ["", "📷 <i>Прислали локацию, но не прислали фото:</i>"]
                lines += [f"👤 {name}" for name in missed_photo]
            send_message("\n".join(lines), chat_id=OWNER_ID)
        if changed:
            save_db(db)
    except Exception as e:
        logging.error(f"check_missed_checkins error: {e}")

# ── ВСЁ ЧТО БЫЛО В ТВОЁМ ФАЙЛЕ ──────────────────────────────

def send_message(text, parse_mode="HTML", chat_id=None, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id or CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    r = requests.post(url, json=payload, timeout=10)
    return r.json()

def grade_emoji(grade):
    return {"S": "🏆", "A": "🔥", "B": "👍", "C": "⚠️"}.get(grade, "")

def fraud_warning(fraud_pct):
    if fraud_pct > 30: return "🚨🚨 КРИТИЧНО"
    if fraud_pct > 15: return "🚨 Высокий"
    if fraud_pct > 5:  return "⚠️ Есть"
    return None

def get_praise(rank, grade, name):
    first_name = name.split()[0]
    if rank == 1:
        phrases = [
            f"🏆 {first_name} снова №1 — машина продаж!",
            f"🚀 {first_name} рвёт всех — абсолютный топ месяца!",
            f"💎 {first_name} на вершине — держи темп, чемпион!"
        ]
    elif rank == 2:
        phrases = [
            f"🥈 {first_name} — крепкий второй! До первого рукой подать 💪",
            f"🥈 {first_name} дышит в спину лидеру — так держать!"
        ]
    elif rank == 3:
        phrases = [
            f"🥉 {first_name} — бронза! В тройке лучших — уже круто 👏",
            f"🥉 {first_name} в топ-3 — отличный результат!"
        ]
    elif grade == "A":
        phrases = [f"🔥 {first_name} — огонь месяца! Растёшь!"]
    else:
        return None
    return random.choice(phrases)

def build_insights(data):
    d = data["current"]
    prev = data.get("prev")
    insights = []
    if prev:
        forecast = d.get("forecast")
        prev_total = prev.get("total", 0)
        if forecast and prev_total:
            # Сравниваем прогноз на месяц с прошлым месяцем — не сырой факт
            # на сегодня (что всегда выглядит как обвал в начале месяца).
            diff = forecast - prev_total
            diff_pct = round(diff / prev_total * 100)
            if diff < 0:
                insights.append(f"📉 По прогнозу активации упадут на {abs(diff)} ({abs(diff_pct)}%) к прошлому месяцу — нужен разбор причин")
            elif diff > 0:
                insights.append(f"📈 По прогнозу активации вырастут на {diff} (+{diff_pct}%) к прошлому месяцу — команда прибавляет!")
        elif prev_total:
            # Нет данных для прогноза (нет last_date) — сравниваем как есть, редкий случай
            diff = d.get("total", 0) - prev_total
            diff_pct = round(diff / prev_total * 100)
            if diff < 0:
                insights.append(f"📉 Активации упали на {abs(diff)} ({abs(diff_pct)}%) — нужен разбор причин")
            elif diff > 0:
                insights.append(f"📈 Активации выросли на {diff} (+{diff_pct}%) — команда прибавила!")
        fraud_now  = d.get("fraud_pct", 0)
        fraud_prev = prev.get("fraud_pct", 0)
        if fraud_now > fraud_prev + 5:
            insights.append(f"⚠️ Фрод вырос с {fraud_prev}% до {fraud_now}% — срочно разобраться!")
        elif fraud_now < fraud_prev - 3:
            insights.append(f"✅ Фрод снизился с {fraud_prev}% до {fraud_now}% — хорошая работа!")
    tp_list = d.get("tp", [])
    if tp_list:
        top = tp_list[0]
        top_name  = top["name"].split("(")[0].strip().split()[0]
        top_share = round(top["acts"] / d.get("total", 1) * 100)
        if top_share > 40:
            insights.append(f"⚡ {top_name} даёт {top_share}% всех активаций — высокая зависимость от одного ТП")
    return insights

def build_report(data, month_label):
    d       = data["current"]
    prev    = data.get("prev")
    tp_list = d.get("tp", [])
    now     = now_msk().strftime("%d.%m.%Y %H:%M")

    lines = [
        f"<b>📊 АС — Отчёт за {month_label}</b>",
        f"<i>Обновлено: {now}</i>", "",
    ]

    total     = d.get("total", 0)
    ap        = d.get("ap", 0)
    p3        = d.get("p3", 0)
    p10       = d.get("p10", 0)
    fraud     = d.get("fraud", 0)
    fraud_pct = d.get("fraud_pct", 0)
    forecast  = d.get("forecast")
    last_date = d.get("last_date", "")

    trend = ""
    if prev:
        diff  = total - prev.get("total", 0)
        trend = f" {'📈 +' if diff >= 0 else '📉 '}{diff} vs пред. мес."

    # Прогноз
    forecast_str = ""
    if forecast and last_date:
        day = int(last_date.split("-")[2]) if last_date else 0
        pct = round(total/forecast*100) if forecast else 0
        forecast_str = f"\n📈 <b>Прогноз на месяц:</b> {forecast:,} акт. (факт по {day}-е — {pct}% выполнения)".replace(",", " ")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        f"⚡ <b>Активаций:</b> {total:,}{trend}".replace(",", " "),
        f"👥 <b>Партнёров активных:</b> {ap}",
        f"🟢 <b>3+ продаж:</b> {p3}  |  🥇 <b>10+ продаж:</b> {p10}",
    ]

    fw = fraud_warning(fraud_pct)
    if fw:
        lines.append(f"🚨 <b>Фрод:</b> {fraud} активаций ({fraud_pct}%) — {fw}")
    else:
        lines.append(f"✅ <b>Фрод:</b> {fraud} акт. ({fraud_pct}%) — в норме")

    # Топ с похвалами
    lines += ["", "━━━━━━━━━━━━━━━━━━━━", "🏅 <b>ТОП торговых:</b>", ""]
    medals  = ["🥇", "🥈", "🥉"]
    eff     = data.get("efficiency", [])
    eff_map = {e["name"].split("(")[0].strip(): e for e in eff}

    for i, tp in enumerate(tp_list[:5]):
        name   = tp["name"].split("(")[0].strip()
        fp     = tp.get("fraud_pct", 0)
        fnote  = f" 🚨{fp}%" if fp > 10 else ""
        medal  = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} <b>{name}</b> — {tp['acts']} акт. | {tp['partners']} партн.{fnote}")
        eff_data = eff_map.get(name)
        grade    = eff_data["grade"] if eff_data else ("A" if i == 0 else "B")
        praise   = get_praise(i + 1, grade, name)
        if praise:
            lines.append(f"   <i>{praise}</i>")

    # Фрод
    fraud_tp = sorted([t for t in tp_list if t.get("fraud", 0) > 0],
                      key=lambda x: -x.get("fraud_pct", 0))
    if fraud_tp:
        lines += ["", "━━━━━━━━━━━━━━━━━━━━", "🚨 <b>Внимание — фрод у ТП:</b>", ""]
        for tp in fraud_tp[:3]:
            name = tp["name"].split("(")[0].strip()
            lines.append(f"• <b>{tp['fraud']}</b> фрод ({tp['fraud_pct']}%) → {name}")

    # Эффективность
    if eff:
        lines += ["", "━━━━━━━━━━━━━━━━━━━━", "⭐ <b>Рейтинг эффективности:</b>", ""]
        for item in eff[:5]:
            name = item["name"].split("(")[0].strip()
            lines.append(f"{grade_emoji(item['grade'])} <b>{item['grade']}</b> {name} — {item['score']} очков")

    # Выводы
    insights = build_insights(data)
    if insights:
        lines += ["", "━━━━━━━━━━━━━━━━━━━━", "💡 <b>Выводы:</b>", ""]
        for ins in insights:
            lines.append(ins)

    if DASHBOARD_URL and "ваш-дашборд" not in DASHBOARD_URL:
        lines += ["", "━━━━━━━━━━━━━━━━━━━━",
                  f"📱 <a href='{DASHBOARD_URL}'>Открыть полный дашборд →</a>"]

    return "\n".join(lines)

def build_personal_report(tp_data, month_label, rank):
    name      = tp_data["name"].split("(")[0].strip()
    grade     = tp_data.get("grade", "B")
    score     = tp_data.get("score", 0)
    acts      = tp_data["acts"]
    partners  = tp_data["partners"]
    p3        = tp_data.get("p3", 0)
    p10       = tp_data.get("p10", 0)
    fraud     = tp_data.get("fraud", 0)
    fraud_pct = tp_data.get("fraud_pct", 0)
    phrases = {
        "S": ["🏆 Машина продаж! Держи темп!", "🚀 Ты ракета этого месяца!", "💎 Абсолютный топ команды!"],
        "A": ["🔥 Горишь! Так держать!", "💪 Крепкий результат, продолжай!", "📈 Растёшь — это видно!"],
        "B": ["👍 Стабильно, но есть куда расти", "🎯 Ровный темп, давай прибавим?", "📊 Хорошая база, нужен рывок"],
        "C": ["⚠️ Нужно прибавить, поговорим?", "🔧 Есть над чем поработать", "💬 Давай разберём что мешает"]
    }
    phrase = random.choice(phrases.get(grade, phrases["B"]))
    lines = [
        f"<b>👋 {name}, отчёт за {month_label}</b>", "",
        phrase, "",
        f"📍 Место в рейтинге: <b>#{rank}</b>  |  Оценка: <b>{grade} ({score} очков)</b>", "",
        f"⚡ Активаций: <b>{acts}</b>",
        f"👥 Партнёров: <b>{partners}</b>  (3+: {p3} | 10+: {p10})",
        f"🚨 Фрод: <b>{fraud}</b> акт. ({fraud_pct}%) — обрати внимание!" if fraud > 0 else "✅ Фрода нет — чисто!",
    ]
    if DASHBOARD_URL and "ваш-дашборд" not in DASHBOARD_URL:
        lines += ["", f"📱 <a href='{DASHBOARD_URL}'>Полный дашборд</a>"]
    return "\n".join(lines)

# ── РАЗБОР ПОЛЁТОВ ───────────────────────────────────────────

# ── ФЁДОР И ВАЛЕРА ───────────────────────────────────────────

TRANSFORM_PHRASES = [
    "😤 Фёдор прочитал отчёт...\n🔄 Фёдор трансформируется...\n💥 <b>ВАЛЕРА АКТИВИРОВАН!</b>",
    "😒 Фёдор посмотрел на цифры...\n⚡ Внутри что-то щёлкнуло...\n🦁 <b>ВАЛЕРА ВЫШЕЛ НА ОХОТУ!</b>",
    "🖊️ Фёдор достал красную ручку...\n🌀 Началась трансформация...\n🔥 <b>ВАЛЕРА УЖЕ ЗДЕСЬ!</b>",
    "📊 Фёдор изучил таблицу...\n😡 Брови сдвинулись...\n💢 <b>ВАЛЕРА БЕРЁТ СЛОВО!</b>",
]

VALERA_INTRO = [
    "Привет, это Валера. Настало моё время. 😈",
    "Валера на связи. Поговорим? 😤",
    "Это Валера. Я прочитал. Мне есть что сказать. 🗣️",
    "Валера здесь. И Валера не в восторге. 😠",
]

def mention_for(name, contacts):
    """Возвращает кликабельное упоминание агента в Telegram, если известен
    его chat_id (после того как он один раз написал боту/прошёл регистрацию).
    Приоритет: @username (Telegram сам подсвечивает такой текст как ссылку),
    иначе — упоминание по id (работает, даже если у агента нет юзернейма)."""
    display = clean_tp_name(name)
    contact = contacts.get(name)
    if not contact:
        return display
    username = contact.get("username")
    uid = contact.get("id")
    if username:
        return f"{display} (@{username})"
    if uid:
        return f'<a href="tg://user?id={uid}">{display}</a>'
    return display

FRAUD_RANTS = [
    "{name}, это Валера. {fraud} фрод-активаций — {pct}%! Ты вообще партнёров проверяешь?! 🚨",
    "Слушай, {name}! Валера смотрит: {fraud} фродов. Это не случайность — это халтура! Разберись! 😡",
    "{name}! {fraud} фродов ({pct}%) — Валера так не договаривался! Объяснений жду сегодня! 🔴",
    "Валера открыл твой раздел, {name}. {fraud} фрод-активаций. Валера закрыл. Валера снова открыл. Всё ещё {fraud}. 😶 Объясняй! 📋",
]

DROP_RANTS = [
    "{name}! Прошлый месяц было {prev}, при текущем темпе прогноз на этот — {cur}. Минус {diff} активаций ({pct}%)! Валера слушает объяснения! 📉",
    "Валера смотрит на тебя, {name}. По прогнозу выйдешь на {cur} вместо {prev} в прошлом месяце — минус {diff}. Партнёры разбежались? Отвечай! 🤔",
    "{name}, по текущему темпу падение на {pct}% к прошлому месяцу — это не рабочий момент, это провал! Валера ждёт план восстановления! 📋",
    "Слушай {name}, при таком темпе к концу месяца потеряем {diff} активаций к прошлому! Валера хочет знать — почему и что делаем! 😤",
]

PRIVL_RANTS = [
    "{name}! Валера смотрит на привлечение: {count} всего, уникальных {uniq}. Где новые партнёры?! 😤",
    "{name}, {count} привлечений за месяц — это серьёзно мало! Валера ожидал большего от тебя! 📊",
    "Слушай {name}, {uniq} уникальных — клиентская база не растёт! Валера не доволен! 🔻",
    "{name}! Одни субдилеры! {uniq} уникальных из {count}. Валера хочет видеть новые лица в базе! 👥",
]

VALERA_VACATION = [
    "😌 Фёдор изучил отчёт...\n✅ Цифры в норме...\n🏖️ <b>Валера сегодня в отпуске!</b>\n\nВсё чисто, команда! Фёдор доволен. Так держать! 💚",
    "😊 Фёдор проверил данные...\n🎉 Нарушений нет...\n🌴 <b>Валера отдыхает — заслужили!</b>\n\nОтличная работа, команда! Фёдор аплодирует! 👏",
    "🧐 Фёдор всё проверил...\n✨ Чисто, как слеза...\n🏄 <b>Валера на пляже, его сегодня не ждите!</b>\n\nТак держать! Фёдор гордится командой! 🏆",
]

PRAISE_PUBLIC = [
    "💚 И пока Валера отдыхает — отдельный респект <b>{name}</b>!\nПо прогнозу на месяц {acts} активаций — это машина продаж! 🚀",
    "💚 Кстати, <b>{name}</b> снова топ по прогнозу месяца!\n{acts} акт. — Фёдор доволен! 👏",
    "💚 <b>{name}</b> показывает как надо!\nПрогноз {acts} активаций — берите пример! 🏆",
]

def build_forecast_callout(d, dp, show_transform=True):
    """Вызов/похвала на основе прогноза. Возвращает (текст, была_ли_трансформация)."""
    forecast = d.get("forecast")
    last_date = d.get("last_date", "")
    total = d.get("total", 0)
    
    if not forecast or not last_date:
        return None, False
    
    day = int(last_date.split("-")[2]) if last_date else 0
    pct = round(total/forecast*100) if forecast else 0
    
    prev_total = dp.get("total", 0) if dp else 0
    
    lines = ["", "━━━━━━━━━━━━━━━━━━━━"]
    is_bad = False
    
    if prev_total and forecast >= prev_total * 1.1:
        # Прогноз превышает прошлый месяц на 10%+
        growth = round((forecast - prev_total) / prev_total * 100)
        lines += [
            f"🚀 <b>ПРОГНОЗ МЕСЯЦА</b>",
            "",
            f"📈 При текущем темпе выйдем на <b>{forecast:,} активаций</b>".replace(",", " "),
            f"Это <b>+{growth}%</b> к прошлому месяцу ({prev_total:,})!".replace(",", " "),
            f"Данные по {day}-е числу — {pct}% выполнения.",
            "",
            "💪 Команда жжёт! Фёдор доволен. Держим темп!",
        ]
    elif prev_total and forecast < prev_total * 0.9:
        # Прогноз ниже прошлого месяца на 10%+
        is_bad = True
        drop = round((prev_total - forecast) / prev_total * 100)
        if show_transform:
            lines += [f"{random.choice(TRANSFORM_PHRASES)}", "", f"<i>{random.choice(VALERA_INTRO)}</i>", ""]
        lines += [
            f"📉 <b>ПРОГНОЗ НИЖЕ ПРОШЛОГО МЕСЯЦА</b>",
            "",
            f"При текущем темпе выйдем на <b>{forecast:,} акт.</b>".replace(",", " "),
            f"Прошлый месяц был <b>{prev_total:,}</b> — падение на <b>{drop}%</b>!".replace(",", " "),
            f"Данные по {day}-е числу — осталось {100-pct}% месяца.",
            "",
            "Валера ждёт объяснений и план действий! 📋",
        ]
    elif pct < 40 and day > 15:
        # После середины месяца выполнено меньше 40%
        is_bad = True
        if show_transform:
            lines += [f"{random.choice(TRANSFORM_PHRASES)}", "", f"<i>{random.choice(VALERA_INTRO)}</i>", ""]
        lines += [
            f"⚠️ <b>ТЕМП СЛАБЫЙ</b>",
            "",
            f"По {day}-е числу факт <b>{total:,} акт.</b> — только {pct}% от прогноза.".replace(",", " "),
            f"Прогноз на месяц: <b>{forecast:,} акт.</b>".replace(",", " "),
            "",
            "Валера смотрит и хмурится. Нужно прибавить! 💢",
        ]
    else:
        # Всё нормально
        lines += [
            f"📊 <b>ПРОГНОЗ МЕСЯЦА</b>",
            "",
            f"При текущем темпе: <b>{forecast:,} акт.</b>".replace(",", " "),
            f"Данные по {day}-е числу — {pct}% выполнения.",
            "Фёдор следит за динамикой 👀",
        ]
    
    return "\n".join(lines), is_bad


def get_fraud_offenders(tp_list, threshold_pct=15, threshold_abs=10):
    return [t for t in tp_list
            if t.get('fraud', 0) >= threshold_abs and t.get('fraud_pct', 0) >= threshold_pct
            and not any(ex in t["name"] for ex in EXCLUDED_FROM_REPORT)]

def build_fraud_callout(tp_list, threshold_pct=15, threshold_abs=10, show_transform=True, contacts=None):
    """Вызов за фрод"""
    contacts = contacts or {}
    offenders = get_fraud_offenders(tp_list, threshold_pct, threshold_abs)
    if not offenders:
        return None

    lines = ["", "━━━━━━━━━━━━━━━━━━━━"]
    if show_transform:
        lines += [f"<b>{random.choice(TRANSFORM_PHRASES)}</b>", ""]
    lines += [
        "🚨 <b>ФРОД — РАЗБОР ПОЛЁТОВ</b>",
        "",
    ]
    for tp in sorted(offenders, key=lambda x: -x.get('fraud_pct', 0)):
        name = mention_for(tp['name'], contacts)
        rant = random.choice(FRAUD_RANTS).format(
            name=name,
            fraud=tp['fraud'],
            pct=tp['fraud_pct']
        )
        lines.append(f"👆 {rant}")
        lines.append("")

    lines.append("Фёдор ждёт объяснений в личке. ⏰")
    return "\n".join(lines)


def get_drop_offenders(tp_list_cur, tp_list_prev, last_date=None, days_in_month=None, threshold_pct=20):
    if not tp_list_prev:
        return []
    day = int(last_date.split("-")[2]) if last_date else None
    prev_map = {t['name']: t['acts'] for t in tp_list_prev}
    offenders = []
    for t in tp_list_cur:
        if any(ex in t["name"] for ex in EXCLUDED_FROM_REPORT):
            continue
        prev = prev_map.get(t['name'], 0)
        if prev == 0:
            continue
        # Сравниваем не "факт на сегодня" с "фактом за весь прошлый месяц" (это всегда
        # выглядит как обвал в начале месяца), а прогнозируемый темп ТП на весь месяц —
        # тем же способом, каким считается общий прогноз по компании.
        if day and days_in_month and day > 0:
            forecast_acts = round(t['acts'] / day * days_in_month)
        else:
            forecast_acts = t['acts']
        diff = forecast_acts - prev
        drop_pct = round(abs(diff) / max(prev, 1) * 100)
        if diff < 0 and drop_pct >= threshold_pct:
            offenders.append({**t, 'prev': prev, 'diff': abs(diff), 'drop_pct': drop_pct, 'forecast_acts': forecast_acts})
    return offenders

def build_drop_callout(tp_list_cur, tp_list_prev, last_date=None, days_in_month=None, threshold_pct=20, show_transform=True, contacts=None):
    """Вызов за падение активаций (по прогнозу темпа, не по сырому факту)"""
    contacts = contacts or {}
    offenders = get_drop_offenders(tp_list_cur, tp_list_prev, last_date, days_in_month, threshold_pct)
    if not offenders:
        return None

    lines = ["", "━━━━━━━━━━━━━━━━━━━━"]
    if show_transform:
        lines += [f"<b>{random.choice(TRANSFORM_PHRASES)}</b>", ""]
    lines += [
        "📉 <b>ПАДЕНИЕ АКТИВАЦИЙ (ПО ПРОГНОЗУ) — РАЗБОР ПОЛЁТОВ</b>",
        "",
    ]
    for tp in sorted(offenders, key=lambda x: -x['drop_pct']):
        name = mention_for(tp['name'], contacts)
        rant = random.choice(DROP_RANTS).format(
            name=name,
            diff=tp['diff'],
            pct=tp['drop_pct'],
            prev=tp['prev'],
            cur=tp['forecast_acts']
        )
        lines.append(f"👆 {rant}")
        lines.append("")

    lines.append("Фёдор ждёт план восстановления. 📋")
    return "\n".join(lines)


def get_privl_offenders(tp_list, privl, threshold_count=3, threshold_uniq=2):
    privl_map = {p['n']: p for p in privl}
    offenders = []
    for t in tp_list:
        if any(ex in t["name"] for ex in EXCLUDED_FROM_REPORT):
            continue
        name = t['name'].split('(')[0].strip()
        pm = None
        for k, v in privl_map.items():
            if name.split()[0] in k or k.split()[0] in name:
                pm = v
                break
        if pm is None:
            pm = {'v': 0, 'u': 0}  # нет записей вообще — значит буквально ноль привлечений
        if pm['v'] <= threshold_count or pm['u'] <= threshold_uniq:
            offenders.append({**t, 'privl': pm})
    return offenders

def build_privl_callout(tp_list, privl, threshold_count=3, threshold_uniq=2, show_transform=True, contacts=None):
    """Вызов за мало привлечений"""
    contacts = contacts or {}
    offenders = get_privl_offenders(tp_list, privl, threshold_count, threshold_uniq)
    if not offenders:
        return None

    lines = ["", "━━━━━━━━━━━━━━━━━━━━"]
    if show_transform:
        lines += [f"<b>{random.choice(TRANSFORM_PHRASES)}</b>", ""]
    lines += [
        "👥 <b>МАЛО ПРИВЛЕЧЕНИЙ — РАЗБОР ПОЛЁТОВ</b>",
        "",
    ]
    for tp in sorted(offenders, key=lambda x: (x['privl']['v'], x['privl']['u']))[:5]:
        name = mention_for(tp['name'], contacts)
        pm = tp['privl']
        rant = random.choice(PRIVL_RANTS).format(
            name=name,
            count=pm['v'],
            uniq=pm['u']
        )
        lines.append(f"👆 {rant}")
        lines.append("")

    lines.append("Фёдор ждёт новых партнёров в базе. 📲")
    return "\n".join(lines)


def build_public_praise(tp_list, has_bad=False, last_date=None, days_in_month=None, contacts=None):
    """Похвала топа — по прогнозируемому темпу на месяц (не по сырому факту на сегодня),
    и в сравнении с остальными ТП. Или Валера в отпуске, если всё хорошо."""
    contacts = contacts or {}
    candidates = [t for t in tp_list if not any(ex in t["name"] for ex in EXCLUDED_FROM_REPORT)]
    if not candidates:
        return None

    day = int(last_date.split("-")[2]) if last_date else None
    def forecast_for(t):
        if day and days_in_month and day > 0:
            return round(t['acts'] / day * days_in_month)
        return t['acts']

    top = max(candidates, key=forecast_for)
    top_forecast = forecast_for(top)
    name = mention_for(top['name'], contacts)
    praise = random.choice(PRAISE_PUBLIC).format(name=name, acts=top_forecast)
    if not has_bad:
        vacation = random.choice(VALERA_VACATION)
        return f"\n━━━━━━━━━━━━━━━━━━━━\n{vacation}\n\n{praise}"
    else:
        return f"\n━━━━━━━━━━━━━━━━━━━━\n💚 <b>НО ЕСТЬ И ХОРОШИЕ НОВОСТИ!</b>\n\n{praise}\nФёдор гордится! 💪"


def calc_efficiency(d, privl):
    privl_map  = {p["n"]: p for p in privl}
    tp_list    = d.get("tp", [])
    if not tp_list:
        return []
    max_acts   = max((t["acts"] for t in tp_list), default=1)
    max_p3     = max((t["p3"]   for t in tp_list), default=1)
    max_privl  = max((privl_map.get(t["name"], {}).get("u", 0) for t in tp_list), default=1)
    max_privl  = max(max_privl, 1)  # защита от деления на ноль
    result = []
    for t in tp_list:
        pm    = privl_map.get(t["name"], {})
        score = max(0, round(
            (t["acts"]/max_acts)*40 +
            (t["p3"]/max_p3)*25 +
            (pm.get("u", 0)/max_privl)*20 -
            (t.get("fraud_pct", 0))*0.3
        ))
        grade = "S" if score>=75 else "A" if score>=50 else "B" if score>=30 else "C"
        result.append({**t, "score": score, "grade": grade})
    return sorted(result, key=lambda x: -x["score"])

# ── ОТСЛЕЖИВАНИЕ ОТВЕТОВ НА РАЗБОР ВАЛЕРЫ ───────────────────

CALLOUT_REASON_TEXT = {
    "fraud": "высокий процент фрода",
    "drop": "заметное падение активаций по сравнению с прошлым месяцем",
    "privl": "мало привлечённых партнёров в этом месяце",
    "territory": "чек-ин вне закреплённой территории",
}

def send_personal_callouts(db, offenders_by_reason):
    """Кроме общего поста в группе с тегами — личное сообщение каждому
    нарушителю в директ (если известен его chat_id), с перечислением
    всех поводов разом, если их несколько."""
    contacts = db.get("tp_contacts", {})
    reasons_by_name = {}
    for reason, names in offenders_by_reason.items():
        for name in names:
            reasons_by_name.setdefault(name, []).append(reason)

    for name, reasons in reasons_by_name.items():
        contact = contacts.get(name)
        if not contact or not contact.get("id"):
            continue  # не зарегистрирован в боте — личку послать некому
        reason_lines = "\n".join(f"• {CALLOUT_REASON_TEXT.get(r, r)}" for r in reasons)
        send_message(
            f"⚠️ Валера вызывает тебя на разбор:\n\n{reason_lines}\n\n"
            f"Напиши, пожалуйста, объяснение прямо сюда.",
            chat_id=contact["id"]
        )

def register_pending_calls(db, offenders_by_reason):
    """offenders_by_reason: {reason_code: [tp_name, ...]}
    Добавляет новых нарушителей в db['pending_calls'], не трогая тех,
    кто уже там ждёт ответа (чтобы не сбрасывать таймер напоминания)."""
    now_iso = now_msk().isoformat()
    pending = db.setdefault("pending_calls", {})
    for reason, names in offenders_by_reason.items():
        for name in names:
            entry = pending.get(name)
            if entry is None:
                pending[name] = {"since": now_iso, "reasons": [reason], "reminded": False}
            elif reason not in entry.get("reasons", []):
                entry["reasons"].append(reason)

REASON_SUMMARY_HEADERS = {
    "fraud": "🚨 ФРОД",
    "drop": "📉 ПАДЕНИЕ АКТИВАЦИЙ",
    "privl": "👥 МАЛО ПРИВЛЕЧЕНИЙ",
}

def build_answers_summary(db):
    """Собирает свод ответов, когда все вызванные на разбор ответили —
    сгруппированный по причине (сначала все по фроду, потом все по
    падению и т.д.), а не единым плоским списком."""
    answered = db.get("answered_calls", [])
    if not answered:
        return None

    by_reason = {}
    for a in answered:
        for r in (a.get("reasons") or ["other"]):
            by_reason.setdefault(r, []).append(a)

    lines = [
        "<b>📋 Свод ответов по разбору Валеры</b>",
        f"<i>Все {len(answered)} ответили</i>", "",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    # Сначала известные категории по порядку, потом всё остальное
    ordered_reasons = list(REASON_SUMMARY_HEADERS.keys()) + [r for r in by_reason if r not in REASON_SUMMARY_HEADERS]
    for r in ordered_reasons:
        if r not in by_reason:
            continue
        header = REASON_SUMMARY_HEADERS.get(r, r.upper())
        names_list = ", ".join(clean_tp_name(a["name"]) for a in by_reason[r])
        lines.append(f"<b>{header} — замечены: {names_list}</b>")
        lines.append("")
        for a in by_reason[r]:
            lines += [f"👤 {clean_tp_name(a['name'])}", f"💬 {a['text']}", ""]
    return "\n".join(lines)

def check_reminders():
    """Раз в N минут проверяет, кто не ответил Валере дольше REMINDER_HOURS,
    и шлёт напоминание — в личку, если знаем tg id, иначе в группу с тегом по имени."""
    try:
        db = load_db()
        pending = db.get("pending_calls", {})
        if not pending:
            return
        now = now_msk()
        changed = False
        for name, info in pending.items():
            if info.get("reminded"):
                continue
            try:
                since = datetime.fromisoformat(info["since"])
            except Exception:
                continue
            if now - since < timedelta(hours=REMINDER_HOURS):
                continue
            display_name = clean_tp_name(name)
            reasons = info.get("reasons", [])
            reason_lines = "\n".join(f"• {CALLOUT_REASON_TEXT.get(r, r)}" for r in reasons) or "• (причина не указана)"
            contact = db.get("tp_contacts", {}).get(name)
            if contact and contact.get("id"):
                send_message(
                    f"⏰ {display_name.split()[0]}, Валера всё ещё ждёт твой ответ на разбор по пункту(-ам):\n\n"
                    f"{reason_lines}\n\n"
                    f"Напиши объяснение прямо сюда, я передам руководителю.",
                    chat_id=contact["id"]
                )
            else:
                send_message(
                    f"⏰ <b>{display_name}</b> — Валера уже {REMINDER_HOURS:.0f}ч ждёт ответа на разбор "
                    f"по пункту(-ам):\n\n{reason_lines}\n\n"
                    f"а связаться в личку не вышло. Напиши боту @SV_AS_FedorBot напрямую!"
                )
            info["reminded"] = True
            changed = True
        if changed:
            save_db(db)
            logging.info("Reminders sent")
    except Exception as e:
        logging.error(f"check_reminders error: {e}")

def maybe_send_summary(db):
    """Если очередь разборов опустела — шлём владельцу свод и очищаем её"""
    if not db.get("pending_calls") and db.get("answered_calls"):
        summary = build_answers_summary(db)
        if summary:
            send_message(summary, chat_id=OWNER_ID)
            logging.info("Answers summary sent to owner")
        db["answered_calls"] = []

# ── ENDPOINTS ─────────────────────────────────────────────────

@app.route("/tg-webhook", methods=["POST"])
def tg_webhook():
    """Принимает сообщения от пользователей боту и пересылает владельцу"""
    try:
        with db_lock():
            data = request.json

            # Нажатие на inline-кнопку выбора имени при регистрации
            callback_query = data.get("callback_query")
            if callback_query:
                cq_data = callback_query.get("data", "")
                if cq_data.startswith("reg:"):
                    handle_registration_callback(callback_query)
                elif cq_data.startswith("terrreq:"):
                    handle_territory_request_callback(callback_query)
                elif cq_data.startswith("terrapp:"):
                    handle_territory_approval_callback(callback_query)
                return jsonify({"ok": True})

            msg = data.get("message", {})
            if not msg:
                return jsonify({"ok": True})

            # Реагируем ТОЛЬКО на личные сообщения боту.
            # Всё, что происходит в группе (в т.ч. ваши собственные сообщения,
            # обсуждения торговых между собой и т.д.) — полностью игнорируем.
            if msg.get("chat", {}).get("type") != "private":
                return jsonify({"ok": True})

            from_user = msg.get("from", {})
            text = msg.get("text", "")
            user_name = (from_user.get("first_name", "") + " " + from_user.get("last_name", "")).strip()
            username = from_user.get("username", "")
            user_id = from_user.get("id", "")

            # Геолокация (чек-ин) — обрабатываем отдельно и сразу выходим
            if "location" in msg:
                db = load_db()
                handle_checkin(msg, db)
                return jsonify({"ok": True})

            # Фото после чек-ина — тоже отдельно и сразу выходим
            if "photo" in msg:
                db = load_db()
                handle_checkin_photo(msg, db)
                return jsonify({"ok": True})

            if not text or text.startswith("/"):
                # Отвечаем на /start — сразу просим явно выбрать своё имя из списка,
                # вместо ненадёжного угадывания по first name (тёзки, разные написания)
                if text == "/start":
                    db = load_db()
                    send_message(
                        "👋 Привет! Это бот АС — партнёрский дашборд.\n\nЕсли Валера вызвал тебя на разбор — пиши объяснение прямо сюда, я передам руководителю.",
                        chat_id=user_id
                    )
                    send_registration_keyboard(user_id, db)
                elif text == "/whoami" or text == "/register":
                    # На случай, если агент сменил телефон/аккаунт и надо перерегистрироваться
                    db = load_db()
                    send_registration_keyboard(user_id, db)
                return jsonify({"ok": True})

            db = load_db()

            # Сопоставляем с теми, кого ждём на разборе — чтобы закрыть вопрос,
            # даже если человек ответил текстом, а не через кнопку регистрации.
            # ВАЖНО: больше не записываем это угадывание в tp_contacts —
            # регистрация (chat_id ↔ имя) происходит ТОЛЬКО через явный выбор
            # кнопкой при /start (см. handle_registration_callback). Раньше тут
            # было автоматическое угадывание по любому сообщению — из-за него
            # люди попадали в контакты, даже не подтвердив, кто они.
            pending = db.get("pending_calls", {})
            matched_key = match_tp_name(user_name, pending.keys()) if pending else None

            if matched_key and matched_key in pending:
                # Это ответ на разбор Валеры — закрываем вопрос по этому ТП.
                info = pending.pop(matched_key)
                reasons = info.get("reasons", [])

                if "territory" in reasons:
                    # Территория — особый случай: шлём владельцу сразу же,
                    # отдельно от общего свода (не копим в answered_calls,
                    # чтобы не задваивалось при финальной сводке).
                    send_message(
                        f"📍 <b>Ответ по территории:</b>\n\n"
                        f"👤 {clean_tp_name(matched_key)}\n\n"
                        f"💬 {text}",
                        chat_id=OWNER_ID
                    )
                else:
                    db.setdefault("answered_calls", []).append({
                        "name": matched_key,
                        "text": text,
                        "answered_at": now_msk().isoformat(),
                        "reasons": reasons,
                    })

                send_message("✅ Спасибо! Передал руководителю.", chat_id=user_id)
                maybe_send_summary(db)
                save_db(db)
                logging.info(f"Reply to Valera's callout matched to {matched_key}")
                return jsonify({"ok": True})

            # Не связано с конкретным разбором — обычная пересылка владельцу
            save_db(db)
            forward_text = (
                f"📩 <b>Сообщение от торгового:</b>\n\n"
                f"👤 {user_name}"
                + (f" (@{username})" if username else "")
                + f"\n\n💬 {text}"
            )
            send_message(forward_text, chat_id=OWNER_ID)
            send_message("✅ Твоё сообщение получено и передано руководителю!", chat_id=user_id)

            logging.info(f"Message forwarded from {user_name} to owner")
            return jsonify({"ok": True})
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return jsonify({"ok": True})


@app.route("/ping")
def ping():
    db = load_db()
    return jsonify({
        "status": "ok",
        "bot": "АС Партнёрский бот",
        "months_stored": list(db["months"].keys()),
        "privl_count": len(db.get("privl", []))
    })

# Дашборд тянет все данные при открытии
@app.route("/data")
def get_data():
    db = load_db()
    return jsonify(db)

# Дашборд заливает данные активаций за месяц
@app.route("/upload/activations", methods=["POST"])
def upload_activations():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    payload = request.json
    month   = payload.get("month")
    data    = payload.get("data")
    if not month or not data:
        return jsonify({"error": "month and data required"}), 400

    db      = load_db()
    today   = now_msk()
    cur_m   = f"{today.year}-{str(today.month).zfill(2)}"

    if month in db["months"] and month != cur_m:
        return jsonify({"status": "skipped", "reason": "closed month", "month": month})

    db["months"][month] = data
    save_db(db)
    logging.info(f"Saved activations {month}: {data.get('total')} acts, {data.get('fraud')} fraud")
    return jsonify({"status": "ok", "month": month, "total": data.get("total")})

# Дашборд заливает данные привлечения
@app.route("/upload/privl", methods=["POST"])
def upload_privl():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    payload = request.json
    db      = load_db()
    # Новый формат: privl_months: {month: {privl:[...], tot:{...}}}
    privl_months = payload.get("privl_months", {})
    if privl_months:
        if "privl_months" not in db:
            db["privl_months"] = {}
        # Перезаписываем только те месяцы что пришли
        for m, data in privl_months.items():
            db["privl_months"][m] = data
            logging.info(f"Saved privl month {m}: {data.get('tot',{}).get('v',0)} entries")
        # Для совместимости: общий privl = данные последнего месяца
        last_m = sorted(db["privl_months"].keys())[-1]
        db["privl"]       = db["privl_months"][last_m].get("privl", [])
        db["privl_total"] = db["privl_months"][last_m].get("tot", {})
    else:
        # Старый формат — просто сохраняем
        db["privl"]       = payload.get("privl", [])
        db["privl_total"] = payload.get("privl_total", {})
    save_db(db)
    return jsonify({"status": "ok", "months": list(privl_months.keys()) or ["legacy"]})

# Ручной запуск рассылки запроса геолокации (кнопка на дашборде, помимо расписания)
@app.route("/checkin/request", methods=["POST"])
def checkin_request_endpoint():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    sent = request_all_checkins()
    return jsonify({"status": "ok", "sent": sent})

# Дашборд забирает точки чек-инов за дату (YYYY-MM-DD)
@app.route("/checkins/<date>", methods=["GET"])
def get_checkins(date):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    db = load_db()
    points  = db.get("checkins", {}).get(date, {})
    requests_ = db.get("checkin_requests", {}).get(date, {})
    contacts = db.get("tp_contacts", {})
    on_vacation = [n for n in contacts.keys() if is_on_vacation(db, n, date)]
    return jsonify({"date": date, "checkins": points, "requests": requests_, "on_vacation": on_vacation})

# Прокси для фото чек-ина — не отдаём токен бота напрямую в браузер,
# скачиваем файл от имени бота и отдаём уже готовые байты картинки
@app.route("/checkin-photo/<file_id>", methods=["GET"])
def get_checkin_photo(file_id):
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        info = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10
        ).json()
        if not info.get("ok"):
            return jsonify({"error": "Telegram file not found"}), 404
        file_path = info["result"]["file_path"]
        file_resp = requests.get(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}", timeout=15
        )
        return Response(file_resp.content, mimetype="image/jpeg")
    except Exception as e:
        logging.error(f"get_checkin_photo error: {e}")
        return jsonify({"error": "Failed to fetch photo"}), 500

# Территории: закреплённые города по каждому ТП
@app.route("/territories", methods=["GET"])
def get_territories():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    db = load_db()
    return jsonify(db.get("territories", {}))

@app.route("/territories", methods=["POST"])
def set_territories():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    with db_lock():
        db = load_db()
        db["territories"] = data.get("territories", {})
        save_db(db)
    return jsonify({"status": "ok"})

# Контакты ТП — кто реально зарегистрирован в боте (chat_id известен),
# чтобы владелец мог видеть/актуализировать список и убрать ошибочные записи
@app.route("/tp-contacts", methods=["GET"])
def get_tp_contacts():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    db = load_db()
    all_names = get_all_tp_names(db, include_inactive=True)  # тут показываем всех, включая уволенных — чтобы можно было управлять
    contacts = db.get("tp_contacts", {})
    inactive = db.get("inactive_tp", [])
    return jsonify({"all_names": all_names, "contacts": contacts, "inactive": inactive})

@app.route("/tp-inactive", methods=["POST"])
def set_tp_inactive():
    """Помечает торгового уволенным/активным — скрывает из бота и территорий,
    но НЕ трогает статистику по активациям."""
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    name = data.get("name")
    inactive = bool(data.get("inactive"))
    if not name:
        return jsonify({"error": "name required"}), 400
    with db_lock():
        db = load_db()
        lst = db.setdefault("inactive_tp", [])
        if inactive and name not in lst:
            lst.append(name)
        elif not inactive and name in lst:
            lst.remove(name)
        save_db(db)
    return jsonify({"status": "ok"})

@app.route("/tp-contacts/remove", methods=["POST"])
def remove_tp_contact():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    name = data.get("name")
    with db_lock():
        db = load_db()
        contacts = db.get("tp_contacts", {})
        if name in contacts:
            del contacts[name]
            save_db(db)
            return jsonify({"status": "ok"})
    return jsonify({"status": "not_found"}), 404

# Отпуска — график по каждому торговому, чтобы бот не дёргал его
# запросами геолокации и не считал пропуском, пока человек отдыхает
@app.route("/vacations", methods=["GET"])
def get_vacations():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    db = load_db()
    return jsonify(db.get("vacations", {}))

@app.route("/vacations", methods=["POST"])
def set_vacations():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    with db_lock():
        db = load_db()
        db["vacations"] = data.get("vacations", {})
        save_db(db)
    return jsonify({"status": "ok"})

# Триггер отчёта в TG (вызывается дашбордом после загрузки)
@app.route("/send-report", methods=["POST"])
def send_report():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        payload     = request.json or {}

        # Берём данные с сервера
        db          = load_db()
        months      = db.get("months", {})
        if not months:
            return jsonify({"error": "no data on server"}), 400

        sorted_keys = sorted(months.keys())
        cur_key     = sorted_keys[-1]
        prev_key    = sorted_keys[-2] if len(sorted_keys) > 1 else None

        # month_label — всегда берём из реальных данных, не из payload
        def fmt_month(m):
            y, mo = m.split("-")
            names = ["","Янв","Фев","Мар","Апр","Май","Июн","Июл","Авг","Сен","Окт","Ноя","Дек"]
            return names[int(mo)] + " " + y
        month_label = fmt_month(cur_key)

        def privl_for_month(month_key):
            """Привлечение строго за тот же месяц, что и активации в отчёте —
            а не просто 'последний когда-либо залитый месяц привлечения',
            чтобы не смешивать разные месяцы в эффективности/разборе."""
            pm = db.get("privl_months", {}).get(month_key)
            if pm is not None:
                return pm.get("privl", [])
            # Нет помесячных данных за этот месяц — старый формат/фолбэк
            return db.get("privl", [])

        eff = calc_efficiency(months[cur_key], privl_for_month(cur_key))

        data = {
            "current":    months[cur_key],
            "prev":       months[prev_key] if prev_key else None,
            "efficiency": eff[:5]
        }
        text   = build_report(data, month_label)
        result = send_message(text)
        logging.info(f"TG report sent: {result.get('ok')}")

        # Разбор полётов — отдельные сообщения после основного отчёта
        import time
        tp_cur  = months[cur_key].get("tp", [])
        tp_prev = months[prev_key].get("tp", []) if prev_key else []
        privl   = privl_for_month(cur_key)
        contacts = db.get("tp_contacts", {})

        last_date     = months[cur_key].get("last_date", "")
        days_in_month = calendar.monthrange(*[int(x) for x in cur_key.split("-")])[1]

        # Валера трансформируется один раз за весь отчёт — в первом же сообщении,
        # где реально есть плохие новости. Дальше просто продолжает ругаться без
        # повторного "Фёдор трансформируется..." в каждом следующем сообщении.
        forecast_msg, transformed = build_forecast_callout(
            months[cur_key], months[prev_key] if prev_key else None
        )

        fraud_msg = build_fraud_callout(tp_cur, show_transform=not transformed, contacts=contacts)
        if fraud_msg:
            transformed = True

        drop_msg = build_drop_callout(
            tp_cur, tp_prev, last_date=last_date, days_in_month=days_in_month,
            show_transform=not transformed, contacts=contacts
        )
        if drop_msg:
            transformed = True

        privl_msg = build_privl_callout(tp_cur, privl, show_transform=not transformed, contacts=contacts)
        if privl_msg:
            transformed = True

        has_bad = any([fraud_msg, drop_msg, privl_msg])

        # Ставим нарушителей "на разбор" — чтобы отследить, кто ответил Валере
        offenders_by_reason = {
            "fraud": [t["name"] for t in get_fraud_offenders(tp_cur)],
            "drop":  [t["name"] for t in get_drop_offenders(tp_cur, tp_prev, last_date=last_date, days_in_month=days_in_month)],
            "privl": [t["name"] for t in get_privl_offenders(tp_cur, privl)],
        }
        if any(offenders_by_reason.values()):
            db = load_db()
            register_pending_calls(db, offenders_by_reason)
            send_personal_callouts(db, offenders_by_reason)
            save_db(db)
        praise_msg   = build_public_praise(tp_cur, has_bad=has_bad, last_date=last_date, days_in_month=days_in_month, contacts=contacts)
        callouts = [forecast_msg, fraud_msg, drop_msg, privl_msg, praise_msg]
        sent = 0
        for msg in callouts:
            if msg:
                time.sleep(1)
                send_message(msg)
                sent += 1
                logging.info("Callout sent")

        return jsonify({"status": "ok", "tg_ok": result.get("ok"),
                        "month": cur_key, "callouts_sent": sent})
    except Exception as e:
        logging.error(f"send_report error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# Персональный отчёт (опционально)
@app.route("/send-personal", methods=["POST"])
def send_personal():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        data             = request.json
        personal_chat_id = data.get("personal_chat_id")
        if not personal_chat_id:
            return jsonify({"status": "skip", "reason": "no personal_chat_id"})
        text   = build_personal_report(data["tp_data"], data["month_label"], data["rank"])
        result = send_message(text, chat_id=personal_chat_id)
        return jsonify({"status": "ok", "tg_result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def setup_webhook():
    """Регистрируем webhook в Telegram"""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TOKEN_HERE":
        logging.warning("BOT_TOKEN не задан — webhook не регистрирую")
        return
    webhook_url = "https://proud-beauty-production.up.railway.app/tg-webhook"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            json={"url": webhook_url}, timeout=10
        )
        logging.info(f"Webhook set: {r.json()}")
    except Exception as e:
        logging.error(f"setup_webhook error: {e}")

@app.route("/setup-webhook")
def setup_webhook_endpoint():
    """Ручная перерегистрация webhook, на случай если авторегистрация при старте не сработала"""
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    setup_webhook()
    return jsonify({"status": "ok"})

# ── СТАРТ ────────────────────────────────────────────────────
# Выполняется при импорте модуля — то есть и при обычном запуске (`python bot.py`),
# и при запуске через gunicorn (который __main__ не вызывает и раньше пропускал этот шаг,
# из-за чего webhook приходилось регистрировать руками после каждого рестарта).
setup_webhook()

# Railway по умолчанию крутится в UTC — без явного часового пояса
# CHECKIN_HOURS считались бы по серверному времени, а не по Москве
_scheduler = BackgroundScheduler(timezone=MOSCOW_TZ)
_scheduler.add_job(check_reminders, "interval", minutes=15, id="valera_reminders")

# Автоматическая рассылка запроса геолокации в заданные часы (CHECKIN_HOURS)
_scheduler.add_job(
    request_all_checkins, "cron",
    hour=CHECKIN_HOURS, minute=0, id="checkin_requests"
)
# Проверка пропущенных чек-инов
_scheduler.add_job(check_missed_checkins, "interval", minutes=10, id="checkin_missed")
_scheduler.add_job(
    send_daily_checkin_summary, "cron",
    hour=DAILY_SUMMARY_HOUR, minute=0, id="daily_checkin_summary"
)

_scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
