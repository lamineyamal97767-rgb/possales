# Stock-out Sales Analysis

## Overview

Stock-out Sales Analysis is a Product Intelligence report that compares sales in
a selected period with the immediately preceding equal-length period and shows
current stock and dated stock observations for each product.

The report is a review aid. A stock observation is a point-in-time fact; the
report does not claim that a stock-out caused a sales decline.

Page: `/menus/stock-out-analysis`

## Data sources and rules

| Measure | Source |
| --- | --- |
| Sales quantity | `SUM(sales_details.QTY)` grouped by product |
| Sale date | `DATE(sales.SALES_DATE)` after collapsing duplicate `sales` rows by `SALES_ID` |
| Stock for non-serial products | `SUM(stock_non_serial.STOCK_QTY)` |
| Stock for `SERIAL='Y'` products | `SUM(stock_serial.STOCK_QTY)` |
| Stock observation time | `MAX(TIMESTAMP)` in the applicable stock table |
| Daily stock history | `stock_out_daily_snapshots` |

- The default period is the latest 30 calendar days, compared with the previous
  30 days. A custom range is limited to 366 calendar days.
- Negative sales quantities (returns) are retained.
- Missing stock rows mean **Unknown**, never zero.
- Zero and negative known stock are both treated as out of stock. Negative stock
  is displayed separately.
- `Needs Restock Review` means known stock is zero or negative **and** sales
  quantity decreased versus the previous period.
- History is period-specific. Missing snapshot days are shown as Unknown.
  `History Unavailable` means no snapshots exist inside the selected period.
- Summary counts are calculated from the filtered result set before pagination.

## Database migration

The migration creates `stock_out_daily_snapshots` and moves the existing
Stock-out Analysis menu under Product Intelligence without changing its menu ID
or permission grants.

```powershell
.\venv\Scripts\python.exe -m db.migrations.migrate_stock_out_analysis --status
.\venv\Scripts\python.exe -m db.migrations.migrate_stock_out_analysis --up
```

The snapshot table uses a unique key on `(product_id, observation_date)`. The
snapshot job is retry-safe and preserves the first observation captured for a
day. It does not backdate observations, so history begins when the job first
runs.

### Rollback

Rollback drops the snapshot table, so back it up first:

```powershell
mysqldump -u root -p print_db stock_out_daily_snapshots > stock_out_snapshots_backup.sql
.\venv\Scripts\python.exe -m db.migrations.migrate_stock_out_analysis --down
```

The rollback returns the menu to Client Operations and its pre-migration URL.
To restore history, restore the SQL dump and run `--up` again.

## Daily snapshot job

Run from the project root:

```powershell
.\venv\Scripts\python.exe -m db.stock_out_snapshot_job --dry-run
.\venv\Scripts\python.exe -m db.stock_out_snapshot_job
.\venv\Scripts\python.exe -m db.stock_out_snapshot_job --json
```

`--dry-run` reads current stock and reports coverage without writing rows.
`--json` prints a machine-readable result for schedulers and log collection.

The job captures all products, including products with no stock rows. Those
products are recorded as `unknown` so missing data remains visible.

## Windows Task Scheduler

Create a daily task from an elevated PowerShell prompt:

```powershell
$root = 'C:\Users\SS\printer-data-anylis'
$python = Join-Path $root 'venv\Scripts\python.exe'
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument '-m db.stock_out_snapshot_job --json' `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At '11:55PM'
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew
Register-ScheduledTask `
    -TaskName 'Printer Analytics - Stock Snapshot' `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Capture daily stock observations for Stock-out Sales Analysis'
```

Verify the task:

```powershell
Start-ScheduledTask -TaskName 'Printer Analytics - Stock Snapshot'
Get-ScheduledTaskInfo -TaskName 'Printer Analytics - Stock Snapshot'
```

The task must run under an account that can read the project directory and the
database. Keep `WorkingDirectory` set to the project root so Python can resolve
the `db` package. If a log file is required, run the same module through an
approved wrapper or redirect output in the task action.

Scheduling guidance:

- Run after the business day is complete, for example 23:55 local time.
- Use one run per day; retries are safe.
- Do not schedule multiple concurrent instances.
- History cannot be reconstructed retroactively. Schedule the job before the
  first day for which historical comparisons are needed.

## API and export

- `GET /api/stock-out-analysis/report`
- `GET /api/stock-out-analysis/products/{product_id}`
- `GET /api/stock-out-analysis/report.xlsx`

All endpoints require authentication and the Stock-out Analysis menu permission.
The report API accepts `start_date`, `end_date`, `search`, `category_id`,
`stock_status`, `review_status`, `sort_by`, `sort_order`, `page`, and `page_size`.
`page_size` is capped at 200. Excel export uses all rows matching the filters and
keeps text values such as `=formula` as text.

## Tests

```powershell
.\venv\Scripts\python.exe -m unittest test_stock_out_analysis -v
```

Synthetic tests use a database transaction that is rolled back. Existing sales
reports, stock alerts, menu IDs, and Customer Gifts permissions must continue to
pass.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Everything shows Unknown stock | Confirm product `SERIAL` values and rows in the matching stock table |
| History Unavailable | Run the snapshot job and confirm `stock_out_daily_snapshots` has rows for the selected period |
| Task succeeds but no history | Verify task working directory and `DB_*` configuration available to the task account |
| Report returns 500 | Run `--status`, then check the application log; the API does not expose database details |
| Export returns 503 | Install `openpyxl` in the same virtual environment used by the application |
