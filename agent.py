#!/usr/bin/env python3
"""AiProve best-autonomous-agent — fully autonomous agent (no human in the loop).

Reads skill.md, authenticates with its own keypair (EIP-191 personal_sign),
lists open bounties, and enters + completes as many as it can, in deadline order.

Targets (design doc ~/workspace/aiprove/best-agent/design.md, section 1):
  predictions (24h deadline first): btc-tomorrow, forecast-eth-24h,
    spy-close-1dollar, forecast-gold-tomorrow, signal-eth-pnl-24h, top5-pons-24h,
    forecast-nvda-3d, forecast-btc-7d
  repos (from config, never hardcoded): code-golf-expr, fastest-primes,
    best-autonomous-agent (this repo itself, submitted last)
  SKIPPED (already submitted, provenance_ok): bug-hunt-utils, sample-agent-skillmd

Env:
  AGENT_PRIVATE_KEY   required (0x-prefixed hex; never logged)
  API_BASE_URL        default https://aiprove.fun
  HANDLE              default anka.agent
  REPOS_FILE          default repos.json next to this file
  SESSION_FILE        default ~/workspace/aiprove/aiprove-session.json
  TRANSCRIPT_FILE     default transcript.txt next to this file
  PONS_BASE_URL       default https://ponsfamily.com

Repo config (repos.json) and/or env overrides:
  REPO_CODE_GOLF_URL / REPO_CODE_GOLF_COMMIT
  REPO_FASTEST_PRIMES_URL / REPO_FASTEST_PRIMES_COMMIT
  REPO_BEST_AGENT_URL / REPO_BEST_AGENT_COMMIT
If a repo entry is missing, that submit is SKIPPED (logged) — never guessed.

HTTP uses requests with a browser User-Agent (Cloudflare blocks urllib default).
GETs retry 2x with backoff. Submits are NEVER blind-retried: on a transport
error the agent checks GET /api/me first, and only retries if no submission
for that slug exists (a dropped connection may still have landed server-side).

Price sources (free, no key): CoinGecko + Yahoo Finance chart API.
Binance and Stooq are blocked from this network — never used.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from eth_account import Account
from eth_account.messages import encode_defunct

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_BASE = os.environ.get("API_BASE_URL", "https://aiprove.fun").rstrip("/")
HANDLE = os.environ.get("HANDLE", "anka.agent")
PONS_BASE = os.environ.get("PONS_BASE_URL", "https://ponsfamily.com").rstrip("/")
REPOS_FILE = os.environ.get("REPOS_FILE", os.path.join(BASE_DIR, "repos.json"))
SESSION_FILE = os.environ.get(
    "SESSION_FILE",
    os.path.expanduser("~/workspace/aiprove/aiprove-session.json"),
)
TRANSCRIPT_FILE = os.environ.get(
    "TRANSCRIPT_FILE", os.path.join(BASE_DIR, "transcript.txt")
)
WIB = timezone(timedelta(hours=7))
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
}

# Bounties already submitted with provenance_ok — NEVER resubmit.
ALREADY_SUBMITTED = {
    "bug-hunt-utils": [
        "4b7623bc-af71-4212-b346-1938f6ca6a94",  # 13 bugs
        "66d79c87-6b78-4443-abe1-aa66964ee53c",  # 67 bugs (merged, final)
    ],
    "sample-agent-skillmd": [
        "0fdf4e86…",  # effective submit 2026-11-13 23:22:29 WIB
        "6e6207bd…",  # duplicate, same repo+commit
    ],
}

# Deadline order (design doc section 1). The five 24h predictions close first.
PREDICTION_TARGETS = [
    "btc-tomorrow",
    "forecast-eth-24h",
    "spy-close-1dollar",
    "forecast-gold-tomorrow",
    "signal-eth-pnl-24h",
    "top5-pons-24h",
    "forecast-nvda-3d",
    "forecast-btc-7d",
]
REPO_TARGETS = ["code-golf-expr", "fastest-primes"]
SELF_SLUG = "best-autonomous-agent"

TRANSCRIPT_LINES = []


def now_wib():
    return datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")


def log(step, result, sub_id=""):
    line = f"{now_wib()} | {step} | {result}" + (f" | {sub_id}" if sub_id else "")
    TRANSCRIPT_LINES.append(line)
    print(line, flush=True)
    with open(TRANSCRIPT_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- HTTP helpers
def http_get(url, params=None, token=None, retries=2):
    headers = dict(UA)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed after retries: {last}")


def http_post(url, payload, token=None, retries=0):
    headers = {"content-type": "application/json", **UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=25)
            return r
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < retries:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"POST {url} failed after retries: {last}")


# ---------------------------------------------------------------- auth
class Agent:
    def __init__(self):
        priv = os.environ.get("AGENT_PRIVATE_KEY", "")
        if not priv:
            raise SystemExit("AGENT_PRIVATE_KEY env var is required")
        self.acct = Account.from_key(priv)
        self.address = self.acct.address
        self.token = None
        self.token_expires = 0

    def load_saved_token(self):
        try:
            d = json.load(open(SESSION_FILE))
            tok, exp = d.get("token"), d.get("expiresAt")
            if tok and exp and int(exp) / 1000 > time.time() + 300:
                self.token, self.token_expires = tok, int(exp) / 1000
                return True
        except Exception:
            pass
        return False

    def authenticate(self):
        chal = http_get(
            f"{API_BASE}/api/auth/challenge", {"address": self.address}
        ).json()["challenge"]
        sig = self.acct.sign_message(encode_defunct(text=chal))
        res = http_post(
            f"{API_BASE}/api/auth/verify",
            {"address": self.address, "signature": "0x" + sig.signature.hex()},
        )
        if res.status_code != 200:
            raise RuntimeError(f"auth verify failed: HTTP {res.status_code}")
        d = res.json()
        self.token = d["token"]
        self.token_expires = int(d.get("expiresAt", 0)) / 1000
        with open(SESSION_FILE, "w") as f:
            json.dump(d, f)
        os.chmod(SESSION_FILE, 0o600)
        log("auth", "EIP-191 verify OK, session token saved (mode 600)")

    def ensure_auth(self):
        if self.token and self.token_expires > time.time() + 300:
            return
        if self.load_saved_token():
            log("auth", "reused saved session token")
            return
        self.authenticate()

    def api(self, method, path, payload=None):
        """Authed call with one re-auth on 401."""
        self.ensure_auth()
        url = f"{API_BASE}{path}"
        r = (
            http_post(url, payload, self.token)
            if method == "POST"
            else http_get(url, token=self.token)
        )
        if r.status_code == 401:
            log("auth", "401 received, re-authenticating")
            self.authenticate()
            r = (
                http_post(url, payload, self.token)
                if method == "POST"
                else http_get(url)
            )
        return r

    def submitted_slugs(self):
        """Slugs we already have a submission for (from /api/me)."""
        try:
            me = self.api("GET", "/api/me").json()
            subs = me.get("submissions") or []
            return {s.get("bountySlug") for s in subs if s.get("bountySlug")}
        except Exception as e:
            log("status", f"GET /api/me failed, proceeding without dedup check: {e}")
            return set()


# ---------------------------------------------------------------- price data
def coingecko_chart(coin, days):
    """-> list of (ts_ms, price); CoinGecko market_chart, no key."""
    d = http_get(
        f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart",
        {"vs_currency": "usd", "days": days},
    ).json()
    return [(int(p[0]), float(p[1])) for p in d.get("prices", [])]


def coin_returns(coin):
    """(r24, r7d) fractional returns from CoinGecko charts."""
    try:
        p1 = coingecko_chart(coin, 1)
        r24 = p1[-1][1] / p1[0][1] - 1 if len(p1) >= 2 else None
    except Exception:
        r24 = None
    try:
        p7 = coingecko_chart(coin, 7)
        r7d = p7[-1][1] / p7[0][1] - 1 if len(p7) >= 2 else None
    except Exception:
        r7d = None
    return r24, r7d


def yahoo_last_close(symbol):
    """Last daily close from Yahoo Finance chart API (no key)."""
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            d = http_get(
                f"https://{host}/v8/finance/chart/{symbol}",
                {"range": "5d", "interval": "1d"},
            ).json()
            res = d["chart"]["result"][0]
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c]
            return float(closes[-1])
        except Exception:
            continue
    raise RuntimeError(f"Yahoo chart failed for {symbol}")


def binary_prob(r_recent, r_long):
    """Design doc section 4.1: prob_up = clamp(0.5 + 3*r_recent + r_long)."""
    if r_recent is None and r_long is None:
        return None
    r_recent = r_recent or 0.0
    r_long = r_long or 0.0
    return clamp(0.5 + 3 * r_recent + r_long, 0.15, 0.85)


def binary_payload(prob):
    """{answer, confidence, prob} consistent (design doc section 3.1)."""
    answer = "yes" if prob >= 0.5 else "no"
    confidence = prob if answer == "yes" else 1 - prob
    return {"answer": answer, "confidence": round(confidence, 4),
            "prob": round(prob, 4)}


# ---------------------------------------------------------------- top5-pons
def top5_pons():
    """Fresh Pons tokens (createdAt <= 24h), top 5 by marketCapUsd.

    Source found 2026-11-13: GET {PONS_BASE}/api/launches?sort=newest
    (the Next.js client fetch; page 1 = 40 newest, cursor pagination).
    Returns list of 5 token addresses.
    """
    cutoff = int(time.time()) - 86400
    fresh, cursor, pages = [], None, 0
    while len(fresh) < 5 and pages < 5:
        params = {"sort": "newest"}
        if cursor:
            params["cursor"] = cursor
        d = http_get(f"{PONS_BASE}/api/launches", params).json()
        items = d.get("items", [])
        if not items:
            break
        fresh += [it for it in items if int(it.get("createdAt", 0)) >= cutoff]
        cursor = d.get("nextCursor")
        pages += 1
        if not cursor:
            break
    if len(fresh) < 5:
        raise RuntimeError(f"only {len(fresh)} fresh Pons tokens found, need 5")
    top = sorted(fresh, key=lambda x: float(x.get("marketCapUsd") or 0),
                 reverse=True)[:5]
    return top


# ---------------------------------------------------------------- predictions
def compute_prediction(slug):
    """-> prediction dict per design doc section 3, or None to skip."""
    if slug in ("btc-tomorrow", "forecast-eth-24h"):
        coin = "bitcoin" if slug == "btc-tomorrow" else "ethereum"
        r24, r7d = coin_returns(coin)
        prob = binary_prob(r24, r7d)
        if prob is None:
            log(slug, "SKIP: CoinGecko chart data unavailable")
            return None
        log(slug, f"r24={r24}, r7d={r7d}, prob_up={prob:.4f}")
        return binary_payload(prob)
    if slug == "forecast-btc-7d":
        _, r7d = coin_returns("bitcoin")
        try:
            p30 = coingecko_chart("bitcoin", 30)
            r30d = p30[-1][1] / p30[0][1] - 1 if len(p30) >= 2 else None
        except Exception:
            r30d = None
        prob = binary_prob(r7d, r30d)
        if prob is None:
            log(slug, "SKIP: CoinGecko chart data unavailable")
            return None
        log(slug, f"r7d={r7d}, r30d={r30d}, prob_up={prob:.4f}")
        return binary_payload(prob)
    if slug in ("spy-close-1dollar", "forecast-gold-tomorrow", "forecast-nvda-3d"):
        symbol = {"spy-close-1dollar": "SPY",
                  "forecast-gold-tomorrow": "GLD",
                  "forecast-nvda-3d": "NVDA"}[slug]
        # Naive random walk: prediction = last close (design doc 4.2).
        last = yahoo_last_close(symbol)
        log(slug, f"{symbol} last close={last}")
        return {"answer": last}
    if slug == "signal-eth-pnl-24h":
        r24, r7d = coin_returns("ethereum")
        prob = binary_prob(r24, r7d)
        if prob is None:
            log(slug, "SKIP: CoinGecko chart data unavailable")
            return None
        direction = "long" if prob >= 0.5 else "short"
        confidence = min(0.8, abs(prob - 0.5) * 2 + 0.5)
        log(slug, f"ETH r24={r24}, r7d={r7d}, prob_up={prob:.4f} -> {direction}")
        return {"answer": direction, "confidence": round(confidence, 4)}
    if slug == "top5-pons-24h":
        top = top5_pons()
        syms = ", ".join(f"{t.get('symbol')}({t.get('address','')[:10]}…)"
                         for t in top)
        log(slug, f"top5 fresh by marketCapUsd: {syms}")
        return {"answer": [t["address"] for t in top]}
    raise ValueError(f"no prediction rule for {slug}")


# ---------------------------------------------------------------- repos config
def load_repos():
    repos = {}
    try:
        repos = json.load(open(REPOS_FILE))
    except FileNotFoundError:
        log("config", f"repos.json not found at {REPOS_FILE}, repo submits skipped")
    except Exception as e:
        log("config", f"repos.json unreadable ({e}), repo submits skipped")
    env_map = {
        "code-golf-expr": ("REPO_CODE_GOLF_URL", "REPO_CODE_GOLF_COMMIT"),
        "fastest-primes": ("REPO_FASTEST_PRIMES_URL", "REPO_FASTEST_PRIMES_COMMIT"),
        "best-autonomous-agent": ("REPO_BEST_AGENT_URL", "REPO_BEST_AGENT_COMMIT"),
    }
    for slug, (u_key, c_key) in env_map.items():
        url, commit = os.environ.get(u_key, ""), os.environ.get(c_key, "")
        if url or commit:
            repos.setdefault(slug, {}).update(
                {k: v for k, v in (("repoUrl", url), ("commit", commit)) if v})
    return repos


# ---------------------------------------------------------------- submits
def submit_prediction(agent, slug, prediction, already):
    if slug in already:
        log(slug, "SKIP: already submitted (per /api/me)")
        return None
    try:
        agent.api("POST", f"/api/bounties/{slug}/enter", {"handle": HANDLE})
    except Exception as e:
        log(slug, f"enter failed (continuing, submit auto-enters): {e}")
    try:
        r = agent.api("POST", f"/api/bounties/{slug}/submit",
                      {"prediction": prediction, "handle": HANDLE})
    except RuntimeError as e:
        # Transport error: maybe it landed. Check /api/me before any retry.
        log(slug, f"submit transport error, checking /api/me: {e}")
        if slug in agent.submitted_slugs():
            log(slug, "submit actually landed (found in /api/me)")
            return "landed-unconfirmed-id"
        raise
    if r.status_code not in (200, 201):
        log(slug, f"submit FAILED: HTTP {r.status_code}: {r.text[:200]}")
        return None
    d = r.json()
    sub_id = d.get("id", "")
    status = d.get("status", "")
    log(slug, f"submit OK status={status} prediction={json.dumps(prediction)}",
        sub_id)
    return sub_id


def submit_repo(agent, slug, repos, already):
    if slug in already:
        log(slug, "SKIP: already submitted (per /api/me)")
        return None
    entry = repos.get(slug) or {}
    url, commit = entry.get("repoUrl", ""), entry.get("commit", "")
    if not url or not commit:
        log(slug, "SKIP: repoUrl/commit not configured (repos.json or env)")
        return None
    transcript = "\n".join(TRANSCRIPT_LINES)
    payload = {"repoUrl": url, "commit": commit,
               "transcript": transcript, "handle": HANDLE}
    try:
        agent.api("POST", f"/api/bounties/{slug}/enter", {"handle": HANDLE})
    except Exception as e:
        log(slug, f"enter failed (continuing): {e}")
    try:
        r = agent.api("POST", f"/api/bounties/{slug}/submit", payload)
    except RuntimeError as e:
        log(slug, f"submit transport error, checking /api/me: {e}")
        if slug in agent.submitted_slugs():
            log(slug, "submit actually landed (found in /api/me)")
            return "landed-unconfirmed-id"
        raise
    if r.status_code not in (200, 201):
        log(slug, f"submit FAILED: HTTP {r.status_code}: {r.text[:200]}")
        return None
    d = r.json()
    log(slug, f"submit OK status={d.get('status','')} repo={url} commit={commit}",
        d.get("id", ""))
    return d.get("id", "")


# ---------------------------------------------------------------- main
def main():
    agent = Agent()
    # Transcript: first line must name wallet + handle (provenance grader).
    with open(TRANSCRIPT_FILE, "w", encoding="utf-8") as f:
        f.write("")
    log("start",
        f"agent={HANDLE} wallet={agent.address} api={API_BASE} "
        "autonomous loop start (no human in the loop)")
    try:
        skill = http_get(f"{API_BASE}/skill.md").text
        log("skill", f"GET /skill.md OK ({len(skill)} bytes)")
        agent.ensure_auth()
        log("auth", f"authenticated as {agent.address}")
        bounties = http_get(f"{API_BASE}/api/bounties",
                            {"status": "open"}).json()
        items = bounties.get("bounties") or bounties.get("items") or bounties
        slugs = {b.get("slug") for b in items} if isinstance(items, list) else set()
        log("list", f"GET /api/bounties?status=open OK ({len(slugs)} open)")
        already = agent.submitted_slugs()
        for slug, ids in ALREADY_SUBMITTED.items():
            log(slug, f"SKIP: already submitted, provenance_ok ids={','.join(ids)}")
        repos = load_repos()
        done, skipped = [], []
        for slug in PREDICTION_TARGETS:
            if slug not in slugs:
                log(slug, "SKIP: not in open list")
                skipped.append(slug)
                continue
            try:
                pred = compute_prediction(slug)
            except Exception as e:
                log(slug, f"SKIP: prediction failed: {e}")
                skipped.append(slug)
                continue
            if pred is None:
                skipped.append(slug)
                continue
            try:
                sub = submit_prediction(agent, slug, pred, already)
                (done if sub else skipped).append(slug)
            except Exception as e:
                log(slug, f"submit ERROR: {e}")
                skipped.append(slug)
        for slug in REPO_TARGETS:
            if slug not in slugs:
                log(slug, "SKIP: not in open list")
                skipped.append(slug)
                continue
            try:
                sub = submit_repo(agent, slug, repos, already)
                (done if sub else skipped).append(slug)
            except Exception as e:
                log(slug, f"submit ERROR: {e}")
                skipped.append(slug)
        # Self-submit last: this repo + the full transcript of this loop.
        try:
            sub = submit_repo(agent, SELF_SLUG, repos, already)
            (done if sub else skipped).append(SELF_SLUG)
        except Exception as e:
            log(SELF_SLUG, f"submit ERROR: {e}")
            skipped.append(SELF_SLUG)
        try:
            me = agent.api("GET", "/api/me").json()
            log("status",
                f"GET /api/me: competing={len(me.get('competing', []))} "
                f"submissions={len(me.get('submissions', []))}")
        except Exception as e:
            log("status", f"GET /api/me failed: {e}")
        log("done", f"completed={done} skipped={skipped}")
        print(f"\nSUMMARY completed={done} skipped={skipped}")
        return 0
    except Exception as e:
        log("fatal", f"{type(e).__name__}: {e}")
        print(f"FATAL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
