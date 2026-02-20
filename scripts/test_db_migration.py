#!/usr/bin/env python3
"""
Test suite for database migration
Verifies that both SQLite and PostgreSQL operations work correctly
"""
from db import get_db


def test_connection():
    """Test database connection"""
    print("Testing database connection...")
    db = get_db()
    db_type = "PostgreSQL" if db.is_postgres else "SQLite"
    print(f"✓ Connected to {db_type}")
    db.close()
    return True


def test_schema():
    """Test that all tables exist"""
    print("\nTesting schema...")
    tables = [
        "users",
        "foia_sequence",
        "federal_agencies",
        "requests",
        "action_log",
        "state_laws",
        "request_attachments"
    ]

    db = get_db()

    if db.is_postgres:
        query = """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
        """
    else:
        query = """
            SELECT name as table_name
            FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
        """

    existing_tables = [row['table_name'] for row in db.execute(query).fetchall()]
    db.close()

    for table in tables:
        if table in existing_tables:
            print(f"  ✓ Table '{table}' exists")
        else:
            print(f"  ✗ Table '{table}' MISSING")
            return False

    return True


def test_seed_data():
    """Test that seed data was loaded"""
    print("\nTesting seed data...")
    db = get_db()

    # Test agencies
    agencies = db.execute("SELECT COUNT(*) as cnt FROM federal_agencies").fetchone()
    agency_count = agencies['cnt']
    print(f"  ✓ Federal agencies: {agency_count}")

    # Test states
    states = db.execute("SELECT COUNT(*) as cnt FROM state_laws").fetchone()
    state_count = states['cnt']
    print(f"  ✓ State laws: {state_count}")

    db.close()

    return agency_count > 0 and state_count > 0


def test_insert_with_return_id():
    """Test INSERT operations that return ID"""
    print("\nTesting INSERT with ID return...")
    db = get_db()

    try:
        # Insert a test user
        user_id = db.insert("""
            INSERT INTO users (username, email, password, subscription_status)
            VALUES (?, ?, ?, ?)
        """, ("test_user_migration", "test@migration.test", "hashed_pw", "active"))

        print(f"  ✓ Inserted user with ID: {user_id}")

        # Clean up
        db.execute("DELETE FROM users WHERE id=?", (user_id,))
        db.commit()
        print(f"  ✓ Cleaned up test user")

        db.close()
        return user_id is not None

    except Exception as e:
        print(f"  ✗ Error: {e}")
        db.close()
        return False


def test_dict_row_access():
    """Test that rows can be accessed as dicts"""
    print("\nTesting dict-style row access...")
    db = get_db()

    row = db.execute("""
        SELECT name, abbreviation, foia_email
        FROM federal_agencies
        LIMIT 1
    """).fetchone()

    if row:
        print(f"  ✓ Row access: name='{row['name']}', abbr='{row['abbreviation']}'")
        db.close()
        return True
    else:
        print(f"  ✗ No rows returned")
        db.close()
        return False


def test_parameter_placeholders():
    """Test that parameter placeholders are converted correctly"""
    print("\nTesting parameter placeholder conversion...")
    db = get_db()

    # This should work with ? placeholders (auto-converted to %s for PostgreSQL)
    row = db.execute("""
        SELECT name FROM federal_agencies WHERE id=? LIMIT 1
    """, (1,)).fetchone()

    if row:
        print(f"  ✓ Placeholder conversion works: {row['name']}")
        db.close()
        return True
    else:
        print(f"  ✗ Query failed")
        db.close()
        return False


def run_all_tests():
    """Run all tests"""
    print("=" * 60)
    print("  FOIA.io Database Migration Test Suite")
    print("=" * 60)

    tests = [
        test_connection,
        test_schema,
        test_seed_data,
        test_dict_row_access,
        test_parameter_placeholders,
        test_insert_with_return_id,
    ]

    passed = 0
    failed = 0

    for test_func in tests:
        try:
            if test_func():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  ✗ Exception: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    if failed == 0:
        print("\n🎉 All tests passed! Migration successful!")
        return 0
    else:
        print(f"\n⚠️  {failed} test(s) failed. Please review the errors above.")
        return 1


if __name__ == "__main__":
    exit(run_all_tests())
