// This is a Cloudflare Pages Function.
// The file path decides the URL: functions/api/fetch.js  ->  /api/fetch
//
// It runs on Cloudflare's servers, NOT in the browser. That matters because
// browsers block cross-origin fetches (CORS), but servers don't. So the browser
// asks this function to fetch the target page, and the function does it freely.

// Decode HTML entities so text displays as real characters.
//
// Handles numeric entities generically rather than case by case. The old version
// listed only a handful and missed hex forms, so AbbVie's title came back as
// "Pharmaceutical Research &#x26; Development" instead of "... & Development".
function decodeEntities(str) {
  if (!str) return str;

  const named = {
    amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ',
    ndash: '\u2013', mdash: '\u2014', hellip: '\u2026',
    lsquo: '\u2018', rsquo: '\u2019', ldquo: '\u201c', rdquo: '\u201d',
    trade: '\u2122', reg: '\u00ae', copy: '\u00a9', middot: '\u00b7',
  };

  return (
    str
      // &#x26;  (hex)  and  &#38;  (decimal)
      .replace(/&#x([0-9a-f]+);/gi, (_, hex) => safeCodePoint(parseInt(hex, 16)))
      .replace(/&#(\d+);/g, (_, dec) => safeCodePoint(parseInt(dec, 10)))
      // Named entities. Done last so a decoded "&amp;#38;" doesn't get re-decoded.
      .replace(/&([a-z][a-z0-9]*);/gi, (match, name) => {
        const found = named[name.toLowerCase()];
        return found === undefined ? match : found;
      })
  );
}

function safeCodePoint(code) {
  if (!Number.isFinite(code) || code < 0 || code > 0x10ffff) return '';
  try {
    return String.fromCodePoint(code);
  } catch {
    return '';
  }
}

// Run a regex against the HTML and return the decoded first capture group, or null.
function matchMeta(html, regex) {
  const m = html.match(regex);
  return m ? decodeEntities(m[1].trim()) : null;
}

// Look up a meta tag's content by its key, e.g. "description", "og:image".
// Real-world HTML varies, so we handle:
//   - key stored in  property="..."  (Open Graph style)  or  name="..."  (classic/Twitter)
//   - content= appearing either BEFORE or AFTER the key attribute
function getMeta(html, key) {
  // Escape ':' etc. so the key is safe inside a regex.
  const k = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const patterns = [
    new RegExp(`<meta[^>]*property=["']${k}["'][^>]*content=["']([^"']*)["']`, 'i'),
    new RegExp(`<meta[^>]*content=["']([^"']*)["'][^>]*property=["']${k}["']`, 'i'),
    new RegExp(`<meta[^>]*name=["']${k}["'][^>]*content=["']([^"']*)["']`, 'i'),
    new RegExp(`<meta[^>]*content=["']([^"']*)["'][^>]*name=["']${k}["']`, 'i'),
  ];
  for (const pattern of patterns) {
    const found = matchMeta(html, pattern);
    if (found) return found;
  }
  return null;
}

// Titles used by block / bot-challenge / CAPTCHA pages. These phrases are
// specific enough that a real homepage is very unlikely to use them.
const BLOCK_PAGE_TITLE_PATTERNS = [
  /request (?:has been |was )?blocked/i,
  /access denied/i,
  /access to this page has been denied/i,
  /attention required/i,           // Cloudflare
  /just a moment/i,                // Cloudflare challenge
  /pardon our interruption/i,      // Imperva / Distil
  /are you a (?:robot|human)/i,
  /(?:bot|human) verification/i,
  /verify you are (?:a )?human/i,
  /unusual traffic/i,
  /security check/i,
  /^\s*(?:403|forbidden|error)\s*$/i,
];

function looksLikeBlockPage(title) {
  return BLOCK_PAGE_TITLE_PATTERNS.some((re) => re.test(title));
}

// Strip tags and drop script/style bodies, so an <h1> containing a <span> or an
// icon still yields readable text.
// Treat whitespace-only values as absent. A tag that exists but is empty carries
// no more information than a missing one, and callers shouldn't have to check both.
// Collapse whitespace and treat a blank result as absent.
//
// HTML attribute values keep newlines and indentation, so a meta tag wrapped across
// source lines yields "Preview any URL as a\n    social card". Browsers and crawlers
// collapse that run of whitespace when rendering, so reporting it raw would both look
// broken and overstate the measured width. A tag that exists but is empty carries no
// more information than a missing one, so it becomes null.
function blankToNull(value) {
  if (value === null || value === undefined) return null;
  const collapsed = String(value).replace(/\s+/g, ' ').trim();
  return collapsed === '' ? null : collapsed;
}

function stripTags(fragment) {
  return fragment
    .replace(/<(script|style)\b[\s\S]*?<\/\1>/gi, '')
    .replace(/<[^>]*>/g, ' ');
}

// og:image is sometimes a relative path like "/share.png" instead of a full URL.
// Resolve it against the page's own URL so the browser can actually load it.
function absoluteUrl(maybeRelative, baseUrl) {
  if (!maybeRelative) return null;
  try {
    return new URL(maybeRelative, baseUrl).href;
  } catch {
    return null;
  }
}

// Assume https:// when no scheme is given, so "example.com" just works.
// The test requires "://" rather than just a colon, otherwise "localhost:8788"
// would be read as the scheme "localhost" and left alone.
// A protocol-relative "//example.com" also gets https.
export function normalizeUrl(input) {
  const trimmed = String(input).trim();
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)) return trimmed; // already has a scheme
  if (trimmed.startsWith('//')) return 'https:' + trimmed;
  return 'https://' + trimmed;
}

