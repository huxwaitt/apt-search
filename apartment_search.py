#!/usr/bin/env python3
"""
Apartment Finder v1.0
Multi-platform apartment search with interactive HTML report generation.
Searches Zillow, Apartments.com, Craigslist, and more.
"""

import sys, json, time, random, re, os, argparse, urllib.parse, webbrowser, math, asyncio, shutil
from pathlib import Path
from datetime import datetime

# Ensure Unicode prints cleanly on Windows consoles (cp1252 chokes on box glyphs)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── Auto-install dependencies ─────────────────────────────────────────────────
def _ensure(pkgs):
    import subprocess
    for pip_name, import_name in pkgs:
        try:
            __import__(import_name)
        except ImportError:
            print(f"  Installing {pip_name}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name, "-q"])

print("Checking dependencies...")
# Selenium 4.6+ ships Selenium Manager, which auto-resolves the browser driver —
# no webdriver-manager needed. We drive whichever Chromium browser is installed.
_ensure([
    ("requests", "requests"),
    ("beautifulsoup4", "bs4"),
    ("selenium", "selenium"),
    ("playwright", "playwright"),  # drives real Edge/Chrome to get past Zillow's PerimeterX
])

import requests
from bs4 import BeautifulSoup

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.edge.options import Options as EdgeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    SELENIUM = True
except Exception as _e:
    SELENIUM = False
    print(f"Warning: Selenium unavailable ({_e}) – browser searches will be skipped.")

# ── Constants ─────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ── Project paths / per-run output folder ─────────────────────────────────────
def project_dir():
    """Directory this script lives in — the project root. Works on macOS and Windows."""
    return Path(__file__).resolve().parent

def _slug(text):
    return re.sub(r"[^a-z0-9._-]+", "-", str(text).lower()).strip("-")

def make_run_dir(criteria):
    """Create and return `<project>/data/<yyyy.mm.dd>_<run info>/` for this run.

    The folder name encodes the search (location, beds, max price) so each run is
    self-describing. Uses pathlib throughout, so it behaves the same on macOS and Windows;
    a time suffix is added only if a same-named folder already exists that day."""
    date = datetime.now().strftime("%Y.%m.%d")
    parts = [criteria.get("location") or "search", criteria.get("state") or ""]
    beds = criteria.get("bedrooms")
    if beds is not None:
        parts.append("studio" if beds == 0 else f"{beds}bd")
    if criteria.get("max_price"):
        parts.append(f"max{criteria['max_price']}")
    info = _slug("-".join(str(p) for p in parts if p)) or "search"
    base = project_dir() / "data" / f"{date}_{info}"
    d = base
    if d.exists():
        d = base.with_name(f"{base.name}_{datetime.now().strftime('%H%M%S')}")
    d.mkdir(parents=True, exist_ok=True)
    return d

# ── User Input ────────────────────────────────────────────────────────────────
def ask(prompt, required=False, cast=None, default=None):
    suffix = f" [{default}]" if default is not None else (" (required)" if required else " (optional, press Enter to skip)")
    while True:
        val = input(f"  {prompt}{suffix}: ").strip()
        if not val:
            if required:
                print("  This field is required.")
                continue
            return default
        try:
            return cast(val) if cast else val
        except Exception:
            print(f"  Invalid input.")

def parse_args():
    p = argparse.ArgumentParser(
        description="Multi-platform apartment search → interactive HTML report.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # ── Sensible defaults — a bare run reproduces the usual search ──
    # Generic, non-personal defaults live here (safe to commit). Personal defaults
    # (your home base, commute, etc.) go in a gitignored config.local.json next to
    # this script and override these — see config.example.json.
    D = {
        "location":      "Washington",
        "state":         "DC",
        "postal":        None,
        "min_price":     0,
        "max_price":     3000,
        "bedrooms":      None,
        "radius":        10,
        "metro_station": None,
        "work_address":  None,
        "yelp_key":      None,
    }
    cfg_path = Path(__file__).with_name("config.local.json")
    if cfg_path.exists():
        try:
            loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            D.update({k: v for k, v in loaded.items() if k in D})
        except Exception as e:
            print(f"  (ignoring malformed config.local.json: {e})")
    p.add_argument("--location", default=D["location"], help="City / neighborhood")
    p.add_argument("--state", default=D["state"], help="State abbreviation, e.g. VA")
    p.add_argument("--postal", default=D["postal"], help="ZIP code to center the search on")
    p.add_argument("--min-price", type=int, default=D["min_price"])
    p.add_argument("--max-price", type=int, default=D["max_price"])
    p.add_argument("--bedrooms", type=int, default=D["bedrooms"],
                   help="0=studio; pass -1 for any")
    p.add_argument("--radius", type=int, default=D["radius"], help="Search radius in miles")
    p.add_argument("--metro-station", default=D["metro_station"])
    p.add_argument("--work-address", default=D["work_address"])
    p.add_argument("--gmaps-key", default=None)
    p.add_argument("--yelp-key", default=D["yelp_key"],
                   help="Yelp Fusion API key (free at yelp.com/developers). Required for Yelp "
                        "ratings — Yelp blocks scraping. Can also live in config.local.json.")
    # Google + Yelp review lookups always run — not a flag (intentionally always on).
    p.add_argument("--skip-link-check", action="store_true",
                   help="Skip validating that each listing URL actually resolves")
    p.add_argument("--browser", choices=["auto", "chrome", "edge", "firefox"], default="auto",
                   help="Which browser to drive")
    p.add_argument("--attach", action="store_true",
                   help="Attach to an already-running Chrome (launched with "
                        "--remote-debugging-port) instead of starting a fresh browser. "
                        "Use this to get past Zillow's human check: open Chrome yourself, "
                        "clear the check on zillow.com, then run with --attach.")
    p.add_argument("--attach-port", type=int, default=None,
                   help="Port to attach to with --attach (default 9222 for Chrome, "
                        "2828 for Firefox's Marionette)")
    p.add_argument("--zillow-json", nargs="?", const="auto", default=None,
                   help="Path to a zillow_listings.json produced by the bookmarklet "
                        "(see zillow-bookmarklet.html). Pass with no value (or 'auto') to "
                        "auto-pick the newest zillow_listings*.json in Downloads. This is "
                        "the primary manual fallback when the auto Edge path is challenged.")
    p.add_argument("--apartments-json", nargs="?", const="auto", default=None,
                   help="Path to an apartments_listings.json produced by the bookmarklet "
                        "(see apartments-bookmarklet.html). Pass with no value (or 'auto') to "
                        "auto-pick the newest apartments_listings*.json in Downloads. "
                        "Apartments.com 403s automated scrapers, so this is the way to include it.")
    p.add_argument("--sites-file", default=None,
                   help="Path to a JSON file the skill compiles of ~10 popular local apartment "
                        "complexes ([{name,url,fallback}, ...]). Each is scraped best-effort: "
                        "the complex's own site (A), falling back to its structured listing (B).")
    p.add_argument("--zillow-html", default=None,
                   help="Path to a Zillow rentals page you saved from your own browser "
                        "(Ctrl+S → 'Web Page, HTML only'). Listings are read from it with "
                        "no automation/attach — a deeper fallback if the bookmarklet won't run.")
    p.add_argument("--headless", action="store_true",
                   help="Run the browser headless (no visible window)")
    p.add_argument("--no-open", action="store_true",
                   help="Do not auto-open the report in a browser")
    p.add_argument("--max-listings", type=int, default=40)
    return p.parse_args()

def get_criteria(args):
    """Build criteria from CLI args, prompting interactively only for what's missing."""
    print("\n" + "═" * 60)
    print("  APARTMENT FINDER — Search Setup")
    print("═" * 60)
    # If core args are supplied, run fully non-interactively; otherwise prompt.
    interactive = not (args.location and args.state)
    c = {}

    if interactive:
        c["location"]      = args.location or ask("City / neighborhood to search", required=True)
        c["state"]         = (args.state or ask("State (e.g. DC, VA, MD)", required=True)).upper()
        c["postal"]        = args.postal or ask("ZIP code (optional, improves accuracy)")
        c["min_price"]     = ask("Minimum monthly rent ($)", cast=int, default=args.min_price)
        c["max_price"]     = ask("Maximum monthly rent ($)", cast=int, default=args.max_price)
        c["bedrooms"]      = ask("Number of bedrooms (0=studio, blank=any)", cast=int, default=args.bedrooms)
        c["metro_station"] = args.metro_station or ask("Nearest Metro / subway station (optional)")
        c["work_address"]  = args.work_address or ask("Work address for commute (optional)")
        c["gmaps_key"]     = args.gmaps_key or ask("Google Maps API key (optional)")
    else:
        c["location"]      = args.location
        c["state"]         = args.state.upper()
        c["postal"]        = args.postal
        c["min_price"]     = args.min_price
        c["max_price"]     = args.max_price
        c["bedrooms"]      = None if args.bedrooms is not None and args.bedrooms < 0 else args.bedrooms
        c["metro_station"] = args.metro_station
        c["work_address"]  = args.work_address
        c["gmaps_key"]     = args.gmaps_key

    c["radius"]        = args.radius
    c["yelp_key"]      = args.yelp_key
    c["size_flexible"] = True
    c["reviews"]       = True   # Google + Yelp reviews always run (not configurable)
    c["check_links"]   = not args.skip_link_check
    c["headless"]      = args.headless
    c["browser"]       = args.browser
    c["attach"]        = args.attach
    c["attach_port"]   = args.attach_port
    c["zillow_json"]   = args.zillow_json
    c["apartments_json"] = args.apartments_json
    c["sites"]         = _load_sites(args.sites_file)
    c["zillow_html"]   = args.zillow_html
    c["max_listings"]  = args.max_listings
    print()
    return c

# ── Geocoding via Nominatim ───────────────────────────────────────────────────
def geocode(address):
    """Return (lat, lon) or None."""
    try:
        url = "https://nominatim.openstreetmap.org/search"
        r = SESSION.get(url, params={"q": address, "format": "json", "limit": 1},
                        headers={"User-Agent": "ApartmentFinder/1.0"}, timeout=8)
        data = r.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None

def haversine(lat1, lon1, lat2, lon2):
    """Straight-line distance in miles."""
    R = 3958.8
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a = math.sin(dφ/2)**2 + math.cos(φ1)*math.cos(φ2)*math.sin(dλ/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def driving_distance(origin_coords, dest_coords):
    """Driving distance via OSRM (free, no key)."""
    try:
        lon1, lat1 = origin_coords[1], origin_coords[0]
        lon2, lat2 = dest_coords[1], dest_coords[0]
        url = f"https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
        r = SESSION.get(url, params={"overview": "false"}, timeout=10)
        d = r.json()
        if d.get("routes"):
            dist_miles = d["routes"][0]["distance"] / 1609.34
            dur_mins   = round(d["routes"][0]["duration"] / 60)
            return round(dist_miles, 1), dur_mins
    except Exception:
        pass
    return None, None

def transit_time_gmaps(origin, destination, api_key):
    """Transit time via Google Maps Distance Matrix API."""
    try:
        url = "https://maps.googleapis.com/maps/api/distancematrix/json"
        params = {
            "origins": origin, "destinations": destination,
            "mode": "transit", "key": api_key
        }
        r = SESSION.get(url, params=params, timeout=10)
        data = r.json()
        el = data["rows"][0]["elements"][0]
        if el["status"] == "OK":
            return el["distance"]["text"], el["duration"]["text"]
    except Exception:
        pass
    return None, None

# ── Metro station lookup ──────────────────────────────────────────────────────
def find_metro_coords(station_name, city):
    if not station_name:
        return None
    # Try progressively simpler queries until we get a hit
    for q in [
        f"{station_name} station {city}",
        f"{station_name} {city}",
        f"{station_name}",
    ]:
        coords = geocode(q)
        if coords:
            return coords
        time.sleep(0.5)
    return None

def metro_walk_info(apt_coords, metro_coords):
    """Return (walk_miles, walk_mins_est)."""
    if not apt_coords or not metro_coords:
        return None, None
    d = haversine(*apt_coords, *metro_coords)
    mins = round(d / 0.05)  # ~3 mph walking → 0.05 miles/min
    return round(d, 2), mins

# ── Craigslist Scraper ────────────────────────────────────────────────────────
CL_CITY_MAP = {
    "washington": "washingtondc", "dc": "washingtondc",
    "new york": "newyork", "nyc": "newyork",
    "los angeles": "losangeles", "la": "losangeles",
    "san francisco": "sfbay", "sf": "sfbay",
    "chicago": "chicago", "boston": "boston",
    "seattle": "seattle", "austin": "austin",
    "denver": "denver", "miami": "miami",
    "atlanta": "atlanta", "dallas": "dallas",
    "philadelphia": "philadelphia", "philly": "philadelphia",
}

# Map a metro area to its Craigslist subdomain. Many cities share one regional
# site (e.g. the whole DC/Northern-VA/suburban-MD area lives on washingtondc).
CL_REGION_BY_CITY = dict(CL_CITY_MAP)
CL_REGION_BY_CITY.update({
    "falls church": "washingtondc", "arlington": "washingtondc",
    "alexandria": "washingtondc", "bethesda": "washingtondc",
    "silver spring": "washingtondc", "fairfax": "washingtondc",
    "vienna": "washingtondc", "mclean": "washingtondc",
    "reston": "washingtondc", "tysons": "washingtondc",
    "rockville": "washingtondc", "brooklyn": "newyork",
    "queens": "newyork", "manhattan": "newyork", "oakland": "sfbay",
    "berkeley": "sfbay", "san jose": "sfbay", "cambridge": "boston",
})

# Fallback: pick a regional Craigslist site from the state. Not every state maps
# cleanly (several have multiple sites) but this covers the common metros.
CL_REGION_BY_STATE = {
    "DC": "washingtondc", "VA": "washingtondc", "MD": "washingtondc",
    "NY": "newyork", "CA": "losangeles", "IL": "chicago", "MA": "boston",
    "WA": "seattle", "TX": "austin", "CO": "denver", "FL": "miami",
    "GA": "atlanta", "PA": "philadelphia", "OR": "portland", "AZ": "phoenix",
}

def craigslist_city_code(location, state=None):
    key = location.lower().strip()
    for k, v in CL_REGION_BY_CITY.items():
        if k in key:
            return v
    if state and state.upper() in CL_REGION_BY_STATE:
        return CL_REGION_BY_STATE[state.upper()]
    words = re.sub(r"[^a-z]", "", key)
    return words[:16] if words else "washingtondc"

def _blank_listing(**kw):
    base = {
        "source": "", "name": "", "price": 0, "price_note": "Contact for price",
        "beds": None, "sqft": None, "address": "", "coords": None, "url": "",
        "image": "", "description": "", "rating": None, "reviews": [],
        "metro_walk_miles": None, "metro_walk_mins": None,
        "metro_drive_miles": None, "metro_drive_mins": None,
        "drive_miles": None, "drive_mins": None,
        "transit_distance": None, "transit_time": None,
        "parking_est": None, "pros": [], "cons": [],
        "pets_dogs": None, "pets_cats": None,
        "parking_free": None, "parking_price": None,
        "amenities": [],
    }
    base.update(kw)
    return base

def scrape_craigslist(criteria, driver, max_results=40):
    """
    Craigslist renders its result gallery with JS, so we load it in a real
    browser and parse the rendered cards (real prices, URLs, beds, sqft).
    Coordinates come from the embedded JSON-LD, matched to cards by title.
    """
    city = craigslist_city_code(criteria["location"], criteria.get("state"))
    params = {
        "search_distance": str(criteria.get("radius", 10)),
        "availabilityMode": "0",
    }
    if criteria.get("postal"):
        params["postal"] = criteria["postal"]
    if criteria["min_price"]:
        params["min_price"] = criteria["min_price"]
    if criteria["max_price"]:
        params["max_price"] = criteria["max_price"]
    if criteria.get("bedrooms") is not None:
        params["min_bedrooms"] = criteria["bedrooms"]
        params["max_bedrooms"] = criteria["bedrooms"]

    base_url = f"https://{city}.craigslist.org/search/apa"
    url = base_url + "?" + urllib.parse.urlencode(params) + "#search=2~gallery~0"
    print(f"  Searching Craigslist ({city})...")
    results = []
    if driver is None:
        print("  Craigslist skipped (no browser available).")
        return results
    try:
        # A maximized (non-occluded) window keeps Chrome from throttling the page's
        # image loading — important when the run is in the background.
        try:
            driver.maximize_window()
        except Exception:
            pass
        driver.get(url)
        # Wait for the JS-rendered result cards to appear
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".cl-search-result"))
            )
        except Exception:
            pass
        time.sleep(2.5)
        # Craigslist lazy-loads gallery thumbnails only as a card enters the viewport,
        # and a backgrounded window won't eager-load them — so parsing immediately leaves
        # most cards with a data-URI placeholder instead of a real photo. Bring each card
        # into view with scrollIntoView (which fires the intersection observer reliably
        # even when occluded), then let the images settle before parsing.
        try:
            card_els = driver.find_elements(By.CSS_SELECTOR, ".cl-search-result")[:max_results]
            for el in card_els:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                time.sleep(0.12)
            time.sleep(2)
            driver.execute_script("window.scrollTo(0, 0);")
        except Exception:
            pass
        soup = BeautifulSoup(driver.page_source, "html.parser")

        # Build a title→coords map from JSON-LD
        coord_map = {}
        ld_tag = soup.find("script", id="ld_searchpage_results")
        if ld_tag:
            try:
                ld = json.loads(ld_tag.string)
                for entry in ld.get("itemListElement", []):
                    item = entry.get("item", {})
                    nm = (item.get("name") or "").strip()
                    lat, lon = item.get("latitude"), item.get("longitude")
                    if nm and lat and lon:
                        coord_map[nm] = (lat, lon)
            except Exception:
                pass

        cards = soup.select(".cl-search-result")[:max_results]
        for card in cards:
            try:
                title_el = card.select_one("a.posting-title") or card.select_one("a.cl-app-anchor")
                name = ((title_el.get_text(strip=True) if title_el else "") or
                        card.get("title") or "Craigslist Listing")
                # Replace mangled separators (U+FFFD) that Craigslist titles sometimes carry
                name = name.replace("�", "·").strip(" ·")
                link = title_el.get("href", "") if title_el else ""

                price_el = card.select_one(".priceinfo, .price")
                price_txt = price_el.get_text(strip=True) if price_el else ""
                price = int(re.sub(r"[^\d]", "", price_txt)) if re.search(r"\d", price_txt) else 0

                beds_el = card.select_one(".post-bedrooms")
                beds_m = re.search(r"(\d+)", beds_el.get_text()) if beds_el else None
                beds = int(beds_m.group(1)) if beds_m else None

                sqft_el = card.select_one(".post-sqft")
                sqft_m = re.search(r"(\d+)", sqft_el.get_text()) if sqft_el else None
                sqft = int(sqft_m.group(1)) if sqft_m else None

                # Locality: the meta line ends with the hood/city in parens or trailing text
                hood_el = card.select_one(".meta")
                locality = ""
                if hood_el:
                    txt = hood_el.get_text(" ", strip=True)
                    m = re.search(r"\b([A-Z][A-Za-z .'-]+(?:,\s*[A-Z]{2})?)\s*$", txt)
                    locality = m.group(1).strip() if m else ""
                address = locality or f"{criteria['location']}, {criteria['state']}"

                # Take a real loaded thumbnail; ignore data-URI lazy-load placeholders
                # (a missing image is better than a broken placeholder in the report).
                img = ""
                for ie in card.select("img"):
                    src = ie.get("src", "")
                    if "images.craigslist.org" in src:
                        img = src
                        break
                    if src and not src.startswith("data:") and not img:
                        img = src

                # Filter out listings outside the requested price band (CL sometimes leaks a few)
                if price and criteria.get("max_price") and price > criteria["max_price"]:
                    continue
                if price and criteria.get("min_price") and price < criteria["min_price"]:
                    continue

                results.append(_blank_listing(
                    source="Craigslist",
                    name=name,
                    price=price,
                    price_note="Contact for price" if not price else "",
                    beds=beds,
                    sqft=sqft,
                    address=address,
                    coords=coord_map.get(name.strip()),
                    url=link,
                    image=img,
                    description=f"{beds} bed · {sqft} sqft" if beds and sqft else (locality or ""),
                ))
            except Exception:
                continue
    except Exception as e:
        print(f"  Craigslist error: {e}")
    print(f"  Found {len(results)} Craigslist listings.")
    return results

