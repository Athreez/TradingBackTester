"""Discover and load strategies from strategies/<name>/."""
from __future__ import annotations

import importlib
import inspect
from typing import Any

import yaml

from core.config import ROOT
from core.strategy_base import Strategy

STRATEGIES_DIR = ROOT / "strategies"


def available_strategies() -> list[str]:
    return sorted(
        p.name for p in STRATEGIES_DIR.iterdir()
        if p.is_dir() and (p / "strategy.py").exists()
    )


def load_config(name: str) -> dict[str, Any]:
    path = STRATEGIES_DIR / name / "config.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def load_strategy(name: str, overrides: dict[str, Any] | None = None) -> tuple[Strategy, dict[str, Any]]:
    if name not in available_strategies():
        raise ValueError(f"Unknown strategy '{name}'. Available: {available_strategies()}")
    module = importlib.import_module(f"strategies.{name}.strategy")
    classes = [
        c for _, c in inspect.getmembers(module, inspect.isclass)
        if issubclass(c, Strategy) and c is not Strategy and c.__module__ == module.__name__
    ]
    if len(classes) != 1:
        raise ValueError(f"strategies/{name}/strategy.py must define exactly one Strategy subclass")
    config = load_config(name)
    params = {**config.get("params", {}), **(overrides or {})}
    return classes[0](params), config
