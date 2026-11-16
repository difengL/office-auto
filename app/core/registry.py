"""插件注册表。启动时把 features/ 下的实现注册进来。"""
from __future__ import annotations

from .feature import Feature

_FEATURES: dict[str, Feature] = {}


def register(feature: Feature) -> None:
    if feature.key in _FEATURES:
        raise ValueError(f"功能 key 重复：{feature.key}")
    _FEATURES[feature.key] = feature


def get(key: str) -> Feature | None:
    return _FEATURES.get(key)


def all() -> list[Feature]:
    return list(_FEATURES.values())


def clear() -> None:
    """仅供测试使用。"""
    _FEATURES.clear()