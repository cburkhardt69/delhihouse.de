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


# Duplicate detection: names that differ only by a misspelt word, or (for party and staff accounts in the same
# group) where one name's words contain the other's once numbers and generic words are ignored.
DICT_PATH = '/usr/share/dict/words'
FILLER = {'for', 'of', 'the', 'and', 'dhs', 'ldi', 'fc', 'local', 'new'}
GENERIC = {'adv', 'advance', 'advances', 'account', 'acc', 'ac', 'a', 'c', 'exp', 'expense', 'expenses', 'payable', 'staff'}


def _words():
    try:
        with open(DICT_PATH) as f:
            return {w.strip().lower() for w in f}
    except OSError:
        return None


# Everyday words missing from the macOS dictionary (web2 is old and American)
EXTRA_WORDS = {'internet', 'website', 'online', 'software', 'email', 'childcare', 'consultancy', 'microfinance', 'knowhow',
               'fundraising', 'healthcare', 'centre', 'programme', 'organisation', 'labour'}
# Optional, git-ignored list of issues found outside the Day Book export (e.g. in an All Masters export).
# Columns: list (duplicate|misspelt), books, name, other (similar name or correct spelling), note
KNOWN_ISSUES = os.path.join(HERE, 'known_issues.csv')


def _known(w, words):
    if w in words or w in EXTRA_WORDS or w.isdigit() or len(w) <= 3:
        return True
    if w.endswith('ies') and w[:-3] + 'y' in words:
        return True
    return any(w.endswith(s) and w[:-len(s)] in words for s in ('s', 'es', 'd', 'ed', 'ing', 'er', 'ship'))


def _edit_distance(a, b):
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def duplicate_reason(a, b, words, containment):
    from difflib import SequenceMatcher
    ta, tb = re.findall(r'[a-z]+|[0-9]+', a.lower()), re.findall(r'[a-z]+|[0-9]+', b.lower())
    if ta == tb:
        return 'Names differ only in spacing or punctuation'
    if words is not None:
        fixes, extra = [], []
        for op, i1, i2, j1, j2 in SequenceMatcher(None, ta, tb).get_opcodes():
            if op == 'equal':
                continue
            pairs = list(zip(ta[i1:i2], tb[j1:j2]))
            if op == 'replace' and i2 - i1 == j2 - j1 and all(
                    min(len(x), len(y)) >= 4 and _edit_distance(x, y) <= 2 and not (_known(x, words) and _known(y, words))
                    for x, y in pairs):
                fixes += pairs
            else:
                extra += ta[i1:i2] + tb[j1:j2]
        if fixes and all(w in FILLER for w in extra):
            return 'Spelling: ' + ', '.join(f'"{x}" / "{y}"' for x, y in fixes)
    if containment:
        sa = {w for w in ta if w not in GENERIC and not w.isdigit()}
        sb = {w for w in tb if w not in GENERIC and not w.isdigit()}
        if sa and sb and sa != sb and (sa <= sb or sb <= sa):
            return 'Same account in the same group, one name abbreviated or extended'
    return None


def find_duplicates(led, ent, cca, orgs):
    from itertools import combinations
    words = _words()
    posts = defaultdict(int)
    for r in ent:
        posts[('L', r['company'], r['ledger'])] += 1
    for r in cca:
        posts[('C', orgs[r['company']], r['cost_centre'])] += 1
    out = []
    for c in sorted({r['company'] for r in led}):
        for x, y in combinations([r for r in led if r['company'] == c], 2):
            why = duplicate_reason(x['ledger'], y['ledger'], words,
                                   x['group_name'] == y['group_name'] and x['nature'] in ('Asset', 'Liability'))
            if why:
                out.append(['Ledger', c, x['ledger'], y['ledger'], why, posts[('L', c, x['ledger'])], posts[('L', c, y['ledger'])]])
    for org in sorted(set(orgs.values())):
        names = sorted({r['cost_centre'] for r in cca if orgs[r['company']] == org})
        for x, y in combinations(names, 2):
            why = duplicate_reason(x, y, words, False)
            if why:
                books = ', '.join(sorted({r['company'] for r in cca if orgs[r['company']] == org and r['cost_centre'] in (x, y)}))
                out.append(['Cost centre', books, x, y, why, posts[('C', org, x)], posts[('C', org, y)]])
    return out


