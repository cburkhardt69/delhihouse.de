#!/usr/bin/env python3
"""Refresh the DHS & LDI finance dashboard from Tally Day Book XML exports.

1. Finds the newest Tally XML export for each company in the inbox (e.g. ~/Downloads),
   recognising the company from the file's content, not its name.
2. Converts them into warehouse CSV tables (tally_to_warehouse.py).
3. Builds the dashboard from dashboard_template.html.
4. Encrypts the page with a password (AES-GCM, PBKDF2) so it can sit on a public website.
5. Moves the used XML files into an archive folder.

Usage:
  DASHBOARD_PASSWORD=... python3 refresh_dashboard.py --inbox ~/Downloads \
      --site "/path/to/website-repo/finance"
"""
import argparse, base64, csv, datetime as dt, glob, json, os, re, shutil, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tally_to_warehouse  # noqa: E402

# Exact company names in Tally -> short code used in the dashboard
COMPANIES = {
    'DELHI HOUSE SOCIETY - FC NEW': 'DHS-FC',
    'DELHI HOUSE SOCIETY- LOCAL NEW': 'DHS-Local',
    'LOVE DELHI INITIATIVES - FC': 'LDI-FC',
    'LOVE DELHI INITIATIVES - LOCAL': 'LDI-Local',
}
ORDER = ['DHS-FC', 'DHS-Local', 'LDI-FC', 'LDI-Local']
ADMIN_CC = {'H O Share', 'Operation Administration', 'Personal Administration'}
# How each organisation's books name the other organisation (for inter-company checks)
OTHER_ORG = {'DHS': re.compile(r'love delhi|\bldi\b', re.I), 'LDI': re.compile(r'delhi house|\bdhs\b', re.I)}


def company_of(path):
    raw = open(path, 'rb').read(4096)
    for enc in ('utf-16', 'utf-8-sig', 'utf-8'):
        try:
            m = re.search(r'<SVCURRENTCOMPANY>(.*?)</SVCURRENTCOMPANY>', raw.decode(enc, errors='ignore'))
            if m:
                return m.group(1).strip()
        except UnicodeError:
            pass
    return None


def find_exports(inbox):
    found = {}
    for p in sorted(glob.glob(os.path.join(os.path.expanduser(inbox), '*.xml')), key=os.path.getmtime):
        code = COMPANIES.get(company_of(p) or '')
        if code:
            found[code] = p  # newest wins (sorted by modification time)
    return found


def read(path):
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def lakh(v):
    return f'₹{abs(v) / 1e5:,.1f} lakh'


