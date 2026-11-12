import errno
import hashlib
import importlib.metadata
import json
import math
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from concurrent.futures import CancelledError
from pathlib import Path
from typing import BinaryIO, Literal

from .models import AnalysisOptions, AudioStream, ExternalSubtitle, JSONValue, MediaAnalysis, PreparedFile, SubtitleTrack
from .scoring import FRAME_SECONDS, SAMPLE_RATE, VAD_MODE, display_signal, pearson_score

CancelCheck = Callable[[], bool] | None


@dataclass(frozen=True)
class _SignalOptions:
    duration: float
    cancelled: CancelCheck = None
    bitmap: bool = False


@dataclass(frozen=True)
class _VobSubOptions:
    original: Path
    retained_indices: list[int]
    track_ids: list[int]
    cancelled: CancelCheck = None


def _check(cancelled: CancelCheck) -> None:
    if cancelled is not None and cancelled():
        raise CancelledError('Subtitle cleanup was cancelled')


def _tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f'Install {name} before using subtitle cleanup; automatic downloads are disabled')
    return executable


def _require_space(workspace: Path, required_bytes: int) -> None:
    available = shutil.disk_usage(workspace).free
    if available < required_bytes:
        raise OSError(errno.ENOSPC, f'Subtitle cleanup staging requires {required_bytes} bytes; '
                      f'only {available} bytes are available in {workspace}')


def _run(arguments: list[str], cancelled: CancelCheck = None, output: BinaryIO | None = None) -> bytes:
    _check(cancelled)
    with tempfile.TemporaryFile() as errors, tempfile.TemporaryFile() as captured:
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
                                   stdout=output if output is not None else captured, stderr=errors)
        try:
            while True:
                try:
                    code = process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    _check(cancelled)
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        errors.seek(0)
        diagnostics = errors.read(16384)
        if code != 0 or (Path(arguments[0]).name in ('ffmpeg', 'ffprobe') and diagnostics):
            captured.seek(0)
            details = diagnostics or captured.read(16384)
            raise RuntimeError(f'{Path(arguments[0]).name} failed ({code}): '
                               + details.decode('utf-8', errors='replace'))
        captured.seek(0)
        return captured.read()


def _object(value: JSONValue) -> dict[str, JSONValue]:
    if not isinstance(value, dict):
        raise ValueError('Expected media metadata object')
    return value


def _objects(value: JSONValue) -> list[dict[str, JSONValue]]:
    if not isinstance(value, list):
        raise ValueError('Expected media metadata array')
    return [_object(item) for item in value]


def _subtitle_input_format(path: str) -> list[str]:
    if Path(path).suffix.lower() == '.sub':
        with open(path, 'rb') as subtitle:
            prefix = subtitle.read(256)
        if re.match(rb'\{\d+\}\{\d+\}', prefix):
            return ['-f', 'microdvd']
    return []


def _probe(path: str, cancelled: CancelCheck = None) -> dict[str, JSONValue]:
    return _object(json.loads(_run([_tool('ffprobe'), '-v', 'error', '-show_streams', '-show_format',
                                   '-show_chapters', '-show_programs', '-show_data_hash', 'sha256', '-of', 'json',
                                   *_subtitle_input_format(path), path], cancelled)))


def _streams(probe: dict[str, JSONValue]) -> list[dict[str, JSONValue]]:
    return _objects(probe.get('streams', []))


def _tags(stream: dict[str, JSONValue]) -> dict[str, str]:
    return {key.lower(): str(value) for key, value in _object(stream.get('tags', {})).items()}


def _flags(stream: dict[str, JSONValue]) -> dict[str, JSONValue]:
    return _object(stream.get('disposition', {}))


def _digest(path: str, cancelled: CancelCheck = None) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        while block := source.read(1024 * 1024):
            _check(cancelled)
            digest.update(block)
    return digest.hexdigest()


def _language(value: str) -> str:
    from babelfish import Language
    from babelfish.exceptions import LanguageError

    try:
        language = Language.fromietf(value)
    except (ValueError, LanguageError):
        try:
            language = Language.fromalpha3b(value)
        except (ValueError, LanguageError):
            return value.lower()
    suffix = value.partition('-')[2]
    return language.alpha3 + ('-' + suffix.lower() if suffix else '')


def _paired_path(path: Path, extension: str) -> Path:
    return path.with_suffix(extension.upper() if path.suffix.isupper() else extension)


