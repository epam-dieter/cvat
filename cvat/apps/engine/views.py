# Copyright (C) 2018-2021 Intel Corporation
#
# SPDX-License-Identifier: MIT

import errno
import io
import os
import os.path as osp
import pytz
import shutil
import traceback
from datetime import datetime
from distutils.util import strtobool
from tempfile import mkstemp, NamedTemporaryFile

import cv2
from django.db.models.query import Prefetch
import django_rq
from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.http import HttpResponse, HttpResponseNotFound, HttpResponseBadRequest
from django.utils import timezone
from django.utils.decorators import method_decorator
from django_filters import rest_framework as filters
from django_filters.rest_framework import DjangoFilterBackend
from drf_yasg import openapi
from drf_yasg.inspectors import CoreAPICompatInspector, NotHandled, FieldInspector
from drf_yasg.utils import swagger_auto_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.permissions import SAFE_METHODS
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from django_sendfile import sendfile

import cvat.apps.dataset_manager as dm
import cvat.apps.dataset_manager.views  # pylint: disable=unused-import
from cvat.apps.engine.cloud_provider import get_cloud_storage_instance, Credentials, Status
from cvat.apps.dataset_manager.bindings import CvatImportError
from cvat.apps.dataset_manager.serializers import DatasetFormatsSerializer
from cvat.apps.engine.frame_provider import FrameProvider
from cvat.apps.engine.media_extractors import ImageListReader
from cvat.apps.engine.mime_types import mimetypes
from cvat.apps.engine.models import (
    Job, StatusChoice, Task, Project, Issue, Data,
    Comment, StorageMethodChoice, StorageChoice, Image,
    CredentialsTypeChoice, CloudProviderChoice
)
from cvat.apps.engine.models import CloudStorage as CloudStorageModel
from cvat.apps.engine.serializers import (
    AboutSerializer, AnnotationFileSerializer, BasicUserSerializer,
    DataMetaSerializer, DataSerializer, ExceptionSerializer,
    FileInfoSerializer, JobReadSerializer, JobWriteSerializer, LabeledDataSerializer,
    LogEventSerializer, ProjectSerializer, ProjectSearchSerializer,
    RqStatusSerializer, TaskSerializer, UserSerializer, PluginsSerializer, IssueReadSerializer,
    IssueWriteSerializer, CommentReadSerializer, CommentWriteSerializer, CloudStorageSerializer,
    BaseCloudStorageSerializer, DatasetFileSerializer)

from utils.dataset_manifest import ImageManifestManager
from cvat.apps.engine.utils import av_scan_paths
from cvat.apps.engine import backup
from cvat.apps.engine.mixins import UploadMixin

from . import models, task
from .log import clogger, slogger
from cvat.apps.iam.permissions import (CloudStoragePermission,
    CommentPermission, IssuePermission, JobPermission, ProjectPermission,
    TaskPermission, UserPermission)

class ServerViewSet(viewsets.ViewSet):
    serializer_class = None
    iam_organization_field = None

    # To get nice documentation about ServerViewSet actions it is necessary
    # to implement the method. By default, ViewSet doesn't provide it.
    def get_serializer(self, *args, **kwargs):
        pass

    @staticmethod
    @swagger_auto_schema(method='get', operation_summary='Method provides basic CVAT information',
        responses={'200': AboutSerializer})
    @action(detail=False, methods=['GET'], serializer_class=AboutSerializer)
    def about(request):
        from cvat import __version__ as cvat_version
        about = {
            "name": "Computer Vision Annotation Tool",
            "version": cvat_version,
            "description": "CVAT is completely re-designed and re-implemented " +
                "version of Video Annotation Tool from Irvine, California " +
                "tool. It is free, online, interactive video and image annotation " +
                "tool for computer vision. It is being used by our team to " +
                "annotate million of objects with different properties. Many UI " +
                "and UX decisions are based on feedbacks from professional data " +
                "annotation team."
        }
        serializer = AboutSerializer(data=about)
        if serializer.is_valid(raise_exception=True):
            return Response(data=serializer.data)

    @staticmethod
    @swagger_auto_schema(method='post', request_body=ExceptionSerializer)
    @action(detail=False, methods=['POST'], serializer_class=ExceptionSerializer)
    def exception(request):
        """
        Saves an exception from a client on the server
        Sends logs to the ELK if it is connected
        """
        serializer = ExceptionSerializer(data=request.data)
        if serializer.is_valid(raise_exception=True):
            additional_info = {
                "username": request.user.username,
                "name": "Send exception",
            }
            message = JSONRenderer().render({**serializer.data, **additional_info}).decode('UTF-8')
            jid = serializer.data.get("job_id")
            tid = serializer.data.get("task_id")
            if jid:
                clogger.job[jid].error(message)
            elif tid:
                clogger.task[tid].error(message)
            else:
                clogger.glob.error(message)

            return Response(serializer.data, status=status.HTTP_201_CREATED)

    @staticmethod
    @swagger_auto_schema(method='post', request_body=LogEventSerializer(many=True))
    @action(detail=False, methods=['POST'], serializer_class=LogEventSerializer)
    def logs(request):
        """
        Saves logs from a client on the server
        Sends logs to the ELK if it is connected
        """
        serializer = LogEventSerializer(many=True, data=request.data)
        if serializer.is_valid(raise_exception=True):
            user = { "username": request.user.username }
            for event in serializer.data:
                message = JSONRenderer().render({**event, **user}).decode('UTF-8')
                jid = event.get("job_id")
                tid = event.get("task_id")
                if jid:
                    clogger.job[jid].info(message)
                elif tid:
                    clogger.task[tid].info(message)
                else:
                    clogger.glob.info(message)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

    @staticmethod
    @swagger_auto_schema(
        method='get', operation_summary='Returns all files and folders that are on the server along specified path',
        manual_parameters=[openapi.Parameter('directory', openapi.IN_QUERY, type=openapi.TYPE_STRING, description='Directory to browse')],
        responses={'200' : FileInfoSerializer(many=True)}
    )
    @action(detail=False, methods=['GET'], serializer_class=FileInfoSerializer)
    def share(request):
        param = request.query_params.get('directory', '/')
        if param.startswith("/"):
            param = param[1:]
        directory = os.path.abspath(os.path.join(settings.SHARE_ROOT, param))

        if directory.startswith(settings.SHARE_ROOT) and os.path.isdir(directory):
            data = []
            content = os.scandir(directory)
            for entry in content:
                entry_type = None
                if entry.is_file():
                    entry_type = "REG"
                elif entry.is_dir():
                    entry_type = "DIR"

                if entry_type:
                    data.append({"name": entry.name, "type": entry_type})

            serializer = FileInfoSerializer(many=True, data=data)
            if serializer.is_valid(raise_exception=True):
                return Response(serializer.data)
        else:
            return Response("{} is an invalid directory".format(param),
                status=status.HTTP_400_BAD_REQUEST)

    @staticmethod
    @swagger_auto_schema(method='get', operation_summary='Method provides the list of supported annotations formats',
        responses={'200': DatasetFormatsSerializer()})
    @action(detail=False, methods=['GET'], url_path='annotation/formats')
    def annotation_formats(request):
        data = dm.views.get_all_formats()
        return Response(DatasetFormatsSerializer(data).data)

    @staticmethod
    @swagger_auto_schema(method='get', operation_summary='Method provides allowed plugins.',
        responses={'200': PluginsSerializer()})
    @action(detail=False, methods=['GET'], url_path='plugins', serializer_class=PluginsSerializer)
    def plugins(request):
        response = {
            'GIT_INTEGRATION': apps.is_installed('cvat.apps.dataset_repo'),
            'ANALYTICS':       False,
            'MODELS':          False,
            'PREDICT':         apps.is_installed('cvat.apps.training')
        }
        if strtobool(os.environ.get("CVAT_ANALYTICS", '0')):
            response['ANALYTICS'] = True
        if strtobool(os.environ.get("CVAT_SERVERLESS", '0')):
            response['MODELS'] = True
        return Response(response)


