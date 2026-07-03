from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from sessions import get_session, save_sessions
from calculator import calculate_summary
from handlers.admin import get_current_branch


# ── /loyal — кнопками: выбор машины → скидка текстом ─────────────────────

async def loyal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск с кнопки '💜 Лояльность' — показывает список машин."""
    query  = update.callback_query
    branch = get_current_branch(context)
    if not branch:
        await query.message.reply_text("⚠️ Сначала выбери филиал: /newday"); return
    session = get_session(branch)
    cars = session.get("cars", [])
    if not cars:
        await query.message.reply_text("📋 Список пуст. Сначала добавь машину."); return
    buttons = [
        [InlineKeyboardButton(f"#{c['num']} {c['car']} — {c['price']}₽", callback_data=f"loyal_{c['num']}")]
        for c in cars
    ]
    await query.message.reply_text("💜 *Для какой машины скидка?*", parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons))


async def cb_loyal_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    num = int(query.data.replace("loyal_", ""))
    context.user_data["step"] = "loyal_discount"
    context.user_data["loyal_car_num"] = num
    await query.message.reply_text(f"💜 Напиши сумму скидки для #{num} (например: 200):")


async def handle_loyal_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get("step") != "loyal_discount":
        return False
    branch = get_current_branch(context)
    if not branch:
        await update.message.reply_text("⚠️ Сначала выбери филиал: /newday")
        context.user_data.pop("step", None)
        return True
    try:
        discount = int(update.message.text.strip().replace("р", "").replace("₽", ""))
    except ValueError:
        await update.message.reply_text("❌ Введи число. Пример: 200")
        return True
    num = context.user_data.pop("loyal_car_num", None)
    context.user_data.pop("step", None)
    session = get_session(branch)
    car = next((c for c in session["cars"] if c["num"] == num), None)
    if not car:
        await update.message.reply_text(f"❌ Машина #{num} не найдена.")
        return True
    session["loyalty"].append({"car_num": num, "car": car["car"], "discount": discount})
    save_sessions()
    total = sum(l["discount"] for l in session["loyalty"])
    await update.message.reply_text(
        f"💜 Лояльность добавлена\n🚗 #{num} {car['car']} — скидка {discount}₽\n"
        f"Итого лояльность: {total}₽", parse_mode="Markdown")
    return True


async def loyal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текстовая команда — оставлена для тех, кто привык."""
    branch = get_current_branch(context)
    if not branch:
        await update.message.reply_text("⚠️ Сначала выбери филиал: /newday"); return
    session = get_session(branch)
    args    = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "💜 Формат: `/loyal <номер> <скидка>`\nПример: `/loyal 1 200`",
            parse_mode="Markdown"); return
    try:
        num      = int(args[0])
        discount = int(args[1].replace("р","").replace("₽",""))
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. `/loyal 1 200`", parse_mode="Markdown"); return
    car = next((c for c in session["cars"] if c["num"] == num), None)
    if not car:
        await update.message.reply_text(f"❌ Машина #{num} не найдена."); return
    session["loyalty"].append({"car_num": num, "car": car["car"], "discount": discount})
    save_sessions()
    total = sum(l["discount"] for l in session["loyalty"])
    await update.message.reply_text(
        f"💜 Лояльность добавлена\n🚗 #{num} {car['car']} — скидка {discount}₽\n"
        f"Итого лояльность: {total}₽", parse_mode="Markdown")


# ── /expense — кнопкой запускаем подсказку, дальше текстом одной строкой ──

async def expense_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data["step"] = "expense_text"
    await query.message.reply_text(
        "💸 Напиши расход одной строкой:\nПример: `химия 500`",
        parse_mode="Markdown")


async def handle_expense_step_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get("step") != "expense_text":
        return False
    context.user_data.pop("step", None)
    await _save_expense(update, context, update.message.text.strip())
    return True


async def expense_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    await _save_expense(update, context, text)