def _events(path: str, stream_index: int, options: _SignalOptions) -> list[tuple[float, float]]:
    duration, cancelled, bitmap = options.duration, options.cancelled, options.bitmap
    data = _object(json.loads(_run([_tool('ffprobe'), '-v', 'error', '-select_streams', str(stream_index),
                                   '-show_frames', '-of', 'json', *_subtitle_input_format(path), path], cancelled)))
    intervals: list[tuple[float, float]] = []
    active: tuple[float, float] | None = None
    for frame in _objects(data.get('frames', [])):
        if frame.get('media_type') != 'subtitle':
            continue
        if 'pts_time' not in frame:
            raise ValueError('Decoded subtitle has no presentation timestamp')
        pts = float(str(frame['pts_time']))
        start = pts + float(str(frame.get('start_display_time', 0))) / 1000
        if active is not None:
            intervals.append((active[0], min(active[1], start)))
            active = None
        if int(str(frame.get('num_rects', 0))) == 0:
            continue
        end_ms = int(str(frame.get('end_display_time', 4294967295)))
        end = duration if end_ms == 4294967295 else pts + end_ms / 1000
        if bitmap:
            active = (start, end)
        else:
            intervals.append((start, end))
    if active is not None:
        intervals.append(active)
    return intervals


def _audio_signal(path: str, index: int, workspace: Path, options: _SignalOptions) -> list[float]:
    import webrtcvad

    duration, cancelled = options.duration, options.cancelled
    _require_space(workspace, math.ceil(duration * SAMPLE_RATE * 2))
    with tempfile.TemporaryDirectory(prefix='analysis-', dir=workspace) as directory:
        pcm = Path(directory) / 'reference.pcm'
        _run([_tool('ffmpeg'), '-v', 'error', '-nostdin', '-copyts', '-i', path, '-map', f'0:{index}',
              '-vn', '-sn', '-af', f'aresample={SAMPLE_RATE}:async=1:first_pts=0', '-ac', '1',
              '-ar', str(SAMPLE_RATE), '-t', str(duration), '-f', 's16le', '-n', str(pcm)], cancelled)
        vad = webrtcvad.Vad(VAD_MODE)
        frame_bytes = SAMPLE_RATE // 100 * 2
        count = math.ceil(duration / FRAME_SECONDS)
        signal: list[float] = []
        with pcm.open('rb') as audio:
            for _ in range(count):
                _check(cancelled)
                frame = audio.read(frame_bytes)
                signal.append(float(vad.is_speech(frame.ljust(frame_bytes, b'\0'), SAMPLE_RATE)) if frame else 0.0)
        return signal


def _select_audio_stream(probe: dict[str, JSONValue], audio_stream_index: int | None) -> tuple[list[AudioStream], int | None]:
    audio_streams = [AudioStream(int(str(stream['index'])), _tags(stream).get('language', 'und'),
                                _tags(stream).get('title', ''), bool(_flags(stream).get('default', 0)))
                     for stream in _streams(probe) if stream.get('codec_type') == 'audio']
    if audio_stream_index is None and audio_streams:
        audio_stream_index = next((stream.index for stream in audio_streams if stream.default), audio_streams[0].index)
    if audio_stream_index is not None and audio_stream_index not in {stream.index for stream in audio_streams}:
        raise ValueError('Selected audio stream does not exist')
    return audio_streams, audio_stream_index


