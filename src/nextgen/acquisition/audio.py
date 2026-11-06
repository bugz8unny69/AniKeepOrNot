"""Audio requirement setting and release-title audio hints.

Japanese+English acquisitions require a Dual/Multi Audio title hint, but
ffprobe remains the only proof of actual audio tracks.
"""
import re

OFF = 'off'
JAPANESE = 'japanese'
JAPANESE_ENGLISH = 'japanese_english'
MODES = (OFF, JAPANESE, JAPANESE_ENGLISH)

_REQUIRED = {OFF: frozenset(), JAPANESE: frozenset({'jpn'}),
             JAPANESE_ENGLISH: frozenset({'jpn', 'eng'})}

# Groups spell this many ways. "Multi-Subs" is subtitles and does not count.
_SEP = r'[ ._+&/,-]*'
_JA = r'(?:ja|jp|jpn|jap|japanese)'
_EN = r'(?:en|eng|english)'
_TAG = re.compile(
    r'(?<![a-z0-9])(?:'
    r'dual' + _SEP + r'(?:audio|aud)?'
    r'|multi' + _SEP + r'(?:audio|aud)'
    r'|multi(?!' + _SEP + r'(?:sub|subs|subtitles?)\b)'
    r'|' + _EN + _SEP + r'dub(?:bed|s)?'
    r'|dub(?:bed)?'
    r'|' + _JA + _SEP + _EN + r'|' + _EN + _SEP + _JA +
    r')(?![a-z0-9])', re.I)


def required_languages(mode=None):
    """Audio languages ffprobe must find.

    If the configuration variable has no value (None or empty), defaults to 'japanese'.
    Passing 'none' is an invalid mode and raises ValueError.
    """
    if mode is None or mode == '':
        mode = JAPANESE
    if mode == 'none':
        raise ValueError("invalid audio check: 'none' is not a valid language requirement")
    try:
        return _REQUIRED[mode]
    except (KeyError, TypeError):
        raise ValueError('unknown audio check') from None


def wants_english(mode=None):
    return 'eng' in required_languages(mode)


# Explicit Dual/Multi Audio or Japanese+English title hints only. English
# Dub alone does not advertise the Japanese track required by this mode.
_DUAL_TAG = re.compile(
    r'(?<![a-z0-9])(?:'
    r'dual' + _SEP + r'(?:audio|aud)?'
    r'|multi' + _SEP + r'(?:audio|aud)'
    r'|multi(?!' + _SEP + r'(?:sub|subs|subtitles?)\b)'
    r'|' + _JA + _SEP + _EN +
    r'|' + _EN + _SEP + _JA +
    r')(?![a-z0-9])', re.I)


def dual_audio_tagged(title):
    """Candidate hint for both audio tracks; actual languages require ffprobe."""
    return type(title) is str and _DUAL_TAG.search(title) is not None


def audio_tagged(title):
    """True when the release title advertises dual/multi audio or an English dub."""
    return type(title) is str and _TAG.search(title) is not None


_INCOMPATIBLE_TAG = re.compile(
    r'(?<![a-z0-9])(?:'
    r'french|vostfr|\bvf\b|ita(?:liano?)?|ger(?:man|dub)?|espa[nñ]ol|castellano|latino|rus(?:sian)?'
    r')(?![a-z0-9])', re.I)


def is_audio_incompatible(title, mode=JAPANESE_ENGLISH):
    """True if release title explicitly advertises an incompatible foreign dub without dual/multi audio."""
    if mode != JAPANESE_ENGLISH or not title or type(title) is not str:
        return False
    if dual_audio_tagged(title):
        return False
    return _INCOMPATIBLE_TAG.search(title) is not None

