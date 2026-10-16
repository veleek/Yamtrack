import csv
import logging
from collections import defaultdict
from csv import DictReader
from io import BytesIO, StringIO

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.dateparse import parse_datetime

import app
from app import config
from app.models import MediaTypes, Sources
from app.providers import services
from app.templatetags import app_tags
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError
from lists.models import CustomList, CustomListItem

logger = logging.getLogger(__name__)


def importer(file, user, mode):
    """Import a Yamtrack media or lists CSV export.

    The lists export is recognised by its header, so both files can be
    uploaded through the same Yamtrack import form.
    """
    content = file.read()
    try:
        first_line = content.decode("utf-8").partition("\n")[0]
    except UnicodeDecodeError as e:
        msg = "Invalid file format. Please upload a CSV file."
        raise MediaImportError(msg) from e

    header = next(csv.reader([first_line]), [])
    if "list_name" in header:
        return YamtrackListImporter(BytesIO(content), user, mode).import_data()

    csv_importer = YamtrackImporter(BytesIO(content), user, mode)
    return csv_importer.import_data()


class YamtrackImporter:
    """Class to handle importing user data from CSV files."""

    def __init__(self, file, user, mode):
        """Initialize the importer with file, user, and mode.

        Args:
            file: Uploaded CSV file object
            user: Django user object to import data for
            mode (str): Import mode ("new" or "overwrite")
        """
        self.file = file
        self.user = user
        self.mode = mode
        self.warnings = []

        # Track existing media for "new" mode
        self.existing_media = helpers.get_existing_media(user)

        # Track media IDs to delete in overwrite mode
        self.to_delete = defaultdict(lambda: defaultdict(set))

        # Track bulk creation lists for each media type
        self.bulk_media = defaultdict(list)

        logger.info(
            "Initialized Yamtrack CSV importer for user %s with mode %s",
            user.username,
            mode,
        )

    def import_data(self):
        """Import all user data from the CSV file."""
        try:
            decoded_file = self.file.read().decode("utf-8")
        except UnicodeDecodeError as e:
            msg = "Invalid file format. Please upload a CSV file."
            raise MediaImportError(msg) from e

        reader = DictReader(StringIO(decoded_file, newline=""))

        for row in reader:
            try:
                self._process_row(row)
            except services.ProviderAPIError as error:
                error_msg = (
                    f"Error processing entry with ID {row['media_id']} "
                    f"({app_tags.media_type_readable(row['media_type'])}): {error}"
                )
                self.warnings.append(error_msg)
                continue
            except Exception as error:
                error_msg = f"Error processing entry: {row}"
                raise MediaImportUnexpectedError(error_msg) from error

        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)

        imported_counts = {
            media_type: len(media_list)
            for media_type, media_list in self.bulk_media.items()
        }

        deduplicated_messages = "\n".join(dict.fromkeys(self.warnings))
        return imported_counts, deduplicated_messages

    def _process_row(self, row):
        """Process a single row from the CSV file."""
        media_type = row["media_type"]

        season_number = (
            int(row["season_number"]) if row["season_number"] != "" else None
        )
        episode_number = (
            int(row["episode_number"]) if row["episode_number"] != "" else None
        )

        if row["progress"] == "":
            row["progress"] = 0

        parent_type = (
            MediaTypes.TV.value
            if media_type in (MediaTypes.SEASON.value, MediaTypes.EPISODE.value)
            else media_type
        )

        # Check if we should process this movie based on mode
        if not helpers.should_process_media(
            self.existing_media,
            self.to_delete,
            parent_type,
            row["source"],
            row["media_id"],
            self.mode,
        ):
            return

        if row["title"] == "" or row["image"] == "":
            self._handle_missing_metadata(
                row,
                media_type,
                season_number,
                episode_number,
            )

        item, _ = app.models.Item.objects.update_or_create(
            media_id=row["media_id"],
            source=row["source"],
            media_type=media_type,
            season_number=season_number,
            episode_number=episode_number,
            defaults={
                "title": row["title"],
                "image": row["image"],
            },
        )

        model = apps.get_model(app_label="app", model_name=media_type)
        instance = model(item=item)
        if media_type != MediaTypes.EPISODE.value:  # episode has no user field
            instance.user = self.user

        row["item"] = item
        form = app.forms.get_form_class(media_type)(
            row,
            instance=instance,
        )

        if form.is_valid():
            # Episodes have no progressed_at field, so their exported column is
            # blank; fall back to end_date (the watch date) so the activity
            # heatmap lands on the day watched instead of the import day.
            progressed_at = row.get("progressed_at") or row.get("end_date")
            if progressed_at:
                form.instance._history_date = parse_datetime(progressed_at)
            self.bulk_media[media_type].append(form.instance)
        else:
            error_msg = f"{row['title']} ({media_type}): {form.errors.as_json()}"
            self.warnings.append(error_msg)
            logger.error(error_msg)

    @staticmethod
    def _handle_missing_metadata(row, media_type, season_number, episode_number):
        """Handle missing metadata by fetching from provider."""
        if row["source"] == Sources.MANUAL.value and row["image"] == "":
            row["image"] = settings.IMG_NONE
            return

        if row.get("media_id", "") != "":
            metadata = services.get_media_metadata(
                media_type,
                row["media_id"],
                row["source"],
                [season_number],
                episode_number,
            )
            row["title"] = metadata["title"]
            row["image"] = metadata["image"]
            return

        if row.get("title", "") != "":
            source = row.get("source", "")
            if source == "":
                source = config.get_default_source_name(media_type).value

            metadata = services.search(
                media_type,
                row["title"],
                1,
                source,
            )

            first_result = metadata["results"][0]
            row["title"] = first_result["title"]
            row["source"] = first_result["source"]
            row["media_id"] = first_result["media_id"]
            row["media_type"] = media_type
            row["image"] = first_result["image"]

            logger.info("Added title from %s: %s", source, row["title"])
            logger.info("Obtained media id: %s", row["media_id"])
            return

        msg = f"Missing metadata for: {row}"
        raise MediaImportError(msg)


