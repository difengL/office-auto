"""美餐订餐业务逻辑：历史学习 → 候选打分 → 下单。

全部是纯函数式接口（凭据 payload + cache 显式传入），不碰全局状态，
因此可以同时服务多个用户。
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from datetime import time as dt_time

from app.core.feature import JobResult

from .client import MeicanClient, MeicanError
from .preference import DishCandidate, PreferenceModel, rank_candidates, summarize

STATUS_ORDERED = "ORDER"

# 餐次（calendarItem）的状态。美餐前端 82b73ae.js 的 switch(t.status) 列出：
#   AVAILABLE / ORDER / CLOSED / NOT_YET / ALL_RESTAURANT_EXCEED
# 其中只有 AVAILABLE 与 ORDER 能下单；NOT_YET 是「还没到预订时间」，
# 此时餐厅列表每家 open 都是 false，若当成「餐厅打烊」会得出完全错误的结论。
SLOT_OPEN_STATUSES = ("AVAILABLE", STATUS_ORDERED)


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


def slot_unavailable_message(target_day: date, status: str, reason: str = "") -> str:
    """餐次本身不可下单时给出准确说明。返回 "" 表示餐次开放。

    这一步必须在下单前单独判断：餐次没开放时，餐厅列表里每家 open 都是
    false，继续往下走会被统一判成「餐厅打烊/订满」，报出与事实相反的原因
    （2026-11-19 的误报就是这样产生的）。
    """
    st = (status or "").upper()
    if st in SLOT_OPEN_STATUSES or not st:
        return ""
    day = target_day.isoformat()
    detail = f"（{reason}）" if reason else ""
    if st == "NOT_YET":
        return f"{day} 的餐次尚未开放预订{detail}"
    if st == "CLOSED":
        return f"{day} 的餐次已关闭{detail}"
    if st == "ALL_RESTAURANT_EXCEED":
        return f"{day} 所有餐厅均已订满（售罄）{detail}"
    return f"{day} 的餐次当前不可下单（状态 {status}）{detail}"


def slot_unavailable_reason(item: dict) -> str:
    """餐次级不可订原因代码；"" 表示餐次开放。

    与 restaurant_unavailable_reason 同样拆成两套文案，但这里是餐次
    本身的问题：NOT_YET 时餐厅列表每家 open 都是 false，若只为餐厅
    判断，会把「还没到预订时间」误报成「餐厅打烊」。
    """
    st = (item.get("status") or "").upper()
    if st in SLOT_OPEN_STATUSES or not st:
        return ""
    if st == "ALL_RESTAURANT_EXCEED":
        return "sold_out"
    if st == "CLOSED":
        return "closed"
    return "not_yet"


def _unavailable_summary(cands: list[dict]) -> str:
    """统计候选不可订原因，让失败信息能直接指向问题。"""
    closed = sum(1 for c in cands if c.get("unavailable_reason") == "closed")
    sold = sum(1 for c in cands if c.get("unavailable_reason") == "sold_out")
    parts = []
    if sold:
        parts.append(f"{sold} 家已订满")
    if closed:
        parts.append(f"{closed} 家已关闭点餐")
    if not parts:
        return f"共 {len(cands)} 道菜均不可订"
    return "、".join(parts)


def within_order_window(item: dict, now: datetime | None = None) -> bool:
    """当前是否在餐次的订餐窗口内（默认 09:00–16:00）。

    窗口是餐次自带的 openingTime.openTime/closeTime，不是餐厅的 open 字段：
    已订餐时餐厅列表的 open 会被整体置 false，已经不能用来判断时间了。
    缺字段时保守返回 True，交给后端给明确报错。
    """
    opening = item.get("openingTime") or {}
    start = opening.get("openTime")
    end = opening.get("closeTime")
    if not start or not end:
        return True
    now = now or datetime.now()

    def _hm(v: str) -> dt_time | None:
        try:
            h, m = v.split(":")[:2]
            return dt_time(int(h), int(m))
        except (ValueError, AttributeError):
            return None

    t0, t1 = _hm(start), _hm(end)
    if t0 is None or t1 is None:
        return True
    return t0 <= now.time() <= t1


def restaurant_unavailable_reason(r: dict, *, window_open: bool = True,
                                  already_ordered: bool = False) -> str:
    """餐厅为什么不能下单。返回 "" 表示可订。

    依据美餐前端源码（demeter-fe/82b73ae.js: generateRestaurantSections）：
        var e = !t.open,                    // isDisabled
            a = t.availableDishCount <= 0;  // isDisabledByRestaurantCount
    afd1e10.js 里两处对应不同文案：
        isDisabledByRestaurantCount → restaurant_status_exceed_limit「已超过点餐限额」
        isDisabled                 → restaurant_status_close「该餐厅已经关闭点餐」

    **但 `open` 有两种含义，必须靠上下文区分**（2026-11-19 踩坑）：
    - 未订餐：open=false 是餐厅真的不在供餐时段 / 关闭点餐
    - 已订餐：餐次状态变 ORDER，美餐把整列表 open 置 false。源码
      aa22780.js: `"AVAILABLE" !== o.status && (o.isAvailable = !1)`
      实测：12:30 已订餐时 9 家 open 全 false，但份数还有 37~86、
      restaurantStatus 仍为 0——这时是「下单入口关闭」，不是打烊。
      此时只要还在订餐窗口内，就可以更换订单。

    因此判断顺序：
    1. 份数为 0 → sold_out（与订没订无关）
    2. 不在订餐窗口 → closed
    3. restaurantStatus 明示关闭/超限 → 对应原因（已订餐也要认，换到关闭的店没意义）
    4. open=false 且未订餐 → closed；已订餐则放行
    """
    avail = r.get("availableDishCount")
    if avail is not None and avail <= 0:
        return "sold_out"
    if not window_open:
        return "closed"
    status = r.get("restaurantStatus")
    if status is not None and status not in (0,):
        return "sold_out" if status == 2 else "closed"
    if r.get("open") is False and not already_ordered:
        return "closed"
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


def build_candidates(client: MeicanClient, item: dict, max_price: float,
                     window_open: bool = True) -> list[DishCandidate]:
    tab = (item.get("userTab") or {}).get("uniqueId")
    tt = _target_time_str(item)
    # 是否已订餐决定 open=false 的含义，取餐次自带的 corpOrderUser
    ordered = bool(item.get("corpOrderUser"))
    out: list[DishCandidate] = []
    for r in client.restaurants(tab, tt):
        reason = restaurant_unavailable_reason(
            r, window_open=window_open, already_ordered=ordered)
        avail = not reason
        # 已订餐时服务端把 open 一律置 false，这个值只反映「我下过单」，
        # 不是餐厅状态。标成 None（未知）而不是存 false——否则写进全局
        # 共享缓存会污染其他未订餐的用户。
        raw_open = r.get("open") if not ordered else None
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
                                     restaurant_open=raw_open,
                                     restaurant_status=r.get("restaurantStatus"),
                                     unavailable_reason=reason))
    return out


# 当天可订菜品对所有同企业用户是固定的，拉一次全局共享缓存即可，
# 不必每个用户、每次推荐都重新打几十次餐厅菜单接口。
MENU_TTL = 6 * 3600        # 菜单缓存 6 小时（当天菜单基本不变）


# 缓存结构版本。v2 起不再存 available（用户态），改为存客观字段现算。
# 老版本条目读到时字段对不上，直接当未命中重拉，避免新旧混用。
MENU_CACHE_VERSION = 2


def _menu_cache_key(target_day: date, item: dict) -> str:
    corp = (item.get("userTab") or {}).get("corp") or {}
    ns = corp.get("namespace") or corp.get("uniqueId") or "default"
    return (f"meican:menu:v{MENU_CACHE_VERSION}:"
            f"{target_day.isoformat()}:{ns}:{_target_time_str(item)}")


def candidates_to_raw(cands: list[DishCandidate]) -> list[dict]:
    """序列化候选写进全局共享缓存。

    刻意**不存 available / unavailable_reason**：这两项依赖用户自己的状态
    （有没有订餐），而缓存是全企业共享的。存进去会让「已订餐」用户的结论
    污染「未订餐」用户。读取时由 apply_availability 现算。
    这里只保留 open 原始值，以及份数、餐厅自报状态等客观字段。
    """
    return [{"restaurant_name": c.restaurant_name, "restaurant_id": c.restaurant_id,
             "dish_id": c.dish_id, "dish_name": c.dish_name, "price": c.price,
             "dish_limit": c.dish_limit,
             "available_count": c.available_count,
             "restaurant_open": c.restaurant_open,
             "restaurant_status": c.restaurant_status}
            for c in cands]


def apply_availability(raw: list[dict], item: dict,
                       now: datetime | None = None) -> list[dict]:
    """按「是否已订餐 + 是否在订餐窗口」现算每道菜的可订状态。

    放在读取时算而不是写缓存时算，是因为缓存跨用户共享，而这两项都是
    用户/时刻相关的量。
    """
    window_open = within_order_window(item, now)
    ordered = bool(item.get("corpOrderUser"))
    out = []
    for c in raw:
        # restaurant_open 为 None 表示「服务端没给可信值」（拉缓存的用户
        # 当时已订餐，open 被整体置 false）。此时不要把 None 当 false——
        # 让 restaurant_unavailable_reason 走保守分支。
        r = {"open": c.get("restaurant_open"),
             "availableDishCount": c.get("available_count"),
             "restaurantStatus": c.get("restaurant_status")}
        reason = restaurant_unavailable_reason(
            r, window_open=window_open, already_ordered=ordered)
        out.append({**c, "available": not reason, "unavailable_reason": reason})
    return out


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

    raw = candidates_to_raw(build_candidates(
        client, item, max_price=0, window_open=within_order_window(item)))
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

    # 可订状态是用户/时刻相关的，缓存里只有客观字段，这里现算
    raw = apply_availability(raw, item)
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
        # 餐次未开放时给一句明确说明，前端在上方统一提示，
        # 而不是让每道菜都显示「已关闭」。
        "slot_reason": slot_unavailable_reason(item),
        "slot_note": slot_unavailable_message(
            target_day, item.get("status"), item.get("reason", "")),
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

    # 先判餐次是否开放。餐次没开时餐厅列表全为 open=false，
    # 若跳过这步会被误判成「所有餐厅都打烊/订满」。
    slot_msg = slot_unavailable_message(target_day, status.get("status"),
                                        status.get("reason", ""))
    if slot_msg:
        return JobResult(False, slot_msg, {"slot_status": status.get("status")})

    if dish is None:
        rec = recommend(payload, cache, target_day, limit=5, max_price=max_price)
        if rec.get("error"):
            return JobResult(False, rec["error"])
        cands = rec.get("candidates", [])
        # 自动挑菜只挑可订的（订满/打烊的跳过）
        avail = [c for c in cands if c.get("available", True)]
        if not avail:
            return JobResult(
                False,
                f"{target_day} 没有可订的菜品（{_unavailable_summary(cands)}）",
                {"candidates": len(cands)})
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

    # 已经订过餐，下面要先退旧单。餐次不可下单时必须先拦住——
    # 否则退了旧单又下不了新单，当天就没饭吃了。
    slot_msg = slot_unavailable_message(target_day, status.get("status"),
                                        status.get("reason", ""))
    if slot_msg:
        return JobResult(False, f"{slot_msg}，未更换（原订单保留）")

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