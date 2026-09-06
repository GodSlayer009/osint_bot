"""MongoDB persistence helpers for the Telegram bot."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo import ReturnDocument


INITIAL_CREDITS = 2


class OsintBot:
    """Store bot users and pending channel-join requests."""

    def __init__(self, mongo_uri: str, database_name: str) -> None:
        self.mongo_uri, self.database_name = mongo_uri, database_name
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5_000)
        database = client[database_name]
        self.users: Collection = database["users"]
        self.pending_join_requests: Collection = database["pending_join_requests"]
        self.users.create_index("telegram_id", unique=True)
        self.pending_join_requests.create_index(
            [("chat_id", 1), ("telegram_id", 1)], unique=True
        )

    def register_user(self, user: Any, referrer_id: int | None = None) -> bool:
        """Create or refresh a user record and return whether it is new."""
        now = datetime.now(timezone.utc)
        result = self.users.update_one(
            {"telegram_id": user.id},
            {
                "$set": {
                    "username": user.username,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "created_at": now,
                    "credits": INITIAL_CREDITS,
                    "referrer_id": referrer_id if referrer_id != user.id else None,
                    "referral_completed": False,
                },
            },
            upsert=True,
        )
        self.users.update_one(
            {"telegram_id": user.id, "credits": {"$exists": False}},
            {"$set": {"credits": INITIAL_CREDITS}},
        )
        return result.upserted_id is not None

    def complete_referral(self, user_id: int) -> int | None:
        """Count a referral once and return the referrer's Telegram ID."""
        referred_user = self.users.find_one_and_update(
            {
                "telegram_id": user_id,
                "referrer_id": {"$type": "number"},
                "referral_completed": {"$ne": True},
            },
            {"$set": {"referral_completed": True}},
            return_document=ReturnDocument.AFTER,
        )
        if not referred_user:
            return None

        referrer_id = referred_user["referrer_id"]
        if referrer_id == user_id:
            return None
        referrer = self.users.find_one_and_update(
            {"telegram_id": referrer_id},
            {"$inc": {"successful_referrals": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if not referrer:
            return None

        successful_referrals = int(referrer.get("successful_referrals", 0))
        if successful_referrals % 2 == 0:
            self.users.update_one(
                {"telegram_id": referrer_id},
                {"$inc": {"credits": 1, "referral_credits_earned": 1}},
            )
        return int(referrer_id)

    def referral_stats(self, user_id: int) -> tuple[int, int]:
        """Return completed referrals and credits earned from referrals."""
        user = self.users.find_one(
            {"telegram_id": user_id},
            {"successful_referrals": 1, "referral_credits_earned": 1},
        )
        if not user:
            return 0, 0
        return (
            int(user.get("successful_referrals", 0)),
            int(user.get("referral_credits_earned", 0)),
        )

    def consume_credit(self, user_id: int) -> bool:
        """Atomically deduct one credit, returning False when none remain."""
        result = self.users.update_one(
            {"telegram_id": user_id, "credits": {"$gt": 0}},
            {"$inc": {"credits": -1}},
        )
        return result.modified_count == 1

    def get_credits(self, user_id: int) -> int:
        """Return a user's current credit balance."""
        user = self.users.find_one({"telegram_id": user_id}, {"credits": 1})
        return int(user.get("credits", 0)) if user else 0

    def add_credits(self, user_id: int, amount: int) -> bool:
        """Add credits to an existing registered user."""
        result = self.users.update_one(
            {"telegram_id": user_id}, {"$inc": {"credits": amount}}
        )
        return result.matched_count == 1

    def add_credits_to_all(self, amount: int) -> int:
        """Add credits to every registered user and return their count."""
        result = self.users.update_many({}, {"$inc": {"credits": amount}})
        return result.modified_count

    def record_pending_join_request(self, chat_id: int | str, user_id: int) -> None:
        self.pending_join_requests.update_one(
            {"chat_id": chat_id, "telegram_id": user_id},
            {"$set": {"recorded_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    def has_pending_join_request(self, chat_id: int | str, user_id: int) -> bool:
        return self.pending_join_requests.find_one(
            {"chat_id": chat_id, "telegram_id": user_id}, {"_id": 1}
        ) is not None

    def clear_pending_join_request(self, chat_id: int | str, user_id: int) -> None:
        self.pending_join_requests.delete_one({"chat_id": chat_id, "telegram_id": user_id})