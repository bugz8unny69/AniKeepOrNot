"""The one encoder: probe a video, plan its streams, run ffmpeg, check the result.

Standard library only; nothing here imports src.anime. The agent's server
(server.py) and AniKeepOrNot's Local transcode mode both call transcode().

What an encode does (work item T1 1.3):
* keeps every stream (`-map 0`): subtitles, attachments (ASS fonts), stream
  order, languages, titles and default/forced flags are copied;
* copies lossy audio (AAC, Opus, AC3, E-AC3, MP3) and turns everything else
  (FLAC, PCM, TrueHD, DTS-HD MA, ...) into Opus, 128 kbps stereo or 256 kbps
  for more channels;
* encodes the video as HEVC 10-bit Main10 at the given bitrate: VAAPI
  (hevc_vaapi) when "gpu" is allowed, libx265 two-pass when "cpu" is allowed.
  GPU first; when it fails and cpu is allowed, the file is encoded on the CPU.
* probes the output: same number of audio and subtitle streams and
  attachments as the source, duration within one second, or the job fails.
"""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading

ENCODERS = ('gpu', 'cpu')
COPY_AUDIO = frozenset({'aac', 'opus', 'ac3', 'eac3', 'mp3'})
OPUS_STEREO_KBPS = 128
OPUS_MULTI_KBPS = 256
# Used only to estimate the size of a copied track when the container does
# not say its bitrate (MKV often does not): typical bitrates of each codec.
_TYPICAL_KBPS = {'aac': 160, 'opus': 128, 'ac3': 448, 'eac3': 640, 'mp3': 192}
DEFAULT_VAAPI_DEVICE = '/dev/dri/renderD128'
MAX_PROBE_BYTES = 4 * 1024 * 1024


class EncodeError(RuntimeError):
    """The encode failed; str(exc) is the last line ffmpeg said, or what was wrong."""


def parse_encoders(value):
    """'gpu', 'cpu' or 'gpu,cpu' in any order -> the allowed encoders, GPU first."""
    names = {part.strip().lower() for part in str(value or '').split(',') if part.strip()}
    if not names or names - set(ENCODERS):
        raise ValueError('encoders must be gpu, cpu or gpu,cpu')
    return tuple(name for name in ENCODERS if name in names)


def probe(path, *, ffprobe='ffprobe'):
    """ffprobe the file: {'duration': seconds, 'streams': [...]}; EncodeError when unreadable."""
    command = [ffprobe, '-v', 'error', '-show_entries',
               'format=duration:stream=index,codec_type,codec_name,channels,bit_rate,profile'
               ':stream_tags=language,BPS,BPS-eng,title:stream_disposition=default,forced', '-of', 'json', str(path)]
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EncodeError(f'ffprobe could not run: {exc}') from None
    if completed.returncode != 0 or len(completed.stdout) > MAX_PROBE_BYTES:
        raise EncodeError('ffprobe failed: ' + _last_line(completed.stderr))
    try:
        document = json.loads(completed.stdout.decode('utf-8', 'replace'))
        duration = float(document['format']['duration'])
        streams = document['streams']
        if type(streams) is not list or not math.isfinite(duration) or not duration > 0:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise EncodeError('ffprobe output could not be read') from None
    return {'duration': duration, 'streams': streams}


def counts(streams):
    """How many video, audio, subtitle and attachment streams a probe has."""
    result = {'video': 0, 'audio': 0, 'subtitle': 0, 'attachment': 0}
    for stream in streams:
        kind = stream.get('codec_type')
        if kind in result:
            result[kind] += 1
    return result


def audio_plan(streams):
    """What happens to each audio stream, in stream order.

    Each entry: {'audio_index': n (0-based among audio streams), 'codec', 'action':
    'copy' or 'opus', 'kbps': size estimate (copy) or the Opus bitrate (opus)}.
    """
    plan = []
    position = 0
    for stream in streams:
        if stream.get('codec_type') != 'audio':
            continue
        codec = str(stream.get('codec_name') or '').lower()
        channels = stream.get('channels') or 2
        if codec in COPY_AUDIO:
            plan.append({'audio_index': position, 'codec': codec, 'action': 'copy',
                         'kbps': _stream_kbps(stream) or _TYPICAL_KBPS[codec]})
        else:
            plan.append({'audio_index': position, 'codec': codec, 'action': 'opus',
                         'kbps': OPUS_MULTI_KBPS if int(channels) > 2 else OPUS_STEREO_KBPS})
        position += 1
    return plan


