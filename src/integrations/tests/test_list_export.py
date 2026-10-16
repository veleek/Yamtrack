import csv
from datetime import UTC, datetime
from io import BytesIO, StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.models import Item, MediaTypes, Sources
from integrations import exports
from integrations.imports import yamtrack
from integrations.tasks import format_import_message
from lists.models import CustomList, CustomListItem

OTHER_LOGIN = {"password": "12345"}


def add_to_list(custom_list, item, date_added):
    """Add an item to a list with a fixed date_added."""
    list_item = CustomListItem.objects.create(item=item, custom_list=custom_list)
    CustomListItem.objects.filter(pk=list_item.pk).update(date_added=date_added)


class ListExportTest(TestCase):
    """Test exporting custom lists to CSV and importing them back."""

    def setUp(self):
        """Create users, items, and lists."""
        user_model = get_user_model()
        self.credentials = {"username": "ben", "password": "12345"}
        self.user = user_model.objects.create_user(**self.credentials)
        self.alice = user_model.objects.create_user(username="alice", **OTHER_LOGIN)
        self.carol = user_model.objects.create_user(username="carol", **OTHER_LOGIN)
        self.client.login(**self.credentials)

        self.movie = Item.objects.create(
            media_id="10494",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Perfect Blue",
            image="https://image.url/movie",
        )
        self.season = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            image="https://image.url/season",
            season_number=1,
        )

        self.owned = CustomList.objects.create(
            name="Favorites",
            description="Best of the best",
            owner=self.user,
        )
        self.owned.collaborators.add(self.carol)
        add_to_list(self.owned, self.movie, datetime(2024, 1, 2, 3, 4, tzinfo=UTC))
        add_to_list(self.owned, self.season, datetime(2024, 2, 3, 4, 5, tzinfo=UTC))

        self.shared = CustomList.objects.create(
            name="Movie night",
            description="Alice's list",
            owner=self.alice,
        )
        self.shared.collaborators.add(self.user)
        add_to_list(self.shared, self.movie, datetime(2024, 3, 4, 5, 6, tzinfo=UTC))

        CustomList.objects.create(name="Empty", owner=self.user)

        # A list the user has no access to must not be exported
        CustomList.objects.create(name="Private", owner=self.alice)

    def export(self):
        """Export the user's lists and return the raw CSV bytes."""
        response = self.client.get(reverse("export_lists_csv"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("yamtrack_lists_", response["Content-Disposition"])
        return b"".join(response.streaming_content)

    def test_export_rows(self):
        """Owned, shared, and empty lists are exported with their entries."""
        rows = list(csv.DictReader(StringIO(self.export().decode("utf-8"))))

        self.assertEqual(
            [(row["list_name"], row["title"]) for row in rows],
            [
                ("Empty", ""),
                ("Favorites", "Perfect Blue"),
                ("Favorites", "Friends"),
                ("Movie night", "Perfect Blue"),
            ],
        )

        favorites = rows[1]
        self.assertEqual(favorites["list_description"], "Best of the best")
        self.assertEqual(favorites["list_owner"], "ben")
        self.assertEqual(favorites["list_role"], "owner")
        self.assertEqual(favorites["list_collaborators"], "carol")
        self.assertEqual(favorites["media_id"], "10494")
        self.assertEqual(favorites["source"], Sources.TMDB.value)
        self.assertEqual(favorites["image"], "https://image.url/movie")
        self.assertEqual(favorites["date_added"], "2024-01-02T03:04:00+00:00")
        self.assertEqual(rows[2]["season_number"], "1")
        self.assertEqual(rows[2]["episode_number"], "")

        shared = rows[3]
        self.assertEqual(shared["list_owner"], "alice")
        self.assertEqual(shared["list_role"], "collaborator")
        self.assertEqual(shared["list_collaborators"], "ben")

    def test_round_trip_to_new_user(self):
        """Importing on a fresh account recreates every list as owned."""
        data = self.export()
        new_user = get_user_model().objects.create_user(username="ben2", **OTHER_LOGIN)

        counts, warnings = yamtrack.importer(BytesIO(data), new_user, "new")

        self.assertEqual(counts, {"lists": 3, "list_items": 3})
        self.assertEqual(warnings, "")

        favorites = CustomList.objects.get(owner=new_user, name="Favorites")
        self.assertEqual(favorites.description, "Best of the best")
        self.assertEqual(
            set(favorites.collaborators.values_list("username", flat=True)),
            {"carol"},
        )
        self.assertEqual(
            list(favorites.customlistitem_set.values_list("item", "date_added")),
            [
                (self.movie.id, datetime(2024, 1, 2, 3, 4, tzinfo=UTC)),
                (self.season.id, datetime(2024, 2, 3, 4, 5, tzinfo=UTC)),
            ],
        )

        # The shared list becomes the importer's own; its owner and the
        # collaborators that exist on this instance are added back
        movie_night = CustomList.objects.get(owner=new_user, name="Movie night")
        self.assertEqual(
            set(movie_night.collaborators.values_list("username", flat=True)),
            {"alice", "ben"},
        )
        self.assertTrue(
            CustomList.objects.filter(owner=new_user, name="Empty").exists()
        )

    def test_reimport_same_user_is_idempotent(self):
        """Re-importing onto the same account adds nothing new."""
        data = self.export()

        counts, _ = yamtrack.importer(BytesIO(data), self.user, "new")

        self.assertEqual(counts, {"lists": 0, "list_items": 0})
        self.assertEqual(CustomList.objects.filter(owner=self.user).count(), 2)
        # Still matched to Alice's list rather than duplicating it
        self.assertFalse(
            CustomList.objects.filter(owner=self.user, name="Movie night").exists(),
        )

    def test_shared_list_restored_into_original(self):
        """Missing entries are re-added to a list that is still shared."""
        data = self.export()
        CustomListItem.objects.filter(custom_list=self.shared).delete()

        counts, _ = yamtrack.importer(BytesIO(data), self.user, "new")

        self.assertEqual(counts, {"lists": 0, "list_items": 1})
        self.assertTrue(self.shared.items.filter(pk=self.movie.pk).exists())

    def test_overwrite_replaces_owned_list(self):
        """Overwrite mode replaces the description and entries."""
        data = self.export()
        extra = Item.objects.create(
            media_id="1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Extra",
            image="https://image.url/extra",
        )
        add_to_list(self.owned, extra, datetime(2025, 1, 1, tzinfo=UTC))
        self.owned.description = "changed"
        self.owned.save()

        yamtrack.importer(BytesIO(data), self.user, "overwrite")

        self.owned.refresh_from_db()
        self.assertEqual(self.owned.description, "Best of the best")
        self.assertEqual(
            set(self.owned.items.values_list("pk", flat=True)),
            {self.movie.pk, self.season.pk},
        )

    @patch("app.providers.services.get_media_metadata")
    def test_import_fetches_missing_items(self, mock_metadata):
        """Unknown items are created, with metadata fetched when absent."""
        mock_metadata.return_value = {
            "title": "Fetched",
            "image": "https://image.url/fetched",
        }
        header = ",".join(exports.LIST_FIELDS)
        data = (
            f"{header}\n"
            "Queue,,someone,owner,,999,tmdb,movie,,,,,\n"
            "Queue,,someone,owner,,998,tmdb,movie,Known,https://img,,,\n"
        ).encode()

        counts, _ = yamtrack.importer(BytesIO(data), self.user, "new")

        self.assertEqual(counts, {"lists": 1, "list_items": 2})
        queue = CustomList.objects.get(owner=self.user, name="Queue")
        self.assertEqual(
            sorted(queue.items.values_list("title", flat=True)),
            ["Fetched", "Known"],
        )
        mock_metadata.assert_called_once()

    def test_import_message(self):
        """List counts are reported in the import result message."""
        self.assertEqual(
            format_import_message({"lists": 1, "list_items": 3}),
            "Imported 1 list and 3 list items.",
        )