def analyze_media(video_path: str, external_subtitles: Iterable[ExternalSubtitle], workspace: str,
                  options: AnalysisOptions | None = None) -> MediaAnalysis:
    options = options or AnalysisOptions()
    audio_stream_index, cancelled = options.audio_stream_index, options.cancelled
    video_path = str(Path(video_path).resolve(strict=True))
    initial_inputs = {video_path: _digest(video_path, cancelled)}
    workspace_path = Path(workspace)
    workspace_path.mkdir(parents=True, exist_ok=True)
    probe = _probe(video_path, cancelled)
    duration = float(str(_object(probe.get('format', {})).get('duration', 0)))
    if duration <= 0 or not math.isfinite(duration):
        raise ValueError('Media has no finite positive presentation duration')
    audio_streams, audio_stream_index = _select_audio_stream(probe, audio_stream_index)
    reference: list[float] | None = None
    audio_error = 'Media has no audio stream'
    if audio_stream_index is not None:
        try:
            reference = _audio_signal(video_path, audio_stream_index, workspace_path, _SignalOptions(duration, cancelled))
        except (RuntimeError, ValueError) as error:
            audio_error = str(error)
    tracks: list[SubtitleTrack] = []
    sources: list[tuple[str, Literal['embedded', 'external'], ExternalSubtitle | None, dict[str, JSONValue]]] = [
        (video_path, 'embedded', None, stream) for stream in _streams(probe) if stream.get('codec_type') == 'subtitle']
    companions: set[str] = set()
    seen = {video_path}
    for external in external_subtitles:
        path = Path(external.path).resolve(strict=True)
        if path.suffix.lower() == '.sub' and _paired_path(path, '.idx').is_file():
            path = _paired_path(path, '.idx')
        path_string = str(path)
        if path_string in seen:
            continue
        seen.add(path_string)
        initial_inputs[path_string] = _digest(path_string, cancelled)
        if path.suffix.lower() == '.idx':
            companion = str(_paired_path(path, '.sub').resolve(strict=True))
            companions.add(companion)
            initial_inputs[companion] = _digest(companion, cancelled)
        try:
            external_streams = [stream for stream in _streams(_probe(path_string, cancelled))
                                if stream.get('codec_type') == 'subtitle']
            if not external_streams:
                raise ValueError('External subtitle contains no subtitle streams')
        except (RuntimeError, ValueError) as error:
            key = hashlib.sha256(f'external\0{path_string}\0{0}'.encode()).hexdigest()
            tracks.append(SubtitleTrack(key, 'external', path_string, 0, _language(external.language), external.forced,
                                        external.hearing_impaired, path.suffix.lstrip('.'), None, str(error), [str(_paired_path(path, '.sub'))] if path.suffix.lower() == '.idx' else []))
            continue
        sources.extend((path_string, 'external', external, stream) for stream in external_streams)
    for path, source, external, stream in sources:
        _check(cancelled)
        index = int(str(stream['index']))
        codec = str(stream.get('codec_name', 'unknown'))
        tags, flags = _tags(stream), _flags(stream)
        language = tags.get('language', external.language if external else 'und')
        # Multilingual VobSub stream languages take precedence over the sidecar label.
        if external and Path(path).suffix.lower() != '.idx':
            language = external.language
        language = _language(language)
        key = hashlib.sha256(f'{source}\0{path}\0{index}'.encode()).hexdigest()
        score, error_message = None, audio_error if reference is None else None
        try:
            if codec == 'microdvd':
                with open(path, 'rb') as subtitle:
                    header = subtitle.read(256).decode('ascii', errors='replace')
                if re.match(r'(?:\{1\}\{1\}|\{0\}\{0\})\d+(?:\.\d+)?(?:\r?\n|$)', header) is None:
                    raise ValueError('MicroDVD timing has no explicit frame rate; an assumed frame rate cannot be scored')
            intervals = _events(path, index, _SignalOptions(duration, cancelled, codec in ('hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle')))
            if reference is not None:
                score, error_message = pearson_score(reference, display_signal(intervals, duration))
        except (RuntimeError, ValueError) as error:
            error_message = str(error)
        tracks.append(SubtitleTrack(key, source, path, index, language,
                                    bool(flags.get('forced', 0)) or bool(external and external.forced),
                                    bool(flags.get('hearing_impaired', 0)) or bool(external and external.hearing_impaired),
                                    codec, score, error_message, [str(_paired_path(Path(path), '.sub'))] if source == 'external' and Path(path).suffix.lower() == '.idx' else []))
    paths = sorted({video_path, *(track.path for track in tracks), *companions})
    final_inputs = {path: _digest(path, cancelled) for path in paths}
    if initial_inputs != final_inputs:
        raise ValueError('Input changed during subtitle analysis')
    algorithm: dict[str, JSONValue] = {
        'name': 'speech-display-pearson-v1', 'vad': 'webrtcvad', 'vad_mode': VAD_MODE,
        'sample_rate': SAMPLE_RATE, 'frame_seconds': FRAME_SECONDS, 'subtitle_signal': 'display occupancy fraction',
        'timeline': 'original presentation timestamps; audio resampled with first_pts=0 and gap filling',
        'decoder': 'ffprobe decoded AVSubtitle display events; bitmap clears end previous display',
        'ffmpeg': _run([_tool('ffmpeg'), '-version'], cancelled).decode().splitlines()[0],
        'ffprobe': _run([_tool('ffprobe'), '-version'], cancelled).decode().splitlines()[0],
        'webrtcvad': importlib.metadata.version('webrtcvad-wheels'),
        'companion_paths': [path for path in sorted(companions)],
        'input_sha256': {path: value for path, value in final_inputs.items()},
    }
    return MediaAnalysis(video_path, duration, audio_stream_index, audio_streams, tracks, algorithm)


