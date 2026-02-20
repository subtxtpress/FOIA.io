"""
seed_state_local_agencies.py — Import state & local law enforcement agencies
from the FBI Law Enforcement Employees CSV (lee_1960_2024.csv) into FOIA.io.

Filters to the most recent year (2024) to get the current active agency roster.
Uses your existing db.py abstraction — works locally (SQLite) and on Railway (Postgres).

Usage:
    python3 seed_state_local_agencies.py
    python3 seed_state_local_agencies.py --csv ~/Downloads/lee_1960_2024.csv
    python3 seed_state_local_agencies.py --year 2023  # use a different year
"""

import csv
import os
import sys
import argparse
from pathlib import Path

# ── Allow running from any directory ─────────────────────────────────────────
script_dir = Path(__file__).parent
sys.path.insert(0, str(script_dir))

from db import get_db


# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_CSV = Path.home() / "Downloads" / "lee_1960_2024.csv"
DEFAULT_YEAR = "2024"


# ── Schema ────────────────────────────────────────────────────────────────────
CREATE_TABLE_SQLITE = """
CREATE TABLE IF NOT EXISTS state_local_agencies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ori                 TEXT UNIQUE,
    agency_name         TEXT NOT NULL,
    agency_unit         TEXT,
    state_abbr          TEXT NOT NULL,
    county_name         TEXT,
    city_name           TEXT,
    agency_type         TEXT,
    population          INTEGER,
    foia_officer        TEXT,
    foia_email          TEXT,
    foia_phone          TEXT,
    foia_address        TEXT,
    foia_portal_url     TEXT,
    website             TEXT,
    notes               TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_TABLE_POSTGRES = """
CREATE TABLE IF NOT EXISTS state_local_agencies (
    id                  SERIAL PRIMARY KEY,
    ori                 TEXT UNIQUE,
    agency_name         TEXT NOT NULL,
    agency_unit         TEXT,
    state_abbr          TEXT NOT NULL,
    county_name         TEXT,
    city_name           TEXT,
    agency_type         TEXT,
    population          INTEGER,
    foia_officer        TEXT,
    foia_email          TEXT,
    foia_phone          TEXT,
    foia_address        TEXT,
    foia_portal_url     TEXT,
    website             TEXT,
    notes               TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sla_state ON state_local_agencies(state_abbr);",
    "CREATE INDEX IF NOT EXISTS idx_sla_agency_type ON state_local_agencies(agency_type);",
    "CREATE INDEX IF NOT EXISTS idx_sla_county ON state_local_agencies(county_name);",
]


def safe_int(val):
    """Convert a value to int, return None if empty or invalid."""
    try:
        return int(float(val)) if val and val.strip() else None
    except (ValueError, TypeError):
        return None


def clean(val):
    """Strip whitespace, return None for NULL/empty strings."""
    if not val or val.strip().upper() in ("NULL", ""):
        return None
    return val.strip()


def create_schema(db):
    """Create the state_local_agencies table if it doesn't exist."""
    print("Creating schema...")

    if db.is_postgres:
        db.execute(CREATE_TABLE_POSTGRES)
    else:
        db.execute(CREATE_TABLE_SQLITE)

    for idx_sql in CREATE_INDEXES:
        db.execute(idx_sql)

    db.commit()
    print("  ✓ Table and indexes ready")


