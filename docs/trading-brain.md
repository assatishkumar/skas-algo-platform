# Trading Brain (Obsidian + Claude Desktop)

The "trading brain" is a memory of every SKAS Algo run + decision, kept as Markdown in an **Obsidian
vault**, with **Claude Desktop** as the reasoning/chat surface. The app's only job is to **export** its
activity into the vault; Obsidian (Dataview) + Claude Desktop do the rest. Nothing is published — your
data stays in your files; only compact note text is read by Claude when *you* ask.

## 1. Point the app at a vault
Set the vault folder (any folder you'll open in Obsidian):

```bash
export SKAS_VAULT_PATH="$HOME/Documents/Trading Brain"   # or add SKAS_VAULT_PATH=... to .env
```

When unset, all vault export is a **no-op** (zero impact).

## 2. Generate it
```bash
skas-algo export-vault --backfill --scaffold
```
- `--backfill` — a **run-card** per existing run (`Runs/`)
- `--scaffold` — the home note, Dataview **Dashboards**, **Journal Index**, and **Recipes**

After this, the app keeps the vault fresh automatically:
- **backtest saved** / **deployment stopped** → a run-card (`persist_backtest` / `finalize_live_run`)
- **deploy** → a run-card + a `deploy` journal entry
- **flatten / manual order / pause-resume / stop** → an `intervene` / `lifecycle` journal entry
- **FibRet / Donchian screen** → a `screen` journal entry

Re-running `export-vault` is safe — it rewrites only the machine region (between
`<!-- skas:begin -->` / `<!-- skas:end -->`); anything below (your notes, Claude's Insight notes) is kept.

## 3. Vault layout
```
Trading Brain/
  Trading Brain.md          # home — links the dashboards + recipes
  Runs/                     # one run-card per run: frontmatter (strategy/mode/return/maxDD/win/regime/outcome) + synopsis
  Strategies/               # one note per strategy; Dataview lists its runs
  Journal/  <date>.md       # deploys / interventions / screens / lifecycle (append-only)
  Dashboards/               # Leaderboard, Consistency (backtest→paper→live), Regime, Journal Index
  Insights/                 # Claude Desktop writes these
  Recipes.md                # prompt recipes for Claude Desktop
```
Install the Obsidian **Dataview** community plugin so the dashboards render.

## 4. Connect Claude Desktop
Add the Obsidian MCP server (or the filesystem MCP) to Claude Desktop, pointed at the vault, e.g.
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "obsidian-vault": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/you/Documents/Trading Brain"]
    }
  }
}
```
(or the dedicated Obsidian MCP server with its API key). Restart Claude Desktop, then open `Recipes.md`
in the vault and paste a recipe — weekly digest, consistency check, post-mortem, recommendations, or
ad-hoc Q&A. Claude reads the run-cards/journal and writes Insight notes back into `Insights/`.

## What stays in the app
Live monitoring, the backtest engine and the screeners are unchanged — the brain only *consumes* their
output via the vault export. Real-time ticks are never exported; the vault gets lifecycle + EOD records.
