#!/usr/bin/env python3
"""
Database migration script for FOIA.io
Converts app.py from SQLite to cross-compatible SQLite/PostgreSQL using db.py abstraction
"""
import re
import sys


def migrate_app_py(input_file="app.py", output_file="app_migrated.py"):
    """
    Migrate app.py to use the new database abstraction layer.

    Transformations:
    1. Replace sqlite3 import with db module import
    2. Replace get_db() function
    3. Replace all conn.execute() with db.execute()
    4. Replace all c.lastrowid with db.insert()
    5. Replace last_insert_rowid() with db.insert()
    6. Update init_db() to use cross-compatible schema
    7. Replace INSERT OR IGNORE with db.insert_or_ignore()
    """

    with open(input_file, 'r') as f:
        content = f.read()

    print("Starting migration...")

    # Step 1: Update imports
    print("1. Updating imports...")
    content = content.replace(
        "import sqlite3",
        "from db import get_db, Database"
    )

    # Step 2: Replace get_db() function
    print("2. Replacing get_db() function...")
    old_get_db = """def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn"""

    # The new get_db() is imported from db.py, so we remove the old one
    content = content.replace(old_get_db, "# get_db() now imported from db.py")

    # Step 3: Replace INTEGER PRIMARY KEY AUTOINCREMENT in init_db()
    print("3. Updating schema for cross-database compatibility...")
    content = re.sub(
        r'INTEGER PRIMARY KEY AUTOINCREMENT',
        r'BIGSERIAL PRIMARY KEY',  # Works in PostgreSQL, we'll handle SQLite in db.py
        content
    )

    # Step 4: Handle INSERT OR IGNORE pattern
    print("4. Updating INSERT OR IGNORE patterns...")
    # This is complex, will need manual intervention for the FOIA sequence

    # Step 5: Replace conn = get_db() pattern with db = get_db()
    print("5. Updating variable names from conn to db...")
    content = re.sub(
        r'\bconn\s*=\s*get_db\(\)',
        'db = get_db()',
        content
    )

    # Step 6: Replace conn.execute with db.execute
    print("6. Replacing conn.execute with db.execute...")
    content = re.sub(
        r'\bconn\.execute\(',
        'db.execute(',
        content
    )

    # Step 7: Replace c.execute with db.execute (cursor execute)
    print("7. Replacing c.execute with db.execute...")
    content = re.sub(
        r'\bc\.execute\(',
        'db.execute(',
        content
    )

    # Step 8: Replace conn.commit() with db.commit()
    print("8. Replacing conn.commit() with db.commit()...")
    content = re.sub(
        r'\bconn\.commit\(\)',
        'db.commit()',
        content
    )

    # Step 9: Replace conn.close() with db.close()
    print("9. Replacing conn.close() with db.close()...")
    content = re.sub(
        r'\bconn\.close\(\)',
        'db.close()',
        content
    )

    # Step 10: Remove cursor declarations
    print("10. Removing cursor declarations...")
    content = re.sub(
        r'\s*c\s*=\s*conn\.cursor\(\)\s*\n',
        '',
        content
    )
    content = re.sub(
        r'\s*cursor\s*=\s*conn\.cursor\(\)\s*\n',
        '',
        content
    )

    # Step 11: Handle c.lastrowid
    print("11. Handling c.lastrowid...")
    # This requires manual attention as it needs context

    # Step 12: Remove DATABASE constant (no longer needed)
    print("12. Updating DATABASE constant...")
    content = re.sub(
        r'DATABASE\s*=\s*os\.path\.join\([^)]+\)',
        '# DATABASE path now handled by db.py',
        content
    )

    with open(output_file, 'w') as f:
        f.write(content)

    print(f"\nMigration complete! Output written to {output_file}")
    print("\nMANUAL STEPS REQUIRED:")
    print("1. Review and update init_db() CREATE TABLE statements")
    print("2. Find and replace c.lastrowid with db.insert() calls")
    print("3. Find and replace last_insert_rowid() calls")
    print("4. Update INSERT OR IGNORE in next_foia_number()")
    print("5. Test all database operations")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        migrate_app_py(sys.argv[1])
    else:
        migrate_app_py()
