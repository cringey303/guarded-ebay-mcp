# LEARNING.md

A study guide for this project: what each file does, and why it was built this way. Written so
you can explain it in an interview.

## The one-sentence pitch

"I built an MCP server that lets an AI assistant manage my eBay listings, and I designed it so
the AI is treated as an untrusted client: every money-losing action is blocked by server-side
code, not by instructions to the model."

## The core idea: do not trust the model

An LLM can misread a number, hallucinate a cost, or be manipulated by text it reads (prompt
injection, for example a malicious listing title). Telling it "never price below cost" in a
prompt is a request, not a control. So the project follows one rule:

> Anything that matters is enforced in code the model cannot change.

The prompt (server instructions, tool descriptions) only makes the happy path smooth. If you
deleted every instruction, the worst the model could do is ask for changes the server refuses.

This is the same idea as never trusting client-side validation in a web app.

## How a write flows

```
update_price (preview)                confirm_change (write)
  validate item ID                      kill switch on? else refuse
  read live listing from eBay           claim change_id (unused, unexpired, once)
  read cost from the sheet              read live listing AGAIN
  compute floor, check every rule       read cost AGAIN, check every rule AGAIN
  store exact change in SQLite          price drifted since preview? skip that item
  return random change_id               call eBay, log result (applied / failed / skipped)
```

Things worth being able to explain:

* **Why two phases?** It forces a human checkpoint. The model cannot write in one step, and
  Claude's own "always ask" permission on `confirm_change` adds a second, independent gate.
* **Why store the change server-side?** `confirm_change` takes only an ID. The model cannot
  alter the price between preview and confirm, because the price is not an argument.
* **Why re-check at confirm time?** Time of check versus time of use (TOCTOU). In 15 minutes you
  might edit the listing in the app, or fix a cost in the sheet. The second check uses fresh data.
* **Why single use and expiry?** It stops replay. An old approval cannot be reused later.
* **Why a kill switch that defaults to off?** Fail safe defaults. A fresh install cannot write.

## File by file

### `src/ebay_mcp/pricing.py`
All the money rules as **pure functions**: same inputs, same outputs, no network, no clock, no
files. That is why they are easy to test exhaustively.

* **Decimal, never float.** `0.1 + 0.2 != 0.3` in binary floating point. Money code uses
  `Decimal`, and inputs are converted through `str()` so `62.1` stays `62.1`.
* **Rounding direction is a safety decision.** Floors use `ROUND_CEILING`. Half-up rounding would
  turn a 58.9832 break-even into 58.98, which loses a fraction of a cent. Always round toward
  the safe side.
* **Exact break-even.** Inside one fee tier, net is a straight line in price, so you can solve
  `price x (1 - rate x (1 + tax)) = cost + fixed fee` directly. Fees have tiers ($0.30 vs $0.40
  per order, 8% for sneakers at $150+), so the solver computes a candidate per tier, keeps the
  ones that really fall in their tier, and returns the lowest. A test compares it with a
  brute-force cent-by-cent scan.
* **`today` is a parameter.** Functions that call `date.today()` are hard to test and give
  different answers on different days. Passing the date in is called dependency injection.
* **The constants at the top** are the tunable policy. Logic and policy are separate.

### `src/ebay_mcp/service.py`
The business logic and the heart of the safety model. Each kind of change has one function that
builds a "row" from live data and returns a list of problems (`_price_row`, `_offer_row`,
`_package_row`). Preview and confirm both call the same function, so the rules cannot drift
apart between the two phases. `confirm_change` is the only method that calls a write on eBay.

### `src/ebay_mcp/server.py`
The MCP layer. It declares the 11 tools with the SDK's `MCPServer` (the 2.x name for FastMCP),
attaches annotations (`destructive_hint` on `confirm_change`, `read_only_hint` on everything
else), and converts expected errors into messages the model can read, after redacting tokens.
It contains no rules. If this file were wrong, the service layer would still refuse bad writes.

### `src/ebay_mcp/costs.py`
Where cost comes from: a Google Sheet (read with a service account limited to
`spreadsheets.readonly`) or a local JSON file. Key decisions:

