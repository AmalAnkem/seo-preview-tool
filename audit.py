#!/usr/bin/env python3
"""
SEO audit harness — runs our tool against a batch of large S&P 500 company sites
and reports what it found, plus the verdicts our real measurement code produces.

Usage:
    npx wrangler pages dev . --port 8788      # in another terminal
    python3 audit.py                          # then this

How it works
------------
1. Calls our own /api/fetch endpoint for each site (concurrently).
2. Builds a throwaway HTML page that loads the REAL <style> block and the REAL
   measurement functions extracted from index.html, and runs them over every
   result. Verdicts must be computed in a browser because they depend on genuine
   text layout (scrollHeight / Range geometry), which Node cannot simulate.
3. Runs that page in headless Chrome and collects the verdicts.
4. Prints a report and writes audit-results-batch<N>.csv.

Batches:
    python3 audit.py --batch 1     55 mega-cap companies
    python3 audit.py --batch 2     100 more, spanning every sector
    python3 audit.py --batch all   all 155

IMPORTANT LIMITATION
--------------------
This script does NOT fetch what Google displays. Scraping Google's result pages
violates their terms of service and gets blocked, so the Google-side comparison
cannot be automated here. What this script gives you instead is the input side:
the exact tags each site publishes, and whether they fit Google's display limits.
The REWRITE_RISK column flags sites where Google is most likely to show something
different, so you can spot-check those few by hand rather than all of them.
"""

import concurrent.futures
import csv
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request

# This Python install has no usable system CA bundle, so HTTPS requests fail with
# CERTIFICATE_VERIFY_FAILED (a common macOS python.org quirk, unrelated to the tool
# being audited). Point at certifi's bundle when it's available rather than asking
# anyone to run "Install Certificates.command" or, worse, disabling verification.
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

# Default to the local dev server. Use --api to audit the deployed site instead:
#   python3 audit.py --batch all --api https://seo-preview-tool.pages.dev/api/fetch
#
# This matters more than it sounds. Sites that block a residential IP often serve
# Cloudflare's edge without complaint (microsoft.com is the clearest example), so
# local runs understate how many sites the deployed tool can actually read.
API = 'http://localhost:8788/api/fetch'
INDEX_HTML = 'public/index.html'
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
REQUEST_TIMEOUT = 25      # our API gives up at 12s; allow margin
WORKERS = 6

# Large S&P 500 companies. Ordered roughly by market cap for the mega caps
# (Nvidia, Apple, Microsoft, Amazon, Alphabet lead the index as of 2026), then a
# broad spread of sectors. Corporate domains are used where the company's main
# site is separate from its consumer storefront.
BATCH_1 = [
    ("NVIDIA", "nvidia.com"),
    ("Apple", "apple.com"),
    ("Microsoft", "microsoft.com"),
    ("Amazon", "amazon.com"),
    ("Alphabet", "abc.xyz"),
    ("Meta", "meta.com"),
    ("Broadcom", "broadcom.com"),
    ("Tesla", "tesla.com"),
    ("Berkshire Hathaway", "berkshirehathaway.com"),
    ("JPMorgan Chase", "jpmorganchase.com"),
    ("Eli Lilly", "lilly.com"),
    ("Visa", "visa.com"),
    ("UnitedHealth", "unitedhealthgroup.com"),
    ("Mastercard", "mastercard.com"),
    ("Costco", "costco.com"),
    ("Walmart", "walmart.com"),
    ("Procter & Gamble", "pg.com"),
    ("Johnson & Johnson", "jnj.com"),
    ("Home Depot", "homedepot.com"),
    ("Netflix", "netflix.com"),
    ("AbbVie", "abbvie.com"),
    ("Bank of America", "bankofamerica.com"),
    ("Coca-Cola", "coca-colacompany.com"),
    ("Merck", "merck.com"),
    ("Chevron", "chevron.com"),
    ("AMD", "amd.com"),
    ("Salesforce", "salesforce.com"),
    ("PepsiCo", "pepsico.com"),
    ("Adobe", "adobe.com"),
    ("Thermo Fisher", "thermofisher.com"),
    ("McDonald's", "mcdonalds.com"),
    ("Cisco", "cisco.com"),
    ("Wells Fargo", "wellsfargo.com"),
    ("Accenture", "accenture.com"),
    ("Abbott", "abbott.com"),
    ("Intuit", "intuit.com"),
    ("Qualcomm", "qualcomm.com"),
    ("Texas Instruments", "ti.com"),
    ("Verizon", "verizon.com"),
    ("Amgen", "amgen.com"),
    ("Nike", "nike.com"),
    ("Disney", "thewaltdisneycompany.com"),
    ("IBM", "ibm.com"),
    ("Caterpillar", "caterpillar.com"),
    ("American Express", "americanexpress.com"),
    ("Starbucks", "starbucks.com"),
    ("Goldman Sachs", "goldmansachs.com"),
    ("Boeing", "boeing.com"),
    ("Target", "target.com"),
    ("Best Buy", "bestbuy.com"),
    ("Intel", "intel.com"),
    ("Oracle", "oracle.com"),
    ("Uber", "uber.com"),
    ("PayPal", "paypal.com"),
    ("T-Mobile", "t-mobile.com"),
]