def source_paths(analysis: MediaAnalysis) -> list[str]:
    companions = analysis.algorithm.get('companion_paths', [])
    if not isinstance(companions, list) or any(not isinstance(path, str) for path in companions):
        raise ValueError('Invalid persisted companion paths')
    return sorted({analysis.video_path, *(track.path for track in analysis.tracks),
                   *(path for track in analysis.tracks for path in track.companion_paths),
                   *(str(path) for path in companions)})


def _ebml_elements(source: BinaryIO, end: int, cancelled: CancelCheck) -> Iterator[tuple[int, int]]:
    from enzyme.parsers.ebml import read_element_id, read_element_size

    while source.tell() < end:
        _check(cancelled)
        identity = read_element_id(source)
        size_start = source.tell()
        size = read_element_size(source)
        size_width = source.tell() - size_start
        if identity is None or size is None:
            raise ValueError('Cannot read Matroska segment identity element')
        # All-one size data denotes unknown length: https://www.rfc-editor.org/rfc/rfc8794.html#section-6.2
        if size == (1 << (7 * size_width)) - 1:
            if identity != 0x18538067:
                raise ValueError('Cannot verify segment identities past an unknown-size Matroska element')
            element_end = end
        else:
            element_end = source.tell() + size
        if element_end > end:
            raise ValueError('Matroska segment identity element exceeds its parent')
        yield identity, element_end
        source.seek(element_end)


# Matroska element identifiers: https://www.matroska.org/technical/elements.html
_SEGMENT_LINK_FIELDS = {
    0x73A4: ('SegmentUID', 'uid'), 0x7384: ('SegmentFilename', 'string'),
    0x3CB923: ('PreviousSegmentUID', 'uid'), 0x3C83AB: ('PreviousSegmentFilename', 'string'),
    0x3EB923: ('NextSegmentUID', 'uid'), 0x3E83BB: ('NextSegmentFilename', 'string'),
    0x4444: ('SegmentFamily', 'uid'), 0x6924: ('ChapterTranslate', 'master'),
    0x69FC: ('ChapterTranslateEditionUID', 'integer'), 0x69BF: ('ChapterTranslateCodec', 'integer'),
    0x69A5: ('ChapterTranslateID', 'binary'),
}


def _segment_link_field(source: BinaryIO, identity: int, end: int, cancelled: CancelCheck) -> ET.Element:
    name, kind = _SEGMENT_LINK_FIELDS[identity]
    element = ET.Element(name)
    if kind == 'master':
        element.extend(_segment_link_fields(source, end, cancelled))
        return element
    size = end - source.tell()
    if kind == 'uid' and size != 16:
        raise ValueError('Matroska segment link UID must contain exactly 16 bytes')
    if kind == 'integer' and not 1 <= size <= 8:
        raise ValueError('Invalid Matroska chapter translation integer')
    data = source.read(size)
    if len(data) != size:
        raise ValueError('Truncated Matroska segment identity')
    if kind == 'string':
        element.text = data.decode('utf-8').rstrip('\0')
    elif kind == 'integer':
        element.text = str(int.from_bytes(data, 'big'))
    else:
        element.set('format', 'hex')
        element.text = data.hex()
    return element


def _segment_link_fields(source: BinaryIO, end: int, cancelled: CancelCheck) -> list[ET.Element]:
    result = [_segment_link_field(source, identity, element_end, cancelled)
              for identity, element_end in _ebml_elements(source, end, cancelled) if identity in _SEGMENT_LINK_FIELDS]
    return result


def _segment_info_from_segment(source: BinaryIO, end: int, cancelled: CancelCheck) -> ET.Element:
    infos = [_segment_link_fields(source, child_end, cancelled)
             for child_id, child_end in _ebml_elements(source, end, cancelled) if child_id == 0x1549A966]
    if len(infos) != 1:
        raise ValueError('Cannot preserve Matroska identities without exactly one Info element')
    result = ET.Element('Info')
    result.extend(sorted(infos[0], key=lambda element: ET.tostring(element)))
    return result


def _segment_info(path: str, cancelled: CancelCheck) -> ET.Element:
    with open(path, 'rb') as source:
        segments = [_segment_info_from_segment(source, segment_end, cancelled)
                    for identity, segment_end in _ebml_elements(source, Path(path).stat().st_size, cancelled)
                    if identity == 0x18538067]
    if len(segments) != 1:
        raise ValueError('Cannot preserve identities without exactly one Matroska segment')
    return segments[0]


def _mkv_tracks(path: str, cancelled: CancelCheck) -> list[dict[str, JSONValue]]:
    return _objects(_object(json.loads(_run([_tool('mkvmerge'), '-J', path], cancelled))).get('tracks', []))


