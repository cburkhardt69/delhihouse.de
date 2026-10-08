#!/usr/bin/env python3
"""Convert TallyPrime XML exports (Day Book / masters, XML Data Interchange) into
star-schema CSV tables for a data warehouse.

Usage:  python3 tally_to_warehouse.py OUTPUT_DIR CODE=FILE.xml [CODE=FILE.xml ...]
e.g.    python3 tally_to_warehouse.py out DHS-FC=DHS-FC.xml DHS-Local=DHS-Local.xml

Sign convention in all outputs: debit = positive, credit = negative.
"""
import csv, os, re, sys
import xml.etree.ElementTree as ET

PRIMARY_NATURE = {
    'Capital Account': 'Liability', 'Loans (Liability)': 'Liability', 'Current Liabilities': 'Liability',
    'Suspense A/c': 'Liability', 'Branch / Divisions': 'Liability',
    'Fixed Assets': 'Asset', 'Investments': 'Asset', 'Current Assets': 'Asset', 'Misc. Expenses (ASSET)': 'Asset',
    'Direct Incomes': 'Income', 'Indirect Incomes': 'Income', 'Sales Accounts': 'Income',
    'Direct Expenses': 'Expense', 'Indirect Expenses': 'Expense', 'Purchase Accounts': 'Expense',
}


def load(path):
    raw = open(path, 'rb').read()
    for enc in ('utf-16', 'utf-8-sig'):
        try:
            text = raw.decode(enc)
            if '<ENVELOPE' in text[:200]:
                break
        except UnicodeError:
            continue
    # Tally emits control characters such as &#4; that are illegal in XML 1.0
    text = re.sub(r'&#(\d+);', lambda m: '' if int(m.group(1)) < 32 and int(m.group(1)) not in (9, 10, 13) else m.group(0), text)
    return ET.fromstring(text)


def amt(s):
    s = (s or '').strip()
    if not s:
        return 0.0
    s = s.split('=')[-1].split('@')[0]  # forex amounts like "-100.00 EUR @ ... = -9000.00"
    s = re.sub(r'[^0-9.\-]', '', s)
    return -float(s) + 0.0 if s not in ('', '-', '.') else 0.0  # Tally: negative = debit -> flip


def t(e, tag):
    return (e.findtext(tag) or '').strip()


def read_masters(root):
    """Groups, ledgers (with opening balance and its cost-centre split) and cost centres found in an export."""
    groups = {g.get('NAME'): t(g, 'PARENT') for g in root.iter('GROUP')}
    ledgers = {}
    for l in root.iter('LEDGER'):
        n = l.get('NAME')
        if n in ledgers:
            continue
        # Opening balance split by cost centre, stored in the ledger master when cost centres are on
        ccop = [(t(cat, 'CATEGORY'), t(cc, 'NAME'), round(amt(cc.findtext('AMOUNT')), 2))
                for cat in [l] + list(l.iter('CATEGORYALLOCATIONS.LIST')) for cc in cat.findall('COSTCENTREALLOCATIONS.LIST')]
        ledgers[n] = dict(parent=t(l, 'PARENT'), ob=round(amt(l.findtext('OPENINGBALANCE')), 2),
                          cc_on=t(l, 'ISCOSTCENTRESON'), ccop=ccop)
    costcentres = {c.get('NAME'): (t(c, 'PARENT'), t(c, 'CATEGORY')) for c in root.iter('COSTCENTRE')}
    return groups, ledgers, costcentres


def is_daybook(path):
    """A Day Book export contains vouchers; a masters export does not."""
    root = load(path)
    return root.find('.//VOUCHER') is not None


