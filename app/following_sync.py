from __future__ import annotations

from typing import Any


def sync_items_trusted(items: object) -> bool:
    return bool(getattr(items, "trusted", True))


def sync_items_skipped_reason(items: object) -> str | None:
    reason = getattr(items, "skipped_reason", None)
    return str(reason) if reason else None


async def store_provider_following(db: Any, source_type: str, items: list[dict]) -> dict:
    if not sync_items_trusted(items):
        return {
            "synced": 0,
            "trusted": False,
            "skippedReason": sync_items_skipped_reason(items) or "Following sync skipped",
        }

    synced_usernames = set()
    for item in items or []:
        username = item.get("username")
        if not username:
            continue
        thumbnail = item.get("thumbnail_url") or item.get("thumbnail")
        is_online = bool(item.get("is_online", item.get("isOnline", False)))
        if source_type == "chaturbate" and not is_online and thumbnail and "roomimg.stream.highwebmedia.com" in thumbnail:
            thumbnail = None
        await db.upsert_followed_model(
            username=username,
            display_name=item.get("display_name") or username,
            is_online=is_online,
            viewers=int(item.get("viewers") or 0),
            thumbnail_url=thumbnail,
            source_type=source_type,
            room_status=item.get("room_status") or item.get("roomStatus"),
        )
        synced_usernames.add(username)

    await db.remove_unfollowed(synced_usernames, source_type=source_type)
    return {"synced": len(synced_usernames), "trusted": True, "skippedReason": None}