def _mkv_mapping(path: str, probe: dict[str, JSONValue], cancelled: CancelCheck) -> dict[int, int]:
    streams = [stream for stream in _streams(probe) if stream.get('codec_type') != 'attachment']
    tracks = _mkv_tracks(path, cancelled)
    if len(streams) != len(tracks):
        raise ValueError('Cannot establish exact MKVToolNix stream mapping')
    mapping = {}
    for stream, track in zip(streams, tracks):
        if stream.get('codec_type') != str(track.get('type')).removesuffix('s'):
            raise ValueError('MKVToolNix stream ordering does not match FFprobe')
        mapping[int(str(stream['index']))] = int(str(track['id']))
    return mapping


def _normalize_vobsub_input(path: str, directory: Path) -> str:
    if Path(path).suffix != '.IDX':
        return path
    normalized = directory / 'vobsub-input'
    normalized.mkdir()
    input_path = str(normalized / 'source.idx')
    shutil.copyfile(path, input_path)
    shutil.copyfile(_paired_path(Path(path), '.sub'), normalized / 'source.sub')
    return input_path


def _remux(path: str, output: Path, remove: set[int], cancelled: CancelCheck) -> None:
    probe = _probe(path, cancelled)
    streams = _streams(probe)
    if probe.get('programs'):
        raise ValueError('Container program metadata preservation is unsupported; originals remain unchanged')
    retained = [stream for stream in streams if int(str(stream['index'])) not in remove]
    matroska = 'matroska' in str(_object(probe.get('format', {})).get('format_name', ''))
    if matroska or Path(path).suffix.lower() == '.idx':
        input_path = _normalize_vobsub_input(path, output.parent)
        mapping = _mkv_mapping(input_path, probe, cancelled)
        subtitle_ids = [mapping[int(str(stream['index']))] for stream in retained if stream.get('codec_type') == 'subtitle']
        arguments = [_tool('mkvmerge'), '--no-date', '-o', str(output)]
        if matroska:
            segment_info = output.parent / 'segment-info.xml'
            ET.ElementTree(_segment_info(path, cancelled)).write(segment_info, encoding='utf-8', xml_declaration=True)
            arguments += ['--segmentinfo', str(segment_info)]
        arguments += ['--subtitle-tracks', ','.join(map(str, subtitle_ids))] if subtitle_ids else ['--no-subtitles']
        flag_names = {'default': 'default', 'forced': 'forced-display', 'hearing_impaired': 'hearing-impaired',
                      'visual_impaired': 'visual-impaired', 'descriptions': 'text-descriptions',
                      'original': 'original', 'comment': 'commentary'}
        for stream in retained:
            index = int(str(stream['index']))
            if index not in mapping:
                continue
            for flag, option in flag_names.items():
                if flag == 'original' and not (_flags(stream).get('original') or _flags(stream).get('dub')):
                    continue
                arguments += [f'--{option}-flag' if option != 'default' else '--default-track-flag',
                              f'{mapping[index]}:{int(bool(_flags(stream).get(flag, 0)))}']
        order = ','.join(f'0:{mapping[int(str(stream["index"]))]}' for stream in retained
                         if int(str(stream['index'])) in mapping)
        arguments += ['--track-order', order, input_path]
        _run(arguments, cancelled)
        creation_time = _tags(_object(probe.get('format', {}))).get('creation_time')
        if creation_time:
            creation_time = re.sub(r'\.0+Z$', 'Z', creation_time)
            _run([_tool('mkvpropedit'), str(output), '--edit', 'info', '--set', f'date={creation_time}'], cancelled)
    else:
        arguments = [_tool('ffmpeg'), '-v', 'error', '-nostdin', '-copyts', '-i', path]
        for stream in retained:
            arguments += ['-map', f'0:{stream["index"]}']
        arguments += ['-map_metadata', '0', '-map_chapters', '0', '-copy_unknown', '-c', 'copy', '-avoid_negative_ts', 'disabled']
        for index, stream in enumerate(retained):
            flags = '+'.join(flag for flag, value in _flags(stream).items() if value)
            arguments += [f'-disposition:{index}', flags or '0']
        if 'mov' in str(_object(probe.get('format', {})).get('format_name', '')).split(','):
            arguments += ['-write_btrt', '0', '-movflags', 'use_metadata_tags']
        _run(arguments + ['-n', str(output)], cancelled)
    _verify(path, str(output), remove, cancelled)


