from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import home as home_helpers
from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
)
from events.models import Event, SentinelDatetime

EPISODE_COUNT = 10


def mock_get_media_metadata(
    media_type,
    _media_id,
    _source,
    season_numbers=None,
    _episode_number=None,
):
    """Return metadata for a show whose seasons have ten episodes each."""
    episodes = [{"episode_number": i} for i in range(1, EPISODE_COUNT + 1)]
    season_number = season_numbers[0] if season_numbers else 1

    if media_type == "tv_with_seasons":
        return {f"season/{season_number}": {"episodes": episodes}}

    if media_type == MediaTypes.SEASON.value:
        return {"episodes": episodes}

    return {
        "title": "Test Show",
        "image": "http://example.com/image.jpg",
        "details": {"seasons": 2},
        "related": {"seasons": []},
    }


class UpNextTestMixin:
    """Shared fixtures for Up next tests."""

    def setUp(self):
        """Create a user, mock providers and skip release fetching."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        metadata_patcher = patch(
            "app.providers.services.get_media_metadata",
            side_effect=mock_get_media_metadata,
        )
        metadata_patcher.start()
        self.addCleanup(metadata_patcher.stop)

        releases_patcher = patch.object(Item, "fetch_releases")
        releases_patcher.start()
        self.addCleanup(releases_patcher.stop)

        self.now = timezone.now()

    def create_tv(self, media_id, title="Test Show", user=None):
        """Create an in-progress TV show."""
        item = Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title=title,
            image="http://example.com/image.jpg",
        )
        return TV.objects.create(
            item=item,
            user=user or self.user,
            status=Status.IN_PROGRESS.value,
        )

    def create_season(
        self,
        tv,
        season_number=1,
        watched=0,
        released=0,
        upcoming=0,
    ):
        """Create an in-progress season with watched episodes and release events."""
        item = Item.objects.create(
            media_id=tv.item.media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title=tv.item.title,
            image="http://example.com/image.jpg",
            season_number=season_number,
        )
        season = Season.objects.create(
            item=item,
            related_tv=tv,
            user=tv.user,
            status=Status.IN_PROGRESS.value,
        )

        for episode_number in range(1, watched + 1):
            episode_item = Item.objects.create(
                media_id=tv.item.media_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title=tv.item.title,
                image="http://example.com/image.jpg",
                season_number=season_number,
                episode_number=episode_number,
            )
            Episode.objects.create(
                item=episode_item,
                related_season=season,
                end_date=self.now
                - timezone.timedelta(days=watched - episode_number + 1),
            )

        for episode_number in range(1, released + 1):
            Event.objects.create(
                item=item,
                content_number=episode_number,
                datetime=self.now
                - timezone.timedelta(days=released - episode_number + 1),
            )

        for offset in range(1, upcoming + 1):
            Event.objects.create(
                item=item,
                content_number=released + offset,
                datetime=self.now + timezone.timedelta(days=offset),
            )

        season.refresh_from_db()
        return season


class GetUpNextTests(UpNextTestMixin, TestCase):
    """Test building Up next entries."""

    def test_next_released_episode(self):
        """The first released episode after the current progress is up next."""
        tv = self.create_tv("1")
        season = self.create_season(tv, watched=3, released=5, upcoming=2)

        entries = home_helpers.get_up_next(self.user)

        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["season"], season)
        self.assertEqual(entry["tv_id"], tv.id)
        self.assertEqual(entry["episode_number"], 4)
        self.assertEqual(entry["remaining"], 2)
        self.assertIsNotNone(entry["air_date"])

    def test_not_started_season_starts_at_first_episode(self):
        """An in-progress season with nothing watched starts at episode one."""
        tv = self.create_tv("1")
        self.create_season(tv, watched=0, released=3)

        entries = home_helpers.get_up_next(self.user)

        self.assertEqual(entries[0]["episode_number"], 1)
        self.assertEqual(entries[0]["remaining"], 3)

    def test_caught_up_season_is_skipped(self):
        """A season with only unreleased episodes left has nothing up next."""
        tv = self.create_tv("1")
        self.create_season(tv, watched=5, released=5, upcoming=1)

        self.assertEqual(home_helpers.get_up_next(self.user), [])

    def test_season_without_events_is_skipped(self):
        """Seasons without release data are not guessed at."""
        tv = self.create_tv("1")
        self.create_season(tv, watched=2)

        self.assertEqual(home_helpers.get_up_next(self.user), [])

    def test_unknown_air_date_is_hidden(self):
        """Episodes released at an unknown date have no air date shown."""
        tv = self.create_tv("1")
        season = self.create_season(tv, watched=1)
        Event.objects.create(
            item=season.item,
            content_number=2,
            datetime=SentinelDatetime.min_datetime(),
        )

        entries = home_helpers.get_up_next(self.user)

        self.assertEqual(entries[0]["episode_number"], 2)
        self.assertIsNone(entries[0]["air_date"])

    def test_one_entry_per_show_using_lowest_season(self):
        """A show with several in-progress seasons shows its earliest one."""
        tv = self.create_tv("1")
        self.create_season(tv, season_number=2, watched=1, released=4)
        first_season = self.create_season(tv, season_number=1, watched=2, released=5)

        entries = home_helpers.get_up_next(self.user)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["season"], first_season)
        self.assertEqual(entries[0]["episode_number"], 3)

    def test_sorted_by_most_recently_watched(self):
        """Shows watched most recently come first."""
        older = self.create_season(self.create_tv("1", "Older"), watched=2, released=5)
        newer = self.create_season(self.create_tv("2", "Newer"), watched=2, released=5)
        Episode.objects.filter(related_season=older).update(
            end_date=self.now - timezone.timedelta(days=30),
        )

        entries = home_helpers.get_up_next(self.user)

        self.assertEqual([entry["season"] for entry in entries], [newer, older])

    def test_filter_by_tv_ids(self):
        """Entries can be limited to specific shows."""
        first_tv = self.create_tv("1")
        second_tv = self.create_tv("2")
        self.create_season(first_tv, watched=1, released=3)
        self.create_season(second_tv, watched=1, released=3)

        entries = home_helpers.get_up_next(self.user, tv_ids={second_tv.id})

        self.assertEqual([entry["tv_id"] for entry in entries], [second_tv.id])

    def test_other_users_seasons_are_ignored(self):
        """Only the requesting user's seasons are included."""
        other_user = get_user_model().objects.create_user(
            username="other",
            password="12345",  # noqa: S106
        )
        self.create_season(self.create_tv("1", user=other_user), released=3)

        self.assertEqual(home_helpers.get_up_next(self.user), [])

    def test_disabled_tv_returns_nothing(self):
        """Up next is empty when TV tracking is disabled."""
        self.create_season(self.create_tv("1"), watched=1, released=3)
        self.user.tv_enabled = False
        self.user.season_enabled = False
        self.user.save(update_fields=["tv_enabled", "season_enabled"])

        self.assertEqual(home_helpers.get_up_next(self.user), [])


