# SEO Preview Tool

**Live: https://seo-preview-tool.pages.dev**

A free web tool: paste a URL, and it shows how the page looks in Google search results,
previews its social share card, and flags common SEO problems.

Personal learning project — the goal is to learn to build AND deploy end to end.

## Deploy

Cloudflare Pages is connected to this repo, so **pushing to `main` deploys automatically**.
No deploy command needed.

Build settings (Cloudflare dashboard): framework preset None, build command empty,
output directory `public`. There is nothing to compile — the page is plain HTML and
Pages bundles `functions/` on its own.

`wrangler.toml` pins `compatibility_date` so production runs the same runtime as local,
and sets `pages_build_output_dir = "public"` so only `public/` is published — `audit.py`,
the CSVs and this README are never served.

Note: a Pages project cannot be converted between Direct Upload and Git-connected.
This project started as Direct Upload and had to be deleted and recreated to connect
Git, which is why the repo is the source of truth.

## Security notes

`/api/fetch` is public and fetches arbitrary URLs, which makes it an SSRF surface.
`isBlockedHost()` refuses loopback, RFC1918 private ranges, link-local (including the
169.254.169.254 cloud metadata address), CGNAT, IPv6 unique-local/link-local, and
`.local`/`.internal`-style hostnames. There is deliberately no auth or rate limiting:
a public SEO tool is open by design, and Cloudflare's free tier absorbs normal abuse.
Revisit if it ever gets real traffic.

## Stack
- Frontend: plain HTML + JavaScript (no framework)
- Hosting: Cloudflare Pages + Pages Functions
- The page is fetched server-side (in a Pages Function) because browser CORS blocks
  cross-origin fetches.

## Project layout
```
index.html            The page you see in the browser (input box + result)
functions/api/fetch.js  Serverless function at /api/fetch that fetches the target page
```

## Run locally
Requires Node.js (already installed). Uses Wrangler, Cloudflare's local dev tool.