def _semantic_tags(value: dict[str, JSONValue]) -> dict[str, str]:
    return {key: _language(item) if key == 'language' else item for key, item in _tags(value).items()
            if key not in ('encoder', 'duration') and not key.startswith(('_statistics_', 'number_of_', 'bps'))}


def _packet_signatures(path: str, subtitles: set[int], cancelled: CancelCheck) -> dict[int, str]:
    with tempfile.TemporaryFile() as packets:
        _run([_tool('ffprobe'), '-v', 'error', '-show_packets', '-show_data_hash', 'sha256',
              '-show_entries', 'packet=stream_index,pts_time,duration_time,data_hash:packet_side_data',
              '-of', 'compact=p=0:nk=0', path], cancelled, packets)
        packets.seek(0)
        hashes = {}
        for line in packets:
            _check(cancelled)
            fields = dict(field.split('=', 1) for field in line.decode().strip().split('|') if '=' in field)
            if 'stream_index' not in fields:
                continue
            index = int(fields.pop('stream_index'))
            if index not in hashes:
                hashes[index] = hashlib.sha256()
            # Bitmap packet durations can be inferred differently; decoded display events are verified separately.
            if index in subtitles:
                fields.pop('duration_time', None)
            hashes[index].update(json.dumps(fields, sort_keys=True).encode() + b'\n')
        return {index: digest.hexdigest() for index, digest in hashes.items()}


def _xml_state(element: ET.Element) -> JSONValue:
    return [element.tag, {key: value for key, value in sorted(element.attrib.items())},
            element.text if len(element) == 0 else None, [_xml_state(child) for child in element]]


def _matroska_metadata(path: str, removed_uids: set[str], cancelled: CancelCheck) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {'segment_info': _xml_state(_segment_info(path, cancelled))}
    statistics = {'BPS', 'DURATION', 'NUMBER_OF_FRAMES', 'NUMBER_OF_BYTES', '_STATISTICS_WRITING_APP',
                  '_STATISTICS_WRITING_DATE_UTC', '_STATISTICS_TAGS'}
    for kind in ('tags', 'chapters'):
        raw = _run([_tool('mkvextract'), path, kind], cancelled)
        if not raw.strip():
            result[kind] = None
            continue
        document = ET.fromstring(raw)
        if kind == 'tags':
            for tag in list(document):
                targets = tag.find('Targets')
                if targets is not None and any(uid.text in removed_uids for uid in targets.findall('TrackUID')):
                    document.remove(tag)
                    continue
                for simple in list(tag.findall('Simple')):
                    if simple.findtext('Name') in statistics:
                        tag.remove(simple)
                if not tag.findall('Simple'):
                    document.remove(tag)
        result[kind] = _xml_state(document)
    return result


def _verify(original: str, staged: str, remove: set[int], cancelled: CancelCheck) -> None:
    before, after = _probe(original, cancelled), _probe(staged, cancelled)
    retained = [stream for stream in _streams(before) if int(str(stream['index'])) not in remove]
    output = _streams(after)
    if len(retained) != len(output):
        raise ValueError('Staged replacement lost or added streams')
    before_subtitles = {int(str(stream['index'])) for stream in _streams(before) if stream.get('codec_type') == 'subtitle'}
    after_subtitles = {int(str(stream['index'])) for stream in output if stream.get('codec_type') == 'subtitle'}
    before_packets = _packet_signatures(original, before_subtitles, cancelled)
    after_packets = _packet_signatures(staged, after_subtitles, cancelled)
    fields = ('codec_name', 'codec_type', 'profile', 'width', 'height', 'pix_fmt', 'sample_rate', 'channels',
              'channel_layout', 'extradata_hash', 'sample_aspect_ratio', 'color_range', 'color_space',
              'color_transfer', 'color_primaries', 'field_order', 'side_data_list')
    for source, target in zip(retained, output):
        old_index, new_index = int(str(source['index'])), int(str(target['index']))
        if any(source.get(field) != target.get(field) for field in fields):
            raise ValueError(f'Staged replacement changed retained stream {old_index} codec properties')
        if _flags(source) != _flags(target):
            raise ValueError(f'Staged replacement changed retained stream {old_index} dispositions')
        if _semantic_tags(source) != _semantic_tags(target):
            raise ValueError(f'Staged replacement changed retained stream {old_index} metadata')
        if before_packets.get(old_index) != after_packets.get(new_index):
            raise ValueError(f'Staged replacement changed retained stream {old_index} payload or presentation timestamps')
        if source.get('codec_type') == 'subtitle':
            duration = float(str(_object(before.get('format', {})).get('duration', 0)))
            bitmap = source.get('codec_name') in ('dvd_subtitle', 'hdmv_pgs_subtitle', 'dvb_subtitle')
            if _events(original, old_index, _SignalOptions(duration, cancelled, bitmap)) != _events(staged, new_index, _SignalOptions(duration, cancelled, bitmap)):
                raise ValueError(f'Staged replacement changed retained subtitle {old_index} display timing')
    if 'matroska' in str(_object(before.get('format', {})).get('format_name', '')):
        track_mapping = _mkv_mapping(original, before, cancelled)
        removed_ids = {track_mapping[index] for index in remove}
        removed_uids = {str(_object(track.get('properties', {})).get('uid'))
                        for track in _mkv_tracks(original, cancelled) if int(str(track['id'])) in removed_ids}
        if _matroska_metadata(original, removed_uids, cancelled) != _matroska_metadata(staged, set(), cancelled):
            raise ValueError('Staged replacement changed Matroska tags or chapter editions or segment identities')
    if _semantic_tags(_object(before.get('format', {}))) != _semantic_tags(_object(after.get('format', {}))):
        raise ValueError('Staged replacement changed global metadata')
    chapters_before, chapters_after = _objects(before.get('chapters', [])), _objects(after.get('chapters', []))
    chapter_fields = ('start_time', 'end_time', 'tags')
    if [[item.get(field) for field in chapter_fields] for item in chapters_before] != [
            [item.get(field) for field in chapter_fields] for item in chapters_after]:
        raise ValueError('Staged replacement changed chapters')


