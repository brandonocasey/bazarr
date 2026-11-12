import hashlib
import json
import struct
import subprocess
from pathlib import Path

import pytest

from subtitles.cleanup.media import analyze_media, prepare_outputs, source_paths
from subtitles.cleanup.models import ExternalSubtitle, MediaAnalysis


def run(*args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pgs(path):
    def segment(pts, kind, payload):
        return b'PG' + struct.pack('>IIBH', pts * 90000, pts * 90000, kind, len(payload)) + payload

    def composition(objects, number):
        return struct.pack('>HHBHBBBB', 1920, 1080, 0x10, number, 0x80, 0, 0, objects) + (
            struct.pack('>HBBHH', 0, 0, 0, 100, 100) if objects else b'')

    palette = b'\0\0' + bytes([0, 0, 128, 128, 0, 1, 255, 128, 128, 255])
    image = b'\x01\x01\x00\x00\x01\x01\x00\x00'
    object_data = b'\0\0\0\xc0' + (len(image) + 4).to_bytes(3, 'big') + struct.pack('>HH', 2, 2) + image
    path.write_bytes(segment(1, 0x16, composition(1, 0)) + segment(1, 0x17, b'\x01\0' + struct.pack('>HHHH', 100, 100, 2, 2))
                     + segment(1, 0x14, palette) + segment(1, 0x15, object_data) + segment(1, 0x80, b'')
                     + segment(2, 0x16, composition(0, 1)) + segment(2, 0x80, b''))


@pytest.fixture
def media(tmp_path):
    first, second = tmp_path / 'first.en.srt', tmp_path / 'second.en.srt'
    first.write_text('1\n00:00:01,000 --> 00:00:02,000\nhello\n')
    second.write_text('1\n00:00:02,000 --> 00:00:03,000\nother\n')
    bitmap = tmp_path / 'captions.sup'
    pgs(bitmap)
    attachment = tmp_path / 'attachment.txt'
    attachment.write_text('preserve this attachment')
    chapters = tmp_path / 'chapters.txt'
    chapters.write_text(';FFMETADATA1\ntitle=Preserved title\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=3000\ntitle=First chapter\n')
    base = tmp_path / 'base.mkv'
    run('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=32x32:rate=10:duration=4',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4', '-i', str(first), '-i', str(second),
        '-i', str(chapters), '-map', '0', '-map', '1', '-map', '2', '-map', '3', '-map_metadata', '4',
        '-map_chapters', '4', '-c:v', 'ffv1', '-c:a', 'pcm_s16le', '-c:s', 'srt',
        '-metadata:s:s:0', 'language=eng', '-metadata:s:s:0', 'title=Caption one',
        '-metadata:s:s:1', 'language=eng', '-disposition:s:0', 'default', '-disposition:s:1', 'hearing_impaired',
        '-attach', str(attachment), '-metadata:s:t', 'mimetype=text/plain', str(base))
    video = tmp_path / 'Film ü ; v1.2.mkv'
    run('mkvmerge', '-o', str(video), str(base), '--language', '0:eng', str(bitmap))
    return video, first, second, bitmap


def test_analyze_and_stage_preserve_streams_metadata_and_original_files(media, tmp_path):
    video, first, second, bitmap = media
    inputs = [video, first, second, bitmap]
    before = {str(path): digest(path) for path in inputs}
    analysis = analyze_media(str(video), [ExternalSubtitle(str(first), 'eng'), ExternalSubtitle(str(second), 'eng')], str(tmp_path / 'analysis'))
    assert len(analysis.tracks) == 5
    assert analysis.audio_stream_index == 1
    assert analysis.algorithm['vad'] == 'webrtcvad'
    assert all(track.score is not None or track.error for track in analysis.tracks)
    same_display = [track.score for track in analysis.tracks
                    if track.codec == 'hdmv_pgs_subtitle' or track.path == str(first)
                    or track.source == 'embedded' and track.stream_index == 2]
    assert len(same_display) == 3 and same_display[0] is not None
    assert same_display == pytest.approx([same_display[0]] * 3)
    snapshot = json.loads(json.dumps(analysis.to_dict()))
    for track in snapshot['tracks']:
        track['download_score'] = 99999
    assert MediaAnalysis.from_dict(snapshot).to_dict() == analysis.to_dict()
    assert prepare_outputs(analysis, [track.key for track in analysis.tracks], str(tmp_path / 'noop')) == []
    assert not (tmp_path / 'noop').exists()
    keep = [track.key for track in analysis.tracks if track.source == 'embedded' and track.codec != 'subrip']
    staged = prepare_outputs(analysis, keep, str(tmp_path / 'stage'))
    assert {item.original_path for item in staged} == {str(video), str(first), str(second)}
    rewritten = next(item for item in staged if item.original_path == str(video))
    assert rewritten.staged_path and Path(rewritten.staged_path).name == video.name
    remaining = json.loads(run('ffprobe', '-v', 'error', '-show_streams', '-of', 'json', rewritten.staged_path))['streams']
    assert [stream['codec_type'] for stream in remaining] == ['video', 'audio', 'subtitle', 'attachment']
    assert next(stream for stream in remaining if stream['codec_type'] == 'subtitle')['codec_name'] == 'hdmv_pgs_subtitle'
    assert {str(path): digest(path) for path in inputs} == before
    assert source_paths(analysis) == sorted([str(video), str(first), str(second)])


def test_stale_input_unknown_keeper_and_cancellation_never_stage_replacement(media, tmp_path):
    video, first, _, _ = media
    analysis = analyze_media(str(video), [ExternalSubtitle(str(first), 'eng')], str(tmp_path / 'analysis'))
    with pytest.raises(ValueError, match='unknown'):
        prepare_outputs(analysis, ['wrong key'], str(tmp_path / 'stage'))
    first.write_text(first.read_text() + '\nchanged')
    with pytest.raises(ValueError, match='changed after analysis'):
        prepare_outputs(analysis, [], str(tmp_path / 'stage'))
    from concurrent.futures import CancelledError
    with pytest.raises(CancelledError):
        analyze_media(str(video), [], str(tmp_path / 'cancelled'), cancelled=lambda: True)


@pytest.fixture
def vobsub(media, tmp_path):
    _, _, _, bitmap = media
    encoded = tmp_path / 'dvd.mks'
    run('ffmpeg', '-v', 'error', '-copyts', '-fix_sub_duration', '-i', str(bitmap), '-c:s', 'dvdsub', '-f', 'matroska', str(encoded))
    pair = tmp_path / 'one.idx'
    run('mkvextract', str(encoded), 'tracks', f'0:{pair}')
    index = pair.read_text()
    header, section = index.split('id: ', 1)
    section = section[section.index('\n') + 1:]
    raw = pair.with_suffix('.sub').read_bytes()
    assert len(raw) == 2048
    offset = raw.index(b'\x00\x00\x01\xbd')
    substream_position = offset + 9 + raw[offset + 8]
    assert raw[substream_position] == 0x20
    multilingual = tmp_path / 'Film multilingual.idx'
    data = bytearray()
    lines = [header]
    for number, language in enumerate(('en', 'fr', 'de')):
        payload = bytearray(raw)
        payload[substream_position] = 0x20 + number
        lines.append(f'id: {language}, index: {number}\n' + section.replace('filepos: 000000000', f'filepos: {len(data):09x}'))
        data.extend(payload)
    multilingual.write_text('\n'.join(lines))
    multilingual.with_suffix('.sub').write_bytes(data)
    assert len(json.loads(run('ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(multilingual)))['streams']) == 3
    return multilingual


@pytest.mark.parametrize('retained_indices', [[0], [0, 2], []])
def test_vobsub_retains_languages_payloads_timing_and_companion_coherence(media, vobsub, tmp_path, retained_indices):
    video = media[0]
    before = {str(path): digest(path) for path in (video, vobsub, vobsub.with_suffix('.sub'))}
    analysis = analyze_media(str(video), [ExternalSubtitle(str(vobsub), 'und'),
                                         ExternalSubtitle(str(vobsub.with_suffix('.sub')), 'und')], str(tmp_path / 'analysis'))
    external = [track for track in analysis.tracks if track.source == 'external']
    assert all(track.companion_paths == [str(vobsub.with_suffix('.sub'))] for track in external)
    assert source_paths(analysis) == sorted([str(video), str(vobsub), str(vobsub.with_suffix('.sub'))])
    assert [(track.stream_index, track.language) for track in external] == [(0, 'eng'), (1, 'fra'), (2, 'deu')]
    keep = [track.key for track in analysis.tracks if track.source == 'embedded' or track.stream_index in retained_indices]
    outputs = prepare_outputs(analysis, keep, str(tmp_path / 'stage'))
    assert {item.original_path for item in outputs} == {str(vobsub), str(vobsub.with_suffix('.sub'))}
    assert all((item.staged_path is not None) == bool(retained_indices) for item in outputs)
    if retained_indices:
        staged = next(item.staged_path for item in outputs if item.original_path == str(vobsub))
        streams = json.loads(run('ffprobe', '-v', 'error', '-show_streams', '-of', 'json', staged))['streams']
        assert [stream['tags']['language'] for stream in streams] == [['en', 'fr', 'de'][index] for index in retained_indices]
    assert {path: digest(Path(path)) for path in before} == before


def test_ffmpeg_mp4_rewrite_retains_selected_caption_and_audio_video(media, tmp_path):
    _, first, second, _ = media
    video = tmp_path / 'movie.mp4'
    run('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=32x32:rate=10:duration=4',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4', '-i', str(first), '-i', str(second),
        '-map', '0', '-map', '1', '-map', '2', '-map', '3', '-c:v', 'mpeg4', '-c:a', 'aac', '-c:s', 'mov_text',
        '-metadata', 'title=MP4 title', '-metadata:s:s:0', 'language=eng', '-metadata:s:s:1', 'language=eng',
        '-disposition:s:0', 'default', '-disposition:s:1', '0', str(video))
    before = digest(video)
    analysis = analyze_media(str(video), [], str(tmp_path / 'analysis'))
    keep = [analysis.tracks[0].key]
    outputs = prepare_outputs(analysis, keep, str(tmp_path / 'stage'))
    assert len(outputs) == 1 and outputs[0].original_path == str(video)
    assert digest(video) == before
    streams = json.loads(run('ffprobe', '-v', 'error', '-show_streams', '-of', 'json', outputs[0].staged_path))['streams']
    assert [stream['codec_name'] for stream in streams] == ['mpeg4', 'aac', 'mov_text']


def test_selected_audio_is_shared_and_silent_reference_remains_unrankable(media, tmp_path):
    video, first, _, _ = media
    dual = tmp_path / 'dual.mkv'
    run('ffmpeg', '-v', 'error', '-i', str(video), '-f', 'lavfi', '-i', 'anullsrc=r=16000:cl=mono',
        '-map', '0:v', '-map', '0:a', '-map', '1:a', '-map', '0:s', '-c', 'copy', '-c:a:1', 'pcm_s16le',
        '-t', '4', '-disposition:a:0', '0', '-disposition:a:1', 'default', str(dual))
    external = [ExternalSubtitle(str(first), 'en')]
    silent = analyze_media(str(dual), external, str(tmp_path / 'silent'))
    assert silent.audio_stream_index == 2
    assert all(track.score is None and 'constant' in track.error for track in silent.tracks)
    audible = analyze_media(str(dual), external, str(tmp_path / 'audible'), audio_stream_index=1)
    assert audible.audio_stream_index == 1
    assert all(track.score is not None for track in audible.tracks)
    equal_timing = [track.score for track in audible.tracks if track.codec == 'hdmv_pgs_subtitle' or track.source == 'external']
    assert equal_timing[0] == pytest.approx(equal_timing[1])
    assert all(track.language == 'eng' for track in audible.tracks)
    with pytest.raises(ValueError, match='does not exist'):
        analyze_media(str(dual), external, str(tmp_path / 'invalid'), audio_stream_index=0)


@pytest.mark.parametrize('damage,reason', [('metadata', 'metadata'), ('attachment', 'lost or added'), ('timing', 'presentation timestamps'), ('edition', 'Matroska tags or chapter editions')])
def test_prepare_rejects_successful_but_lossy_tool_output(media, tmp_path, monkeypatch, damage, reason):
    import os
    import shutil
    import sys

    video = media[0]
    before = digest(video)
    analysis = analyze_media(str(video), [], str(tmp_path / 'analysis'))
    binaries = tmp_path / 'binaries'
    binaries.mkdir()
    wrapper = binaries / 'mkvmerge'
    real_merge, real_edit = shutil.which('mkvmerge'), shutil.which('mkvpropedit')
    real_extract = shutil.which('mkvextract')
    wrapper.write_text(f'''#!{sys.executable}
import os
import subprocess
import sys
subprocess.run([{real_merge!r}, *sys.argv[1:]], check=True)
if '-o' in sys.argv:
    output = sys.argv[sys.argv.index('-o') + 1]
    if {damage!r} == 'metadata':
        subprocess.run([{real_edit!r}, output, '--edit', 'info', '--set', 'title=Damaged'], check=True)
    elif {damage!r} == 'attachment':
        subprocess.run([{real_edit!r}, output, '--delete-attachment', '1'], check=True)
    elif {damage!r} == 'edition':
        import xml.etree.ElementTree as ET
        document = ET.fromstring(subprocess.check_output([{real_extract!r}, output, 'chapters']))
        edition = document.find('EditionEntry')
        flag = edition.find('EditionFlagHidden')
        if flag is None:
            flag = ET.SubElement(edition, 'EditionFlagHidden')
        flag.text = '1'
        temporary = output + '.chapters.xml'
        ET.ElementTree(document).write(temporary, encoding='utf-8', xml_declaration=True)
        subprocess.run([{real_edit!r}, output, '--chapters', temporary], check=True)
    else:
        temporary = output + '.damaged.mkv'
        subprocess.run([{real_merge!r}, '-o', temporary, '--sync', '0:500', output], check=True)
        os.replace(temporary, output)
''')
    wrapper.chmod(0o755)
    monkeypatch.setenv('PATH', str(binaries) + os.pathsep + os.defpath)
    keep = [track.key for track in analysis.tracks if track.codec == 'hdmv_pgs_subtitle']
    with pytest.raises(ValueError, match=reason):
        prepare_outputs(analysis, keep, str(tmp_path / 'stage'))
    assert digest(video) == before


def test_missing_audio_and_malformed_external_subtitle_are_visible_and_unscored(media, tmp_path):
    video = media[0]
    silent = tmp_path / 'no-audio.mkv'
    run('ffmpeg', '-v', 'error', '-i', str(video), '-map', '0:v', '-map', '0:s', '-c', 'copy', str(silent))
    malformed = tmp_path / 'broken.srt'
    malformed.write_text('not a subtitle')
    before = digest(malformed)
    analysis = analyze_media(str(silent), [ExternalSubtitle(str(malformed), 'eng')], str(tmp_path / 'analysis'))
    assert analysis.audio_stream_index is None
    assert len(analysis.tracks) == 4
    assert all(track.score is None and track.error for track in analysis.tracks)
    outputs = prepare_outputs(analysis, [track.key for track in analysis.tracks if track.source == 'embedded'], str(tmp_path / 'stage'))
    assert len(outputs) == 1 and outputs[0].original_path == str(malformed) and outputs[0].staged_path is None
    assert digest(malformed) == before


def test_uppercase_vobsub_names_are_preserved(media, vobsub, tmp_path):
    import shutil

    uppercase = tmp_path / 'Captions.IDX'
    companion = tmp_path / 'Captions.SUB'
    shutil.copyfile(vobsub, uppercase)
    shutil.copyfile(vobsub.with_suffix('.sub'), companion)
    analysis = analyze_media(str(media[0]), [ExternalSubtitle(str(uppercase), 'eng')], str(tmp_path / 'analysis'))
    keep = [track.key for track in analysis.tracks if track.source == 'embedded' or track.stream_index == 0]
    outputs = prepare_outputs(analysis, keep, str(tmp_path / 'stage'))
    assert {item.original_path for item in outputs} == {str(uppercase), str(companion)}
    assert {Path(item.staged_path).name for item in outputs} == {uppercase.name, companion.name}


def test_microdvd_without_explicit_frame_rate_is_never_scored_using_a_guessed_rate(media, tmp_path):
    video, first, _, _ = media
    known, unknown = tmp_path / 'known.sub', tmp_path / 'unknown.sub'
    known.write_text('{1}{1}25.000\n{25}{50}hello\n')
    unknown.write_text('{25}{50}hello\n')
    analysis = analyze_media(str(video), [ExternalSubtitle(str(path), 'eng') for path in (known, unknown, first)], str(tmp_path / 'analysis'))
    by_name = {Path(track.path).name: track for track in analysis.tracks if track.source == 'external'}
    assert by_name[known.name].score == pytest.approx(by_name[first.name].score), by_name[known.name].error
    assert by_name[unknown.name].score is None
    assert 'no explicit frame rate' in by_name[unknown.name].error