# ── Human-in-the-loop wait for bot-walled sites ───────────────────────────────
def await_listings(driver, selector, label, headless, timeout=240):
    """
    Poll until `selector` appears. If the page is showing a PerimeterX/Cloudflare
    'Press & Hold' or 'denied' challenge, pause and ask the user to solve it in the
    visible browser window, then keep waiting. Returns True once listings load.
    """
    block_markers = ("press & hold", "px-captcha", "access to this page has been denied",
                     "verify you are a human", "needs to review the security")
    # Headless can't solve a human check, so never wait long there.
    if headless:
        timeout = 15
    deadline = time.time() + timeout
    prompted = False
    while time.time() < deadline:
        try:
            if driver.find_elements(By.CSS_SELECTOR, selector):
                if prompted:
                    print(f"  {label}: challenge cleared — continuing.        ")
                return True
        except Exception:
            pass
        src = (driver.page_source or "").lower()
        blocked = (any(m in src for m in block_markers)
                   or "denied" in (driver.title or "").lower())
        if blocked:
            if headless:
                print(f"  {label}: bot-check present and browser is headless — cannot solve. Skipping.")
                print(f"        Re-run WITHOUT --headless so you can complete the check.")
                return False
            if not prompted:
                prompted = True
                print(f"\n  ┌─ ACTION NEEDED ─────────────────────────────────────────┐")
                print(f"  │ {label} is showing a 'Press & Hold' / human check.")
                print(f"  │ Complete it in the browser window that just opened.")
                print(f"  │ Scraping resumes automatically once listings load.")
                print(f"  │ (waiting up to {timeout}s)")
                print(f"  └─────────────────────────────────────────────────────────┘")
            remaining = int(deadline - time.time())
            print(f"  {label}: waiting for you to clear the check… {remaining}s left   ", end="\r")
        time.sleep(2)
    print(f"\n  {label}: timed out waiting for listings.                     ")
    return False

# ── Zillow Scraper (undetected-chromedriver, no watch window) ─────────────────
def _find_chrome_binary():
    """Locate a Chrome binary: a real install, else the Chrome-for-Testing that
    Selenium Manager already downloaded for this project. Returns (path, version_main)."""
    import glob
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c, None
    cache = os.path.join(os.path.expanduser("~"), ".cache", "selenium", "chrome", "**", "chrome.exe")
    found = sorted(glob.glob(cache, recursive=True))
    if found:
        path = found[-1]                       # newest version dir
        m = re.search(r"[\\/](\d+)\.\d+\.\d+", path)
        return path, (int(m.group(1)) if m else None)
    return None, None

def _zillow_price(text):
    if not text:
        return 0
    m = re.search(r"[\d,]+", text)
    return int(m.group(0).replace(",", "")) if m else 0

def _zillow_extract_listings(raw):
    """Pull the listResults array out of a __NEXT_DATA__ JSON string."""
    try:
        data = json.loads(raw)
    except Exception:
        return None
    bucket = {}
    def _walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("listResults"), list):
                bucket.setdefault("lr", o["listResults"])
            for v in o.values():
                _walk(v)
        elif isinstance(o, list):
            for v in o:
                _walk(v)
    _walk(data)
    return bucket.get("lr")

async def _zillow_fetch(url):
    """Load a Zillow page in a real browser and return its __NEXT_DATA__ text.

    Key detail: we drive the *installed consumer browser* (Edge, then Chrome) via
    Playwright's `channel=`, not a bundled/testing Chromium. Zillow's PerimeterX
    fingerprints the automation-flavored binaries (plain Selenium, Chrome-for-Testing,
    undetected-chromedriver) and challenges them, but a genuine Edge/Chrome build on a
    residential IP passes — so no 'Press & Hold' ever appears.
    """
    from playwright.async_api import async_playwright
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    hdrs = {"Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    async with async_playwright() as p:
        browser = None
        for channel in ("msedge", "chrome", None):     # prefer real consumer browsers
            try:
                kw = {"headless": False, "args": args}
                if channel:
                    kw["channel"] = channel
                browser = await p.chromium.launch(**kw)
                break
            except Exception:
                continue
        if browser is None:
            return None
        try:
            ctx = await browser.new_context(user_agent=ua, viewport={"width": 1366, "height": 768},
                                            locale="en-US", extra_http_headers=hdrs)
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(2.5, 4))
            await page.mouse.wheel(0, random.randint(400, 900))
            await asyncio.sleep(random.uniform(1.5, 2.5))
            return await page.evaluate(
                "()=>{const e=document.getElementById('__NEXT_DATA__');return e?e.textContent:null;}")
        finally:
            await browser.close()

def _zillow_url(criteria):
    """Build the clean slug rentals URL (e.g. .../falls-church-va/rentals/).
    This form is far less likely to be challenged than /homes/for_rent/<encoded>/."""
    slug = f"{criteria['location']} {criteria['state']}".strip().lower()
    slug = re.sub(r"[,\s]+", "-", slug)
    slug = re.sub(r"[^a-z0-9\-]", "", slug).strip("-")
    return f"https://www.zillow.com/{slug}/rentals/"

def _zillow_photos(it, limit=15):
    """Expand a listing's carouselPhotosComposable into full photo URLs.

    Zillow ships one cover photo as imgSrc but the whole gallery as photoKeys under
    carouselPhotosComposable.photoData, each formed by substituting {photoKey} into baseUrl
    (e.g. https://photos.zillowstatic.com/fp/{photoKey}-p_e.jpg). Falls back to [imgSrc]."""
    cp = it.get("carouselPhotosComposable") or {}
    base = cp.get("baseUrl")
    keys = [p.get("photoKey") for p in (cp.get("photoData") or []) if p.get("photoKey")]
    urls = []
    if base and "{photoKey}" in base:
        urls = [base.replace("{photoKey}", k) for k in keys[:limit]]
    img = it.get("imgSrc", "")
    if not urls:
        return [img] if img else []
    if img and img not in urls:                # keep the chosen cover photo first
        urls = [img] + [u for u in urls if u != img]
    return urls[:limit]

def _zillow_build_results(items, criteria, max_results):
    """Turn Zillow listResults into our listing dicts, expanding building unit
    ranges into one entry per bedroom type that matches the price/beds filters."""
    results = []
    want_beds = criteria.get("bedrooms")
    want_beds = int(want_beds) if want_beds is not None else -1
    max_price = int(criteria["max_price"]) if criteria.get("max_price") else None
    min_price = int(criteria.get("min_price") or 0)
    for it in items:
            try:
                addr = it.get("address") or it.get("buildingName") or ""
                link = it.get("detailUrl") or ""
                if link and not link.startswith("http"):
                    link = "https://www.zillow.com" + link
                img = it.get("imgSrc", "")
                photos = _zillow_photos(it)
                name = it.get("buildingName") or it.get("statusText") or addr
                # Build (beds, price) candidates from the unit list or top-level price.
                cands = []
                for u in (it.get("units") or []):
                    cands.append((u.get("beds"), _zillow_price(u.get("price"))))
                if not cands:
                    cands.append((it.get("beds"), _zillow_price(it.get("price")) or
                                  int(it.get("minBaseRent") or 0)))
                seen_beds = set()
                for beds_raw, price in sorted(cands, key=lambda c: c[1] or 0):
                    try:
                        beds = int(beds_raw) if beds_raw not in (None, "") else None
                    except (ValueError, TypeError):
                        beds = None
                    if not price:
                        continue
                    if max_price and price > max_price:
                        continue
                    if min_price and price < min_price:
                        continue
                    if want_beds >= 0 and beds is not None and beds != want_beds:
                        continue
                    if beds in seen_beds:           # one entry per bedroom type per building
                        continue
                    seen_beds.add(beds)
                    bed_label = "Studio" if beds == 0 else (f"{beds}-bd" if beds else "")
                    results.append({
                        "source": "Zillow",
                        "name": f"{name} · {bed_label}".strip(" ·") if bed_label else name,
                        "price": price,
                        "beds": beds,
                        "sqft": None,
                        "address": addr,
                        "url": link,
                        "image": img,
                        "images": photos,
                        "description": it.get("statusText", ""),
                        "rating": None,
                        "reviews": [],
                        "metro_walk_miles": None,
                        "metro_walk_mins": None,
                        "drive_miles": None,
                        "drive_mins": None,
                        "transit_distance": None,
                        "transit_time": None,
                        "parking_est": None,
                        "pros": [],
                        "cons": [],
                    })
                    if len(results) >= max_results:
                        break
                if len(results) >= max_results:
                    break
            except Exception:
                continue
    return results

def scrape_zillow_api(criteria, max_results=20):
    """Scrape Zillow rentals via Playwright + the installed consumer browser (Edge/Chrome).
    No human interaction — reads listings from the page's embedded __NEXT_DATA__ JSON."""
    print(f"  Searching Zillow...")
    results = []
    try:
        url = _zillow_url(criteria)
        items = None
        for attempt in range(1, 3):
            raw = asyncio.run(_zillow_fetch(url))
            items = _zillow_extract_listings(raw) if raw else None
            if items:
                break
            if attempt < 2:
                print(f"  Zillow: no data (attempt {attempt}/2), retrying…")
                time.sleep(random.uniform(3, 5))
        if not items:
            print(f"  Zillow: listings data not found; 0 listings.")
            return results
        results = _zillow_build_results(items, criteria, max_results)
    except Exception as e:
        print(f"  Zillow error: {str(e).splitlines()[0]}")
    print(f"  Found {len(results)} Zillow listings.")
    return results

def _newest_zillow_dump():
    """Return the path to the most recent zillow_listings*.json in Downloads, or None."""
    import glob
    dl = os.path.join(os.path.expanduser("~"), "Downloads")
    cands = glob.glob(os.path.join(dl, "zillow_listings*.json"))
    return max(cands, key=os.path.getmtime) if cands else None

def scrape_zillow_from_json(criteria, path, max_results=20):
    """Parse listings from a bookmarklet JSON dump (see zillow-bookmarklet.html).

    The bookmarklet runs in the user's own, already-cleared browser and downloads the bare
    `listResults` array — no automation touches the page, so PerimeterX never challenges it.
    We feed the array straight to _zillow_build_results (no __NEXT_DATA__ unwrapping needed)."""
    print("  Searching Zillow (from bookmarklet dump)...")
    results = []
    try:
        if path in (None, "auto"):
            path = _newest_zillow_dump()
        if not path or not os.path.isfile(path):
            print("  Zillow: no bookmarklet dump found in Downloads; 0 listings.")
            return results
        age_min = (time.time() - os.path.getmtime(path)) / 60
        if age_min > 30:
            print(f"  Zillow: warning - dump is {age_min:.0f} min old "
                  f"({os.path.basename(path)}); may be a stale/other search.")
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            items = json.load(fh)
        if not isinstance(items, list) or not items:
            print("  Zillow: bookmarklet dump is empty; 0 listings.")
            return results
        results = _zillow_build_results(items, criteria, max_results)
    except Exception as e:
        print(f"  Zillow error: {str(e).splitlines()[0]}")
    print(f"  Found {len(results)} Zillow listings.")
    return results

