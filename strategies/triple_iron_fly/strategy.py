"""Triple iron fly -- rules to be defined. Placeholder that trades nothing."""
from __future__ import annotations

from core.strategy_base import Context, Strategy


class TripleIronFly(Strategy):
    def on_bar(self, ctx: Context) -> None:
        pass
