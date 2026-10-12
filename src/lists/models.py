from django.conf import settings
from django.db import models
from django.db.models import Prefetch, Q

from app.models import Item, MediaTypes

# Item types that belong to one show; any of them in a list puts the show's
# seasons on the list's tracker.
SHOW_MEDIA_TYPES = (
    MediaTypes.TV.value,
    MediaTypes.SEASON.value,
    MediaTypes.EPISODE.value,
)


class CustomListManager(models.Manager):
    """Manager for custom lists."""

    def get_user_lists(self, user):
        """Return the custom lists that the user owns or collaborates on."""
        return (
            self.filter(Q(owner=user) | Q(collaborators=user))
            .select_related("owner")
            .prefetch_related(
                "collaborators",
                Prefetch(
                    "items",
                    queryset=Item.objects.order_by("-customlistitem__date_added"),
                ),
                Prefetch(
                    "customlistitem_set",
                    queryset=CustomListItem.objects.order_by("-date_added"),
                ),
            )
            .distinct()
        )

    def get_pinned_lists(self, user):
        """Return the lists the user pinned to the sidebar and can still view."""
        return (
            self.filter(Q(owner=user) | Q(collaborators=user), pinned_by=user)
            .distinct()
            .order_by("name")
        )

    def get_user_lists_with_item(self, user, item):
        """Return user lists with item membership status."""
        return (
            self.filter(Q(owner=user) | Q(collaborators=user))
            .annotate(
                has_item=models.Exists(
                    CustomListItem.objects.filter(
                        custom_list_id=models.OuterRef("id"),
                        item=item,
                    ),
                ),
            )
            .prefetch_related("collaborators")
            .distinct()
            .order_by("name")
        )


class CustomList(models.Model):
    """Model for custom lists."""

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    collaborators = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="collaborated_lists",
        blank=True,
    )
    items = models.ManyToManyField(
        Item,
        related_name="custom_lists",
        blank=True,
        through="CustomListItem",
    )
    tracker_view = models.BooleanField(
        default=False,
        verbose_name="Show as tracker",
        help_text=("Open this list with the Home tracker view, limited to its items."),
    )
    pinned_by = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="pinned_lists",
        blank=True,
    )

    objects = CustomListManager()

    class Meta:
        """Meta options for the model."""

        ordering = ["name"]

    def __str__(self):
        """Return the name of the custom list."""
        return self.name

    def user_can_view(self, user):
        """Check if the user can view the list."""
        return self.owner == user or user in self.collaborators.all()

    def user_can_edit(self, user):
        """Check if the user can edit the list."""
        return self.owner == user or user in self.collaborators.all()

    def user_can_delete(self, user):
        """Check if the user can delete the list."""
        return self.owner == user

    def is_pinned_by(self, user):
        """Check if the user pinned the list to the sidebar."""
        return self.pinned_by.filter(id=user.id).exists()

    def tracker_items(self):
        """Return the items whose tracked media belongs on this list's tracker.

        Items match directly, and a TV show, season or episode in the list also
        matches the show and all of its seasons, since Home tracks seasons.
        """
        query = Q(id__in=self.items.values("id"))
        shows = (
            self.items.filter(media_type__in=SHOW_MEDIA_TYPES)
            .values_list("source", "media_id")
            .distinct()
        )
        for source, media_id in shows:
            query |= Q(
                source=source,
                media_id=media_id,
                media_type__in=[MediaTypes.TV.value, MediaTypes.SEASON.value],
            )
        return Item.objects.filter(query)

    @property
    def image(self):
        """Return the image of the first item in the list."""
        return self.items.first().image if self.items.first() else settings.IMG_NONE


class CustomListItemManager(models.Manager):
    """Manager for custom list items."""

    def get_last_added_date(self, custom_list):
        """Return the last time an item was added to a specific list."""
        try:
            return self.filter(custom_list=custom_list).latest("date_added").date_added
        except self.model.DoesNotExist:
            return None


class CustomListItem(models.Model):
    """Model for items in custom lists."""

    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    custom_list = models.ForeignKey(CustomList, on_delete=models.CASCADE)
    date_added = models.DateTimeField(auto_now_add=True)

    objects = CustomListItemManager()

    class Meta:
        """Meta options for the model."""

        ordering = ["date_added"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "custom_list"],
                name="%(app_label)s_customlistitem_unique_item_list",
            ),
        ]

    def __str__(self):
        """Return the name of the list item."""
        return self.item.title
