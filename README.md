# Penultimate

An automatic crypto trading bot. It runs on its own, inside hard limits you set once.

## Start it

**On a Mac:** double-click **Penultimate** (the app in this folder), or `Start Penultimate.command`. The first start sets itself up (about a minute), then the dashboard opens in your browser at http://127.0.0.1:8787.

After that it runs in the background: it starts again when you log in, restarts itself if it stops, and keeps the Mac from idle-sleeping. A closed lid on battery still sleeps the Mac, so for true round-the-clock running put it on a small cloud server with `Penultimate server setup.txt`. `Stop Penultimate.command` turns the background service off.

It starts on **real prices with fake money** ($500 practice balance). Nothing real is touched.

## Going live (the four ticks on the dashboard)

1. **Real prices connected** – automatic.
2. **API key saved** – create it on Coinbase yourself (trading allowed, transfers off) and paste the downloaded key file into the dashboard yourself.
3. **Live mode on** – click "Go live with real money" and type GO LIVE.
4. **Dollars deposited** – deposit on Coinbase. The bot checks the balance every couple of seconds and starts by itself when the money arrives.

The bot's whole budget is `max_gross_exposure_usd` in `limits.yaml` ($500). It never has more than that working, even if the account holds more.

## What is automatic

- Reading prices, placing and cancelling orders, taking profit, stopping out.
- Cutting size or pausing when the market panics, the spread blows out, or prices stop arriving.
- Selling everything and halting when the daily loss cap is hit.
- Restarting itself after an error, and picking up where it left off after a restart.
- Grading tracked forecaster calls at their deadlines (real prices only).

## What stays manual

- Resuming after a halt.
- Changing `limits.yaml`.
- Going live.
- Taking money off the exchange. The bot contains no withdrawal code.

## Files

| File | What it is |
|---|---|
| `limits.yaml` | The hard caps. The bot cannot change them. |
| `config.yaml` | Mode, exchange, strategy settings. |
| `data/` | The log database, saved keys, crash log. |
| `data/signals.json` | Optional. Write `{"risk_off": true, "note": "why"}` here and the bot cuts size to a quarter. This is the hook for outside alerts (bond volatility, AI-stock drops, hack alerts). |
| `Penultimate server setup.txt` | A short text to paste into a cloud server's startup-script box. The server downloads the bot from its GitHub repository, runs it 24/7, keeps it up to date, and is reachable only from your own devices (Tailscale). |
| `penultimate/lab.py` | Strategy lab, test only. Every few seconds it tests 48 variants of the resting-bid strategy on real past prices, keeps the best 10, mutates them and tests again. It never trades and never changes the live settings. The dashboard shows the family tree, every variant's running result, each individual trade path, and a 3D map of everything tried so far (floor: two settings; height: the result on unseen history). What it has learned is kept in `data/lab.json`, so a restart does not lose it. The number that matters is the result on history the lab never trained on. |
| `penultimate/updater.py` | On a server: checks the repository for new versions. On fake money they install by themselves; with real money they wait for your click. Never touches `config.yaml`, `limits.yaml` or `data/`. |
| `deploy/server-setup.sh`, `deploy/install-server.sh` | What the server runs on first boot: install, start on boot, private access, firewall. |
| `tests/` | 60 automatic checks: the money-protecting parts, plus one that runs every part together and checks each hands the next what it expects. |

## Honest status

- **Tested:** accounting, every hard limit, every automatic reaction, restart recovery, the dashboard controls, and the live-order logic against a stand-in exchange.
- **Not yet tested:** anything against a real exchange. The build environment has no exchange access. Stage 2 is where that gets proven on your machine, and the bot runs a read-only self-check before it will trade live.
- **The two starter strategies are plumbing, not a proven edge.** On the practice market with a $500 balance at Coinbase-level fees they lost money in 8 of 10 two-week runs (about $19, or 4%, on average). Treat real-price practice results as the evidence that matters.
- **The funding trade is switched off in live mode** until the futures leg is verified on your exchange (`allow_live` in `config.yaml`).
- **Speed:** this version reacts in fractions of a second, not microseconds. That suits the slower strategies; pure speed races are not what it is for.

## Security

- The dashboard listens on this computer only and answers only to its own address (`127.0.0.1`, `localhost`, a plain IP, `penultimate` or a Tailscale name). A request addressed to any other website name is refused, other pages cannot embed it, and every command needs the page's session token.
- The exchange key is stored in `data/keys.json`, created readable by you only. It is never shown by the dashboard or written to logs. Give it trading permission only; the bot contains no code that withdraws.
- Updates on a server come only from this GitHub repository, are unpacked file by file (plain files only), must compile and import before they replace anything, and wait for a click once real money is on. Whoever can push to the repository can change what the server runs, so protect that GitHub account with two-factor sign-in.
- The forecast model's weights are pinned to exact published versions.
- Checked with `bandit` (code) and `pip-audit` (dependencies); `tests/test_dashboard_security.py` covers the points above.
