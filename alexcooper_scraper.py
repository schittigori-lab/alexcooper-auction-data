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
from datetime import date, datetime

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
    all_json_urls = []  # log every JSON url we see for debugging

    async def handle_response(response):
        try:
            url = response.url
            ct  = response.headers.get("content-type", "")
            if response.status == 200 and "json" in ct:
                all_json_urls.append(url)
                # Broad capture: any JSON response (so we don't miss the listings endpoint)
                data = await response.json()
                print(f"    JSON hit: {url[:100]}")
                api_data.append({"url": url, "data": data})
        except Exception:
            pass

    page.on("response", handle_response)

    print("  Loading foreclosures page...")
    # The page is server-rendered by Angular — lots appear in initial HTML.
    # Use networkidle so Angular finishes rendering before we read the DOM.
    await page.goto(FORECLOSURES_URL, wait_until="networkidle", timeout=45000)
    await page.wait_for_timeout(3000)

    print(f"  Intercepted {len(api_data)} JSON response(s) (informational)")
    print(f"  All JSON URLs seen: {all_json_urls}")

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

    # Always save debug snapshot so we can inspect what the page looks like
    content = await page.content()
    with open("alexcooper_debug.html", "w", encoding="utf-8") as f:
        f.write(content)
    print("  Saved alexcooper_debug.html")
    if not auctions:
        print("  No listings found — check alexcooper_debug.html and workflow logs")

    return auctions


# ── DOM scraper — Alex Cooper foreclosure page structure ──────────────────────
async def scrape_listings_dom(page):
    """
    The foreclosure page renders lots server-side inside Angular ng-repeat.
    Structure (siblings in DOM order):
      .foreclosure-date-header  → tracks current auction date
      .alexcooper-foreclosure-container  → one lot, contains:
          .county-title          → county name (only on first lot per county)
          .foreclosure-lot       → has class 'cancelled'/'postponed' if not active
              .foreclosure-title → "9:30 am 123 Main St, City, ZIP Dep. $20,000"
          .foreclosure-location-description .location-value → courthouse address
    """
    raw = await page.evaluate(r"""
    () => {
        const BASE = 'https://realestate.alexcooper.com';
        const results = [];
        const elements = document.querySelectorAll(
            '.foreclosure-date-header, .alexcooper-foreclosure-container'
        );

        let currentDate   = '';
        let currentCounty = '';

        for (const el of elements) {
            if (el.classList.contains('foreclosure-date-header')) {
                const month = el.querySelector('.full-date .month');
                const day   = el.querySelector('.full-date .date');
                const year  = el.querySelector('.full-date .year');
                const m = month ? month.textContent.trim() : '';
                const d = day   ? day.textContent.replace(/[^0-9]/g, '').trim() : '';
                const y = year  ? year.textContent.trim() : String(new Date().getFullYear());
                currentDate = d && m ? `${m} ${d}, ${y}` : '';
            } else {
                const idMatch    = el.className.match(/list-lot-id-([\w-]+)/);
                const lotId      = idMatch ? idMatch[1] : '';
                const numericId  = (el.id || '').replace('list-lot-', '');
                const countyEl   = el.querySelector('.county-title');
                const titleEl    = el.querySelector('.foreclosure-title');
                const locationEl = el.querySelector('.location-value');
                const lotEl      = el.querySelector('.foreclosure-lot');
                const cancelled  = lotEl && lotEl.classList.contains('cancelled');
                const postponed  = lotEl && lotEl.classList.contains('postponed');

                // County header only appears on first lot per county — track it
                if (countyEl) currentCounty = countyEl.textContent.trim();

                results.push({
                    lotId,
                    numericId,
                    date:     currentDate,
                    county:   currentCounty,
                    title:    titleEl    ? titleEl.textContent.trim()    : '',
                    location: locationEl ? locationEl.textContent.trim() : '',
                    cancelled,
                    postponed,
                    detailUrl: numericId ? `${BASE}/lots/${numericId}` : '',
                });
            }
        }
        return results;
    }
    """)

    print(f"  DOM: found {len(raw)} lot element(s) on page")

    auctions = []
    last_location = ''  # location only appears on last lot per auction group

    for item in reversed(raw):
        if item.get('location'):
            last_location = item['location']
        item['_loc'] = last_location

    last_location = ''
    for item in raw:
        if item.get('location'):
            last_location = item['location']
        else:
            # Use forward-propagated location, or backward-looked-ahead _loc if not available yet
            item['location'] = last_location or item.get('_loc', '')

    for item in raw:
        title = item.get('title', '')
        if not title:
            continue

        # Parse "9:30 am 123 Main St, City, ZIP Dep. $20,000"
        time_m = re.match(r'^(\d+:\d+\s*(?:am|pm))\s+', title, re.IGNORECASE)
        auction_time = time_m.group(1).upper() if time_m else ''
        remaining    = title[len(time_m.group(0)):].strip() if time_m else title

        dep_m   = re.search(r'Dep\.?\s*\$?([\d,]+)', remaining, re.IGNORECASE)
        deposit = f'${dep_m.group(1)}' if dep_m else ''
        address = remaining[:dep_m.start()].strip() if dep_m else remaining.strip()

        status = 'cancelled' if item.get('cancelled') else 'postponed' if item.get('postponed') else 'active'

        auctions.append({
            'auction_date':     item.get('date', ''),
            'property_address': address,
            'auction_time':     auction_time,
            'auction_location': item.get('location', ''),
            'bid_deposit':      deposit,
            'opening_bid':      '',
            'detail_url':       item.get('detailUrl', ''),
            'status':           status,
            'county':           item.get('county', ''),
        })

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

        # Auction date fallback — parsed from detail page body text
        # e.g. "APRIL 28, 2026 AT 9:15 AM" inside a <b> tag
        detail_date = ""
        date_m = re.search(
            r'(January|February|March|April|May|June|July|August|September'
            r'|October|November|December)\s+(\d+),?\s*(\d{4})',
            text, re.IGNORECASE
        )
        if date_m:
            detail_date = f"{date_m.group(1).capitalize()} {date_m.group(2)}, {date_m.group(3)}"

        return {
            "principal_balance":  principal,
            "substitute_trustee": trustee,
            "trustee_phone":      phone,
            "detail_date":        detail_date,
        }

    except Exception as e:
        print(f"    Warning: detail page error — {e}")
        return {"principal_balance": "", "substitute_trustee": "", "trustee_phone": "", "detail_date": ""}


