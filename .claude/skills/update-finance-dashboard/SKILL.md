---
name: update-finance-dashboard
description: Rebuild and publish the DHS & LDI finance dashboard (delhihouse.de/finance) from the Tally Day Book exports in ~/Downloads
disable-model-invocation: true
---

Update the finance dashboard at https://delhihouse.de/finance/.

1. List the Tally XML files in ~/Downloads and confirm that exports for all four companies are there
   (DHS-FC, DHS-Local, LDI-FC, LDI-Local; the company is identified from `<SVCURRENTCOMPANY>` inside each file).
   If one is missing, stop and say which.
2. Build without pushing: `PUSH=no _finance-dashboard/update_dashboard.command`
3. Report in a few lines: the period covered and the "points to check" listed in the script output.
4. Check that `git status` shows only `finance/index.html` changed and that no XML or CSV file is staged.
5. Ask me before pushing. When I confirm, run `git push`.
