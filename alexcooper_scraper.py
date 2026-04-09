# coding: utf-8
"""
Alex Cooper Auctioneers - Foreclosure Scraper
==============================================
Scrapes all upcoming foreclosure auctions from realestate.alexcooper.com/foreclosures
Requires login to access full detail pages.

OUTPUT: alexcooper_auctions.json  (uploaded to GitHub Pages)
RUN:    py alexcooper_scraper.py

Credentials stored in .env file or GitHub Secrets:
  AC_EMAIL=your@email.com
  AC_PASSWORD=yourpassword
  GITHUB_TOKEN=ghp_xxxx
  GITHUB_USERNAME=schittigori-lab
  GITHUB_REPO=alexcooper-auction-data
"""

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
from datetime import date

# ── Auto-install dependencies ──────────────────────────────────────────────────
def install(pkg):
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

try:
    import requests
except ImportError:
    install("requests")
    import requests

try:
    from playwright.async_api import async_playwright
except ImportError:
    install("playwright")
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
    from playwright.async_api import async_playwright

try:
    from dotenv import load_dotenv
except ImportError:
    install("python-dotenv")
    from dotenv import load_dotenv


# ── Config ─────────────────────────────────────────────────────────────────────
load_dotenv()

FORECLOSURES_URL = "https://realestate.alexcooper.com/foreclosures"
LOGIN_URL        = "https://realestate.alexcooper.com/login"
BASE_URL         = "https://realestate.alexcooper.com"
OUTPUT_JSON      = "alexcooper_auctions.json"

AC_EMAIL    = os.getenv("AC_EMAIL", "")
AC_PASSWORD = os.getenv("AC_PASSWORD", "")

GITHUB_TOKEN    = os.getenv("GITHUB_TOKEN", "")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "schittigori-lab")
GITHUB_REPO     = os.getenv("GITHUB_REPO", "alexcooper-auction-data")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


# ── Login ──────────────────────────────────────────────────────────────────────
async def login(page):
    print("  Logging in to Alex Cooper...")
    print(f"  Using email: {AC_EMAIL[:4]}****")  # partial log to confirm secret loaded

    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)  # wait for Angular to render login form

    # Try all common email input selectors
    email_selectors = [
        'input[type="email"]',
        'input[name="email"]',
        'input[placeholder*="Email" i]',
        'input[placeholder*="email" i]',
        'input[ng-model*="email" i]',
    ]
    filled_email = False
    for sel in email_selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await page.fill(sel, AC_EMAIL)
                filled_email = True
                print(f"  Email filled using selector: {sel}")
                break
        except Exception:
            continue

    if not filled_email:
        print("  WARNING: Could not find email input field")

    await page.wait_for_timeout(500)

    # Fill password
    try:
        await page.fill('input[type="password"]', AC_PASSWORD)
        print("  Password filled")
    except Exception as e:
        print(f"  WARNING: Could not fill password — {e}")

    await page.wait_for_timeout(500)

    # Click submit
    submit_selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Log In")',
        'button:has-text("Login")',
        'button:has-text("Sign In")',
        '[ng-click*="login" i]',
    ]
    clicked = False
    for sel in submit_selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                await page.click(sel)
                clicked = True
                print(f"  Submit clicked using selector: {sel}")
                break
        except Exception:
            continue

    if not clicked:
        print("  WARNING: Could not find submit button — trying Enter key")
        await page.keyboard.press("Enter")

    # Wait for redirect after login
    await page.wait_for_timeout(5000)

    # Check login success
    url_now  = page.url
    content  = await page.content()
    is_logged = (
        "logout" in content.lower()
        or "my activity" in content.lower()
        or "bid-sheet" in url_now
        or "/login" not in url_now
    )

    if is_logged:
        print(f"  Login successful! Current URL: {url_now}")
    else:
        print(f"  WARNING: Login may have failed. Current URL: {url_now}")
        # Save login page debug snapshot
        with open("login_debug.html", "w", encoding="utf-8") as f:
            f.write(content)
        print("  Saved login_debug.html for inspection")

    return is_logged


