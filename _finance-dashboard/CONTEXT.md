# Finance dashboard – background and design decisions

Background for working on the DHS & LDI finance dashboard. Worked out in a Claude chat in October 2026.
Sensitive findings (figures, compliance questions) are kept in `CLAUDE.local.md`, which is not committed.

## Organisations and Tally setup
- Two Indian NGOs, each with separate FC (foreign contribution, FCRA) and Local books in TallyPrime:
  | Code | Exact company name in Tally |
  |---|---|
  | DHS-FC | `DELHI HOUSE SOCIETY - FC NEW` |
  | DHS-Local | `DELHI HOUSE SOCIETY- LOCAL NEW` (no space before the hyphen) |
  | LDI-FC | `LOVE DELHI INITIATIVES - FC` |
  | LDI-Local | `LOVE DELHI INITIATIVES - LOCAL` |
- DHS = Delhi House Society, LDI = Love Delhi Initiatives. The dashboard groups them as DHS = DHS-FC + DHS-Local and
  LDI = LDI-FC + LDI-Local. Inter-company ledgers ("Delhi House Society(Branch) - FC", "Love Delhi Initiatives - FC",
  "DHS Local (Adjusted FCRA & Local)" …) are not eliminated in group totals.
- Financial year April–March. Cost centres are used for projects/programmes (DHS: SEWA ASHRAM - PROJECT,
  Mother & Childcare Program (LC Narela), LC BAWANA, H O Share, Love Delhi Initiatives; LDI: Operation Administration,
  Personal Administration, Youth Development Program, Social Concern, regional programmes …).
- Tally runs on a hosted cloud Windows server. Access is only through the browser (remote session) plus downloading
  files from the server's file system. Port 9000 (Tally XML/HTTP server, ODBC) is not reachable from outside.

## How data gets out of Tally (and why)
- Chosen: manual export of the Day Book per company: Display More Reports > Day Book, Alt+F2 for the period
  (financial year to date), Alt+E Export, format XML (Data Interchange); then download the four files to ~/Downloads.
- Tried / rejected:
  - FTP export from Tally: does not work in this setup; it would also be unencrypted.
  - Tally "web portal" upload (HTTP/HTTPS POST to a URL, optional user/password): possible but undocumented details;
    would need an HTTPS receiver on a small server. Not pursued for now.
  - tally-database-loader (github.com/dhananjay1405/tally-database-loader, MIT): best long-term option. Runs on the
    Tally server, pulls via Tally's XML server and loads SQL Server/MySQL/PostgreSQL/BigQuery or CSV. One company per
    run (`--tally-company`, `--database-schema`, `--tally-fromdate/--tally-todate` or `auto`); exports all masters.
    Needs the hosting provider to install Node + the loader and keep Tally running with the four companies loaded
    (TallyPrime: F1 > Settings > Startup > Load companies on startup, or `tally.exe /LOAD:<company no.>`).

## Tally XML export quirks (handled in tally_to_warehouse.py)
- Files are UTF-16 LE; contain invalid XML character references such as `&#4;` that must be stripped.
- Amounts: Tally negative = debit. The warehouse flips this: debit positive, credit negative.
- Voucher > ALLLEDGERENTRIES.LIST > CATEGORYALLOCATIONS.LIST > COSTCENTREALLOCATIONS.LIST. Cost-centre allocations are a
  level below the ledger entry; use them as the fact grain for project analysis to avoid double counting.
- A Day Book export includes only the masters referenced by its vouchers. Ledgers without movement (e.g. General Fund,
  Unrestricted Fund) and their opening balances are missing, so opening balances don't sum to zero and the balance
  sheet is incomplete; cash and bank balances are complete. "All Masters" export (Gateway > Alt+E > Masters) gives
  all ledgers with opening balances but no vouchers, and does not include cost-centre masters.
- Skip vouchers with ISCANCELLED / ISOPTIONAL / ISDELETED = Yes.
- Masters export (Export > Masters, type Accounting Masters, "Export closing balance as opening balance" = No) is
  optional per company. `find_exports()` sorts files by content: with vouchers = Day Book, without = masters.
  `convert()` merges masters into the Day Book's groups, ledgers and cost centres (column `source` in `dim_ledger`:
  daybook / masters / both). If a ledger's opening balance differs between the two, the Day Book value is kept and
  the ledger goes to `check_opening_mismatch` (sign that the closing-as-opening option was on).
  With a masters export, opening balances must sum to zero; otherwise the build flags a Tally opening difference.
- Opening cost-centre splits are read from any COSTCENTREALLOCATIONS.LIST inside a LEDGER master. As of October 2026 no
  export contained one, so check the first export after cost centres are enabled on the bank accounts.

## Warehouse tables (CSV, star schema)
`dim_company` (company, company_name, fund_type FC/Local, organisation DHS/LDI), `dim_group` (flattened hierarchy,
primary group, nature), `dim_ledger` (group, primary group, nature Asset/Liability/Income/Expense, opening balance,
cost_centres_on), `fact_ledger_cc_opening` (opening balance split by cost centre from the ledger master; only written when
Tally has such a split, e.g. for bank accounts once cost centres are switched on), `dim_costcentre`, `dim_voucher` (date, type, number, narration), `fact_entry` (one row per ledger
entry), `fact_costcentre_alloc` (one row per cost-centre allocation). Written to `_finance-dashboard/tables/`;
used XML exports are moved to `_finance-dashboard/archive/<date>/`. Both folders are git-ignored and stay local
(until October 2026 they were in a "Tally Warehouse" folder in Google Drive).

## Dashboard
- Built by `refresh_dashboard.py` from `dashboard_template.html` (`__DATA__` placeholder, vanilla JS + inline SVG,
  light/dark theme). Filters: All / DHS / LDI, the four sets of books, month; click a cost centre to see expense heads.
- Panels: KPIs (income, expenditure, surplus, cash & bank, admin cost-centre share), DHS vs LDI table, monthly
  income/expenditure, expenditure by cost centre and expense head, income by donor, cash & bank balances,
  inter-company balances, postings without cost centre, automatic "points to check".
- Automatic checks in `build_data()`: unbalanced vouchers; DHS vs LDI inter-company balances that don't mirror each
  other (per FC and Local); admin cost centres (H O Share, Operation/Personal Administration) above 20% of FC received
  (FCRA admin limit); expenses without cost centre; a bank ledger appearing in several sets of books; opening
  balances not summing to zero; possible duplicate ledgers/cost centres and misspelt names (word-level check against
  `/usr/share/dict/words`; party and staff accounts only against words used elsewhere in the books, to skip proper names).
- Issues that the Day Book export cannot show (e.g. a misspelt ledger without postings, seen in an All Masters export)
  go into `known_issues.csv` (columns `list` = duplicate|misspelt, `books`, `name`, `other`, `note`). It is git-ignored.
- Published at https://delhihouse.de/finance/ via GitHub Pages. GitHub Pages is always public, so the page is a
  password prompt; the dashboard HTML is AES-GCM encrypted (PBKDF2-SHA256, 600k iterations) and decrypted in the
  browser. Password in the macOS Keychain (`dhs-finance-dashboard`). Page carries `noindex`.
- Dependencies on the Mac: python3 and the `cryptography` package (`pip3 install --user cryptography`).

## Possible next steps
- Export All Masters for each company too and merge opening balances, for a complete balance sheet per company.
- Map projects that are only encoded in ledger names (e.g. "- Sewa Ashram", "- Circle") to cost centres.
- Move to tally-database-loader once the hosting provider allows it; the table structure is close to this one.
