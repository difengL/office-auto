"""基于历史订单推测口味偏好，并对当日菜单打分排序。

思路（刻意保持简单可解释）：
1. 从历史订单提取「餐厅频次」和「菜品关键词权重」
2. 对当日每个候选套餐打分 = 餐厅分 + 菜品关键词分 - 近期吃过的惩罚
3. 输出 top N，并给出可读的推荐理由
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

# 常见食材/口味词，用于从菜名里抽取偏好特征
TASTE_KEYWORDS = [
    "鸡", "牛", "猪", "排骨", "鸭", "鱼", "虾", "羊", "肉",
    "面", "饭", "粉", "包", "粥", "饺", "饼", "汉堡", "堡", "披萨",
    "辣", "麻", "酸", "甜", "咖喱", "芝士", "烤", "炸", "炖", "焖", "炒",
    "水果", "整果", "沙拉", "素食", "蔬菜", "汤",
    "麻辣", "香辣", "酸辣", "红烧", "照烧", "黑胡椒", "蒜香",
]

# 名称里的杂质词，抽取前先剔除
NOISE = [
    "百融云创专供", "专供", "套餐", "配", "含", "克", "g", "G", "ml",
]

PUNCT_RE = re.compile(r"[()（）/&、+·,，。\s]")


@dataclass
class DishCandidate:
    restaurant_name: str
    restaurant_id: str
    dish_id: int | str
    dish_name: str
    price: float
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    available: bool = True        # 餐厅是否可订（订满/打烊为 False，前端置灰）
    dish_limit: int | None = None      # 餐厅总份数上限
    available_count: int | None = None  # 剩余可订份数
    # 餐厅下发的原始状态字段，供缓存复用与后续重算。
    # open 在「已订餐」时会被服务端一律置 false，那不代表餐厅打烊，
    # 此时记为 None（未知），避免写入共享缓存污染其他用户。
    restaurant_open: bool | None = None
    restaurant_status: int | None = None
    # 不可订的具体原因，与美餐前端的两套文案一致：
    #   sold_out → restaurant_status_exceed_limit「已超过餐厅的点餐限额」
    #   closed   → restaurant_status_close「该餐厅已经关闭点餐」
    # 空字符串表示可订。分开保存才能给出准确提示，而不是笼统说「订满」。
    unavailable_reason: str = ""


def _clean(name: str) -> str:
    s = name
    for n in NOISE:
        s = s.replace(n, "")
    s = re.sub(r"\d+", "", s)
    return PUNCT_RE.sub(" ", s).strip()


def extract_keywords(name: str) -> list[str]:
    cleaned = _clean(name)
    return [kw for kw in TASTE_KEYWORDS if kw in cleaned]


def _bigrams(s: str) -> set[str]:
    s = _clean(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def name_similarity(a: str, b: str) -> float:
    """菜名相似度（字符二元组 Jaccard）。

    比关键词匹配更能区分「拌面/汤面/盖饭」这类同大类不同做法的菜，
    也更贴近「上次吃的那个我再点一次类似的」这种真实偏好。
    """
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    inter = len(ba & bb)
    union = len(ba | bb)
    return inter / union if union else 0.0


@dataclass
class PreferenceModel:
    restaurant_counts: Counter = field(default_factory=Counter)
    keyword_counts: Counter = field(default_factory=Counter)
    recent_dishes: list[tuple[date, str]] = field(default_factory=list)
    sample_days: int = 0

    @classmethod
    def from_history(cls, records: list[dict]) -> "PreferenceModel":
        """records: [{"date": "2026-11-16", "restaurant": "...", "dish": "..."}]"""
        m = cls()
        days = set()
        for rec in records:
            d = rec.get("date")
            try:
                day = date.fromisoformat(d) if isinstance(d, str) else d
            except ValueError:
                continue
            days.add(day)
            m.restaurant_counts[rec.get("restaurant", "")] += 1
            for kw in extract_keywords(rec.get("dish", "")):
                m.keyword_counts[kw] += 1
            m.recent_dishes.append((day, rec.get("dish", "")))
        m.sample_days = len(days)
        m.recent_dishes.sort(key=lambda x: x[0], reverse=True)
        return m

    def score(self, cand: DishCandidate, target_day: date) -> DishCandidate:
        reasons: list[str] = []
        total = 0.0

        # 餐厅偏好：历史点过的店加权（最多 +6）
        rc = self.restaurant_counts.get(cand.restaurant_name, 0)
        if rc:
            pts = min(rc * 1.2, 6.0)
            total += pts
            reasons.append(f"这家店你点过 {rc} 次")

        # 主信号：与历史吃过的菜名相似度（带时间衰减）
        # 这是最贴近真实口味的信号——「上次吃的那个类似的」
        best_sim = 0.0
        best_dish = ""
        best_gap = 999
        for d, past_dish in self.recent_dishes[:20]:
            gap = (target_day - d).days
            if gap < 0:
                continue
            sim = name_similarity(cand.dish_name, past_dish)
            decay = 0.5 ** (gap / 14.0)      # 14 天半衰期
            weighted = sim * decay
            if weighted > best_sim:
                best_sim, best_dish, best_gap = weighted, past_dish, gap
        if best_sim > 0:
            pts = best_sim * 8.0
            total += pts
            reasons.append(f"与「{best_dish[:14]}…」相似（{best_gap} 天前）")

        # 次要信号：食材/口味关键词（归一化，避免「肉/饭」霸榜）
        kws = extract_keywords(cand.dish_name)
        total_kw = sum(self.keyword_counts.values()) or 1
        kw_pts = 0.0
        hit_kws: list[tuple[str, int]] = []
        for kw in kws:
            c = self.keyword_counts.get(kw, 0)
            if c:
                kw_pts += (c / total_kw) ** 0.5 * 3.0
                hit_kws.append((kw, c))
        kw_pts = min(kw_pts, 3.0)
        if kw_pts:
            total += kw_pts
            top = sorted(hit_kws, key=lambda x: x[1], reverse=True)[:2]
            reasons.append("口味：" + "、".join(f"{k}×{c}" for k, c in top))

        # 近期吃过的同款/高度近似 → 降权，避免连续重复
        for d, past_dish in self.recent_dishes[:10]:
            gap = (target_day - d).days
            if gap < 0:
                continue
            if past_dish == cand.dish_name:
                if gap <= 3:
                    total -= 12.0
                    reasons.append(f"{gap} 天前刚吃过同款，降权")
                elif gap <= 7:
                    total -= 5.0
                    reasons.append(f"{gap} 天前吃过同款")
                break
            sim = name_similarity(cand.dish_name, past_dish)
            if sim >= 0.55 and gap <= 5:
                total -= 5.0
                reasons.append(f"{gap} 天前吃过相近的，降权")
                break

        cand.score = round(total, 3)
        cand.reasons = reasons or ["暂无历史偏好数据，按默认推荐"]
        return cand


def rank_candidates(cands: list[DishCandidate], target_day: date,
                    model: PreferenceModel, limit: int = 5) -> list[DishCandidate]:
    scored = [model.score(c, target_day) for c in cands]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:limit]


def summarize(model: PreferenceModel) -> dict:
    return {
        "sample_days": model.sample_days,
        "top_restaurants": model.restaurant_counts.most_common(5),
        "top_keywords": model.keyword_counts.most_common(8),
        "recent": [(d.isoformat(), name) for d, name in model.recent_dishes[:10]],
    }