async def _save_expense(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    branch = get_current_branch(context)
    if not branch:
        await update.message.reply_text("⚠️ Сначала выбери филиал: /newday"); return
    session = get_session(branch)
    parts   = text.split()
    if len(parts) < 2:
        await update.message.reply_text(
            "💸 Формат: `химия 500`", parse_mode="Markdown"); return
    try:
        amount = int(parts[-1].replace("р","").replace("₽",""))
        name   = " ".join(p for p in parts[:-1] if p.lower() != "расход")
        if not name:
            name = " ".join(parts[:-1])
    except ValueError:
        await update.message.reply_text("❌ Укажи сумму числом."); return
    session["expenses"].append({"name": name, "amount": amount})
    save_sessions()
    total = sum(e["amount"] for e in session["expenses"])
    await update.message.reply_text(f"💸 Расход: {name} — {amount}₽\nВсего расходов: {total}₽")


# ── /summary, /list ────────────────────────────────────────────────────────

async def show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    branch = get_current_branch(context)
    msg = update.message or update.callback_query.message
    if not branch:
        await msg.reply_text("⚠️ Сначала выбери филиал: /newday"); return
    session = get_session(branch)
    if not session["cars"] and not session.get("products"):
        await msg.reply_text("📋 Нет данных."); return
    s = calculate_summary(session)
    salary_lines = []
    for emp, sal in s["washer_salaries"].items():
        salary_lines.append(f"  {emp} — {sal}₽")
    salary_lines.append(f"  Администратор — {s['admin_salary']}₽")

    text = (
        f"📊 *Сводка за {session['date']}* | 📍 {branch}\n\n"
        f"💰 Касса: *{s['total']}₽*\n"
        f"  Нал: {s['cash']}₽ | Visa: {s['visa']}₽ | Безнал: {s['beznal']}₽\n"
    )
    if s.get("total_products"):
        text += f"🧴 Из них товары: {s['total_products']}₽\n"
    if s["total_loyalty"] > 0:
        loy = session.get("loyalty", [])
        loy_str = "; ".join(f"#{l['car_num']} -{l['discount']}₽" for l in loy)
        # Показываем из чего вычтена скидка
        loy_detail_parts = []
        if s.get("loyalty_cash"):   loy_detail_parts.append(f"нал -{s['loyalty_cash']}₽")
        if s.get("loyalty_visa"):   loy_detail_parts.append(f"visa -{s['loyalty_visa']}₽")
        if s.get("loyalty_beznal"): loy_detail_parts.append(f"безнал -{s['loyalty_beznal']}₽")
        loy_detail = " | ".join(loy_detail_parts)
        text += f"💜 Лояльность (скидки): *-{s['total_loyalty']}₽* ({loy_detail})\n   {loy_str}\n"
    text += (
        f"\n👷 *Зарплаты:*\n" + "\n".join(salary_lines) +
        f"\n\n💸 Расходы: {s['total_expenses']}₽ ({s['expenses_str']})"
        f"\n🏦 Остаток: *{s['remainder']}₽*"
    )
    keyboard = [[
        InlineKeyboardButton("📄 PDF", callback_data="cmd_pdf"),
        InlineKeyboardButton("📋 Список", callback_data="cmd_list"),
    ]]
    await msg.reply_text(text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard))


async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    branch = get_current_branch(context)
    msg = update.message or update.callback_query.message
    if not branch:
        await msg.reply_text("⚠️ Сначала выбери филиал: /newday"); return
    session = get_session(branch)
    products = session.get("products", [])
    if not session["cars"] and not products:
        await msg.reply_text("📋 Список пуст."); return
    lines = [f"📋 *Список за {session['date']}* | 📍 {branch}\n"]
    for c in session["cars"]:
        lines.append(f"#{c['num']} {c['car']} | {c['employee']} | {c['service']} | {c['price']}₽ | {c['payment']}")
    total = sum(c["price"] for c in session["cars"])
    lines.append(f"\n💰 Итого: {total}₽ | Машин: {len(session['cars'])}")
    if products:
        lines.append(f"\n🧴 *Товары:*")
        for p in products:
            lines.append(f"#{p['num']} {p['name']} | {p['price']}₽ | {p['payment']}")
        total_p = sum(p["price"] for p in products)
        lines.append(f"\n🧴 Итого товаров: {total_p}₽ | Шт: {len(products)}")
    keyboard = [[
        InlineKeyboardButton("💰 Сводка", callback_data="cmd_summary"),
        InlineKeyboardButton("✏️ Изменить цену", callback_data="cmd_edit"),
    ], [
        InlineKeyboardButton("🗑 Удалить", callback_data="cmd_delete"),
        InlineKeyboardButton("💜 Лояльность", callback_data="cmd_loyal"),
    ]]
    if products:
        keyboard.append([InlineKeyboardButton("🗑 Удалить товар", callback_data="cmd_deleteproduct")])
    await msg.reply_text("\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard))
