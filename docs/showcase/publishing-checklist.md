# Publishing checklist

This repository runs real money and its working notes are operational. Nothing here goes
public until every line below is done. The recommended route is a separate showcase
repository built from `docs/showcase/` plus selected modules; the second route, making this
repository public, needs the same scrub applied to the full history.

## Blockers (either route)

- [ ] **The sibling `skas-data` package is a hard import.** A visitor cannot run anything
      without it. Either publish it too (after its own secret scrub: Kite credentials were
      committed there in the past) or ship a stub provider for the demo.
- [ ] **Operational identifiers in the working notes.** `CLAUDE.md`, `docs/DEPLOY.md`,
      `docs/ARCHITECTURE.md` and the FEATURES catalog name the VPS public IP, the Tailscale
      address, the hostname, broker account labels and people. Strip or generalise every one.
- [ ] **Git history.** `git log -p` grep for `api_key`, `access_token`, `secret`, `password`,
      `Bearer `, phone numbers and the IPs above. `.env` is ignored, but check that it never
      landed in an early commit. If anything is found, the showcase route is the only safe one;
      do not rewrite and force-push a repository other machines pull from.
- [ ] **Run-specific notes.** The docs quote real P&L, run numbers and account behaviour.
      Fine as anonymised examples in the showcase documents; remove from anything published
      verbatim.
- [ ] **Screenshots.** The eight in `docs/showcase/img/` were taken on the Mac (paper box).
      Two need a decision before publishing: `research.png` shows a broker-session label in
      the "Compare BS vs market" dropdown (crop or blur it), and `live-paper-fleet.png` is the
      PAPER fleet with paper rupees (the caption says so; keep it that way or drop it). Never
      publish a Live-page shot from the production box.

## Showcase repository contents

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
