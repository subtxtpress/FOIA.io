#!/usr/bin/env python3
"""
Import scraped law enforcement agency data into FOIA.io database
"""

import sqlite3
import csv
import sys

# Increase CSV field size limit to handle large fields
csv.field_size_limit(10000000)

def import_agencies(csv_path, db_path):
    """Import agencies from CSV into state_local_agencies table."""
    
    # Connect to database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Clear existing data
    print("Clearing existing agency data...")
    cursor.execute("DELETE FROM state_local_agencies")
    
    # Read CSV and import
    print(f"Reading {csv_path}...")
    
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        count = 0
        skipped = 0
        
        for row in reader:
            try:
                # Create unique ORI by combining fips with row count
                ori = f"{row.get('fips', 'UNK')}-{count:05d}"
                
                # Truncate long fields
                website = (row.get('website') or row.get('foia_portal_url') or '')[:500]
                address = (row.get('foia_address') or '')[:500]
                
                cursor.execute("""
                    INSERT INTO state_local_agencies (
                        ori, agency_name, agency_unit, state_abbr, county_name,
                        agency_type, population_group, population,
                        division_name, region_name, officer_ct, civilian_ct,
                        total_pe_ct, data_year,
                        foia_officer, foia_email, foia_phone, foia_address, 
                        foia_portal_url, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ori,                                     # ori (unique)
                    row.get('name', '')[:200],              # agency_name
                    '',                                      # agency_unit
                    row.get('state', '')[:10],              # state_abbr
                    row.get('county', '')[:100],            # county_name
                    row.get('type', '')[:100],              # agency_type
                    '',                                      # population_group
                    row.get('ptciv', '') or row.get('ftciv', ''),  # population
                    '',                                      # division_name
                    '',                                      # region_name
                    row.get('sworn', ''),                   # officer_ct
                    '',                                      # civilian_ct
                    '',                                      # total_pe_ct
                    '2024',                                  # data_year
                    '',                                      # foia_officer
                    (row.get('foia_email') or '')[:200],    # foia_email
                    (row.get('foia_phone') or '')[:50],     # foia_phone
                    address,                                 # foia_address
                    website,                                 # foia_portal_url
                    f"Scraped {row.get('scraped_at', '')[:50]}" # notes
                ))
                count += 1
                
                if count % 100 == 0:
                    print(f"  Imported {count} agencies...")
                    
            except Exception as e:
                print(f"  Error importing {row.get('name', 'unknown')}: {e}")
                skipped += 1
                continue
    
    # Commit and close
    conn.commit()
    conn.close()
    
    print(f"\n✓ Successfully imported {count} agencies!")
    if skipped > 0:
        print(f"  Skipped {skipped} agencies due to errors")
    print(f"  Database: {db_path}")

if __name__ == '__main__':
    csv_path = '/Users/home/Documents/sites/scraper/output.csv'
    db_path = '/Users/home/Documents/sites/FOIA.io/foia_io.db'
    
    print("=" * 60)
    print("FOIA.io - Agency Data Import")
    print("=" * 60)
    
    try:
        import_agencies(csv_path, db_path)
    except FileNotFoundError as e:
        print(f"\n✗ Error: File not found - {e}")
        print("  Make sure paths are correct:")
        print(f"  CSV: {csv_path}")
        print(f"  DB:  {db_path}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