def _copy_vobsub(source: Path, destination: BinaryIO, stream_id: int, cancelled: CancelCheck) -> int:
    written = 0
    with source.open('rb') as packets:
        while prefix := packets.read(4):
            _check(cancelled)
            if len(prefix) != 4 or prefix[:3] != b'\x00\x00\x01':
                raise ValueError('Unsupported VobSub MPEG packet framing')
            code = prefix[3]
            if code == 0xba:
                header = packets.read(10)
                if len(header) != 10 or header[0] & 0xc0 != 0x40:
                    raise ValueError('Unsupported VobSub MPEG pack header')
                size = header[9] & 7
                stuffing = packets.read(size)
                if len(stuffing) != size:
                    raise ValueError('Truncated VobSub MPEG pack header')
                packet = prefix + header + stuffing
            elif code == 0xb9:
                packet = prefix
            else:
                length = packets.read(2)
                if len(length) != 2:
                    raise ValueError('Truncated VobSub packet header')
                size = int.from_bytes(length, 'big')
                payload = bytearray(packets.read(size))
                if not size or len(payload) != size:
                    raise ValueError('Truncated or unbounded VobSub packet')
                if code == 0xbd:
                    if len(payload) < 3 or payload[0] & 0xc0 != 0x80:
                        raise ValueError('Unsupported VobSub PES header')
                    position = 3 + payload[2]
                    if position >= len(payload) or not 0x20 <= payload[position] <= 0x3f:
                        raise ValueError('Invalid VobSub private subtitle stream')
                    payload[position] = 0x20 + stream_id
                packet = prefix + length + payload
            destination.write(packet)
            written += len(packet)
    return written