def import_csv(db, csv_path: Path, year: str):
    """
    Read the FBI LEE CSV, filter to the target year,
    and upsert into state_local_agencies.
    """
    print(f"\nReading {csv_path.name} for year {year}...")

    if not csv_path.exists():
        print(f"\n  ✗ File not found: {csv_path}")
        print("  Pass the correct path with --csv /path/to/lee_1960_2024.csv")
        sys.exit(1)

    inserted = 0
    updated = 0
    skipped = 0
    total_read = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        for row in reader:
            # Filter to target year only
            if row.get("data_year", "").strip() != year:
                continue

            total_read += 1
            ori = clean(row.get("ori", ""))
            if not ori:
                skipped += 1
                continue

            agency_name = clean(row.get("pub_agency_name", "")) or "Unknown"

            values = (
                ori,
                agency_name,
                clean(row.get("pub_agency_unit")),
                clean(row.get("state_abbr", "")) or "",
                clean(row.get("county_name")),
                clean(row.get("agency_type_name")),
                clean(row.get("population_group_desc")),
                safe_int(row.get("population")),
                clean(row.get("division_name")),
                clean(row.get("region_name")),
                safe_int(row.get("officer_ct")),
                safe_int(row.get("civilian_ct")),
                safe_int(row.get("total_pe_ct")),
                safe_int(row.get("data_year")),
            )

            # Upsert: insert or update on ORI conflict
            if db.is_postgres:
                upsert_sql = """
                    INSERT INTO state_local_agencies
                        (ori, agency_name, agency_unit, state_abbr, county_name,
                         agency_type, population_group, population, division_name,
                         region_name, officer_ct, civilian_ct, total_pe_ct, data_year)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ori) DO UPDATE SET
                        agency_name      = EXCLUDED.agency_name,
                        agency_unit      = EXCLUDED.agency_unit,
                        state_abbr       = EXCLUDED.state_abbr,
                        county_name      = EXCLUDED.county_name,
                        agency_type      = EXCLUDED.agency_type,
                        population_group = EXCLUDED.population_group,
                        population       = EXCLUDED.population,
                        division_name    = EXCLUDED.division_name,
                        region_name      = EXCLUDED.region_name,
                        officer_ct       = EXCLUDED.officer_ct,
                        civilian_ct      = EXCLUDED.civilian_ct,
                        total_pe_ct      = EXCLUDED.total_pe_ct,
                        data_year        = EXCLUDED.data_year,
                        updated_at       = CURRENT_TIMESTAMP
                    RETURNING (xmax = 0) AS was_inserted
                """
                cursor = db.execute(upsert_sql, values)
                result = cursor.fetchone()
                if result and result.get("was_inserted"):
                    inserted += 1
                else:
                    updated += 1

            else:
                # SQLite: INSERT OR REPLACE
                upsert_sql = """
                    INSERT OR REPLACE INTO state_local_agencies
                        (ori, agency_name, agency_unit, state_abbr, county_name,
                         agency_type, population_group, population, division_name,
                         region_name, officer_ct, civilian_ct, total_pe_ct, data_year)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                db.execute(upsert_sql, values)
                inserted += 1

            # Commit in batches of 500 to avoid locking issues
            if (inserted + updated) % 500 == 0:
                db.commit()
                print(f"  ... {inserted + updated} processed", end="\r")

    db.commit()
    return total_read, inserted, updated, skipped


def print_summary(db):
    """Print a quick breakdown of what's in the table."""
    print("\n── Summary ──────────────────────────────────────────────")

    total = db.execute("SELECT COUNT(*) as n FROM state_local_agencies").fetchone()
    print(f"  Total agencies:  {total['n']:,}")

    by_type = db.execute("""
        SELECT agency_type, COUNT(*) as n
        FROM state_local_agencies
        GROUP BY agency_type
        ORDER BY n DESC
        LIMIT 8
    """).fetchall()

    print("\n  By agency type:")
    for row in by_type:
        t = row["agency_type"] or "Unknown"
        print(f"    {t:<35} {row['n']:>5,}")

    by_state = db.execute("""
        SELECT state_abbr, COUNT(*) as n
        FROM state_local_agencies
        GROUP BY state_abbr
        ORDER BY n DESC
        LIMIT 5
    """).fetchall()

    print("\n  Top 5 states by agency count:")
    for row in by_state:
        print(f"    {row['state_abbr']}  {row['n']:,}")

    print("─────────────────────────────────────────────────────────\n")


def main():
    parser = argparse.ArgumentParser(description="Seed state & local agencies from FBI LEE CSV")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV,
                        help=f"Path to lee_1960_2024.csv (default: {DEFAULT_CSV})")
    parser.add_argument("--year", default=DEFAULT_YEAR,
                        help=f"Year to import (default: {DEFAULT_YEAR})")
    parser.add_argument("--drop", action="store_true",
                        help="Drop and recreate the table before importing (fresh seed)")
    args = parser.parse_args()

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("  FOIA.io — State & Local Agency Seeder")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")

    db = get_db()
    mode = "PostgreSQL (Railway)" if db.is_postgres else "SQLite (local)"
    print(f"  Database: {mode}")
    print(f"  CSV:      {args.csv}")
    print(f"  Year:     {args.year}\n")

    # Optional: drop table for a clean re-seed
    if args.drop:
        print("  ⚠ Dropping existing table...")
        db.execute("DROP TABLE IF EXISTS state_local_agencies")
        db.commit()

    create_schema(db)
    total_read, inserted, updated, skipped = import_csv(db, args.csv, args.year)

    print(f"\n  ✓ Done!")
    print(f"    Rows read:    {total_read:,}")
    print(f"    Inserted:     {inserted:,}")
    print(f"    Updated:      {updated:,}")
    print(f"    Skipped:      {skipped:,}")

    print_summary(db)
    db.close()


if __name__ == "__main__":
    main()
