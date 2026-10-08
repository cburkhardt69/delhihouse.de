# Finance dashboard (DHS & LDI)

Builds the password-protected dashboard at https://delhihouse.de/finance/ from Tally Day Book exports.
This folder starts with an underscore, so GitHub Pages (Jekyll) does not publish it.

| File | Purpose |
|---|---|
| `update_dashboard.command` | Double-click entry point: build, commit, push |
| `refresh_dashboard.py` | Finds the four exports, converts, builds, encrypts, archives |
| `tally_to_warehouse.py` | Tally XML → warehouse CSV tables (star schema) |
| `dashboard_template.html` | Dashboard page; `__DATA__` is replaced with the data |

**Data never goes into this repository.** The XML exports are read from `~/Downloads`; the CSV tables and the XML
archive are written to `tables/` and `archive/` in this folder, which `.gitignore` excludes, so they stay on this Mac only.
`finance/index.html` contains only AES-GCM-encrypted data (PBKDF2-SHA256, 600k iterations); the password lives in the
macOS Keychain under `dhs-finance-dashboard`.

## One-time setup
```bash
pip3 install --user cryptography
security add-generic-password -s dhs-finance-dashboard -a dashboard -w   # prompts for the password
```

## Monthly update
1. In Tally, export the Day Book (XML Data Interchange, financial year to date) for all four companies and download
   the files to `~/Downloads`. File names don't matter; the company is read from the file content.
   Optionally also export the masters per company (Export > Masters > Configure: type **Accounting Masters**,
   format XML, **"Export closing balance as opening balance" = No**) into the same folder. They supply every
   ledger with its opening balance and cost-centre split, so the balance sheet is complete. The build tells
   Day Book and masters files apart by content (vouchers or not) and checks the masters against the Day Book.
2. Double-click `_finance-dashboard/update_dashboard.command`, or run `/update-finance-dashboard` in Claude Code.
   The Claude Code command also refreshes the private, unencrypted copy of the dashboard on claude.ai
   (`refresh_dashboard.py --artifact-page FILE`, which refuses to write inside this repository).

Company names are mapped to codes in `COMPANIES` at the top of `refresh_dashboard.py`.