// Hostnames we refuse to fetch.
//
// This endpoint is public and fetches whatever URL it's given, which makes it a
// Server-Side Request Forgery (SSRF) surface: without this guard, someone could
// point it at loopback or private-network addresses and use it to probe things it
// shouldn't reach. Cloudflare's edge egresses to the public internet so the real
// blast radius there is small, but the guard matters when running locally or on
// any host with a private network, and refusing is cheap.
function isBlockedHost(hostname) {
  const host = hostname.toLowerCase().replace(/^\[|\]$/g, '');

  // Loopback and link-local names.
  if (host === 'localhost' || host.endsWith('.localhost')) return true;
  if (host === '::1' || host === '0.0.0.0') return true;

  // Internal-only TLDs commonly used on private networks.
  if (/\.(local|internal|localdomain|home|lan|corp|intranet)$/.test(host)) return true;

  // IPv4 private / loopback / link-local / CGNAT ranges.
  const v4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (v4) {
    const [a, b] = [Number(v4[1]), Number(v4[2])];
    if (a === 127 || a === 10 || a === 0) return true;          // loopback, private, this-network
    if (a === 172 && b >= 16 && b <= 31) return true;           // private
    if (a === 192 && b === 168) return true;                    // private
    if (a === 169 && b === 254) return true;                    // link-local / cloud metadata
    if (a === 100 && b >= 64 && b <= 127) return true;          // CGNAT
  }

  // IPv6 unique-local (fc00::/7) and link-local (fe80::/10).
  if (/^f[cd][0-9a-f]{2}:/.test(host)) return true;
  if (/^fe[89ab][0-9a-f]:/.test(host)) return true;

  return false;
}