class UpNextViewTests(UpNextTestMixin, TestCase):
    """Test the Up next strip on the home page and its watch action."""

    def test_home_renders_up_next(self):
        """The home page includes the Up next strip."""
        tv = self.create_tv("1")
        self.create_season(tv, watched=3, released=5)

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["up_next"]), 1)
        self.assertEqual(response.context["up_next_total"], 1)
        self.assertContains(response, "Up next")
        self.assertContains(response, f'id="up-next-tv-{tv.id}"')
        self.assertContains(response, "S01E04")

    def test_home_without_up_next(self):
        """The strip is omitted when nothing is up next."""
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["up_next"], [])
        self.assertNotContains(response, "up-next-card")

    def test_home_limits_up_next(self):
        """The strip shows a limited number of shows but counts them all."""
        total = home_helpers.UP_NEXT_LIMIT + 2
        for i in range(total):
            self.create_season(self.create_tv(str(i)), watched=1, released=2)

        response = self.client.get(reverse("home"))

        self.assertEqual(len(response.context["up_next"]), home_helpers.UP_NEXT_LIMIT)
        self.assertEqual(response.context["up_next_total"], total)

    def test_watch_marks_episode_and_returns_next(self):
        """Watching from the strip records the episode and shows the next one."""
        tv = self.create_tv("1")
        season = self.create_season(tv, watched=3, released=6)

        response = self.client.post(
            reverse("up_next_watch", args=[season.id]),
            {"episode_number": 4},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Episode.objects.filter(
                related_season=season,
                item__episode_number=4,
            ).exists(),
        )
        self.assertEqual(response.context["entry"]["episode_number"], 5)
        self.assertContains(response, f'id="up-next-tv-{tv.id}"')
        self.assertContains(response, "S01E05")
        # the season card in the in-progress grid is refreshed out of band
        self.assertContains(response, f'id="progress-season-{season.id}"')
        self.assertContains(response, 'hx-swap-oob="innerHTML"')

    def test_watch_last_released_episode_removes_card(self):
        """Watching the last released episode removes the show from the strip."""
        tv = self.create_tv("1")
        season = self.create_season(tv, watched=3, released=4, upcoming=1)

        response = self.client.post(
            reverse("up_next_watch", args=[season.id]),
            {"episode_number": 4},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["entry"])
        self.assertNotContains(response, "up-next-card")

    def test_watch_invalid_episode_number(self):
        """A missing or non-numeric episode number is rejected."""
        season = self.create_season(self.create_tv("1"), released=2)
        url = reverse("up_next_watch", args=[season.id])

        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertEqual(
            self.client.post(url, {"episode_number": "abc"}).status_code,
            400,
        )
        self.assertFalse(Episode.objects.filter(related_season=season).exists())

    def test_watch_other_users_season(self):
        """Users cannot mark episodes on someone else's season."""
        other_user = get_user_model().objects.create_user(
            username="other",
            password="12345",  # noqa: S106
        )
        season = self.create_season(self.create_tv("1", user=other_user), released=2)

        response = self.client.post(
            reverse("up_next_watch", args=[season.id]),
            {"episode_number": 1},
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(Episode.objects.filter(related_season=season).exists())

    def test_watch_requires_post(self):
        """The watch action only accepts POST."""
        season = self.create_season(self.create_tv("1"), released=2)

        response = self.client.get(reverse("up_next_watch", args=[season.id]))

        self.assertEqual(response.status_code, 405)

    def test_progress_edit_refreshes_up_next_card(self):
        """Changing season progress from the grid refreshes its Up next card."""
        tv = self.create_tv("1")
        season = self.create_season(tv, watched=3, released=6)

        response = self.client.post(
            reverse("progress_edit", args=[MediaTypes.SEASON.value, season.id]),
            {"operation": "increase", "home_status": Status.IN_PROGRESS.value},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["up_next_oob"]["entry"]["episode_number"], 5)
        self.assertContains(response, f'id="up-next-tv-{tv.id}"')
        self.assertContains(response, 'hx-swap-oob="true"')

    def test_progress_edit_removes_caught_up_card(self):
        """Catching up from the grid removes the show from the strip."""
        tv = self.create_tv("1")
        season = self.create_season(tv, watched=3, released=4, upcoming=1)

        response = self.client.post(
            reverse("progress_edit", args=[MediaTypes.SEASON.value, season.id]),
            {"operation": "increase", "home_status": Status.IN_PROGRESS.value},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["up_next_oob"]["entry"])
        self.assertContains(response, 'hx-swap-oob="delete"')
