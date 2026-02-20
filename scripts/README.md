# FOIA.io Scripts

Utility and data scripts. These are not part of the application — they're run manually by developers as needed.

---

## Data Scripts

### `fetch_agencies.py`
Fetches federal agency FOIA contact info from the public [api.foia.gov](https://api.foia.gov) API and writes it to `federal_agencies_seed_data.py`.

**Requires:** A free FOIA.gov API key
**Run:** `python3 scripts/fetch_agencies.py YOUR_API_KEY`
**Output:** `scripts/federal_agencies_seed_data.py` (re-generates the seed list embedded in `app.py`)

---

### `federal_agencies_seed_data.py`
Auto-generated Python file containing the `AGENCIES` list — 611 federal agency FOIA contacts sourced from api.foia.gov. This data is embedded directly in `app.py` and seeded into the `federal_agencies` table on first run.

**Do not edit manually** — regenerate with `fetch_agencies.py`.

---

### `seed_state_local_agencies.py`
Imports state & local law enforcement agencies from the FBI Law Enforcement Employees (LEE) CSV into the `state_local_agencies` table.

**Run:** `python3 scripts/seed_state_local_agencies.py --csv ~/Downloads/lee_1960_2024.csv`
**Options:** `--year 2024`, `--drop` (drops and recreates table first)

---

### `import_scraped_agencies.py`
One-time importer for scraped agency data (from the FOIA.io web scraper project). Used to populate `state_local_agencies` with FOIA-specific contact info (email, phone, address, portal URL) that the FBI CSV doesn't include.

**Input:** `/Users/home/Documents/sites/scraper/output.csv`

---

### `export_agencies_csv.py`
Exports both agency tables from the local SQLite database to clean CSV files in `data/`. Strips unused columns. Run this after any major data update to keep the `data/` directory in sync.

**Run:** `python3 scripts/export_agencies_csv.py`
**Output:** `data/state_local_agencies.csv`, `data/federal_agencies.csv`

---

## Migration Scripts (completed — kept for reference)

### `migrate_db.py`
One-time migration scaffolding that converted `app.py` from raw SQLite calls to the `db.py` abstraction layer (enabling both SQLite and PostgreSQL support). **Already applied.**

### `convert_db_calls.py`
Surgical find-and-replace script that updated database call patterns in `app.py` during the SQLite→PostgreSQL migration. **Already applied.**

### `test_db_migration.py`
Test suite that verified the database migration was successful. **Migration is complete.**

### `MIGRATION_COMPLETE.md`
Documentation of the migration from SQLite-only to dual SQLite/PostgreSQL architecture.
