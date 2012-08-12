from cStringIO import StringIO
import json
from django.conf import settings
from django.conf.urls import patterns, url
from django.core.exceptions import ValidationError
from django.core.files import uploadhandler
from django.http import HttpResponse, QueryDict, Http404
from django.http.multipartparser import MultiPartParser
from django.views.decorators.csrf import csrf_exempt
from django.views.generic.base import View
import http


class ForbiddenException(Exception):
    pass

class ResourceMeta(object):
    allowed_methods = ['get']
    allowed_filters = ['pk']
    fields = []
    exclude_fields = []
    pk_pattern = '(?P<pk>\d+)'
    resource_name = None
    model = None
    form = None

    def __init__(self, meta):
        for field in dir(meta):
            val = getattr(meta, field)
            if hasattr(self, field) and not field.startswith('_'):
                setattr(self, field, val)


class ModelMetaClass(type):
    def __new__(cls, name, bases, attrs):
        new_class = super(ModelMetaClass, cls).__new__(cls, name, bases, attrs)
        meta = attrs.get('Meta', None)
        meta = ResourceMeta(meta)
        new_class._meta = meta
        if not new_class._meta.resource_name and new_class._meta.model:
            new_class._meta.resource_name = new_class._meta.model.__name__.lower()

        new_class.fields = meta.fields
        return new_class


class ModelResource(View):

    __metaclass__ = ModelMetaClass

    @property
    def model(self):
        return self._meta.model

    def serialize_object(self, object):
        attrs = {}
        for field in self.fields:
            field_value = getattr(object, field)
            field_value = field_value.id if hasattr(field_value, 'id') else field_value
            attrs[field] = field_value

        return json.dumps(attrs)

    def serialize(self, objects):
        return map(self.serialize_object, objects)

    def dispatch(self, request, *args, **kwargs):
        # Try to dispatch to the right method; if a method doesn't exist,
        # defer to the error handler. Also defer to the error handler if the
        # request method isn't on the approved list.
        http_methods_map = {
            'GET': 'get',
            'PUT': 'create',
            'POST': 'update',
            'DELETE': 'delete',
        }

        request_method = request.method
        if request_method == 'POST' and request.POST.get('_method'):
            request_method = request.POST.get('_method').upper()

        handler_name = http_methods_map.get(request_method)
        if request_method == 'GET' and not 'pk' in kwargs:
            handler_name = 'list'

        if request_method.lower() in self._meta.allowed_methods:
            handler = getattr(self, handler_name, self.http_method_not_allowed)
        else:
            return http.HttpForbidden()

        self.request = request
        self.args = args
        self.kwargs = kwargs

        try:
            ret = handler()
        except ValidationError, e:
            if request.is_ajax():
                return HttpResponse(json.dumps({
                    'error': 'Validation failed',
                    'error_code': 400,
                    'message': e.message_dict
                }))
            return http.HttpBadRequest(json.dumps(e.message_dict))

        except ForbiddenException, e:
            return http.HttpForbidden()

        except Http404, e:
            return http.HttpNotFound()

        if not ret:
            return HttpResponse()
#            return http.HttpNoContent()

        if handler_name == 'list':
            return HttpResponse(self.serialize(ret))

        return HttpResponse(self.serialize_object(ret))

    def get_query_set(self):
        """
        You can overwrite this methods
        for example:
            return self.model.objects.filter(user=request.user, is_active=True)
        """
        return self.model.objects.all()

    def list(self):
        """
        list all objects
        you should overwrite get_query_set method
        """
        queryset = self.get_query_set()
        return queryset.all()[:20]

    def get_object(self, pk, filters):
        return self.model.objects.get(pk=pk, **filters)

    def get(self):
        """
        Get a single instance of object
        """
        filters = {}
        pk = self.kwargs['pk']
        try:
            return self.get_object(pk, filters)
        except self.model.DoesNotExist:
            raise Http404

    @property
    def upload_handlers(self):
        self._upload_handlers = [uploadhandler.load_handler(handler, self)
                     for handler in settings.FILE_UPLOAD_HANDLERS]
        return self._upload_handlers

    def parse_request_data(self):
        if self.request.method == 'POST':
            return self.request.POST, self.request.FILES

        data = self.request.raw_post_data
        if self.request.META.get('CONTENT_TYPE', '').startswith('multipart'):
            data = StringIO(data)
            parser = MultiPartParser(self.request.META, data, self.upload_handlers)
            query =  parser.parse()
            return query

        return QueryDict(data), {}

    def get_form_for_request(self, instance=None):
        """
        get a form instance for this request
        it will get instance and pass to form instance automatically if method is 'POST'.
        """
        params, files = self.parse_request_data()
        form_class = self._meta.form
#        if self.request.method == 'POST':
        if instance:
            form = form_class(self.request, params, instance=instance)
        else:
            form = form_class(self.request, params)

        return form

    def process_form_errors(self, form):
        errors = {}
        for field, error in form.errors.items():
            errors[field] = error
        raise ValidationError(errors)

    def check_permission(self, object, operation):
        if not self.is_authenticated(object, operation):
            raise ForbiddenException()

    def create(self):
        """
        Create an object
        """
        form = self.get_form_for_request()

        if form.is_valid():
            return form.save(commit=True)
        else:
            self.process_form_errors(form)

    def update(self):
        instance = self.get()
        self.check_permission(instance, 'update')
        form = self.get_form_for_request(instance=instance)
        if form.is_valid():
            return form.save(commit=True)
        self.process_form_errors(form)

    def delete(self):
        """
        delete an object
        """
        object = self.get()
        self.check_permission(object, 'delete')
        self.delete_object(object)
#        object.delete()

    def delete_object(self, object):
        """
        overwrite this method to add your own delete object method.
        for example: set object.is_active = False other than really delete this object
        """
        object.delete()

    def is_authenticated(self, object, method):
        """
        is the current user has permission to do `method`
        method is ['get', 'create', 'update', 'delete']
        """
        raise NotImplementedError("Please overwrite is_authenticated function to grant client update and delete access")

    @classmethod
    def urls(cls):
        resource_name = cls._meta.resource_name
        pk_pattern = cls._meta.pk_pattern
        urlpatterns = patterns('',
            url(r"^(?P<resource_name>%s)/$" % (resource_name, ), csrf_exempt(cls.as_view()), name=resource_name),
            url(r"^(?P<resource_name>%s)/%s/$" % (resource_name, pk_pattern), csrf_exempt(cls.as_view()), name=resource_name),
        )
        return urlpatterns