* **The model never supplies cost.** No tool has a cost parameter. A test checks that.
* **Fail closed.** No record, or the sheet is unreachable: writes are blocked. The opposite
  ("fail open": assume it is fine) is how safety systems quietly stop working.
* **Highest cost wins** when one item ID is on several rows, so the floor is conservative.
* **Service account auth** is itself a small OAuth flow: sign a JWT with the account's private
  key, trade it for a 1-hour access token.

### `src/ebay_mcp/store.py`
SQLite, three tables: `pending` (previews), `audit` (the log), `api_calls` (daily counters).
Claiming a change is an atomic `UPDATE ... WHERE status='pending'`, so two confirms racing each
other cannot both win. All SQL uses `?` parameters, never string formatting (no SQL injection).
Change IDs come from `secrets.token_urlsafe`, a cryptographic random source, not `random`.

### `src/ebay_mcp/auth.py`
eBay OAuth 2.0.

* **Authorization code flow** (run once by `scripts/get_refresh_token.py`): you approve the app
  in a browser, eBay returns a code, the script exchanges it for a long-lived refresh token.
* **The `state` parameter** is a random value sent with the request and checked on return. It
  proves the response belongs to the request you started (CSRF protection). It is compared with
  `secrets.compare_digest`, a constant-time comparison.
* **Refresh token grant**: the server trades the refresh token for 2-hour access tokens and
  caches them in memory, refreshing 5 minutes early. A lock prevents several refreshes at once.
* **Client credentials grant**: an application token with no user attached, used for Browse.
* **Least privilege**: only four scopes, and fulfillment is read-only. A stolen token cannot
  issue refunds or change account settings.

### `src/ebay_mcp/http.py`
One place for every outbound eBay call: pacing (about 2 per second), daily caps per API, retries
with exponential backoff and jitter, and one re-auth on 401. Writes are marked non-idempotent
and are not retried on 5xx, because the first attempt may have worked. `redact()` strips
anything token-shaped from text before it can reach a log or the model.

### `src/ebay_mcp/trading.py`
The XML Trading API, needed because listings made on the website are not Inventory API listings.

* **Injection defense on the way out**: values are validated (digits only, whole numbers, an
  allowlist of package types) before being placed in XML. Validation beats escaping because the
  set of valid values is tiny.
* **XXE defense on the way in**: responses are parsed with `defusedxml`, which rejects DTDs and
  entities. XML External Entity attacks make a parser read local files or explode memory
  ("billion laughs"). Tests feed it both payloads.
* A subtle bug the tests caught: the regex `\d` matches non-ASCII digits, and `$` allows a
  trailing newline. The code uses `[0-9]` with `fullmatch`.

### `src/ebay_mcp/rest.py`
The JSON APIs: Fulfillment (orders), Finances (fees and labels), Negotiation (offers), Browse
(comps). `summarize_order` builds its output from an allowlist of fields, so buyer names and
addresses are dropped by construction. That is data minimization: you cannot leak what you
never return.

### `src/ebay_mcp/config.py`
Settings from environment variables or an env file outside the repo. Secrets are wrapped in a
`Secret` class whose `repr` is `***`, so printing the settings object cannot leak them. On
Linux and macOS the env file must not be readable by other users, like an SSH private key.

### `src/ebay_mcp/remote.py`
Optional HTTP mode. The server is an OAuth **resource server**: it does not log anyone in, it
only validates JWTs from an identity provider. Checks: signature against the issuer's JWKS,
allowed algorithms only (RS256, ES256), issuer, audience, expiry, and an allowlisted subject.

Classic JWT attacks the tests cover:
* `alg=none`: a token with no signature. Rejected because algorithms are pinned.
* Algorithm confusion: an HS256 token "signed" with the public key. Rejected for the same reason.
* Wrong audience: a valid token meant for another service. Rejected by the `aud` check.
* Right issuer, wrong person: any user of the IdP. Rejected by the subject allowlist.

The mode refuses to start without OAuth, and DNS rebinding protection checks the Host header.

### `src/ebay_mcp/gsheets.py`
A small Google Sheets client over plain HTTPS: it signs a JWT with the service account key,
trades it for an access token, reads ranges and writes cells. The cost reader and the sale
logger share it. Two keys exist on purpose: a read-only one for costs and an editor one for the
logger, so the part that runs most often can never write.

