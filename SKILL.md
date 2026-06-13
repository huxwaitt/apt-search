---
name: apartment-search
description: This skill should be used when the user says "/apartment-search", "apt-search", "search for apartments", "find apartments", "find rentals", "run an apartment search", or asks to generate an apartment report.
version: 1.0.0
---

# Apartment Search Skill

Run a multi-platform apartment search (Craigslist, Zillow, Apartments.com, HotPads) and generate a beautiful interactive HTML report with commute distances, metro proximity, parking cost estimates, and pros/cons for each listing.

## When to invoke

Trigger when the user asks to:
- Search for apartments / find apartments
- Find rentals in a city
- Run an apartment search
- Generate an apartment report

## How Claude runs this

The user triggers the skill (e.g. "/apartment-search" or "find me 2BRs in Chicago under
$2,500"). **Claude runs the script for the user — the user never runs Python themselves.**

Steps for Claude:

1. **Parse the user's request into flags.** Map natural language to the flags below
   (city → `--location`, state → `--state`, "under $X" → `--max-price`, "2 bed" →
   `--bedrooms 2`, "near <station>" → `--metro-station`, "commute to <place>" →
   `--work-address`, etc.). Anything the user didn't specify falls back to the sensible
   defaults baked into the script, so a bare run is fine too.

2. **Run it visibly and in the background** (so the user can solve any CAPTCHA). The script
   sits in this skill's own folder, so run `apartment_search.py` from the skill directory:
   ```
   python apartment_search.py --location "Chicago" --state IL --max-price 2500 --bedrooms 2
   ```
   - Do **not** pass `--headless` — the browser must be visible for the "Press & Hold" check.
   - If a site shows the bot-check, tell the user to complete it in the browser window; the
     script resumes automatically and then opens the finished report.
   - A bare `python apartment_search.py` reproduces the user's usual search from
     `config.local.json` (their private defaults; falls back to generic Washington, DC if absent).

   Full example with every option:
   ```
   python apartment_search.py --location "Chicago" --state IL --postal 60614 \
     --bedrooms 2 --max-price 2500 --radius 8 \
     --metro-station "Fullerton" --browser firefox
   ```

   Useful flags:
   | Flag | Purpose |
   |------|---------|
   | `--location` / `--state` | City and state (committed default: Washington / DC) |
   | `--postal` | ZIP code to center the search on (improves accuracy) |
   | `--min-price` / `--max-price` | Rent band ($/mo; default max $2,500) |
   | `--bedrooms` | 0 = studio; `-1` for any (default 2) |
   | `--radius` | Search radius in miles (default 10) |
   | `--metro-station` | Enables walk-time-to-metro calculation |
   | `--work-address` | Enables driving-commute calculation |
   | `--gmaps-key` | Optional Google Maps key for transit times |
   | `--browser` | `auto` (default), `chrome`, `edge`, or `firefox` |
   | `--headless` | Run the browser with no visible window (disables CAPTCHA solving) |
   | `--reviews` | Also scrape Google/Yelp ratings (slow, often rate-limited) |
   | `--no-open` | Don't auto-open the finished report |
   | `--max-listings` | Cap listings per source (default 40) |

   The script will:
   - Auto-install required packages (`requests`, `beautifulsoup4`, `selenium`)
   - Launch a real browser (Chrome → Edge → Firefox) — Selenium 4.6+ resolves the matching
     driver automatically, so `webdriver-manager` is no longer needed
   - Scrape Craigslist from the rendered page (real prices, links, beds, sqft) — the most
     reliable source — plus Zillow, Apartments.com, and HotPads
   - **Pause for human CAPTCHA solving** on bot-walled sites (see below)
   - Geocode each listing via OpenStreetMap Nominatim (free, no key)
   - Compute walking distance to the metro and driving distance/time to work via OSRM (free)
   - Generate a self-contained interactive HTML report in Downloads and open it

**Output**: `apartments_<city>_<timestamp>.html` in the user's Downloads folder, opened automatically.

## Getting Zillow / Apartments.com / HotPads to work (CAPTCHA)

These sites sit behind PerimeterX/Cloudflare and show a **"Press & Hold" human check**
that defeats automated browsers. The script handles this with a **human-in-the-loop**:

- **Run with a visible browser** (the default — do **not** pass `--headless`).
- When a site shows the check, the script prints an `ACTION NEEDED` box and **waits** while
  you complete the "Press & Hold" in the browser window. It resumes automatically once
  listings load (up to ~240s).
- If a browser is bot-flagged repeatedly, try `--browser firefox` — a non-Chromium engine
  sometimes slips through, and you can still solve the check manually.
- In `--headless` mode the check can't be solved, so those sites are skipped fast and only
  Craigslist returns results.

## Notes on reliability

- **Craigslist is the dependable, no-CAPTCHA source.** It renders results with JavaScript,
  so the script drives a real browser and parses the rendered cards. Suburban towns map to
  their parent metro's Craigslist region (e.g. Northern-VA / suburban-MD towns resolve to
  `washingtondc`); the script maps cities and states automatically.
