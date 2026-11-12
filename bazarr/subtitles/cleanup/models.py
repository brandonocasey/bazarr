from dataclasses import asdict, dataclass, field
from typing import Literal, TypeAlias

JSONValue: TypeAlias = str | int | float | bool | None | list['JSONValue'] | dict[str, 'JSONValue']


@dataclass(frozen=True)
class ExternalSubtitle:
    path: str
    language: str
    forced: bool = False
    hearing_impaired: bool = False


@dataclass(frozen=True)
class AudioStream:
    index: int
    language: str
    title: str
    default: bool


@dataclass(frozen=True)
class SubtitleTrack:
    key: str
    source: Literal['embedded', 'external']
    path: str
    stream_index: int
    language: str
    forced: bool
    hearing_impaired: bool
    codec: str
    score: float | None
    error: str | None
    companion_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MediaAnalysis:
    video_path: str
    duration: float
    audio_stream_index: int | None
    audio_streams: list[AudioStream]
    tracks: list[SubtitleTrack]
    algorithm: dict[str, JSONValue]

    def to_dict(self) -> dict[str, JSONValue]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, JSONValue]) -> 'MediaAnalysis':
        audio = value['audio_streams']
        tracks = value['tracks']
        algorithm = value['algorithm']
        if not isinstance(audio, list) or not isinstance(tracks, list) or not isinstance(algorithm, dict):
            raise ValueError('Invalid persisted cleanup analysis')
        return cls(
            video_path=_string(value, 'video_path'),
            duration=_number(value, 'duration'),
            audio_stream_index=_optional_int(value, 'audio_stream_index'),
            audio_streams=[_audio(item) for item in audio],
            tracks=[_track(item) for item in tracks],
            algorithm=algorithm,
        )


@dataclass(frozen=True)
class PreparedFile:
    original_path: str
    staged_path: str | None


def _mapping(value: JSONValue) -> dict[str, JSONValue]:
    if not isinstance(value, dict):
        raise ValueError('Expected persisted object')
    return value


def _string(value: dict[str, JSONValue], key: str) -> str:
    item = value[key]
    if not isinstance(item, str):
        raise ValueError(f'Expected string: {key}')
    return item


def _number(value: dict[str, JSONValue], key: str) -> float:
    item = value[key]
    if isinstance(item, bool) or not isinstance(item, (float, int)):
        raise ValueError(f'Expected number: {key}')
    return float(item)


def _optional_int(value: dict[str, JSONValue], key: str) -> int | None:
    item = value[key]
    if item is not None and (isinstance(item, bool) or not isinstance(item, int)):
        raise ValueError(f'Expected integer or null: {key}')
    return item


def _boolean(value: dict[str, JSONValue], key: str) -> bool:
    item = value[key]
    if not isinstance(item, bool):
        raise ValueError(f'Expected boolean: {key}')
    return item


def _audio(item: JSONValue) -> AudioStream:
    value = _mapping(item)
    index = _optional_int(value, 'index')
    if index is None:
        raise ValueError('Audio index is required')
    return AudioStream(index, _string(value, 'language'), _string(value, 'title'), _boolean(value, 'default'))


def _track(item: JSONValue) -> SubtitleTrack:
    value = _mapping(item)
    source = _string(value, 'source')
    if source not in ('embedded', 'external'):
        raise ValueError('Invalid subtitle source')
    index = _optional_int(value, 'stream_index')
    if index is None:
        raise ValueError('Subtitle index is required')
    error = value['error']
    if error is not None and not isinstance(error, str):
        raise ValueError('Invalid subtitle error')
    return SubtitleTrack(_string(value, 'key'), source, _string(value, 'path'), index,
                         _string(value, 'language'), _boolean(value, 'forced'),
                         _boolean(value, 'hearing_impaired'), _string(value, 'codec'),
                         None if value['score'] is None else _number(value, 'score'), error, _companions(value))


def _companions(value: dict[str, JSONValue]) -> list[str]:
    paths = value.get('companion_paths', [])
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        raise ValueError('Invalid subtitle companion paths')
    return [str(path) for path in paths]