def find_misspellings(led, costcentres):
    """Names containing a word that is not in the dictionary but is one or two letters away from a known word.
    Party and staff accounts (mostly people and firms) are only compared with words used elsewhere in the books."""
    words = _words()
    if words is None:
        return []
    tok = lambda s: re.findall(r'[a-z0-9`]+', s.lower())
    names = ([('Ledger', r['company'], r['ledger'], r['nature'] in ('Income', 'Expense')) for r in led]
             + [('Cost centre', r['company'], r['cost_centre'], True) for r in costcentres])
    corpus = {w for _, _, n, _ in names for w in tok(n) if len(w) >= 4 and _known(w, words)}
    by_len = defaultdict(list)
    for w in words:
        if w.isalpha() and w.islower():
            by_len[len(w)].append(w)

    def best(w, cands, lim):
        def suffix(c):
            n = 0
            while n < min(len(c), len(w)) and c[-1 - n] == w[-1 - n]:
                n += 1
            return n
        scored = [(_edit_distance(w, c), -suffix(c), c) for c in cands if abs(len(c) - len(w)) <= lim]
        top = min(scored, default=None)
        return top[2] if top and top[0] <= lim else None

    cache = {}

    def suggest(w, use_dict):
        if (w, use_dict) not in cache:
            s = None
            if len(w) >= 5 and not any(ch.isdigit() for ch in w) and not _known(w, words):
                lim = 1 if len(w) < 8 else 2
                s = best(w, corpus, lim)
                if s is None and use_dict and len(w) >= 8:
                    s = best(w, [c for n in range(len(w) - lim, len(w) + lim + 1) for c in by_len.get(n, ()) if c[0] == w[0]], lim)
            cache[(w, use_dict)] = s
        return cache[(w, use_dict)]

    found = {}
    for kind, company, name, use_dict in names:
        fixes = [f'"{w}" → "{s}"' for w in tok(name) for s in [suggest(w, use_dict)] if s]
        if fixes:
            found.setdefault((kind, name), [set(), ', '.join(fixes)])[0].add(company)
    return [[k, ', '.join(sorted(cs)), n, fx] for (k, n), (cs, fx) in sorted(found.items())]


def known_issues():
    if not os.path.exists(KNOWN_ISSUES):
        return [], []
    dups, miss = [], []
    for r in read(KNOWN_ISSUES):
        note = (r.get('note') or '').strip() or 'Recorded manually'
        if r['list'].strip().lower() == 'duplicate':
            dups.append(['Ledger', r['books'], r['name'], r['other'], note, '–', '–'])
        else:
            miss.append(['Ledger', r['books'], r['name'], f'→ "{r["other"]}" ({note})'])
    return dups, miss


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
                          f'{len(rows)} postings, listed below, are not assigned to any project.'])
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
    # 7. ledgers and cost centres that look like duplicates, and misspelt names
    kd, km = known_issues()
    dups = find_duplicates(led, ent, cca, orgs) + kd
    costcentres = read(f'{tables}/dim_costcentre.csv') if os.path.exists(f'{tables}/dim_costcentre.csv') else []
    miss = find_misspellings(led, costcentres) + km
    if dups:
        flags.append(['warn', f'{len(dups)} possible duplicate {"ledgers or cost centres" if len(dups) > 1 else "ledger or cost centre"}',
                      'The names listed below look like the same account with a typo, an abbreviation or an extra word, so postings may be '
                      'split between them. The Day Book export only contains ledgers with postings; duplicates found elsewhere are added by hand.'])
    if miss:
        flags.append(['warn', f'{len(miss)} ledger and cost-centre names are misspelt',
                      'Listed below with the probable correct spelling. Renaming them in Tally keeps reports and searches consistent.'])

    return dict(dups=dups, miss=miss,companies=co, months=months, ccs=ccs, period=period, built=dt.date.today().strftime('%-d %b %Y'),
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