class YamtrackListImporter:
    """Restore custom lists from a Yamtrack lists CSV export.

    Lists are matched by name. A list the user collaborated on is matched to
    the existing shared list when it is still there (same name and owner);
    otherwise it is recreated as a list owned by the importing user.
    Collaborators, and the original owner of a shared list, are added only
    when a user with that username exists.
    """

    def __init__(self, file, user, mode):
        """Initialize the importer with file, user, and mode."""
        self.file = file
        self.user = user
        self.mode = mode
        self.warnings = []
        self.lists = {}
        self.owned_lists = {}
        self.created_lists = 0
        self.added_items = 0

    def import_data(self):
        """Import all lists from the CSV file."""
        decoded_file = self.file.read().decode("utf-8")
        reader = DictReader(StringIO(decoded_file, newline=""))

        with transaction.atomic():
            for row in reader:
                try:
                    self._process_row(row)
                except (services.ProviderAPIError, MediaImportError) as error:
                    self.warnings.append(
                        f"{row.get('title') or row.get('media_id')} "
                        f"(list {row['list_name']}): {error}",
                    )
                except Exception as error:
                    error_msg = f"Error processing list entry: {row}"
                    raise MediaImportUnexpectedError(error_msg) from error

        imported_counts = {"lists": self.created_lists, "list_items": self.added_items}
        deduplicated_messages = "\n".join(dict.fromkeys(self.warnings))
        return imported_counts, deduplicated_messages

    def _process_row(self, row):
        """Process a single list entry row."""
        custom_list = self._get_list(row)

        if row["media_id"] == "" and row["title"] == "":
            return  # row for an empty list

        item = self._get_item(row)
        list_item, created = CustomListItem.objects.get_or_create(
            item=item,
            custom_list=custom_list,
        )
        if not created:
            return

        # date_added is auto_now_add, so restore the original date afterwards
        date_added = parse_datetime(row["date_added"]) if row["date_added"] else None
        if date_added:
            CustomListItem.objects.filter(pk=list_item.pk).update(
                date_added=date_added,
            )
        self.added_items += 1

    def _get_list(self, row):
        """Return the target list for a row, creating it on first use."""
        key = (row["list_owner"], row["list_name"])
        if key in self.lists:
            return self.lists[key]

        custom_list = None
        if row["list_owner"] != self.user.username:
            # Still shared with this user? Then add to the original list.
            custom_list = CustomList.objects.filter(
                name=row["list_name"],
                owner__username=row["list_owner"],
                collaborators=self.user,
            ).first()

        if custom_list is None:
            custom_list = self._get_owned_list(row)

        self.lists[key] = custom_list
        return custom_list

    def _get_owned_list(self, row):
        """Return or create a list owned by the importing user."""
        if row["list_name"] in self.owned_lists:
            return self.owned_lists[row["list_name"]]

        custom_list = CustomList.objects.filter(
            name=row["list_name"],
            owner=self.user,
        ).first()

        if custom_list is None:
            custom_list = CustomList.objects.create(
                name=row["list_name"],
                description=row["list_description"],
                owner=self.user,
            )
            self.created_lists += 1
        elif self.mode == "overwrite":
            custom_list.description = row["list_description"]
            custom_list.save(update_fields=["description"])
            CustomListItem.objects.filter(custom_list=custom_list).delete()

        usernames = {
            username for username in row["list_collaborators"].split(",") if username
        }
        if row["list_role"] == "collaborator":
            usernames.add(row["list_owner"])
        usernames.discard(self.user.username)
        collaborators = get_user_model().objects.filter(username__in=usernames)
        custom_list.collaborators.add(*collaborators)

        self.owned_lists[row["list_name"]] = custom_list
        return custom_list

    def _get_item(self, row):
        """Return the Item for a row, fetching metadata if it is missing."""
        media_type = row["media_type"]
        season_number = (
            int(row["season_number"]) if row["season_number"] != "" else None
        )
        episode_number = (
            int(row["episode_number"]) if row["episode_number"] != "" else None
        )
        lookup = {
            "media_id": row["media_id"],
            "source": row["source"],
            "media_type": media_type,
            "season_number": season_number,
            "episode_number": episode_number,
        }

        item = app.models.Item.objects.filter(**lookup).first()
        if item is not None:
            return item

        if row["title"] == "" or row["image"] == "":
            YamtrackImporter._handle_missing_metadata(
                row,
                media_type,
                season_number,
                episode_number,
            )
            lookup["media_id"] = row["media_id"]
            lookup["source"] = row["source"]

        item, _ = app.models.Item.objects.get_or_create(
            **lookup,
            defaults={"title": row["title"], "image": row["image"]},
        )
        return item
