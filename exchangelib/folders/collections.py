import logging

from cached_property import threaded_cached_property

from .queryset import FOLDER_TRAVERSAL_CHOICES
from ..fields import FieldPath, InvalidField
from ..items import Persona, ITEM_TRAVERSAL_CHOICES, SHAPE_CHOICES, ID_ONLY
from ..properties import CalendarView
from ..queryset import QuerySet, SearchableMixIn, Q
from ..restriction import Restriction
from ..services import FindFolder, GetFolder, FindItem, FindPeople, SyncFolderItems, SyncFolderHierarchy, \
    SubscribeToPull, SubscribeToPush, SubscribeToStreaming
from ..util import require_account

log = logging.getLogger(__name__)


class SyncCompleted(Exception):
    """This is a really ugly way of returning the sync state."""

    def __init__(self, sync_state):
        super().__init__(sync_state)
        self.sync_state = sync_state


class FolderCollection(SearchableMixIn):
    """A class that implements an API for searching folders."""

    # These fields are required in a FindFolder or GetFolder call to properly identify folder types
    REQUIRED_FOLDER_FIELDS = ('name', 'folder_class')

    def __init__(self, account, folders):
        """Implement a search API on a collection of folders.

        :param account: An Account object
        :param folders: An iterable of folders, e.g. Folder.walk(), Folder.glob(), or [a.calendar, a.inbox]
        """
        self.account = account
        self._folders = folders

    @threaded_cached_property
    def folders(self):
        # Resolve the list of folders, in case it's a generator
        return list(self._folders)

    def __len__(self):
        return len(self.folders)

    def __iter__(self):
        yield from self.folders

    def get(self, *args, **kwargs):
        return QuerySet(self).get(*args, **kwargs)

    def all(self):
        return QuerySet(self).all()

    def none(self):
        return QuerySet(self).none()

    def filter(self, *args, **kwargs):
        """Find items in the folder(s).

        Non-keyword args may be a list of Q instances.

        Optional extra keyword arguments follow a Django-like QuerySet filter syntax (see
           https://docs.djangoproject.com/en/1.10/ref/models/querysets/#field-lookups).

        We don't support '__year' and other date-related lookups. We also don't support '__endswith' or '__iendswith'.

        We support the additional '__not' lookup in place of Django's exclude() for simple cases. For more complicated
        cases you need to create a Q object and use ~Q().

        Examples:

            my_account.inbox.filter(datetime_received__gt=EWSDateTime(2016, 1, 1))
            my_account.calendar.filter(start__range=(EWSDateTime(2016, 1, 1), EWSDateTime(2017, 1, 1)))
            my_account.tasks.filter(subject='Hi mom')
            my_account.tasks.filter(subject__not='Hi mom')
            my_account.tasks.filter(subject__contains='Foo')
            my_account.tasks.filter(subject__icontains='foo')

        'endswith' and 'iendswith' could be emulated by searching with 'contains' or 'icontains' and then
        post-processing items. Fetch the field in question with additional_fields and remove items where the search
        string is not a postfix.
        """
        return QuerySet(self).filter(*args, **kwargs)

    def exclude(self, *args, **kwargs):
        return QuerySet(self).exclude(*args, **kwargs)

    def people(self):
        return QuerySet(self).people()

    def view(self, start, end, max_items=None, *args, **kwargs):
        """Implement the CalendarView option to FindItem. The difference between 'filter' and 'view' is that 'filter'
        only returns the master CalendarItem for recurring items, while 'view' unfolds recurring items and returns all
        CalendarItem occurrences as one would normally expect when presenting a calendar.

        Supports the same semantics as filter, except for 'start' and 'end' keyword attributes which are both required
        and behave differently than filter. Here, they denote the start and end of the timespan of the view. All items
        the overlap the timespan are returned (items that end exactly on 'start' are also returned, for some reason).

        EWS does not allow combining CalendarView with search restrictions (filter and exclude).

        'max_items' defines the maximum number of items returned in this view. Optional.

        :param start:
        :param end:
        :param max_items:  (Default value = None)
        :return:
        """
        qs = QuerySet(self).filter(*args, **kwargs)
        qs.calendar_view = CalendarView(start=start, end=end, max_items=max_items)
        return qs

    def allowed_item_fields(self):
        # Return non-ID fields of all item classes allowed in this folder type
        fields = set()
        for item_model in self.supported_item_models:
            fields.update(set(item_model.supported_fields(version=self.account.version)))
        return fields

    @property
    def supported_item_models(self):
        return tuple(item_model for folder in self.folders for item_model in folder.supported_item_models)

    def validate_item_field(self, field, version):
        # For each field, check if the field is valid for any of the item models supported by this folder
        for item_model in self.supported_item_models:
            try:
                item_model.validate_field(field=field, version=version)
                break
            except InvalidField:
                continue
        else:
            raise InvalidField("%r is not a valid field on %s" % (field, self.supported_item_models))

    def find_items(self, q, shape=ID_ONLY, depth=None, additional_fields=None, order_fields=None,
                   calendar_view=None, page_size=None, max_items=None, offset=0):
        """Private method to call the FindItem service.

        :param q: a Q instance containing any restrictions
        :param shape: controls whether to return (id, chanegkey) tuples or Item objects. If additional_fields is
          non-null, we always return Item objects. (Default value = ID_ONLY)
        :param depth: controls the whether to return soft-deleted items or not. (Default value = None)
        :param additional_fields: the extra properties we want on the return objects. Default is no properties. Be aware
          that complex fields can only be fetched with fetch() (i.e. the GetItem service).
        :param order_fields: the SortOrder fields, if any (Default value = None)
        :param calendar_view: a CalendarView instance, if any (Default value = None)
        :param page_size: the requested number of items per page (Default value = None)
        :param max_items: the max number of items to return (Default value = None)
        :param offset: the offset relative to the first item in the item collection (Default value = 0)

        :return: a generator for the returned item IDs or items
        """
        if not self.folders:
            log.debug('Folder list is empty')
            return
        if q.is_never():
            log.debug('Query will never return results')
            return
        if shape not in SHAPE_CHOICES:
            raise ValueError("'shape' %s must be one of %s" % (shape, SHAPE_CHOICES))
        if depth is None:
            depth = self._get_default_item_traversal_depth()
        if depth not in ITEM_TRAVERSAL_CHOICES:
            raise ValueError("'depth' %s must be one of %s" % (depth, ITEM_TRAVERSAL_CHOICES))
        if additional_fields:
            for f in additional_fields:
                self.validate_item_field(field=f, version=self.account.version)
                if f.field.is_complex:
                    raise ValueError("find_items() does not support field '%s'. Use fetch() instead" % f.field.name)
        if calendar_view is not None and not isinstance(calendar_view, CalendarView):
            raise ValueError("'calendar_view' %s must be a CalendarView instance" % calendar_view)

        # Build up any restrictions
        if q.is_empty():
            restriction = None
            query_string = None
        elif q.query_string:
            restriction = None
            query_string = Restriction(q, folders=self.folders, applies_to=Restriction.ITEMS)
        else:
            restriction = Restriction(q, folders=self.folders, applies_to=Restriction.ITEMS)
            query_string = None
        log.debug(
            'Finding %s items in folders %s (shape: %s, depth: %s, additional_fields: %s, restriction: %s)',
            self.folders,
            self.account,
            shape,
            depth,
            additional_fields,
            restriction.q if restriction else None,
        )
        yield from FindItem(account=self.account, chunk_size=page_size).call(
            folders=self.folders,
            additional_fields=additional_fields,
            restriction=restriction,
            order_fields=order_fields,
            shape=shape,
            query_string=query_string,
            depth=depth,
            calendar_view=calendar_view,
            max_items=calendar_view.max_items if calendar_view else max_items,
            offset=offset,
        )

    def _get_single_folder(self):
        if len(self.folders) > 1:
            raise ValueError('Syncing folder hierarchy can only be done on a single folder')
        if not self.folders:
            log.debug('Folder list is empty')
            return None
        return self.folders[0]

    def find_people(self, q, shape=ID_ONLY, depth=None, additional_fields=None, order_fields=None,
                    page_size=None, max_items=None, offset=0):
        """Private method to call the FindPeople service.

        :param q: a Q instance containing any restrictions
        :param shape: controls whether to return (id, chanegkey) tuples or Persona objects. If additional_fields is
          non-null, we always return Persona objects. (Default value = ID_ONLY)
        :param depth: controls the whether to return soft-deleted items or not. (Default value = None)
        :param additional_fields: the extra properties we want on the return objects. Default is no properties.
        :param order_fields: the SortOrder fields, if any (Default value = None)
        :param page_size: the requested number of items per page (Default value = None)
        :param max_items: the max number of items to return (Default value = None)
        :param offset: the offset relative to the first item in the item collection (Default value = 0)

        :return: a generator for the returned personas
        """
        folder = self._get_single_folder()
        if not folder:
            return
        if q.is_never():
            log.debug('Query will never return results')
            return
        if shape not in SHAPE_CHOICES:
            raise ValueError("'shape' %s must be one of %s" % (shape, SHAPE_CHOICES))
        if depth is None:
            depth = self._get_default_item_traversal_depth()
        if depth not in ITEM_TRAVERSAL_CHOICES:
            raise ValueError("'depth' %s must be one of %s" % (depth, ITEM_TRAVERSAL_CHOICES))
        if additional_fields:
            for f in additional_fields:
                Persona.validate_field(field=f, version=self.account.version)
                if f.field.is_complex:
                    raise ValueError("find_people() does not support field '%s'" % f.field.name)

        # Build up any restrictions
        if q.is_empty():
            restriction = None
            query_string = None
        elif q.query_string:
            restriction = None
            query_string = Restriction(q, folders=[folder], applies_to=Restriction.ITEMS)
        else:
            restriction = Restriction(q, folders=[folder], applies_to=Restriction.ITEMS)
            query_string = None
        yield from FindPeople(account=self.account, chunk_size=page_size).call(
                folder=[folder],
                additional_fields=additional_fields,
                restriction=restriction,
                order_fields=order_fields,
                shape=shape,
                query_string=query_string,
                depth=depth,
                max_items=max_items,
                offset=offset,
        )

    def get_folder_fields(self, target_cls, is_complex=None):
        return {
            FieldPath(field=f) for f in target_cls.supported_fields(version=self.account.version)
            if is_complex is None or f.is_complex is is_complex
        }

    def _get_target_cls(self):
        # We may have root folders that don't support the same set of fields as normal folders. If there is a mix of
        # both folder types in self.folders, raise an error so we don't risk losing some fields in the query.
        from .base import Folder
        from .roots import RootOfHierarchy
        has_roots = False
        has_non_roots = False
        for f in self.folders:
            if isinstance(f, RootOfHierarchy):
                if has_non_roots:
                    raise ValueError('Cannot call GetFolder on a mix of folder types: {}'.format(self.folders))
                has_roots = True
            else:
                if has_roots:
                    raise ValueError('Cannot call GetFolder on a mix of folder types: {}'.format(self.folders))
                has_non_roots = True
        return RootOfHierarchy if has_roots else Folder

    def _get_default_traversal_depth(self, traversal_attr):
        unique_depths = {getattr(f, traversal_attr) for f in self.folders}
        if len(unique_depths) == 1:
            return unique_depths.pop()
        raise ValueError(
            'Folders in this collection do not have a common %s value. You need to define an explicit traversal depth'
            'with QuerySet.depth() (values: %s)' % (traversal_attr, unique_depths)
        )

    def _get_default_item_traversal_depth(self):
        # When searching folders, some folders require 'Shallow' and others 'Associated' traversal depth.
        return self._get_default_traversal_depth('DEFAULT_ITEM_TRAVERSAL_DEPTH')

    def _get_default_folder_traversal_depth(self):
        # When searching folders, some folders require 'Shallow' and others 'Deep' traversal depth.
        return self._get_default_traversal_depth('DEFAULT_FOLDER_TRAVERSAL_DEPTH')

    def resolve(self):
        # Looks up the folders or folder IDs in the collection and returns full Folder instances with all fields set.
        from .base import BaseFolder
        resolveable_folders = []
        for f in self.folders:
            if isinstance(f, BaseFolder) and not f.get_folder_allowed:
                log.debug('GetFolder not allowed on folder %s. Non-complex fields must be fetched with FindFolder', f)
                yield f
            else:
                resolveable_folders.append(f)
        # Fetch all properties for the remaining folders of folder IDs
        additional_fields = self.get_folder_fields(target_cls=self._get_target_cls(), is_complex=None)
        yield from self.__class__(account=self.account, folders=resolveable_folders).get_folders(
                additional_fields=additional_fields
        )

    @require_account
    def find_folders(self, q=None, shape=ID_ONLY, depth=None, additional_fields=None, page_size=None, max_items=None,
                     offset=0):
        # 'depth' controls whether to return direct children or recurse into sub-folders
        from .base import BaseFolder, Folder
        if q is None:
            q = Q()
        if not self.folders:
            log.debug('Folder list is empty')
            return
        if q.is_never():
            log.debug('Query will never return results')
            return
        if q.is_empty():
            restriction = None
        else:
            restriction = Restriction(q, folders=self.folders, applies_to=Restriction.FOLDERS)
        if shape not in SHAPE_CHOICES:
            raise ValueError("'shape' %s must be one of %s" % (shape, SHAPE_CHOICES))
        if depth is None:
            depth = self._get_default_folder_traversal_depth()
        if depth not in FOLDER_TRAVERSAL_CHOICES:
            raise ValueError("'depth' %s must be one of %s" % (depth, FOLDER_TRAVERSAL_CHOICES))
        if additional_fields is None:
            # Default to all non-complex properties. Subfolders will always be of class Folder
            additional_fields = self.get_folder_fields(target_cls=Folder, is_complex=False)
        else:
            for f in additional_fields:
                if f.field.is_complex:
                    raise ValueError("find_folders() does not support field '%s'. Use get_folders()." % f.field.name)

        # Add required fields
        additional_fields.update(
            (FieldPath(field=BaseFolder.get_field_by_fieldname(f)) for f in self.REQUIRED_FOLDER_FIELDS)
        )

        yield from FindFolder(account=self.account, chunk_size=page_size).call(
                folders=self.folders,
                additional_fields=additional_fields,
                restriction=restriction,
                shape=shape,
                depth=depth,
                max_items=max_items,
                offset=offset,
        )

    def get_folders(self, additional_fields=None):
        # Expand folders with their full set of properties
        from .base import BaseFolder
        if not self.folders:
            log.debug('Folder list is empty')
            return
        if additional_fields is None:
            # Default to all complex properties
            additional_fields = self.get_folder_fields(target_cls=self._get_target_cls(), is_complex=True)

        # Add required fields
        additional_fields.update(
            (FieldPath(field=BaseFolder.get_field_by_fieldname(f)) for f in self.REQUIRED_FOLDER_FIELDS)
        )

        yield from GetFolder(account=self.account).call(
                folders=self.folders,
                additional_fields=additional_fields,
                shape=ID_ONLY,
        )

    def subscribe_to_pull(self, event_types=SubscribeToPull.EVENT_TYPES, watermark=None, timeout=60):
        if not self.folders:
            log.debug('Folder list is empty')
            return
        yield from SubscribeToPull(account=self.account).call(
            folders=self.folders, event_types=event_types, watermark=watermark, timeout=timeout,
        )

    def subscribe_to_push(self, callback_url, event_types=SubscribeToPush.EVENT_TYPES, watermark=None,
                          status_frequency=1):
        if not self.folders:
            log.debug('Folder list is empty')
            return
        yield from SubscribeToPush(account=self.account).call(
            folders=self.folders, event_types=event_types, watermark=watermark, status_frequency=status_frequency,
            url=callback_url,
        )

    def subscribe_to_streaming(self, event_types=SubscribeToPush.EVENT_TYPES):
        if not self.folders:
            log.debug('Folder list is empty')
            return
        yield from SubscribeToStreaming(account=self.account).call(folders=self.folders, event_types=event_types)

    def sync_items(self, sync_state=None, only_fields=None, ignore=None, max_changes_returned=None, sync_scope=None):
        folder = self._get_single_folder()
        if not folder:
            return
        if only_fields is None:
            # We didn't restrict list of field paths. Get all fields from the server, including extended properties.
            additional_fields = {FieldPath(field=f) for f in folder.allowed_item_fields(version=self.account.version)}
        else:
            for field in only_fields:
                folder.validate_item_field(field=field, version=self.account.version)
            # Remove ItemId and ChangeKey. We get them unconditionally
            additional_fields = {f for f in folder.normalize_fields(fields=only_fields) if not f.field.is_attribute}

        svc = SyncFolderItems(account=self.account)
        while True:
            yield from svc.call(
                folder=folder,
                shape=ID_ONLY,
                additional_fields=additional_fields,
                sync_state=sync_state,
                ignore=ignore,
                max_changes_returned=max_changes_returned,
                sync_scope=sync_scope,
            )
            if svc.sync_state == sync_state:
                # We sometimes get the same sync_state back, even though includes_last_item_in_range is False. Stop here
                break
            sync_state = svc.sync_state  # Set the new sync state in the next call
            if svc.includes_last_item_in_range:  # Try again if there are more items
                break
        raise SyncCompleted(sync_state=svc.sync_state)

    def sync_hierarchy(self, sync_state=None, only_fields=None):
        folder = self._get_single_folder()
        if not folder:
            return
        if only_fields is None:
            # We didn't restrict list of field paths. Get all fields from the server, including extended properties.
            additional_fields = {FieldPath(field=f) for f in folder.supported_fields(version=self.account.version)}
        else:
            for f in only_fields:
                folder.validate_field(field=f, version=self.account.version)
            # Remove ItemId and ChangeKey. We get them unconditionally
            additional_fields = {f for f in folder.normalize_fields(fields=only_fields) if not f.field.is_attribute}

        # Add required fields
        additional_fields.update(
            (FieldPath(field=folder.get_field_by_fieldname(f)) for f in self.REQUIRED_FOLDER_FIELDS)
        )

        svc = SyncFolderHierarchy(account=self.account)
        while True:
            yield from svc.call(
                folder=folder,
                shape=ID_ONLY,
                additional_fields=additional_fields,
                sync_state=sync_state,
            )
            if svc.sync_state == sync_state:
                # We sometimes get the same sync_state back, even though includes_last_item_in_range is False. Stop here
                break
            sync_state = svc.sync_state  # Set the new sync state in the next call
            if svc.includes_last_item_in_range:  # Try again if there are more items
                break
        raise SyncCompleted(sync_state=svc.sync_state)