- **Zillow / Apartments.com / HotPads** now work *when run with a visible browser and the
  user solves the one-time "Press & Hold"* check. Headless runs skip them.
- The report always generates, even with zero results (an empty shell), so a run never
  ends without an artifact.

## Inputs collected from the user

| Field | Required | Notes |
|-------|----------|-------|
| City / neighborhood | ✅ | e.g. "Washington" or "Capitol Hill" |
| State | ✅ | e.g. "DC", "VA", "NY" |
| Min price ($/mo) | No | Default 0 |
| Max price ($/mo) | No | Default 5000 |
| Bedrooms | No | 0 = studio; blank = any |
| Size flexible | No | y/n |
| Nearest metro station | No | Enables walk-time calculation |
| Work address | No | Enables commute calculation |
| Google Maps API key | No | Enables transit time calculation |

## HTML report features

- **Grid / list view** toggle
- **Live filters**: price slider, bedrooms, source (Craigslist / Zillow / Apartments.com / HotPads)
- **Sort**: price low→high, high→low, metro distance, commute time
- **Search bar**: filter by name or address
- **Summary stats**: total listings, average rent, lowest rent, avg metro walk
- **Cards**: photo, price, beds, sqft, metro walk time, commute time, pros/cons tags
- **Detail modal** (click any card): full breakdown including monthly cost estimate (rent + parking), transit vs. driving commute, full pros/cons list, link to original listing
- **Dark / light mode** toggle
- **Print-ready** layout

## Dependencies

All installed automatically by the script on first run:
- `requests` — HTTP requests
- `beautifulsoup4` — HTML parsing
- `selenium` — Chrome automation for Zillow / Apartments.com / HotPads
- `webdriver-manager` — auto-downloads ChromeDriver

External services used (all free, no key required unless noted):
- OpenStreetMap Nominatim — geocoding
- OSRM routing API — driving distances
- Google Maps Distance Matrix API (optional) — transit times

## Troubleshooting

- **Zillow/Apartments.com show 0 results**: Anti-bot measures may have triggered. Run again with a short delay or try a different time. Craigslist results will still appear.
- **Browser/driver error**: Selenium 4.6+ auto-resolves the driver. Ensure Chrome or Edge is installed. Force one with `--browser edge` or `--browser chrome`.
- **`ERR_NAME_NOT_RESOLVED` on Craigslist**: the city didn't map to a valid Craigslist region. Use a major nearby city or a mapped state; the script already maps DC/VA/MD → `washingtondc`.
- **No listings found**: Check the city name. Use the Craigslist city name format (e.g. "Washington" for DC, "newyork" for NYC).
- **Geocoding fails**: OpenStreetMap Nominatim is rate-limited; the script already includes polite delays.
