# Sydney Motorsport Park → Outlook Calendar

This repository generates a subscribable `.ics` calendar from the public Sydney Motorsport Park event calendar:

https://events.sydneymotorsportpark.com.au/eventcalendar/s/

## How it works

1. GitHub Actions runs the scraper daily.
2. Playwright opens the dynamically rendered SMSP calendar.
3. The scraper extracts event data from structured data and rendered event links.
4. It writes:
   * `docs/calendar.ics`
   * `docs/events.json`
   * `docs/debug.json`
5. GitHub Pages can serve `docs/calendar.ics` as a stable Outlook subscription URL.

## Run manually

Open **Actions → Update Sydney Motorsport Park calendar → Run workflow**.

The workflow can also be run automatically once per day.

## Enable GitHub Pages

In this repository open:

**Settings → Pages**

Under **Build and deployment** choose:

* **Source:** Deploy from a branch
* **Branch:** `main`
* **Folder:** `/docs`

Save the setting.

The calendar feed should then be available at:

`https://akumarau.github.io/smsp-calendar/calendar.ics`

## Add to Outlook

In Outlook:

**Calendar → Add calendar → Subscribe from web**

Paste:

`https://akumarau.github.io/smsp-calendar/calendar.ics`

Name it **Sydney Motorsport Park** and import it.

## Diagnostics

If the scraper fails to discover events, inspect:

`docs/debug.json`

It records a preview of the rendered page, candidate event links, and a limited sample of JSON responses captured while the page loads. This is intentionally stored to make changes to the SMSP site easier to diagnose.

## Notes

* The calendar uses `Australia/Sydney` for timed events.
* Stable event UIDs are generated from event URLs so normal updates do not create duplicates.
* All day events use date values.
* The generated feed is refreshed daily by GitHub Actions.
