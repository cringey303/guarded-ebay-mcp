"""One-time eBay consent flow (OAuth authorization code grant). Saves a refresh token.

Usage (Git Bash on Windows):
  EBAY_MCP_ENV_FILE=~/.ebay-mcp/sandbox.env .venv/Scripts/python scripts/get_refresh_token.py
Linux/macOS:
  EBAY_MCP_ENV_FILE=~/.ebay-mcp/sandbox.env .venv/bin/python scripts/get_refresh_token.py

1. Opens the eBay consent URL. Sign in with the account Claude should manage
   (a sandbox test user in sandbox, your real seller account in production).
2. After you accept, eBay redirects to your "auth accepted" URL. Copy that whole
   URL from the address bar and paste it here.
3. The script checks the state value (CSRF protection), swaps the code for
   tokens, and writes EBAY_REFRESH_TOKEN into your env file.

The token is never printed unless you run without an env file.
"""
import asyncio
import getpass
import os
import sys
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ebay_mcp.auth import AuthError, build_consent_url, exchange_code, state_matches  # noqa: E402
from ebay_mcp.config import ConfigError, Settings, load_env_file  # noqa: E402


def save_refresh_token(path: Path, token: str) -> None:
    """Replace the EBAY_REFRESH_TOKEN line in the env file, or add one. Keeps the file private."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    lines = [ln for ln in lines if not ln.strip().startswith("EBAY_REFRESH_TOKEN=")]
    lines.append(f"EBAY_REFRESH_TOKEN={token}")
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # created private from the start
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    if os.name == "posix":
        os.chmod(path, 0o600)


def main():
    env = dict(os.environ)
    target = env.get("EBAY_MCP_ENV_FILE")
    try:
        if target:
            load_env_file(target, env)
        if not env.get("EBAY_CLIENT_SECRET"):
            env["EBAY_CLIENT_SECRET"] = getpass.getpass("Client secret (Cert ID): ")
        env.pop("EBAY_MCP_ENV_FILE", None)
        s = Settings.from_env(env, require_refresh_token=False)
        url, state = build_consent_url(s)
    except (ConfigError, AuthError) as exc:
        sys.exit(str(exc))

    print(f"\nEnvironment: {s.env}\nOpen this URL, sign in and accept:\n\n{url}\n")
    webbrowser.open(url)
    redirected = input("Paste the full URL you were redirected to: ").strip()
    q = parse_qs(urlparse(redirected).query)
    if not state_matches(state, q.get("state", [""])[0]):
        sys.exit("State mismatch. Do not use this code; start over.")
    code = q.get("code", [""])[0]
    if not code:
        sys.exit("No code in that URL. Did you decline consent?")

    try:
        tokens = asyncio.run(exchange_code(s, code))
    except AuthError as exc:
        sys.exit(str(exc))
    refresh = tokens["refresh_token"]
    days = int(tokens.get("refresh_token_expires_in", 0)) // 86400
    print(f"\nGot a refresh token valid for about {days} days.")
    if target:
        save_refresh_token(Path(target).expanduser(), refresh)
        print(f"Saved to {target} as EBAY_REFRESH_TOKEN.")
    else:
        print("\nEBAY_REFRESH_TOKEN=" + refresh + "\n\nStore it somewhere private, then clear your terminal.")


if __name__ == "__main__":
    main()