def build_data(tables):
    co = read(f'{tables}/dim_company.csv')
    co.sort(key=lambda r: ORDER.index(r['company']) if r['company'] in ORDER else 99)
    comps = [r['company'] for r in co]
    led = read(f'{tables}/dim_ledger.csv')
    ent = read(f'{tables}/fact_entry.csv')
    cca = read(f'{tables}/fact_costcentre_alloc.csv') if os.path.exists(f'{tables}/fact_costcentre_alloc.csv') else []
    vch = {(r['company'], r['voucher_guid']): r for r in read(f'{tables}/dim_voucher.csv')}
    for r in ent + cca:
        r['amount'] = float(r['amount'])
    for r in led:
        r['opening_balance'] = float(r['opening_balance'] or 0)

    lkey = {(r['company'], r['ledger']): i for i, r in enumerate(led)}
    months = sorted({r['month'] for r in ent})
    with_cc = {r['entry_id'] for r in cca}
    agg = defaultdict(float)
    for r in ent:
        if r['entry_id'] not in with_cc:
            agg[(lkey[(r['company'], r['ledger'])], r['month'], '')] += r['amount']
    for r in cca:
        agg[(lkey[(r['company'], r['ledger'])], r['month'], r['cost_centre'])] += r['amount']
    ccs = sorted({k[2] for k in agg})
    lines = [[li, months.index(m), ccs.index(c), round(v, 2)] for (li, m, c), v in agg.items() if abs(v) > 0.004]

    nature = {(r['company'], r['ledger']): r['nature'] for r in led}
    move = defaultdict(float)
    for r in ent:
        move[(r['company'], r['ledger'])] += r['amount']
    closing = {(r['company'], r['ledger']): r['opening_balance'] + move[(r['company'], r['ledger'])] for r in led}

    dates = sorted(r['date'] for r in ent)
    d0, d1 = dt.date.fromisoformat(dates[0]), dt.date.fromisoformat(dates[-1])
    period = dict(label=f'{d0.day} {d0:%b %Y} – {d1.day} {d1:%b %Y}', short=f'{d0:%b} – {d1:%b}', toLabel=f'{d1.day} {d1:%b %Y}')

    # postings on income/expense ledgers without a cost centre
    nocc = []
    for r in ent:
        if r['entry_id'] in with_cc or nature.get((r['company'], r['ledger'])) not in ('Income', 'Expense'):
            continue
        v = vch.get((r['company'], r['voucher_guid']), {})
        nocc.append([comps.index(r['company']), r['date'], f"{v.get('voucher_type', '')} {v.get('voucher_number', '')}".strip(),
                     r['ledger'], abs(round(r['amount'], 2)), (v.get('narration') or '')[:160]])
    nocc.sort(key=lambda x: (x[0], x[1]))

    flags = []
    # 1. every voucher must balance
    bal = defaultdict(float)
    for r in ent:
        bal[(r['company'], r['voucher_guid'])] += r['amount']
    bad = [k for k, v in bal.items() if abs(v) > 0.01]
    if bad:
        flags.append(['crit', f'{len(bad)} vouchers do not balance', 'Debits and credits differ within these vouchers. The export may be incomplete; export the Day Book again.'])
    # 2. inter-company balances should mirror each other
    orgs = {r['company']: r['organisation'] for r in co}
    funds = {r['company']: r['fund_type'] for r in co}
    for fund in ('FC', 'Local'):
        a = [c for c in comps if orgs[c] == 'DHS' and funds[c] == fund]
        b = [c for c in comps if orgs[c] == 'LDI' and funds[c] == fund]
        if not a or not b:
            continue
        sa = sum(v for (c, l), v in closing.items() if c in a and OTHER_ORG['DHS'].search(l) and nature.get((c, l)) in ('Asset', 'Liability'))
        sb = sum(v for (c, l), v in closing.items() if c in b and OTHER_ORG['LDI'].search(l) and nature.get((c, l)) in ('Asset', 'Liability'))
        if abs(sa + sb) > 1000:
            flags.append(['crit' if fund == 'FC' else 'warn', f'DHS and LDI {fund} books disagree on their mutual balance',
                          f'DHS-{fund} shows a net {"receivable" if sa >= 0 else "payable"} of {lakh(sa)} towards LDI; LDI-{fund} shows a net '
                          f'{"payable" if sb <= 0 else "receivable"} of {lakh(sb)} towards DHS. The two should mirror each other; the gap is {lakh(sa + sb)}.'
                          + (' For FC funds this also bears on FCRA, which prohibits passing foreign contribution to another organisation.' if fund == 'FC' else '')])
    # 3. administration share in FC books
    for c in comps:
        if funds[c] != 'FC':
            continue
        exp = sum(r['amount'] for r in cca if r['company'] == c and nature.get((c, r['ledger'])) == 'Expense' and r['cost_centre'] in ADMIN_CC)
        inc = -sum(r['amount'] for r in ent if r['company'] == c and nature.get((c, r['ledger'])) == 'Income')
        if inc > 0 and exp / inc > 0.2:
            flags.append(['warn', f'Administration share of {c} is {exp / inc:.0%} of FC received',
                          f'The administration cost centres total {lakh(exp)} against {lakh(inc)} foreign contribution received in the period. '
                          'FCRA caps administrative expenses at 20% of FC received; check with the accountant whether these cost centres match the FCRA definition.'])
    # 4. expenses without cost centre
    for i, c in enumerate(comps):
        rows = [x for x in nocc if x[0] == i and nature.get((c, x[3])) == 'Expense']
        if rows:
            flags.append(['warn', f'{lakh(sum(x[4] for x in rows))} of {c} expenditure has no cost centre',
                          f'{len(rows)} postings, listed in the table above, are not assigned to any project.'])
    # 5. bank accounts that appear in more than one set of books
    banks, label = defaultdict(set), {}
    for r in led:
        if r['group_name'] == 'Bank Accounts':
            k = re.sub(r'\s+', ' ', r['ledger'].lower()); banks[k].add(r['company']); label.setdefault(k, r['ledger'])
    for name, cs in banks.items():
        if len(cs) > 1:
            flags.append(['warn', 'One bank account appears in several sets of books',
                          f'"{label[name]}" is a ledger in {", ".join(sorted(cs))}. Its balance is only meaningful per set of books together with the inter-company ledgers.'])
    # 6. opening balances incomplete (Day Book exports omit untouched ledgers)
    gaps = [c for c in comps if abs(sum(r['opening_balance'] for r in led if r['company'] == c)) > 1]
    if gaps:
        flags.append(['warn', 'The fund side of the balance sheet is incomplete',
                      f'Opening balances do not add up to zero in {", ".join(gaps)} because a Day Book export only includes ledgers with transactions. '
                      'Cash and bank balances are complete; export All Masters as well for a full balance sheet.'])

    return dict(companies=co, months=months, ccs=ccs, period=period, built=dt.date.today().strftime('%-d %b %Y'),
                ledgers=[[comps.index(r['company']), r['ledger'], r['group_name'], r['primary_group'], r['nature'], round(r['opening_balance'], 2)] for r in led],
                lines=lines, nocc=nocc, flags=flags)