# ── Intercept API responses ────────────────────────────────────────────────────
async def scrape_listings(page):
    print("  Setting up API interception...")
    api_data = []

    async def handle_response(response):
        try:
            url = response.url
            ct  = response.headers.get("content-type", "")
            # Capture ALL json responses from the alex cooper domain
            if response.status == 200 and "json" in ct and (
                "alexcooper" in url or
                "auctionmobility" in url or
                "auctionatlas" in url
            ):
                data = await response.json()
                print(f"    API hit: {url[:80]}")
                api_data.append({"url": url, "data": data})
        except Exception:
            pass

    page.on("response", handle_response)

    print("  Loading foreclosures page...")
    await page.goto(FORECLOSURES_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(8000)  # give Angular extra time to load data

    print(f"  Intercepted {len(api_data)} API response(s)")

    # Parse API data
    auctions = []
    seen     = set()

    for item in api_data:
        data = item["data"]
        lots = []
        if isinstance(data, dict):
            # Try all common AuctionMobility response keys
            for key in ["lots", "results", "data", "items", "properties"]:
                if key in data and isinstance(data[key], list):
                    lots = data[key]
                    break
        elif isinstance(data, list):
            lots = data

        for lot in lots:
            if not isinstance(lot, dict):
                continue

            auction     = lot.get("auction", {})
            detail_path = lot.get("_detail_url", "")
            detail_url  = BASE_URL + "/" + detail_path.lstrip("/") if detail_path else ""

            if detail_url and detail_url in seen:
                continue
            if detail_url:
                seen.add(detail_url)

            raw_date     = _safe(auction.get("time_start") or auction.get("date", ""))
            auction_time = ""
            tm = re.search(r'\d+:\d+\s*[AP]M', raw_date, re.IGNORECASE)
            if tm:
                auction_time = tm.group(0)

            auctions.append({
                "auction_date":     raw_date,
                "property_address": _safe(lot.get("title") or lot.get("lot_location", "")),
                "auction_time":     auction_time,
                "auction_location": _safe(lot.get("lot_location") or auction.get("county", "")),
                "bid_deposit":      _safe(lot.get("deposit_amount", "")),
                "opening_bid":      _safe(lot.get("starting_price", "")),
                "detail_url":       detail_url,
            })

    # If API interception got nothing, try DOM scraping
    if not auctions:
        print("  No API data — trying DOM scraping...")
        auctions = await scrape_listings_dom(page)

    # If still nothing, save debug snapshot
    if not auctions:
        print("  No listings found — saving debug snapshot...")
        content = await page.content()
        with open("alexcooper_debug.html", "w", encoding="utf-8") as f:
            f.write(content)
        print("  Saved alexcooper_debug.html")

    return auctions


# ── DOM fallback scraper ───────────────────────────────────────────────────────
async def scrape_listings_dom(page):
    auctions = []
    seen     = set()

    # Only grab links that point to actual lot/property detail pages
    # Real property links on AuctionMobility look like /lots/123 or /upcoming/123
    cards = await page.query_selector_all(
        'a[href*="/lots/"], a[href*="/upcoming/"], a[href*="/lot-detail/"]'
    )
    print(f"  DOM: found {len(cards)} property link elements")

    for card in cards:
        try:
            text = (await card.inner_text()).strip()
            href = await card.get_attribute("href") or ""

            # Skip nav links, empty, or very short text
            if not text or len(text) < 10:
                continue
            # Skip if it looks like a nav/menu item
            if any(skip in text.lower() for skip in [
                "all weeks", "foreclosures", "upcoming", "login",
                "register", "contact", "sold", "buy now"
            ]):
                continue

            detail_url = BASE_URL + href if href.startswith("/") else href
            if detail_url in seen:
                continue
            seen.add(detail_url)

            auctions.append({
                "auction_date":     "",
                "property_address": text[:100],
                "auction_time":     "",
                "auction_location": "",
                "bid_deposit":      "",
                "opening_bid":      "",
                "detail_url":       detail_url,
            })
        except Exception:
            continue

    return auctions


# ── Scrape detail page ─────────────────────────────────────────────────────────
async def scrape_detail(page, url):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(2000)
        text = await page.inner_text("body")

        # Principal balance
        principal = ""
        for pattern in [
            r'original principal amount of\s*\$?([\d,]+\.?\d*)',
            r'principal balance of\s*\$?([\d,]+\.?\d*)',
            r'principal sum of\s*\$?([\d,]+\.?\d*)',
        ]:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                principal = "$" + m.group(1)
                break

        # Phone
        phone  = ""
        phones = re.findall(r'\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}', text)
        if phones:
            phone = phones[-1].strip()

        # Substitute Trustee
        trustee = ""
        tm = re.search(
            r'([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+(?:,?\s+et al\.?)?)'
            r',?\s+Substitute Trustees?',
            text
        )
        if tm:
            trustee = tm.group(1).strip()

        return {
            "principal_balance":  principal,
            "substitute_trustee": trustee,
            "trustee_phone":      phone,
        }

    except Exception as e:
        print(f"    Warning: detail page error — {e}")
        return {"principal_balance": "", "substitute_trustee": "", "trustee_phone": ""}


# ── Helper ─────────────────────────────────────────────────────────────────────
def _safe(val):
    return "" if val is None else str(val).strip()


# ── Save JSON (always saves, even if empty) ────────────────────────────────────
def save_json(auctions):
    records = [{
        "auction_date":       a.get("auction_date", ""),
        "property_address":   a.get("property_address", ""),
        "auction_time":       a.get("auction_time", ""),
        "auction_location":   a.get("auction_location", ""),
        "bid_deposit":        a.get("bid_deposit", ""),
        "opening_bid":        a.get("opening_bid", ""),
        "principal_balance":  a.get("principal_balance", ""),
        "substitute_trustee": a.get("substitute_trustee", ""),
        "trustee_phone":      a.get("trustee_phone", ""),
        "detail_url":         a.get("detail_url", ""),
    } for a in auctions]

    output = {
        "last_updated":   date.today().strftime("%Y-%m-%d"),
        "total_auctions": len(records),
        "auctions":       records,
    }

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n  JSON saved: {OUTPUT_JSON}  ({len(records)} auctions)")


# ── Upload to GitHub ───────────────────────────────────────────────────────────
def upload_to_github(json_path):
    if not GITHUB_TOKEN:
        print("\n  GitHub upload skipped — GITHUB_TOKEN not set")
        return

    print("\n  Uploading JSON to GitHub...")
    api_url = (
        f"https://api.github.com/repos/{GITHUB_USERNAME}/{GITHUB_REPO}"
        f"/contents/{OUTPUT_JSON}"
    )
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
    }

    with open(json_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")

    sha   = None
    check = requests.get(api_url, headers=headers)
    if check.status_code == 200:
        sha = check.json().get("sha")

    payload = {
        "message": f"Foreclosure update {date.today().strftime('%Y-%m-%d')}",
        "content": encoded,
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(api_url, headers=headers, json=payload)
    if resp.status_code in (200, 201):
        live_url = f"https://{GITHUB_USERNAME}.github.io/{GITHUB_REPO}/{OUTPUT_JSON}"
        print(f"  GitHub upload successful!")
        print(f"  Live URL: {live_url}")
    else:
        print(f"  GitHub upload failed: {resp.status_code} — {resp.text[:200]}")


# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    if not AC_EMAIL or not AC_PASSWORD:
        print("\n  ERROR: AC_EMAIL and AC_PASSWORD not found.")
        print("  Add them as GitHub Secrets or in a local .env file.")
        sys.exit(1)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        page    = await context.new_page()

        # 1. Login
        await login(page)

        # 2. Scrape listings
        print("\n  Scraping foreclosure listings...")
        auctions = await scrape_listings(page)
        print(f"  Found {len(auctions)} listings")

        # 3. Scrape detail pages (only if listings found)
        if auctions:
            print(f"\n  Fetching details for {len(auctions)} listings...\n")
            for i, auction in enumerate(auctions):
                url  = auction.get("detail_url", "")
                addr = auction.get("property_address", "")[:45]
                print(f"    [{i+1}/{len(auctions)}] {addr}...")
                if url:
                    details = await scrape_detail(page, url)
                    auction.update(details)
                else:
                    auction.update({
                        "principal_balance":  "",
                        "substitute_trustee": "",
                        "trustee_phone":      "",
                    })
                await asyncio.sleep(0.5)

        await browser.close()

    # 4. Always save JSON (even if empty, so workflow doesn't crash)
    save_json(auctions)
    upload_to_github(OUTPUT_JSON)


if __name__ == "__main__":
    print("=" * 60)
    print("  Alex Cooper Auctioneers - Foreclosure Scraper")
    print("  Source: realestate.alexcooper.com/foreclosures")
    print("=" * 60)
    asyncio.run(main())
    print("\n  Done!")
    print(f"  Output: {OUTPUT_JSON}")