def audio_kbps(streams):
    """Total audio bitrate of the output, in kbps, from the same rule the encode uses."""
    return sum(entry['kbps'] for entry in audio_plan(streams))


def _stream_kbps(stream):
    for value in (stream.get('bit_rate'), (stream.get('tags') or {}).get('BPS'),
                  (stream.get('tags') or {}).get('BPS-eng')):
        try:
            bps = int(value)
        except (TypeError, ValueError):
            continue
        if bps > 0:
            return max(1, round(bps / 1000))
    return None


def _audio_arguments(plan):
    arguments = []
    for entry in plan:
        if entry['action'] == 'opus':
            arguments += [f'-c:a:{entry["audio_index"]}', 'libopus',
                          f'-b:a:{entry["audio_index"]}', f'{entry["kbps"]}k']
    return arguments


def _rate_arguments(video_kbps):
    return ['-b:v', f'{video_kbps}k', '-maxrate', f'{int(video_kbps * 1.5)}k', '-bufsize', f'{video_kbps * 2}k']


def commands(encoder, source, output, *, video_kbps, plan, vaapi_device=DEFAULT_VAAPI_DEVICE,
             workdir=None, ffmpeg='ffmpeg'):
    """The ffmpeg command line(s) for one encoder: one for VAAPI, two passes for libx265."""
    source, output = str(source), str(output)
    base = [ffmpeg, '-y', '-nostdin', '-hide_banner', '-nostats', '-progress', 'pipe:1', '-loglevel', 'error']
    keep = ['-map', '0', '-c', 'copy', '-max_muxing_queue_size', '1024']
    if encoder == 'gpu':
        # Software decode, upload to the GPU as 10-bit, encode with VAAPI.
        return [base + ['-vaapi_device', vaapi_device, '-i', source] + keep
                + ['-filter:v:0', 'format=p010,hwupload', '-c:v:0', 'hevc_vaapi', '-profile:v:0', 'main10']
                + _rate_arguments(video_kbps) + _audio_arguments(plan) + ['-f', 'matroska', output]]
    if encoder == 'cpu':
        stats = str(Path(workdir or Path(output).parent) / 'x265.stats')
        first = base + ['-i', source, '-map', '0:v:0', '-c:v', 'libx265', '-preset', 'medium',
                        '-pix_fmt', 'yuv420p10le', '-profile:v', 'main10', '-b:v', f'{video_kbps}k',
                        '-x265-params', f'pass=1:stats={stats}:log-level=error', '-an', '-sn', '-f', 'null', os.devnull]
        second = (base + ['-i', source] + keep
                  + ['-c:v:0', 'libx265', '-preset', 'medium', '-pix_fmt', 'yuv420p10le', '-profile:v:0', 'main10']
                  + _rate_arguments(video_kbps)
                  + ['-x265-params', f'pass=2:stats={stats}:log-level=error'] + _audio_arguments(plan)
                  + ['-f', 'matroska', output])
        return [first, second]
    raise ValueError('unknown encoder')


def _last_line(raw):
    text = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw or '')
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:500] if lines else ''