def scrape_zillow_from_file(criteria, path, max_results=20):
    """Parse Zillow listings from a page the user saved from their OWN browser.

    The whole Marionette/attach dance is avoided: the user opens the Zillow
    rentals page in their normal Firefox (which isn't challenged), hits Ctrl+S,
    and we read the embedded __NEXT_DATA__ JSON out of the saved .html file.
    Zero automation touches their browser."""
    print(f"  Searching Zillow (from saved page)...")
    results = []
    try:
        if not os.path.isfile(path):
            print(f"  Zillow: saved file not found at {path}; 0 listings.")
            return results
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            html = fh.read()
        m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        items = _zillow_extract_listings(m.group(1)) if m else None
        if not items:
            print(f"  Zillow: no listings found in saved page "
                  f"(save the rentals results page as 'Web Page, HTML only'); 0 listings.")
            return results
        results = _zillow_build_results(items, criteria, max_results)
    except Exception as e:
        print(f"  Zillow error: {str(e).splitlines()[0]}")
    print(f"  Found {len(results)} Zillow listings.")
    return results

def scrape_zillow_via_driver(criteria, driver, max_results=20):
    """Scrape Zillow through an already-attached Selenium browser — e.g. a real
    Firefox the user launched with -marionette and cleared. We reuse their live,
    un-challenged session, navigate to the rentals page, and read __NEXT_DATA__
    from the rendered source. No Press & Hold, because it's their real browser."""
    print(f"  Searching Zillow (via your attached browser)...")
    results = []
    try:
        url = _zillow_url(criteria)
        driver.get(url)
        time.sleep(random.uniform(4, 6))
        for _ in range(3):
            try:
                driver.execute_script("window.scrollBy(0, document.body.scrollHeight/3);")
            except Exception:
                break
            time.sleep(1)
        m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                      driver.page_source, re.S)
        items = _zillow_extract_listings(m.group(1)) if m else None
        if not items:
            print(f"  Zillow: no listings in the attached session "
                  f"(is the rentals page loaded & cleared?); 0 listings.")
            return results
        results = _zillow_build_results(items, criteria, max_results)
    except Exception as e:
        print(f"  Zillow error: {str(e).splitlines()[0]}")
    print(f"  Found {len(results)} Zillow listings.")
    return results

# ── Zillow Scraper (Selenium) ─────────────────────────────────────────────────
def scrape_zillow(criteria, driver, max_results=20):
    location = urllib.parse.quote_plus(f"{criteria['location']}, {criteria['state']}")
    url = f"https://www.zillow.com/homes/for_rent/{location}/"
    print(f"  Searching Zillow...")
    results = []
    try:
        driver.get(url)
        time.sleep(random.uniform(2, 3))
        if not await_listings(driver, "article[data-test='property-card']",
                               "Zillow", criteria.get("headless", False)):
            print(f"  Found 0 Zillow listings.")
            return results
        # Scroll to trigger lazy-loaded cards
        for _ in range(4):
            driver.execute_script("window.scrollBy(0, document.body.scrollHeight/3);")
            time.sleep(1)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        cards = soup.select("article[data-test='property-card']")[:max_results]
        for card in cards:
            try:
                title_el = card.select_one("a[data-test='property-card-link']")
                price_el = card.select_one("span[data-test='property-card-price']")
                detail_el = card.select_one("ul[data-test='property-card-details']")
                addr_el  = card.select_one("address")
                img_el   = card.select_one("img")
                title = title_el.get_text(strip=True) if title_el else "Zillow Listing"
                price_text = price_el.get_text(strip=True) if price_el else ""
                price = int(re.sub(r"[^\d]", "", price_text.split("/")[0])) if price_text else 0
                details = detail_el.get_text(" ", strip=True) if detail_el else ""
                address = addr_el.get_text(strip=True) if addr_el else ""
                link = title_el.get("href", "") if title_el else ""
                if link and not link.startswith("http"):
                    link = "https://www.zillow.com" + link
                img = img_el.get("src", "") if img_el else ""
                beds_m = re.search(r"(\d+)\s*bd", details)
                sqft_m = re.search(r"([\d,]+)\s*sqft", details)
                beds = int(beds_m.group(1)) if beds_m else None
                sqft = int(re.sub(r",", "", sqft_m.group(1))) if sqft_m else None
                results.append({
                    "source": "Zillow",
                    "name": title if title != "Zillow Listing" else address,
                    "price": price,
                    "beds": beds,
                    "sqft": sqft,
                    "address": address,
                    "url": link,
                    "image": img,
                    "description": details,
                    "rating": None,
                    "reviews": [],
                    "metro_walk_miles": None,
                    "metro_walk_mins": None,
                    "drive_miles": None,
                    "drive_mins": None,
                    "transit_distance": None,
                    "transit_time": None,
                    "parking_est": None,
                    "pros": [],
                    "cons": [],
                })
            except Exception:
                continue
        time.sleep(random.uniform(1, 2))
    except Exception as e:
        print(f"  Zillow error: {e}")
    print(f"  Found {len(results)} Zillow listings.")
    return results

# ── Apartments.com Scraper (Selenium) ─────────────────────────────────────────
def _apartments_price(text):
    if not text:
        return 0
    m = re.search(r"[\d,]+", text)
    return int(m.group(0).replace(",", "")) if m else 0

def _apartments_beds(text):
    if not text:
        return None
    if re.search(r"studio", text, re.I):
        return 0
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None

def _apartments_build_results(items, criteria, max_results):
    """Turn bookmarklet placard objects into listing dicts, expanding each placard's
    per-bedroom rentRollup boxes into one entry per bed type that matches the filters
    (mirrors _zillow_build_results)."""
    results = []
    want_beds = criteria.get("bedrooms")
    want_beds = int(want_beds) if want_beds is not None else -1
    max_price = int(criteria["max_price"]) if criteria.get("max_price") else None
    min_price = int(criteria.get("min_price") or 0)
    for it in items:
        try:
            link = it.get("url") or ""
            if link and not link.startswith("http"):
                link = "https://www.apartments.com" + link
            title = it.get("title") or it.get("address") or "Apartments.com Listing"
            img = it.get("image", "")
            amenities = it.get("amenities") or []
            # (beds, price) candidates from the rentRollup boxes; fall back to a regex over
            # the placard's info text if the structured boxes are absent.
            cands = []
            for u in (it.get("units") or []):
                cands.append((_apartments_beds(u.get("bedText")),
                              _apartments_price(u.get("priceText"))))
            if not cands:
                blob = it.get("infoText", "")
                cands.append((_apartments_beds(blob), _apartments_price(blob)))
            seen_beds = set()
            for beds, price in sorted(cands, key=lambda c: c[1] or 0):
                if not price:
                    continue
                if max_price and price > max_price:
                    continue
                if min_price and price < min_price:
                    continue
                if want_beds >= 0 and beds is not None and beds != want_beds:
                    continue
                if beds in seen_beds:
                    continue
                seen_beds.add(beds)
                bed_label = "Studio" if beds == 0 else (f"{beds}-bd" if beds else "")
                results.append({
                    "source": "Apartments.com",
                    "name": f"{title} · {bed_label}".strip(" ·") if bed_label else title,
                    "price": price,
                    "beds": beds,
                    "sqft": None,
                    "address": it.get("address", ""),
                    "url": link,
                    "image": img,
                    "images": [img] if img else [],
                    "description": ", ".join(amenities),
                    "amenities": amenities,
                    "rating": None,
                    "reviews": [],
                    "metro_walk_miles": None,
                    "metro_walk_mins": None,
                    "drive_miles": None,
                    "drive_mins": None,
                    "transit_distance": None,
                    "transit_time": None,
                    "parking_est": None,
                    "pros": [],
                    "cons": [],
                })
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break
        except Exception:
            continue
    return results

def _newest_apartments_dump():
    """Return the most recent apartments_listings*.json in Downloads, or None."""
    import glob
    dl = os.path.join(os.path.expanduser("~"), "Downloads")
    cands = glob.glob(os.path.join(dl, "apartments_listings*.json"))
    return max(cands, key=os.path.getmtime) if cands else None

def scrape_apartments_from_json(criteria, path, max_results=20):
    """Parse listings from an Apartments.com bookmarklet dump (see apartments-bookmarklet.html).

    Apartments.com 403s automated scrapers, so the user clicks a bookmarklet on a results page
    in their own browser; it normalizes each placard (id/url/title/address/photo/rentRollup
    boxes/amenities) and downloads apartments_listings.json. No automation touches the page."""
    print("  Searching Apartments.com (from bookmarklet dump)...")
    results = []
    try:
        if path in (None, "auto"):
            path = _newest_apartments_dump()
        if not path or not os.path.isfile(path):
            print("  Apartments.com: no bookmarklet dump found in Downloads; 0 listings.")
            return results
        age_min = (time.time() - os.path.getmtime(path)) / 60
        if age_min > 30:
            print(f"  Apartments.com: warning - dump is {age_min:.0f} min old "
                  f"({os.path.basename(path)}); may be a stale/other search.")
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            items = json.load(fh)
        if not isinstance(items, list) or not items:
            print("  Apartments.com: bookmarklet dump is empty; 0 listings.")
            return results
        results = _apartments_build_results(items, criteria, max_results)
    except Exception as e:
        print(f"  Apartments.com error: {str(e).splitlines()[0]}")
    print(f"  Found {len(results)} Apartments.com listings.")
    return results

def scrape_apartments_com(criteria, driver, max_results=20):
    loc = re.sub(r"\s+", "-", f"{criteria['location']}-{criteria['state']}").lower()
    loc = re.sub(r"[^a-z0-9\-]", "", loc)
    url = f"https://www.apartments.com/{loc}/"
    if criteria["max_price"]:
        url += f"min-{criteria['min_price']}-max-{criteria['max_price']}/"
    print(f"  Searching Apartments.com...")
    results = []
    try:
        driver.get(url)
        time.sleep(random.uniform(2, 3))
        if not await_listings(driver, "article.placard, .placard",
                              "Apartments.com", criteria.get("headless", False), timeout=25):
            print(f"  Found 0 Apartments.com listings.")
            return results
        soup = BeautifulSoup(driver.page_source, "html.parser")
        cards = soup.select("article.placard")[:max_results]
        for card in cards:
            try:
                title_el = card.select_one(".property-title")
                price_el = card.select_one(".price-range, .rent-price")
                addr_el  = card.select_one(".property-address")
                link_el  = card.select_one("a.property-link")
                img_el   = card.select_one("img.lzy")
                beds_el  = card.select_one(".beds-range, .unitLabel")
                title   = title_el.get_text(strip=True) if title_el else "Apartments.com Listing"
                p_text  = price_el.get_text(strip=True) if price_el else ""
                price   = int(re.sub(r"[^\d]", "", p_text.split("–")[0])) if p_text else 0
                address = addr_el.get_text(strip=True) if addr_el else ""
                link    = link_el.get("href", "") if link_el else ""
                img     = img_el.get("data-src", img_el.get("src", "")) if img_el else ""
                beds_text = beds_el.get_text(strip=True) if beds_el else ""
                beds_m  = re.search(r"(\d+)", beds_text)
                beds    = int(beds_m.group(1)) if beds_m else None
                results.append({
                    "source": "Apartments.com",
                    "name": title,
                    "price": price,
                    "beds": beds,
                    "sqft": None,
                    "address": address,
                    "url": link,
                    "image": img,
                    "description": beds_text,
                    "rating": None,
                    "reviews": [],
                    "metro_walk_miles": None,
                    "metro_walk_mins": None,
                    "drive_miles": None,
                    "drive_mins": None,
                    "transit_distance": None,
                    "transit_time": None,
                    "parking_est": None,
                    "pros": [],
                    "cons": [],
                })
            except Exception:
                continue
        time.sleep(random.uniform(1, 2))
    except Exception as e:
        print(f"  Apartments.com error: {e}")
    print(f"  Found {len(results)} Apartments.com listings.")
    return results

# ── Local apartment sites (generic best-effort scraper) ───────────────────────
def _load_sites(path):
    """Read the sites list the skill compiled (one complex per item). Accepts a JSON list of
    {name,url,fallback} objects or bare URL strings, or a newline-delimited URL file."""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        try:
            data = [l.strip() for l in open(path, encoding="utf-8") if l.strip()]
        except Exception:
            return []
    out = []
    for x in (data if isinstance(data, list) else []):
        if isinstance(x, str):
            out.append({"name": None, "url": x, "fallback": None})
        elif isinstance(x, dict) and x.get("url"):
            out.append({"name": x.get("name"), "url": x.get("url"),
                        "fallback": x.get("fallback")})
    return out

def _jsonld_blocks(soup):
    """Yield every JSON-LD object on the page, flattening @graph and nested objects."""
    blocks = []
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except Exception:
            continue
        stack = [data]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                blocks.append(o)
                if isinstance(o.get("@graph"), list):
                    stack.extend(o["@graph"])
            elif isinstance(o, list):
                stack.extend(o)
    return blocks

def _extract_listing_from_html(html, url, source):
    """Best-effort single-listing extraction from an arbitrary apartment page:
    schema.org JSON-LD first, then OpenGraph, then a price/beds regex over the text."""
    soup = BeautifulSoup(html, "html.parser")
    name = address = image = desc = None
    price = 0
    beds = None
    for o in _jsonld_blocks(soup):
        name = name or o.get("name")
        a = o.get("address")
        if isinstance(a, dict):
            address = address or ", ".join(
                [p for p in (a.get("streetAddress"), a.get("addressLocality"),
                             a.get("addressRegion")) if p])
        elif isinstance(a, str):
            address = address or a
        im = o.get("image")
        if isinstance(im, list):
            im = im[0] if im else None
        if isinstance(im, dict):
            im = im.get("url")
        image = image or im
        off = o.get("offers")
        if isinstance(off, list):
            off = off[0] if off else None
        if isinstance(off, dict) and not price:
            price = _apartments_price(str(off.get("price") or off.get("lowPrice") or ""))
        beds = beds or o.get("numberOfBedrooms") or o.get("numberOfRooms")
        desc = desc or o.get("description")

    def og(prop):
        m = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return m.get("content").strip() if m and m.get("content") else None

    name = name or og("og:title") or (soup.title.get_text(strip=True) if soup.title else None)
    image = image or og("og:image")
    desc = desc or og("og:description")
    text = soup.get_text(" ", strip=True)
    if not price:
        # Prefer a price with an explicit monthly suffix (most precise)…
        m = re.search(r"\$\s?([\d,]{3,5})\s*(?:/\s*mo|/\s*month|per month|monthly|\+|\s*month)", text, re.I)
        if m:
            price = _apartments_price(m.group(1))
    if not price:
        # …else take the smallest plausible monthly rent on the page (operator floorplan cards
        # render "$1,789" with no suffix; $700–$20k filters out fees/deposits and phone digits).
        cands = [int(x.replace(",", "")) for x in re.findall(r"\$\s?([\d]{1,2},\d{3})\b", text)]
        cands = [v for v in cands if 700 <= v <= 20000]
        if cands:
            price = min(cands)
    if beds is not None:
        bm = re.search(r"\d+", str(beds))
        beds = int(bm.group()) if bm else None
    else:
        nums = [int(x) for x in re.findall(r"(\d+)\s*(?:bed|bedroom|br|bd)\b", text, re.I) if int(x) <= 6]
        if nums:
            beds = min(nums)
        elif re.search(r"\bstudio\b", text, re.I):
            beds = 0
    if not name:
        return None
    return _blank_listing(
        source=source, name=str(name).strip()[:120], price=price, beds=beds,
        address=address or "", url=url, image=image or "",
        images=[image] if image else [], description=(desc or "")[:500])

