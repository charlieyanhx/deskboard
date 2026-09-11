"""Replay a recorded session file through the bus at N× speed (0 = as fast as possible).

The file is a parquet/CSV of events with columns `ts, topic, payload` (payload JSON) — the
same shape a live connector would publish, so everything downstream is identical in
replay and live. Ordering is by (ts, seq-in-file); wall-clock sleeps only pace delivery.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pandas as pd

from ..bus import Bus


def load_session(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    df = df.reset_index(drop=True)
    df["_i"] = range(len(df))
    return df.sort_values(["ts", "_i"], kind="stable").drop(columns="_i").reset_index(drop=True)


def save_session(df: pd.DataFrame, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    (df.to_parquet if path.suffix == ".parquet" else df.to_csv)(path, index=False)


class ReplayFeed:
    def __init__(self, bus: Bus, session: pd.DataFrame, speed: float = 0.0):
        self.bus, self.df, self.speed = bus, session, speed
        self.position = 0
        self.done = asyncio.Event()

    async def run(self) -> None:
        prev_ts = None
        t_wall = time.perf_counter()
        for row in self.df.itertuples(index=False):
            ts = float(row.ts)
            if self.speed > 0 and prev_ts is not None:
                target = t_wall + (ts - prev_ts) / self.speed
                delay = target - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                t_wall = max(target, time.perf_counter())
            elif self.speed == 0 and self.position % 500 == 0:
                await asyncio.sleep(0)  # let the UI breathe
            payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
            await self.bus.publish(row.topic, ts, payload)
            prev_ts = ts
            self.position += 1
        self.done.set()
