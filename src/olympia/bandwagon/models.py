import uuid

from datetime import datetime

from django.db import connection, models
from django.utils.encoding import python_2_unicode_compatible

import six

from olympia import activity, amo
from olympia.access import acl
from olympia.addons.models import Addon
from olympia.addons.utils import clear_get_featured_ids_cache
from olympia.amo.fields import ManyToManyField, PositiveAutoField
from olympia.amo.models import BaseQuerySet, ManagerBase, ModelBase
from olympia.amo.templatetags.jinja_helpers import absolutify
from olympia.amo.urlresolvers import reverse
from olympia.translations.fields import (
    LinkifiedField, NoLinksNoMarkupField, TranslatedField, save_signal)
from olympia.users.models import UserProfile


SPECIAL_SLUGS = amo.COLLECTION_SPECIAL_SLUGS


class CollectionQuerySet(BaseQuerySet):

    def with_has_addon(self, addon_id):
        """Add a `has_addon` property to each collection.

        `has_addon` will be `True` if `addon_id` exists in that
        particular collection.
        """
        has_addon = """
            select 1 from addons_collections as ac
                where ac.addon_id = %s and ac.collection_id = collections.id
                limit 1"""

        return self.extra(
            select={'has_addon': has_addon},
            select_params=(addon_id,))


class CollectionManager(ManagerBase):
    _queryset_class = CollectionQuerySet

    def get_queryset(self):
        qs = super(CollectionManager, self).get_queryset()
        return qs.transform(Collection.transformer)

    def manual(self):
        """Only hand-crafted, favorites, and featured collections should appear
        in this filter."""
        types = (amo.COLLECTION_NORMAL, amo.COLLECTION_FAVORITES,
                 amo.COLLECTION_FEATURED, )

        return self.filter(type__in=types)

    def listed(self):
        """Return public collections only."""
        return self.filter(listed=True)

    def owned_by(self, user):
        """Collections authored by a user."""
        return self.filter(author=user.pk)


