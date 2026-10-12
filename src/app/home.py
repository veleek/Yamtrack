from django.utils import timezone
from django.utils.text import slugify

from app.models import BasicMedia, MediaTypes, Status
from events.models import SentinelDatetime

UP_NEXT_LIMIT = 12


def build_home_section(key, media_types):
    """Build home section payload."""
    return {
        "key": key,
        "id": slugify(key),
        "media_types": media_types,
        "count": sum(media_list["total"] for media_list in media_types.values()),
    }


def is_active_in_progress_media(media):
    """Return True when media still has released backlog."""
    if not _is_incoming_media(media):
        return True

    return media.max_progress is not None and media.progress < media.max_progress


def get_home_media_types(
    request,
    sort_by,
    section_key,
    items_limit,
    specific_media_type=None,
    *,
    hide_unreleased=False,
):
    """Return media types for a home section, or one type's load-more page."""
    media_types = BasicMedia.objects.get_home_status(
        user=request.user,
        status=section_key,
        sort_by=sort_by,
        items_limit=None if hide_unreleased else items_limit,
        specific_media_type=specific_media_type,
    )

    if not hide_unreleased:
        return media_types

    return _paginate_home_media_types(
        _filter_home_media_types(
            media_types,
            lambda media: _is_released_home_media(media, section_key),
        ),
        items_limit,
        page_start=items_limit if specific_media_type else 0,
    )


def _filter_home_media_types(media_types, predicate):
    """Filter home media entries by predicate."""
    filtered_media_types = {}
    for media_type, media_list in media_types.items():
        filtered_items = [media for media in media_list["items"] if predicate(media)]
        if filtered_items:
            filtered_media_types[media_type] = {
                "items": filtered_items,
                "total": len(filtered_items),
            }
    return filtered_media_types


def _paginate_home_media_types(media_types, items_limit, page_start=0):
    """Paginate already-grouped home media entries."""
    paginated_media_types = {}
    for media_type, media_list in media_types.items():
        page_end = None if items_limit is None else page_start + items_limit
        items = media_list["items"][page_start:page_end]

        if items or (page_start > 0 and media_list["total"]):
            paginated_media_types[media_type] = {
                "items": items,
                "total": media_list["total"],
            }
    return paginated_media_types


def _is_incoming_media(media):
    """Return True when media has a real upcoming release."""
    return bool(media.next_event and not media.next_event.is_max_datetime)


def _is_released_home_media(media, section_key):
    """Return True when media should remain after hiding unreleased entries."""
    if section_key == Status.IN_PROGRESS.value:
        return is_active_in_progress_media(media)

    return not _is_incoming_media(media)


def get_up_next(user, tv_ids=None):
    """Return the next released, unwatched episode for each in-progress TV show.

    Episode availability comes from the season's release events, so building the
    strip never calls a metadata provider. Seasons without released episodes
    left to watch are skipped. When a show has several in-progress seasons, the
    lowest season number wins.
    """
    if MediaTypes.SEASON.value not in user.get_active_media_types():
        return []

    seasons = BasicMedia.objects.get_media_list(
        user=user,
        media_type=MediaTypes.SEASON.value,
        status_filter=Status.IN_PROGRESS.value,
        sort_filter=None,
    )
    if tv_ids is not None:
        seasons = [season for season in seasons if season.related_tv_id in tv_ids]

    now = timezone.now()
    min_datetime = SentinelDatetime.min_datetime()
    entries_by_tv = {}
    for season in seasons:
        entry = _build_up_next_entry(season, now, min_datetime)
        if entry is None:
            continue

        current = entries_by_tv.get(season.related_tv_id)
        if (
            current is None
            or season.item.season_number < current["season"].item.season_number
        ):
            entries_by_tv[season.related_tv_id] = entry

    return sorted(
        entries_by_tv.values(),
        key=lambda entry: (
            -(entry["season"].progressed_at or entry["season"].created_at).timestamp(),
            entry["season"].item.title.lower(),
        ),
    )


def _build_up_next_entry(season, now, min_datetime):
    """Return the up next entry for a season, or None when caught up."""
    progress = season.progress
    released = {}
    for event in getattr(season.item, "prefetched_events", []):
        if (
            event.content_number is not None
            and event.content_number > progress
            and event.datetime <= now
        ):
            released[event.content_number] = event.datetime

    if not released:
        return None

    episode_number = min(released)
    air_date = released[episode_number]
    return {
        "season": season,
        "tv_id": season.related_tv_id,
        "episode_number": episode_number,
        "air_date": None if air_date <= min_datetime else air_date,
        "remaining": len(released),
    }
