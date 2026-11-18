"""美餐订餐业务逻辑：历史学习 → 候选打分 → 下单。

全部是纯函数式接口（凭据 payload + cache 显式传入），不碰全局状态，
因此可以同时服务多个用户。
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta

from app.core.feature import JobResult

from .client import MeicanClient, MeicanError
from .preference import DishCandidate, PreferenceModel, rank_candidates, summarize

STATUS_ORDERED = "ORDER"


def _client(payload: dict) -> MeicanClient:
    return MeicanClient(payload.get("accessToken"))


def keepalive(payload: dict) -> bool:
    """探测登录态是否有效。"""
    try:
        _client(payload).account()
        return True
    except MeicanError:
        return False


HISTORY_TTL = 12 * 3600        # 历史订单缓存 12 小时
# 美餐日历接口实测只能查约 75 天内，更早返回 "Period is out of range"。
# 取 60 天留足余量；偏好模型本身用 14 天半衰期，60 天足够。
HISTORY_DAYS = 60


def fetch_history(payload: dict, cache: dict, days: int = HISTORY_DAYS,
                  max_pages: int = 8) -> list[dict]:
    """拉取历史订单，带 12 小时缓存。

    cache 是按用户持久化的字典，含两个独立子键：
      - "history": {"ts": 拉取时间, "records": [...]}
      - "restaurant_names": {餐厅id: 餐厅名}
    首次使用（缓存为空）会自动从美餐 API 拉取——新用户登录、上生产
    初始化都走这一条，无需迁移旧库。调用方负责把 cache 回写持久化。
    """
    cached = cache.get("history")
    if cached and time.time() - cached.get("ts", 0) < HISTORY_TTL:
        return cached["records"]

    client = _client(payload)
    end = date.today()
    start = end - timedelta(days=days)
    records: list[dict] = []
    names = cache.get("restaurant_names") or {}
    cursor = start
    pages = 0
    while cursor <= end and pages < max_pages:
        chunk_end = min(cursor + timedelta(days=13), end)
        try:
            day_items = client.calendar_items(cursor, chunk_end, with_order=True)
        except MeicanError:
            # 单段失败（如超出可查范围）跳过继续，不要整体中断，
            # 否则一段报错就丢掉后面所有能查到的历史。
            cursor = chunk_end + timedelta(days=1)
            continue
        pages += 1
        for day in day_items:
            d = day.get("targetTime") or cursor.isoformat()
            for item in day.get("calendarItemList") or []:
                order = item.get("corpOrderUser")
                if not order:
                    continue
                for ri in order.get("restaurantItemList") or []:
                    rid = (ri.get("restaurant") or {}).get("uniqueId", "")
                    rname = names.get(rid) or (ri.get("restaurant") or {}).get("name") or rid
                    for di in ri.get("dishItemList") or []:
                        dish = di.get("dish") or {}
                        records.append({
                            "date": d[:10],
                            "restaurant": rname,
                            "dish": dish.get("name", ""),
                            "price": dish.get("priceInCent", 0) / 100,
                        })
        cursor = chunk_end + timedelta(days=1)
    cache["history"] = {"ts": time.time(), "records": records}
    return records


def _target_time_str(item: dict) -> str:
    ts = item.get("targetTime")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
    opening = item.get("openingTime") or {}
    return f"{date.today().isoformat()} {opening.get('closeTime', '18:00')}"


def find_meal_item(items: list[dict]) -> dict | None:
    if not items:
        return None
    for it in items:
        if "晚餐" in (it.get("title") or ""):
            return it
    return items[0]


def restaurant_unavailable_reason(r: dict) -> str:
    """餐厅为什么不能下单。返回 "" 表示可订。

    依据美餐前端源码（demeter-fe/82b73ae.js: generateRestaurantSections）：
        var e = !t.open,                    // isDisabled
            a = t.availableDishCount <= 0;  // isDisabledByRestaurantCount
    两处分别对应不同提示文案（afd1e10.js）：
        isDisabledByRestaurantCount → restaurant_status_exceed_limit
            「很抱歉，当前已超过餐厅的点餐限额」
        isDisabled                 → restaurant_status_close
            「很抱歉，该餐厅已经关闭点餐」

    所以这是两个并列的原因，不能合并成一个布尔值——「份数用完」和
    「不在供餐时段」对用户的含义不同，提示也该不同。

    restaurantStatus 是总状态（1=关闭点餐, 2=超限），与上面两个标记
    是一致的，但单看它无法区分原因，故仍以前端同源的 open/份数为主，
    status 仅作兜底：万一 open/count 字段缺失但 status 明示不可订，
    也不该放行。
    """
    avail = r.get("availableDishCount")
    if avail is not None and avail <= 0:
        return "sold_out"
    if r.get("open") is False:
        return "closed"
    # 兜底：字段缺失但总状态明示不可订
    status = r.get("restaurantStatus")
    if status is not None and status not in (0,):
        return "sold_out" if status == 2 else "closed"
    return ""


def _restaurant_available(r: dict) -> bool:
    """餐厅是否还能下单。"""
    return not restaurant_unavailable_reason(r)


def unavailable_message(restaurant_name: str, reason: str) -> str:
    """按不可订原因给出与美餐一致的提示。

    文案对应美餐前端 i18n：
      sold_out → restaurant_status_exceed_limit「已超过餐厅的点餐限额」
      closed   → restaurant_status_close「该餐厅已经关闭点餐」
    """
    name = restaurant_name or "该餐厅"
    if reason == "sold_out":
        return f"「{name}」已超过点餐限额（订满），无法下单"
    if reason == "closed":
        return f"「{name}」当前已关闭点餐，无法下单"
    # 原因缺失（如前端传来的是旧结构）：给出涵盖两种可能的中性说明
    return f"「{name}」已订满或已关闭点餐，无法下单"


def build_candidates(client: MeicanClient, item: dict, max_price: float) -> list[DishCandidate]:
    tab = (item.get("userTab") or {}).get("uniqueId")
    tt = _target_time_str(item)
    out: list[DishCandidate] = []
    for r in client.restaurants(tab, tt):
        reason = restaurant_unavailable_reason(r)
        avail = not reason
        try:
            menu = client.restaurant_menu(r["uniqueId"], tab, tt)
        except MeicanError:
            continue
        for dish in menu.get("dishList") or []:
            if dish.get("isSection"):
                continue
            price = dish.get("priceInCent", 0) / 100
            if max_price and price > max_price:
                continue
            out.append(DishCandidate(r.get("name", ""), r.get("uniqueId", ""),
                                     dish.get("id"), dish.get("name", ""), price,
                                     available=avail,
                                     dish_limit=r.get("dishLimit"),
                                     available_count=r.get("availableDishCount"),
                                     unavailable_reason=reason))
    return out


# 当天可订菜品对所有同企业用户是固定的，拉一次全局共享缓存即可，
# 不必每个用户、每次推荐都重新打几十次餐厅菜单接口。
MENU_TTL = 6 * 3600        # 菜单缓存 6 小时（当天菜单基本不变）


def _menu_cache_key(target_day: date, item: dict) -> str:
    corp = (item.get("userTab") or {}).get("corp") or {}
    ns = corp.get("namespace") or corp.get("uniqueId") or "default"
    return f"meican:menu:{target_day.isoformat()}:{ns}:{_target_time_str(item)}"


def candidates_to_raw(cands: list[DishCandidate]) -> list[dict]:
    return [{"restaurant_name": c.restaurant_name, "restaurant_id": c.restaurant_id,
             "dish_id": c.dish_id, "dish_name": c.dish_name, "price": c.price,
             "available": c.available, "dish_limit": c.dish_limit,
             "available_count": c.available_count,
             "unavailable_reason": c.unavailable_reason}
            for c in cands]


def today_meal_item(client: MeicanClient, target_day: date) -> dict:
    """取当天餐次。返回 {} 表示当天没有可订餐次。"""
    days = client.calendar_items(target_day, target_day, with_order=True)
    if not days or not days[0].get("calendarItemList"):
        return {}
    return find_meal_item(days[0]["calendarItemList"]) or {}


def refresh_menu_cache(client: MeicanClient, target_day: date) -> tuple[dict, list[dict]]:
    """强制重拉当天菜单与份数并写回缓存。返回 (餐次item, 原始候选)。

    与 get_candidates_cached 的区别：不看 TTL，总是打接口。
    份数（availableDishCount）会随别人下单实时变化，所以需要定期刷。
    """
    from app.core import db
    item = today_meal_item(client, target_day)
    if not item:
        return {}, []

    raw = candidates_to_raw(build_candidates(client, item, max_price=0))
    db.gdata_set(_menu_cache_key(target_day, item),
                 {"ts": time.time(), "candidates": raw})
    return item, raw


def get_candidates_cached(payload: dict, target_day: date,
                          max_price: float = 0,
                          refresh: bool = False) -> tuple[list[DishCandidate], dict]:
    """取当天可订菜品，优先用全局共享缓存。返回 (候选列表, 餐次item)。

    缓存键含日期+企业namespace+餐次时间：同企业用户共享，不同企业互不干扰。
    max_price 在缓存之上过滤（缓存存全量，限价各用户可能不同）。
    refresh=True 时跳过 TTL 强制刷新（后台定时任务用）。
    """
    from app.core import db
    client = _client(payload)
    days = client.calendar_items(target_day, target_day, with_order=True)
    if not days or not days[0].get("calendarItemList"):
        return [], {}
    item = find_meal_item(days[0]["calendarItemList"])
    if not item:
        return [], {}

    key = _menu_cache_key(target_day, item)
    cached = db.gdata_get(key)
    if not refresh and cached and time.time() - cached.get("ts", 0) < MENU_TTL:
        raw = cached["candidates"]
    else:
        item, raw = refresh_menu_cache(client, target_day)

    out = []
    for c in raw:
        if max_price and c["price"] > max_price:
            continue
        out.append(DishCandidate(c["restaurant_name"], c["restaurant_id"],
                                 c["dish_id"], c["dish_name"], c["price"],
                                 available=c.get("available", True),
                                 dish_limit=c.get("dish_limit"),
                                 available_count=c.get("available_count"),
                                 unavailable_reason=c.get("unavailable_reason", "")))
    return out, item


def check_status(payload: dict, target_day: date) -> dict:
    client = _client(payload)
    days = client.calendar_items(target_day, target_day, with_order=True)
    if not days or not days[0].get("calendarItemList"):
        return {"date": target_day.isoformat(), "has_item": False, "ordered": False}
    item = find_meal_item(days[0]["calendarItemList"])
    if not item:
        return {"date": target_day.isoformat(), "has_item": False, "ordered": False}
    return {
        "date": target_day.isoformat(), "has_item": True,
        "ordered": bool(item.get("corpOrderUser")) or item.get("status") == STATUS_ORDERED,
        "status": item.get("status"), "reason": item.get("reason", ""),
        "title": item.get("title"),
        "opening_time": item.get("openingTime"),
        "order_detail": _extract_order(item),
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
            dishes.append({"name": d.get("name"),
                           "price": d.get("priceInCent", 0) / 100,
                           "count": di.get("count")})
    return {"dishes": dishes, "pay_status": order.get("payStatus"),
            "corp_order_status": order.get("corpOrderStatus"),
            "postbox": order.get("postbox"), "address": order.get("corpAddress")}


def recommend(payload: dict, cache: dict, target_day: date, limit: int = 5,
              max_price: float = 25.0) -> dict:
    cands, item = get_candidates_cached(payload, target_day, max_price)
    if not item:
        return {"date": target_day.isoformat(), "error": "该日期没有可订餐次"}
    model = PreferenceModel.from_history(fetch_history(payload, cache))
    top = rank_candidates(cands, target_day, model, limit=limit)
    # 可订的排前面，订满/打烊的置灰排后面（仍展示，但不可订）
    top.sort(key=lambda c: not c.available)
    return {
        "date": target_day.isoformat(),
        "title": item.get("title"),
        "status": item.get("status"),
        "already_ordered": bool(item.get("corpOrderUser")),
        "history_summary": summarize(model),
        "candidates": [{
            "restaurant_name": c.restaurant_name, "restaurant_id": c.restaurant_id,
            "dish_id": c.dish_id, "dish_name": c.dish_name,
            "price": c.price, "score": c.score, "reasons": c.reasons,
            "available": c.available,
            "dish_limit": c.dish_limit, "available_count": c.available_count,
            "unavailable_reason": c.unavailable_reason,
        } for c in top],
        "total_candidates": len(cands),
    }


def do_order(payload: dict, cache: dict, target_day: date,
             dish: dict | None = None, dry_run: bool = False,
             max_price: float = 25.0) -> JobResult:
    """下单。dish 为空则自动挑最高分候选。dry_run 只演算不提交。"""
    status = check_status(payload, target_day)
    if not status.get("has_item"):
        return JobResult(False, f"{target_day} 没有可订餐次")
    if status.get("ordered"):
        return JobResult(True, f"{target_day} 已订过餐，跳过", {"skipped": True})

    if dish is None:
        rec = recommend(payload, cache, target_day, limit=5, max_price=max_price)
        if rec.get("error"):
            return JobResult(False, rec["error"])
        # 自动挑菜只挑可订的（订满/打烊的跳过）
        avail = [c for c in rec.get("candidates", []) if c.get("available", True)]
        if not avail:
            return JobResult(False, "没有可订的候选菜品（餐厅可能均已订满）")
        dish = avail[0]

    if not dish.get("available", True):
        return JobResult(False, unavailable_message(dish.get("restaurant_name", ""),
                                                    dish.get("unavailable_reason", "")))

    detail = {"date": target_day.isoformat(), "dish": dish, "dry_run": dry_run}
    if dry_run:
        return JobResult(True, f"[试运行] 将下单：{dish['restaurant_name']} / {dish['dish_name']}", detail)

    client = _client(payload)
    item = status["raw_item"]
    tab = (item.get("userTab") or {}).get("uniqueId")
    tt = _target_time_str(item)
    address_id = _resolve_address(client, item)
    if not address_id:
        return JobResult(False, "下单失败：未找到企业取餐地址，请先在美餐里选一次取餐地址", detail)
    try:
        res = client.place_order(
            tab_unique_id=tab, target_time=tt,
            dishes=[{"dishId": dish["dish_id"], "count": 1}],
            address_unique_id=address_id,
        )
    except MeicanError as exc:
        return JobResult(False, f"下单失败：{exc}", detail)
    detail["response"] = res
    cache.setdefault("restaurant_names", {})[dish["restaurant_id"]] = dish["restaurant_name"]
    return JobResult(True, f"下单成功：{dish['restaurant_name']} / {dish['dish_name']}", detail)


def change_order(payload: dict, cache: dict, target_day: date,
                 dish: dict) -> JobResult:
    """更换已订的餐：先取消旧订单，再下新单。

    取消失败则不动（避免退了旧单又下不了新单导致没饭吃）；
    取消成功但新单失败时如实告警，提示用户当前无订单。
    """
    if not dish.get("available", True):
        return JobResult(False, unavailable_message(dish.get("restaurant_name", ""),
                                                    dish.get("unavailable_reason", "")))

    status = check_status(payload, target_day)
    if not status.get("has_item"):
        return JobResult(False, f"{target_day} 没有可订餐次")
    if not status.get("ordered"):
        # 还没订过，直接下新单即可
        return do_order(payload, cache, target_day, dish, dry_run=False)

    client = _client(payload)
    old_order = (status["raw_item"].get("corpOrderUser") or {})
    old_id = old_order.get("uniqueId")
    old_name = ""
    for ri in old_order.get("restaurantItemList") or []:
        for di in ri.get("dishItemList") or []:
            old_name = (di.get("dish") or {}).get("name", "")
            break
        if old_name:
            break

    detail = {"date": target_day.isoformat(), "old_dish": old_name, "dish": dish}
    if old_id:
        try:
            client.delete_order(old_id)
        except MeicanError as exc:
            return JobResult(False, f"取消原订单失败，未更换：{exc}", detail)

    # 旧单已退，下新单
    result = do_order(payload, cache, target_day, dish, dry_run=False)
    result.detail = {**detail, **(result.detail or {})}
    if result.ok:
        result.message = f"已更换：{old_name or '原订单'} → {dish['restaurant_name']} / {dish['dish_name']}"
    else:
        result.message = f"原订单已取消，但新单下单失败：{result.message}（当前无订单，请重新订餐）"
    return result


def _resolve_address(client: MeicanClient, item: dict) -> str:
    """取企业取餐地址 uniqueId：优先餐次自带，其次最近用过，再次地址列表首个。

    美餐下单必须带企业地址，否则返回 CORP_ADDRESS_NOT_FOUND。
    """
    addr = (item.get("corpAddress") or {}).get("uniqueId")
    if addr:
        return addr
    corp = (item.get("userTab") or {}).get("corp") or {}
    ns = corp.get("namespace") or corp.get("uniqueId") or ""
    if not ns:
        return ""
    try:
        data = client.corp_addresses(ns)
    except MeicanError:
        return ""
    recent = data.get("recentList") or []
    if recent and recent[0].get("uniqueId"):
        return recent[0]["uniqueId"]
    for a in data.get("addressList") or []:
        fv = a.get("finalValue") or {}
        if fv.get("uniqueId"):
            return fv["uniqueId"]
    return ""


def load_cache(user_id: int) -> dict:
    """读取该用户的美餐缓存（历史 + 餐厅名），两个独立 KV 键合成一个 dict。"""
    from app.core import db
    return {
        "history": db.fdata_get(user_id, "meican", "history", default=None),
        "restaurant_names": db.fdata_get(user_id, "meican", "restaurant_names", default={}) or {},
    }


def save_cache(user_id: int, cache: dict) -> None:
    """把缓存回写持久化。历史与餐厅名分别存独立键，互不污染。"""
    from app.core import db
    if cache.get("history") is not None:
        db.fdata_set(user_id, "meican", "history", cache["history"])
    db.fdata_set(user_id, "meican", "restaurant_names", cache.get("restaurant_names") or {})


# ---------------- 本周水果菜单 ----------------

FRUIT_TTL = 12 * 3600        # 水果菜单每周换一次，缓存 12 小时足够新鲜
_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def parse_fruit_menu(content: str) -> dict:
    """从公告正文解析每天的水果。

    正文形如：
        周一\\r\\n【整果】云南青柚+鸡心果+香蕉+龙眼1000g\\r\\n周二\\r\\n...
    返回 {"周一": "【整果】...", ...}。识别不到的天跳过。
    每天的水果行以「【...】」开头；遇到「温馨提示」等说明性文字即停止
    累积，避免把公告尾部说明混进最后一天。
    """
    text = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    out: dict[str, str] = {}
    current = None
    for ln in lines:
        if ln in _WEEKDAYS:
            current = ln
            out.setdefault(current, "")
        elif current and ln:
            if ln.startswith("【"):
                out[current] = (out[current] + " " + ln).strip() if out[current] else ln
            else:
                # 非水果描述行（如「温馨提示」），结束当前天的累积
                current = None
    return {k: v for k, v in out.items() if v}


def get_fruit_menu(payload: dict, target_day: date) -> dict:
    """取本周水果菜单，按企业全局缓存。返回当天 + 整周。

    水果菜单是企业公告，同企业用户共享，每周换一次。
    """
    from app.core import db
    client = _client(payload)
    # 先确定企业namespace（从当天餐次取），用于缓存键
    ns = "default"
    try:
        days = client.calendar_items(target_day, target_day, with_order=False)
        if days and days[0].get("calendarItemList"):
            item = find_meal_item(days[0]["calendarItemList"])
            corp = (item.get("userTab") or {}).get("corp") or {}
            ns = corp.get("namespace") or corp.get("uniqueId") or "default"
    except MeicanError:
        pass

    # 缓存键按「本周一」对齐，整周共享一份
    monday = target_day - timedelta(days=target_day.weekday())
    key = f"meican:fruit:{monday.isoformat()}:{ns}"
    cached = db.gdata_get(key)
    if cached and time.time() - cached.get("ts", 0) < FRUIT_TTL:
        menu = cached["menu"]
        title = cached.get("title", "本周水果菜单")
    else:
        menu, title = {}, "本周水果菜单"
        try:
            for notice in client.corp_notices():
                if "水果" in (notice.get("title") or ""):
                    menu = parse_fruit_menu(notice.get("content") or "")
                    title = notice.get("title") or title
                    break
        except MeicanError:
            menu = {}
        db.gdata_set(key, {"ts": time.time(), "menu": menu, "title": title})

    weekday = _WEEKDAYS[target_day.weekday()]
    return {
        "date": target_day.isoformat(),
        "weekday": weekday,
        "title": title,
        "today": menu.get(weekday, ""),
        "week": [{"weekday": w, "fruit": menu[w]} for w in _WEEKDAYS[:5] if w in menu],
        "available": bool(menu),
    }