"""业务编排：检测订餐状态、提取历史、推荐、下单。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import db
from .meican import MeicanClient, MeicanError
from .preference import DishCandidate, PreferenceModel, rank_candidates, summarize

# 状态常量
STATUS_ORDERED = "ORDER"
STATUS_NOT_YET = "NOT_YET"


def _client_from_creds(user_id: int) -> MeicanClient:
    creds = db.load_creds(user_id)
    if not creds.access_token:
        raise MeicanError("尚未登录，请先完成手机验证码登录")
    return MeicanClient(creds.access_token)


def fetch_history(user_id: int, days: int = 45) -> list[dict]:
    """拉取历史订单，返回扁平记录 [{date, restaurant, dish, dish_id, price}]

    订单里只有餐厅 id（restaurantItemList[].uniqueId），没有餐厅名。
    餐厅列表只在当天可查，所以历史订单的餐厅名靠本地缓存逐步补齐：
    每次调用都会把「当前能查到的 id→名字」累积写入 data/restaurant_names.json。
    """
    client = _client_from_creds(user_id)
    cache = db.load_restaurant_names(user_id)
    end = date.today()
    start = end - timedelta(days=days)
    mid = start + timedelta(days=days // 2)

    # 阶段一：拉取所有日历项，并尽量补全餐厅 id→名字 缓存
    raw_items: list[tuple[str, dict]] = []
    for b, e in ((start, mid), (mid + timedelta(days=1), end)):
        if b > e:
            continue
        for day in client.calendar_items(b, e, with_order=True):
            d = day.get("date")
            for item in day.get("calendarItemList") or []:
                if not item.get("corpOrderUser"):
                    continue
                raw_items.append((d, item))
                tab = (item.get("userTab") or {}).get("uniqueId")
                if tab:
                    try:
                        for r in client.restaurants(tab, _target_time_str(item)):
                            rid, rname = r.get("uniqueId", ""), r.get("name", "")
                            if rid and rname:
                                cache[rid] = rname
                    except MeicanError:
                        pass
    db.save_restaurant_names(user_id, cache)

    # 阶段二：用补全后的缓存解析记录
    records: list[dict] = []
    for d, item in raw_items:
        order = item["corpOrderUser"]
        for ri in order.get("restaurantItemList") or []:
            rid = ri.get("uniqueId", "")
            rname = cache.get(rid) or "(未知餐厅)"
            for di in ri.get("dishItemList") or []:
                dish = di.get("dish") or {}
                records.append({
                    "date": d,
                    "restaurant": rname,
                    "restaurant_id": rid,
                    "dish": dish.get("name", ""),
                    "dish_id": dish.get("id"),
                    "price": dish.get("priceInCent", 0) / 100,
                    "count": di.get("count", 1),
                    "corp_order_status": order.get("corpOrderStatus"),
                })
    records.sort(key=lambda r: r["date"] or "")
    return records


def find_meal_item(items: list[dict], prefer_title: str = "") -> dict | None:
    """从某天的 calendarItemList 里挑出要处理的餐次（晚餐优先）。"""
    if not items:
        return None
    if prefer_title:
        for it in items:
            if prefer_title in (it.get("title") or ""):
                return it
    for it in items:
        if "晚餐" in (it.get("title") or ""):
            return it
    return items[0]


def build_candidates(client: MeicanClient, item: dict, max_price: float) -> list[DishCandidate]:
    tab = item.get("userTab") or {}
    tab_id = tab.get("uniqueId")
    target_time = _target_time_str(item)
    cands: list[DishCandidate] = []
    for r in client.restaurants(tab_id, target_time):
        try:
            menu = client.restaurant_menu(r["uniqueId"], tab_id, target_time)
        except MeicanError:
            continue
        for dish in menu.get("dishList") or []:
            if dish.get("isSection"):
                continue
            price = dish.get("priceInCent", 0) / 100
            if max_price and price > max_price:
                continue
            cands.append(DishCandidate(
                restaurant_name=r.get("name", ""),
                restaurant_id=r.get("uniqueId", ""),
                dish_id=dish.get("id"),
                dish_name=dish.get("name", ""),
                price=price,
            ))
    return cands


def _target_time_str(item: dict) -> str:
    ts = item.get("targetTime")
    if ts:
        # 毫秒时间戳 → 本地时间字符串
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
    opening = item.get("openingTime") or {}
    close = opening.get("closeTime", "18:00")
    return f"{date.today().isoformat()} {close}"


def check_status(user_id: int, target_day: date | None = None) -> dict:
    """检测指定日期的订餐状态。未指定时查「今天」——语义更符合直觉。"""
    target_day = target_day or date.today()
    client = _client_from_creds(user_id)
    days = client.calendar_items(target_day, target_day, with_order=True)
    if not days:
        return {"date": target_day.isoformat(), "has_item": False, "ordered": False,
                "raw": []}
    items = days[0].get("calendarItemList") or []
    item = find_meal_item(items)
    if not item:
        return {"date": target_day.isoformat(), "has_item": False, "ordered": False, "raw": items}
    ordered = bool(item.get("corpOrderUser")) or item.get("status") == STATUS_ORDERED
    return {
        "date": target_day.isoformat(),
        "has_item": True,
        "ordered": ordered,
        "status": item.get("status"),
        "reason": item.get("reason", ""),
        "title": item.get("title"),
        "tab_unique_id": (item.get("userTab") or {}).get("uniqueId"),
        "opening_time": item.get("openingTime"),
        "order_detail": _extract_order(item),
        "address": (item.get("corpOrderUser") or {}).get("corpAddress"),
        "raw_item": item,
    }


def _extract_order(item: dict) -> dict | None:
    order = item.get("corpOrderUser")
    if not order:
        return None
    dishes = []
    for ri in order.get("restaurantItemList") or []:
        for di in ri.get("dishItemList") or []:
            d = di.get("dish") or {}
            dishes.append({"name": d.get("name"), "price": d.get("priceInCent", 0) / 100,
                           "count": di.get("count")})
    return {
        "dishes": dishes,
        "pay_status": order.get("payStatus"),
        "corp_order_status": order.get("corpOrderStatus"),
        "postbox": order.get("postbox"),
        "address": order.get("corpAddress"),
    }


def recommend(user_id: int, target_day: date | None = None, limit: int = 5) -> dict:
    """为指定日期（默认明天）生成推荐。"""
    target_day = target_day or (date.today() + timedelta(days=1))
    client = _client_from_creds(user_id)
    days = client.calendar_items(target_day, target_day, with_order=True)
    if not days or not days[0].get("calendarItemList"):
        return {"date": target_day.isoformat(), "error": "该日期没有可订餐次"}
    item = find_meal_item(days[0]["calendarItemList"])
    if not item:
        return {"date": target_day.isoformat(), "error": "未找到餐次"}

    history = fetch_history(user_id)
    model = PreferenceModel.from_history(history)
    cands = build_candidates(client, item, db.load_config(user_id).max_price)
    top = rank_candidates(cands, target_day, model, limit=limit)
    return {
        "date": target_day.isoformat(),
        "title": item.get("title"),
        "status": item.get("status"),
        "ordered": bool(item.get("corpOrderUser")),
        "candidates_total": len(cands),
        "recommendations": [
            {"restaurant": c.restaurant_name, "dish": c.dish_name, "price": c.price,
             "score": c.score, "reasons": c.reasons,
             "dish_id": c.dish_id, "restaurant_id": c.restaurant_id}
            for c in top
        ],
        "model": summarize(model),
    }


def _last_known_address(client: MeicanClient) -> str | None:
    """从最近 30 天的订单里找取餐地址。同一餐次地址基本固定。"""
    end = date.today()
    try:
        for day in client.calendar_items(end - timedelta(days=30), end, with_order=True):
            for item in day.get("calendarItemList") or []:
                order = item.get("corpOrderUser") or {}
                addr = order.get("corpAddress") or {}
                if addr.get("uniqueId"):
                    return addr["uniqueId"]
    except MeicanError:
        return None
    return None


def place_order(user_id: int, target_day: date | None = None, dish: dict | None = None,
                dry_run: bool = True) -> dict:
    """下单。dish 为 None 时自动取推荐第一名。"""
    target_day = target_day or (date.today() + timedelta(days=1))
    client = _client_from_creds(user_id)

    # 已订则不重复下单
    st = check_status(user_id, target_day)
    if st.get("ordered"):
        return {"ok": False, "skipped": True, "message": "该日期已订餐，跳过", "status": st}

    if dish is None:
        rec = recommend(user_id, target_day, limit=1)
        if rec.get("error") or not rec.get("recommendations"):
            return {"ok": False, "error": rec.get("error", "没有可用推荐")}
        dish = rec["recommendations"][0]

    item = st.get("raw_item")
    if not item:
        return {"ok": False, "error": "找不到餐次信息"}
    target_time = _target_time_str(item)
    tab_id = st.get("tab_unique_id")

    # 地址：目标日若已有历史订单，直接用它的地址；
    # 否则回退到最近一次成功订单的地址（同一餐次一般固定为办公区取餐点）。
    address_id = ""
    addr = ((item.get("corpOrderUser") or {}).get("corpAddress") or {})
    if addr.get("uniqueId"):
        address_id = addr["uniqueId"]
    else:
        address_id = _last_known_address(client) or ""

    plan = {
        "date": target_day.isoformat(),
        "target_time": target_time,
        "tab_unique_id": tab_id,
        "address_id": address_id,
        "dish": dish,
        "dry_run": dry_run,
    }
    if not address_id:
        return {"ok": False, "error": "找不到取餐地址，请先手动在网页订一次以确定地址", "plan": plan}

    if dry_run:
        db.log(user_id, "order_dry_run", f"演练：将下单 {dish.get('dish')}", plan)
        return {"ok": True, "dry_run": True, "plan": plan}

    try:
        result = client.place_order(
            tab_unique_id=tab_id,
            target_time=target_time,
            dishes=[{"dishId": dish["dish_id"], "count": 1}],
            address_unique_id=address_id,
        )
    except MeicanError as exc:
        db.log(user_id, "order_failed", exc.message, {**plan, "error": str(exc)})
        return {"ok": False, "error": exc.message, "plan": plan}

    db.log(user_id, "order_success", f"已下单 {dish.get('dish')}", {**plan, "result": result})
    return {"ok": True, "plan": plan, "result": result}


def run_daily_job(user_id: int) -> dict:
    """每日 15:30 定时任务：检测 → 未订则推荐并下单。"""
    cfg = db.load_config(user_id)
    if not cfg.enabled:
        return {"skipped": True, "reason": "自动订餐未启用"}
    target = date.today() + timedelta(days=1) if cfg.order_target == "next_day" else date.today()
    db.log(user_id, "job_start", f"开始每日检测 {target.isoformat()}")
    st = check_status(user_id, target)
    if st.get("ordered"):
        db.log(user_id, "job_skip", f"{target.isoformat()} 已订餐")
        return {"ok": True, "already_ordered": True, "status": st}

    result = place_order(user_id, target, dry_run=cfg.dry_run)
    db.log(user_id, "job_done", f"检测完成，下单结果 {result.get('ok')}", result)
    return result
