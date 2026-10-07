"""MongoDB persistence helpers for the Telegram bot."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo import ReturnDocument


INITIAL_CREDITS = 2


class OsintBot:
    """Store bot users, protected numbers, and join requests."""

    def __init__(self, mongo_uri: str, database_name: str) -> None:
        self.mongo_uri, self.database_name = mongo_uri, database_name
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5_000)
        database = client[database_name]
        self.users: Collection = database["users"]
        self.protected_numbers: Collection = database["protected_numbers"]
        self.pending_join_requests: Collection = database["pending_join_requests"]
        self.users.create_index("telegram_id", unique=True)
        self.protected_numbers.create_index("number", unique=True)
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
        """Use a credit unless the user has an active premium subscription."""
        if self.is_premium(user_id):
            return True
        result = self.users.update_one(
            {"telegram_id": user_id, "credits": {"$gt": 0}},
            {"$inc": {"credits": -1}},
        )
        return result.modified_count == 1

    def is_premium(self, user_id: int) -> bool:
        """Return whether a subscription is active at the current UTC time."""
        return self.users.find_one(
            {
                "telegram_id": user_id,
                "premium_expires_at": {"$gte": datetime.now(timezone.utc)},
            },
            {"_id": 1},
        ) is not None

    def premium_expiry(self, user_id: int) -> datetime | None:
        """Return a user's stored premium expiry timestamp."""
        user = self.users.find_one(
            {"telegram_id": user_id}, {"premium_expires_at": 1}
        )
        expiry = user.get("premium_expires_at") if user else None
        return expiry if isinstance(expiry, datetime) else None

    def set_premium(self, user_id: int, expires_at: datetime) -> None:
        """Set a subscription expiry without changing the user's saved credits."""
        self.users.update_one(
            {"telegram_id": user_id},
            {
                "$set": {"premium_expires_at": expires_at},
                "$setOnInsert": {
                    "created_at": datetime.now(timezone.utc),
                    "credits": INITIAL_CREDITS,
                    "referral_completed": False,
                },
            },
            upsert=True,
        )

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

    def protect_number(self, number: str, protected_by: int) -> bool:
        """Save a phone number that must not be returned by number lookups."""
        result = self.protected_numbers.update_one(
            {"number": number},
            {
                "$setOnInsert": {
                    "number": number,
                    "protected_by": protected_by,
                    "protected_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
        return result.upserted_id is not None

    def is_protected_number(self, number: str) -> bool:
        """Return whether a phone number is blocked from lookup results."""
        return self.protected_numbers.find_one({"number": number}, {"_id": 1}) is not None

    def user_ids(self) -> list[int]:
        """Return Telegram IDs for every user registered with the bot."""
        return [
            int(user["telegram_id"])
            for user in self.users.find({}, {"telegram_id": 1})
            if isinstance(user.get("telegram_id"), int)
        ]

    def find_user(self, identifier: str) -> dict[str, Any] | None:
        """Find a user by Telegram ID or saved username (with or without @)."""
        value = identifier.strip().lstrip("@")
        query: dict[str, Any]
        if value.isdigit():
            query = {"telegram_id": int(value)}
        else:
            query = {"username": {"$regex": f"^{re.escape(value)}$", "$options": "i"}}
        return self.users.find_one(query, {"_id": 0})

    def set_credits(self, user_id: int, amount: int) -> bool:
        result = self.users.update_one({"telegram_id": user_id}, {"$set": {"credits": amount}})
        return result.matched_count == 1

    def set_credits_minimum(self, amount: int) -> int:
        return self.users.update_many({"credits": {"$lt": amount}}, {"$set": {"credits": amount}}).modified_count

    def remove_premium(self, user_id: int) -> bool:
        result = self.users.update_one({"telegram_id": user_id}, {"$unset": {"premium_expires_at": ""}})
        return result.matched_count == 1

    def premium_users(self) -> list[dict[str, Any]]:
        return list(self.users.find({"premium_expires_at": {"$gte": datetime.now(timezone.utc)}}, {"_id": 0, "telegram_id": 1, "username": 1, "first_name": 1, "premium_expires_at": 1}).sort("premium_expires_at", 1))

    def block_user(self, user_id: int) -> bool:
        result = self.users.update_one({"telegram_id": user_id}, {"$set": {"blocked": True}})
        return result.matched_count == 1

    def unblock_user(self, user_id: int) -> bool:
        result = self.users.update_one({"telegram_id": user_id}, {"$unset": {"blocked": ""}})
        return result.matched_count == 1

    def is_blocked(self, user_id: int) -> bool:
        return self.users.find_one({"telegram_id": user_id, "blocked": True}, {"_id": 1}) is not None

    def blocked_users(self) -> list[dict[str, Any]]:
        return list(self.users.find({"blocked": True}, {"_id": 0, "telegram_id": 1, "username": 1, "first_name": 1}))

    def statistics(self) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        return {
            "users": self.users.count_documents({}),
            "premium": self.users.count_documents({"premium_expires_at": {"$gte": now}}),
            "blocked": self.users.count_documents({"blocked": True}),
            "credits": int(self.users.aggregate([{"$group": {"_id": None, "total": {"$sum": "$credits"}}}]).next().get("total", 0)) if self.users.count_documents({"credits": {"$exists": True}}) else 0,
        }

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
