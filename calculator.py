from config import SALARY_ADMIN, get_blended_percent

def round_salary(amount: float) -> int:
    base = int(amount // 50) * 50
    rem  = int(amount % 50)
    if rem <= 25:   return base
    if rem <= 50:   return base + 50
    if rem <= 80:   return base + 50
    return base + 100

def calculate_summary(session: dict) -> dict:
    cars     = session.get("cars", [])
    products = session.get("products", [])
    expenses = session.get("expenses", [])
    loyalty  = session.get("loyalty", [])

    # Скидки по машинам
    loyalty_by_car = {}
    for l in loyalty:
        loyalty_by_car[l["car_num"]] = loyalty_by_car.get(l["car_num"], 0) + l["discount"]

    # car["price"] хранит ПОЛНУЮ стоимость услуги (без вычета скидки).
    # Скидка лояльности хранится отдельно в session["loyalty"].
    # Поэтому чтобы получить реально поступившие деньги — вычитаем скидку.
    raw_cash   = sum(c["price"] for c in cars if c["payment"].lower() in ["нал","наличка"])
    raw_visa   = sum(c["price"] for c in cars if c["payment"].lower() in ["visa","виза"])
    raw_beznal = sum(c["price"] for c in cars if c["payment"].lower() in ["безнал","петрон","petron"])

    # Для отображения — сколько скидок пришлось на какой тип оплаты
    loyalty_cash   = sum(loyalty_by_car.get(c["num"], 0) for c in cars if c["payment"].lower() in ["нал","наличка"])
    loyalty_visa   = sum(loyalty_by_car.get(c["num"], 0) for c in cars if c["payment"].lower() in ["visa","виза"])
    loyalty_beznal = sum(loyalty_by_car.get(c["num"], 0) for c in cars if c["payment"].lower() in ["безнал","петрон","petron"])

    # Реально поступившие деньги (после вычета скидки)
    cash   = raw_cash   - loyalty_cash
    visa   = raw_visa   - loyalty_visa
    beznal = raw_beznal - loyalty_beznal

    products_cash   = sum(p["price"] for p in products if p["payment"].lower() in ["нал","наличка"])
    products_visa   = sum(p["price"] for p in products if p["payment"].lower() in ["visa","виза"])
    products_beznal = sum(p["price"] for p in products if p["payment"].lower() in ["безнал","петрон","petron"])
    total_products  = products_cash + products_visa + products_beznal

    cash   += products_cash
    visa   += products_visa
    beznal += products_beznal
    total  = cash + visa + beznal

    total_loyalty = sum(l["discount"] for l in loyalty)

    washer_totals   = {}
    washer_salaries = {}
    for car in cars:
        emp      = car["employee"]
        loy      = loyalty_by_car.get(car["num"], 0)
        # Зарплата — от полной суммы (car["price"] уже полная, до вычета скидки)
        full_sum = car["price"]
        washer_totals[emp] = washer_totals.get(emp, 0) + full_sum

        breakdown = car.get("price_breakdown")
        if breakdown:
            salary_part = sum(v["price"] * v["percent"] for v in breakdown.values())
            avg_pct     = sum(v["percent"] for v in breakdown.values()) / len(breakdown)
            salary_part += loy * avg_pct
        else:
            pct         = get_blended_percent(car.get("service_keys") or [car.get("service_key", "")])
            salary_part = full_sum * pct
        washer_salaries[emp] = washer_salaries.get(emp, 0) + salary_part

    washer_salaries = {e: round_salary(v) for e, v in washer_salaries.items()}

    total_washers = sum(washer_totals.values())
    admin_base    = total_washers + total_products
    admin_salary  = round_salary(admin_base * session.get("admin_percent", SALARY_ADMIN))

    total_expenses = sum(e["amount"] for e in expenses)
    expenses_str   = "; ".join(f"{e['name']} - {e['amount']}" for e in expenses) or "нет"

    return {
        "total": total, "grand_total": total + total_loyalty,
        "cash": cash, "visa": visa, "beznal": beznal,
        "total_loyalty": total_loyalty,
        "loyalty_cash": loyalty_cash, "loyalty_visa": loyalty_visa, "loyalty_beznal": loyalty_beznal,
        "total_products": total_products,
        "washer_totals": washer_totals, "washer_salaries": washer_salaries,
        "admin_salary": admin_salary,
        "total_expenses": total_expenses, "expenses_str": expenses_str,
        "remainder": cash - total_expenses,
    }
