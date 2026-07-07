from config import SALARY_ADMIN, get_blended_percent

def round_salary(amount: float) -> int:
    """
    Округление по последним двум цифрам суммы:
      0–30  → вниз до XX00   (1830 → 1800)
      31–50 → вверх до XX50  (1831 → 1850)
      51–70 → вниз до XX50   (1861 → 1850)
      71–99 → вверх до (XX+1)00 (1871 → 1900)
    """
    amount = int(round(amount))
    base = (amount // 100) * 100
    rem = amount % 100
    if rem <= 30:
        return base
    elif rem <= 50:
        return base + 50
    elif rem <= 70:
        return base + 50
    else:
        return base + 100

def calculate_summary(session: dict) -> dict:
    cars     = session.get("cars", [])
    products = session.get("products", [])
    expenses = session.get("expenses", [])
    incomes  = session.get("incomes", [])
    loyalty  = session.get("loyalty", [])

    # Скидки по машинам
    loyalty_by_car = {}
    for l in loyalty:
        loyalty_by_car[l["car_num"]] = loyalty_by_car.get(l["car_num"], 0) + l["discount"]

    # car["price"] хранит ПОЛНУЮ стоимость услуги (без вычета скидки).
    # Скидка лояльности хранится отдельно в session["loyalty"].
    # Поэтому чтобы получить реально поступившие деньги — вычитаем скидку.
    def car_amounts(c):
        """Возвращает {метод: сумма} для машины — с учётом раздельной оплаты."""
        split = c.get("payment_split")
        if split:
            return {k.lower(): v for k, v in split.items() if v}
        return {c["payment"].lower(): c["price"]}

    def sum_method(cars_list, keys):
        total = 0
        for c in cars_list:
            for method, amount in car_amounts(c).items():
                if method in keys:
                    total += amount
        return total

    raw_cash   = sum_method(cars, ["нал", "наличка"])
    raw_visa   = sum_method(cars, ["visa", "виза"])
    raw_beznal = sum_method(cars, ["безнал", "петрон", "petron"])

    # Для отображения — сколько скидок пришлось на какой тип оплаты
    # (для машин с раздельной оплатой скидка относится к оплате наличными по умолчанию)
    def loyalty_method(c):
        split = c.get("payment_split")
        if split:
            for k in split:
                if k.lower() in ["нал", "наличка"]:
                    return "нал"
            return list(split.keys())[0].lower()
        return c["payment"].lower()

    loyalty_cash   = sum(loyalty_by_car.get(c["num"], 0) for c in cars if loyalty_method(c) in ["нал","наличка"])
    loyalty_visa   = sum(loyalty_by_car.get(c["num"], 0) for c in cars if loyalty_method(c) in ["visa","виза"])
    loyalty_beznal = sum(loyalty_by_car.get(c["num"], 0) for c in cars if loyalty_method(c) in ["безнал","петрон","petron"])

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
        # Зарплата — от полной суммы (car["price"] уже полная, до вычета скидки)
        full_sum = car["price"]
        washer_totals[emp] = washer_totals.get(emp, 0) + full_sum

        breakdown = car.get("price_breakdown")
        if breakdown:
            salary_part = sum(v["price"] * v["percent"] for v in breakdown.values())
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

    total_incomes = sum(i["amount"] for i in incomes)
    incomes_str   = "; ".join(f"{i['name']} - {i['amount']}" for i in incomes) or "нет"

    return {
        "total": total, "grand_total": total + total_loyalty,
        "cash": cash, "visa": visa, "beznal": beznal,
        "total_loyalty": total_loyalty,
        "loyalty_cash": loyalty_cash, "loyalty_visa": loyalty_visa, "loyalty_beznal": loyalty_beznal,
        "total_products": total_products,
        "washer_totals": washer_totals, "washer_salaries": washer_salaries,
        "admin_salary": admin_salary,
        "total_expenses": total_expenses, "expenses_str": expenses_str,
        "total_incomes": total_incomes, "incomes_str": incomes_str,
        "remainder": cash - total_expenses + total_incomes,
    }
