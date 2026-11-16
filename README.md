# guarded-ebay-mcp

An MCP server that lets Claude read and manage your eBay resale listings (prices, offers to
watchers, package weights, orders, exact fees). The pricing rules are enforced by code, not by
the prompt, so a confused or manipulated model still cannot sell below your floor.

New here? Follow [FIRST_RUN.md](FIRST_RUN.md) (under 30 minutes). Want to understand the code?
Read [LEARNING.md](LEARNING.md).

## Tools

| Tool | What it does | Writes to eBay? |
|---|---|---|
| `list_listings` | Active listings: item ID, title, price, watchers, days listed, package weight and dimensions, cost, floor | No |
| `get_pricing_floor(item_id)` | Cost, exact break-even, quick estimate, floor and the rule that set it | No |
| `update_price(changes[1-20])` | Preview price changes; returns a `change_id` | No, preview only |
| `send_offer_to_watchers(item_id, offer_price or discount_pct, message)` | Preview an offer to interested buyers | No, preview only |
| `update_package_weight(item_id, lb, oz, dims, package_type)` | Preview a weight or dimension fix | No, preview only |
| `confirm_change(change_id)` | Apply a previewed change | **Yes. The only write tool.** |
| `undo_change(audit_id)` | Preview reverting a logged price or package change | No, preview only |
| `recent_changes` | Audit log: item, old value, new value, UTC time, reason, status, change_id | No |
| `get_orders` | Recent orders: order number, items, price, buyer-paid shipping, eBay-collected tax, ship-by date. No buyer names or addresses. | No |
| `get_order_fees(order_id)` | Exact transaction fees, fee breakdown and label cost, mapped to sheet columns K-R | No |
| `search_comps(query)` | Active listings with min, median and max delivered price. These are asking prices, not sold prices. | No |

`get_order_fees` returns `sheet_values` for your sheet: K sale date, L "eBay", M sold price,
N transaction fees, O tax, P shipping paid, Q 0.00, R label cost (SHIPPING_LABEL debits minus
voided-label credits).

## Architecture

```
Claude Desktop (stdio)  or  Claude custom connector (HTTPS + OAuth)
   |
server.py    tool definitions, annotations (confirm_change = destructive), error mapping
   |
service.py   preview -> change_id -> confirm_change -> re-read live listing -> re-check rules -> write -> audit
   |-- pricing.py   pure Decimal math: fee model, exact break-even, floor rules, typo guard, weight limits
   |-- costs.py     cost from your Google Sheet (read-only service account) or a local costs.json
   |-- store.py     SQLite: pending previews (15 min, single use), audit log, daily API call counts
   |-- trading.py   Trading API XML: GetMyeBaySelling, GetItem, ReviseInventoryStatus, ReviseFixedPriceItem
   `-- rest.py      Fulfillment (orders), Finances (fees, labels), Negotiation (offers), Browse (comps)
          |
       http.py      auth headers, 2 requests/sec pacing, daily caps, retry on 429/5xx, one re-auth on 401
       auth.py      refresh token -> 2-hour access token (memory only); application token for Browse
       config.py    settings from env vars or an env file outside the repo; secrets wrapped so they never print
       remote.py    optional HTTP mode: JWT validation, refuses to start without OAuth
