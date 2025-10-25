"""
Small one-off migration: add `user_id` column to the `transcripts` table if it doesn't exist.
Supports SQLite and Postgres.
Run from the repository root: python backend/add_userid_column.py
"""
import os
from sqlalchemy import create_engine, text, inspect
from dotenv import load_dotenv

load_dotenv()

def main():
    database_url = os.getenv("DATABASE_URL", "sqlite:///transcripts.db")
    print(f"Using DATABASE_URL={database_url}")
    engine = create_engine(database_url)
    insp = inspect(engine)
    if not insp.has_table("transcripts"):
        print("Table `transcripts` does not exist. Nothing to do.")
        return
    cols = [c["name"] for c in insp.get_columns("transcripts")]
    print("Existing columns:", cols)
    if "user_id" in cols:
        print("Column `user_id` already exists. Nothing to do.")
        return

    dialect = engine.dialect.name
    print("Database dialect:", dialect)
    try:
        with engine.connect() as conn:
            if dialect == "sqlite":
                # SQLite supports ADD COLUMN but has some limitations; adding a nullable text column is fine
                conn.execute(text('ALTER TABLE transcripts ADD COLUMN user_id TEXT'))
                print("Added column `user_id` (TEXT) to `transcripts` (sqlite)")
            else:
                # Postgres and others: use IF NOT EXISTS when supported
                conn.execute(text('ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS user_id TEXT'))
                print("Added column `user_id` (TEXT) to `transcripts`")
    except Exception as e:
        print("Failed to add column:", e)

if __name__ == '__main__':
    main()