# A second, non-overlapping set of 100 S&P 500 companies spanning every sector.
BATCH_2 = [
    # Energy / industrials / materials
    ("Exxon Mobil", "corporate.exxonmobil.com"),
    ("Linde", "linde.com"),
    ("Air Products", "airproducts.com"),
    ("Sherwin-Williams", "sherwin-williams.com"),
    ("Ecolab", "ecolab.com"),
    ("Corning", "corning.com"),
    ("Honeywell", "honeywell.com"),
    ("RTX", "rtx.com"),
    ("GE Aerospace", "geaerospace.com"),
    ("Lockheed Martin", "lockheedmartin.com"),
    ("Northrop Grumman", "northropgrumman.com"),
    ("General Dynamics", "gd.com"),
    ("Emerson Electric", "emerson.com"),
    ("Illinois Tool Works", "itw.com"),
    ("Parker Hannifin", "parker.com"),
    ("Deere", "deere.com"),
    ("Danaher", "danaher.com"),
    # Transport / logistics
    ("Union Pacific", "up.com"),
    ("CSX", "csx.com"),
    ("Norfolk Southern", "norfolksouthern.com"),
    ("FedEx", "fedex.com"),
    ("UPS", "ups.com"),
    ("Waste Management", "wm.com"),
    ("Republic Services", "republicservices.com"),
    # Financials
    ("Morgan Stanley", "morganstanley.com"),
    ("Citigroup", "citigroup.com"),
    ("Charles Schwab", "schwab.com"),
    ("Blackstone", "blackstone.com"),
    ("Chubb", "chubb.com"),
    ("Progressive", "progressive.com"),
    ("Marsh & McLennan", "marshmclennan.com"),
    ("Aon", "aon.com"),
    ("Fiserv", "fiserv.com"),
    ("FIS", "fisglobal.com"),
    ("Global Payments", "globalpayments.com"),
    # Healthcare / pharma
    ("Bristol-Myers Squibb", "bms.com"),
    ("Medtronic", "medtronic.com"),
    ("Stryker", "stryker.com"),
    ("Gilead Sciences", "gilead.com"),
    ("Vertex Pharmaceuticals", "vrtx.com"),
    ("Regeneron", "regeneron.com"),
    ("CVS Health", "cvshealth.com"),
    ("Elevance Health", "elevancehealth.com"),
    ("Becton Dickinson", "bd.com"),
    ("Zoetis", "zoetis.com"),
    ("IDEXX Labs", "idexx.com"),
    ("ResMed", "resmed.com"),
    # Semis / hardware
    ("Applied Materials", "appliedmaterials.com"),
    ("Lam Research", "lamresearch.com"),
    ("KLA", "kla.com"),
    ("Micron", "micron.com"),
    ("Analog Devices", "analog.com"),
    ("Microchip Technology", "microchip.com"),
    ("NXP Semiconductors", "nxp.com"),
    ("ON Semiconductor", "onsemi.com"),
    ("HP", "hp.com"),
    ("Dell Technologies", "dell.com"),
    ("HPE", "hpe.com"),
    ("NetApp", "netapp.com"),
    ("Seagate", "seagate.com"),
    ("Western Digital", "westerndigital.com"),
    ("Motorola Solutions", "motorolasolutions.com"),
    ("Garmin", "garmin.com"),
    ("Keysight", "keysight.com"),
    ("Amphenol", "amphenol.com"),
    ("TE Connectivity", "te.com"),
    # Software / internet
    ("ServiceNow", "servicenow.com"),
    ("Synopsys", "synopsys.com"),
    ("Cadence", "cadence.com"),
    ("Autodesk", "autodesk.com"),
    ("Workday", "workday.com"),
    ("Snowflake", "snowflake.com"),
    ("Datadog", "datadoghq.com"),
    ("Palantir", "palantir.com"),
    ("Fortinet", "fortinet.com"),
    ("Palo Alto Networks", "paloaltonetworks.com"),
    ("CrowdStrike", "crowdstrike.com"),
    ("Cognizant", "cognizant.com"),
    ("Booking Holdings", "bookingholdings.com"),
    ("Airbnb", "airbnb.com"),
    ("DoorDash", "doordash.com"),
    ("Comcast", "corporate.comcast.com"),
    # Consumer staples
    ("Philip Morris", "pmi.com"),
    ("Altria", "altria.com"),
    ("Mondelez", "mondelezinternational.com"),
    ("Colgate-Palmolive", "colgatepalmolive.com"),
    ("Kimberly-Clark", "kimberly-clark.com"),
    ("General Mills", "generalmills.com"),
    ("Kraft Heinz", "kraftheinzcompany.com"),
    ("Hershey", "thehersheycompany.com"),
    ("Kroger", "kroger.com"),
    ("Sysco", "sysco.com"),
    # Consumer discretionary / retail / restaurants
    ("Lowe's", "lowes.com"),
    ("TJX", "tjx.com"),
    ("Ross Stores", "rossstores.com"),
    ("Dollar General", "dollargeneral.com"),
    ("AutoZone", "autozone.com"),
    ("Tractor Supply", "tractorsupply.com"),
    ("Yum Brands", "yum.com"),
    ("Chipotle", "chipotle.com"),
    ("Marriott", "marriott.com"),
    ("Hilton", "hilton.com"),
    # Utilities / telecom / other
    ("NextEra Energy", "nexteraenergy.com"),
    ("Duke Energy", "duke-energy.com"),
    ("Southern Company", "southerncompany.com"),
    ("American Tower", "americantower.com"),
    ("Automatic Data Processing", "adp.com"),
    ("Cintas", "cintas.com"),
    ("Paychex", "paychex.com"),
]

