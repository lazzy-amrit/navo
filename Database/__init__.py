import datetime as dt
import os

from dotenv import load_dotenv
from peewee import SqliteDatabase

load_dotenv()

DATABASE_PATH = os.getenv("DATABASE_PATH", "insql.db")

db = SqliteDatabase(
    DATABASE_PATH,
    pragmas={"journal_mode": "wal", "foreign_keys": 1, "busy_timeout": 5000},
)


def utcnow():
    """Naive UTC now, matching Auth.services.config.utcnow."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