class ProjectFilter(filters.FilterSet):
    name = filters.CharFilter(field_name="name", lookup_expr="icontains")
    owner = filters.CharFilter(field_name="owner__username", lookup_expr="icontains")
    assignee = filters.CharFilter(field_name="assignee__username", lookup_expr="icontains")
    status = filters.CharFilter(field_name="status", lookup_expr="icontains")

    class Meta:
        model = models.Project
        fields = ("id", "name", "owner", "status")

@method_decorator(name='list', decorator=swagger_auto_schema(
    operation_summary='Returns a paginated list of projects according to query parameters (12 projects per page)',
    manual_parameters=[
        openapi.Parameter('id', openapi.IN_QUERY, description="A unique number value identifying this project",
            type=openapi.TYPE_NUMBER),
        openapi.Parameter('name', openapi.IN_QUERY, description="Find all projects where name contains a parameter value",
            type=openapi.TYPE_STRING),
        openapi.Parameter('owner', openapi.IN_QUERY, description="Find all project where owner name contains a parameter value",
            type=openapi.TYPE_STRING),
        openapi.Parameter('status', openapi.IN_QUERY, description="Find all projects with a specific status",
            type=openapi.TYPE_STRING, enum=[str(i) for i in StatusChoice]),
        openapi.Parameter('names_only', openapi.IN_QUERY, description="Returns only names and id's of projects.",
            type=openapi.TYPE_BOOLEAN)]))
