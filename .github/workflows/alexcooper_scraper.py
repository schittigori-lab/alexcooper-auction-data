# coding: utf-8
"""
Alex Cooper Auctioneers - Foreclosure Scraper
==============================================
Scrapes all upcoming foreclosure auctions from realestate.alexcooper.com/foreclosures
Requires login to access full detail pages.

OUTPUT: alexcooper_auctions.json  (uploaded to GitHub Pages)
RUN:    py alexcooper_scraper.py

Credentials stored in .env file (never hardcoded):
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

# Credentials — set in .env file or GitHub Secrets
AC_EMAIL    = os.getenv("AC_EMAIL", "")
AC_PASSWORD = os.getenv("AC_PASSWORD", "")

# GitHub config
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
    """Log in to Alex Cooper and return True on success."""
    print("  Logging in to Alex Cooper...")
    await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(1500)

    # Fill email
    await page.fill(
        'input[type="email"], input[name="email"], input[placeholder*="Email" i]',
        AC_EMAIL
    )
    await page.wait_for_timeout(400)

    # Fill password
    await page.fill('input[type="password"]', AC_PASSWORD)
    await page.wait_for_timeout(400)

    # Submit
    await page.click(
        'button[type="submit"], input[type="submit"], '
        'button:has-text("Login"), button:has-text("Sign In")'
    )
    await page.wait_for_timeout(3000)

    content = await page.content()
    if "logout" in content.lower() or "my activity" in content.lower():
        print("  Login successful!")
        return True
    else:
        print("  WARNING: Login may have failed — check AC_EMAIL / AC_PASSWORD in .env")
        return False


# ── Scrape listings (primary: DOM selectors) ───────────────────────────────────
async def scrape_listings(page):
    """Navigate to foreclosures page and extract all lot listings."""
    print("  Loading foreclosures page...")
    await page.goto(FORECLOSURES_URL, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(3000)  # let Angular finish rendering

    auctions = []
    seen     = set()

    lot_cards = await page.query_selector_all(
        '.lot-card, .property-card, [class*="lot-item"], [class*="property-item"]'
    )

    if not lot_cards:
        print("  Standard selectors not matched — trying API interception fallback...")
        return await scrape_listings_via_api(page)

    print(f"  Found {len(lot_cards)} lot cards")

    for card in lot_cards:
        try:
            # Address / title
            address = ""
            title_el = await card.query_selector('.lot-title, .property-title, h3, h4, h5')
            if title_el:
                address = (await title_el.inner_text()).strip()

            # Date
            auction_date = ""
            date_el = await card.query_selector('[class*="date"], time')
            if date_el:
                auction_date = (await date_el.inner_text()).strip()

            # Time (extracted from date string)
            auction_time = ""
            time_m = re.search(r'\d+:\d+\s*[AP]M', auction_date, re.IGNORECASE)
            if time_m:
                auction_time = time_m.group(0)

            # County / location
            auction_location = ""
            county_el = await card.query_selector('[class*="county"], [class*="location"]')
            if county_el:
                auction_location = (await county_el.inner_text()).strip()

            # Deposit
            bid_deposit = ""
            deposit_el = await card.query_selector('[class*="deposit"]')
            if deposit_el:
                raw = (await deposit_el.inner_text()).strip()
                bid_deposit = re.sub(r'[Dd]eposit:?\s*', '', raw).strip()

            # Opening bid
            opening_bid = ""
            bid_el = await card.query_selector('[class*="opening"], [class*="starting"], [class*="price"]')
            if bid_el:
                opening_bid = (await bid_el.inner_text()).strip()

            # Detail URL
            detail_url = ""
            link_el = await card.query_selector('a')
            if link_el:
                href = await link_el.get_attribute('href')
                if href:
                    detail_url = (
                        BASE_URL + href if href.startswith('/') else href
                    )

            if not address:
                continue
            if detail_url in seen:
                continue
            if detail_url:
                seen.add(detail_url)

            auctions.append({
                "auction_date":     auction_date,
                "property_address": address,
                "auction_time":     auction_time,
                "auction_location": auction_location,
                "bid_deposit":      bid_deposit,
                "opening_bid":      opening_bid,
                "detail_url":       detail_url,
            })

        except Exception as e:
            print(f"    Warning: error parsing card — {e}")
            continue

    return auctions


# ── Fallback: intercept AuctionMobility API responses ─────────────────────────
async def scrape_listings_via_api(page):
    """
    AuctionMobility Angular apps load data from a JSON API.
    We intercept those network responses during page load.
    """
    auctions  = []
    api_data  = []

    async def handle_response(response):
        if "/api/" in response.url and response.status == 200:
            try:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    data = await response.json()
                    api_data.append(data)
            except Exception:
                pass

    page.on("response", handle_response)
    await page.goto(FORECLOSURES_URL, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(3000)

    print(f"  Intercepted {len(api_data)} API response(s)")

    seen = set()
    for data in api_data:
        # AuctionMobility returns {"lots": [...]} or {"results": [...]}
        lots = []
        if isinstance(data, dict):
            lots = data.get("lots", data.get("results", data.get("data", [])))
        elif isinstance(data, list):
            lots = data

        for lot in lots:
            if not isinstance(lot, dict):
                continue

            auction      = lot.get("auction", {})
            detail_path  = lot.get("_detail_url", "")
            detail_url   = BASE_URL + "/" + detail_path.lstrip("/") if detail_path else ""

            if detail_url in seen:
                continue
            if detail_url:
                seen.add(detail_url)

            # Format date from timestamp if needed
            raw_date = auction.get("time_start") or auction.get("date", "")
            auction_date = _safe(raw_date)

            auction_time = ""
            time_m = re.search(r'\d+:\d+\s*[AP]M', auction_date, re.IGNORECASE)
            if time_m:
                auction_time = time_m.group(0)

            auctions.append({
                "auction_date":     auction_date,
                "property_address": _safe(lot.get("title") or lot.get("lot_location", "")),
                "auction_time":     auction_time,
                "auction_location": _safe(lot.get("lot_location") or auction.get("county", "")),
                "bid_deposit":      _safe(lot.get("deposit_amount", "")),
                "opening_bid":      _safe(lot.get("starting_price", "")),
                "detail_url":       detail_url,
            })

    if not auctions:
        print("  No data from API interception either.")
        print("  Saving debug snapshot to alexcooper_debug.html...")
        content = await page.content()
        with open("alexcooper_debug.html", "w", encoding="utf-8") as f:
            f.write(content)
        print("  Open alexcooper_debug.html to inspect the page structure.")

    return auctions


# ── Scrape individual detail page ──────────────────────────────────────────────
async def scrape_detail(page, url):
    """Visit a foreclosure detail page and extract trustee / balance info."""
    try:
        await page.goto(url, wait_until="networkidle", timeout=25000)
        await page.wait_for_timeout(1000)
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

        # Phone number (last one = firm contact)
        phone = ""
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
        return {
            "principal_balance":  "",
            "substitute_trustee": "",
            "trustee_phone":      "",
        }


# ── Helper ─────────────────────────────────────────────────────────────────────
def _safe(val):
    return "" if val is None else str(val).strip()


# ── Save JSON ──────────────────────────────────────────────────────────────────
def save_json(auctions):
    if not auctions:
        print("\n  No auction data to save.")
        return

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
        print("\n  GitHub upload skipped — GITHUB_TOKEN not set in .env")
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

    # Fetch existing SHA (needed to update an existing file)
    sha = None
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
        print("\n  ERROR: AC_EMAIL and AC_PASSWORD must be set in your .env file.")
        print("  Create a .env file alongside this script with:")
        print("    AC_EMAIL=your@email.com")
        print("    AC_PASSWORD=yourpassword")
        sys.exit(1)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        page    = await context.new_page()

        # 1. Login
        logged_in = await login(page)
        if not logged_in:
            print("  Proceeding anyway — detail data may be limited.")

        # 2. Scrape listings
        print("\n  Scraping foreclosure listings...")
        auctions = await scrape_listings(page)
        print(f"  Found {len(auctions)} listings")

        if not auctions:
            await browser.close()
            return

        # 3. Scrape detail pages
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

    # 4. Save + upload
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