BATCHES = {'1': BATCH_1, '2': BATCH_2, 'all': BATCH_1 + BATCH_2}

# Set from the command line in main().
COMPANIES = BATCH_1


# --------------------------------------------------------------------------
# Step 1: call our API for every site
# --------------------------------------------------------------------------
def request(url, timeout):
    """GET a URL, identifying ourselves properly.

    Cloudflare returns 403 for the default "Python-urllib/3.x" User-Agent, so
    auditing our own deployed site fails without this. Our own tool bot-blocking
    our own audit script is a neat illustration of the thing it measures.
    """
    req = urllib.request.Request(url, headers={
        'User-Agent': 'SEO-Preview-Tool-Audit/1.0 (+https://github.com/AmalAnkem/seo-preview-tool)',
        'Accept': 'application/json, text/html',
    })
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT)


def fetch_one(entry):
    name, domain = entry
    url = f'{API}?url=' + urllib.parse.quote(f'https://{domain}', safe='')
    try:
        with request(url, REQUEST_TIMEOUT) as r:
            data = json.loads(r.read().decode('utf-8'))
    except Exception as exc:
        data = {'error': f'request failed locally: {exc}'}
    data['_name'] = name
    data['_domain'] = domain
    return data


def collect():
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_one, c): c for c in COMPANIES}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())
            done += 1
            sys.stderr.write(f'\r  fetched {done}/{len(COMPANIES)}')
            sys.stderr.flush()
    sys.stderr.write('\n')
    order = {d: i for i, (n, d) in enumerate(COMPANIES)}
    results.sort(key=lambda r: order[r['_domain']])
    return results