```

Your listings were created on the website and app, not with the Inventory API, so all listing
reads and revisions go through the Trading API.

## Safety model

All of this is enforced in code.

1. **Two-phase writes.** Preview tools store the exact change in SQLite and return a random
   `change_id` (128 bits). It expires after 15 minutes and works once. `confirm_change` re-reads
   the live listing and re-checks every rule. If the price changed since the preview, that item
   is skipped.
2. **Kill switch.** `EBAY_MCP_WRITES_ENABLED` defaults to `false`. `confirm_change` refuses
   unless it is `true`.
3. **The server reads cost itself.** No tool accepts a cost. If an item has no cost record,
   every write for it is blocked. If the cost source is down, writes are blocked too.
4. **Audit log.** Every attempt is logged: item ID, old value, new value, UTC timestamp,
   reason, status (`applied`, `failed`, `skipped`) and `change_id`.
5. **Typo guard.** A new price more than 3x the current price is blocked.
6. **Weight sanity limits.** By title keyword: hats and beanies 2 lb, tops 4 lb, jackets 7 lb,
   sneakers 8 lb, anything else 10 lb, unless `allow_unusual_weight=true`. "Sell similar" copies
   carry the old listing's weight; this is what catches a cap listed at 25 lb.
7. **Tool annotations.** `confirm_change` is marked destructive. Every other tool is marked
   read-only. The server instructions tell Claude to show the preview and wait for your explicit
   yes.
8. **Input handling.** Item IDs must be 9-19 ASCII digits and order IDs digits and dashes before
   they reach XML or a URL. Trading API XML is parsed with `defusedxml`. Tokens are redacted from
   errors and logs. Buyer names and addresses are dropped from order output.
9. **No double writes.** Reads retry on 429, 5xx and network errors. Writes retry only on 429
   (eBay did not process the call), so an offer or revision is never sent twice.

## Pricing rules

All in `src/ebay_mcp/pricing.py`, as pure functions on `Decimal`. The constants at the top of
that file are yours to edit: `PROFIT_PCT`, `TAX_RATE`, `WINTER_MONTHS`, `WINTER_KEYWORDS`,
`WEIGHT_LIMITS_LB`, the fee rates, and `ASSUMED_BUYER_SHIPPING` / `ASSUMED_LABEL_COST`.

### Fee model

```
order total = (price + buyer shipping) x (1 + tax)          tax defaults to 7%
fees        = 13.6% x order total + $0.40                   $0.30 if the order total is $10 or less
athletic shoes with an order total of $150 or more: 8% x order total, no per-order fee
net         = price - fees + buyer shipping - label cost
```

`break_even(cost, buyer_shipping=0, label_cost=0, category)` is the lowest price whose net is at
least cost. It is solved exactly from the fee model, tier by tier, and rounded UP to the cent
(`ROUND_CEILING`), so a floor never undercuts break-even. The quick estimate `cost x 1.17 + $1`
is shown next to it for comparison only.

### Floor rules

| Situation | Floor |
|---|---|
| Winter item (title contains beanie, balaclava or thermal) in Sep-Dec | profit floor |
| Held under 6 months | profit floor = break-even of cost x 1.10 (net at least 10% over cost) |
| Held 6-12 months (exactly 6 counts here) | break-even |
| Held over 12 months, not flagged aged | break-even |
| Held over 12 months and flagged aged | break-even of cost x (1 - loss); loss defaults to 10%, hard cap 20% |
| Aged flag under 12 months | ignored |
| Per-item floor override | can only raise the floor |

Months held counts full months only: bought Apr 23, today Oct 22 is 5 months; bought Apr 22 is 6.

### Worked examples (cost $50.00, no shipping in the math, 7% tax)

| Case | Math | Floor |
|---|---|---|
| Break-even | (50 + 0.40) / (1 - 0.136 x 1.07) = 58.9832, rounded up | **$58.99** |
| Check | at $58.99: fees = 0.136 x 63.1193 + 0.40 = $8.98, net = $50.01. At $58.98 net = $49.997, a loss. | |
| Quick estimate | 50 x 1.17 + 1 | $59.50 |
| Held 3 months | break-even of 55.00 | **$64.84** |
| Held 8 months | break-even of 50.00 | **$58.99** |
| Held 14 months, not aged | break-even of 50.00 | **$58.99** |
| Held 14 months, aged | break-even of 45.00 | **$53.14** |
| Aged, record asks for 50% loss | capped at 20%: break-even of 40.00 | **$47.29** |
| Beanie in October, held 20 months, aged | winter rule wins: break-even of 55.00 | **$64.84** |
| Same beanie in February | aged rule: break-even of 45.00 | **$53.14** |
| Floor override $80 / $10 | $80 raises the floor / $10 is ignored | **$80.00** / $58.99 |

Sneakers (category Athletic Shoes), cost $140: at 13.6% break-even would be $164.32. That order
total is over $150, so the 8% tier applies and the real break-even is 140 / (1 - 0.08 x 1.07) =
**$153.11**.

## Setup

### 1. Install

Windows 11, Git Bash, Python 3.14:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pip install --no-deps -e .
.venv/Scripts/python -m pytest
```

Linux and macOS: the same with `.venv/bin/python`.

### 2. eBay developer setup

1. Sign in at developer.ebay.com and wait for the developer account to be approved.
2. **Application Keys**: create a **Sandbox** keyset. Note the App ID (Client ID) and Cert ID
   (Client Secret).
3. **Sandbox test seller**: create a sandbox user in the developer portal (Sandbox user
   registration). Sign in to sandbox.ebay.com as that user and create one or two fixed-price
   listings.
4. **RuName**: on the sandbox keyset open **User Tokens**, then "Get a Token from eBay via Your
   Application", and add a redirect entry. eBay gives you a RuName. Set "Your auth accepted URL"
   to any HTTPS page you control. You only need to copy the URL eBay sends your browser to.
