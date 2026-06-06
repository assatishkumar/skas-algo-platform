"""Coerce numpy scalars to native Python types for JSON serialization.

Market data from skas-data arrives as numpy float32 (and int64), which json.dumps
cannot serialize (only numpy float64 happens to subclass float). Reports and trade
logs are persisted to JSON columns and returned over the API, so we normalize them
to native Python types at that boundary.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def to_native(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_native(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    return obj
