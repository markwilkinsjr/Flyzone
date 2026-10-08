# Flyzone — XSP Butterfly Bot

An automated XSP (Mini-S&P 500) butterfly bot for Tastytrade, with Telegram
alerts and commands. It is built to run in the Tastytrade **sandbox** first.

> **Status: sandbox / paper testing only.** Nothing here has been run against
> live Tastytrade servers yet. Start with `DRY_RUN=true`, then sandbox orders,
> and only consider real money after weeks of clean sandbox results.

---

## The strategy in one page

### 1. VIX gate (8:31 AM CT) picks the day's structure

| VIX | Regime | Trade | Wings | Max debit |
|---|---|---|---|---|
| under 17 | Zombieland | 1-DTE **Batman** at 2:00 PM | 2 pt | $0.20 |
| 17 to 27.99 | Normal | 0-DTE **single fly** at 9:00 AM | 3 pt | $0.30 |
| 28 and up | Chaos | 0-DTE **single fly** at 9:00 AM | 4 pt | $0.40 |

The max debit is always 10% of the wing width (about 1 to 9 risk/reward).

### 2. Single fly (VIX 17+): directional gate at 9:00 AM

Uses the 0-DTE XSP gamma levels and the **RQ SWMA Momentum SuperTrend**
(5-minute bars, Intraday preset) with its momentum gate active:

| Spot vs zero gamma | SuperTrend | Condition | Trade |
|---|---|---|---|
| above | bullish | A | Call fly at the Call Wall |
| above | bearish | B | Put fly at the nearer of Zero Gamma / Put Wall |
| below | bearish | C | Put fly at the Put Wall |
| below | bullish | D | Stand by, no trade |

Then:
- Abort if spot is already inside the tent.
- Abort if the probability of touch of the nearest long strike is under 67%.
- If the fly costs more than the cap, shift one strike at a time away from
  spot until it fits. The 67% gate limits how far it can shift.
- One Day limit order at the cap. Cancelled if unfilled at **10:00 AM**.

### 3. Batman (VIX under 17): price rules only, 2:00 PM

- A call fly near the Call Wall and a put fly near the Put Wall, on the
  next trading day's expiration. Gamma levels come from that expiration.
- Each wing is chosen to price between **$0.15 and $0.20**, with its centre
  strike out of the money and within 1 standard deviation of spot.
- Both wings start at $0.15. Once one fills, the other walks up $0.01 a
  minute to $0.20.
- Unfilled wings are cancelled at **2:45 PM**. A filled wing is kept alone.
- The next day gets no new trade.

### 4. Exits — winners only

Losers are never touched; they expire. All exits are market orders.

1. **+100% floor.** When a wing's value reaches 2x the entry debit, a floor
   locks at exactly 2x. It never moves down.
2. **2:15 PM trail** (expiration day only). The floor ratchets to 20% below
   the peak once that level is above entry. You also get a Telegram
   recommendation for each open wing.
   Example: entry $0.30, peak $0.90, floor $0.72. Peak $1.10, floor $0.88.
3. **Inside the tent, never close.** A pin is the best outcome.
4. Once spot leaves the tent: close if value is at or below the floor, or if
   the trade is still a winner.

Value means the mid price of the fly, which is what Tastytrade shows as P&L.
A market sell fills nearer the bid side, so realised exits can land a little
below the floor.

### 5. Other rules

- One position set at a time. 1 contract per fly.
- Hourly gamma report at :00 from 9 AM to 2 PM, with a warning (alert only)
  when spot is outside a wing's tent in negative gamma.
- No new trades on market holidays or early-close days.

---

## Files

| File | What it does |
|---|---|
| `config.py` | Loads `.env`, and holds every strategy number in one place |
| `tasty_client.py` | Tastytrade sessions, market data stream, order routing with sandbox guard |
| `gex_engine.py` | Gamma exposure, walls, zero-gamma line, probability of touch |
| `indicators/supertrend.py` | Python port of the RQ SWMA Momentum SuperTrend (Pine source alongside) |
| `strategy.py` | VIX gate, directional gate, strike selection |
| `risk_manager.py` | Fly valuation, +100% floor, 2:15 trail, hold-in-tent rule |
| `notifier.py` | Telegram messages and commands |
| `state.py` | Crash-safe `state.json` so restarts pick up where they left off |
| `market_calendar.py` | Holidays, early closes, expirations |
| `main.py` | The scheduler and main loop |
| `tests/` | 35 unit and end-to-end tests with fake market data |

---

## Setup on your PC

### 1. Install Python 3.11 or newer

Download it from python.org. On Windows, tick **"Add Python to PATH"**.

### 2. Get the code and install packages

```bash
git clone https://github.com/markwilkinsjr/Flyzone.git
cd Flyzone
python -m venv .venv
# Windows:      .venv\Scripts\activate
# Mac / Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Create your Telegram bot

1. In Telegram, message **@BotFather**, send `/newbot`, and follow the steps.
   It gives you a bot token.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
   Your chat ID is the number under `"chat":{"id": ...}`.

### 4. Fill in `.env`

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Open `.env` and fill in:
- Sandbox client secret, refresh token and account number.
- Production client secret and refresh token. These are used **only to read
  market data**. The bot never places orders with them.
- Telegram bot token and chat ID.
- Leave `DRY_RUN=true` and `TASTYTRADE_IS_SANDBOX=true` for now.

Never commit `.env`. It is already in `.gitignore`.

### 5. Run the tests

```bash
pytest
```

### 6. Check your connections (no trading)

```bash
python main.py --check
```

This logs in to both accounts, reads VIX, builds a gamma report, computes
the SuperTrend, prints everything, and sends one Telegram message. Compare
the walls and trend with your own charts before going further.

### 7. Run the bot

```bash
python main.py
```

Leave the window open during market hours. Stop it with Ctrl+C.

Keep the PC from sleeping between 8:15 AM and 3:15 PM CT
(Windows: Settings > System > Power > Sleep = Never).

---

## Testing plan

1. **Dry run, 1–2 weeks** (`DRY_RUN=true`). Real data, every alert, fills
   simulated from live quotes. No orders reach Tastytrade.
2. **Sandbox orders** (`DRY_RUN=false`, `TASTYTRADE_IS_SANDBOX=true`). Real
   orders to the sandbox. Sandbox fill prices are fake, so the bot books
   entries at your limit and exits at the live model price.
3. **Live** only later, which needs `TASTYTRADE_IS_SANDBOX=false`,
   `TASTYTRADE_LIVE_ACCOUNT_NUMBER`, and
   `I_UNDERSTAND_THIS_IS_LIVE_TRADING=yes`.

---

## Telegram commands

| Command | Effect |
|---|---|
| `/status` | Open wings, current value, peak, floor, today's plan |
| `/pause` | No new entries; open positions are still managed |
| `/resume` | Allow new entries again |
| `/flatten` then `/flatten CONFIRM` | Close everything at market, cancel working orders |

Only your chat ID can control the bot.

---

## Things to know

- **Restarts are safe.** State lives in `state.json`. On startup the bot
  compares it with your Tastytrade positions. If they differ, it pauses new
  entries and messages you.
- **Hold-zone switch.** `HOLD_ZONE` in `risk_manager.py` is `"tent"` by
  default, so the floor and trail only act after spot leaves the tent. Set
  it to `"core"` to let them act anywhere outside the middle half of the
  tent.
- **Logs** go to `flyzone.log` in the project folder.
