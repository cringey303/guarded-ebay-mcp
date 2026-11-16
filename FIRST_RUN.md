# First run checklist

For the day eBay approves your developer account. About 30 minutes. Commands are for Git Bash
on Windows; on Linux use `.venv/bin/python`.

## Before you start (5 min)

- [ ] `python -m venv .venv`
- [ ] `.venv/Scripts/python -m pip install -r requirements-dev.txt`
- [ ] `.venv/Scripts/python -m pip install --no-deps -e .`
- [ ] `.venv/Scripts/python -m pytest` shows every test passing

## eBay developer portal (10 min)

- [ ] Application Keys: create a **Sandbox** keyset. Copy the App ID and Cert ID.
- [ ] Create a Sandbox test user (seller).
- [ ] User Tokens, "Get a Token from eBay via Your Application": add a redirect entry, set the
      auth accepted URL to any HTTPS page, copy the **RuName**.
- [ ] Sign in to sandbox.ebay.com as the test user and create one fixed-price listing. Note its item ID.

## Secrets and token (5 min)

- [ ] `mkdir -p ~/.ebay-mcp && cp .env.example ~/.ebay-mcp/sandbox.env`
- [ ] Fill in `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `EBAY_RU_NAME` in `~/.ebay-mcp/sandbox.env`
- [ ] `cp costs.example.json ~/.ebay-mcp/costs.json` and replace an item ID with your sandbox listing's ID
- [ ] `EBAY_MCP_ENV_FILE=~/.ebay-mcp/sandbox.env .venv/Scripts/python scripts/get_refresh_token.py`
- [ ] Sign in as the sandbox test user, accept, paste the redirect URL. The script says "Saved".

## Claude Desktop (5 min)

- [ ] Add the `guarded-ebay-mcp` block from the README to `%APPDATA%\Claude\claude_desktop_config.json`
- [ ] Quit Claude Desktop fully and reopen it
- [ ] Set the `confirm_change` tool permission to always ask

## Prove it works (5 min)

- [ ] "List my eBay listings." You see the sandbox listing with a cost and a floor.
- [ ] "What is the pricing floor for item <id>?" The rule and break-even look right.
- [ ] "Drop the price of <id> to $1." The preview is **blocked** (below the floor).
- [ ] "Set the price of <id> to <something above the floor>." You get a preview and a change_id.
- [ ] Say yes. `confirm_change` **refuses** because writes are disabled. That is the kill switch working.
- [ ] Set `EBAY_MCP_WRITES_ENABLED=true` in `sandbox.env`, restart Claude Desktop, preview and confirm again. The sandbox listing changes.
- [ ] "Show recent changes", then "undo that change" and confirm. The price is back.

Production comes later: see "Going to production" in the README, and keep writes off for the first day.