5. **Production only, later**: before the production keyset works, eBay requires you to
   subscribe to Marketplace Account Deletion notifications or request an exemption. This server
   only touches your own account and stores no other users' data, so request the exemption on
   the keyset's Alerts and Notifications page ("I do not persist eBay data").

### 3. Secrets file

Secrets live outside the repo.

```bash
mkdir -p ~/.ebay-mcp
cp .env.example ~/.ebay-mcp/sandbox.env
chmod 600 ~/.ebay-mcp/sandbox.env     # Linux/macOS; the server refuses a world-readable file there
```

Fill in `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` and `EBAY_RU_NAME`. On Windows `~` is
`C:\Users\<you>`, which other accounts cannot read by default.

### 4. Get a refresh token (once)

```bash
EBAY_MCP_ENV_FILE=~/.ebay-mcp/sandbox.env .venv/Scripts/python scripts/get_refresh_token.py
```

The script opens eBay's consent page, you sign in as the sandbox test seller, then paste the URL
you were redirected to. It verifies the OAuth `state` value, exchanges the code, and writes
`EBAY_REFRESH_TOKEN` into the env file. The refresh token lasts about 18 months. The server
turns it into 2-hour access tokens on its own and keeps those in memory only.

Scopes requested: `api_scope`, `sell.inventory`, `sell.fulfillment.readonly`, `sell.finances`.
Browse uses a separate application token.

### 5. Cost source

**Sandbox:** copy `costs.example.json` to `~/.ebay-mcp/costs.json` and use your sandbox item IDs.
Write costs as strings (`"52.30"`).

**Production, Google Sheet tab "Lucas Resell":**

1. Add three columns: **V** eBay Item ID, **W** Aged (`Y` allows a loss after 12 months),
   **X** Floor override (optional minimum price). The server also reads A (date), B (item),
   I (total cost) and J (status). Every letter is configurable with `EBAY_MCP_SHEET_*_COL`.
2. In Google Cloud: create a project, enable the Google Sheets API, create a service account
   (no roles needed), and download a JSON key to `~/.ebay-mcp/sheets-reader.json`.
3. Share the spreadsheet with the service account's email as **Viewer**.
4. In the env file set `EBAY_MCP_COST_SOURCE=sheets`, `EBAY_MCP_SHEET_ID` (the long ID in the
   sheet URL) and `GOOGLE_SERVICE_ACCOUNT_FILE`.

The server requests only the `spreadsheets.readonly` scope and caches the tab for 5 minutes. If
one item ID is on several rows, the highest cost wins. Rows without an item ID, cost or date are
skipped, so those items have no cost record and their writes are blocked.

### 6. Connect to Claude Desktop (stdio, the default)

Open Settings, Developer, Edit Config (`%APPDATA%\Claude\claude_desktop_config.json`) and add
this, replacing `YOUR_USER` and the repo path:

```json
{
  "mcpServers": {
    "guarded-ebay-mcp": {
      "command": "C:\\Users\\YOUR_USER\\projects\\guarded-ebay-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "ebay_mcp.server"],
      "env": {
        "EBAY_MCP_ENV_FILE": "C:\\Users\\YOUR_USER\\.ebay-mcp\\sandbox.env"
      }
    }
  }
}
```

Quit Claude Desktop fully (system tray too) and start it again. The config holds only a path;
the secrets stay in the env file. In the connector's tool permissions, set `confirm_change` to
always ask.

## Going to production

1. Create the **Production** keyset and its RuName, and get the account deletion exemption.
2. `cp ~/.ebay-mcp/sandbox.env ~/.ebay-mcp/prod.env`. Set `EBAY_ENV=production`, the production
   keys and RuName, `EBAY_MCP_DB=~/.ebay-mcp/prod.db`, and the sheet settings. Delete the
   sandbox `EBAY_REFRESH_TOKEN` line.
3. Run `get_refresh_token.py` with `EBAY_MCP_ENV_FILE=~/.ebay-mcp/prod.env` and sign in as your
   real seller account.
4. Point Claude Desktop at `prod.env`. **Leave `EBAY_MCP_WRITES_ENABLED=false` for a day.** Use
   `list_listings`, `get_pricing_floor` and `get_order_fees`, and compare the numbers with Seller
   Hub and your sheet. Run a few previews and read them. `confirm_change` will refuse.
5. Set `EBAY_MCP_WRITES_ENABLED=true` and restart Claude Desktop. Start with one
   `update_package_weight` fix, then one small price change, then `undo_change` it to prove the
   rollback path.

## Remote hosting (optional)

Streamable HTTP, stateless, as a Claude custom connector. **The server refuses to start in HTTP
mode without OAuth.** There is no switch to turn that off.

