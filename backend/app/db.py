from pymongo import MongoClient
from pymongo.database import Database


def create_client(uri: str) -> MongoClient:
    return MongoClient(uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)


def get_database(client: MongoClient, name: str) -> Database:
    return client[name]
