"""
cs_signal.py — Shared signal dataclass returned by all CS strategies.

Every CSSignal carries all information needed to open a trade and log it
in the trade record. No lookahead data may be stored here.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class CSSignal:
    strategy: str           # "CS1" | "CS2" | "CS3" | "CS4"
    direction: str          # "LONG" | "SHORT"
    entry_price: float
    sl_price: float

    # TP candidates list — TPCandidate objects from tp_engine
    tp_candidates: list = field(default_factory=list)

    # Modification tracking (max ONE per trade)
    modification_type: str = "NONE"

    # Context recorded for logging
    setup_context: str = ""      # human-readable description of what triggered this
    bar_index: int = -1
