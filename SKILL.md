---
name: apartment-search
description: This skill should be used when the user says "/apartment-search", "apt-search", "search for apartments", "find apartments", "find rentals", "run an apartment search", or asks to generate an apartment report.
version: 1.2.0
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

**Exception — work address / commute:** if the user gave no `--work-address` AND none is set
in their `config.local.json`, do **not** assume a commute. Before running, ask whether they
want to provide a work address for commute times. If they decline, run without it (the report
omits commute). Don't invent or reuse an old work location.

### Step 3 — Zillow: attach to the user's own Chrome (don't fight the check)

Zillow's "Press & Hold" check reliably defeats the automated browser, so **don't rely on
it solving the check in the launched window.** Instead, attach to a Chrome the user has
already cleared. Before running the search (or right after, if a first run returns 0 from
Zillow), do this:

1. Ask the user to launch a debuggable browser and clear the check themselves. Tell them to
   type **one** of these in the session (the `!` runs it for them):

   **Chrome:**
   > `! & "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:TEMP\apt-chrome"`

   **Firefox:**
   > `! & "C:\Program Files\Mozilla Firefox\firefox.exe" -marionette -no-remote -profile "$env:TEMP\apt-firefox"`

   Then say: *"In that new window, go to **zillow.com**, clear any 'Press & Hold' check so
   listings show, and tell me when you're in."*

   > A separate profile dir is required either way — Chrome only opens the debug port (and
   > Firefox only listens on Marionette) for a fresh profile, so this won't conflict with
   > their normal browser.

2. **Wait for the user to confirm they're in.** Only after they say so, run the search with
   `--attach` — the script locks onto that browser and inherits their cleared session. For
   Firefox, add `--browser firefox`:

   ```
   python apartment_search.py <flags> --attach                    # Chrome (port 9222)
   python apartment_search.py <flags> --attach --browser firefox  # Firefox (Marionette 2828)
   ```

   The script never closes the user's browser. If it can't reach the port it warns and falls
   back to a fresh browser (Craigslist still works).

If the user doesn't want to bother with Zillow, just run without `--attach` — Craigslist
never needs a check, so the report always produces results.

### Step 4 — Report the result

A complete run prints **both** of these near the end:
- `[OK] Link check: X/Y listing links reachable` — every listing URL is validated; dead/expired
  ones are flagged with a con in the report.
- `[OK] Report saved: <path>` — and the report opens automatically.

Review the output: confirm the link check ran and note how many links were reachable, then tell
the user the report opened, how many listings were found, and the link-check result.

> A **Stop hook** (`check_complete.py`, registered in `~/.claude/settings.json`) enforces this —
> if the search ran but the report or link check didn't complete, it blocks returning to the user
> until the run finishes. Don't pass `--skip-link-check` unless the user asks.

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
- **Titles are normalized** in the report: marketing/spam titles ("Put a smile on your
  face!") are rewritten into a factual summary like `1-Bedroom apartment · 1 bath · 712 sqft
  · Falls Church`. The original title is kept in each listing's `raw_name`.
- **Attached-home detection**: listings that are a basement, in-law suite, single room, or
  shared house (not a standalone unit) are flagged with a con like "Basement unit attached to
  a home", and the title reflects it (e.g. `1-Bedroom basement apartment · Arlington`).
- **Pet policy** read per listing, **dogs and cats separately** (Details shows `Dogs ✓ · Cats ✗`;
  pros/cons added; unstated = "Not stated").
- **Parking** read per listing: "Free" when included, stated `$X/mo` when paid, else a city-tier
  estimate labeled "est.". The monthly-cost total uses the real figure when known.
- **Commute** is only computed when a work address is provided — never assumed (see Step 2).

## Reliability & CAPTCHA details

- **Craigslist is the dependable, no-CAPTCHA source.** It renders results with JavaScript,
  so the script drives a real browser and parses the rendered cards. Suburban towns map to
  their parent metro's Craigslist region (e.g. Northern-VA / suburban-MD → `washingtondc`);
  the script maps cities and states automatically.
- **Zillow / Apartments.com / HotPads** sit behind PerimeterX/Cloudflare. Zillow's check
  reliably beats the launched browser, so use `--attach` (Step 3) to lock onto a Chrome the
  user has already cleared. Headless runs skip these sites entirely.
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
| `--attach` | Attach to the user's own browser (Chrome via `--remote-debugging-port`, or Firefox via `-marionette` with `--browser firefox`) so a cleared Zillow check carries over; doesn't close their browser |
| `--attach-port` | Port to attach to (default 9222 for Chrome, 2828 for Firefox) |
| `--headless` | No visible window (disables CAPTCHA solving — skips bot-walled sites) |
| `--reviews` | Also scrape Google/Yelp ratings (slow, often rate-limited) |
| `--skip-link-check` | Skip validating that each listing URL actually resolves (on by default) |
| `--no-open` | Don't auto-open the finished report |
| `--max-listings` | Cap listings per source (default 40) |

## Troubleshooting

- **0 results from Zillow/Apartments/HotPads**: the user didn't clear the "Press & Hold"
  check, or the site changed. Craigslist results still appear. Try `--browser firefox`.
- **`ERR_NAME_NOT_RESOLVED`**: the city has no Craigslist region; the script maps DC/VA/MD →
  washingtondc and other metros automatically — use a major nearby city if it persists.
- **Browser won't start**: ensure Chrome, Edge, or Firefox is installed. Selenium 4.6+
  resolves the driver automatically (no `webdriver-manager` needed).