export async function onRequest(context) {
  // context.request is the incoming request from the browser.
  const { searchParams } = new URL(context.request.url);
  const target = searchParams.get('url');

  // Always respond with JSON so the frontend can parse it consistently.
  const json = (obj, status = 200) =>
    new Response(JSON.stringify(obj), {
      status,
      headers: { 'content-type': 'application/json' },
    });

  if (!target) {
    return json({ error: 'No url provided.' }, 400);
  }

  // Basic validation: make sure it's a real http(s) URL before we fetch it.
  let parsed;
  try {
    parsed = new URL(normalizeUrl(target));
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return json({ error: 'URL must start with http:// or https://' }, 400);
    }
  } catch {
    return json({ error: 'That does not look like a valid URL.' }, 400);
  }

  if (isBlockedHost(parsed.hostname)) {
    return json(
      { error: 'For security reasons this tool only fetches public websites, not local or private addresses.' },
      400
    );
  }

  // Set up a timeout. A fetch can hang for a long time if the site is slow or
  // the domain doesn't exist. AbortController lets us cancel it after N seconds.
  // 12s, not 8s. Some legitimate sites deliberately slow-walk automated requests
  // rather than blocking them: bestbuy.com returns a valid 200 but takes 8-11s to
  // send its first byte. 8s cut those off. Waiting on a fetch doesn't consume
  // Worker CPU time, so a longer ceiling is cheap; the cost is only user patience.
  const controller = new AbortController();
  const TIMEOUT_MS = 12000;
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  try {
    // Fetch the target page's HTML. We send a User-Agent because some sites
    // reject requests that don't look like a normal browser.
    const pageResponse = await fetch(parsed.href, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (compatible; SEO-Preview-Tool/0.1)',
      },
      signal: controller.signal,
    });

    clearTimeout(timer);

    if (!pageResponse.ok) {
      // 401/403/429 from a live site almost always means bot protection, not a
      // missing page. Say so, rather than sending the user off to check the URL.
      // 406 (Not Acceptable) and 451 show up as soft bot-blocks too, alongside
      // the more obvious 401/403/429.
      if ([401, 403, 406, 429, 451].includes(pageResponse.status)) {
        return json({
          error:
            `This site refused our request (HTTP ${pageResponse.status}). It's protected against ` +
            `automated traffic, so its tags can't be read. Some large brands are configured this way.`,
        });
      }
      if (pageResponse.status === 404) {
        return json({ error: 'That page returned 404 Not Found. Check the URL path.' });
      }

      // 520-527 are Cloudflare's own codes, generated when the edge can't get a
      // usable answer from the origin. The site never sent them, so reporting
      // "the site responded with 520" is simply wrong. In practice these show up
      // for origins that are very slow to respond: every site that produced a 520
      // here had also timed out when the same request was made from a laptop.
      if (pageResponse.status >= 520 && pageResponse.status <= 527) {
        return json({
          error:
            'That site never sent a usable response — it was too slow or refused the ' +
            'connection outright. Sites that throttle automated traffic often behave ' +
            'this way. Trying again sometimes works.',
        });
      }

      return json({
        error: `The site responded with status ${pageResponse.status}, so we couldn't read its tags.`,
      });
    }

    const html = await pageResponse.text();

    // Detect bot-blocking / challenge pages. Some big sites (Amazon, sites behind
    // Cloudflare/Akamai WAFs) don't send the real page to a non-browser. Signs:
    //   - a near-empty body
    //   - a WAF challenge header
    //   - an unusual status like 202 (Accepted) instead of a normal 200
    const wafChallenge = pageResponse.headers.get('x-amzn-waf-action');
    const looksBlocked =
      wafChallenge ||
      pageResponse.status === 202 ||
      html.trim().length < 200;

    if (looksBlocked) {
      return json({
        error:
          "This site is blocking automated requests (it wants a real browser), so we can't read its tags. " +
          "Big sites like Amazon do this. Try a smaller site or your own page.",
      });
    }

    // Grab the <title>.
    // (A later milestone will parse tags properly with Cloudflare's HTMLRewriter.
    //  Regex on HTML is fragile, but fine for now while we learn.)
    // An empty <title></title> must read as absent, not as an empty string, or
    // callers see a falsy-but-present value. paypal.com serves exactly this to
    // some clients.
    const titleMatch = html.match(/<title[^>]*>([^<]*)<\/title>/i);
    const title = blankToNull(titleMatch ? decodeEntities(titleMatch[1]) : null);

    // Some sites serve a block/challenge page with HTTP 200 and a full-size body,
    // which slips past every check above. microsoft.com does exactly this: 200 OK,
    // 201KB of HTML, titled "Your request has been blocked."
    //
    // This matters more than a normal failure. Without this check we'd report that
    // Microsoft has a too-long title, no meta description and no social tags — all
    // invented from a page we never actually saw. Reporting fake SEO problems is
    // worse than reporting nothing.
    if (title && looksLikeBlockPage(title)) {
      return json({
        error:
          `This site served a block page instead of its real content (its title is ` +
          `"${title.slice(0, 60)}"). It allows browsers but not automated requests, so ` +
          `its actual tags can't be read.`,
      });
    }

    const description = blankToNull(getMeta(html, 'description'));

    const finalUrl = pageResponse.url || parsed.href;

    // --- Social sharing tags ------------------------------------------------
    // Open Graph (og:*) is what Slack, LinkedIn, iMessage, Facebook etc. read.
    // Twitter Cards (twitter:*) are X's variant; X falls back to og:* if absent.
    // blankToNull on every text value, so whitespace wrapped across source lines is
    // collapsed the way a crawler would see it and empty tags read as absent.
    const og = {
      title: blankToNull(getMeta(html, 'og:title')),
      description: blankToNull(getMeta(html, 'og:description')),
      image: absoluteUrl(blankToNull(getMeta(html, 'og:image')), finalUrl),
      url: blankToNull(getMeta(html, 'og:url')),
      siteName: blankToNull(getMeta(html, 'og:site_name')),
      type: blankToNull(getMeta(html, 'og:type')),
    };

    const twitter = {
      card: blankToNull(getMeta(html, 'twitter:card')),
      title: blankToNull(getMeta(html, 'twitter:title')),
      description: blankToNull(getMeta(html, 'twitter:description')),
      image: absoluteUrl(blankToNull(getMeta(html, 'twitter:image')), finalUrl),
    };

    // --- On-page signals for the health check -------------------------------
    // robots comes first in importance: a noindex page is invisible to Google no
    // matter how good its tags are, so nothing else matters if it's set.
    const robots = getMeta(html, 'robots');
    const viewport = getMeta(html, 'viewport');

    const langMatch = html.match(/<html[^>]*\slang=["']([^"']+)["']/i);
    const lang = langMatch ? langMatch[1].trim() : null;

    const h1s = [...html.matchAll(/<h1\b[^>]*>([\s\S]*?)<\/h1>/gi)]
      .map((m) => decodeEntities(stripTags(m[1])).replace(/\s+/g, ' ').trim())
      .filter(Boolean);

    // Count <img> tags missing alt entirely. Note: alt="" is VALID and means
    // "decorative, skip me" to screen readers, so it must not be counted as an
    // error. Only a completely absent alt attribute is a problem.
    const imgTags = html.match(/<img\b[^>]*>/gi) || [];
    const imagesMissingAlt = imgTags.filter((tag) => !/\salt\s*=/i.test(tag)).length;

    const canonical = absoluteUrl(
      matchMeta(html, /<link[^>]*rel=["']canonical["'][^>]*href=["']([^"']*)["']/i) ||
        matchMeta(html, /<link[^>]*href=["']([^"']*)["'][^>]*rel=["']canonical["']/i),
      finalUrl
    );

    return json({
      title,
      description,
      finalUrl,
      og,
      twitter,
      page: {
        robots,
        viewport,
        lang,
        h1Count: h1s.length,
        h1First: h1s[0] || null,
        imagesTotal: imgTags.length,
        imagesMissingAlt,
        canonical,
      },
    });
  } catch (err) {
    clearTimeout(timer);

    // Log the real error server-side so we can diagnose. The user gets a
    // friendly message; we keep the technical detail in the logs.
    console.error('fetch failed for', parsed.href, '|', err.name, '|', err.message);

    if (err.name === 'AbortError') {
      return json({
        error:
          'That site took more than 12 seconds to respond, so we gave up. Some sites ' +
          'deliberately slow down automated requests instead of blocking them. Trying again ' +
          'sometimes works.',
      });
    }

    // Everything else lands here: DNS failures (domain really doesn't exist) and
    // connections that get dropped by bot protection before HTTP even completes.
    // We genuinely cannot tell these apart from inside the function, so the
    // message must name both possibilities instead of guessing wrong.
    return json({
      error:
        "Couldn't load that site. Either the domain doesn't exist, or the site dropped our " +
        "connection because it blocks automated requests. If the site opens fine in your " +
        "browser, it's the second one and there's nothing this tool can do about it.",
    });
  }
}
