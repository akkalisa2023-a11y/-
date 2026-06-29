import os, json, logging, random
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from datetime import datetime

app = Flask(__name__)
CORS(app)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TOKEN_HERE")
CHAT_ID   = os.environ.get("CHAT_ID", "YOUR_CHAT_ID")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://ваш-дашборд.com")

logging.basicConfig(level=logging.INFO)

def send_message(text, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
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
    """Генерирует похвалу для топ торговых"""
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
    """Генерирует аналитические выводы"""
    d = data["current"]
    prev = data.get("prev")
    insights = []

    if prev:
        diff = d.get("total", 0) - prev.get("total", 0)
        diff_pct = round(diff / prev.get("total", 1) * 100)
        if diff < 0:
            insights.append(f"📉 Активации упали на {abs(diff)} ({abs(diff_pct)}%) vs прошлого месяца — нужен разбор причин")
        elif diff > 0:
            insights.append(f"📈 Активации выросли на {diff} (+{diff_pct}%) — команда прибавила!")

        fraud_now = d.get("fraud_pct", 0)
        fraud_prev = prev.get("fraud_pct", 0)
        if fraud_now > fraud_prev + 5:
            insights.append(f"⚠️ Фрод вырос с {fraud_prev}% до {fraud_now}% — срочно разобраться!")
        elif fraud_now < fraud_prev - 3:
            insights.append(f"✅ Фрод снизился с {fraud_prev}% до {fraud_now}% — хорошая работа!")

    tp_list = d.get("tp", [])
    if tp_list:
        top = tp_list[0]
        top_name = top["name"].split("(")[0].strip().split()[0]
        top_share = round(top["acts"] / d.get("total", 1) * 100)
        if top_share > 40:
            insights.append(f"⚡ {top_name} даёт {top_share}% всех активаций — высокая зависимость от одного ТП")

    return insights

def build_report(data, month_label):
    d = data["current"]
    prev = data.get("prev")
    tp_list = d.get("tp", [])

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    lines = [
        f"<b>📊 АС — Отчёт за {month_label}</b>",
        f"<i>Обновлено: {now}</i>",
        "",
    ]

    total = d.get("total", 0)
    ap    = d.get("ap", 0)
    p3    = d.get("p3", 0)
    p10   = d.get("p10", 0)
    fraud = d.get("fraud", 0)
    fraud_pct = d.get("fraud_pct", 0)

    trend = ""
    if prev:
        diff = total - prev.get("total", 0)
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

    # --- Топ торговых с похвалами ---
    lines += ["", "━━━━━━━━━━━━━━━━━━━━", "🏅 <b>ТОП торговых:</b>", ""]

    medals = ["🥇", "🥈", "🥉"]
    eff = data.get("efficiency", [])
    eff_map = {e["name"].split("(")[0].strip(): e for e in eff}

    for i, tp in enumerate(tp_list[:5]):
        name = tp["name"].split("(")[0].strip()
        acts = tp["acts"]
        partners = tp["partners"]
        fp = tp.get("fraud_pct", 0)
        fraud_note = f" 🚨{fp}%" if fp > 10 else ""
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} <b>{name}</b> — {acts} акт. | {partners} партн.{fraud_note}")

        # Добавляем похвалу для топ-3
        eff_data = eff_map.get(name)
        grade = eff_data["grade"] if eff_data else ("A" if i == 0 else "B")
        praise = get_praise(i + 1, grade, name)
        if praise:
            lines.append(f"   <i>{praise}</i>")

    # --- Антитоп (фрод) ---
    fraud_tp = [t for t in tp_list if t.get("fraud", 0) > 0]
    fraud_tp.sort(key=lambda x: -x.get("fraud_pct", 0))
    if fraud_tp:
        lines += ["", "━━━━━━━━━━━━━━━━━━━━", "🚨 <b>Внимание — фрод у ТП:</b>", ""]
        for tp in fraud_tp[:3]:
            name = tp["name"].split("(")[0].strip()
            lines.append(f"• <b>{tp['fraud']}</b> фрод ({tp['fraud_pct']}%) → {name}")

    # --- Эффективность ---
    if eff:
        lines += ["", "━━━━━━━━━━━━━━━━━━━━", "⭐ <b>Рейтинг эффективности:</b>", ""]
        for item in eff[:5]:
            name = item["name"].split("(")[0].strip()
            grade = item["grade"]
            score = item["score"]
            emoji = grade_emoji(grade)
            lines.append(f"{emoji} <b>{grade}</b> {name} — {score} очков")

    # --- Аналитические выводы ---
    insights = build_insights(data)
    if insights:
        lines += ["", "━━━━━━━━━━━━━━━━━━━━", "💡 <b>Выводы:</b>", ""]
        for ins in insights:
            lines.append(ins)

    # --- Ссылка на дашборд ---
    if DASHBOARD_URL and DASHBOARD_URL != "https://ваш-дашборд.com":
        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            f"📱 <a href='{DASHBOARD_URL}'>Открыть полный дашборд →</a>"
        ]

    return "\n".join(lines)


