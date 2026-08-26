<div align="center">

# 🔎 AmiSearch

**Self-hosted price tracking and restock alerts for AmiAmi — with alerts that actually arrive in time.**

[![Build and publish](https://github.com/bejak1999/ami_search/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/bejak1999/ami_search/actions/workflows/docker-publish.yml)
[![GHCR](https://img.shields.io/badge/ghcr.io-bejak1999%2Fami__search-2496ED?logo=docker&logoColor=white)](https://github.com/bejak1999/ami_search/pkgs/container/ami_search)
[![License: MIT](https://img.shields.io/badge/License-MIT-f97316.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![React 18](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black)](https://react.dev/)

```bash
docker run -d --name amisearch -p 8080:8080 -v ./data:/data ghcr.io/bejak1999/ami_search:latest
```

</div>

---

## 🧭 What this is

Pre-owned figures on AmiAmi sell in minutes, and a listing that sells is **deleted**, not marked sold out. If your alert arrives ten minutes late, the item was never really available to you.

AmiSearch polls AmiAmi as fast as is reasonable, diffs every result against what it saw last time, and pushes the moment something genuinely changes — to Telegram, your browser, ntfy, Discord, Gotify or e-mail.

It also answers the question the shop price does not: **what will this actually cost me once shipping, customs duty and import VAT are added?** You can set a watch to trigger on that number instead of the sticker price.

## ✨ Features

### 🔔 Alerts that arrive in time
- **Six notification channels** — Telegram, browser push, ntfy, Discord, Gotify, e-mail, plus a generic webhook
- **Adaptive polling** — down to 15 s per watch, automatically speeding up when a match nears your target and slowing down when nothing moves
- **No day-one flood** — the first check of a new watch records what already exists and sends a single summary; alerts start from the *next* change
- **Fires on the crossing, not the state** — an item that was already cheap is not news; one that just crossed your target is
- **Quiet hours** with an urgent override, per-item cooldowns and a hard daily cap

### 💶 The price you really pay
- Live **JPY → EUR** conversion from two keyless sources, with the last good rate cached
- **Shipping estimated by weight**, guessed from the product's spec sheet or its type
- **Customs duty and import VAT** modelled properly, including the EU duty-free threshold
- Every landed figure comes with a **full breakdown** on hover — nothing is a magic number
- Per watch: compare against the **shop price** or the **delivered total**

### 🏅 Condition grades, handled properly
- A pre-owned product code often covers **several graded copies at different prices** — AmiAmi's "More Buying Choices". The headline price is the cheapest of them, never an arbitrary one
- Figure and box grades are read separately (`S`, `A`, `B+`, `B`, `C`, `D`)
- A watch can demand **"Item:A or better"**, and the target price then applies to the cheapest copy that actually qualifies
- Alerts name the exact listing and its grade, because a price without a condition means nothing

### 📈 Real price history
- Every price and stock change is recorded — one row per *change*, not per poll
- **Step charts**, because a price is a fact that holds until it changes
- Lowest / highest / average ever seen, and how long an item has been tracked
- **Deal radar** flags anything unusually cheap against its own history, no watch required

### 🏷️ Discovery by MyFigureCollection tag
- Items are cross-referenced with MyFigureCollection **by barcode**, which is exact
- Browse AmiAmi by MFC tags: character, series, pose, outfit, sculptor, material
- A **local tag index** means results are instant and everything shown is buyable now
- Or search MFC directly to reach figures nobody on your instance has looked for yet

### 🗂️ Wishlist and collection
- Wishlist → ordered → owned → sold, with notes, tags and priorities
- What you spent, what it is worth now, and what your wishlist would cost delivered
- CSV and JSON export and import

### 🎨 Two complete skins
| | |
|---|---|
| **Midnight** | Deep blue-grey, one warm accent. Calm, dense, image-first. |
| **Sakura** | Pink and violet gradients, rounder cards, more personality. |

Each with light, dark and system modes. Your choice follows your account to any device.

---

## 🚀 Install

### Docker (recommended)

```bash
docker run -d \
  --name amisearch \
  --restart unless-stopped \
  -p 8080:8080 \
  -v ./data:/data \
  -e TZ=Europe/Berlin \
  -e BASE_URL=http://localhost:8080 \
  ghcr.io/bejak1999/ami_search:latest
```

Open `http://localhost:8080`. **The first account you create becomes the administrator.**

### Docker Compose

```bash
curl -O https://raw.githubusercontent.com/bejak1999/ami_search/main/docker-compose.yml
docker compose up -d
```

### 🖥️ TrueNAS SCALE

AmiSearch is a single container with one volume, so it installs as a **Custom App**:

| Setting | Value |
|---|---|
| Image repository | `ghcr.io/bejak1999/ami_search` |
| Image tag | `latest` |
| Container port | `8080` |
| Node port | `8080` (or whatever you prefer) |
| Host path | your dataset, e.g. `/mnt/tank/apps/amisearch` |
| Mount path | `/data` |

Then add the environment variables you want from the table below. `TZ` and `BASE_URL` are the two worth setting immediately.

<details>
<summary><b>Running it behind a reverse proxy</b></summary>

Set `BASE_URL` to the public HTTPS URL. That makes the session cookie `Secure` and puts working links in your notifications.

Server-sent events power the live alert toasts, so buffering must be off:

```nginx
location / {
    proxy_pass http://amisearch:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Required for the live alert stream.
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
}
```

Browser push needs HTTPS. Everything else works over plain HTTP on a LAN.
</details>

<details>
<summary><b>Building from source</b></summary>

```bash
git clone https://github.com/bejak1999/ami_search.git
cd ami_search

# Backend
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data uvicorn app.main:app --reload --port 8080

# Frontend, in a second shell
cd frontend
npm install
npm run dev        # http://localhost:5173, proxies /api to :8080
```

`npm run build` writes the bundle into `backend/app/static`, which FastAPI then serves itself.
</details>

---

## ⚙️ Configuration

Everything is optional; the defaults are sensible.

| Variable | Default | What it does |
|---|---|---|
| `TZ` | `UTC` | Container time zone |
| `BASE_URL` | `http://localhost:8080` | Public URL. Drives notification links and the `Secure` cookie flag |
| `SECRET_KEY` | *generated* | Signs sessions. Left empty, a persistent key is written to `/data` on first boot |
| `DATA_DIR` | `/data` | Database, secret key and price history live here |
| `DATABASE_URL` | *SQLite* | Set to `postgresql+psycopg://…` to use Postgres instead |
| `ALLOW_REGISTRATION` | `true` | Turn off once everyone has an account. The first account is always allowed |
| `MIN_POLL_INTERVAL_SECONDS` | `15` | Hard floor. No watch may poll faster, whatever a user sets |
| `DEFAULT_POLL_INTERVAL_SECONDS` | `300` | Baseline for new watches |
| `ADAPTIVE_POLLING` | `true` | Let the scheduler speed up and slow down on its own |
| `WORKER_CONCURRENCY` | `4` | Threads checking watches in parallel |
| `PROVIDER_REQUESTS_PER_MINUTE` | `40` | **Shared budget across every watch.** The one knob that matters |
| `PROVIDER_MAX_CONCURRENCY` | `3` | Simultaneous requests to one shop |
| `DISPLAY_CURRENCY` | `EUR` | Default for new accounts |
| `FX_REFRESH_HOURS` | `6` | How often exchange rates are refetched |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | — | Required for browser push. Generate a pair from **Administration** |
| `VAPID_SUBJECT` | `mailto:admin@example.com` | Contact address sent with push messages |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | — | Required for e-mail |
| `PRICE_HISTORY_RETENTION_DAYS` | `1095` | Three years of history |
| `ALERT_RETENTION_DAYS` | `365` | Alerts older than this are pruned nightly |

See [`.env.example`](.env.example) for the full list.

### 📲 Setting up Telegram

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, copy the token
2. Send your new bot any message, so it is allowed to reply to you
3. In AmiSearch: **Settings → Notifications → Telegram**, paste the token, press **Detect my chat**, pick your chat
4. **Send a test**

### 🔔 Setting up browser push

1. **Administration → Generate and copy** — the VAPID pair is copied to your clipboard
2. Paste both values into your compose file or environment and restart
3. **Settings → Notifications → Browser push**, allow the permission prompt

Push needs HTTPS unless you are on `localhost`.

---

## 🧩 How it works

```
                 ┌──────────────────────────────────────┐
   Browser ──────┤  FastAPI  ·  /api + the built SPA     │
   (SSE) ◄───────┤                                       │
                 │  ┌────────────────────────────────┐  │
                 │  │ Scheduler (APScheduler)         │  │
                 │  │  ticks every 5 s, asks the DB   │  │
                 │  │  which watches are due          │  │
                 │  └───────────────┬────────────────┘  │
                 │                  │ thread pool        │
                 │  ┌───────────────▼────────────────┐  │
                 │  │ Provider  ·  token bucket +     │  │
                 │  │ circuit breaker, shared budget  │──┼──► api.amiami.com
                 │  └───────────────┬────────────────┘  │
                 │                  │                    │
                 │  ┌───────────────▼────────────────┐  │
                 │  │ Matcher  ·  diff vs last seen   │  │
                 │  │ → Alert → six notifiers         │──┼──► Telegram, push, …
                 │  └────────────────────────────────┘  │
                 │  ┌────────────────────────────────┐  │
                 │  │ Enrichment · MFC by barcode     │──┼──► myfigurecollection.net
                 │  │ (slow, polite, background)      │  │
                 │  └────────────────────────────────┘  │
                 └──────────────────────────────────────┘
                          SQLite (or Postgres) in /data
```

Watches are not registered as individual scheduler jobs. Intervals change adaptively after every run, so a "which watches are due?" query is far simpler to reason about than several hundred jobs being constantly rescheduled.

### 🔌 Where the data comes from

AmiAmi runs an undocumented but stable JSON API behind the same Cloudflare edge as the shop. **No HTML scraping is involved.**

```http
GET https://api.amiami.com/api/v1.0/items?s_keywords=nendoroid&pagecnt=1&pagemax=50&lang=eng
GET https://api.amiami.com/api/v1.0/item?gcode=FIGURE-153570-R&lang=eng
X-User-Key: amiami_dev
```

Three things about it are worth knowing, and all three shaped this codebase:

- **Cloudflare blocks on the TLS fingerprint, not the headers.** `requests` and `httpx` get a 403 before a single header is inspected. AmiSearch uses [`curl_cffi`](https://github.com/lexiforest/curl_cffi) to present a real Chrome handshake.
- **`soldout_flg` on the detail endpoint is always `1`**, including for pre-orders that opened this morning. The usable signals are `stock`, `order_closed_flg` and `cart_type`.
- **A sold-out pre-owned listing is deleted**, not flagged. The API answers `Invalid Request 21`. That is normal, expected information for a tracker — so it is treated as "this item is gone", not as an error, and it never trips the circuit breaker.

`pagemax` is capped at 50 upstream. Price ranges and most sort orders are not supported by the API, so they are applied locally to the page that was fetched.

MyFigureCollection has no working public API any more, so it is scraped — gently, at 12 requests per minute, with every page cached. The barcode AmiAmi publishes gives an **exact** match against MFC, with a scored title search as the fallback.

### 🏬 Adding another shop

Everything shop-specific lives behind one interface. Implement `ShopProvider`, register it, and it appears in the UI, the watch editor and the scheduler with no other changes:

```python
class MandarakeProvider(ShopProvider):
    id = "mandarake"
    name = "Mandarake"
    currency = "JPY"

    def search(self, query: SearchQuery) -> SearchResult: ...
    def get_item(self, code: str) -> NormalizedItem: ...
    def parse_url(self, url: str) -> str | None: ...
```

Then add it in `backend/app/providers/registry.py`. The database already keys every item, watch and alert by `provider`.

---

## 🧪 Tests

```bash
cd backend

python tests/test_offline.py       # 135 assertions, no network
python tests/test_migration.py     # 15 assertions: upgrading an old database
python tests/smoke_e2e.py          # 68 assertions against the live AmiAmi API
python tests/smoke_discovery.py    # 25 assertions across AmiAmi and MFC
```

The first two are what CI runs. They cover the parts where a silent mistake would be expensive: the landed-cost arithmetic, the rules that decide whether you get woken up, the condition-grade logic, and the promise that an upgrade never costs you data.

---

## 🔐 Data and security

**Passwords** are stored as bcrypt hashes at cost 12, with a random salt per
password, so the same password produces a different hash for every account.
One verification takes roughly 200 ms, which puts an offline attacker at about
five guesses per second per core. Passphrases longer than bcrypt's 72-byte
input limit are pre-hashed with SHA-256 rather than silently truncated. The
plaintext is never written anywhere, and changing a password revokes every
other session.

Sessions are JWTs in an httpOnly cookie, each backed by a revocable row, so
you can sign out a specific device from **Settings → Account**.

**Upgrades never destroy the database.** On every start the schema is compared
against the models and any missing column is added; nothing is ever dropped,
renamed or retyped. The column list is derived from the mappers rather than
hand-maintained, because a hand-maintained list is exactly what gets forgotten.
Before any change is applied, SQLite databases are copied aside to
`amisearch.pre-migration-<timestamp>.db` in your data directory, and the last
five such backups are kept. `tests/test_migration.py` builds a database in an
older shape, upgrades it, and asserts every row survived — CI runs it on
every push.

**Notification secrets** — bot tokens, webhook URLs, push subscriptions — are
stored in the database and never sent back to the browser; the edit form shows
only a masked hint, and leaving a secret field empty keeps the stored value.

---

## ⚠️ Please be reasonable

This talks to a real shop and a small community site.

- The default of **40 requests per minute is shared across every watch you own**. Raising it makes alerts faster and a temporary block more likely.
- 15-second polling exists for a handful of grails, not for forty broad searches.
- MyFigureCollection is scraped at a deliberately slow rate and everything is cached. Please do not raise that.
- This project is not affiliated with AmiAmi or MyFigureCollection.

---

## 📄 Licence

MIT — see [LICENSE](LICENSE).

Built because notifications that arrive after the listing is gone are not notifications.
