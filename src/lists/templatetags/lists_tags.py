from django import template

from lists.models import CustomList

register = template.Library()


@register.simple_tag
def pinned_lists(user):
    """Return the lists the user pinned to the sidebar."""
    if not user.is_authenticated:
        return CustomList.objects.none()
    return CustomList.objects.get_pinned_lists(user)