LOCK_PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>DHS &amp; LDI Finance Cube</title>
<style>:root{--bg:#f5f7f8;--panel:#fff;--fg:#17242b;--muted:#5b6c75;--line:#dde4e8;--accent:#1b6f8a;--crit:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#0f171b;--panel:#162228;--fg:#e3ebef;--muted:#93a6b0;--line:#26363e;--accent:#5fb0cc;--crit:#ff8a80;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}
form{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:24px;width:100%;max-width:360px;display:grid;gap:12px}
h1{font-size:1.2rem;margin:0}p{margin:0;color:var(--muted);font-size:.9rem}
input{font:inherit;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}
button{font:inherit;font-weight:600;padding:9px;border:0;border-radius:8px;background:var(--accent);color:var(--panel);cursor:pointer}
#err{color:var(--crit);font-size:.85rem;min-height:1.2em}</style></head><body>
<form id="f"><h1>DHS &amp; LDI Finance Cube</h1><p>Enter the password to open the dashboard.</p>
<input id="pw" type="password" autocomplete="current-password" aria-label="Password" autofocus>
<button type="submit">Open</button><div id="err" role="alert"></div></form>
<script>
const P=__PAYLOAD__;
const b64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
document.getElementById('f').addEventListener('submit',async e=>{e.preventDefault();const err=document.getElementById('err');err.textContent='Opening…';
 try{const base=await crypto.subtle.importKey('raw',new TextEncoder().encode(document.getElementById('pw').value),'PBKDF2',false,['deriveKey']);
  const key=await crypto.subtle.deriveKey({name:'PBKDF2',salt:b64(P.salt),iterations:P.iter,hash:'SHA-256'},base,{name:'AES-GCM',length:256},false,['decrypt']);
  const html=new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:b64(P.iv)},key,b64(P.data)));
  document.open();document.write(html);document.close();}
 catch(x){err.textContent='Wrong password.';}});
</script></body></html>'''


def encrypt_page(html, password):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
    except ImportError:
        sys.exit('The "cryptography" package is missing. Install it once with:  pip3 install --user cryptography')
    salt, iv, iters = os.urandom(16), os.urandom(12), 600_000
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iters).derive(password.encode())
    data = AESGCM(key).encrypt(iv, html.encode('utf-8'), None)
    enc = lambda b: base64.b64encode(b).decode()
    return LOCK_PAGE.replace('__PAYLOAD__', json.dumps(dict(salt=enc(salt), iv=enc(iv), iter=iters, data=enc(data))))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--inbox', default='~/Downloads', help='folder with the Tally XML exports')
    ap.add_argument('--warehouse', default=HERE, help='folder for CSV tables and the XML archive')
    ap.add_argument('--site', required=True, help='folder in the website repository that receives index.html')
    ap.add_argument('--allow-partial', action='store_true', help='build even if not all four companies are present')
    ap.add_argument('--no-archive', action='store_true', help='leave the XML files in the inbox')
    a = ap.parse_args()

    password = os.environ.get('DASHBOARD_PASSWORD')
    if not password:
        sys.exit('Set the DASHBOARD_PASSWORD environment variable (update_dashboard.command reads it from the Keychain).')

    found = find_exports(a.inbox)
    missing = [c for c in ORDER if c not in found]
    for c in ORDER:
        print(f'  {c:10} {os.path.basename(found[c]) if c in found else "— not found"}')
    if not found or (missing and not a.allow_partial):
        sys.exit(f'Missing exports for: {", ".join(missing)}. Download them from Tally into {a.inbox} and run again.')

    tables = os.path.join(a.warehouse, 'tables')
    if os.path.isdir(tables):
        shutil.rmtree(tables)
    tally_to_warehouse.convert(tables, [(c, found[c]) for c in ORDER if c in found])

    data = build_data(tables)
    template = open(os.path.join(HERE, 'dashboard_template.html'), encoding='utf-8').read()
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">'
            '</head><body>' + template.replace('__DATA__', json.dumps(data, ensure_ascii=False, separators=(',', ':'))) + '</body></html>')
    os.makedirs(a.site, exist_ok=True)
    with open(os.path.join(a.site, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(encrypt_page(page, password))
    print(f'Dashboard written to {os.path.join(a.site, "index.html")} ({data["period"]["label"]}, {len(data["flags"])} points to check)')
    for level, title, _ in data['flags']:
        print(f'  [{"!!" if level == "crit" else "! "}] {title}')

    if not a.no_archive:
        arch = os.path.join(a.warehouse, 'archive', dt.datetime.now().strftime('%Y-%m-%d_%H%M'))
        os.makedirs(arch, exist_ok=True)
        for c, p in found.items():
            shutil.move(p, os.path.join(arch, f'{c}.xml'))
        print(f'Exports archived in {arch}')


if __name__ == '__main__':
    main()
