"""Every registered strategy must be documented — in the catalog AND in the app's Docs page.

Two doc surfaces rot silently when a strategy lands:
  * `docs/FEATURES.md` §3 — the plain-language catalog a fresh session (or the owner) reads
    to learn WHAT the platform does;
  * `web/src/pages/StrategiesPage.tsx` — the in-app `/docs` reference cards, the only
    strategy documentation a non-developer ever sees.
Nothing in the browser or the API fails when a card is missing; the strategy simply doesn't
exist as far as the docs are concerned (fair_value_calendar, happy_twins, intraday_straddle,
intraday_strangle_combo, put_condor, straddle_btst, asymmetric_premium_intra and
double_diagonal_calendar had all drifted out by 2026-08-20). This pins both.
"""

from __future__ import annotations

import re
from pathlib import Path

from skas_algo.strategies.registry import available

_ROOT = Path(__file__).resolve().parents[1]
_DOCS_PAGE = _ROOT / "web" / "src" / "pages" / "StrategiesPage.tsx"
_FEATURES = _ROOT / "docs" / "FEATURES.md"

# Registered, but deliberately NOT a card on /docs — these are tools, not strategies the
# owner picks and deploys:
#   custom_options / custom_equity — the Trade builders' user-authored one-off positions;
#   broker_smoke_test              — the real-order probe on the Brokers page;
#   donchian_strangle_bt           — the backtest sibling, documented inside its live card.
_NOT_A_CARD = {"custom_options", "custom_equity", "broker_smoke_test", "donchian_strangle_bt"}


def _card_ids() -> set[str]:
    return set(re.findall(r'^    id: "([\w\d_]+)"', _DOCS_PAGE.read_text(), re.M))


def _meta_keys() -> set[str]:
    body = _DOCS_PAGE.read_text().split("const META: Record<string, Meta> = {", 1)[1]
    return set(re.findall(r'^  "?([\w\d_]+)"?: \{$', body, re.M))


def test_every_strategy_has_a_docs_card() -> None:
    missing = sorted(s for s in available() if s not in _NOT_A_CARD and s not in _card_ids())
    assert not missing, (
        f"no /docs card for {missing} — add a STRATEGIES entry in {_DOCS_PAGE.name} "
        "(or list it in _NOT_A_CARD with the reason)"
    )


def test_every_card_has_presentation_meta() -> None:
    """A card without META falls into the 'Other' index group with a default bias pill."""
    missing = sorted(_card_ids() - _meta_keys())
    assert not missing, f"cards without a META entry (group/facts/deploy footer): {missing}"


def test_every_strategy_is_in_the_feature_catalog() -> None:
    text = _FEATURES.read_text()
    missing = sorted(s for s in available() if f"`{s}`" not in text)
    assert not missing, f"docs/FEATURES.md §3 never mentions {missing}"


def test_catalog_states_the_real_strategy_count() -> None:
    """The §3 preamble counts IDs and files — stale counts are the tell that it wasn't updated."""
    m = re.search(r"registry\.py` \((\d+) IDs across (\d+) files\)", _FEATURES.read_text())
    assert m, "docs/FEATURES.md §3 no longer states the strategy count"
    registry_py = (_ROOT / "src" / "skas_algo" / "strategies" / "registry.py").read_text()
    n_files = len(set(re.findall(r"^from \.(\w+) import", registry_py, re.M)))
    assert (int(m.group(1)), int(m.group(2))) == (len(available()), n_files)
