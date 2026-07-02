import os, json, logging, random
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from datetime import datetime
from pathlib import Path

app = Flask(__name__)
CORS(app)

BOT_TOKEN     = os.environ.get("BOT_TOKEN", "YOUR_TOKEN_HERE")
CHAT_ID       = os.environ.get("CHAT_ID", "YOUR_CHAT_ID")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://ваш-дашборд.com")
API_SECRET    = os.environ.get("API_SECRET", "as_secret_2026")  # вставь свой секрет в Railway

# Railway хранит файл в /tmp (сбрасывается при рестарте)
# Для постоянного хранения подключи Railway Volume или Postgres
DATA_FILE = Path("/tmp/dash_data.json")

logging.basicConfig(level=logging.INFO)

# ── ХРАНИЛИЩЕ ────────────────────────────────────────────────

def load_db():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {"months": {}, "privl": [], "privl_total": {}}

def save_db(db):
    DATA_FILE.write_text(json.dumps(db, ensure_ascii=False))

# ── AUTH ─────────────────────────────────────────────────────

def check_auth():
    secret = request.headers.get("X-API-Secret") or request.args.get("secret")
    return secret == API_SECRET

# ── ВСЁ ЧТО БЫЛО В ТВОЁМ ФАЙЛЕ ──────────────────────────────

def send_message(text, parse_mode="HTML", chat_id=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id or CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False
    }
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
        diff = d.get("total", 0) - prev.get("total", 0)
        diff_pct = round(diff / prev.get("total", 1) * 100)
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
    now     = datetime.now().strftime("%d.%m.%Y %H:%M")

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

    trend = ""
    if prev:
        diff  = total - prev.get("total", 0)
        trend = f" {'📈 +' if diff >= 0 else '📉 '}{diff} vs пред. мес."

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

FRAUD_RANTS = [
    "{name}, это Валера. {fraud} фрод-активаций — {pct}%! Ты вообще партнёров проверяешь?! 🚨",
    "Слушай, {name}! Валера смотрит: {fraud} фродов. Это не случайность — это халтура! Разберись! 😡",
    "{name}! {fraud} фродов ({pct}%) — Валера так не договаривался! Объяснений жду сегодня! 🔴",
    "Валера открыл твой раздел, {name}. {fraud} фрод-активаций. Валера закрыл. Валера снова открыл. Всё ещё {fraud}. 😶 Объясняй! 📋",
]

DROP_RANTS = [
    "{name}! Было {prev}, стало {cur}. Минус {diff} активаций — {pct}% падения! Валера слушает объяснения! 📉",
    "Валера смотрит на тебя, {name}. Куда делись {diff} активаций? Партнёры разбежались? Отвечай! 🤔",
    "{name}, падение на {pct}% — это не рабочий момент, это провал! Валера ждёт план восстановления! 📋",
    "Слушай {name}, {diff} активаций потеряли! Валера хочет знать — почему и что делаем! 😤",
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
    "💚 И пока Валера отдыхает — отдельный респект <b>{name}</b>!\n{acts} активаций — это машина продаж! 🚀",
    "💚 Кстати, <b>{name}</b> снова топ!\n{acts} активаций — Фёдор доволен! 👏",
    "💚 <b>{name}</b> показывает как надо!\n{acts} активаций — берите пример! 🏆",
]

def build_fraud_callout(tp_list, threshold_pct=15, threshold_abs=10):
    """Вызов за фрод"""
    offenders = [t for t in tp_list
                 if t.get('fraud', 0) >= threshold_abs and t.get('fraud_pct', 0) >= threshold_pct]
    if not offenders:
        return None

    lines = [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"<b>{random.choice(TRANSFORM_PHRASES)}</b>",
        "",
        "🚨 <b>ФРОД — РАЗБОР ПОЛЁТОВ</b>",
        "",
    ]
    for tp in sorted(offenders, key=lambda x: -x.get('fraud_pct', 0)):
        name = tp['name'].split('(')[0].strip()
        rant = random.choice(FRAUD_RANTS).format(
            name=name,
            fraud=tp['fraud'],
            pct=tp['fraud_pct']
        )
        lines.append(f"👆 {rant}")
        lines.append("")

    lines.append("Фёдор ждёт объяснений в личке. ⏰")
    return "\n".join(lines)


