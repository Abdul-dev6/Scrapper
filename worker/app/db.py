from pymongo import MongoClient
from pymongo.database import Database

from .config import Settings


def connect(settings: Settings) -> tuple[MongoClient, Database]:
    client = MongoClient(
        settings.mongodb_uri,
        serverSelectionTimeoutMS=8000,
        appname=f"lead-scraper-{settings.worker_id}",
        tz_aware=True,
    )
    client.admin.command("ping")  # fail fast with a clear error if the URI or IP allow-list is wrong
    return client, client[settings.mongodb_db]
