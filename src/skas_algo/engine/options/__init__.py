"""Options support for the engine: instrument model, pricing/greeks, chain, settlement, margin.

Everything option-specific lives here so the equity engine stays untouched. The
project invariant — backtest == forward-test == live from one engine, with the SST
equity parity tests byte-identical — is preserved by gating every new path on an
option symbol (``instrument.parse`` returns ``None`` for ordinary tickers) or a short
position (``Lot.direction == -1``).
"""
