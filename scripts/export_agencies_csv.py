"""
export_agencies_csv.py — Export agency tables to clean CSV files in data/

Strips unused/empty columns and exports only what the app actually uses.
Run this after any major data update to keep data/ in sync with the database.

Usage:
    python3 scripts/export_agencies_csv.py

Output:
    data/state_local_agencies.csv  (~17,979 rows)
    data/federal_agencies.csv      (~611 rows)
"""

import csv
import os
import sqlite3
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
DB_PATH = PROJECT_DIR / "foia_io.db"
DATA_DIR = PROJECT_DIR / "data"


def export_table(conn, table, columns, output_path, description):
    cols_sql = ", ".join(columns)
    cursor = conn.execute(f"SELECT {cols_sql} FROM {table} ORDER BY {columns[1]}")
    rows = cursor.fetchall()

    DATA_DIR.mkdir(exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)

    print(f"  ✓ {description}: {len(rows):,} rows → {output_path.name}")
    return len(rows)


def main():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        print("Run the app locally once to initialize the database, then try again.")
        raise SystemExit(1)

    print(f"Connecting to {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)

    print("\nExporting tables to data/...")

    # ── State & Local Agencies ─────────────────────────────────────────────────
    # Only export columns that are actually used by the app (no empty legacy columns)
    sla_columns = [
        "id",
        "agency_name",
        "agency_unit",
        "state_abbr",
        "county_name",
        "city_name",
        "agency_type",
        "foia_email",
        "foia_phone",
        "foia_address",
        "foia_portal_url",
        "website",
        "ori",
    ]
    export_table(
        conn,
        "state_local_agencies",
        sla_columns,
        DATA_DIR / "state_local_agencies.csv",
        "State & Local Agencies",
    )

    # ── Federal Agencies ───────────────────────────────────────────────────────
    fed_columns = [
        "id",
        "name",
        "abbreviation",
        "foia_officer_title",
        "foia_email",
        "foia_address",
        "foia_phone",
        "foia_fax",
        "response_days",
        "portal_url",
    ]
    export_table(
        conn,
        "federal_agencies",
        fed_columns,
        DATA_DIR / "federal_agencies.csv",
        "Federal Agencies",
    )

    conn.close()
    print("\nDone. Commit the data/ directory to make these files available on GitHub.")


if __name__ == "__main__":
    main()
