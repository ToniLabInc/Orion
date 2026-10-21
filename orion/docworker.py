"""The conversion worker: turns documents into Markdown, in a process of its own (see ``orion.documents``).

A document is untrusted input to parsers written in C and in Python, so nothing of Orion runs here beside this file:
``python -I -B -m orion.docworker MEMORY_MB SECONDS CHARS TABLES SPEC...`` (TABLES is 1 when a PDF's tables are to become
Markdown tables, which markitdown does slowly, and 0 for its text alone). A SPEC is a JSON list ``[extension, inode,
size, mtime_ns, path]``: what the scan saw of a file, which the worker checks again on the file it opens (never following a
link), so a file swapped for another after the scan is refused. The limits come first: address space, no core dumps,
no files written (not even a ``.pyc``), a processor-time limit, the lowest priority; only then is markitdown imported.

For each SPEC it prints ``{"start": i}``, then ``{"i": i, "ok": true, "text": ..., "cut": bool}`` or ``{"i": i, "ok":
false, "error": ...}``, one JSON object per line. The wall-clock limit per document is an alarm that raises an
exception markitdown cannot catch (it catches ``Exception`` and tries the next converter, which would only take longer);
a document that never returns to Python is ended by whoever started this process, which knows from the lines above
which document it was.

The worker has no need of a file system or a network: the one call that would take both of them away
(``orion.sandbox``, when it exists) wraps this process and nothing else changes.
"""

import json
import os
import re
import resource
import signal
import stat
import sys
from typing import Final

ERROR_CHARS: Final = 240  # the longest error message sent back
OPEN_FILES: Final = 256
_CONTROL: Final = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # all but tab and newline (a PDF page ends in \x0c)


class _TooSlow(BaseException):
    """The alarm for one document went off (a BaseException, so that no ``except Exception`` of a library eats it)."""


def _alarm(_signal: int, _frame: object) -> None:
    raise _TooSlow


def _confine(memory_mb: int, cpu_s: int) -> None:
    """Take away what a parser has no business with, before any parser is imported."""
    limit = memory_mb << 20
    for what, soft, hard in ((resource.RLIMIT_AS, limit, limit), (resource.RLIMIT_CORE, 0, 0),
                             (resource.RLIMIT_FSIZE, 0, 0), (resource.RLIMIT_NOFILE, OPEN_FILES, OPEN_FILES),
                             (resource.RLIMIT_CPU, cpu_s, cpu_s + 10)):
        resource.setrlimit(what, (soft, hard))
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # a write then fails with an error instead of ending the process
    signal.signal(signal.SIGALRM, _alarm)
    os.nice(15)
    try:
        os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))  # only the processor time nobody wants
    except OSError:
        pass  # niceness alone then


def _open(spec: list[object]) -> tuple[int, str]:
    """The file a SPEC names, opened without following a link and checked against what the scan saw.

    Returns:
        (the file descriptor, the extension).

    Raises:
        OSError: it can't be opened, is not a plain file, or is not the file the scan saw.
    """
    extension, inode, size, mtime_ns, path = spec
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            raise OSError("it is not a plain file")
        if (status.st_ino, status.st_size, status.st_mtime_ns) != (inode, size, mtime_ns):
            raise OSError("it changed after it was found")
    except BaseException:
        os.close(fd)
        raise
    return fd, str(extension)


def _why(error: BaseException) -> str:
    """What went wrong, in words for the user (a message from a library is cut short)."""
    name = type(error).__name__
    attempts = [a.exc_info[1] for a in getattr(error, "attempts", ()) if a.exc_info and a.exc_info[1]]  # a reader's own failures
    if isinstance(error, MemoryError) or any(isinstance(cause, MemoryError) for cause in attempts):
        return "it needs more memory than a conversion may use (documents.worker_memory_mb)"
    if isinstance(error, OSError):
        return error.strerror or str(error)
    if name == "UnsupportedFormatException":
        return "this kind of file can't be turned into text"
    if name == "FileConversionException":
        return "it is damaged, or not what its name says" + (f" ({attempts[0]})" if attempts else "")
    if name == "MissingDependencyException":
        return "a library for this kind of file is missing (uv sync)"
    return f"{name}: {error}"


def _pdf_text_converter() -> object:
    """A converter for PDF files that reads their text with pdfminer alone. markitdown's own looks at every page's words
    twice more (for tables and forms) and takes about fourteen times as long for the same words on prose; this is what
    it does itself for a document with no table in it."""
    import io

    from markitdown import DocumentConverter, DocumentConverterResult
    from pdfminer.high_level import extract_text

    class PdfText(DocumentConverter):
        def accepts(self, file_stream: object, stream_info: object, **kwargs: object) -> bool:
            return (getattr(stream_info, "extension", "") or "").lower() == ".pdf"

        def convert(self, file_stream: io.BufferedIOBase, stream_info: object, **kwargs: object) -> object:
            return DocumentConverterResult(markdown=extract_text(io.BytesIO(file_stream.read())))

    return PdfText()


def main(argv: list[str]) -> int:
    """Convert the documents the arguments name; returns the process's exit code."""
    memory_mb, seconds, chars, tables = int(argv[0]), int(argv[1]), int(argv[2]), argv[3] == "1"
    specs = [json.loads(arg) for arg in argv[4:]]
    reply = os.fdopen(os.dup(1), "w", encoding="utf-8", errors="replace")  # the protocol: nothing else may write here
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    _confine(memory_mb, seconds * len(specs) + 30)

    def say(**fields: object) -> None:
        reply.write(json.dumps(fields, ensure_ascii=False) + "\n")
        reply.flush()

    try:
        from markitdown import MarkItDown, StreamInfo

        converter = MarkItDown(enable_plugins=False)
        if not tables:
            converter.register_converter(_pdf_text_converter(), priority=-1.0)  # type: ignore[arg-type]  # tried first
    except Exception as e:  # noqa: BLE001 (any failure to load the parsers is the same to the user)
        for i in range(len(specs)):
            say(i=i, ok=False, error=f"the document reader can't start ({type(e).__name__}: {e})"[:ERROR_CHARS])
        return 3
    for i, spec in enumerate(specs):
        say(start=i)
        fd = -1
        try:
            fd, extension = _open(spec)
            signal.setitimer(signal.ITIMER_REAL, seconds)
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                text = converter.convert_stream(stream, stream_info=StreamInfo(extension=f".{extension}")).markdown
                after = os.fstat(stream.fileno())
            if (after.st_ino, after.st_size, after.st_mtime_ns) != (spec[1], spec[2], spec[3]):
                raise OSError("it changed while it was being read")
            text = _CONTROL.sub("", text.replace("\x0c", "\n\n")).encode("utf-8", "replace").decode("utf-8")
            say(i=i, ok=True, text=text[:chars], cut=len(text) > chars)
        except _TooSlow:
            say(i=i, ok=False, error=f"it took longer than {seconds} seconds (documents.convert_timeout_s)")
        except Exception as e:  # noqa: BLE001 (whatever a parser raises is one document's error, not the batch's)
            say(i=i, ok=False, error=_why(e)[:ERROR_CHARS])
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if fd >= 0:
                os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
