"""Named checks that log what they checked, and what failed, at DEBUG (JD-1d).

Every activity states its checks through this module:

    from src.nextgen.checks import check, require

    check(logger, 'TORRENT_PRESENT', found is not None, actual=state)
    require(logger, 'SERIES_PATH_INSIDE_LIBRARY', path.is_relative_to(root),
            KeepReview, expected=str(root), actual=str(path), path=str(path))

* Each check logs one DEBUG line: the check (file:line function NAME, always
  the real line), passed or failed, and when given, expected, actual and the
  path. Nothing is formatted unless DEBUG is on, so checks are cheap otherwise.
* `require` raises the given error when the check fails, carrying the same
  check as `failed_check` (and the path as `failed_path`). The worker records
  them in History and the dashboard shows them.
"""
import logging
import re
import sys

from src.nextgen.diagnostics import event

_NAME = re.compile('[A-Z][A-Z0-9_]{0,31}')        # the NAME part of a History failed_check

_ROOT_MARK = '/src/nextgen/'


def _where(depth, name):
    frame = sys._getframe(depth)
    filename = frame.f_code.co_filename.replace('\\', '/')
    relative = 'src/nextgen/' + filename.split(_ROOT_MARK, 1)[-1] if _ROOT_MARK in filename else filename
    return f'{relative}:{frame.f_lineno} {frame.f_code.co_name} {name}'


def _text(value):
    if value is None:
        return None
    return value if type(value) is str else repr(value)


def check(logger, name, passed, *, expected=None, actual=None, path=None, _depth=2, **ids):
    """Log one named check at DEBUG and return whether it passed."""
    if type(name) is not str or not _NAME.fullmatch(name):
        raise ValueError('check names are UPPER_SNAKE_CASE, at most 32 characters')
    passed = bool(passed)
    if logger is not None and logger.isEnabledFor(logging.DEBUG):
        event(logger, logging.DEBUG, 'CHECK_PASSED' if passed else 'CHECK_FAILED',
              check=_where(_depth, name), result='passed' if passed else 'failed',
              expected=_text(expected), actual=_text(actual), path=_text(path), component='worker', **ids)
    return passed


def require(logger, name, passed, error, *, expected=None, actual=None, path=None, **ids):
    """Like check(); a failed check raises `error` naming this check and path.

    `error` is an exception class that accepts failed_check/failed_path
    keywords (KeepReview, StagingReview, …) or an instance to raise as is.
    """
    if check(logger, name, passed, expected=expected, actual=actual, path=path, _depth=3, **ids):
        return
    where = _where(2, name)
    if isinstance(error, BaseException):
        raise error
    try:
        raise error(failed_check=where, failed_path=path if type(path) is str and path.startswith('/') else None)
    except TypeError:
        exc = error()
        exc.failed_check = where
        raise exc from None


def here(name):
    """The failed_check for a check that fails on the calling line: always the real line.

        raise KeepReview(failed_check=here('SERIES_MISSING'))
    """
    if type(name) is not str or not _NAME.fullmatch(name):
        raise ValueError('check names are UPPER_SNAKE_CASE, at most 32 characters')
    return _where(2, name)
