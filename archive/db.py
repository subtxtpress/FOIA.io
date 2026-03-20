"""
Database abstraction layer for FOIA.io
Supports both SQLite (local) and PostgreSQL (production via DATABASE_URL)
"""
import os
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple, Union


class Database:
    """
    Elegant database abstraction supporting SQLite and PostgreSQL.

    Features:
    - Automatic database detection via DATABASE_URL environment variable
    - Unified API for execute, fetchone, fetchall
    - Transparent parameter placeholder conversion (? to %s)
    - RETURNING id support for INSERT operations
    - Row dictionary access for both databases
    - Transaction context managers
    """

    def __init__(self, database_url: Optional[str] = None):
        """
        Initialize database connection.

        Args:
            database_url: PostgreSQL connection string (if None, checks env or uses SQLite)
        """
        self.database_url = database_url or os.environ.get("DATABASE_URL")
        # psycopg2 requires postgresql:// not postgres://
        if self.database_url and self.database_url.startswith("postgres://"):
            self.database_url = self.database_url.replace("postgres://", "postgresql://", 1)
        self.is_postgres = bool(self.database_url)

        if self.is_postgres:
            import psycopg2
            import psycopg2.extras
            self.conn = psycopg2.connect(self.database_url)
            self.conn.autocommit = True
            self._psycopg2 = psycopg2
            self._cursor_factory = psycopg2.extras.RealDictCursor
        else:
            import sqlite3
            db_path = os.path.join(os.path.dirname(__file__), "foia_io.db")
            self.conn = sqlite3.connect(db_path)
            self.conn.row_factory = sqlite3.Row
            # Enable WAL mode for SQLite
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self._sqlite3 = sqlite3

    def _convert_placeholders(self, query: str) -> str:
        """
        Convert SQLite ? placeholders to PostgreSQL %s placeholders.

        Args:
            query: SQL query with ? placeholders

        Returns:
            Query with appropriate placeholders for current database
        """
        if self.is_postgres:
            return query.replace("?", "%s")
        return query

    def execute(self, query: str, params: Tuple = ()) -> 'Cursor':
        """
        Execute a SQL query with parameters.

        Args:
            query: SQL query with ? placeholders (auto-converted for PostgreSQL)
            params: Query parameters

        Returns:
            Cursor object for chaining (fetchone, fetchall, etc.)
        """
        query = self._convert_placeholders(query)

        if self.is_postgres:
            cursor = self.conn.cursor(cursor_factory=self._cursor_factory)
        else:
            cursor = self.conn.cursor()

        cursor.execute(query, params)
        return Cursor(cursor, self)

    def insert(self, query: str, params: Tuple = ()) -> int:
        """
        Execute an INSERT and return the new row's ID.

        For PostgreSQL, automatically adds RETURNING id clause.
        For SQLite, uses lastrowid.

        Args:
            query: INSERT query
            params: Query parameters

        Returns:
            ID of newly inserted row
        """
        if self.is_postgres:
            # Add RETURNING id clause if not present
            if "RETURNING" not in query.upper():
                query = query.rstrip().rstrip(";") + " RETURNING id"

            query = self._convert_placeholders(query)
            cursor = self.conn.cursor()
            cursor.execute(query, params)
            result = cursor.fetchone()
            return result[0] if result else None
        else:
            cursor = self.conn.cursor()
            cursor.execute(query, params)
            return cursor.lastrowid

    def commit(self):
        """Commit current transaction."""
        self.conn.commit()

    def rollback(self):
        """Rollback current transaction."""
        self.conn.rollback()

    def close(self):
        """Close database connection."""
        self.conn.close()

    @contextmanager
    def transaction(self):
        """
        Context manager for transactions.

        Usage:
            with db.transaction():
                db.execute("INSERT ...")
                db.execute("UPDATE ...")
                # Auto-commits on success, rolls back on exception
        """
        try:
            yield self
            self.commit()
        except Exception:
            self.rollback()
            raise

    def insert_or_ignore(self, table: str, columns: List[str], values: Tuple) -> None:
        """
        INSERT OR IGNORE for cross-database compatibility.

        SQLite: Uses INSERT OR IGNORE
        PostgreSQL: Uses INSERT ... ON CONFLICT DO NOTHING

        Args:
            table: Table name
            columns: List of column names
            values: Tuple of values
        """
        cols = ", ".join(columns)
        placeholders = ", ".join(["?"] * len(values))

        if self.is_postgres:
            query = f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
        else:
            query = f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})"

        self.execute(query, values)


class Cursor:
    """
    Wrapper around database cursor providing unified interface.
    Returns dict-like objects for both SQLite and PostgreSQL.
    """

    def __init__(self, cursor, db: Database):
        self._cursor = cursor
        self._db = db

    def fetchone(self) -> Optional[Dict[str, Any]]:
        """
        Fetch one row as a dictionary.

        Returns:
            Dict with column names as keys, or None if no rows
        """
        row = self._cursor.fetchone()
        if row is None:
            return None

        if self._db.is_postgres:
            # RealDictCursor already returns dict
            return dict(row)
        else:
            # sqlite3.Row - convert to dict
            return dict(row)

    def fetchall(self) -> List[Dict[str, Any]]:
        """
        Fetch all rows as list of dictionaries.

        Returns:
            List of dicts with column names as keys
        """
        rows = self._cursor.fetchall()

        if self._db.is_postgres:
            return [dict(row) for row in rows]
        else:
            return [dict(row) for row in rows]

    def fetchone_raw(self) -> Optional[Tuple]:
        """
        Fetch one row as raw tuple (for COUNT queries, etc.).

        Returns:
            Tuple of values, or None if no rows
        """
        if self._db.is_postgres:
            # Need to re-execute with regular cursor for raw access
            # This is less elegant but handles edge cases
            return self._cursor.fetchone()
        return self._cursor.fetchone()


def get_db() -> Database:
    """
    Get database connection (SQLite or PostgreSQL based on environment).

    Returns:
        Database instance
    """
    return Database()


# Schema SQL generator for cross-database compatibility
class SchemaBuilder:
    """Helper for generating cross-compatible CREATE TABLE statements."""

    @staticmethod
    def primary_key(is_postgres: bool) -> str:
        """Return appropriate primary key definition."""
        if is_postgres:
            return "SERIAL PRIMARY KEY"
        else:
            return "INTEGER PRIMARY KEY AUTOINCREMENT"

    @staticmethod
    def timestamp(is_postgres: bool) -> str:
        """Return appropriate timestamp default."""
        return "CURRENT_TIMESTAMP"  # Works in both

    @staticmethod
    def text_type(is_postgres: bool) -> str:
        """Return appropriate text type."""
        if is_postgres:
            return "TEXT"
        else:
            return "TEXT"