### `src/ebay_mcp/sales_log.py`
The sale logger. It is a plain script because logging has exactly one right answer for each
order, so judgment (an LLM) would only add ways to be wrong. Ideas worth explaining:

* **Plan, then act.** It builds the full list of cell writes first and prints it (dry run is the
  default). That is the same two-phase idea as preview and confirm in the server.
* **Idempotency.** The eBay order ID is written into the row. A second run sees it and skips the
  order, so a crash or a rerun can never log a sale twice.
* **No hardcoded row numbers.** Rows are found by content (item ID, status, purchase date), and
  just before writing the sheet is read again. If any target row changed, it refuses. This is
  the same time-of-check versus time-of-use problem as price drift.
* **Atomic orders.** An order is planned completely or not at all, and written in one request.
  A half-logged order is worse than an unlogged one, because it looks done.
* **Reconciliation.** Splitting money across units can drift by a cent, so it uses a
  largest-remainder split that always adds up exactly, then checks the total against eBay's own
  payout number. Disagreement stops the write. Checking your output against an independent
  source is how financial code catches its own bugs.
* **Allowlisted columns.** The code refuses to write any column outside the sale columns and the
  marker columns, and never writes a non-empty Z. Defense in depth: even a bug in the planner
  cannot overwrite purchase data or the Gmail message IDs your purchase script depends on.
* **Fees by type.** eBay labels every fee with a type (FINAL_VALUE_FEE, AD_FEE and so on).
  `fee_column` in rest.py maps each type to the closest sheet column: only types that say tax go
  in Tax, payment or processing types go in Payment processing fees, the rest are Transaction
  Fees. One small rule shared by the MCP tool and the logger keeps both writing the same thing.

### `scripts/get_refresh_token.py`
The one-time consent flow. It writes the refresh token into the env file by replacing the line,
creating the file with private permissions from the start.

### `tests/`
All eBay and Google traffic is mocked with `httpx2.MockTransport`, so tests are fast and need
no credentials. `fakes.py` has in-memory stand-ins for the eBay clients for the service tests.
The tests are the executable version of the spec: each rule in the README has a test.

### `Dockerfile`
For remote mode. Runs as a non-root user, keeps SQLite on a volume, takes secrets from env vars.

## Design decisions and trade-offs

| Decision | Why | Cost |
|---|---|---|
| Rules in code, not prompts | Prompts are not a security boundary | More code |
| Preview then confirm | Human checkpoint, no replay, no drift | Two tool calls per change |
| Fail closed on missing cost | A wrong floor is worse than no action | New listings need a sheet row first |
| Exact break-even plus a quick estimate | Exact for safety, estimate for intuition | A tiered solver instead of one line |
| SQLite | Zero setup, atomic updates, survives restarts | One machine only |
| stdio by default | No network listener means nothing to attack | Works only where Claude Desktop runs |
| Writes not retried on 5xx | Never apply a change twice | You may need to retry by hand |
| Trading API for revisions | Website listings are not Inventory API listings | XML instead of JSON |

## Security concepts this project demonstrates

Least privilege, defense in depth, fail-safe defaults, fail closed, complete mediation (every
write goes through one checked path), TOCTOU handling, input validation with allowlists, XXE
and injection defense, secrets management, token redaction, CSRF protection with `state`, JWT
validation, replay prevention, audit logging, and data minimization.

## Interview questions to practice

1. Why is a system prompt not a security control? What would you do instead?
2. Walk through what happens if the model calls `confirm_change` with a made-up ID.
3. What is TOCTOU and where does this project handle it?
4. Why round floors up? Give a number where half-up rounding loses money.
5. Why does the server not retry a failed write on a 500?
6. What does the OAuth `state` parameter protect against?
7. Name three ways a JWT check can go wrong and how each is prevented here.
8. Why use `defusedxml` when the XML comes from eBay, a trusted party?
9. What would you change to support several sellers? (Per-user tokens and cost sources, a real
   database, per-user audit logs, and authorization checks on every item.)
