# delhihouse.de

Website of Delhi House e.V., served by GitHub Pages (custom domain in `CNAME`, reached through an Ionos proxy).
This repository holds the built site: `index.html`, one `index.html` per route folder (`ueber-uns/`, `kontakt/`, …)
and hashed bundles in `assets/`. Route pages use `<base href="/">` and absolute asset paths so direct URLs work
through the proxy.

## Finance dashboard (`finance/`, `_finance-dashboard/`)

- `finance/index.html` is generated. Never edit it by hand; rebuild it with `_finance-dashboard/update_dashboard.command`
  or the `/update-finance-dashboard` command.
- It is a password page: the dashboard data inside is AES-GCM encrypted. Never commit an unencrypted dashboard,
  Tally XML exports or CSV tables to this repository; the site and the repository are public. `.gitignore` blocks
  XML/CSV files under `_finance-dashboard/`.
- Data flow: Tally Day Book XML exports (four companies: DHS-FC, DHS-Local, LDI-FC, LDI-Local) in `~/Downloads`
  → `tally_to_warehouse.py` (star-schema CSVs in `_finance-dashboard/tables/`)
  → `refresh_dashboard.py` (builds `dashboard_template.html`, encrypts, moves the XML to `_finance-dashboard/archive/`)
  → `finance/index.html`. `tables/` and `archive/` hold real financial data; they are git-ignored and exist only on
  this Mac. Never `git add -f` them and never run `git clean -x` in this repository.
- The password is in the macOS Keychain under `dhs-finance-dashboard`. Do not print it or write it to any file.
- Amounts in the warehouse tables are debit-positive, credit-negative.
- Background, design decisions and Tally quirks: @_finance-dashboard/CONTEXT.md
- To change the dashboard layout or checks, edit `_finance-dashboard/dashboard_template.html` or the `build_data()`
  function in `refresh_dashboard.py`, then rebuild.