def run_ffmpeg(command, duration, on_progress=None, cancelled=None):
    """Run one ffmpeg command. Returns (returncode, last stderr line).

    on_progress(fraction) is called from ffmpeg's -progress output as the
    encode advances (0.0 to 1.0).
    """
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
    except OSError as exc:
        return 127, f'ffmpeg could not start: {exc}'
    finished = threading.Event()
    def cancel_process():
        while not finished.wait(.1):
            if cancelled is not None and cancelled.is_set():
                process.kill()
                return
    watcher = threading.Thread(target=cancel_process, daemon=True)
    watcher.start()
    errors = bytearray()

    def drain():
        while True:
            chunk = process.stderr.read1(65536)
            if not chunk:
                return
            del errors[:-65536]
            errors.extend(chunk)
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        for raw in process.stdout:
            line = raw.decode('utf-8', 'replace').strip()
            if on_progress is None or duration <= 0:
                continue
            if line.startswith('out_time_us=') or line.startswith('out_time_ms='):
                try:
                    micros = int(line.split('=', 1)[1])
                except ValueError:
                    continue
                on_progress(min(1.0, max(0.0, micros / 1_000_000 / duration)))
            elif line == 'progress=end':
                on_progress(1.0)
    except BaseException:
        process.kill()
        raise
    finally:
        process.wait()
        finished.set()
        watcher.join()
        reader.join(timeout=5)
        process.stdout.close()
        process.stderr.close()
    return process.returncode, _last_line(bytes(errors))


def transcode(source, output, *, video_kbps, encoders=('gpu', 'cpu'), vaapi_device=DEFAULT_VAAPI_DEVICE,
              workdir=None, on_progress=None, run=run_ffmpeg, ffmpeg='ffmpeg', ffprobe='ffprobe',
              cancelled=None, source_info=None, on_encoder=None):
    """Encode source into output (MKV). Returns {'encoder': 'gpu'|'cpu', 'duration': s}.

    Raises EncodeError with ffmpeg's last error line when every allowed
    encoder failed, or when the output does not match the source.
    """
    encoders = tuple(encoders)
    if type(video_kbps) is not int or video_kbps <= 0:
        raise ValueError('video_kbps must be a positive integer')
    if not encoders or set(encoders) - set(ENCODERS):
        raise ValueError('encoders must be gpu, cpu or both')
    info = source_info if source_info is not None else probe(source, ffprobe=ffprobe)
    before = counts(info['streams'])
    if before['video'] < 1:
        raise EncodeError('the source has no video stream')
    plan = audio_plan(info['streams'])
    last_error = ''
    for encoder in ENCODERS:                      # Fixed order: GPU, then CPU.
        if encoder not in encoders:
            continue
        if on_encoder is not None: on_encoder(encoder)
        steps = commands(encoder, source, output, video_kbps=video_kbps, plan=plan,
                         vaapi_device=vaapi_device, workdir=workdir, ffmpeg=ffmpeg)
        failed = None
        for number, command in enumerate(steps):
            def report(fraction, number=number, total=len(steps)):
                if on_progress is not None:
                    on_progress((number + fraction) / total)
            if cancelled is not None and cancelled.is_set():
                raise EncodeError('cancelled')
            if run is run_ffmpeg:
                code, error = run(command, info['duration'], report, cancelled=cancelled)
            else:
                code, error = run(command, info['duration'], report)
            if code != 0:
                failed = error or f'ffmpeg exited with status {code}'
                break
        if cancelled is not None and cancelled.is_set():
            _remove(output)
            raise EncodeError('cancelled')
        if failed is None:
            check(source_info=info, output=output, ffprobe=ffprobe)
            return {'encoder': encoder, 'duration': info['duration']}
        last_error = f'{encoder}: {failed}'
        _remove(output)
    raise EncodeError(last_error or 'no encoder ran')


def check(*, source_info, output, ffprobe='ffprobe'):
    """The output keeps every audio, subtitle and attachment stream and the duration."""
    result = probe(output, ffprobe=ffprobe)
    before, after = counts(source_info['streams']), counts(result['streams'])
    for kind in ('audio', 'subtitle', 'attachment'):
        if before[kind] != after[kind]:
            _remove(output)
            raise EncodeError(f'output has {after[kind]} {kind} streams, the source has {before[kind]}')
    if abs(result['duration'] - source_info['duration']) > 1.0:
        _remove(output)
        raise EncodeError(f'output duration {result["duration"]:.2f}s differs from source {source_info["duration"]:.2f}s')
    return result


def _remove(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def free_bytes(path):
    """Free space on the filesystem holding path."""
    return shutil.disk_usage(path).free
