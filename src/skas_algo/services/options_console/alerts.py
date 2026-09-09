"""Armed levels, shared by the replay session and the live console.

An alert fires ONCE at a minute and holds it; in a replay the cursor decides whether that
minute is the past (fired) or not yet (armed), so a rewind re-arms it. Live there is no
rewind, so a fired alert stays fired until cleared."""

from __future__ import annotations

KINDS = ("target", "stop", "delta", "above", "below")


class AlertBook:
    alerts: list[dict]
    _alert_seq: int

    def _init_alerts(self) -> None:
        self.alerts = []
        self._alert_seq = 0

    def arm_alert(self, kind: str, value: float, *, note: str | None = None) -> dict:
        """``target``/``stop`` are rupees of TOTAL MTM (stop is a loss, given as a positive
        number); ``delta`` is |net Δ| in units; ``above``/``below`` are spot."""
        if kind not in KINDS:
            raise ValueError(f"unknown alert kind {kind!r}")
        self._alert_seq += 1
        a = {"id": f"A{self._alert_seq}", "kind": kind, "value": float(value),
             "note": note, "fired_at": None, "fired_value": None}
        self.alerts.append(a)
        return a

    def clear_alert(self, alert_id: str) -> bool:
        before = len(self.alerts)
        self.alerts = [a for a in self.alerts if a["id"] != alert_id]
        return len(self.alerts) < before

    def _evaluate_alerts(self, now: str, mtm: float, net_delta: float | None,
                         spot: float | None, *, rewindable: bool = True) -> None:
        for a in self.alerts:
            if rewindable and a["fired_at"] and a["fired_at"] > now:
                a["fired_at"], a["fired_value"] = None, None     # rewound past it
            if a["fired_at"]:
                continue
            k, v = a["kind"], a["value"]
            hit = ((k == "target" and mtm >= v)
                   or (k == "stop" and mtm <= -abs(v))
                   or (k == "delta" and net_delta is not None and abs(net_delta) >= v)
                   or (k == "above" and spot is not None and spot >= v)
                   or (k == "below" and spot is not None and spot <= v))
            if hit:
                a["fired_at"] = now
                a["fired_value"] = round(mtm if k in ("target", "stop")
                                         else (net_delta if k == "delta" else spot) or 0.0, 2)

    def _alerts_out(self) -> list[dict]:
        return [{**a, "state": "fired" if a["fired_at"] else "armed"} for a in self.alerts]