def _finalize_site(entry, listing, max_price):
    """Apply the complex's known name and the budget cap to an extracted listing."""
    if entry.get("name"):
        listing["name"] = entry["name"]
    if max_price and listing["price"] and listing["price"] > max_price:
        return None                                    # over budget — drop the lead
    return listing

async def _render_pages(urls, concurrency=3):
    """Render JS-heavy pages in real Edge (Playwright) and return {url: html}. Big operators
    (Equity, AvalonBay, Greystar/RentCafe, Bozzuto, etc.) load floorplan pricing via JS and ship
    nothing scrapeable in raw HTML; a real browser that also passes PerimeterX (the same channel
    trick as Zillow) renders the prices into the DOM so _extract_listing_from_html can read them."""
    from playwright.async_api import async_playwright
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    out = {}
    async with async_playwright() as p:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                kw = {"headless": False,
                      "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"]}
                if channel:
                    kw["channel"] = channel
                browser = await p.chromium.launch(**kw)
                break
            except Exception:
                continue
        if browser is None:
            return out
        try:
            ctx = await browser.new_context(user_agent=ua, locale="en-US",
                                            viewport={"width": 1366, "height": 900})
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            sem = asyncio.Semaphore(concurrency)

            async def one(u):
                async with sem:
                    page = await ctx.new_page()
                    try:
                        await page.goto(u, wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(random.uniform(3, 5))      # let JS hydrate pricing
                        await page.mouse.wheel(0, random.randint(600, 1200))
                        await asyncio.sleep(random.uniform(1.5, 2.5))
                        return u, await page.content()
                    except Exception:
                        return u, None
                    finally:
                        try:
                            await page.close()
                        except Exception:
                            pass

            for fut in asyncio.as_completed([one(u) for u in urls]):
                try:
                    u, h = await fut
                    if h:
                        out[u] = h
                except Exception:
                    pass
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    return out

def scrape_local_sites(criteria, driver, max_results=10):
    """Scrape the ~10 local complexes the skill found, each tried A (own site) then B (structured
    fallback). Two passes: a fast `requests` pass, then a real-Edge render pass for the JS-heavy
    sites (most big operators) that requests can't read. A usable result needs a price or beds —
    address-only hits are dropped as noise."""
    entries = criteria.get("sites") or []
    if not entries:
        return []
    print(f"  Searching local apartment sites ({len(entries)})...")
    mp = int(criteria["max_price"]) if criteria.get("max_price") else None
    results, pending = [], []

    def usable(l):
        return l and (l["price"] or l["beds"])

    # ── Pass 1: requests (catches sites that ship structured data in raw HTML) ──
    for e in entries[:max_results]:
        urls = [u for u in (e.get("url"), e.get("fallback")) if u]
        hit = None
        for u in urls:
            try:
                r = SESSION.get(u, timeout=12)
                if r.status_code < 400 and len(r.text) > 500:
                    l = _extract_listing_from_html(r.text, u, "Local site")
                    if usable(l):
                        hit = _finalize_site(e, l, mp); break
            except Exception:
                continue
        if hit:
            results.append(hit)
        elif urls:
            pending.append((e, urls))

    # ── Pass 2: render the misses in real Edge, then extract from the rendered DOM ──
    if pending:
        rendered = {}
        try:
            rendered = asyncio.run(_render_pages([u for _, urls in pending for u in urls]))
        except Exception:
            rendered = {}
        for e, urls in pending:
            for u in urls:
                h = rendered.get(u)
                if not h:
                    continue
                try:
                    l = _extract_listing_from_html(h, u, "Local site")
                except Exception:
                    l = None
                if usable(l):
                    fin = _finalize_site(e, l, mp)
                    if fin:
                        results.append(fin); break

    results = [r for r in results if r]
    print(f"  Found {len(results)} local-site listings.")
    return results

# ── HotPads Scraper ───────────────────────────────────────────────────────────
def scrape_hotpads(criteria, driver, max_results=15):
    loc = urllib.parse.quote_plus(f"{criteria['location']}, {criteria['state']}")
    url = f"https://hotpads.com/{criteria['location'].lower().replace(' ', '-')}-{criteria['state'].lower()}/apartments-for-rent"
    print(f"  Searching HotPads...")
    results = []
    try:
        driver.get(url)
        time.sleep(random.uniform(2, 3))
        if not await_listings(driver, "li[data-test='listing-card']",
                              "HotPads", criteria.get("headless", False), timeout=25):
            print(f"  Found 0 HotPads listings.")
            return results
        soup = BeautifulSoup(driver.page_source, "html.parser")
        cards = soup.select("li[data-test='listing-card']")[:max_results]
        for card in cards:
            try:
                title_el = card.select_one("[data-test='listing-card-address']")
                price_el = card.select_one("[data-test='listing-card-price']")
                link_el  = card.select_one("a")
                img_el   = card.select_one("img")
                title  = title_el.get_text(strip=True) if title_el else "HotPads Listing"
                p_text = price_el.get_text(strip=True) if price_el else ""
                price  = int(re.sub(r"[^\d]", "", p_text)) if p_text else 0
                link   = link_el.get("href", "") if link_el else ""
                if link and not link.startswith("http"):
                    link = "https://hotpads.com" + link
                img = img_el.get("src", "") if img_el else ""
                results.append({
                    "source": "HotPads",
                    "name": title,
                    "price": price,
                    "beds": None,
                    "sqft": None,
                    "address": title,
                    "url": link,
                    "image": img,
                    "description": "",
                    "rating": None,
                    "reviews": [],
                    "metro_walk_miles": None,
                    "metro_walk_mins": None,
                    "drive_miles": None,
                    "drive_mins": None,
                    "transit_distance": None,
                    "transit_time": None,
                    "parking_est": None,
                    "pros": [],
                    "cons": [],
                })
            except Exception:
                continue
    except Exception as e:
        print(f"  HotPads error: {e}")
    print(f"  Found {len(results)} HotPads listings.")
    return results

# ── Selenium Driver ───────────────────────────────────────────────────────────
def _apply_common_opts(opts, headless):
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1440,1000")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    # Keep rendering/loading active even when the window is occluded or in the
    # background — otherwise Chrome throttles paint and Craigslist's lazy-loaded
    # gallery thumbnails never fetch, leaving most listings imageless.
    opts.add_argument("--disable-backgrounding-occluded-windows")
    opts.add_argument("--disable-renderer-backgrounding")
    opts.add_argument("--disable-background-timer-throttling")
    # On Windows, Chrome detects a covered window as "occluded" and stops rendering
    # it, which halts Craigslist's intersection-observer thumbnail loading. Disabling
    # native occlusion calculation makes a background window behave like a visible one.
    opts.add_argument("--disable-features=CalculateNativeWinOcclusion")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(f"user-agent={HEADERS['User-Agent']}")

def _start_chrome(headless):
    opts = ChromeOptions()
    _apply_common_opts(opts, headless)
    driver = webdriver.Chrome(options=opts)
    driver.execute_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    return driver

def _start_edge(headless):
    opts = EdgeOptions()
    _apply_common_opts(opts, headless)
    driver = webdriver.Edge(options=opts)
    driver.execute_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    return driver

def _start_firefox(headless):
    from selenium.webdriver.firefox.options import Options as FxOptions
    opts = FxOptions()
    if headless:
        opts.add_argument("--headless")
    opts.set_preference("general.useragent.override", HEADERS["User-Agent"])
    opts.set_preference("dom.webdriver.enabled", False)
    opts.set_preference("useAutomationExtension", False)
    driver = webdriver.Firefox(options=opts)
    driver.set_window_size(1440, 1000)
    return driver

def _attach_chrome(port):
    """Attach to a Chrome the user already started with --remote-debugging-port=<port>.
    Because it's the user's own session, any Zillow 'Press & Hold' check they cleared
    stays cleared. We must NOT quit this browser when done — it's theirs."""
    opts = ChromeOptions()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    driver = webdriver.Chrome(options=opts)
    driver._apt_attached = True
    return driver

def _attach_firefox(marionette_port):
    """Attach to a Firefox the user started with `-marionette` (default port 2828) and a
    separate profile. geckodriver's --connect-existing reuses that live session, so a Zillow
    check they cleared carries over. We must NOT quit it — it's the user's browser."""
    from selenium.webdriver.firefox.service import Service as FxService
    service = FxService(service_args=["--connect-existing", "--marionette-port", str(marionette_port)])
    driver = webdriver.Firefox(service=service)
    driver._apt_attached = True
    return driver

def make_driver(headless=False, preference="auto", attach=False, attach_port=None):
    """
    Build a browser via Selenium Manager (auto-resolves the driver).
    preference: 'chrome', 'edge', 'firefox', or 'auto' (Chrome → Edge → Firefox).
    Firefox is often a useful alternative when a Chromium browser gets bot-flagged.
    With attach=True, connect to the user's own browser (Chrome via --remote-debugging-port,
    or Firefox via -marionette) so their cleared Zillow human-check carries over; falls back
    to launching a fresh browser. Defaults: port 9222 for Chrome, 2828 for Firefox.
    """
    if attach:
        if preference == "firefox":
            port = attach_port or 2828
            try:
                driver = _attach_firefox(port)
                print(f"  Browser: attached to your Firefox (Marionette port {port})")
                return driver
            except Exception as e:
                print(f"  Could not attach to Firefox on Marionette port {port}: {str(e).splitlines()[0]}")
                print(f"  Falling back to launching a fresh browser (Zillow may show a check).")
        else:
            port = attach_port or 9222
            try:
                driver = _attach_chrome(port)
                print(f"  Browser: attached to your Chrome on port {port}")
                return driver
            except Exception as e:
                print(f"  Could not attach to Chrome on port {port}: {str(e).splitlines()[0]}")
                print(f"  Falling back to launching a fresh browser (Zillow may show a check).")
    order = {
        "chrome":  [("Google Chrome", _start_chrome)],
        "edge":    [("Microsoft Edge", _start_edge)],
        "firefox": [("Mozilla Firefox", _start_firefox)],
        "auto":    [("Google Chrome", _start_chrome), ("Microsoft Edge", _start_edge),
                    ("Mozilla Firefox", _start_firefox)],
    }[preference]

    errors = []
    for label, starter in order:
        try:
            driver = starter(headless)
            print(f"  Browser: {label}")
            return driver
        except Exception as e:
            errors.append(f"{label}: {str(e).splitlines()[0]}")
    print("  Could not start a browser — " + " | ".join(errors))
    return None

# ── Deduplication ─────────────────────────────────────────────────────────────
def deduplicate(apts):
    seen, out = set(), []
    for a in apts:
        key = re.sub(r"[^a-z0-9]", "", (a["name"] + a["address"]).lower())[:40]
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out

# ── Title normalization & listing-type detection ─────────────────────────────
# Markers that strongly imply the unit is part of someone's home (a basement,
# in-law suite, or a single room) rather than a standalone/complex apartment.
ATTACHED_HOME_MARKERS = (
    "english basement", "basement apartment", "basement apt", "basement for rent",
    "basement", "walkout", "walk-out", "walk out entrance",
    "in-law", "in law suite", "mother-in-law", "mother in law", "au pair",
    "room for rent", "rooms for rent", "private room", "furnished room",
    "room available", "room in a", "room in the", "single room", "bedroom for rent",
    "roommate", "shared house", "share house", "share a house", "shared home",
    "in a private home", "in private home", "in a single family", "in single family",
    "in my home", "in my house", "in our home", "in our house",
    "in a home", "in a house", "in a townhouse", "in a condo i live",
    "live-in", "owner occupied", "owner-occupied",
)

def detect_attached_home(*texts):
    """Return a short reason string if the listing looks attached to a home, else ''."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return ""
    for marker in ATTACHED_HOME_MARKERS:
        if marker in blob:
            if "basement" in marker or "walkout" in marker or "walk-out" in marker or "walk out" in marker:
                return "Basement unit attached to a home"
            if "in-law" in marker or "in law" in marker or "au pair" in marker:
                return "In-law / accessory unit within a home"
            if "room" in marker or "bedroom for rent" in marker or "roommate" in marker:
                return "Single room rented within a home"
            if "shared" in marker or "share" in marker:
                return "Shared house — not a private unit"
            return "Unit attached to someone's home"
    return ""

def normalize_title(apt):
    """
    Build a concise, factual title summarizing what the listing actually is,
    replacing Craigslist/marketing fluff like 'Put a smile on your face!'.
    Examples: '1-Bedroom apartment · 1 bath · 712 sqft · Falls Church',
              '1-Bedroom basement apartment · Arlington'.
    """
    blob = f"{apt.get('raw_name','')} {apt.get('description','')}"

    beds = apt.get("beds")
    if beds == 0:
        bed_str = "Studio"
    elif beds:
        bed_str = f"{beds}-Bedroom"
    else:
        bed_str = None

    # Bath count, if it can be read from the original text (e.g. "1B/1B", "2 bath")
    bath = None
    m = re.search(r"(\d+(?:\.\d)?)\s*(?:ba\b|bath)", blob, re.I)
    if not m:
        m = re.search(r"\d+\s*b(?:r|d)?\s*/\s*(\d+(?:\.\d)?)\s*ba", blob, re.I)
    if m:
        bath = m.group(1).rstrip(".0") or m.group(1)

    # Listing type from attached-home detection
    reason = apt.get("attached_home") or ""
    rl = reason.lower()
    if "basement" in rl:
        type_word = "basement apartment"
    elif "in-law" in rl or "accessory" in rl:
        type_word = "in-law unit"
    elif "room" in rl:
        type_word = "room in a home"
    elif "shared" in rl:
        type_word = "room in a shared house"
    elif reason:
        type_word = "unit in a private home"
    else:
        type_word = "apartment"

    # Head phrase
    if type_word == "room in a home" or type_word == "room in a shared house":
        head = type_word.capitalize() if not bed_str or bed_str == "Studio" else type_word.capitalize()
    elif bed_str:
        head = f"{bed_str} {type_word}"
    else:
        head = type_word.capitalize()

    parts = [head]
    if bath:
        parts.append(f"{bath} bath")
    if apt.get("sqft"):
        parts.append(f"{apt['sqft']:,} sqft")
    locality = (apt.get("address") or "").strip()
    if locality:
        parts.append(locality)
    return " · ".join(parts)

# ── Link validation ───────────────────────────────────────────────────────────
def validate_link(url):
    """Return True if the listing URL actually resolves (HTTP < 400), False if it's
    broken/expired, or None if there's no real URL to check. Tries a cheap HEAD first,
    falling back to GET for servers that reject HEAD."""
    if not url or not url.startswith("http"):
        return None
    try:
        r = SESSION.head(url, allow_redirects=True, timeout=8)
        if r.status_code == 405 or r.status_code >= 400:
            r = SESSION.get(url, allow_redirects=True, timeout=10, stream=True)
        # 403/429 from an anti-bot wall (e.g. Zillow's PerimeterX) means the page
        # exists but blocks our checker — not a dead listing. Don't penalize it.
        if r.status_code in (403, 429):
            return True
        return r.status_code < 400
    except Exception:
        return False

# ── Pet & parking policy detection ────────────────────────────────────────────
def detect_pets(text):
    """Read a listing's text for pet policy. Returns (dogs, cats), each True (allowed),
    False (explicitly not allowed), or None (not stated)."""
    t = (text or "").lower()
    dogs = cats = None
    # "Dog and Cat friendly" / "cats and dogs welcome" → both.
    if re.search(r"(dogs?\s+and\s+cats?|cats?\s+and\s+dogs?)[\s-]*(friendly|welcome|ok|okay|allowed)", t):
        dogs = True; cats = True
    # Generic pet-friendly implies both, unless contradicted below.
    if re.search(r"pet[\s-]?friendly|pets?\s+(ok|okay|welcome|allowed|considered)|we welcome your pets|pet lover|pet[\s-]?lover", t):
        dogs = True; cats = True
    if re.search(r"dogs?\s+(ok|okay|welcome|allowed|considered)|dog[\s-]?friendly", t):
        dogs = True
    if re.search(r"cats?\s+(ok|okay|welcome|allowed|considered)|cat[\s-]?friendly", t):
        cats = True
    # Explicit negatives override positives.
    if re.search(r"no pets|pets?\s+not\s+allowed|no animals|sorry,?\s*no pets", t):
        dogs = False; cats = False
    if "no dogs" in t:
        dogs = False
    if "no cats" in t:
        cats = False
    return dogs, cats

def detect_parking(text):
    """Read a listing's text for parking. Returns (free, price): free True (free/included),
    False (paid), or None (not stated); price = monthly $ if a number is stated, else None."""
    t = (text or "").lower()
    free = None
    price = None
    if re.search(r"free parking|parking (is )?(free|included)|includes? .{0,15}parking|"
                 r"parking included|complimentary parking|no[\s-]?cost parking|parking at no", t):
        free = True; price = 0
    # A dollar figure near the word "parking" → paid parking with a known price.
    m = (re.search(r"parking[^$\n]{0,25}\$\s?([\d,]{2,5})", t)
         or re.search(r"\$\s?([\d,]{2,5})[^$\n]{0,15}parking", t))
    if m:
        price = int(m.group(1).replace(",", "")); free = False
    return free, price

# Common amenities worth surfacing, mapped to the patterns that signal them. Display order =
# list order. Run over the listing text plus any explicit amenity list so every source
# normalizes to the same vocabulary (Apartments.com "In Unit Washer & Dryer" and a Craigslist
# "w/d in unit" both become "In-unit W/D").
_AMENITY_PATTERNS = [
    ("In-unit W/D",          r"in[\s-]?unit\s+(washer|laundry|w/?d)|washer\s*(/|and|&|\+)?\s*dryer\s+in[\s-]?unit|w/?d\s+in[\s-]?unit|in[\s-]?suite laundry|washer\s*(&|and|/)\s*dryer"),
    ("Laundry on-site",      r"laundry\s+(room|on[\s-]?site|facilit|center)|on[\s-]?site laundry|coin[\s-]?(op|laundry)|shared laundry"),
    ("Dishwasher",           r"dishwasher"),
    ("Air conditioning",     r"air[\s-]?condition|central air|central a/?c|\ba/?c\b|\bhvac\b"),
    ("Fitness center",       r"fitness|\bgym\b|exercise room|workout"),
    ("Pool",                 r"\bpool\b|swimming"),
    ("Parking/Garage",       r"parking|garage|carport"),
    ("Balcony/Patio",        r"balcon|patio|\bdeck\b|terrace"),
    ("Hardwood floors",      r"hardwood|wood floor"),
    ("Walk-in closet",       r"walk[\s-]?in closet"),
    ("Stainless appliances", r"stainless"),
    ("Elevator",             r"elevator"),
    ("Doorman/Concierge",    r"doorman|concierge|front desk"),
    ("Rooftop",              r"rooftop|roof deck"),
    ("Pet friendly",         r"pet[\s-]?friendly|pets?\s+(ok|okay|welcome|allowed)|dog[\s-]?friendly|cat[\s-]?friendly"),
    ("Furnished",            r"\bfurnished\b"),
    ("EV charging",          r"\bev\b\s*charg|electric vehicle charg|ev[\s-]?charg"),
]

def detect_amenities(text):
    """Return de-duplicated, normalized amenity labels found in the text, in a stable display
    order. Source-agnostic — feed it the listing text plus any explicit amenity list."""
    t = (text or "").lower()
    return [label for label, pat in _AMENITY_PATTERNS if re.search(pat, t)]

# ── Enrichment: Distance & Reviews ───────────────────────────────────────────
def infer_pros_cons(apt, criteria, metro_coords):
    pros, cons = [], []
    if apt.get("attached_home"):
        cons.append(apt["attached_home"])
    if apt.get("link_ok") is False:
        cons.append("Listing link appears broken or expired")
    # Pets
    if apt.get("pets_dogs"):
        pros.append("Dogs welcome")
    if apt.get("pets_cats"):
        pros.append("Cats welcome")
    if apt.get("pets_dogs") is False and apt.get("pets_cats") is False:
        cons.append("No pets allowed")
    elif apt.get("pets_dogs") is False:
        cons.append("No dogs")
    elif apt.get("pets_cats") is False:
        cons.append("No cats")
    # Parking
    if apt.get("parking_free"):
        pros.append("Free parking")
    elif apt.get("parking_price"):
        cons.append(f"Paid parking ~${apt['parking_price']}/mo")
    if apt["price"] and apt["price"] <= criteria["max_price"] * 0.8:
        pros.append("Well under budget")
    if apt["price"] and apt["price"] > criteria["max_price"] * 0.95:
        cons.append("Near top of budget")
    if apt["sqft"] and apt["sqft"] >= 800:
        pros.append(f"Spacious at {apt['sqft']} sqft")
    if apt["sqft"] and apt["sqft"] < 500:
        cons.append(f"Small at {apt['sqft']} sqft")
    if apt["metro_walk_mins"] is not None:
        if apt["metro_walk_mins"] <= 10:
            pros.append(f"Short {apt['metro_walk_mins']} min walk to metro")
        elif apt["metro_walk_mins"] >= 25:
            cons.append(f"Long {apt['metro_walk_mins']} min walk to metro")
    if apt["source"] == "Craigslist":
        cons.append("Posted on Craigslist – verify legitimacy")
    return pros, cons

def estimate_parking(address, city):
    """Rough monthly parking estimate by city tier."""
    tiers = {
        "new york": 400, "nyc": 400, "san francisco": 350, "boston": 280,
        "washington": 220, "dc": 220, "chicago": 200, "seattle": 180,
        "los angeles": 160, "denver": 120, "austin": 100, "miami": 130,
    }
    city_l = city.lower()
    for k, v in tiers.items():
        if k in city_l:
            return v
    return 100

def scrape_reviews(name, address, city):
    """Fetch Google and Yelp review scores for an apartment property."""
    google_rating, google_count, google_url = None, None, None
    yelp_rating, yelp_count, yelp_url_found = None, None, None

    query = f"{name} {city} apartments reviews"
    # A geographic query that actually resolves to the property on a map/review page
    place_query = ", ".join(filter(None, [name, address, city])).strip(", ")
    hdrs  = {**HEADERS, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}

    # ── Google ──────────────────────────────────────────────────────────────
    try:
        r = SESSION.get(
            "https://www.google.com/search",
            params={"q": query},
            headers=hdrs, timeout=10
        )
        soup = BeautifulSoup(r.text, "html.parser")
        # Try structured JSON-LD first
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(tag.string or "")
                if isinstance(d, list):
                    d = d[0]
                agg = d.get("aggregateRating") or {}
                rv  = agg.get("ratingValue") or d.get("ratingValue")
                rc  = agg.get("reviewCount") or agg.get("ratingCount") or d.get("reviewCount")
                if rv:
                    google_rating = round(float(rv), 1)
                    google_count  = int(rc) if rc else None
                    break
            except Exception:
                continue
        # Fallback: scrape the visible rating text
        if not google_rating:
            for span in soup.find_all("span", attrs={"aria-label": True}):
                label = span["aria-label"]
                m = re.search(r"([\d.]+)\s*(?:out of 5|stars?|/5)", label, re.I)
                if m:
                    google_rating = round(float(m.group(1)), 1)
                    count_m = re.search(r"([\d,]+)\s*reviews?", label, re.I)
                    google_count  = int(re.sub(r",", "", count_m.group(1))) if count_m else None
                    break
        # Google Maps lands directly on the place card with its reviews,
        # not a generic web-search results page.
        google_url = ("https://www.google.com/maps/search/?api=1&query="
                      + urllib.parse.quote_plus(place_query))
    except Exception:
        pass

    # ── Yelp ────────────────────────────────────────────────────────────────
    try:
        yelp_q = urllib.parse.urlencode({"find_desc": name, "find_loc": city})
        r2 = SESSION.get(
            f"https://www.yelp.com/search?{yelp_q}",
            headers=hdrs, timeout=10
        )
        soup2 = BeautifulSoup(r2.text, "html.parser")
        # JSON-LD on Yelp search page
        for tag in soup2.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(tag.string or "")
                items = d if isinstance(d, list) else d.get("itemListElement", [d])
                for item in items:
                    it = item.get("item", item)
                    agg = it.get("aggregateRating", {})
                    rv  = agg.get("ratingValue")
                    rc  = agg.get("reviewCount") or agg.get("ratingCount")
                    url = it.get("url", "")
                    if rv:
                        yelp_rating     = round(float(rv), 1)
                        yelp_count      = int(rc) if rc else None
                        # JSON-LD often gives a relative /biz/... path — make it absolute
                        if url.startswith("/"):
                            url = "https://www.yelp.com" + url
                        yelp_url_found  = url or None
                        break
                if yelp_rating:
                    break
            except Exception:
                continue
        # Fallback: aria-label patterns
        if not yelp_rating:
            for el in soup2.find_all(attrs={"aria-label": re.compile(r"star rating", re.I)}):
                m = re.search(r"([\d.]+)", el.get("aria-label", ""))
                if m:
                    yelp_rating = round(float(m.group(1)), 1)
                    break
        if not yelp_url_found:
            yelp_url_found = f"https://www.yelp.com/search?{yelp_q}"
    except Exception:
        pass

    return {
        "google_rating": google_rating,
        "google_count":  google_count,
        "google_url":    google_url,
        "yelp_rating":   yelp_rating,
        "yelp_count":    yelp_count,
        "yelp_url":      yelp_url_found,
    }

def fetch_craigslist_images(url, max_imgs=8):
    """Craigslist listing detail pages are plain HTML (no bot wall) and embed every
    photo. Pull the gallery image URLs so the report can show more than one picture."""
    if not url or "craigslist.org" not in url:
        return []
    try:
        r = SESSION.get(url, timeout=10)
        if r.status_code != 200:
            return []
        urls = re.findall(r'https://images\.craigslist\.org/[0-9A-Za-z_]+_[0-9]+x[0-9]+\.jpg', r.text)
        seen, out = set(), []
        for u in urls:
            base = u.rsplit("_", 1)[0]   # dedupe the same image across size variants
            if base in seen:
                continue
            seen.add(base)
            out.append(u)
            if len(out) >= max_imgs:
                break
        return out
    except Exception:
        return []

async def _rating_google(page, name, city):
    """Read a Google Maps rating + count for one property, or None."""
    try:
        q = f"{name} {city}"
        await page.goto("https://www.google.com/maps/search/" + urllib.parse.quote(q),
                        wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(3, 4.5))
        labels = await page.eval_on_selector_all(
            "[role='feed'] [role='img'][aria-label*='star'], "
            "[role='main'] [role='img'][aria-label*='star'], "
            "span[aria-label*='stars']",
            "els=>els.map(e=>e.getAttribute('aria-label'))")
        rating = count = None
        for L in (labels or []):               # prefer a label with both rating + count
            m = re.search(r'([0-5]\.\d)\s*stars?\s*([\d,]+)?\s*[Rr]eview', L or '')
            if m:
                rating = float(m.group(1))
                count = int((m.group(2) or "0").replace(",", "")) or None
                break
        if rating is None:
            for L in (labels or []):
                m = re.search(r'([0-5]\.\d)\s*stars?', L or '')
                if m:
                    rating = float(m.group(1)); break
        return (rating, count) if rating is not None else None
    except Exception:
        return None

def _fetch_yelp_api(name, city, key):
    """Yelp rating + count + url via the official Fusion API, or None. Yelp hard-blocks
    scraping (serves an empty anti-bot shell to any browser), so the API key is the only
    reliable path. Free tier at yelp.com/developers."""
    if not key:
        return None
    try:
        r = requests.get(
            "https://api.yelp.com/v3/businesses/search",
            headers={"Authorization": f"Bearer {key}"},
            params={"term": name, "location": city, "limit": 1},
            timeout=10)
        if r.status_code != 200:
            return None
        bs = (r.json() or {}).get("businesses") or []
        if not bs:
            return None
        b = bs[0]
        if b.get("rating") is None:
            return None
        return (b.get("rating"), b.get("review_count"), b.get("url"))
    except Exception:
        return None

async def _fetch_ratings(queries, concurrency=5):
    """Look up Google Maps ratings + counts for named properties in ONE Edge session, running up
    to `concurrency` properties in parallel (own tab each). queries: list of (key, name, city).
    Returns {key: (rating, count)|None}. Uses the real installed browser (same trick as Zillow)
    — Maps renders ratings in aria-labels but ships nothing scrapeable to plain requests."""
    from playwright.async_api import async_playwright
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    out = {}
    async with async_playwright() as p:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                kw = {"headless": False, "args": ["--no-sandbox"]}
                if channel:
                    kw["channel"] = channel
                browser = await p.chromium.launch(**kw)
                break
            except Exception:
                continue
        if browser is None:
            return out
        try:
            ctx = await browser.new_context(user_agent=ua, locale="en-US")
            sem = asyncio.Semaphore(concurrency)

            async def one(key, name, city):
                async with sem:                    # cap concurrent tabs to avoid rate-limiting
                    page = await ctx.new_page()
                    try:
                        return key, await _rating_google(page, name, city)
                    finally:
                        try:
                            await page.close()
                        except Exception:
                            pass

            for fut in asyncio.as_completed([one(k, n, c) for k, n, c in queries]):
                try:
                    key, g = await fut
                    out[key] = g
                except Exception:
                    pass
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    return out

def _property_name_for_reviews(apt):
    """Return a real property/building name worth a Google/Yelp rating lookup, else None.
    Only named complexes get reviews — a generic Craigslist title would match a random
    nearby place and show a bogus rating, so those are skipped."""
    if apt.get("source") in ("Zillow", "Apartments.com", "Local site"):
        nm = (apt.get("name") or "").split(" · ")[0].strip()
        return nm or None
    # Craigslist private rentals have no review page; skip unless clearly a named complex.
    return None

_RATINGS_TTL = 30 * 24 * 3600   # ratings change slowly — cache for 30 days

def _ratings_cache_file():
    return project_dir() / "data" / ".ratings_cache.json"

def _load_ratings_cache():
    try:
        return json.loads(_ratings_cache_file().read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_ratings_cache(cache):
    try:
        f = _ratings_cache_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass

def _enrich_reviews(apts, criteria):
    """Batch-fetch Google + Yelp ratings for named properties (deduped) and attach them.
    Cached results (<30 days) are reused so repeat runs skip the slow browser lookups."""
    city = f"{criteria['location']}, {criteria['state']}"
    by_name = {}
    for i, apt in enumerate(apts):
        nm = _property_name_for_reviews(apt)
        if nm:
            by_name.setdefault(nm, []).append(i)
    if not by_name:
        return
    ykey = criteria.get("yelp_key")
    cache = _load_ratings_cache()
    now = time.time()
    fresh, todo = {}, []
    for nm in by_name:
        e = cache.get(f"{nm}|{city}".lower())
        # Re-fetch if stale, or if we now have a Yelp key but the cached entry has no Yelp data.
        stale = not e or (now - e.get("ts", 0) >= _RATINGS_TTL)
        if not stale and ykey and not e.get("y"):
            stale = True
        if stale:
            todo.append(nm)
        else:
            fresh[nm] = e
    label = "Google + Yelp" if ykey else "Google"
    print(f"  Looking up {label} ratings for {len(by_name)} named properties "
          f"({len(fresh)} cached, {len(todo)} to fetch)...")
    if todo:
        try:
            gmap = asyncio.run(_fetch_ratings([(nm, nm, city) for nm in todo]))
        except Exception:
            gmap = {}
        for nm in todo:
            y = _fetch_yelp_api(nm, city, ykey) if ykey else None
            rec = {"g": gmap.get(nm), "y": y, "ts": now}
            cache[f"{nm}|{city}".lower()] = rec
            fresh[nm] = rec
        _save_ratings_cache(cache)
    for nm, idxs in by_name.items():
        gurl = ("https://www.google.com/maps/search/?api=1&query="
                + urllib.parse.quote_plus(f"{nm} {city}"))
        rec = fresh.get(nm) or {}
        g = rec.get("g")
        y = rec.get("y")
        for i in idxs:
            apts[i]["google_url"] = gurl
            if g:
                apts[i]["google_rating"] = g[0]
                apts[i]["google_count"] = g[1]
            if y:
                apts[i]["yelp_rating"] = y[0]
                apts[i]["yelp_count"] = y[1]
                apts[i]["yelp_url"] = y[2]

def enrich_apartments(apts, criteria, metro_coords, work_coords):
    print(f"\nEnriching {len(apts)} listings with distance data and reviews...")
    # Batch Google + Yelp ratings up front (named properties only) so pros/cons can use them.
    # Always runs by design — there is no opt-out flag.
    _enrich_reviews(apts, criteria)
    for i, apt in enumerate(apts):
        label = apt["name"][:48]
        print(f"  [{i+1}/{len(apts)}] {label}...", end="\r")

        # Geocode (skip if Craigslist already provided coords)
        if not apt.get("coords"):
            addr_str = apt["address"] or apt["name"]
            if addr_str:
                apt["coords"] = geocode(f"{addr_str}, {criteria['location']}, {criteria['state']}")
            else:
                apt["coords"] = None

        # Metro distance — walk estimate + drive via OSRM
        if apt["coords"] and metro_coords:
            w_mi, w_min = metro_walk_info(apt["coords"], metro_coords)
            apt["metro_walk_miles"] = w_mi
            apt["metro_walk_mins"]  = w_min
            d_mi, d_min = driving_distance(apt["coords"], metro_coords)
            apt["metro_drive_miles"] = d_mi
            apt["metro_drive_mins"]  = d_min

        # Work commute
        if apt["coords"] and work_coords:
            d_mi, d_min = driving_distance(apt["coords"], work_coords)
            apt["drive_miles"] = d_mi
            apt["drive_mins"]  = d_min
            if criteria.get("gmaps_key") and apt["address"]:
                t_dist, t_time = transit_time_gmaps(
                    apt["address"], criteria["work_address"], criteria["gmaps_key"]
                )
                apt["transit_distance"] = t_dist
                apt["transit_time"]     = t_time

        # Parking estimate
        apt["parking_est"] = estimate_parking(apt["address"], criteria["location"])

        # Detect whether the unit is attached to someone's home (basement/room/in-law),
        # then rewrite the title into a clean factual summary.
        apt["attached_home"] = detect_attached_home(
            apt.get("name"), apt.get("description"), apt.get("address")
        )
        apt["raw_name"] = apt.get("name", "")
        apt["name"] = normalize_title(apt)

        # Validate that the listing link actually resolves
        apt["link_ok"] = validate_link(apt.get("url")) if criteria.get("check_links", True) else None

        # Pet policy (dogs/cats separately) and parking (free vs paid + price) from the
        # original listing text — use raw_name since name was just normalized.
        pet_blob = f"{apt.get('raw_name','')} {apt.get('description','')}"
        apt["pets_dogs"], apt["pets_cats"] = detect_pets(pet_blob)
        apt["parking_free"], apt["parking_price"] = detect_parking(pet_blob)

        # Amenities: normalize from the listing text plus any explicit amenity list (e.g. the
        # Apartments.com bookmarklet captures one) into a single shared vocabulary.
        amen_blob = f"{pet_blob} {' '.join(apt.get('amenities') or [])}"
        apt["amenities"] = detect_amenities(amen_blob)

        # Multiple photos: Craigslist detail pages carry the full gallery; pull them so
        # the report can show more than one picture. (Ratings handled in batch above.)
        if apt.get("source") == "Craigslist" and apt.get("url"):
            imgs = fetch_craigslist_images(apt["url"])
            if imgs:
                apt["images"] = imgs
                if not apt.get("image"):
                    apt["image"] = imgs[0]
        if not apt.get("images"):
            apt["images"] = [apt["image"]] if apt.get("image") else []

        # Pros/cons
        apt["pros"], apt["cons"] = infer_pros_cons(apt, criteria, metro_coords)

        time.sleep(0.4)  # Polite delay for geocoding + routing APIs

    print(f"  Enrichment complete.         ")
    return apts

# ── HTML Generation ───────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Apartments — {location}, {state}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=Hanken+Grotesk:wght@400;500;600&display=swap');

/* ── Reset ── */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
html {{ scroll-behavior: smooth; font-size: 16px; }}
body {{
  font-family: 'Hanken Grotesk', -apple-system, system-ui, sans-serif;
  background: var(--bg); color: var(--text);
  min-height: 100vh; line-height: 1.6;
  font-feature-settings: 'tnum' 1;
  -webkit-font-smoothing: antialiased;
}}

/* ── Tokens: warm paper & ink, one clay accent ── */
:root {{
  --bg:      #f7f5ef;
  --surface: #f7f5ef;
  --raised:  #fffefb;
  --border:  #e6e1d4;
  --border2: #d6d0c0;
  --text:    #1b1a16;
  --text2:   #6b6657;
  --text3:   #9c9684;
  --accent:  #9e4a2e;
  --accent2: #b86a4b;
  --g-amber: #b08400;
  --y-red:   #9e4a2e;
  --green:   #46603e;
  --red:     #9e4a2e;
  --radius:  3px;
  --radius-s:2px;
  --serif:   'Fraunces', Georgia, serif;
}}
[data-theme="dark"] {{
  --bg:      #15140f;
  --surface: #15140f;
  --raised:  #1d1b15;
  --border:  #2b291f;
  --border2: #3a382c;
  --text:    #ece7da;
  --text2:   #a39d8b;
  --text3:   #6f6a59;
  --accent:  #cf855f;
  --accent2: #e0a07f;
  --g-amber: #d9b25a;
  --y-red:   #cf855f;
  --green:   #9ab089;
  --red:     #cf855f;
}}

::selection {{ background: var(--accent); color: var(--bg); }}

/* ── Header ── */
header {{ background: var(--bg); border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 100; }}
.header-inner {{
  max-width: 1180px; margin: 0 auto;
  display: flex; align-items: baseline; gap: 2rem;
  padding: 0 2.5rem; height: 84px;
}}
.logo {{
  font-family: var(--serif); font-size: 1.5rem; font-weight: 500;
  color: var(--text); letter-spacing: -.01em; white-space: nowrap; flex-shrink: 0;
}}
.logo-meta {{ font-family: 'Hanken Grotesk'; color: var(--text3); font-weight: 400; margin-left: .6rem; font-size: .72rem; letter-spacing: .06em; text-transform: uppercase; }}
.search-wrap {{
  flex: 1; max-width: 280px; display: flex; align-items: center; gap: .55rem;
  background: transparent; border: none; border-bottom: 1px solid var(--border2);
  border-radius: 0; padding: .3rem .1rem; transition: border-color .2s;
}}
.search-wrap:focus-within {{ border-color: var(--accent); }}
.search-wrap svg {{ color: var(--text3); flex-shrink: 0; }}
.search-wrap input {{ border: none; background: transparent; width: 100%; font-size: .82rem; color: var(--text); outline: none; font-family: inherit; }}
.search-wrap input::placeholder {{ color: var(--text3); }}
.hdr-right {{ margin-left: auto; display: flex; gap: 1.5rem; align-items: baseline; }}
.hdr-count {{ font-size: .72rem; color: var(--text3); letter-spacing: .06em; text-transform: uppercase; }}
.hdr-count strong {{ color: var(--text); font-weight: 600; }}
.hdr-btn {{
  font-size: .72rem; letter-spacing: .06em; text-transform: uppercase;
  color: var(--text3); background: none; border: none; cursor: pointer;
  padding: 0; transition: color .2s; font-family: inherit;
}}
.hdr-btn:hover {{ color: var(--accent); }}

/* ── Layout ── */
.layout {{ max-width: 1180px; margin: 0 auto; display: grid; grid-template-columns: 200px 1fr; gap: 3.5rem; padding: 3rem 2.5rem 5rem; }}

/* ── Sidebar ── */
aside {{ position: sticky; top: 108px; align-self: start; max-height: calc(100vh - 130px); overflow-y: auto; padding-right: .25rem; }}
aside::-webkit-scrollbar {{ width: 0; }}
.sb-section {{ margin-bottom: 2.25rem; }}
.sb-title {{ font-size: .66rem; font-weight: 600; letter-spacing: .14em; text-transform: uppercase; color: var(--text3); margin-bottom: 1.1rem; display: block; }}
.sb-row {{ margin-bottom: 1rem; }}
.sb-label {{ font-size: .8rem; color: var(--text2); display: flex; justify-content: space-between; margin-bottom: .7rem; }}
.sb-label span {{ font-family: var(--serif); color: var(--text); font-weight: 500; font-size: .95rem; }}
input[type=range] {{ width: 100%; accent-color: var(--accent); height: 1px; cursor: pointer; display: block; }}
.range-ends {{ display: flex; justify-content: space-between; font-size: .68rem; color: var(--text3); margin-top: .5rem; letter-spacing: .03em; }}
.check-list {{ display: flex; flex-direction: column; gap: .1rem; }}
.check-item {{ display: flex; align-items: center; gap: .6rem; font-size: .82rem; color: var(--text2); cursor: pointer; padding: .3rem 0; transition: color .15s; }}
.check-item input {{ accent-color: var(--accent); cursor: pointer; width: 13px; height: 13px; }}
.check-item:hover {{ color: var(--text); }}
.src-list {{ display: flex; flex-wrap: wrap; gap: .4rem; }}
.src-tag {{ font-size: .72rem; padding: .25rem 0; margin-right: .9rem; border: none; color: var(--text3); cursor: pointer; user-select: none; border-bottom: 1px solid transparent; transition: all .15s; }}
.src-tag.on {{ color: var(--text); border-bottom-color: var(--accent); }}
.sb-select {{ width: 100%; font-family: inherit; font-size: .82rem; color: var(--text); background: transparent; border: none; border-bottom: 1px solid var(--border2); border-radius: 0; padding: .4rem 0; cursor: pointer; outline: none; transition: border-color .2s; }}
.sb-select:focus {{ border-color: var(--accent); }}
.sb-divider {{ border: none; border-top: 1px solid var(--border); margin: 2rem 0; }}
.sb-meta {{ font-size: .76rem; color: var(--text3); line-height: 2; }}
.sb-meta strong {{ color: var(--text2); font-weight: 500; }}

/* ── Stats: oversized serif numerals ── */
.stats-bar {{ display: flex; gap: 3rem; margin-bottom: 2.75rem; padding-bottom: 0; border: none; flex-wrap: wrap; }}
.stat-n {{ font-family: var(--serif); font-size: 2.4rem; font-weight: 400; color: var(--text); letter-spacing: -.02em; line-height: 1; }}
.stat-l {{ font-size: .66rem; color: var(--text3); text-transform: uppercase; letter-spacing: .12em; margin-top: .6rem; }}

/* ── Toolbar ── */
.toolbar {{ display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 2rem; padding-bottom: 1rem; border-bottom: 1px solid var(--border); }}
.toolbar-left {{ font-size: .72rem; color: var(--text3); letter-spacing: .08em; text-transform: uppercase; }}
.view-btns {{ display: flex; gap: 1.25rem; }}
.v-btn {{ padding: 0; font-size: .72rem; letter-spacing: .06em; text-transform: uppercase; border: none; background: none; color: var(--text3); cursor: pointer; transition: color .2s; font-family: inherit; }}
.v-btn.on {{ color: var(--accent); }}

/* ── Cards: airy, borderless, hairline separated ── */
.grid-wrap {{ border: none; border-radius: 0; overflow: visible; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(310px, 1fr)); gap: 2.75rem 2.5rem; }}
.grid.list {{ grid-template-columns: 1fr; gap: 0; }}

.card {{ background: transparent; cursor: pointer; border: none; display: flex; flex-direction: column; transition: transform .25s ease; }}
.card:last-child {{ border: none; }}
.grid:not(.list) .card {{ border: none; }}
.card:hover {{ background: transparent; transform: translateY(-3px); }}
.card:hover .card-name {{ color: var(--accent); }}
.grid.list .card {{ flex-direction: row; gap: 1.75rem; align-items: flex-start; padding: 1.75rem 0; border-bottom: 1px solid var(--border); transition: none; }}
.grid.list .card:hover {{ transform: none; }}
.grid.list .card-photo {{ width: 200px; flex-shrink: 0; }}
.grid.list .card-body {{ flex: 1; }}

.card-photo {{ position: relative; height: 210px; background: var(--border); overflow: hidden; flex-shrink: 0; border-radius: 2px; }}
.card-photo img {{ width: 100%; height: 100%; object-fit: cover; display: block; filter: grayscale(1) contrast(.96); transition: filter .5s ease, transform .6s ease; }}
.card:hover .card-photo img {{ filter: grayscale(0); transform: scale(1.03); }}
.card-photo-none {{ width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; color: var(--text3); font-size: .72rem; letter-spacing: .08em; text-transform: uppercase; font-family: var(--serif); font-style: italic; }}
.card-source {{ position: absolute; top: .7rem; left: .7rem; font-size: .6rem; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: var(--text); background: var(--bg); padding: .25rem .5rem; border-radius: 2px; }}

.card-body {{ padding: 1.1rem .15rem 0; flex: 1; display: flex; flex-direction: column; gap: .55rem; }}
.card-name {{ font-family: var(--serif); font-size: 1.12rem; font-weight: 500; color: var(--text); line-height: 1.3; letter-spacing: -.01em; transition: color .2s; }}
.card-addr {{ font-size: .76rem; color: var(--text3); letter-spacing: .02em; }}
.card-price {{ font-family: var(--serif); font-size: 1.35rem; font-weight: 500; color: var(--text); letter-spacing: -.01em; margin-top: .15rem; }}
.card-price .mo {{ font-family: 'Hanken Grotesk'; font-size: .72rem; font-weight: 400; color: var(--text3); }}
.card-price .contact {{ font-family: var(--serif); font-style: italic; font-size: .95rem; font-weight: 400; color: var(--text3); }}

.card-chips {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
.chip {{ font-size: .74rem; color: var(--text2); background: transparent; border: none; border-radius: 0; padding: 0; position: relative; }}
.chip + .chip::before {{ content: ""; position: absolute; left: -.5rem; top: 50%; width: 2px; height: 2px; border-radius: 50%; background: var(--border2); transform: translateY(-50%); }}

.card-reviews {{ display: flex; gap: .9rem; align-items: center; }}
.rev-badge {{ display: flex; align-items: center; gap: .35rem; font-size: .74rem; }}
.rev-badge .stars {{ color: var(--g-amber); letter-spacing: -.05em; font-size: .72rem; }}
.rev-badge .score {{ font-weight: 600; color: var(--text); }}
.rev-badge .ct {{ color: var(--text3); }}
.rev-badge .brand {{ font-size: .6rem; font-weight: 600; letter-spacing: .1em; text-transform: uppercase; color: var(--text3); }}
.rev-badge.yelp .stars {{ color: var(--y-red); }}
.rev-sep {{ color: var(--border2); font-size: .7rem; }}

.card-dist {{ font-size: .76rem; color: var(--text2); display: flex; flex-direction: column; gap: .3rem; padding-top: .35rem; border-top: 1px solid var(--border); margin-top: .25rem; }}
.dist-row {{ display: flex; justify-content: space-between; gap: .5rem; }}
.dist-label {{ color: var(--text3); white-space: nowrap; letter-spacing: .02em; }}
.dist-val {{ font-weight: 500; color: var(--text2); text-align: right; }}

.card-tags {{ display: flex; gap: .5rem; flex-wrap: wrap; margin-top: .15rem; padding-top: 0; }}
.tag {{ font-size: .68rem; padding: 0; border-radius: 0; font-weight: 500; letter-spacing: .01em; }}
.tag.pro {{ background: transparent; color: var(--green); border: none; }}
.tag.pro::before {{ content: "+ "; }}
.tag.con {{ background: transparent; color: var(--accent); border: none; }}
.tag.con::before {{ content: "– "; }}
.tag + .tag {{ margin-left: .35rem; }}

.card-link {{ padding: 0; border-top: none; display: none; }}

/* ── Empty ── */
.empty {{ padding: 6rem 2rem; text-align: center; color: var(--text3); grid-column: 1 / -1; }}
.empty strong {{ font-family: var(--serif); font-weight: 500; font-size: 1.15rem; color: var(--text2); }}
.empty p {{ font-size: .82rem; margin-top: .6rem; }}

/* ── Modal ── */
.overlay {{ display: none; position: fixed; inset: 0; background: color-mix(in srgb, var(--text) 22%, transparent); backdrop-filter: blur(3px); z-index: 200; align-items: center; justify-content: center; padding: 1.5rem; }}
.overlay.open {{ display: flex; animation: fade .25s ease; }}
@keyframes fade {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
.modal {{ background: var(--raised); border-radius: 3px; max-width: 580px; width: 100%; max-height: 92vh; overflow-y: auto; border: 1px solid var(--border); position: relative; box-shadow: 0 30px 80px -20px color-mix(in srgb, var(--text) 35%, transparent); }}
.modal::-webkit-scrollbar {{ width: 0; }}
.m-close {{ position: absolute; top: 1.1rem; right: 1.1rem; width: 30px; height: 30px; border-radius: 50%; border: 1px solid var(--border2); background: var(--raised); color: var(--text2); cursor: pointer; z-index: 5; display: flex; align-items: center; justify-content: center; font-size: .8rem; transition: all .2s; }}
.m-close:hover {{ background: var(--accent); color: var(--bg); border-color: var(--accent); }}
.m-photo {{ width: 100%; height: 240px; object-fit: cover; display: block; filter: grayscale(.35); }}
.m-gallery {{ display: flex; overflow-x: auto; scroll-snap-type: x mandatory; gap: 4px; scrollbar-width: thin; }}
.m-gallery .m-photo {{ flex: 0 0 100%; scroll-snap-align: center; }}
.m-gallery::-webkit-scrollbar {{ height: 7px; }}
.m-gallery::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}
.m-count {{ font-size: .72rem; color: var(--text3); padding: .25rem 0 0; }}
.m-photo-none {{ width: 100%; height: 120px; background: var(--border); display: flex; align-items: center; justify-content: center; color: var(--text3); font-family: var(--serif); font-style: italic; font-size: .82rem; }}
.m-body {{ padding: 2rem 2.25rem 2.25rem; }}
.m-source {{ font-size: .62rem; font-weight: 600; letter-spacing: .14em; text-transform: uppercase; color: var(--accent); margin-bottom: .6rem; }}
.m-name {{ font-family: var(--serif); font-size: 1.55rem; font-weight: 500; color: var(--text); line-height: 1.2; letter-spacing: -.02em; margin-bottom: .35rem; }}
.m-addr {{ font-size: .8rem; color: var(--text3); margin-bottom: 1.75rem; }}
.m-top {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; margin-bottom: 2rem; padding-bottom: 1.75rem; border-bottom: 1px solid var(--border); }}
.m-price {{ font-family: var(--serif); font-size: 2.1rem; font-weight: 500; color: var(--text); letter-spacing: -.03em; }}
.m-price .mo {{ font-family: 'Hanken Grotesk'; font-size: .8rem; font-weight: 400; color: var(--text3); }}
.m-reviews-block {{ display: flex; flex-direction: column; gap: .45rem; align-items: flex-end; }}
.m-rev-row {{ display: flex; align-items: center; gap: .4rem; font-size: .78rem; text-decoration: none; }}
.m-rev-row .brand {{ font-size: .6rem; font-weight: 600; letter-spacing: .1em; text-transform: uppercase; width: 40px; color: var(--text3); }}
.m-rev-row .stars {{ letter-spacing: -.05em; }}
.m-rev-row .g-stars {{ color: var(--g-amber); }}
.m-rev-row .y-stars {{ color: var(--y-red); }}
.m-rev-row .score {{ font-weight: 600; color: var(--text); }}
.m-rev-row .ct {{ color: var(--text3); }}
.m-rev-row:hover .score {{ color: var(--accent); }}
.m-sec {{ margin-bottom: 2rem; }}
.m-sec-title {{ font-size: .62rem; font-weight: 600; letter-spacing: .14em; text-transform: uppercase; color: var(--text3); margin-bottom: 1.1rem; padding-bottom: 0; border-bottom: none; }}
.m-chips {{ display: flex; flex-wrap: wrap; gap: .5rem; }}
.m-chip {{ font-size: .76rem; color: var(--text2); background: var(--bg2, rgba(127,127,127,.10)); border: 1px solid var(--border); border-radius: 999px; padding: .3rem .7rem; white-space: nowrap; }}
.m-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem 1.75rem; }}
.m-cell {{ padding: 0; background: transparent; border-radius: 0; border-bottom: 1px solid var(--border); padding-bottom: .7rem; }}
.m-cell .lbl {{ font-size: .64rem; text-transform: uppercase; letter-spacing: .1em; color: var(--text3); }}
.m-cell .val {{ font-family: var(--serif); font-size: 1.05rem; font-weight: 500; color: var(--text); margin-top: .35rem; }}
.cost-rows {{ display: flex; flex-direction: column; gap: 0; }}
.cost-row {{ display: flex; justify-content: space-between; font-size: .85rem; padding: .65rem 0; border-bottom: 1px solid var(--border); color: var(--text2); }}
.cost-row:last-child {{ border-bottom: none; font-family: var(--serif); font-weight: 500; color: var(--text); font-size: 1.05rem; padding-top: .9rem; }}
.cost-row span:last-child {{ font-weight: 600; }}
.pc-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; }}
.pc-col {{ padding: 0; background: transparent; border-radius: 0; border-top: 1px solid var(--border); padding-top: .9rem; }}
.pc-col.pros, .pc-col.cons {{ border-top: 1px solid var(--border); }}
.pc-col h4 {{ font-size: .62rem; font-weight: 600; text-transform: uppercase; letter-spacing: .1em; margin-bottom: .8rem; color: var(--text3); }}
.pc-col ul {{ list-style: none; display: flex; flex-direction: column; gap: .5rem; }}
.pc-col ul li {{ font-size: .82rem; color: var(--text2); padding-left: 1rem; position: relative; line-height: 1.45; }}
.pc-col.pros ul li::before {{ content: "+"; position: absolute; left: 0; color: var(--green); }}
.pc-col.cons ul li::before {{ content: "–"; position: absolute; left: 0; color: var(--accent); }}
.m-link {{ display: inline-block; text-align: left; margin-top: 1.5rem; padding: 0 0 .25rem; background: transparent; color: var(--text); border-bottom: 1px solid var(--accent); border-radius: 0; font-family: var(--serif); font-size: 1rem; font-weight: 500; text-decoration: none; transition: color .2s; }}
.m-link::after {{ content: " \2197"; }}
.m-link:hover {{ color: var(--accent); }}

/* ── Responsive ── */
@media (max-width: 860px) {{
  .layout {{ grid-template-columns: 1fr; gap: 2.5rem; }}
  aside {{ position: static; max-height: none; }}
  .header-inner {{ padding: 0 1.5rem; height: 72px; gap: 1.25rem; }}
  .grid {{ grid-template-columns: 1fr 1fr; gap: 2rem 1.5rem; }}
}}
@media (max-width: 560px) {{
  .grid {{ grid-template-columns: 1fr; }}
  .layout {{ padding: 2rem 1.5rem 3rem; }}
  .search-wrap {{ display: none; }}
  .m-top {{ flex-direction: column; gap: .75rem; }}
  .m-reviews-block {{ align-items: flex-start; }}
  .m-grid, .pc-grid {{ grid-template-columns: 1fr; gap: 1.25rem; }}
  .stats-bar {{ gap: 2rem; }}
}}
::-webkit-scrollbar {{ width: 6px; height: 6px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{ background: var(--border2); border-radius: 3px; }}
</style>
</head>
<body>

<header>
  <div class="header-inner">
    <div class="logo">{location}, {state}<span class="logo-meta">· {date}</span></div>
    <div class="search-wrap">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input type="text" id="searchInput" placeholder="Search listings…" oninput="filterCards()">
    </div>
    <div class="hdr-right">
      <span class="hdr-count"><strong id="countBadge">0</strong> listings</span>
      <button class="hdr-btn" onclick="toggleTheme()">Light / Dark</button>
      <button class="hdr-btn" onclick="window.print()">Print</button>
    </div>
  </div>
</header>

<div class="layout">
  <aside>
    <div class="sb-section">
      <span class="sb-title">Price</span>
      <div class="sb-row">
        <div class="sb-label">Max rent <span id="priceVal">${max_price}</span>/mo</div>
        <input type="range" id="priceFilter" min="{min_price}" max="{max_price_cap}"
               value="{max_price_cap}" step="50" oninput="updatePrice(this.value);filterCards()">
        <div class="range-ends"><span>${min_price}</span><span>${max_price_cap}</span></div>
      </div>
    </div>

    <div class="sb-section">
      <span class="sb-title">Bedrooms</span>
      <div class="check-list" id="bedsFilter">
        <label class="check-item"><input type="checkbox" value="0" onchange="filterCards()"> Studio</label>
        <label class="check-item"><input type="checkbox" value="1" onchange="filterCards()"> 1 bed</label>
        <label class="check-item"><input type="checkbox" value="2" onchange="filterCards()"> 2 beds</label>
        <label class="check-item"><input type="checkbox" value="3" onchange="filterCards()"> 3+ beds</label>
      </div>
    </div>

    <div class="sb-section">
      <span class="sb-title">Source</span>
      <div class="src-list" id="sourcePills"></div>
    </div>

    <div class="sb-section">
      <span class="sb-title">Sort</span>
      <select class="sb-select" onchange="sortCards(this.value)">
        <option value="price">Price: low to high</option>
        <option value="price_desc">Price: high to low</option>
        <option value="metro">Metro distance</option>
        <option value="commute">Commute time</option>
        <option value="rating">Google rating</option>
      </select>
    </div>

    <hr class="sb-divider">

    <div class="sb-meta">
      <div><strong>Location</strong> {location}, {state}</div>
      {metro_line}
      {work_line}
      <div><strong>Budget</strong> ${min_price} – ${max_price}/mo</div>
      {beds_line}
    </div>
  </aside>

  <main>
    <div class="stats-bar" id="statsBar"></div>

    <div class="toolbar">
      <span class="toolbar-left" id="toolbarLabel"></span>
      <div class="view-btns">
        <button class="v-btn on" onclick="setView('grid',this)">Grid</button>
        <button class="v-btn" onclick="setView('list',this)">List</button>
      </div>
    </div>

    <div class="grid-wrap">
      <div class="grid" id="cardsGrid"></div>
    </div>
  </main>
</div>

<!-- Modal -->
<div class="overlay" id="overlay" onclick="closeModal(event)">
  <div class="modal" id="modal">
    <button class="m-close" onclick="closeModal()">&#x2715;</button>
    <div id="modalContent"></div>
  </div>
</div>

<script>
const DATA = {data_json};
let filtered = [...DATA];

function stars(rating, max=5) {{
  if (!rating) return '';
  const full = Math.round(rating);
  return '★'.repeat(full) + '☆'.repeat(max - full);
}}

function updatePrice(v) {{
  document.getElementById('priceVal').textContent = '$' + Number(v).toLocaleString();
}}

function filterCards() {{
  const q   = document.getElementById('searchInput').value.toLowerCase();
  const maxP = +document.getElementById('priceFilter').value;
  const beds = [...document.querySelectorAll('#bedsFilter input:checked')].map(x=>x.value);
  const srcs = [...document.querySelectorAll('.src-tag.on')].map(x=>x.dataset.s);

  filtered = DATA.filter(a => {{
    if (q && !((a.name||'').toLowerCase().includes(q)) && !((a.address||'').toLowerCase().includes(q))) return false;
    if (a.price && a.price > maxP) return false;
    if (beds.length) {{
      const b = a.beds == null ? null : (a.beds >= 3 ? '3' : String(a.beds));
      if (b !== null && !beds.includes(b)) return false;
    }}
    if (srcs.length && !srcs.includes(a.source)) return false;
    return true;
  }});
  renderCards();
}}

function sortCards(by) {{
  filtered.sort((a,b) => {{
    if (by==='price')    return (a.price||99999)-(b.price||99999);
    if (by==='price_desc') return (b.price||0)-(a.price||0);
    if (by==='metro')    return (a.metro_walk_mins||999)-(b.metro_walk_mins||999);
    if (by==='commute')  return (a.drive_mins||999)-(b.drive_mins||999);
    if (by==='rating')   return (b.google_rating||0)-(a.google_rating||0);
    return 0;
  }});
  renderCards();
}}

function setView(v, btn) {{
  const g = document.getElementById('cardsGrid');
  g.className = v==='list' ? 'grid list' : 'grid';
  document.querySelectorAll('.v-btn').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
}}

function renderCards() {{
  const grid = document.getElementById('cardsGrid');
  document.getElementById('countBadge').textContent = filtered.length;
  document.getElementById('toolbarLabel').textContent = filtered.length + ' listing' + (filtered.length!==1?'s':'');

  if (!filtered.length) {{
    grid.innerHTML = '<div class="empty"><strong>No listings match your filters</strong><p>Try adjusting the price or bedroom filters.</p></div>';
    renderStats([]);
    return;
  }}
  grid.innerHTML = filtered.map((a,i)=>cardHTML(a,i)).join('');
  renderStats(filtered);
}}

function revBadges(a) {{
  const g = a.google_rating
    ? `<span class="rev-badge google">
         <span class="brand">Google</span>
         <span class="stars">${{stars(a.google_rating)}}</span>
         <span class="score">${{a.google_rating}}</span>
         ${{a.google_count ? `<span class="ct">(${{a.google_count.toLocaleString()}})</span>` : ''}}
       </span>` : '';
  const y = a.yelp_rating
    ? `<span class="rev-badge yelp">
         <span class="brand">Yelp</span>
         <span class="stars">${{stars(a.yelp_rating)}}</span>
         <span class="score">${{a.yelp_rating}}</span>
         ${{a.yelp_count ? `<span class="ct">(${{a.yelp_count.toLocaleString()}})</span>` : ''}}
       </span>` : '';
  if (!g && !y) return '';
  return `<div class="card-reviews">${{g}}${{g && y ? '<span class="rev-sep">·</span>' : ''}}${{y}}</div>`;
}}

function cardHTML(a, idx) {{
  const hasImg = !!a.image;
  const photo = hasImg
    ? `<img src="${{a.image}}" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
    : '';
  const photoNone = `<div class="card-photo-none" style="${{hasImg?'display:none':'display:flex'}}">No photo</div>`;

  const beds = a.beds != null ? (a.beds===0?'Studio':a.beds+' bed') : null;
  const chips = [beds, a.sqft?a.sqft.toLocaleString()+' sqft':null].filter(Boolean)
    .map(c=>`<span class="chip">${{c}}</span>`).join('');

  const metroWalk  = a.metro_walk_mins  != null ? `<div class="dist-row"><span class="dist-label">Metro walk</span><span class="dist-val">${{a.metro_walk_mins}} min&thinsp;·&thinsp;${{a.metro_walk_miles}} mi</span></div>` : '';
  const metroDrive = a.metro_drive_mins != null ? `<div class="dist-row"><span class="dist-label">Metro drive</span><span class="dist-val">${{a.metro_drive_mins}} min&thinsp;·&thinsp;${{a.metro_drive_miles}} mi</span></div>` : '';
  const drive      = a.drive_mins       != null ? `<div class="dist-row"><span class="dist-label">Work drive</span><span class="dist-val">${{a.drive_mins}} min&thinsp;·&thinsp;${{a.drive_miles}} mi</span></div>` : '';

  return `<div class="card" onclick="openModal(${{idx}})">
    <div class="card-photo">${{photo}}${{photoNone}}<span class="card-source">${{a.source}}</span></div>
    <div class="card-body">
      <div class="card-name">${{a.name||'Unnamed Listing'}}</div>
      <div class="card-addr">${{a.address||'—'}}</div>
      <div class="card-price">
        ${{a.price ? '$'+a.price.toLocaleString() : ''}}
        ${{a.price ? '<span class="mo">/mo</span>' : `<span class="contact">${{a.price_note||'Contact for price'}}</span>`}}
      </div>
      ${{chips ? `<div class="card-chips">${{chips}}</div>` : ''}}
      ${{revBadges(a)}}
      ${{(metroWalk||metroDrive||drive) ? `<div class="card-dist">${{metroWalk}}${{metroDrive}}${{drive}}</div>` : ''}}
    </div>
    <div class="card-link">
      ${{a.url ? `<a href="${{a.url}}" target="_blank" onclick="event.stopPropagation()">View listing</a>` : '<span></span>'}}
      <span style="font-size:.7rem;color:var(--text3)">${{a.source}}</span>
    </div>
  </div>`;
}}

function renderStats(apts) {{
  const prices = apts.map(a=>a.price).filter(p=>p>0);
  const lo  = prices.length ? Math.min(...prices) : null;
  const hi  = prices.length ? Math.max(...prices) : null;

  const s = (n, l) => `<div class="stat"><div class="stat-n">${{n}}</div><div class="stat-l">${{l}}</div></div>`;
  document.getElementById('statsBar').innerHTML = [
    s(apts.length, 'Listings'),
    lo ? s('$'+lo.toLocaleString(), 'Lowest')  : '',
    hi ? s('$'+hi.toLocaleString(), 'Highest') : '',
  ].join('');
}}

function openModal(idx) {{
  const a = filtered[idx];
  const imgs = (a.images && a.images.length) ? a.images : (a.image ? [a.image] : []);
  const photo = imgs.length
    ? `<div class="m-gallery">${{imgs.map(u=>`<img class="m-photo" src="${{u}}" alt="" loading="lazy" onerror="this.remove()">`).join('')}}</div>`
      + (imgs.length>1 ? `<div class="m-count">${{imgs.length}} photos · swipe →</div>` : '')
    : '<div class="m-photo-none">No photo available</div>';

  // Parking: prefer what the listing actually states (free / a real price), else fall
  // back to the city-tier estimate and clearly label it as an estimate.
  const parkCost  = a.parking_free ? 0 : (a.parking_price != null ? a.parking_price : (a.parking_est||0));
  const parking   = a.parking_free ? 'Free'
                    : (a.parking_price != null ? '$'+a.parking_price.toLocaleString()+'/mo'
                    : (a.parking_est ? '~$'+a.parking_est+'/mo (est.)' : '—'));
  const total     = (a.price||0) + parkCost;
  const petsTxt   = (() => {{
    const p = [];
    if (a.pets_dogs===true) p.push('Dogs ✓'); else if (a.pets_dogs===false) p.push('Dogs ✗');
    if (a.pets_cats===true) p.push('Cats ✓'); else if (a.pets_cats===false) p.push('Cats ✗');
    return p.length ? p.join(' · ') : 'Not stated';
  }})();
  const transit   = a.transit_time ? `${{a.transit_distance}} · ${{a.transit_time}}` : (a.drive_mins ? `~${{a.drive_mins}} min drive` : '—');
  const prosHTML  = (a.pros||[]).length ? (a.pros||[]).map(p=>`<li>${{p}}</li>`).join('') : '<li style="color:var(--text3)">None noted</li>';
  const consHTML  = (a.cons||[]).length ? (a.cons||[]).map(c=>`<li>${{c}}</li>`).join('') : '<li style="color:var(--text3)">None noted</li>';
  const amenHTML  = (a.amenities||[]).length
    ? `<div class="m-sec">
         <div class="m-sec-title">Amenities</div>
         <div class="m-chips">${{(a.amenities||[]).map(x=>`<span class="m-chip">${{x}}</span>`).join('')}}</div>
       </div>` : '';

  const gRev = a.google_rating
    ? `<a class="m-rev-row" href="${{a.google_url||'#'}}" target="_blank" rel="noopener">
         <span class="brand">Google</span>
         <span class="stars g-stars">${{stars(a.google_rating)}}</span>
         <span class="score">${{a.google_rating}}</span>
         ${{a.google_count ? `<span class="ct">(${{a.google_count.toLocaleString()}} reviews)</span>` : ''}}
       </a>` : '';
  const yRev = a.yelp_rating
    ? `<a class="m-rev-row" href="${{a.yelp_url||'#'}}" target="_blank" rel="noopener">
         <span class="brand">Yelp</span>
         <span class="stars y-stars">${{stars(a.yelp_rating)}}</span>
         <span class="score">${{a.yelp_rating}}</span>
         ${{a.yelp_count ? `<span class="ct">(${{a.yelp_count.toLocaleString()}} reviews)</span>` : ''}}
       </a>` : '';

  document.getElementById('modalContent').innerHTML = `
    ${{photo}}
    <div class="m-body">
      <div class="m-source">${{a.source}}</div>
      <div class="m-name">${{a.name||'Unnamed Listing'}}</div>
      <div class="m-addr">${{a.address||'—'}}</div>
      <div class="m-top">
        <div class="m-price">
          ${{a.price ? '$'+a.price.toLocaleString()+'<span class="mo"> /mo</span>' : (a.price_note||'—')}}
        </div>
        ${{(gRev||yRev) ? `<div class="m-reviews-block">${{gRev}}${{yRev}}</div>` : ''}}
      </div>

      <div class="m-sec">
        <div class="m-sec-title">Details</div>
        <div class="m-grid">
          <div class="m-cell"><div class="lbl">Bedrooms</div><div class="val">${{a.beds!=null?(a.beds===0?'Studio':a.beds+' bed'):'—'}}</div></div>
          <div class="m-cell"><div class="lbl">Size</div><div class="val">${{a.sqft?a.sqft.toLocaleString()+' sqft':'—'}}</div></div>
          <div class="m-cell"><div class="lbl">Parking</div><div class="val">${{parking}}</div></div>
          <div class="m-cell"><div class="lbl">Pets</div><div class="val">${{petsTxt}}</div></div>
          <div class="m-cell"><div class="lbl">Source</div><div class="val">${{a.source}}</div></div>
        </div>
      </div>

      ${{amenHTML}}

      <div class="m-sec">
        <div class="m-sec-title">Monthly costs</div>
        <div class="cost-rows">
          <div class="cost-row"><span>Rent</span><span>${{a.price?'$'+a.price.toLocaleString():'—'}}</span></div>
          <div class="cost-row"><span>Parking</span><span>${{parking}}</span></div>
          <div class="cost-row"><span>Estimated total</span><span>${{total?'$'+total.toLocaleString():'—'}}</span></div>
        </div>
      </div>

      <div class="m-sec">
        <div class="m-sec-title">Getting around</div>
        <div class="m-grid">
          <div class="m-cell"><div class="lbl">Metro — walk</div><div class="val">${{a.metro_walk_mins!=null?a.metro_walk_mins+' min · '+a.metro_walk_miles+' mi':'—'}}</div></div>
          <div class="m-cell"><div class="lbl">Metro — drive</div><div class="val">${{a.metro_drive_mins!=null?a.metro_drive_mins+' min · '+a.metro_drive_miles+' mi':'—'}}</div></div>
          <div class="m-cell"><div class="lbl">Work — drive</div><div class="val">${{a.drive_miles?a.drive_miles+' mi · '+a.drive_mins+' min':'—'}}</div></div>
          <div class="m-cell"><div class="lbl">Work — transit</div><div class="val">${{transit}}</div></div>
        </div>
      </div>

      <div class="m-sec">
        <div class="m-sec-title">Pros & cons</div>
        <div class="pc-grid">
          <div class="pc-col pros"><h4>Pros</h4><ul>${{prosHTML}}</ul></div>
          <div class="pc-col cons"><h4>Cons</h4><ul>${{consHTML}}</ul></div>
        </div>
      </div>

      ${{a.url ? `<a href="${{a.url}}" target="_blank" class="m-link">View full listing</a>` : ''}}
    </div>`;
  document.getElementById('overlay').classList.add('open');
  document.body.style.overflow = 'hidden';
}}

function closeModal(e) {{
  if (e && e.target!==document.getElementById('overlay') && !e.target.closest('.m-close')) return;
  document.getElementById('overlay').classList.remove('open');
  document.body.style.overflow='';
}}
document.addEventListener('keydown', e => e.key==='Escape' && closeModal());

function toggleTheme() {{
  const h = document.documentElement;
  h.dataset.theme = h.dataset.theme==='dark' ? 'light' : 'dark';
}}

function initPills() {{
  const sources = [...new Set(DATA.map(a=>a.source))];
  document.getElementById('sourcePills').innerHTML = sources.map(s=>
    `<span class="src-tag on" data-s="${{s}}" onclick="this.classList.toggle('on');filterCards()">${{s}}</span>`
  ).join('');
}}

initPills();
filterCards();
</script>
</body>
</html>
"""

def generate_html(apartments, criteria):
    min_p = criteria.get("min_price", 0) or 0
    max_p = criteria.get("max_price", 5000) or 5000
    max_cap = max(max_p + 500, max_p)

    metro_line = f'<div><strong style="color:var(--text)">Metro:</strong> {criteria["metro_station"]}</div>' if criteria.get("metro_station") else ""
    work_line  = f'<div><strong style="color:var(--text)">Work:</strong> {criteria["work_address"]}</div>' if criteria.get("work_address") else ""
    beds_v = criteria.get("bedrooms")
    beds_line  = f'<div><strong style="color:var(--text)">Beds:</strong> {beds_v if beds_v is not None else "Any"}</div>'

    html = HTML_TEMPLATE.format(
        location=criteria["location"],
        state=criteria["state"],
        date=datetime.now().strftime("%B %d, %Y"),
        min_price=min_p,
        max_price=max_p,
        max_price_cap=max_cap,
        metro_line=metro_line,
        work_line=work_line,
        beds_line=beds_line,
        data_json=json.dumps(apartments, default=str),
    )
    # Each run gets its own folder under <project>/data/ (cross-platform).
    rd = make_run_dir(criteria)
    out = rd / f"apartments_{_slug(criteria['location'])}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    out.write_text(html, encoding="utf-8")

    # Archive the run's inputs/metadata alongside the report so the folder is self-contained:
    # the bookmarklet dumps that fed it (if any) and a small run.json.
    try:
        for resolver, used in ((_newest_zillow_dump, criteria.get("zillow_json")),
                               (_newest_apartments_dump, criteria.get("apartments_json"))):
            if used:
                src = resolver()
                if src and os.path.isfile(src):
                    shutil.copy2(src, rd / os.path.basename(src))
        (rd / "run.json").write_text(json.dumps({
            "generated": datetime.now().isoformat(timespec="seconds"),
            "criteria": {k: criteria.get(k) for k in
                         ("location", "state", "bedrooms", "min_price", "max_price",
                          "radius", "metro_station", "work_address")},
            "listing_count": len(apartments),
            "sources": sorted({a.get("source") for a in apartments if a.get("source")}),
        }, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  (run-folder archive warning: {str(e).splitlines()[0]})")
    return out

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "═"*60)
    print("  APARTMENT FINDER")
    print("═"*60)

    args = parse_args()
    criteria = get_criteria(args)

    # Resolve metro and work coordinates
    print("\nResolving locations...")
    metro_coords = None
    work_coords  = None
    if criteria.get("metro_station"):
        metro_coords = find_metro_coords(criteria["metro_station"], f"{criteria['location']}, {criteria['state']}")
        print(f"  Metro station: {metro_coords}")
    if criteria.get("work_address"):
        work_coords = geocode(criteria["work_address"])
        print(f"  Work address:  {work_coords}")

    # Search
    apartments = []
    driver = None
    max_n = criteria.get("max_listings", 40)

    print("\nSearching listings...")
    if SELENIUM:
        print("  Starting browser...")
        driver = make_driver(headless=criteria.get("headless", False),
                             preference=criteria.get("browser", "auto"),
                             attach=criteria.get("attach", False),
                             attach_port=criteria.get("attach_port"))

    if driver is not None:
        try:
            apartments += scrape_craigslist(criteria, driver, max_results=max_n)
            if criteria.get("zillow_json"):
                apartments += scrape_zillow_from_json(criteria, criteria["zillow_json"])
            elif criteria.get("zillow_html"):
                apartments += scrape_zillow_from_file(criteria, criteria["zillow_html"])
            elif criteria.get("attach"):
                apartments += scrape_zillow_via_driver(criteria, driver)
            else:
                apartments += scrape_zillow_api(criteria)
            if criteria.get("apartments_json"):
                apartments += scrape_apartments_from_json(criteria, criteria["apartments_json"])
            else:
                apartments += scrape_apartments_com(criteria, driver)
            if criteria.get("sites"):
                apartments += scrape_local_sites(criteria, driver)
            # HotPads removed: it's Zillow-owned (inventory overlaps the Zillow bookmarklet)
            # and reliably throws its own Press & Hold check, wasting ~25s for 0 results.
        except Exception as e:
            print(f"  Browser search error: {e}")
        finally:
            # Never close the user's own browser when we attached to it.
            if not getattr(driver, "_apt_attached", False):
                try:
                    driver.quit()
                except Exception:
                    pass
    else:
        print("  No browser available — cannot search JS-rendered sites.")

    print(f"\nTotal raw listings: {len(apartments)}")
    apartments = deduplicate(apartments)
    print(f"After deduplication: {len(apartments)} listings")

    if not apartments:
        print("\nNo listings found. Try a wider radius, a different ZIP, or check your connection.")
        print("Generating an empty report shell anyway...")

    # Enrich
    if apartments:
        apartments = enrich_apartments(apartments, criteria, metro_coords, work_coords)
        apartments.sort(key=lambda a: a["price"] or 999999)

        # Report link-validation results (a completion checkpoint the Stop hook verifies)
        if criteria.get("check_links", True):
            checked = [a for a in apartments if a.get("link_ok") is not None]
            reachable = [a for a in checked if a.get("link_ok")]
            if checked:
                print(f"[OK] Link check: {len(reachable)}/{len(checked)} listing links reachable")
            else:
                print("[OK] Link check: no checkable links found")

    # Generate HTML
    print("\nGenerating HTML report...")
    html_path = generate_html(apartments, criteria)
    print(f"\n[OK] Report saved: {html_path}")
    print(f"     {len(apartments)} listings")
    if not args.no_open:
        print("\nOpening in browser...")
        webbrowser.open(html_path.as_uri())

if __name__ == "__main__":
    main()
