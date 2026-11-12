# coding=utf-8

from collections.abc import Callable, Mapping

from flask import request
from flask_restx import Namespace, Resource

from subtitles.cleanup import service, store
from ..utils import authenticate

api_ns_subtitle_cleanup = Namespace('Subtitle cleanup', description='Preview, confirm, and restore redundant subtitles')


def _body() -> dict[str, object]:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise store.CleanupError('A JSON object is required')
    return value


def _media_fields(value: Mapping[str, object]) -> tuple[str, int]:
    media_type = value.get('media_type')
    media_id = value.get('media_id')
    if not isinstance(media_type, str) or media_type not in ('movie', 'series'):
        raise store.CleanupError('Media type must be movie or series')
    if isinstance(media_id, str) and media_id.isdecimal():
        media_id = int(media_id)
    if type(media_id) is not int or media_id < 1:
        raise store.CleanupError('A positive indexed media ID is required')
    return media_type, media_id


def _response(function: Callable[..., object], *arguments: object):
    try:
        return function(*arguments)
    except store.CleanupError as error:
        return {'message': str(error)}, error.status
    except (OSError, ValueError) as error:
        return {'message': str(error)}, 409


@api_ns_subtitle_cleanup.route('subtitles/cleanup')
class CleanupOperations(Resource):
    @authenticate
    def get(self):
        def read():
            media_type, media_id = _media_fields(request.args.to_dict())
            store.media_metadata(media_type, media_id)
            return {'data': store.list_operations(media_type, media_id)}
        return _response(read)


@api_ns_subtitle_cleanup.route('subtitles/cleanup/preview')
class CleanupPreview(Resource):
    @authenticate
    def post(self):
        def create():
            body = _body()
            media_type, media_id = _media_fields(body)
            audio = body.get('audio_stream_index')
            if audio is not None and (type(audio) is not int or audio < 0):
                raise store.CleanupError('Audio stream index must be a non-negative integer')
            return service.preview(media_type, media_id, audio), 202
        return _response(create)


@api_ns_subtitle_cleanup.route('subtitles/cleanup/<string:operation_id>')
class CleanupOperation(Resource):
    @authenticate
    def get(self, operation_id):
        return _response(service.get_operation, operation_id)


@api_ns_subtitle_cleanup.route('subtitles/cleanup/<string:operation_id>/apply')
class CleanupApply(Resource):
    @authenticate
    def post(self, operation_id):
        def confirm():
            body = _body()
            keepers = body.get('keepers')
            if not isinstance(keepers, dict) or not all(isinstance(key, str) and isinstance(value, str)
                                                       for key, value in keepers.items()):
                raise store.CleanupError('Keepers must map group keys to track keys')
            return service.apply(operation_id, body.get('preview_token'), keepers), 202
        return _response(confirm)


@api_ns_subtitle_cleanup.route('subtitles/cleanup/<string:operation_id>/restore')
class CleanupRestore(Resource):
    @authenticate
    def post(self, operation_id):
        def confirm():
            return service.restore(operation_id, _body().get('preview_token')), 202
        return _response(confirm)
