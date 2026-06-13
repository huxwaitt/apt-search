---
name: apartment-search
description: This skill should be used when the user says "/apartment-search", "apt-search", "search for apartments", "find apartments", "find rentals", "run an apartment search", or asks to generate an apartment report.
version: 1.1.0
---

# Apartment Search Skill

Searches Craigslist (and best-effort Zillow / Apartments.com / HotPads) in a real browser,
adds metro-walk + work-commute distances, and generates an interactive HTML report.

The user triggers the skill; **Claude runs the script for them — the user never runs Python.**

## What to do when invoked

Run ONE command, then watch for a CAPTCHA prompt. That's it.

### Step 1 — Run the script (from this skill's directory, in the background)

```
python apartment_search.py <flags>
```

- Run it with `run_in_background: true` (it opens a browser and can take a few minutes).
- **Never pass `--headless`** — the browser must be visible so the user can solve any check.
- **No flags at all** = the user's saved search (from their private `config.local.json`,
  falling back to generic Washington, DC if absent). If the user didn't specify anything,
  run it with no flags.

### Step 2 — Map the user's words to flags (only for what they mention)

| User says | Flag |
|-----------|------|
| a city / neighborhood | `--location "Austin"` |
| a state | `--state TX` |
| "under $X" / "max $X" | `--max-price X` |
| "at least $X" | `--min-price X` |
| "N bedroom(s)" / "studio" | `--bedrooms N` (studio = 0; any = -1) |
| "within N miles" | `--radius N` |
| "near <station>" | `--metro-station "<station>"` |
| "commute to <place>" | `--work-address "<place>"` |

Anything not mentioned uses the saved defaults — do not ask the user for it.

### Step 3 — Handle the CAPTCHA (likely on Zillow/Apartments/HotPads)

While the command runs, watch its output. If you see an `ACTION NEEDED` box or
`waiting for you to clear the check`, tell the user:

> "A site is showing a 'Press & Hold' check — please complete it in the browser window
> that opened. The search will continue automatically."

Then keep waiting for the command to finish. Craigslist never needs a CAPTCHA, so the
report always produces results even if the user ignores the check.

### Step 4 — Report the result

When the command finishes it prints `[OK] Report saved: <path>` and opens the report
automatically. Tell the user the report opened and how many listings were found.

## Setup (first run only)

```
pip install -r requirements.txt
cp config.example.json config.local.json   # the user's saved search (stays private/gitignored)
```

## Notes

- Dependencies (`requests`, `beautifulsoup4`, `selenium`) auto-install on first run.
- The browser is auto-detected: Chrome → Edge → Firefox. To force one: `--browser firefox`.
- Reports save to the user's **Downloads** folder as `apartments_<city>_<timestamp>.html`.
- The HTML design lives in the `HTML_TEMPLATE` string inside `apartment_search.py`; see
  `docs/sample.html` for a rendered showcase with demo data.

## Reliability & CAPTCHA details

- **Craigslist is the dependable, no-CAPTCHA source.** It renders results with JavaScript,
  so the script drives a real browser and parses the rendered cards. Suburban towns map to
  their parent metro's Craigslist region (e.g. Northern-VA / suburban-MD → `washingtondc`);
  the script maps cities and states automatically.
- **Zillow / Apartments.com / HotPads** sit behind PerimeterX/Cloudflare and only return
  results when run with a visible browser and the user clears the one-time "Press & Hold"
  check. Headless runs skip them.
- The report always generates, even with zero results, so a run never ends without an artifact.

## Flags reference

| Flag | Purpose |
|------|---------|
| `--location` / `--state` | City and state (committed default: Washington / DC) |
| `--postal` | ZIP code to center the search on (improves accuracy) |
| `--min-price` / `--max-price` | Rent band ($/mo; default max $3,000) |
| `--bedrooms` | 0 = studio; `-1` for any |
| `--radius` | Search radius in miles (default 10) |
| `--metro-station` | Enables walk-time-to-metro calculation |
| `--work-address` | Enables driving-commute calculation |
| `--gmaps-key` | Optional Google Maps key for transit times |
| `--browser` | `auto` (default), `chrome`, `edge`, or `firefox` |
| `--headless` | No visible window (disables CAPTCHA solving — skips bot-walled sites) |
| `--reviews` | Also scrape Google/Yelp ratings (slow, often rate-limited) |
| `--no-open` | Don't auto-open the finished report |
| `--max-listings` | Cap listings per source (default 40) |

## Troubleshooting

- **0 results from Zillow/Apartments/HotPads**: the user didn't clear the "Press & Hold"
  check, or the site changed. Craigslist results still appear. Try `--browser firefox`.
- **`ERR_NAME_NOT_RESOLVED`**: the city has no Craigslist region; the script maps DC/VA/MD →
  washingtondc and other metros automatically — use a major nearby city if it persists.
- **Browser won't start**: ensure Chrome, Edge, or Firefox is installed. Selenium 4.6+
  resolves the driver automatically (no `webdriver-manager` needed).