# --------------------------------------------------------------------------
# Step 2 + 3: compute verdicts using the REAL measurement code in a browser
# --------------------------------------------------------------------------
def extract_from_index():
    html = open(INDEX_HTML).read()
    style = re.search(r'<style>(.*?)</style>', html, re.S).group(1)
    script = re.search(r'<script>(.*?)</script>', html, re.S).group(1)

    def fn(name):
        start = script.index(f'function {name}(')
        depth, i = 0, script.index('{', start)
        while True:
            if script[i] == '{':
                depth += 1
            elif script[i] == '}':
                depth -= 1
                if depth == 0:
                    return script[start:i + 1]
            i += 1

    consts = '\n'.join([
        re.search(r'const DESC_VISIBLE_LINES = \d+;', script).group(0),
        re.search(r'const GOOGLE_COLUMN_PX = \d+;', script).group(0),
    ])
    funcs = '\n'.join(fn(n) for n in
                      ['textWidthOf', 'lineHeightOf', 'reportTitle',
                       'reportDescription', 'paintMeter'])
    return style, consts, funcs


def compute_verdicts(results):
    style, consts, funcs = extract_from_index()
    payload = json.dumps([
        {'domain': r['_domain'], 'title': r.get('title'), 'description': r.get('description')}
        for r in results if 'error' not in r
    ])

    page = f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{style}</style></head>
<body>
<div class="measure" id="measure-title"></div>
<div class="measure" id="measure-desc"></div>
<div id="sink"></div>
<pre id="OUT"></pre>
<script>
{consts}
const measureTitle = document.getElementById('measure-title');
const measureDesc = document.getElementById('measure-desc');
{funcs}