@python_2_unicode_compatible
class Collection(ModelBase):
    id = PositiveAutoField(primary_key=True)
    TYPE_CHOICES = amo.COLLECTION_CHOICES.items()

    uuid = models.UUIDField(blank=True, unique=True, null=True)
    name = TranslatedField(require_locale=False)
    # nickname is deprecated.  Use slug.
    nickname = models.CharField(max_length=30, blank=True, unique=True,
                                null=True)
    slug = models.CharField(max_length=30, blank=True, null=True)

    description = NoLinksNoMarkupField(require_locale=False)
    default_locale = models.CharField(max_length=10, default='en-US',
                                      db_column='defaultlocale')
    type = models.PositiveIntegerField(db_column='collection_type',
                                       choices=TYPE_CHOICES, default=0)

    listed = models.BooleanField(
        default=True, help_text='Collections are either listed or private.')

    application = models.PositiveIntegerField(choices=amo.APPS_CHOICES,
                                              db_column='application_id',
                                              blank=True, null=True,
                                              db_index=True)
    addon_count = models.PositiveIntegerField(default=0,
                                              db_column='addonCount')

    all_personas = models.BooleanField(
        default=False,
        help_text='Does this collection only contain Themes?')

    addons = ManyToManyField(
        Addon, through='CollectionAddon', related_name='collections')
    author = models.ForeignKey(
        UserProfile, null=True, related_name='collections',
        on_delete=models.CASCADE)

    objects = CollectionManager()

    class Meta(ModelBase.Meta):
        db_table = 'collections'
        unique_together = (('author', 'slug'),)

    def __str__(self):
        return u'%s (%s)' % (self.name, self.addon_count)

    def save(self, **kw):
        if not self.uuid:
            self.uuid = uuid.uuid4()
        if not self.slug:
            # Work with both, strings (if passed manually on .create()
            # and UUID instances)
            self.slug = str(self.uuid).replace('-', '')[:30]
        self.clean_slug()

        super(Collection, self).save(**kw)

    def clean_slug(self):
        if self.type in SPECIAL_SLUGS:
            self.slug = SPECIAL_SLUGS[self.type]
            return

        if self.slug in SPECIAL_SLUGS.values():
            self.slug += '~'

        if not self.author:
            return

        qs = self.author.collections.using('default')
        slugs = dict((slug, id) for slug, id in qs.values_list('slug', 'id'))
        if self.slug in slugs and slugs[self.slug] != self.id:
            for idx in range(len(slugs)):
                new = '%s-%s' % (self.slug, idx + 1)
                if new not in slugs:
                    self.slug = new
                    return

    def get_url_path(self):
        return reverse('collections.detail',
                       args=[self.author_id, self.slug])

    def get_abs_url(self):
        return absolutify(self.get_url_path())

    def edit_url(self):
        return reverse('collections.edit',
                       args=[self.author_id, self.slug])

    def delete_url(self):
        return reverse('collections.delete',
                       args=[self.author_id, self.slug])

    def share_url(self):
        return reverse('collections.share',
                       args=[self.author_id, self.slug])

    def stats_url(self):
        return reverse('collections.stats',
                       args=[self.author_id, self.slug])

    @classmethod
    def get_fallback(cls):
        return cls._meta.get_field('default_locale')

    def set_addons(self, addon_ids, comments=None):
        """Replace the current add-ons with a new list of add-on ids."""
        if comments is None:
            comments = {}
        order = {a: idx for idx, a in enumerate(addon_ids)}

        # Partition addon_ids into add/update/remove buckets.
        existing = set(self.addons.using('default')
                       .values_list('id', flat=True))
        add, update = [], []
        for addon in addon_ids:
            bucket = update if addon in existing else add
            bucket.append((addon, order[addon]))
        remove = existing.difference(addon_ids)
        now = datetime.now()

        with connection.cursor() as cursor:
            if remove:
                cursor.execute("DELETE FROM addons_collections "
                               "WHERE collection_id=%s AND addon_id IN (%s)" %
                               (self.id, ','.join(map(str, remove))))
                if self.listed:
                    for addon in remove:
                        activity.log_create(amo.LOG.REMOVE_FROM_COLLECTION,
                                            (Addon, addon), self)
            if add:
                insert = '(%s, %s, %s, NOW(), NOW())'
                values = [insert % (a, self.id, idx) for a, idx in add]
                cursor.execute("""
                    INSERT INTO addons_collections
                        (addon_id, collection_id, ordering, created, modified)
                    VALUES %s""" % ','.join(values))
                if self.listed:
                    for addon_id, idx in add:
                        activity.log_create(amo.LOG.ADD_TO_COLLECTION,
                                            (Addon, addon_id), self)
        for addon, ordering in update:
            (CollectionAddon.objects.filter(collection=self.id, addon=addon)
             .update(ordering=ordering, modified=now))

        for addon, comment in six.iteritems(comments):
            try:
                c = (CollectionAddon.objects.using('default')
                     .get(collection=self.id, addon=addon))
            except CollectionAddon.DoesNotExist:
                pass
            else:
                c.comments = comment
                c.save(force_update=True)

        self.save()

    def add_addon(self, addon):
        "Adds an addon to the collection."
        CollectionAddon.objects.get_or_create(addon=addon, collection=self)
        if self.listed:
            activity.log_create(amo.LOG.ADD_TO_COLLECTION, addon, self)
        self.save()  # To invalidate Collection.

    def remove_addon(self, addon):
        CollectionAddon.objects.filter(addon=addon, collection=self).delete()
        if self.listed:
            activity.log_create(amo.LOG.REMOVE_FROM_COLLECTION, addon, self)
        self.save()  # To invalidate Collection.

    def owned_by(self, user):
        return user.id == self.author_id

    def can_view_stats(self, request):
        if request and request.user:
            return (self.owned_by(request.user) or
                    acl.action_allowed(request,
                                       amo.permissions.COLLECTION_STATS_VIEW))
        return False

    def is_public(self):
        return self.listed

    def is_featured(self):
        return FeaturedCollection.objects.filter(collection=self).exists()

    @staticmethod
    def transformer(collections):
        if not collections:
            return
        author_ids = set(c.author_id for c in collections)
        authors = dict((u.id, u) for u in
                       UserProfile.objects.filter(id__in=author_ids))
        for c in collections:
            c.author = authors.get(c.author_id)

    @staticmethod
    def post_save(sender, instance, **kwargs):
        from . import tasks
        if kwargs.get('raw'):
            return
        tasks.collection_meta.delay(instance.id)
        if instance.is_featured():
            Collection.update_featured_status(sender, instance, **kwargs)

    @staticmethod
    def post_delete(sender, instance, **kwargs):
        if kwargs.get('raw'):
            return
        if instance.is_featured():
            Collection.update_featured_status(sender, instance, **kwargs)

    @staticmethod
    def update_featured_status(sender, instance, **kwargs):
        from olympia.addons.tasks import index_addons
        addons = kwargs.get(
            'addons', [addon.id for addon in instance.addons.all()])
        if addons:
            clear_get_featured_ids_cache(None, None)
            index_addons.delay(addons)

    def check_ownership(self, request, require_owner, require_author,
                        ignore_disabled, admin):
        """
        Used by acl.check_ownership to see if request.user has permissions for
        the collection.
        """
        from olympia.access import acl
        return acl.check_collection_ownership(request, self, require_owner)