@method_decorator(name='create', decorator=swagger_auto_schema(operation_summary='Method creates a new project'))
@method_decorator(name='retrieve', decorator=swagger_auto_schema(operation_summary='Method returns details of a specific project'))
@method_decorator(name='destroy', decorator=swagger_auto_schema(operation_summary='Method deletes a specific project'))
@method_decorator(name='partial_update', decorator=swagger_auto_schema(operation_summary='Methods does a partial update of chosen fields in a project'))
class ProjectViewSet(viewsets.ModelViewSet):
    queryset = models.Project.objects.prefetch_related(Prefetch('label_set',
        queryset=models.Label.objects.order_by('id')
    ))

    search_fields = ("name", "owner__username", "assignee__username", "status")
    filterset_class = ProjectFilter
    ordering_fields = ("id", "name", "owner", "status", "assignee")
    ordering = ("-id",)
    http_method_names = ('get', 'post', 'head', 'patch', 'delete')
    iam_organization_field = 'organization'

    def get_serializer_class(self):
        if self.request.path.endswith('tasks'):
            return TaskSerializer
        if self.request.query_params and self.request.query_params.get("names_only") == "true":
            return ProjectSearchSerializer
        else:
            return ProjectSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == 'list':
            perm = ProjectPermission('list', self.request, self)
            queryset = perm.filter(queryset)
        return queryset

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user,
            organization=self.request.iam_context['organization'])

    @swagger_auto_schema(
        method='get',
        operation_summary='Returns information of the tasks of the project with the selected id',
        responses={'200': TaskSerializer(many=True)})
    @action(detail=True, methods=['GET'], serializer_class=TaskSerializer)
    def tasks(self, request, pk):
        self.get_object() # force to call check_object_permissions
        queryset = Task.objects.filter(project_id=pk).order_by('-id')

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True,
                context={"request": request})
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True,
            context={"request": request})
        return Response(serializer.data)


    @swagger_auto_schema(method='get', operation_summary='Export project as a dataset in a specific format',
        manual_parameters=[
            openapi.Parameter('format', openapi.IN_QUERY,
                description="Desired output format name\nYou can get the list of supported formats at:\n/server/annotation/formats",
                type=openapi.TYPE_STRING, required=True),
            openapi.Parameter('filename', openapi.IN_QUERY,
                description="Desired output file name",
                type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('action', in_=openapi.IN_QUERY,
                description='Used to start downloading process after annotation file had been created',
                type=openapi.TYPE_STRING, required=False, enum=['download', 'import_status'])
        ],
        responses={'202': openapi.Response(description='Exporting has been started'),
            '201': openapi.Response(description='Output file is ready for downloading'),
            '200': openapi.Response(description='Download of file started'),
            '405': openapi.Response(description='Format is not available'),
        }
    )
    @swagger_auto_schema(method='post', operation_summary='Import dataset in specific format as a project',
        manual_parameters=[
            openapi.Parameter('format', openapi.IN_QUERY,
                description="Desired dataset format name\nYou can get the list of supported formats at:\n/server/annotation/formats",
                type=openapi.TYPE_STRING, required=True)
        ],
        responses={'202': openapi.Response(description='Exporting has been started'),
            '400': openapi.Response(description='Failed to import dataset'),
            '405': openapi.Response(description='Format is not available'),
        }
    )
    @action(detail=True, methods=['GET', 'POST'], serializer_class=None,
        url_path='dataset')
    def dataset(self, request, pk):
        db_project = self.get_object() # force to call check_object_permissions

        if request.method == 'POST':
            format_name = request.query_params.get("format", "")

            return _import_project_dataset(
                request=request,
                rq_id=f"/api/project/{pk}/dataset_import",
                rq_func=dm.project.import_dataset_as_project,
                pk=pk,
                format_name=format_name,
            )
        else:
            action = request.query_params.get("action", "").lower()
            if action in ("import_status",):
                queue = django_rq.get_queue("default")
                rq_job = queue.fetch_job(f"/api/project/{pk}/dataset_import")
                if rq_job is None:
                    return Response(status=status.HTTP_404_NOT_FOUND)
                elif rq_job.is_finished:
                    os.close(rq_job.meta['tmp_file_descriptor'])
                    os.remove(rq_job.meta['tmp_file'])
                    rq_job.delete()
                    return Response(status=status.HTTP_201_CREATED)
                elif rq_job.is_failed:
                    os.close(rq_job.meta['tmp_file_descriptor'])
                    os.remove(rq_job.meta['tmp_file'])
                    rq_job.delete()
                    return Response(
                        data=str(rq_job.exc_info),
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )
                else:
                    return Response(
                        data=self._get_rq_response('default', f'/api/project/{pk}/dataset_import'),
                        status=status.HTTP_202_ACCEPTED
                    )
            else:
                format_name = request.query_params.get("format", "")
                return _export_annotations(
                    db_instance=db_project,
                    rq_id="/api/project/{}/dataset/{}".format(pk, format_name),
                    request=request,
                    action=action,
                    callback=dm.views.export_project_as_dataset,
                    format_name=format_name,
                    filename=request.query_params.get("filename", "").lower(),
                )

    @swagger_auto_schema(method='get', operation_summary='Method allows to download project annotations',
        manual_parameters=[
            openapi.Parameter('format', openapi.IN_QUERY,
                description="Desired output format name\nYou can get the list of supported formats at:\n/server/annotation/formats",
                type=openapi.TYPE_STRING, required=True),
            openapi.Parameter('filename', openapi.IN_QUERY,
                description="Desired output file name",
                type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('action', in_=openapi.IN_QUERY,
                description='Used to start downloading process after annotation file had been created',
                type=openapi.TYPE_STRING, required=False, enum=['download'])
        ],
        responses={
            '202': openapi.Response(description='Dump of annotations has been started'),
            '201': openapi.Response(description='Annotations file is ready to download'),
            '200': openapi.Response(description='Download of file started'),
            '405': openapi.Response(description='Format is not available'),
            '401': openapi.Response(description='Format is not specified'),
        }
    )
    @action(detail=True, methods=['GET'],
        serializer_class=LabeledDataSerializer)
    def annotations(self, request, pk):
        db_project = self.get_object() # force to call check_object_permissions
        format_name = request.query_params.get('format')
        if format_name:
            return _export_annotations(db_instance=db_project,
                rq_id="/api/projects/{}/annotations/{}".format(pk, format_name),
                request=request,
                action=request.query_params.get("action", "").lower(),
                callback=dm.views.export_project_annotations,
                format_name=format_name,
                filename=request.query_params.get("filename", "").lower(),
            )
        else:
            return Response("Format is not specified",status=status.HTTP_400_BAD_REQUEST)

    @action(methods=['GET'], detail=True, url_path='backup')
    def export_backup(self, request, pk=None):
        db_project = self.get_object() # force to call check_object_permissions
        return backup.export(db_project, request)

    @action(detail=False, methods=['POST'], url_path='backup')
    def import_backup(self, request, pk=None):
        return backup.import_project(request)

    @staticmethod
    def _get_rq_response(queue, job_id):
        queue = django_rq.get_queue(queue)
        job = queue.fetch_job(job_id)
        response = {}
        if job is None or job.is_finished:
            response = { "state": "Finished" }
        elif job.is_queued:
            response = { "state": "Queued" }
        elif job.is_failed:
            response = { "state": "Failed", "message": job.exc_info }
        else:
            response = { "state": "Started" }
            response['message'] = job.meta.get('status', '')
            response['progress'] = job.meta.get('progress', 0.)

        return response


class DataChunkGetter:
    def __init__(self, data_type, data_num, data_quality, task_dim):
        possible_data_type_values = ('chunk', 'frame', 'preview', 'context_image')
        possible_quality_values = ('compressed', 'original')

        if not data_type or data_type not in possible_data_type_values:
            raise ValidationError('Data type not specified or has wrong value')
        elif data_type == 'chunk' or data_type == 'frame':
            if not data_num:
                raise ValidationError('Number is not specified')
            elif data_quality not in possible_quality_values:
                raise ValidationError('Wrong quality value')

        self.type = data_type
        self.number = int(data_num) if data_num else None
        self.quality = FrameProvider.Quality.COMPRESSED \
            if data_quality == 'compressed' else FrameProvider.Quality.ORIGINAL

        self.dimension = task_dim


    def __call__(self, request, start, stop, db_data):
        if not db_data:
            raise NotFound(detail='Cannot find requested data')

        frame_provider = FrameProvider(db_data, self.dimension)

        if self.type == 'chunk':
            start_chunk = frame_provider.get_chunk_number(start)
            stop_chunk = frame_provider.get_chunk_number(stop)
            if not (start_chunk <= self.number <= stop_chunk):
                raise ValidationError('The chunk number should be in ' +
                    f'[{start_chunk}, {stop_chunk}] range')

            # TODO: av.FFmpegError processing
            if settings.USE_CACHE and db_data.storage_method == StorageMethodChoice.CACHE:
                buff, mime_type = frame_provider.get_chunk(self.number, self.quality)
                return HttpResponse(buff.getvalue(), content_type=mime_type)

            # Follow symbol links if the chunk is a link on a real image otherwise
            # mimetype detection inside sendfile will work incorrectly.
            path = os.path.realpath(frame_provider.get_chunk(self.number, self.quality))
            return sendfile(request, path)

        elif self.type == 'frame':
            if not (start <= self.number <= stop):
                raise ValidationError('The frame number should be in ' +
                    f'[{start}, {stop}] range')

            buf, mime = frame_provider.get_frame(self.number, self.quality)
            return HttpResponse(buf.getvalue(), content_type=mime)

        elif self.type == 'preview':
            return sendfile(request, frame_provider.get_preview())

        elif self.type == 'context_image':
            if not (start <= self.number <= stop):
                raise ValidationError('The frame number should be in ' +
                    f'[{start}, {stop}] range')

            image = Image.objects.get(data_id=db_data.id, frame=self.number)
            for i in image.related_files.all():
                path = os.path.realpath(str(i.path))
                image = cv2.imread(path)
                success, result = cv2.imencode('.JPEG', image)
                if not success:
                    raise Exception('Failed to encode image to ".jpeg" format')
                return HttpResponse(io.BytesIO(result.tobytes()), content_type='image/jpeg')
            return Response(data='No context image related to the frame',
                status=status.HTTP_404_NOT_FOUND)
        else:
            return Response(data='unknown data type {}.'.format(self.type),
                status=status.HTTP_400_BAD_REQUEST)

class TaskFilter(filters.FilterSet):
    project = filters.CharFilter(field_name="project__name", lookup_expr="icontains")
    name = filters.CharFilter(field_name="name", lookup_expr="icontains")
    owner = filters.CharFilter(field_name="owner__username", lookup_expr="icontains")
    mode = filters.CharFilter(field_name="mode", lookup_expr="icontains")
    status = filters.CharFilter(field_name="status", lookup_expr="icontains")
    assignee = filters.CharFilter(field_name="assignee__username", lookup_expr="icontains")

    class Meta:
        model = Task
        fields = ("id", "project_id", "project", "name", "owner", "mode", "status",
            "assignee")

class DjangoFilterInspector(CoreAPICompatInspector):
    def get_filter_parameters(self, filter_backend):
        if isinstance(filter_backend, DjangoFilterBackend):
            result = super(DjangoFilterInspector, self).get_filter_parameters(filter_backend)
            res = result.copy()

            for param in result:
                if param.get('name') == 'project_id' or param.get('name') == 'project':
                    res.remove(param)
            return res

        return NotHandled

@method_decorator(name='list', decorator=swagger_auto_schema(
    operation_summary='Returns a paginated list of tasks according to query parameters (10 tasks per page)',
    manual_parameters=[
            openapi.Parameter('id',openapi.IN_QUERY,description="A unique number value identifying this task",type=openapi.TYPE_NUMBER),
            openapi.Parameter('name', openapi.IN_QUERY, description="Find all tasks where name contains a parameter value", type=openapi.TYPE_STRING),
            openapi.Parameter('owner', openapi.IN_QUERY, description="Find all tasks where owner name contains a parameter value", type=openapi.TYPE_STRING),
            openapi.Parameter('mode', openapi.IN_QUERY, description="Find all tasks with a specific mode", type=openapi.TYPE_STRING, enum=['annotation', 'interpolation']),
            openapi.Parameter('status', openapi.IN_QUERY, description="Find all tasks with a specific status", type=openapi.TYPE_STRING,enum=['annotation','validation','completed']),
            openapi.Parameter('assignee', openapi.IN_QUERY, description="Find all tasks where assignee name contains a parameter value", type=openapi.TYPE_STRING)
        ],
    filter_inspectors=[DjangoFilterInspector]))
@method_decorator(name='create', decorator=swagger_auto_schema(operation_summary='Method creates a new task in a database without any attached images and videos'))
@method_decorator(name='retrieve', decorator=swagger_auto_schema(operation_summary='Method returns details of a specific task'))
@method_decorator(name='update', decorator=swagger_auto_schema(operation_summary='Method updates a task by id'))
@method_decorator(name='destroy', decorator=swagger_auto_schema(operation_summary='Method deletes a specific task, all attached jobs, annotations, and data'))
@method_decorator(name='partial_update', decorator=swagger_auto_schema(operation_summary='Methods does a partial update of chosen fields in a task'))
class TaskViewSet(UploadMixin, viewsets.ModelViewSet):
    queryset = Task.objects.prefetch_related(
            Prefetch('label_set', queryset=models.Label.objects.order_by('id')),
            "label_set__attributespec_set",
            "segment_set__job_set",
        ).order_by('-id')
    serializer_class = TaskSerializer
    search_fields = ("name", "owner__username", "mode", "status")
    filterset_class = TaskFilter
    ordering_fields = ("id", "name", "owner", "status", "assignee", "subset")
    iam_organization_field = 'organization'

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == 'list':
            perm = TaskPermission('list', self.request, self)
            queryset = perm.filter(queryset)

        return queryset

    @action(detail=False, methods=['POST'], url_path='backup')
    def import_backup(self, request, pk=None):
        return backup.import_task(request)

    @action(methods=['GET'], detail=True, url_path='backup')
    def export_backup(self, request, pk=None):
        db_task = self.get_object() # force to call check_object_permissions
        return backup.export(db_task, request)

    def perform_update(self, serializer):
        instance = serializer.instance
        updated_instance = serializer.save()
        if instance.project:
            instance.project.save()
        if updated_instance.project:
            updated_instance.project.save()

    def perform_create(self, serializer):
        instance = serializer.save(owner=self.request.user,
            organization=self.request.iam_context['organization'])
        if instance.project:
            db_project = instance.project
            db_project.save()
            assert instance.organization == db_project.organization

    def perform_destroy(self, instance):
        task_dirname = instance.get_task_dirname()
        super().perform_destroy(instance)
        shutil.rmtree(task_dirname, ignore_errors=True)
        if instance.data and not instance.data.tasks.all():
            shutil.rmtree(instance.data.get_data_dirname(), ignore_errors=True)
            instance.data.delete()
        if instance.project:
            db_project = instance.project
            db_project.save()

    @swagger_auto_schema(
        method='get',
        operation_summary='Returns a list of jobs for a specific task',
        responses={'200': JobReadSerializer(many=True)})
    @action(detail=True, methods=['GET'], serializer_class=JobReadSerializer)
    def jobs(self, request, pk):
        self.get_object() # force to call check_object_permissions
        queryset = Job.objects.filter(segment__task_id=pk)
        serializer = JobReadSerializer(queryset, many=True,
            context={"request": request})

        return Response(serializer.data)

    def upload_finished(self, request):
        db_task = self.get_object() # call check_object_permissions as well
        task_data = db_task.data
        serializer = DataSerializer(task_data, data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data.items())
        uploaded_files = task_data.get_uploaded_files()
        uploaded_files.extend(data.get('client_files'))
        serializer.validated_data.update({'client_files': uploaded_files})

        db_data = serializer.save()
        db_task.data = db_data
        db_task.save()
        data = {k: v for k, v in serializer.data.items()}

        data['use_zip_chunks'] = serializer.validated_data['use_zip_chunks']
        data['use_cache'] = serializer.validated_data['use_cache']
        data['copy_data'] = serializer.validated_data['copy_data']
        if data['use_cache']:
            db_task.data.storage_method = StorageMethodChoice.CACHE
            db_task.data.save(update_fields=['storage_method'])
        if data['server_files'] and not data.get('copy_data'):
            db_task.data.storage = StorageChoice.SHARE
            db_task.data.save(update_fields=['storage'])
        if db_data.cloud_storage:
            db_task.data.storage = StorageChoice.CLOUD_STORAGE
            db_task.data.save(update_fields=['storage'])
            # if the value of stop_frame is 0, then inside the function we cannot know
            # the value specified by the user or it's default value from the database
        if 'stop_frame' not in serializer.validated_data:
            data['stop_frame'] = None
        task.create(db_task.id, data)
        return Response(serializer.data, status=status.HTTP_202_ACCEPTED)

    @swagger_auto_schema(method='post', operation_summary='Method permanently attaches images or video to a task. Supports tus uploads, see more https://tus.io/',
        request_body=DataSerializer,
        manual_parameters=[
                openapi.Parameter('Upload-Start', in_=openapi.IN_HEADER, type=openapi.TYPE_BOOLEAN,
                    description="Initializes data upload. No data should be sent with this header"),
                openapi.Parameter('Upload-Multiple', in_=openapi.IN_HEADER, type=openapi.TYPE_BOOLEAN,
                    description="Indicates that data with this request are single or multiple files that should be attached to a task"),
                openapi.Parameter('Upload-Finish', in_=openapi.IN_HEADER, type=openapi.TYPE_BOOLEAN,
                    description="Finishes data upload. Can be combined with Upload-Start header to create task data with one request"),
         ]
    )
    @swagger_auto_schema(method='get', operation_summary='Method returns data for a specific task',
        manual_parameters=[
            openapi.Parameter('type', in_=openapi.IN_QUERY, required=True, type=openapi.TYPE_STRING,
                enum=['chunk', 'frame', 'preview', 'context_image'],
                description="Specifies the type of the requested data"),
            openapi.Parameter('quality', in_=openapi.IN_QUERY, required=True, type=openapi.TYPE_STRING,
                enum=['compressed', 'original'],
                description="Specifies the quality level of the requested data, doesn't matter for 'preview' type"),
            openapi.Parameter('number', in_=openapi.IN_QUERY, required=True, type=openapi.TYPE_NUMBER,
                description="A unique number value identifying chunk or frame, doesn't matter for 'preview' type"),
            ]
    )
    @action(detail=True, methods=['OPTIONS', 'POST', 'GET'], url_path=r'data/?$')
    def data(self, request, pk):
        db_task = self.get_object() # call check_object_permissions as well
        if request.method == 'POST' or request.method == 'OPTIONS':
            task_data = db_task.data
            if not task_data:
                task_data = Data.objects.create()
                task_data.make_dirs()
                db_task.data = task_data
                db_task.save()
            elif task_data.size != 0:
                return Response(data='Adding more data is not supported',
                    status=status.HTTP_400_BAD_REQUEST)
            return self.upload_data(request)

        else:
            data_type = request.query_params.get('type', None)
            data_num = request.query_params.get('number', None)
            data_quality = request.query_params.get('quality', 'compressed')

            data_getter = DataChunkGetter(data_type, data_num, data_quality,
                db_task.dimension)

            return data_getter(request, db_task.data.start_frame,
                db_task.data.stop_frame, db_task.data)

    @swagger_auto_schema(method='get', operation_summary='Method allows to download task annotations',
        manual_parameters=[
            openapi.Parameter('format', openapi.IN_QUERY,
                description="Desired output format name\nYou can get the list of supported formats at:\n/server/annotation/formats",
                type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('filename', openapi.IN_QUERY,
                description="Desired output file name",
                type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('action', in_=openapi.IN_QUERY,
                description='Used to start downloading process after annotation file had been created',
                type=openapi.TYPE_STRING, required=False, enum=['download'])
        ],
        responses={
            '202': openapi.Response(description='Dump of annotations has been started'),
            '201': openapi.Response(description='Annotations file is ready to download'),
            '200': openapi.Response(description='Download of file started'),
            '405': openapi.Response(description='Format is not available'),
        }
    )
    @swagger_auto_schema(method='put', operation_summary='Method allows to upload task annotations',
        manual_parameters=[
            openapi.Parameter('format', openapi.IN_QUERY,
                description="Input format name\nYou can get the list of supported formats at:\n/server/annotation/formats",
                type=openapi.TYPE_STRING, required=False),
        ],
        responses={
            '202': openapi.Response(description='Uploading has been started'),
            '201': openapi.Response(description='Uploading has finished'),
            '405': openapi.Response(description='Format is not available'),
        }
    )
    @swagger_auto_schema(method='patch', operation_summary='Method performs a partial update of annotations in a specific task',
        manual_parameters=[openapi.Parameter('action', in_=openapi.IN_QUERY, required=True, type=openapi.TYPE_STRING,
            enum=['create', 'update', 'delete'])])
    @swagger_auto_schema(method='delete', operation_summary='Method deletes all annotations for a specific task')
    @action(detail=True, methods=['GET', 'DELETE', 'PUT', 'PATCH'],
        serializer_class=LabeledDataSerializer)
    def annotations(self, request, pk):
        db_task = self.get_object() # force to call check_object_permissions
        if request.method == 'GET':
            format_name = request.query_params.get('format')
            if format_name:
                return _export_annotations(db_instance=db_task,
                    rq_id="/api/tasks/{}/annotations/{}".format(pk, format_name),
                    request=request,
                    action=request.query_params.get("action", "").lower(),
                    callback=dm.views.export_task_annotations,
                    format_name=format_name,
                    filename=request.query_params.get("filename", "").lower(),
                )
            else:
                data = dm.task.get_task_data(pk)
                serializer = LabeledDataSerializer(data=data)
                if serializer.is_valid(raise_exception=True):
                    return Response(serializer.data)
        elif request.method == 'PUT':
            format_name = request.query_params.get('format')
            if format_name:
                return _import_annotations(
                    request=request,
                    rq_id="{}@/api/tasks/{}/annotations/upload".format(request.user, pk),
                    rq_func=dm.task.import_task_annotations,
                    pk=pk,
                    format_name=format_name,
                )
            else:
                serializer = LabeledDataSerializer(data=request.data)
                if serializer.is_valid(raise_exception=True):
                    data = dm.task.put_task_data(pk, serializer.data)
                    return Response(data)
        elif request.method == 'DELETE':
            dm.task.delete_task_data(pk)
            return Response(status=status.HTTP_204_NO_CONTENT)
        elif request.method == 'PATCH':
            action = self.request.query_params.get("action", None)
            if action not in dm.task.PatchAction.values():
                raise serializers.ValidationError(
                    "Please specify a correct 'action' for the request")
            serializer = LabeledDataSerializer(data=request.data)
            if serializer.is_valid(raise_exception=True):
                try:
                    data = dm.task.patch_task_data(pk, serializer.data, action)
                except (AttributeError, IntegrityError) as e:
                    return Response(data=str(e), status=status.HTTP_400_BAD_REQUEST)
                return Response(data)

    @swagger_auto_schema(method='get', operation_summary='When task is being created the method returns information about a status of the creation process')
    @action(detail=True, methods=['GET'], serializer_class=RqStatusSerializer)
    def status(self, request, pk):
        self.get_object() # force to call check_object_permissions
        response = self._get_rq_response(queue="default", job_id=f"/api/tasks/{pk}")
        serializer = RqStatusSerializer(data=response)

        if serializer.is_valid(raise_exception=True):
            return Response(serializer.data)

    @staticmethod
    def _get_rq_response(queue, job_id):
        queue = django_rq.get_queue(queue)
        job = queue.fetch_job(job_id)
        response = {}
        if job is None or job.is_finished:
            response = { "state": "Finished" }
        elif job.is_queued:
            response = { "state": "Queued" }
        elif job.is_failed:
            response = { "state": "Failed", "message": job.exc_info }
        else:
            response = { "state": "Started" }
            if 'status' in job.meta:
                response['message'] = job.meta['status']
            response['progress'] = job.meta.get('task_progress', 0.)

        return response

    @staticmethod
    @swagger_auto_schema(method='get', operation_summary='Method provides a meta information about media files which are related with the task',
        responses={'200': DataMetaSerializer()})
    @action(detail=True, methods=['GET'], serializer_class=DataMetaSerializer,
        url_path='data/meta')
    def data_info(request, pk):
        db_task = models.Task.objects.prefetch_related(
            Prefetch('data', queryset=models.Data.objects.select_related('video').prefetch_related(
                Prefetch('images', queryset=models.Image.objects.prefetch_related('related_files').order_by('frame'))
            ))
        ).get(pk=pk)

        if hasattr(db_task.data, 'video'):
            media = [db_task.data.video]
        else:
            media = list(db_task.data.images.all())

        frame_meta = [{
            'width': item.width,
            'height': item.height,
            'name': item.path,
            'has_related_context': hasattr(item, 'related_files') and item.related_files.exists()
        } for item in media]

        db_data = db_task.data
        db_data.frames = frame_meta

        serializer = DataMetaSerializer(db_data)
        return Response(serializer.data)

    @swagger_auto_schema(method='get', operation_summary='Export task as a dataset in a specific format',
        manual_parameters=[
            openapi.Parameter('format', openapi.IN_QUERY,
                description="Desired output format name\nYou can get the list of supported formats at:\n/server/annotation/formats",
                type=openapi.TYPE_STRING, required=True),
            openapi.Parameter('filename', openapi.IN_QUERY,
                description="Desired output file name",
                type=openapi.TYPE_STRING, required=False),
            openapi.Parameter('action', in_=openapi.IN_QUERY,
                description='Used to start downloading process after annotation file had been created',
                type=openapi.TYPE_STRING, required=False, enum=['download'])
        ],
        responses={'202': openapi.Response(description='Exporting has been started'),
            '201': openapi.Response(description='Output file is ready for downloading'),
            '200': openapi.Response(description='Download of file started'),
            '405': openapi.Response(description='Format is not available'),
        }
    )
    @action(detail=True, methods=['GET'], serializer_class=None,
        url_path='dataset')
    def dataset_export(self, request, pk):
        db_task = self.get_object() # force to call check_object_permissions

        format_name = request.query_params.get("format", "")
        return _export_annotations(db_instance=db_task,
            rq_id="/api/tasks/{}/dataset/{}".format(pk, format_name),
            request=request,
            action=request.query_params.get("action", "").lower(),
            callback=dm.views.export_task_as_dataset,
            format_name=format_name,
            filename=request.query_params.get("filename", "").lower(),
        )

class CharInFilter(filters.BaseInFilter, filters.CharFilter):
    pass

class JobFilter(filters.FilterSet):
    assignee = filters.CharFilter(field_name="assignee__username", lookup_expr="icontains")
    stage = CharInFilter(field_name="stage", lookup_expr="in")
    state = CharInFilter(field_name="state", lookup_expr="in")

    class Meta:
        model = Job
        fields = ("assignee", )

@method_decorator(name='retrieve', decorator=swagger_auto_schema(operation_summary='Method returns details of a job'))
@method_decorator(name='update', decorator=swagger_auto_schema(operation_summary='Method updates a job by id'))
@method_decorator(name='partial_update', decorator=swagger_auto_schema(
    operation_summary='Methods does a partial update of chosen fields in a job'))
class JobViewSet(viewsets.GenericViewSet, mixins.ListModelMixin,
    mixins.RetrieveModelMixin, mixins.UpdateModelMixin):
    queryset = Job.objects.all().order_by('id')
    filterset_class = JobFilter
    iam_organization_field = 'segment__task__organization'

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == 'list':
            perm = JobPermission.create_list(self.request)
            queryset = perm.filter(queryset)

        return queryset

    def get_serializer_class(self):
        if self.request.method in SAFE_METHODS:
            return JobReadSerializer
        else:
            return JobWriteSerializer

    @swagger_auto_schema(method='get', operation_summary='Method returns annotations for a specific job')
    @swagger_auto_schema(method='put', operation_summary='Method performs an update of all annotations in a specific job')
    @swagger_auto_schema(method='patch', manual_parameters=[
        openapi.Parameter('action', in_=openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True,
            enum=['create', 'update', 'delete'])],
            operation_summary='Method performs a partial update of annotations in a specific job')
    @swagger_auto_schema(method='delete', operation_summary='Method deletes all annotations for a specific job')
    @action(detail=True, methods=['GET', 'DELETE', 'PUT', 'PATCH'],
        serializer_class=LabeledDataSerializer)
    def annotations(self, request, pk):
        self.get_object() # force to call check_object_permissions
        if request.method == 'GET':
            data = dm.task.get_job_data(pk)
            return Response(data)
        elif request.method == 'PUT':
            format_name = request.query_params.get("format", "")
            if format_name:
                return _import_annotations(
                    request=request,
                    rq_id="{}@/api/jobs/{}/annotations/upload".format(request.user, pk),
                    rq_func=dm.task.import_job_annotations,
                    pk=pk,
                    format_name=format_name
                )
            else:
                serializer = LabeledDataSerializer(data=request.data)
                if serializer.is_valid(raise_exception=True):
                    try:
                        data = dm.task.put_job_data(pk, serializer.data)
                    except (AttributeError, IntegrityError) as e:
                        return Response(data=str(e), status=status.HTTP_400_BAD_REQUEST)
                    return Response(data)
        elif request.method == 'DELETE':
            dm.task.delete_job_data(pk)
            return Response(status=status.HTTP_204_NO_CONTENT)
        elif request.method == 'PATCH':
            action = self.request.query_params.get("action", None)
            if action not in dm.task.PatchAction.values():
                raise serializers.ValidationError(
                    "Please specify a correct 'action' for the request")
            serializer = LabeledDataSerializer(data=request.data)
            if serializer.is_valid(raise_exception=True):
                try:
                    data = dm.task.patch_job_data(pk, serializer.data, action)
                except (AttributeError, IntegrityError) as e:
                    return Response(data=str(e), status=status.HTTP_400_BAD_REQUEST)
                return Response(data)

    @swagger_auto_schema(
        method='get',
        operation_summary='Method returns list of issues for the job',
        responses={'200': IssueReadSerializer(many=True)})
    @action(detail=True, methods=['GET'], serializer_class=IssueReadSerializer)
    def issues(self, request, pk):
        db_job = self.get_object()
        queryset = db_job.issues
        serializer = IssueReadSerializer(queryset,
            context={'request': request}, many=True)

        return Response(serializer.data)

    @swagger_auto_schema(method='get', operation_summary='Method returns data for a specific job',
        manual_parameters=[
            openapi.Parameter('type', in_=openapi.IN_QUERY, required=True, type=openapi.TYPE_STRING,
                enum=['chunk', 'frame', 'preview', 'context_image'],
                description="Specifies the type of the requested data"),
            openapi.Parameter('quality', in_=openapi.IN_QUERY, required=True, type=openapi.TYPE_STRING,
                enum=['compressed', 'original'],
                description="Specifies the quality level of the requested data, doesn't matter for 'preview' type"),
            openapi.Parameter('number', in_=openapi.IN_QUERY, required=True, type=openapi.TYPE_NUMBER,
                description="A unique number value identifying chunk or frame, doesn't matter for 'preview' type"),
            ]
    )
    @action(detail=True)
    def data(self, request, pk):
        db_job = self.get_object() # call check_object_permissions as well
        data_type = request.query_params.get('type', None)
        data_num = request.query_params.get('number', None)
        data_quality = request.query_params.get('quality', 'compressed')

        data_getter = DataChunkGetter(data_type, data_num, data_quality,
            db_job.segment.task.dimension)

        return data_getter(request, db_job.segment.start_frame,
            db_job.segment.stop_frame, db_job.segment.task.data)

class IssueViewSet(viewsets.ModelViewSet):
    queryset = Issue.objects.all().order_by('-id')
    http_method_names = ['get', 'post', 'patch', 'delete', 'options']
    iam_organization_field = 'job__segment__task__organization'

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == 'list':
            perm = IssuePermission.create_list(self.request)
            queryset = perm.filter(queryset)

        return queryset

    def get_serializer_class(self):
        if self.request.method in SAFE_METHODS:
            return IssueReadSerializer
        else:
            return IssueWriteSerializer

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @swagger_auto_schema(
        method='get',
        operation_summary='The action returns all comments of a specific issue',
        responses={'200': CommentReadSerializer(many=True)})
    @action(detail=True, methods=['GET'], serializer_class=CommentReadSerializer)
    def comments(self, request, pk):
        db_issue = self.get_object()
        queryset = db_issue.comments
        serializer = CommentReadSerializer(queryset,
            context={'request': request}, many=True)

        return Response(serializer.data)

class CommentViewSet(viewsets.ModelViewSet):
    queryset = Comment.objects.all().order_by('-id')
    http_method_names = ['get', 'post', 'patch', 'delete', 'options']
    iam_organization_field = 'issue__job__segment__task__organization'

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == 'list':
            perm = CommentPermission.create_list(self.request)
            queryset = perm.filter(queryset)

        return queryset

    def get_serializer_class(self):
        if self.request.method in SAFE_METHODS:
            return CommentReadSerializer
        else:
            return CommentWriteSerializer

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

class UserFilter(filters.FilterSet):
    class Meta:
        model = User
        fields = ("id", "is_active")

@method_decorator(name='list', decorator=swagger_auto_schema(
    manual_parameters=[
            openapi.Parameter('id',openapi.IN_QUERY,description="A unique number value identifying this user",type=openapi.TYPE_NUMBER),
            openapi.Parameter('is_active',openapi.IN_QUERY,description="Returns only active users",type=openapi.TYPE_BOOLEAN),
    ],
    operation_summary='Method provides a paginated list of users registered on the server'))
@method_decorator(name='retrieve', decorator=swagger_auto_schema(
    operation_summary='Method provides information of a specific user'))
@method_decorator(name='partial_update', decorator=swagger_auto_schema(
    operation_summary='Method updates chosen fields of a user'))
@method_decorator(name='destroy', decorator=swagger_auto_schema(
    operation_summary='Method deletes a specific user from the server'))
class UserViewSet(viewsets.GenericViewSet, mixins.ListModelMixin,
    mixins.RetrieveModelMixin, mixins.UpdateModelMixin, mixins.DestroyModelMixin):
    queryset = User.objects.prefetch_related('groups').all().order_by('id')
    http_method_names = ['get', 'post', 'head', 'patch', 'delete']
    search_fields = ('username', 'first_name', 'last_name')
    filterset_class = UserFilter
    iam_organization_field = 'memberships__organization'

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == 'list':
            perm = UserPermission(self.request, self)
            queryset = perm.filter(queryset)

        return queryset

    def get_serializer_class(self):
        user = self.request.user
        if user.is_staff:
            return UserSerializer
        else:
            is_self = int(self.kwargs.get("pk", 0)) == user.id or \
                self.action == "self"
            if is_self and self.request.method in SAFE_METHODS:
                return UserSerializer
            else:
                return BasicUserSerializer

    @swagger_auto_schema(method='get', operation_summary='Method returns an instance of a user who is currently authorized')
    @action(detail=False, methods=['GET'])
    def self(self, request):
        """
        Method returns an instance of a user who is currently authorized
        """
        serializer_class = self.get_serializer_class()
        serializer = serializer_class(request.user, context={ "request": request })
        return Response(serializer.data)

class RedefineDescriptionField(FieldInspector):
    # pylint: disable=no-self-use
    def process_result(self, result, method_name, obj, **kwargs):
        if isinstance(result, openapi.Schema):
            if hasattr(result, 'title') and result.title == 'Specific attributes':
                result.description = 'structure like key1=value1&key2=value2\n' \
                    'supported: range=aws_range'
        return result

class CloudStorageFilter(filters.FilterSet):
    display_name = filters.CharFilter(field_name='display_name', lookup_expr='icontains')
    provider_type = filters.CharFilter(field_name='provider_type', lookup_expr='icontains')
    resource = filters.CharFilter(field_name='resource', lookup_expr='icontains')
    credentials_type = filters.CharFilter(field_name='credentials_type', lookup_expr='icontains')
    description = filters.CharFilter(field_name='description', lookup_expr='icontains')
    owner = filters.CharFilter(field_name='owner__username', lookup_expr='icontains')

    class Meta:
        model = models.CloudStorage
        fields = ('id', 'display_name', 'provider_type', 'resource', 'credentials_type', 'description', 'owner')

@method_decorator(
    name='retrieve',
    decorator=swagger_auto_schema(
        operation_summary='Method returns details of a specific cloud storage',
        responses={
            '200': openapi.Response(description='A details of a storage'),
        },
        tags=['cloud storages']
    )
)
@method_decorator(name='list', decorator=swagger_auto_schema(
        operation_summary='Returns a paginated list of storages according to query parameters',
        manual_parameters=[
                openapi.Parameter('provider_type', openapi.IN_QUERY, description="A supported provider of cloud storages",
                                type=openapi.TYPE_STRING, enum=CloudProviderChoice.list()),
                openapi.Parameter('display_name', openapi.IN_QUERY, description="A display name of storage", type=openapi.TYPE_STRING),
                openapi.Parameter('resource', openapi.IN_QUERY, description="A name of bucket or container", type=openapi.TYPE_STRING),
                openapi.Parameter('owner', openapi.IN_QUERY, description="A resource owner", type=openapi.TYPE_STRING),
                openapi.Parameter('credentials_type', openapi.IN_QUERY, description="A type of a granting access", type=openapi.TYPE_STRING, enum=CredentialsTypeChoice.list()),
            ],
        responses={'200': BaseCloudStorageSerializer(many=True)},
        tags=['cloud storages'],
        field_inspectors=[RedefineDescriptionField]
    )
)
@method_decorator(name='destroy', decorator=swagger_auto_schema(
        operation_summary='Method deletes a specific cloud storage',
        tags=['cloud storages']
    )
)
@method_decorator(name='partial_update', decorator=swagger_auto_schema(
        operation_summary='Methods does a partial update of chosen fields in a cloud storage instance',
        tags=['cloud storages'],
        field_inspectors=[RedefineDescriptionField]
    )
)
class CloudStorageViewSet(viewsets.ModelViewSet):
    http_method_names = ['get', 'post', 'patch', 'delete']
    queryset = CloudStorageModel.objects.all().prefetch_related('data').order_by('-id')
    search_fields = ('provider_type', 'display_name', 'resource', 'credentials_type', 'owner__username', 'description')
    filterset_class = CloudStorageFilter
    iam_organization_field = 'organization'

    def get_serializer_class(self):
        if self.request.method in ("POST", "PATCH"):
            return CloudStorageSerializer
        else:
            return BaseCloudStorageSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == 'list':
            perm = CloudStoragePermission(self.request, self)
            queryset = perm.filter(queryset)

        provider_type = self.request.query_params.get('provider_type', None)
        if provider_type:
            if provider_type in CloudProviderChoice.list():
                return queryset.filter(provider_type=provider_type)
            raise ValidationError('Unsupported type of cloud provider')
        return queryset

    def perform_create(self, serializer):
        serializer.save(
            owner=self.request.user,
            organization=self.request.iam_context['organization'])

    def perform_destroy(self, instance):
        cloud_storage_dirname = instance.get_storage_dirname()
        super().perform_destroy(instance)
        shutil.rmtree(cloud_storage_dirname, ignore_errors=True)

    @method_decorator(name='create', decorator=swagger_auto_schema(
            operation_summary='Method creates a cloud storage with a specified characteristics',
            responses={
                '201': openapi.Response(description='A storage has beed created')
            },
            tags=['cloud storages'],
            field_inspectors=[RedefineDescriptionField],
        )
    )
    def create(self, request, *args, **kwargs):
        try:
            response = super().create(request, *args, **kwargs)
        except IntegrityError:
            response = HttpResponseBadRequest('Same storage already exists')
        except ValidationError as exceptions:
                msg_body = ""
                for ex in exceptions.args:
                    for field, ex_msg in ex.items():
                        msg_body += ': '.join([field, ex_msg if isinstance(ex_msg, str) else str(ex_msg[0])])
                        msg_body += '\n'
                return HttpResponseBadRequest(msg_body)
        except APIException as ex:
            return Response(data=ex.get_full_details(), status=ex.status_code)
        except Exception as ex:
            response = HttpResponseBadRequest(str(ex))
        return response

    @swagger_auto_schema(
        method='get',
        operation_summary='Method returns a manifest content',
        manual_parameters=[
            openapi.Parameter('manifest_path', openapi.IN_QUERY,
                description="Path to the manifest file in a cloud storage",
                type=openapi.TYPE_STRING)
        ],
        responses={
            '200': openapi.Response(description='A manifest content'),
        },
        tags=['cloud storages']
    )
    @action(detail=True, methods=['GET'], url_path='content')
    def content(self, request, pk):
        storage = None
        try:
            db_storage = self.get_object()
            credentials = Credentials()
            credentials.convert_from_db({
                'type': db_storage.credentials_type,
                'value': db_storage.credentials,
            })
            details = {
                'resource': db_storage.resource,
                'credentials': credentials,
                'specific_attributes': db_storage.get_specific_attributes()
            }
            storage = get_cloud_storage_instance(cloud_provider=db_storage.provider_type, **details)
            if not db_storage.manifests.count():
                raise Exception('There is no manifest file')
            manifest_path = request.query_params.get('manifest_path', db_storage.manifests.first().filename)
            file_status = storage.get_file_status(manifest_path)
            if file_status == Status.NOT_FOUND:
                raise FileNotFoundError(errno.ENOENT,
                    "Not found on the cloud storage {}".format(db_storage.display_name), manifest_path)
            elif file_status == Status.FORBIDDEN:
                raise PermissionError(errno.EACCES,
                    "Access to the file on the '{}' cloud storage is denied".format(db_storage.display_name), manifest_path)

            full_manifest_path = os.path.join(db_storage.get_storage_dirname(), manifest_path)
            if not os.path.exists(full_manifest_path) or \
                    datetime.utcfromtimestamp(os.path.getmtime(full_manifest_path)).replace(tzinfo=pytz.UTC) < storage.get_file_last_modified(manifest_path):
                storage.download_file(manifest_path, full_manifest_path)
            manifest = ImageManifestManager(full_manifest_path, db_storage.get_storage_dirname())
            # need to update index
            manifest.set_index()
            manifest_files = manifest.data
            return Response(data=manifest_files, content_type="text/plain")

        except CloudStorageModel.DoesNotExist:
            message = f"Storage {pk} does not exist"
            slogger.glob.error(message)
            return HttpResponseNotFound(message)
        except FileNotFoundError as ex:
            msg = f"{ex.strerror} {ex.filename}"
            slogger.cloud_storage[pk].info(msg)
            return Response(data=msg, status=status.HTTP_404_NOT_FOUND)
        except Exception as ex:
            # check that cloud storage was not deleted
            storage_status = storage.get_status() if storage else None
            if storage_status == Status.FORBIDDEN:
                msg = 'The resource {} is no longer available. Access forbidden.'.format(storage.name)
            elif storage_status == Status.NOT_FOUND:
                msg = 'The resource {} not found. It may have been deleted.'.format(storage.name)
            else:
                msg = str(ex)
            return HttpResponseBadRequest(msg)

    @swagger_auto_schema(
        method='get',
        operation_summary='Method returns a preview image from a cloud storage',
        responses={
            '200': openapi.Response(description='Preview'),
        },
        tags=['cloud storages']
    )
    @action(detail=True, methods=['GET'], url_path='preview')
    def preview(self, request, pk):
        storage = None
        try:
            db_storage = self.get_object()
            if not os.path.exists(db_storage.get_preview_path()):
                credentials = Credentials()
                credentials.convert_from_db({
                    'type': db_storage.credentials_type,
                    'value': db_storage.credentials,
                })
                details = {
                    'resource': db_storage.resource,
                    'credentials': credentials,
                    'specific_attributes': db_storage.get_specific_attributes()
                }
                storage = get_cloud_storage_instance(cloud_provider=db_storage.provider_type, **details)
                if not db_storage.manifests.count():
                    raise Exception('Cannot get the cloud storage preview. There is no manifest file')
                preview_path = None
                for manifest_model in db_storage.manifests.all():
                    full_manifest_path = os.path.join(db_storage.get_storage_dirname(), manifest_model.filename)
                    if not os.path.exists(full_manifest_path) or \
                            datetime.utcfromtimestamp(os.path.getmtime(full_manifest_path)).replace(tzinfo=pytz.UTC) < storage.get_file_last_modified(manifest_model.filename):
                        storage.download_file(manifest_model.filename, full_manifest_path)
                    manifest = ImageManifestManager(
                        os.path.join(db_storage.get_storage_dirname(), manifest_model.filename),
                        db_storage.get_storage_dirname()
                    )
                    # need to update index
                    manifest.set_index()
                    if not len(manifest):
                        continue
                    preview_info = manifest[0]
                    preview_path = ''.join([preview_info['name'], preview_info['extension']])
                    break
                if not preview_path:
                    msg = 'Cloud storage {} does not contain any images'.format(pk)
                    slogger.cloud_storage[pk].info(msg)
                    return HttpResponseBadRequest(msg)

                file_status = storage.get_file_status(preview_path)
                if file_status == Status.NOT_FOUND:
                    raise FileNotFoundError(errno.ENOENT,
                        "Not found on the cloud storage {}".format(db_storage.display_name), preview_path)
                elif file_status == Status.FORBIDDEN:
                    raise PermissionError(errno.EACCES,
                        "Access to the file on the '{}' cloud storage is denied".format(db_storage.display_name), preview_path)
                with NamedTemporaryFile() as temp_image:
                    storage.download_file(preview_path, temp_image.name)
                    reader = ImageListReader([temp_image.name])
                    preview = reader.get_preview()
                    preview.save(db_storage.get_preview_path())
            content_type = mimetypes.guess_type(db_storage.get_preview_path())[0]
            return HttpResponse(open(db_storage.get_preview_path(), 'rb').read(), content_type)
        except CloudStorageModel.DoesNotExist:
            message = f"Storage {pk} does not exist"
            slogger.glob.error(message)
            return HttpResponseNotFound(message)
        except PermissionDenied:
            raise
        except Exception as ex:
            # check that cloud storage was not deleted
            storage_status = storage.get_status() if storage else None
            if storage_status == Status.FORBIDDEN:
                msg = 'The resource {} is no longer available. Access forbidden.'.format(storage.name)
            elif storage_status == Status.NOT_FOUND:
                msg = 'The resource {} not found. It may have been deleted.'.format(storage.name)
            else:
                msg = str(ex)
            return HttpResponseBadRequest(msg)

    @swagger_auto_schema(
        method='get',
        operation_summary='Method returns a cloud storage status',
        responses={
            '200': openapi.Response(description='Status'),
        },
        tags=['cloud storages']
    )
    @action(detail=True, methods=['GET'], url_path='status')
    def status(self, request, pk):
        try:
            db_storage = self.get_object()
            credentials = Credentials()
            credentials.convert_from_db({
                'type': db_storage.credentials_type,
                'value': db_storage.credentials,
            })
            details = {
                'resource': db_storage.resource,
                'credentials': credentials,
                'specific_attributes': db_storage.get_specific_attributes()
            }
            storage = get_cloud_storage_instance(cloud_provider=db_storage.provider_type, **details)
            storage_status = storage.get_status()
            return HttpResponse(storage_status)
        except CloudStorageModel.DoesNotExist:
            message = f"Storage {pk} does not exist"
            slogger.glob.error(message)
            return HttpResponseNotFound(message)
        except Exception as ex:
            msg = str(ex)
            return HttpResponseBadRequest(msg)

def rq_handler(job, exc_type, exc_value, tb):
    job.exc_info = "".join(
        traceback.format_exception_only(exc_type, exc_value))
    job.save()
    if "tasks" in job.id.split("/"):
        return task.rq_handler(job, exc_type, exc_value, tb)

    return True

# TODO: Method should be reimplemented as a separated view
# @swagger_auto_schema(method='put', manual_parameters=[openapi.Parameter('format', in_=openapi.IN_QUERY,
#         description='A name of a loader\nYou can get annotation loaders from this API:\n/server/annotation/formats',
#         required=True, type=openapi.TYPE_STRING)],
#     operation_summary='Method allows to upload annotations',
#     responses={'202': openapi.Response(description='Load of annotations has been started'),
#         '201': openapi.Response(description='Annotations have been uploaded')},
#     tags=['tasks'])
# @api_view(['PUT'])
def _import_annotations(request, rq_id, rq_func, pk, format_name):
    format_desc = {f.DISPLAY_NAME: f
        for f in dm.views.get_import_formats()}.get(format_name)
    if format_desc is None:
        raise serializers.ValidationError(
            "Unknown input format '{}'".format(format_name))
    elif not format_desc.ENABLED:
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    queue = django_rq.get_queue("default")
    rq_job = queue.fetch_job(rq_id)

    if not rq_job:
        serializer = AnnotationFileSerializer(data=request.data)
        if serializer.is_valid(raise_exception=True):
            anno_file = serializer.validated_data['annotation_file']
            fd, filename = mkstemp(prefix='cvat_{}'.format(pk))
            with open(filename, 'wb+') as f:
                for chunk in anno_file.chunks():
                    f.write(chunk)

            av_scan_paths(filename)
            rq_job = queue.enqueue_call(
                func=rq_func,
                args=(pk, filename, format_name),
                job_id=rq_id
            )
            rq_job.meta['tmp_file'] = filename
            rq_job.meta['tmp_file_descriptor'] = fd
            rq_job.save_meta()
    else:
        if rq_job.is_finished:
            os.close(rq_job.meta['tmp_file_descriptor'])
            os.remove(rq_job.meta['tmp_file'])
            rq_job.delete()
            return Response(status=status.HTTP_201_CREATED)
        elif rq_job.is_failed:
            os.close(rq_job.meta['tmp_file_descriptor'])
            os.remove(rq_job.meta['tmp_file'])
            exc_info = str(rq_job.exc_info)
            rq_job.delete()

            # RQ adds a prefix with exception class name
            import_error_prefix = '{}.{}'.format(
                CvatImportError.__module__, CvatImportError.__name__)
            if exc_info.startswith(import_error_prefix):
                exc_info = exc_info.replace(import_error_prefix + ': ', '')
                return Response(data=exc_info,
                    status=status.HTTP_400_BAD_REQUEST)
            else:
                return Response(data=exc_info,
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return Response(status=status.HTTP_202_ACCEPTED)

def _export_annotations(db_instance, rq_id, request, format_name, action, callback, filename):
    if action not in {"", "download"}:
        raise serializers.ValidationError(
            "Unexpected action specified for the request")

    format_desc = {f.DISPLAY_NAME: f
        for f in dm.views.get_export_formats()}.get(format_name)
    if format_desc is None:
        raise serializers.ValidationError(
            "Unknown format specified for the request")
    elif not format_desc.ENABLED:
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    queue = django_rq.get_queue("default")
    rq_job = queue.fetch_job(rq_id)

    if rq_job:
        last_instance_update_time = timezone.localtime(db_instance.updated_date)
        if isinstance(db_instance, Project):
            tasks_update = list(map(lambda db_task: timezone.localtime(db_task.updated_date), db_instance.tasks.all()))
            last_instance_update_time = max(tasks_update + [last_instance_update_time])
        request_time = rq_job.meta.get('request_time', None)
        if request_time is None or request_time < last_instance_update_time:
            rq_job.cancel()
            rq_job.delete()
        else:
            if rq_job.is_finished:
                file_path = rq_job.return_value
                if action == "download" and osp.exists(file_path):
                    rq_job.delete()

                    timestamp = datetime.strftime(last_instance_update_time,
                        "%Y_%m_%d_%H_%M_%S")
                    filename = filename or \
                        "{}_{}-{}-{}{}".format(
                            "project" if isinstance(db_instance, models.Project) else "task",
                            db_instance.name, timestamp,
                            format_name, osp.splitext(file_path)[1]
                        )
                    return sendfile(request, file_path, attachment=True,
                        attachment_filename=filename.lower())
                else:
                    if osp.exists(file_path):
                        return Response(status=status.HTTP_201_CREATED)
            elif rq_job.is_failed:
                exc_info = str(rq_job.exc_info)
                rq_job.delete()
                return Response(exc_info,
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            else:
                return Response(status=status.HTTP_202_ACCEPTED)

    try:
        if request.scheme:
            server_address = request.scheme + '://'
        server_address += request.get_host()
    except Exception:
        server_address = None

    ttl = (dm.views.PROJECT_CACHE_TTL if isinstance(db_instance, Project) else dm.views.TASK_CACHE_TTL).total_seconds()
    queue.enqueue_call(func=callback,
        args=(db_instance.id, format_name, server_address), job_id=rq_id,
        meta={ 'request_time': timezone.localtime() },
        result_ttl=ttl, failure_ttl=ttl)
    return Response(status=status.HTTP_202_ACCEPTED)

def _import_project_dataset(request, rq_id, rq_func, pk, format_name):
    format_desc = {f.DISPLAY_NAME: f
        for f in dm.views.get_import_formats()}.get(format_name)
    if format_desc is None:
        raise serializers.ValidationError(
            "Unknown input format '{}'".format(format_name))
    elif not format_desc.ENABLED:
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    queue = django_rq.get_queue("default")
    rq_job = queue.fetch_job(rq_id)

    if not rq_job:
        serializer = DatasetFileSerializer(data=request.data)
        if serializer.is_valid(raise_exception=True):
            dataset_file = serializer.validated_data['dataset_file']
            fd, filename = mkstemp(prefix='cvat_{}'.format(pk))
            with open(filename, 'wb+') as f:
                for chunk in dataset_file.chunks():
                    f.write(chunk)

            rq_job = queue.enqueue_call(
                func=rq_func,
                args=(pk, filename, format_name),
                job_id=rq_id,
                meta={
                    'tmp_file': filename,
                    'tmp_file_descriptor': fd,
                },
            )
    else:
        return Response(status=status.HTTP_409_CONFLICT, data='Import job already exists')

    return Response(status=status.HTTP_202_ACCEPTED)