const rows = {payload};
const sink = document.getElementById('sink');
const out = {{}};
function bandOf(el) {{ return el.className.replace('meter', '').trim() || 'ok'; }}
for (const row of rows) {{
  const rec = {{}};
  if (row.title) {{
    reportTitle(sink, row.title);
    rec.titleBand = bandOf(sink);
    rec.titleText = sink.textContent;
    measureTitle.textContent = row.title;
    rec.titlePx = Math.round(textWidthOf(measureTitle));
  }}
  if (row.description) {{
    reportDescription(sink, row.description);
    rec.descBand = bandOf(sink);
    rec.descText = sink.textContent;
    measureDesc.textContent = row.description;
    rec.descLines = Math.max(1, Math.round(measureDesc.offsetHeight / lineHeightOf(measureDesc)));
  }}
  out[row.domain] = rec;
}}
document.getElementById('OUT').textContent = JSON.stringify(out);
</script>
</body></html>
'''
    tmp = os.path.abspath('_audit_measure.html')
    with open(tmp, 'w') as f:
        f.write(page)
    try:
        proc = subprocess.run(
            [CHROME, '--headless', '--disable-gpu', '--no-sandbox',
             '--window-size=1400,900', '--virtual-time-budget=4000',
             '--dump-dom', f'file://{tmp}'],
            capture_output=True, text=True, timeout=120)
        m = re.search(r'<pre id="OUT">(.*?)</pre>', proc.stdout, re.S)
        if not m:
            return {}
        import html as htmlmod
        return json.loads(htmlmod.unescape(m.group(1)))
    finally:
        os.remove(tmp)


# --------------------------------------------------------------------------
# Step 4: report
# --------------------------------------------------------------------------
def classify_failure(err):
    e = err.lower()
    if 'more than 12 seconds' in e:
        return 'TIMEOUT'
    if 'served a block page' in e:
        return 'BLOCK-PAGE'
    if 'refused our request' in e or 'blocking automated' in e:
        return 'BOT-BLOCKED'
    if "couldn't load" in e:
        return 'BLOCKED/DNS'
    if '404' in e:
        return '404'
    return 'OTHER'


def social_state(r):
    og = r.get('og') or {}
    tw = r.get('twitter') or {}
    has_img = bool(og.get('image') or tw.get('image'))
    og_count = sum(1 for k in ('title', 'description', 'image') if og.get(k))
    if og_count == 3:
        return 'FULL'
    if og_count == 0 and not has_img:
        return 'NONE'
    return 'PARTIAL'


def main():
    global COMPANIES, API

    import argparse
    ap = argparse.ArgumentParser(description='Audit company sites through our SEO tool.')
    ap.add_argument('--batch', choices=sorted(BATCHES), default='1',
                    help='which company set to run (default: 1)')
    ap.add_argument('--out', default=None, help='CSV output path')
    ap.add_argument('--api', default=API,
                    help='API endpoint to audit through (default: local dev server)')
    args = ap.parse_args()
    API = args.api
    COMPANIES = BATCHES[args.batch]
    csv_path = args.out or f'audit-results-batch{args.batch}.csv'

    if not os.path.exists(INDEX_HTML):
        sys.exit(f'Run this from the project root ({INDEX_HTML} not found).')
    if not os.path.exists(CHROME):
        sys.exit(f'Chrome not found at {CHROME}')
    # Probe whichever API we were pointed at, not always localhost.
    probe = API.split('/api/')[0] + '/'
    try:
        request(probe, 15).close()
    except Exception as exc:
        sys.exit(f'{probe} not reachable ({exc}).\n'
                 'For a local run, start the dev server first:\n'
                 '  npx wrangler pages dev --port 8788')

    print(f'Auditing {len(COMPANIES)} sites through our own API...')
    results = collect()
    print('Computing verdicts in headless Chrome...')
    verdicts = compute_verdicts(results)

    ok = [r for r in results if 'error' not in r]
    failed = [r for r in results if 'error' in r]

    # ---- table ----
    print()
    print('=' * 132)
    print('RESULTS'.center(132))
    print('=' * 132)
    hdr = (f'{"COMPANY":<22}{"DOMAIN":<28}{"TITLE":<7}{"PX":>5}  '
           f'{"TITLE FIT":<10}{"DESC":<6}{"LN":>3}  {"DESC FIT":<10}{"SOCIAL":<9}{"OG:IMAGE":<9}')
    print(hdr)
    print('-' * 132)

    rows_csv = []
    for r in results:
        # Truncate so long names can't run into the next column.
        name = r['_name'] if len(r['_name']) <= 21 else r['_name'][:20] + '\u2026'
        domain = r['_domain']
        if 'error' in r:
            kind = classify_failure(r['error'])
            print(f'{name:<22}{domain:<28}{"--":<7}{"--":>5}  {kind:<10}'
                  f'{"--":<6}{"--":>3}  {"--":<10}{"--":<9}{"--":<9}')
            rows_csv.append({
                'company': r['_name'], 'domain': domain, 'status': kind,
                'title': '', 'title_px': '', 'title_fit': '',
                'description': '', 'desc_lines': '', 'desc_fit': '',
                'social': '', 'og_image': '', 'rewrite_risk': '',
                'error': r['error'],
            })
            continue

        v = verdicts.get(domain, {})
        og = r.get('og') or {}
        tw = r.get('twitter') or {}
        has_title = bool(r.get('title'))
        has_desc = bool(r.get('description'))
        img = og.get('image') or tw.get('image')

        band_word = {'ok': 'fits', 'warn': 'tight', 'over': 'CUT'}
        tfit = band_word.get(v.get('titleBand'), '--') if has_title else 'MISSING'
        dfit = band_word.get(v.get('descBand'), '--') if has_desc else 'MISSING'
        lines = v.get('descLines', '') if has_desc else ''
        px = v.get('titlePx', '') if has_title else ''
        soc = social_state(r)

        # Google is most likely to show something different when the description
        # is absent (it invents one) or too long (it re-cuts or rewrites it).
        risk = 'HIGH' if not has_desc or v.get('descBand') == 'over' else 'normal'

        print(f'{name:<22}{domain:<28}{"yes" if has_title else "NO":<7}{px:>5}  '
              f'{tfit:<10}{"yes" if has_desc else "NO":<6}{str(lines):>3}  '
              f'{dfit:<10}{soc:<9}{"yes" if img else "NO":<9}')

        rows_csv.append({
            'company': r['_name'], 'domain': domain, 'status': 'OK',
            'title': r.get('title') or '', 'title_px': px, 'title_fit': tfit,
            'description': r.get('description') or '', 'desc_lines': lines,
            'desc_fit': dfit, 'social': soc, 'og_image': img or '',
            'rewrite_risk': risk, 'error': '',
        })

    # ---- summary ----
    print()
    print('=' * 132)
    print(f'SUMMARY  —  {len(ok)}/{len(results)} sites readable, {len(failed)} unreadable')
    print('=' * 132)

    if failed:
        buckets = {}
        for r in failed:
            buckets.setdefault(classify_failure(r['error']), []).append(r['_name'])
        print('\nUNREADABLE (our tool could not read the page at all):')
        for kind, names in sorted(buckets.items()):
            print(f'  {kind:<14} {len(names):>2}  {", ".join(names)}')

    def names_where(pred):
        return [r['_name'] for r in ok if pred(r, verdicts.get(r['_domain'], {}))]

    checks = [
        ('Missing <title>', lambda r, v: not r.get('title')),
        ('Title too long (cut by Google)', lambda r, v: v.get('titleBand') == 'over'),
        ('Missing meta description', lambda r, v: not r.get('description')),
        ('Description too long (cut by Google)', lambda r, v: v.get('descBand') == 'over'),
        ('No og:image (bare text when shared)',
         lambda r, v: not ((r.get('og') or {}).get('image') or (r.get('twitter') or {}).get('image'))),
        ('No social tags at all', lambda r, v: social_state(r) == 'NONE'),
        ('Complete Open Graph (title+desc+image)', lambda r, v: social_state(r) == 'FULL'),
    ]
    print('\nFINDINGS across the readable sites:')
    for label, pred in checks:
        hits = names_where(pred)
        print(f'  {label:<40} {len(hits):>2}/{len(ok)}  {", ".join(hits) if hits else "-"}')

    high = [r['_name'] for r in ok
            if not r.get('description')
            or verdicts.get(r['_domain'], {}).get('descBand') == 'over']
    print(f'\nWORTH SPOT-CHECKING ON GOOGLE ({len(high)}) — description missing or too long,')
    print('so Google is most likely displaying something other than the tag:')
    print(f'  {", ".join(high) if high else "-"}')

    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows_csv[0].keys()))
        w.writeheader()
        w.writerows(rows_csv)
    print(f'\nFull detail (including every title and description) written to {csv_path}')


if __name__ == '__main__':
    main()
