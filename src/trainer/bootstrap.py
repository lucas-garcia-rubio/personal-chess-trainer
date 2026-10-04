from fastapi import FastAPI

from trainer.adapters.sqlite import SQLiteStorage
from trainer.config import Settings
from trainer.web import create_web_app


def create_app(settings: Settings) -> FastAPI:
    storage = SQLiteStorage(settings.database_path)
    return create_web_app(storage)