def convert(out, pairs, masters=None):
    """pairs: list of (company_code, day_book_xml_path); masters: optional {company_code: masters_xml_path}.
    Masters exports supply all ledgers with their opening balances; vouchers come from the Day Book."""
    os.makedirs(out, exist_ok=True)
    masters = masters or {}
    rows = {k: [] for k in ('company', 'group', 'ledger', 'ledger_cc_opening', 'costcentre', 'voucher', 'entry', 'cc_alloc',
                            'opening_mismatch')}
    for code, path in pairs:
        root = load(path)
        cname = t(root, './/SVCURRENTCOMPANY')
        rows['company'].append(dict(company=code, company_name=cname, fund_type='FC' if 'FC' in code.upper() else 'Local',
                                    organisation=code.split('-')[0], masters_export='Yes' if code in masters else 'No'))
        groups, ledgers, costcentres = read_masters(root)
        source = {n: 'daybook' for n in ledgers}
        if code in masters:
            mg, ml, mc = read_masters(load(masters[code]))
            groups.update(mg)
            costcentres.update(mc)
            for n, m in ml.items():
                d = ledgers.get(n)
                if d and abs(d['ob'] - m['ob']) > 1:
                    # Day Book and masters disagree: most likely "closing balance as opening balance" was on
                    rows['opening_mismatch'].append(dict(company=code, ledger=n, daybook_opening=d['ob'], masters_opening=m['ob']))
                    m = dict(m, ob=d['ob'], ccop=d['ccop'])
                ledgers[n] = m
                source[n] = 'both' if d else 'masters'
        def chain(name):
            c, seen = [], set()
            while name and name not in seen:
                seen.add(name); c.append(name); name = groups.get(name, '')
            return c[::-1]
        for g, p in groups.items():
            c = chain(g)
            rows['group'].append(dict(company=code, group_name=g, parent=p, primary_group=c[0],
                                      level2=c[1] if len(c) > 1 else '', nature=PRIMARY_NATURE.get(c[0], '')))
        for n, l in ledgers.items():
            p = l['parent']; c = chain(p) if p else ['']
            nature = PRIMARY_NATURE.get(c[0], '')
            rows['ledger'].append(dict(company=code, ledger=n, group_name=p, primary_group=c[0],
                                       level2=c[1] if len(c) > 1 else '', nature=nature,
                                       statement='Balance Sheet' if nature in ('Asset', 'Liability') else ('Income & Expenditure' if nature else ''),
                                       opening_balance=l['ob'], cost_centres_on=l['cc_on'], source=source[n]))
            for cat, cc, a in l['ccop']:
                rows['ledger_cc_opening'].append(dict(company=code, ledger=n, category=cat, cost_centre=cc, amount=a))
        for n, (p, cat) in costcentres.items():
            rows['costcentre'].append(dict(company=code, cost_centre=n, parent=p, category=cat))
        eid = 0
        for v in root.iter('VOUCHER'):
            if t(v, 'ISCANCELLED') == 'Yes' or t(v, 'ISOPTIONAL') == 'Yes' or t(v, 'ISDELETED') == 'Yes':
                continue
            guid = t(v, 'GUID'); d = t(v, 'DATE')
            date = f'{d[:4]}-{d[4:6]}-{d[6:8]}'
            rows['voucher'].append(dict(company=code, voucher_guid=guid, date=date, voucher_type=v.get('VCHTYPE') or t(v, 'VOUCHERTYPENAME'),
                                        voucher_number=t(v, 'VOUCHERNUMBER'), party=t(v, 'PARTYLEDGERNAME') or t(v, 'PARTYNAME'),
                                        narration=t(v, 'NARRATION')))
            for le in list(v.findall('ALLLEDGERENTRIES.LIST')) + list(v.findall('LEDGERENTRIES.LIST')):
                eid += 1
                a = round(amt(le.findtext('AMOUNT')), 2)
                entry_id = f'{code}:{eid}'
                rows['entry'].append(dict(company=code, entry_id=entry_id, voucher_guid=guid, date=date, month=date[:7],
                                          ledger=t(le, 'LEDGERNAME'), amount=a))
                for cat in le.findall('CATEGORYALLOCATIONS.LIST'):
                    for cc in cat.findall('COSTCENTREALLOCATIONS.LIST'):
                        rows['cc_alloc'].append(dict(company=code, entry_id=entry_id, date=date, month=date[:7],
                                                     ledger=t(le, 'LEDGERNAME'), category=t(cat, 'CATEGORY'),
                                                     cost_centre=t(cc, 'NAME'), amount=round(amt(cc.findtext('AMOUNT')), 2)))
    names = dict(company='dim_company', group='dim_group', ledger='dim_ledger', ledger_cc_opening='fact_ledger_cc_opening', opening_mismatch='check_opening_mismatch', costcentre='dim_costcentre',
                 voucher='dim_voucher', entry='fact_entry', cc_alloc='fact_costcentre_alloc')
    for k, rs in rows.items():
        if not rs:
            continue
        with open(os.path.join(out, names[k] + '.csv'), 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(rs[0].keys()))
            w.writeheader(); w.writerows(rs)
        print(f'{names[k]}: {len(rs)} rows')


if __name__ == '__main__':
    convert(sys.argv[1], [a.split('=', 1) for a in sys.argv[2:]])