# ── SDAT Assessed Value Lookup (opendata.maryland.gov API) ───────────────────

SDAT_API_URL    = 'https://opendata.maryland.gov/resource/ed4q-f8tm.json'
SDAT_CACHE_FILE = 'sdat_cache.json'

# Maps internal county name -> dataset's county_desc field value
SDAT_COUNTY_MAP = {
    'Allegany':        'ALLEGANY',
    'Anne Arundel':    'ANNE ARUNDEL',
    'Baltimore City':  'BALTIMORE CITY',
    'Baltimore':       'BALTIMORE',
    'Calvert':         'CALVERT',
    'Caroline':        'CAROLINE',
    'Carroll':         'CARROLL',
    'Cecil':           'CECIL',
    'Charles':         'CHARLES',
    'Dorchester':      'DORCHESTER',
    'Frederick':       'FREDERICK',
    'Garrett':         'GARRETT',
    'Harford':         'HARFORD',
    'Howard':          'HOWARD',
    'Kent':            'KENT',
    'Montgomery':      'MONTGOMERY',
    "Prince George's": "PRINCE GEORGE'S",
    "Queen Anne's":    "QUEEN ANNE'S",
    "St. Mary's":      "ST. MARY'S",
    'Somerset':        'SOMERSET',
    'Talbot':          'TALBOT',
    'Washington':      'WASHINGTON',
    'Wicomico':        'WICOMICO',
    'Worcester':       'WORCESTER',
}