It acts as an OAuth resource server. You configure an identity provider (Auth0, WorkOS, Stytch
and so on), and the server accepts a JWT only if it is signed by that issuer's JWKS keys with
RS256 or ES256, has the right `iss`, has `aud` equal to your public URL, is unexpired, and has a
`sub` on your allowlist. DNS rebinding protection is on: only requests whose Host header is your
public hostname are served.

1. In the IdP, create an API whose identifier (audience) is your public URL, for example
   `https://ebay-mcp.example.com/mcp`, and allow Claude's callback
   `https://claude.ai/api/mcp/auth_callback`. Find your own user ID (the `sub` claim).
2. Build and run the container (non-root user, SQLite on a volume). Put secrets in the host's
   secret store, never in the image:
   ```bash
   docker build -t guarded-ebay-mcp .
   docker run -p 8000:8000 -v ebay-mcp-data:/data --env-file /secure/path/prod-remote.env guarded-ebay-mcp
   ```
   Required settings: the eBay ones, plus `EBAY_MCP_PUBLIC_URL`, `EBAY_MCP_OAUTH_ISSUER`,
   `EBAY_MCP_JWKS_URL`, `EBAY_MCP_ALLOWED_SUBJECTS`. Terminate TLS at your host or reverse proxy.
3. Firewall: allow inbound traffic only from Anthropic's egress range **`160.79.104.0/21`**
   (plus your own IP while debugging).
4. In Claude: Settings, Connectors, Add custom connector, enter the public URL. Then open the
   connector's tool permissions and set **`confirm_change` to always ask**. That gives you a
   click-to-approve gate on top of the chat confirmation.

## Rate limits

Requests are paced at about 2 per second. Daily caps per API (Trading 4000, Fulfillment 2000,
Finances 2000, Negotiation 500, Browse 2000) are counted in SQLite and can be changed with
`EBAY_MCP_DAILY_LIMIT_*`. 429 and 5xx responses are retried with exponential backoff (and
`Retry-After` when eBay sends it). A 401 triggers one token refresh and one retry.

## Where this differs from the original spec

* **HTTP client is `httpx2`, not `httpx`.** MCP SDK 2.3 itself depends on `httpx2`, so the
  server uses the same client instead of installing a second one.
* **Fee thresholds use eBay's "total amount of the sale"**, which includes shipping and sales
  tax (eBay selling fees page). So the $10 and $150 thresholds are compared with
  (price + shipping) x 1.07, not the pre-tax amount.
* **Trading API compatibility level is 1489**, the version in the current Trading API reference.
  Override with `EBAY_TRADING_COMPAT_LEVEL`.
* **Exactly 12 months held** is treated as "over 12 months" for the aged flag (the spec says the
  flag is ignored under 12 months).
* **Dates are UTC.** "Today" for months held and the winter rule is the UTC date, which avoids a
  time zone database dependency on Windows.
* **`list_listings` is paged** (25 by default, 100 max). Each listing needs one `GetItem` call
  for its package details, and at 2 requests per second a long list would time out.

## Known limits

* Not verified against live eBay yet. All tests run against mocks. Checked against eBay's
  published OpenAPI specs and Trading reference: the Negotiation rules (5% minimum, 2 days, no
  counteroffers, 2,000 character message, one listing per request), the Finances and Fulfillment
  paths, filters and scopes, and the package fields (whole inches, whole pounds and ounces).
  Not confirmed from the docs: whether the Finances `orderId` filter returns SHIPPING_LABEL
  transactions, the OAuth token lifetimes, and the Trading API error codes for an expired token.
  Confirm those in the sandbox first.
* The Negotiation API does not support listings managed with the Inventory API. Yours are not, so this is fine.
* Variation listings (one listing, several sizes) and auctions are refused.
* Floors assume buyer-paid shipping roughly equals the label cost (both default to 0 in the
  math). eBay still charges its fee on the shipping the buyer pays, about $0.87 on a $6 label.
  Set `ASSUMED_BUYER_SHIPPING` and `ASSUMED_LABEL_COST` in `pricing.py` if you want that in the floor.
* The tax rate is an assumption (7%). Real tax varies by buyer state, so real fees vary by a few cents.
* The fee model stops at eBay's $7,500 tier and ignores promoted listing fees and store discounts.
* Sent offers cannot be withdrawn through the API. They expire after 2 days.
* `search_comps` shows asking prices. Sold prices need eBay's Marketplace Insights API, which is restricted.
* If eBay has not posted a label charge yet, `get_order_fees` returns `R_label_cost: null`.
  Labels bought outside eBay never appear.
* Sale date (column K) is the UTC date of the order.
* The Sandbox has thin data: Browse results and Finances transactions are often empty there.
