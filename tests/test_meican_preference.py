from datetime import date

from app.features.meican.preference import (DishCandidate, PreferenceModel,
                                            name_similarity, rank_candidates)


def test_name_similarity_identical_is_one():
    assert name_similarity("香酥小烤鸭套餐", "香酥小烤鸭套餐") == 1.0


def test_name_similarity_unrelated_is_low():
    assert name_similarity("香酥小烤鸭套餐", "糙米罗勒烤鸡胸碗") < 0.3


def test_model_prefers_similar_history():
    model = PreferenceModel.from_history([
        {"date": "2026-11-16", "restaurant": "金百万", "dish": "烤鸭套餐"},
        {"date": "2026-11-10", "restaurant": "陕味食族", "dish": "油泼面"},
    ])
    day = date(2026, 11, 17)
    duck = DishCandidate("金百万", "r1", 1, "香酥小烤鸭套餐", 25.0)
    noodle = DishCandidate("祐禾", "r2", 2, "半个牛奶吐司套餐", 25.0)
    model.score(duck, day)
    model.score(noodle, day)
    assert duck.score > noodle.score


def test_recent_duplicate_is_penalized():
    model = PreferenceModel.from_history([
        {"date": "2026-11-16", "restaurant": "金百万", "dish": "烤鸭套餐"},
    ])
    day = date(2026, 11, 17)
    same = DishCandidate("金百万", "r1", 1, "烤鸭套餐", 25.0)
    model.score(same, day)
    assert same.score < 0          # 1 天前刚吃过，重罚


def test_rank_returns_limit():
    model = PreferenceModel.from_history([])
    cands = [DishCandidate("A", "1", i, f"菜品{i}", 25.0) for i in range(10)]
    top = rank_candidates(cands, date(2026, 11, 17), model, limit=3)
    assert len(top) == 3