models.signals.post_save.connect(Collection.post_save, sender=Collection,
                                 dispatch_uid='coll.post_save')
models.signals.pre_save.connect(save_signal, sender=Collection,
                                dispatch_uid='coll_translations')
models.signals.post_delete.connect(Collection.post_delete, sender=Collection,
                                   dispatch_uid='coll.post_delete')


class CollectionAddon(ModelBase):
    id = PositiveAutoField(primary_key=True)
    addon = models.ForeignKey(Addon, on_delete=models.CASCADE)
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE)
    # category (deprecated: for "Fashion Your Firefox")
    comments = LinkifiedField(null=True)
    user = models.ForeignKey(UserProfile, null=True, on_delete=models.CASCADE)

    ordering = models.PositiveIntegerField(
        default=0,
        help_text='Add-ons are displayed in ascending order '
                  'based on this field.')

    class Meta(ModelBase.Meta):
        db_table = 'addons_collections'
        unique_together = (('addon', 'collection'),)

    @staticmethod
    def post_save(sender, instance, **kwargs):
        """Update Collection.addon_count and reindex add-on if the collection
        is featured."""
        from . import tasks
        tasks.collection_meta.delay(instance.collection_id)

    @staticmethod
    def post_delete(sender, instance, **kwargs):
        CollectionAddon.post_save(sender, instance, **kwargs)
        if instance.collection.is_featured():
            # The helpers .add_addon() and .remove_addon() already call .save()
            # on the collection, triggering update_featured_status() among
            # other things. However, this only takes care of the add-ons
            # present in the collection at the time, we also need to make sure
            # to invalidate add-ons that have been removed.
            Collection.update_featured_status(
                sender, instance.collection,
                addons=[instance.addon.pk], **kwargs)


models.signals.pre_save.connect(save_signal, sender=CollectionAddon,
                                dispatch_uid='coll_addon_translations')
# Update Collection.addon_count and potentially featured state when a
# collectionaddon changes.
models.signals.post_save.connect(CollectionAddon.post_save,
                                 sender=CollectionAddon,
                                 dispatch_uid='coll.post_save')
models.signals.post_delete.connect(CollectionAddon.post_delete,
                                   sender=CollectionAddon,
                                   dispatch_uid='coll.post_delete')


@python_2_unicode_compatible
class FeaturedCollection(ModelBase):
    id = PositiveAutoField(primary_key=True)
    application = models.PositiveIntegerField(choices=amo.APPS_CHOICES,
                                              db_column='application_id')
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE)
    locale = models.CharField(max_length=10, null=True)

    class Meta:
        db_table = 'featured_collections'

    def __str__(self):
        return u'%s (%s: %s)' % (self.collection, self.application,
                                 self.locale)

    @staticmethod
    def post_save_or_delete(sender, instance, **kwargs):
        Collection.update_featured_status(
            FeaturedCollection, instance.collection, **kwargs)


models.signals.post_save.connect(FeaturedCollection.post_save_or_delete,
                                 sender=FeaturedCollection)
models.signals.post_delete.connect(FeaturedCollection.post_save_or_delete,
                                   sender=FeaturedCollection)


class MonthlyPick(ModelBase):
    addon = models.ForeignKey(Addon, on_delete=models.CASCADE)
    blurb = models.TextField()
    image = models.URLField()
    locale = models.CharField(
        max_length=10, unique=True, null=True, blank=True,
        default=None)

    class Meta:
        db_table = 'monthly_pick'