def _extract_vobsub(intermediate: Path, output: Path, options: _VobSubOptions) -> None:
    original, retained_indices = options.original, options.retained_indices
    track_ids, cancelled = options.track_ids, options.cancelled
    extracted = [output.parent / f'language-{index}.idx' for index in range(len(track_ids))]
    _run([_tool('mkvextract'), str(intermediate), 'tracks',
          *(f'{track_id}:{path}' for track_id, path in zip(track_ids, extracted))], cancelled)
    original_text = original.read_text(encoding='utf-8-sig')
    language_sections = list(re.finditer(r'^id: ([^,]+), index: (\d+)\s*$', original_text, re.MULTILINE))
    if not language_sections or max(retained_indices) >= len(language_sections):
        raise ValueError('Cannot map retained VobSub language metadata')
    original_header = original_text[:language_sections[0].start()]
    selected = re.search(r'^langidx: (\d+)\s*$', original_header, re.MULTILINE)
    if selected:
        selected_id = int(selected.group(1))
        if selected_id not in retained_indices:
            raise ValueError('Retained VobSub pair cannot preserve original default language selection')
        original_header = original_header[:selected.start(1)] + str(retained_indices.index(selected_id)) + original_header[selected.end(1):]
    header: list[str] | None = None
    sections: list[str] = original_header.splitlines()
    offset = 0
    with _paired_path(output, '.sub').open('xb') as combined:
        for index, path in enumerate(extracted):
            text = path.read_text(encoding='utf-8-sig')
            match = re.search(r'^id: ([^,]+), index: \d+\s*$', text, re.MULTILINE)
            if match is None:
                raise ValueError('MKVToolNix extraction did not produce a VobSub language section')
            current_header = text[:match.start()].splitlines()
            # Comments describe extraction; palette, canvas and display settings affect decoding.
            settings = [line for line in current_header if line.strip() and not line.startswith('#')]
            if header is None:
                header = settings
            elif settings != header:
                raise ValueError('Retained VobSub languages require incompatible display settings')
            original_index = retained_indices[index]
            original_section = language_sections[original_index]
            sections.append(f'id: {original_section.group(1)}, index: {index}')
            section_end = language_sections[original_index + 1].start() if original_index + 1 < len(language_sections) else len(original_text)
            settings_text = original_text[original_section.end():section_end]
            sections.extend(line for line in settings_text.splitlines()
                            if line.strip() and not line.startswith(('#', 'timestamp:')))
            body = text[match.end():]
            body = re.sub(r'filepos: ([0-9a-fA-F]+)',
                          lambda item: f'filepos: {int(item.group(1), 16) + offset:09x}', body)
            sections.extend(body.splitlines())
            offset += _copy_vobsub(_paired_path(path, '.sub'), combined, index, cancelled)
    output.write_text('\n'.join(sections) + '\n', encoding='utf-8')


def _staging_directory(by_path: dict[str, list[SubtitleTrack]], keep: set[str], workspace: str) -> Path:
    workspace_path = Path(workspace)
    workspace_path.mkdir(parents=True, exist_ok=True)
    rewritten_paths = {path for path, tracks in by_path.items()
                       if any(track.key not in keep for track in tracks)
                       and (tracks[0].source == 'embedded' or any(track.key in keep for track in tracks))}
    staged_sources = rewritten_paths | {companion for path in rewritten_paths
                                       for track in by_path[path] for companion in track.companion_paths}
    required_bytes = 6 * sum(Path(path).stat().st_size for path in staged_sources)
    if required_bytes:
        _require_space(workspace_path, required_bytes)
    return Path(tempfile.mkdtemp(prefix='prepared-', dir=workspace_path)) if rewritten_paths else workspace_path


def prepare_outputs(analysis: MediaAnalysis, keep_keys: Iterable[str], workspace: str,
                    cancelled: CancelCheck = None) -> list[PreparedFile]:
    keep = set(keep_keys)
    known = {track.key for track in analysis.tracks}
    if not keep <= known:
        raise ValueError('Keeper selection includes an unknown subtitle track')
    expected = _object(analysis.algorithm.get('input_sha256', {}))
    for path in source_paths(analysis):
        if expected.get(path) != _digest(path, cancelled):
            raise ValueError(f'Input changed after analysis: {path}')
    if keep == known:
        return []
    by_path: dict[str, list[SubtitleTrack]] = {}
    for track in analysis.tracks:
        by_path.setdefault(track.path, []).append(track)
    stage = _staging_directory(by_path, keep, workspace)
    prepared: list[PreparedFile] = []
    for path, tracks in by_path.items():
        _check(cancelled)
        removed = {track.stream_index for track in tracks if track.key not in keep}
        if not removed:
            continue
        retained = [track for track in tracks if track.key in keep]
        source = Path(path)
        if tracks[0].source == 'external' and not retained:
            prepared.append(PreparedFile(path, None))
            if source.suffix.lower() == '.idx':
                prepared.append(PreparedFile(str(_paired_path(source, '.sub')), None))
            continue
        output_dir = stage / hashlib.sha256(path.encode()).hexdigest()
        output_dir.mkdir()
        if source.suffix.lower() == '.idx':
            intermediate = output_dir / 'retained.mks'
            _remux(path, intermediate, removed, cancelled)
            mapping = _mkv_mapping(str(intermediate), _probe(str(intermediate), cancelled), cancelled)
            output = output_dir / source.name
            _extract_vobsub(intermediate, output, _VobSubOptions(
                source, [track.stream_index for track in retained], list(mapping.values()), cancelled))
            _verify(path, str(output), removed, cancelled)
            prepared.extend([PreparedFile(path, str(output)),
                             PreparedFile(str(_paired_path(source, '.sub')), str(_paired_path(output, '.sub')))])
        else:
            output = output_dir / source.name
            _remux(path, output, removed, cancelled)
            prepared.append(PreparedFile(path, str(output)))
    return prepared