def build_drop_callout(tp_list_cur, tp_list_prev, threshold_pct=20):
    """Вызов за падение активаций"""
    if not tp_list_prev:
        return None

    prev_map = {t['name']: t['acts'] for t in tp_list_prev}
    offenders = []
    for t in tp_list_cur:
        prev = prev_map.get(t['name'], 0)
        if prev == 0:
            continue
        diff = t['acts'] - prev
        drop_pct = round(abs(diff) / max(prev, 1) * 100)
        if diff < 0 and drop_pct >= threshold_pct:
            offenders.append({**t, 'prev': prev, 'diff': abs(diff), 'drop_pct': drop_pct})

    if not offenders:
        return None

    lines = [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"<b>{random.choice(TRANSFORM_PHRASES)}</b>",
        "",
        "📉 <b>ПАДЕНИЕ АКТИВАЦИЙ — РАЗБОР ПОЛЁТОВ</b>",
        "",
    ]
    for tp in sorted(offenders, key=lambda x: -x['drop_pct']):
        name = tp['name'].split('(')[0].strip()
        rant = random.choice(DROP_RANTS).format(
            name=name,
            diff=tp['diff'],
            pct=tp['drop_pct'],
            prev=tp['prev'],
            cur=tp['acts']
        )
        lines.append(f"👆 {rant}")
        lines.append("")

    lines.append("Фёдор ждёт план восстановления. 📋")
    return "\n".join(lines)


def build_privl_callout(tp_list, privl, threshold_count=3, threshold_uniq=2):
    """Вызов за мало привлечений"""
    privl_map = {p['n']: p for p in privl}

    # Берём только тех у кого есть данные о привлечении
    offenders = []
    for t in tp_list:
        name = t['name'].split('(')[0].strip()
        # Ищем в privl_map по частичному совпадению имени
        pm = None
        for k, v in privl_map.items():
            if name.split()[0] in k or k.split()[0] in name:
                pm = v
                break
        if pm is None:
            continue
        if pm['v'] <= threshold_count or pm['u'] <= threshold_uniq:
            offenders.append({**t, 'privl': pm})

    if not offenders:
        return None

    lines = [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"<b>{random.choice(TRANSFORM_PHRASES)}</b>",
        "",
        "👥 <b>МАЛО ПРИВЛЕЧЕНИЙ — РАЗБОР ПОЛЁТОВ</b>",
        "",
    ]
    for tp in offenders[:5]:
        name = tp['name'].split('(')[0].strip()
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


def build_public_praise(tp_list, has_bad=False):
    """Похвала топа — или Валера в отпуске если всё хорошо"""
    if not tp_list:
        return None
    top = tp_list[0]
    name = top['name'].split('(')[0].strip()
    praise = random.choice(PRAISE_PUBLIC).format(name=name, acts=top['acts'])
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

# ── ENDPOINTS ─────────────────────────────────────────────────

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
    today   = datetime.now()
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

        eff = calc_efficiency(months[cur_key], db.get("privl", []))

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
        privl   = db.get("privl", [])

        fraud_msg = build_fraud_callout(tp_cur)
        drop_msg  = build_drop_callout(tp_cur, tp_prev)
        privl_msg = build_privl_callout(tp_cur, privl)
        has_bad   = any([fraud_msg, drop_msg, privl_msg])
        praise_msg = build_public_praise(tp_cur, has_bad=has_bad)
        callouts = [fraud_msg, drop_msg, privl_msg, praise_msg]
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
