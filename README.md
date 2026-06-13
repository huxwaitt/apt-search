# Apartment Search

A multi-platform apartment search that drives a real browser to scrape **Craigslist**
(plus Zillow / Apartments.com / HotPads), enriches each listing with metro-walk and
work-commute distances, and generates a self-contained, interactive HTML report.

It ships as a **Claude Code skill** (`SKILL.md`) — you trigger it in Claude and it runs the
script for you — but it also runs standalone from the command line.

## Quick start

```bash
pip install -r requirements.txt
cp config.example.json config.local.json   # fill in your usual search (stays private)
python apartment_search.py                  # bare run = your config.local.json defaults
```

Or override anything on the command line:

```bash
python apartment_search.py --location Chicago --state IL --max-price 2500 --bedrooms 1
```

A visible browser opens. If a site shows a **"Press & Hold"** human check, complete it in
the window — scraping resumes automatically. Run with `--headless` to skip those sites.

## Where things live

| Thing | Location |
|-------|----------|
| Search engine + **HTML template** | `apartment_search.py` (the `HTML_TEMPLATE` string) |
| Your personal defaults (private) | `config.local.json` (gitignored) |
| Generic committed defaults | the `D = {...}` block in `apartment_search.py` |
| Generated reports | your **Downloads** folder: `apartments_<city>_<timestamp>.html` |
| Skill definition | `SKILL.md` |

## Privacy & safety

This repo is built to be safe to push publicly:

- **`config.local.json` is gitignored** so your home base, ZIP, and commute never get
  committed. The committed defaults are generic (Washington, DC).
- **Generated reports (`*.html`) are gitignored** — they contain scraped listings and your
  commute details.
- A **pre-commit hook** (`scripts/pre-commit`) blocks accidental commits of private files,
  API keys / secrets, and oversized files. Enable it once per clone:

  ```bash
  git config core.hooksPath scripts
  ```

- API keys (e.g. Google Maps) are passed via `--gmaps-key` or `config.local.json`, never
  hardcoded.

## Installing as a Claude Code skill

Copy or symlink this folder into your skills directory:

```
~/.claude/skills/apartment-search/
```

Then trigger it in Claude with `/apartment-search` or "find me 2BRs in Chicago under $2,500".

## Notes on reliability

Craigslist is the dependable, no-CAPTCHA source. Zillow / Apartments.com / HotPads sit
behind PerimeterX/Cloudflare bot-walls and only return results when you run with a visible
browser and clear the one-time "Press & Hold" check. See `SKILL.md` for details.
