# Publishing checklist

This repository runs real money and its working notes are operational — and it IS public.
The lines below are what keeps that safe; the guard test fails the suite if an identifier
comes back.

## Blockers (either route)

- [ ] **The sibling `skas-data` package is a hard import.** A visitor cannot run anything
      without it. Either publish it too (after its own secret scrub: Kite credentials were
      committed there in the past) or ship a stub provider for the demo.
- [x] **Operational identifiers in the working notes** — scrubbed from the files on
      2026-09-22 (the VPS IP, the Mac's Tailscale address and tailnet, account labels, a person's
      name, a home-directory path) and guarded by `tests/test_no_identifiers.py`. They remain in
      OLD commits; the owner accepted that (all VPS ports are closed from the internet, Tailscale
      addresses are unroutable, the keys once exposed were rotated).
- [x] **Git history** — scanned 2026-09-22: no secret-shaped value in any commit; `.env`,
      the database and backups were never tracked.
- [ ] **Run-specific notes.** The docs quote real P&L, run numbers and account behaviour.
      Fine as anonymised examples in the showcase documents; remove from anything published
      verbatim.
- [ ] **Screenshots.** The eight in `docs/showcase/img/` were taken on the Mac (paper box).
      `research.png` was retaken on the demo box (no broker session in the dropdown);
      `live-paper-fleet.png` is the PAPER fleet with paper rupees and the caption says so. Never
      publish a Live-page shot from the production box.

## If a separate showcase repository is ever made

- [ ] The root `README.md` as is (image paths already point at `docs/showcase/img/`).
- [ ] `docs/design-decisions.md`, `docs/lessons-from-live.md`.
- [ ] `docs/ARCHITECTURE.md` after the scrub (it is the best long document in the repo).
- [ ] Selected source with tests: `engine/execution.py` and the parity tests,
      `brokers/live_broker.py` and `tests/test_live_broker.py`, `services/intraday_replay.py`,
      `services/replay_market.py`, `services/options_console/`, two or three strategies.
- [ ] `scripts/demo.sh`, `services/demo_seed.py`, and the web build so the demo runs.
- [ ] A licence. The code is single-operator by design; say so, and say it is not advice.
- [ ] A 20-second console GIF at the top of the README (build a straddle, step the cursor,
      roll a leg). Record on the demo box.

## LinkedIn

- [ ] One post per idea (drafts in `docs/private/`, never committed), each with one image and one number.
- [ ] No account P&L, no rupee figures from a personal account, no broker screenshots with
      balances.
- [ ] Say plainly that the platform is for a single operator and that nothing in it is a
      recommendation.