def build_personal_report(tp_data, month_label, rank):
    name = tp_data["name"].split("(")[0].strip()
    grade = tp_data.get("grade", "B")
    score = tp_data.get("score", 0)
    acts  = tp_data["acts"]
    partners = tp_data["partners"]
    p3   = tp_data.get("p3", 0)
    p10  = tp_data.get("p10", 0)
    fraud = tp_data.get("fraud", 0)
    fraud_pct = tp_data.get("fraud_pct", 0)

    phrases = {
        "S": ["🏆 Машина продаж! Держи темп!", "🚀 Ты ракета этого месяца!", "💎 Абсолютный топ команды!"],
        "A": ["🔥 Горишь! Так держать!", "💪 Крепкий результат, продолжай!", "📈 Растёшь — это видно!"],
        "B": ["👍 Стабильно, но есть куда расти", "🎯 Ровный темп, давай прибавим?", "📊 Хорошая база, нужен рывок"],
        "C": ["⚠️ Нужно прибавить, поговорим?", "🔧 Есть над чем поработать", "💬 Давай разберём что мешает"]
    }
    phrase = random.choice(phrases.get(grade, phrases["B"]))

    lines = [
        f"<b>👋 {name}, отчёт за {month_label}</b>",
        "",
        f"{phrase}",
        "",
        f"📍 Место в рейтинге: <b>#{rank}</b>  |  Оценка: <b>{grade} ({score} очков)</b>",
        "",
        f"⚡ Активаций: <b>{acts}</b>",
        f"👥 Партнёров: <b>{partners}</b>  (3+: {p3} | 10+: {p10})",
    ]
    if fraud > 0:
        lines.append(f"🚨 Фрод: <b>{fraud}</b> акт. ({fraud_pct}%) — обрати внимание!")
    else:
        lines.append("✅ Фрода нет — чисто!")

    if DASHBOARD_URL and DASHBOARD_URL != "https://ваш-дашборд.com":
        lines += ["", f"📱 <a href='{DASHBOARD_URL}'>Полный дашборд</a>"]
    return "\n".join(lines)


@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok", "bot": "АС Партнёрский бот"})

@app.route("/send-report", methods=["POST"])
def send_report():
    try:
        data = request.json
        month_label = data.get("month_label", "текущий месяц")
        text = build_report(data, month_label)
        result = send_message(text)
        logging.info(f"Report sent: {result}")
        return jsonify({"status": "ok", "tg_result": result})
    except Exception as e:
        logging.error(f"Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/send-personal", methods=["POST"])
def send_personal():
    try:
        data = request.json
        personal_chat_id = data.get("personal_chat_id")
        if not personal_chat_id:
            return jsonify({"status": "skip", "reason": "no personal_chat_id"})

        text = build_personal_report(data["tp_data"], data["month_label"], data["rank"])
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": personal_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }, timeout=10)
        return jsonify({"status": "ok", "tg_result": r.json()})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
