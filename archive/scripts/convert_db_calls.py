#!/usr/bin/env python3
"""
Systematically convert all database operations in app.py to use db.py abstraction.
This script does safe, surgical replacements.
"""
import re


def convert_app_py():
    with open("app.py", "r") as f:
        content = f.read()

    print("Converting database operations...")

    # Step 1: Replace all `conn = get_db()` with `db = get_db()`
    content = re.sub(r'\bconn\s*=\s*get_db\(\)', 'db = get_db()', content)
    print("✓ Replaced conn = get_db() with db = get_db()")

    # Step 2: Remove all cursor declarations
    content = re.sub(r'\s*c\s*=\s*conn\.cursor\(\)\s*\n', '\n', content)
    content = re.sub(r'\s*c\s*=\s*db\.cursor\(\)\s*\n', '\n', content)
    print("✓ Removed cursor declarations")

    # Step 3: Replace conn.execute with db.execute
    content = re.sub(r'\bconn\.execute\(', 'db.execute(', content)
    print("✓ Replaced conn.execute with db.execute")

    # Step 4: Replace c.execute with db.execute
    content = re.sub(r'\bc\.execute\(', 'db.execute(', content)
    print("✓ Replaced c.execute with db.execute")

    # Step 5: Replace conn.commit() with db.commit()
    content = re.sub(r'\bconn\.commit\(\)', 'db.commit()', content)
    print("✓ Replaced conn.commit() with db.commit()")

    # Step 6: Replace conn.close() with db.close()
    content = re.sub(r'\bconn\.close\(\)', 'db.close()', content)
    print("✓ Replaced conn.close() with db.close()")

    # Step 7: Handle .fetchone() and .fetchall() - these should stay as-is after db.execute()
    # No changes needed - db.execute() returns a Cursor that has these methods

    # Step 8: Save the result
    with open("app.py", "w") as f:
        f.write(content)

    print("\n✓ Conversion complete!")
    print("\nMANUAL STEPS STILL REQUIRED:")
    print("1. Find and fix c.lastrowid usage (line ~1345)")
    print("2. Find and fix last_insert_rowid() usage (line ~1808)")
    print("3. Fix INSERT OR IGNORE in next_foia_number() (line ~933)")
    print("\nSearching for these patterns...")

    # Find patterns that need manual fixes
    if "c.lastrowid" in content:
        print("\n⚠️  FOUND c.lastrowid - needs manual conversion to db.insert()")
    if "last_insert_rowid" in content:
        print("⚠️  FOUND last_insert_rowid() - needs manual conversion to db.insert()")
    if "INSERT OR IGNORE" in content:
        print("⚠️  FOUND INSERT OR IGNORE - needs manual conversion to db.insert_or_ignore()")


if __name__ == "__main__":
    convert_app_py()
