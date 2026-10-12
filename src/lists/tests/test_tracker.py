from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import (
    Anime,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from lists.forms import CustomListForm
from lists.models import CustomList, CustomListItem


def mock_get_media_metadata(
    media_type,
    _media_id,
    _source,
    _season_numbers=None,
    _episode_number=None,
):
    """Return minimal metadata so tracked media can be saved."""
    return {
        "title": "Test",
        "image": "http://example.com/image.jpg",
        "max_progress": 10,
        "details": {"seasons": 1},
        "related": {"seasons": [{"season_number": 1, "image": ""}]},
        "season/1": {"episodes": [{"id": i} for i in range(1, 11)]},
        "media_type": media_type,
    }


class ListTrackerTestBase(TestCase):
    """Shared data: a show, an anime and a movie, only some of them listed."""

    def setUp(self):
        """Create tracked media and a tracker list holding part of it."""
        patcher = patch(
            "app.providers.services.get_media_metadata",
            side_effect=mock_get_media_metadata,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.tv_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Listed Show",
        )
        self.season_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Listed Show",
            season_number=1,
        )
        self.season = Season.objects.create(
            item=self.season_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        self.anime_item = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Unlisted Anime",
        )
        Anime.objects.create(
            item=self.anime_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=1,
        )

        self.movie_item = Item.objects.create(
            media_id="10",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Listed Movie",
        )
        Movie.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        self.custom_list = CustomList.objects.create(
            name="Watching together",
            owner=self.user,
            tracker_view=True,
        )
        # The list holds the show, not the season Home tracks
        CustomListItem.objects.create(custom_list=self.custom_list, item=self.tv_item)
        CustomListItem.objects.create(
            custom_list=self.custom_list,
            item=self.movie_item,
        )


class TrackerItemsTests(ListTrackerTestBase):
    """Tests for CustomList.tracker_items."""

    def test_show_in_list_matches_its_seasons(self):
        """A TV show in the list puts the show and its seasons on the tracker."""
        tracker_items = set(self.custom_list.tracker_items())

        self.assertIn(self.tv_item, tracker_items)
        self.assertIn(self.season_item, tracker_items)
        self.assertIn(self.movie_item, tracker_items)
        self.assertNotIn(self.anime_item, tracker_items)

    def test_other_show_seasons_do_not_match(self):
        """Seasons of a show with the same id from another source don't match."""
        other_season = Item.objects.create(
            media_id="1668",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Other Show",
            season_number=1,
        )

        self.assertNotIn(other_season, set(self.custom_list.tracker_items()))

    def test_empty_list_has_no_tracker_items(self):
        """An empty list tracks nothing."""
        empty_list = CustomList.objects.create(name="Empty", owner=self.user)

        self.assertFalse(empty_list.tracker_items().exists())


class ListTrackerViewTests(ListTrackerTestBase):
    """Tests for the list tracker view."""

    def test_tracker_view_shows_only_list_media(self):
        """The tracker renders Home's sections limited to the list's media."""
        response = self.client.get(reverse("list_detail", args=[self.custom_list.id]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/list_tracker.html")
        sections = {
            section["key"]: section for section in response.context["home_sections"]
        }
        in_progress = sections[Status.IN_PROGRESS.value]["media_types"]
        planning = sections[Status.PLANNING.value]["media_types"]

        self.assertEqual(list(in_progress), [MediaTypes.SEASON.value])
        self.assertEqual(in_progress[MediaTypes.SEASON.value]["items"], [self.season])
        self.assertEqual(list(planning), [MediaTypes.MOVIE.value])
        self.assertEqual(
            response.context["section_url"],
            reverse("list_detail", args=[self.custom_list.id]),
        )
        self.assertContains(response, "Listed Show")
        self.assertNotContains(response, "Unlisted Anime")

    def test_items_view_shows_grid(self):
        """The items view still renders the regular list grid."""
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]),
            {"view": "items"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/list_detail.html")
        self.assertEqual(response.context["current_view"], "items")

    def test_list_without_tracker_view_shows_grid(self):
        """Lists without the setting keep the regular grid."""
        self.custom_list.tracker_view = False
        self.custom_list.save()

        response = self.client.get(reverse("list_detail", args=[self.custom_list.id]))

        self.assertTemplateUsed(response, "lists/list_detail.html")
        self.assertNotContains(response, "All items")

    def test_tracker_load_more(self):
        """Loading more of a media type stays limited to the list."""
        response = self.client.get(
            reverse("list_detail", args=[self.custom_list.id]),
            {
                "load_media_type": MediaTypes.ANIME.value,
                "load_status": Status.IN_PROGRESS.value,
            },
            headers={"HX-Request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/home_grid.html")
        self.assertEqual(response.context["media_list"]["items"], [])

    def test_tracker_view_unauthorized(self):
        """Users who can't view the list get a 404."""
        other = get_user_model().objects.create_user(
            **{**self.credentials, "username": "other"},
        )
        other_list = CustomList.objects.create(
            name="Private",
            owner=other,
            tracker_view=True,
        )

        response = self.client.get(reverse("list_detail", args=[other_list.id]))

        self.assertEqual(response.status_code, 404)

    def test_form_has_tracker_view_field(self):
        """The list form exposes the tracker setting."""
        self.assertIn("tracker_view", CustomListForm().fields)


class ListPinTests(ListTrackerTestBase):
    """Tests for pinning lists to the sidebar."""

    def test_pin_and_unpin(self):
        """Posting toggles the pin for the current user only."""
        url = reverse("list_pin_toggle", args=[self.custom_list.id])

        self.client.post(url)
        self.assertTrue(self.custom_list.is_pinned_by(self.user))

        self.client.post(url)
        self.assertFalse(self.custom_list.is_pinned_by(self.user))

    def test_pin_requires_access(self):
        """Users who can't view a list can't pin it."""
        other = get_user_model().objects.create_user(
            **{**self.credentials, "username": "other"},
        )
        other_list = CustomList.objects.create(name="Private", owner=other)

        response = self.client.post(reverse("list_pin_toggle", args=[other_list.id]))

        self.assertEqual(response.status_code, 404)
        self.assertFalse(other_list.is_pinned_by(self.user))

    def test_pinned_list_in_sidebar(self):
        """Pinned lists are linked from the sidebar on every page."""
        self.custom_list.pinned_by.add(self.user)

        response = self.client.get(reverse("home"))

        self.assertContains(
            response,
            f'href="{reverse("list_detail", args=[self.custom_list.id])}"',
        )

    def test_pinned_lists_exclude_lists_no_longer_shared(self):
        """A collaborator removed from a list loses its pin from the sidebar."""
        partner = get_user_model().objects.create_user(
            **{**self.credentials, "username": "partner"},
        )
        self.custom_list.collaborators.add(partner)
        self.custom_list.pinned_by.add(partner)
        self.assertEqual(
            list(CustomList.objects.get_pinned_lists(partner)),
            [self.custom_list],
        )

        self.custom_list.collaborators.remove(partner)

        self.assertFalse(CustomList.objects.get_pinned_lists(partner).exists())