```
npx wrangler pages dev .
```
Then open the local URL it prints (usually http://localhost:8788).

## Batch audit

`audit.py` runs the tool against ~55 large S&P 500 sites and reports every title,
description and social tag it finds, plus the verdicts from the real measurement
code (run in headless Chrome, since verdicts need genuine text layout).

```
npx wrangler pages dev . --port 8788    # terminal 1
python3 audit.py --batch 1              # 55 mega caps
python3 audit.py --batch 2              # 109 more, every sector
python3 audit.py --batch all            # all 164
```

Writes `audit-results-batch<N>.csv` with the full text of every title and description.

Point it at production to audit the deployed tool instead of the dev server:

```
python3 audit.py --batch all --api https://seo-preview-tool.pages.dev/api/fetch
```

### Results across 164 large companies

|                          | local | production |
|--------------------------|-------|------------|
| readable                 | 108   | 112        |
| unreadable               | 34%   | 32%        |

Barely different in total, but the *composition* changes a lot. Microsoft, Cisco, Intel
and Abbott are readable only from production; Alphabet, Bank of America, AbbVie and
Blackstone only from a laptop. Sites block datacenter IPs and residential IPs by
different rules, so neither vantage point is authoritative — roughly a third fail
either way.

Among the 112 readable in production, titles and descriptions were largely fine
(1 missing title, 8% over-long) but social tags were not: **36 of 112 (32%) had no
`og:image`** and 15 had no social tags at all.

It does NOT compare against Google's live results — scraping Google's SERP breaks
their terms and gets blocked. Instead it flags the sites where Google is most
likely to differ (description missing or too long) so those few can be checked by hand.

Bot-blocking is not fully deterministic: Intuit was readable on one run and blocked
on the next, so counts move by one or two between runs.

## Milestones
- [x] M0: input box -> serverless fetch -> show the page <title>
- [x] M1: render a realistic Google search result preview
- [x] M2: pixel-width truncation warnings (canvas measureText, word-boundary cut)
- [x] M3: social share card preview (Open Graph / Twitter) + tag checklist
- [x] Deploy to a real URL on Cloudflare Pages
- [x] Disclaimer that Google rewrites descriptions ~70% of the time
- [x] M4: health check (noindex, title, og:image, H1, image alt, viewport,
      canonical, lang)
- [x] Pass its own health check. Pasting this tool's URL into it used to report no
      description, no og:image and no social tags — failing the check it grades
      hardest. `public/og-image.png` is rendered from `tools/og-image.html` with
      headless Chrome, so it can be edited as HTML and regenerated with:

      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
        --headless --window-size=1200,630 \
        --screenshot=public/og-image.png tools/og-image.html

- [x] Accessibility: labelled input, live-region announcements, focus outlines

### Not done

- The 15 flagged sites have not been checked against Google by hand.
- No automated tests. `audit.py` is a harness, not a test suite: it reports what it
  finds rather than asserting expected values, so nothing fails if behaviour regresses.
- Only the `<head>` and a few body signals are parsed. No structured data (JSON-LD),
  hreflang, robots.txt or sitemap checks.
- Regex is used to parse HTML. It has held up across 164 sites but Cloudflare's
  `HTMLRewriter` would be the correct tool.

## Health check severity

Grades are deliberately uneven. Marking everything red is how a health check becomes
noise people ignore — a mistake earlier versions of this tool made repeatedly.

- `fail` — a searcher or sharer demonstrably loses something: noindex, no title, no og:image
- `warn` — worth fixing, but degrades gracefully: no meta description, no H1,
   images without alt, no viewport
- `info` — a difference, not a defect: multiple H1s (valid HTML5), no canonical, no lang

A missing meta description is only a `warn` on purpose: Google rewrites descriptions
~70% of the time regardless, so its absence often changes nothing there. Social
platforms do use it verbatim, which is why it isn't merely `info`.

`noindex` overrides the summary entirely — a page excluded from the index makes every
other finding moot.

## Production behaves differently from local

Some sites that block a residential IP serve Cloudflare's edge fine. microsoft.com is
the clearest case: locally it returns a "Your request has been blocked" page, but from
production it returns its real title, description and tags. So the 34% unreadable rate
in the audit numbers above was measured locally and is probably pessimistic — point
`audit.py` at the deployed URL to get truer numbers.

## Known limitations (by design, don't rabbit-hole)
- Sites behind bot protection won't return their HTML. We detect this and say so
  instead of falsely reporting missing tags. Two distinct failure modes seen:
    - HTTP 403 (e.g. adidas.com) — the site replies but refuses us.
    - Connection dropped (e.g. newbalance.com) — Akamai kills it below HTTP, which
      surfaces in the Workers runtime as an opaque "internal error". Indistinguishable
      from a nonexistent domain from inside the function, so the error names both causes.
    - Deliberate slow-walking (e.g. bestbuy.com) — returns a valid 200, but takes
      8-11s to send the first byte. Connect + TLS are ~55ms, so the delay is the
      server thinking, not the network. This is why the timeout is 12s, not 8s.
  Not a category thing: nike.com, walmart.com, target.com, bbc.com all work fine.
  It depends on how each site configures its bot protection.

- Do NOT try to defeat bot protection by spoofing a browser User-Agent. Tested on
  bestbuy.com: an honest bot UA gets served (slowly), while a Chrome UA gets the
  connection killed outright on both HTTP/2 and HTTP/1.1 — the UA claims Chrome but
  the TLS fingerprint says otherwise, and that mismatch is itself a detection signal.
  Spoofing harder makes results worse, not better.
- JS-only pages that inject tags client-side won't be readable by a plain fetch.
- Truncation is MEASURED, not predicted. Earlier versions used canvas pixel math
  against a guessed ~920px description limit, which was wrong: Stripe (924px) and
  Best Buy (~950px) both display in full on Google, while Robinhood (1064px) and
  GitHub (1183px) get cut. A single pixel budget is the wrong model — Google wraps
  the description in a ~600px column and clamps it to ~2 lines (~3 on mobile).
  We now render into that same box with a CSS line clamp and ask the browser whether
  it overflowed. Verified against all 4 sites above plus 2 titles: 6/6 agreement.
- Measurement happens in OFF-SCREEN reference boxes (`#measure-title`, `#measure-desc`)
  pinned to Google's ~600px column with an explicit Arial font. Two bugs made this
  necessary, both from measuring inside the visible, responsive preview instead:
    - font-family was inherited (system-ui), so the same title measured 550px on one
      platform and 568px on another, flipping the verdict at the band boundary.
    - the visible box shrinks with the window, so at a 600px window it reported
      GitHub's title as truncated purely because the browser was narrow.
  Verified stable: 7 known strings x 4 viewport widths = 28/28 agreement with Google.
- Still approximate: the 600px column width and 2-line clamp vary by device, and
  Google rewrites descriptions ~70% of the time regardless of length.
```