def load_sdat_cache():
    if os.path.exists(SDAT_CACHE_FILE):
        try:
            with open(SDAT_CACHE_FILE, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_sdat_cache(cache):
    with open(SDAT_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

_SDAT_API_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}
_DIRECTIONS = {
    'N','S','E','W','NE','NW','SE','SW',
    'NORTH','SOUTH','EAST','WEST','NORTHEAST','NORTHWEST','SOUTHEAST','SOUTHWEST',
}
_SUFFIXES = {
    'AVE','AVENUE','BLVD','BOULEVARD','CIR','CIRCLE','CT','COURT',
    'DR','DRIVE','LN','LANE','PKWY','PARKWAY','PL','PLACE',
    'RD','ROAD','ST','STREET','TER','TERR','TERRACE','TRL','TRAIL',
    'WAY','HWY','HIGHWAY','RUN','PASS','PATH','LOOP','PIKE','XING','CROSSING',
}

def sdat_api_lookup(address, county_name):
    """Query opendata.maryland.gov for property assessed value by address.

    Uses num%keyword% LIKE search (avoids compound WHERE / Cloudflare issues),
    then filters by county client-side.
    """
    try:
        clean = address.split(',')[0].strip().upper()
        parts = clean.split()
        if not parts or not parts[0][0].isdigit():
            return ''
        street_num = parts[0]
        name_words = parts[1:]
        while name_words and name_words[0] in _DIRECTIONS:
            name_words = name_words[1:]
        while name_words and name_words[-1] in _SUFFIXES:
            name_words = name_words[:-1]
        if not name_words:
            return ''
        keyword = name_words[0]

        resp = requests.get(
            SDAT_API_URL,
            headers=_SDAT_API_HEADERS,
            params={
                '$where': f"mdp_street_address_mdp_field_address like '{street_num}%{keyword}%'",
                '$limit': '20',
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return ''
        rows = resp.json()

        matches = [
            r for r in rows
            if county_name.lower() in r.get('county_name_mdp_field_cntyname', '').lower()
        ]
        if not matches:
            return ''

        row = matches[0]
        land = float(row.get('base_cycle_data_land_value_sdat_field_154') or 0)
        imp  = float(row.get('base_cycle_data_improvements_value_sdat_field_155') or 0)
        total = land + imp
        return '${:,.0f}'.format(total) if total else ''

    except Exception as e:
        print(f'    SDAT API error: {e}')
        return ''

def get_sdat_value(address, county, cache):
    """Return cached or freshly-fetched SDAT assessed value."""
    cache_key = f'{address}|{county}'
    entry = cache.get(cache_key)
    if entry:
        try:
            age = (datetime.now() - datetime.strptime(entry['lookup_date'], '%Y-%m-%d')).days
            if age < 90:
                val = entry.get('full_cash_value', '')
                print(f'    SDAT cached: {val or "not found"}')
                return val
        except Exception:
            pass

    if county not in SDAT_COUNTY_MAP:
        return ''

    print(f'    SDAT lookup: {address} ({county})')
    value = sdat_api_lookup(address, county)
    if value:
        cache[cache_key] = {
            'full_cash_value': value,
            'lookup_date': datetime.now().strftime('%Y-%m-%d'),
        }
        save_sdat_cache(cache)
    return value


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
        "status":             a.get("status", "active"),
        "county":             a.get("county", ""),
        "full_cash_value":    a.get("full_cash_value", ""),
    } for a in auctions]

    output = {
        "last_updated":   datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
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
                    # Use date from detail page as fallback if listing date was empty
                    if not auction.get("auction_date") and details.get("detail_date"):
                        auction["auction_date"] = details["detail_date"]
                        print(f"      Date recovered from detail page: {details['detail_date']}")
                    auction.update(details)
                else:
                    auction.update({
                        "principal_balance":  "",
                        "substitute_trustee": "",
                        "trustee_phone":      "",
                    })
                await asyncio.sleep(0.5)

        await browser.close()

    # SDAT assessed value enrichment (opendata.maryland.gov API — no browser needed)
    if auctions:
        print(f"\n  Enriching {len(auctions)} auctions with SDAT values...")
        sdat_cache = load_sdat_cache()
        for auction in auctions:
            county = auction.get('county', '')
            if county and auction.get('property_address'):
                auction['full_cash_value'] = get_sdat_value(
                    auction['property_address'], county, sdat_cache)
            else:
                auction['full_cash_value'] = ''

    # 4. Always save JSON (even if empty, so workflow doesn't crash)
    save_json(auctions)

    # Upload via GitHub API only when running locally (workflow uses git push)
    if os.getenv("UPLOAD_TO_GITHUB", "").lower() == "true":
        upload_to_github(OUTPUT_JSON)
    else:
        print("\n  (GitHub upload skipped — workflow will commit the file via git push)")


if __name__ == "__main__":
    print("=" * 60)
    print("  Alex Cooper Auctioneers - Foreclosure Scraper")
    print("  Source: realestate.alexcooper.com/foreclosures")
    print("=" * 60)
    asyncio.run(main())
    print("\n  Done!")
    print(f"  Output: {OUTPUT_JSON}")
