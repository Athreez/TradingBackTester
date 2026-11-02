"""Local Parquet cache so backtests never hit the API.

data/cache/
  candles/<interval>/<EXCHANGE>/<symbol>.parquet
  contracts/<UNDERLYING>/<YYYY-MM-DD>.parquet
  index_codes.json
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from core.config import ROOT

CACHE_DIR = ROOT / "data" / "cache"


class DataStore:
    def __init__(self, root: Path = CACHE_DIR):
        self.root = root

    # ---- candles --------------------------------------------------------

    def _candle_path(self, interval: str, exchange: str, symbol: str) -> Path:
        return self.root / "candles" / interval / exchange / f"{symbol}.parquet"

    def save_candles(self, interval: str, exchange: str, symbol: str, df: pd.DataFrame) -> None:
        if df.empty:
            return
        path = self._candle_path(interval, exchange, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            df = pd.concat([pd.read_parquet(path), df])
            df = df[~df.index.duplicated(keep="last")]
        df.sort_index().to_parquet(path)

    def load_candles(self, interval: str, exchange: str, symbol: str) -> pd.DataFrame:
        path = self._candle_path(interval, exchange, symbol)
        if not path.exists():
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                                index=pd.DatetimeIndex([], name="ts"))
        return pd.read_parquet(path)

    def has_candles(self, interval: str, exchange: str, symbol: str) -> bool:
        return self._candle_path(interval, exchange, symbol).exists()

    # ---- contracts ------------------------------------------------------

    def _contracts_path(self, underlying: str, expiry: date) -> Path:
        return self.root / "contracts" / underlying / f"{expiry.isoformat()}.parquet"

    def save_contracts(self, underlying: str, expiry: date, df: pd.DataFrame) -> None:
        path = self._contracts_path(underlying, expiry)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path)

    def load_contracts(self, underlying: str, expiry: date) -> pd.DataFrame:
        path = self._contracts_path(underlying, expiry)
        return pd.read_parquet(path) if path.exists() else pd.DataFrame()

    def expiries(self, underlying: str) -> list[date]:
        folder = self.root / "contracts" / underlying
        if not folder.exists():
            return []
        return sorted(date.fromisoformat(p.stem) for p in folder.glob("*.parquet"))

    # ---- index scrip codes ---------------------------------------------

    def _codes_path(self) -> Path:
        return self.root / "index_codes.json"

    def get_index_code(self, underlying: str) -> str | None:
        path = self._codes_path()
        return json.loads(path.read_text()).get(underlying) if path.exists() else None

    def set_index_code(self, underlying: str, code: str) -> None:
        path = self._codes_path()
        codes = json.loads(path.read_text()) if path.exists() else {}
        codes[underlying] = code
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(codes, indent=2))
