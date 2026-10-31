"""What a run came to: its outcome from how it ended, what a program given
a manifest reported, and the JSON files exec writes.
"""

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from stat import S_ISREG

from exec.box import Run
from exec.inputs import Declared, Entry, Item
from exec.shared import FILE_NAME, OUTPUT_LIMIT, PrimitiveError

JAVA_OUT_OF_MEMORY = 3
TEXTS = ("label", "message", "staff_message")
"""The text files a report may hold besides its reports, each an output of
its name: a finer name for the outcome, a message for the entrant, and one for
staff alone."""
VERDICT_WORDS = ("time_limit", "memory_limit", "output_limit", "runtime_error")
"""The outcomes a report's verdict may name besides `0` and `1`: a program
that judged another program's run, as `unicon/interactive`'s verdict script
judges the programs exec ran together, says whose limit it was, or that it
crashed. From a `self_reported` run, the entrant's code, such a word is the
run's `runtime_error`, so that code never chooses its own outcome."""
REPORT_TEXT_LIMIT = 64 * 1024
"""The most bytes one file in a report folder may hold."""
REPORT_FILES = 64
"""The most files a report folder may hold."""
DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
"""A reported number, as JSON writes one: so `1e-05`, as Python prints a small
float, is one, and `nan`, `inf` and `0x10` are not."""
WHOLE = re.compile(r"-?(?:0|[1-9][0-9]*)")


class ReportError(Exception):
    """What a run given a manifest reported does not hold: the end of a
    sentence that begins with the run's report."""


@dataclass(frozen=True)
class Report:
    """What a run given a manifest reported: its verdict, `0`, `1` or one of
    `VERDICT_WORDS`, the outputs it gives, and the files only shown with the
    run, by name."""

    verdict: str
    outputs: dict[str, object]
    shown: dict[str, bytes]


def reported(
    item: Item, entry: Entry, outcome: str, folder: Path, out: Path
) -> dict[str, object]:
    """The outputs a run of the one-program form, its program `entry` given
    a manifest, adds from its report `folder`, and the outcome its verdict
    gives, the run's processes all gone.

    Each report is an output on a verdict of `0` alone, as an output that
    counts is written only on `accepted`; the texts in `TEXTS` are outputs on
    either verdict, and every other file is copied under `out/extras/`, shown
    with the run and never a column, as are the reports under `1`.

    A run that did not end `accepted` did not judge, nor did one whose
    report does not hold: an error of the step, retried and then left for
    staff, unless the run is `self_reported`, when what the entrant's code
    did is the run's own outcome, its limit or a `runtime_error`, with no
    report. A `self_reported` run's verdict word is a `runtime_error` too.
    """
    if outcome != "accepted":
        if entry.self_reported:
            return {}
        raise PrimitiveError(
            f"the program given a manifest for run {item.run} did not judge: it "
            f"ended in {outcome.replace('_', ' ')}"
        )
    try:
        report = read_report(folder, entry.manifest or {})
    except ReportError as fault:
        if entry.self_reported:
            return {"outcome": "runtime_error"}
        raise PrimitiveError(f"the report of run {item.run} {fault}") from None
    if entry.self_reported and report.verdict in VERDICT_WORDS:
        return {"outcome": "runtime_error"}
    outputs = dict(report.outputs)
    if report.shown:
        extras = out / "extras"
        extras.mkdir(exist_ok=True)
        for name, content in report.shown.items():
            (extras / name).write_bytes(content)
        outputs["extras"] = {"folder": f"out/{item.number}/extras"}
    outputs["outcome"] = verdict_outcome(report.verdict)
    return outputs


def verdict_outcome(verdict: str) -> str:
    """The outcome a report's verdict gives: `0`, judged and counted, is
    `accepted`, `1` is `wrong_answer`, and a word of `VERDICT_WORDS` is that
    outcome."""
    if verdict == "0":
        return "accepted"
    if verdict == "1":
        return "wrong_answer"
    return verdict


def read_report(folder: Path, manifest: dict[str, Declared]) -> Report:
    """Read what a run wrote to its report folder, against what its manifest
    declares, raising `ReportError` when it does not hold: no `verdict`, or
    one that is not `0`, `1` or a word of `VERDICT_WORDS`, under which, as
    under `1`, the reports are shown unchecked; under `0`, a declared report
    that is not optional missing, or a number that is not a finite decimal
    or lies outside its bounds; on any verdict, a file that is not UTF-8 text
    without a NUL, as every file is shown as text, a file over
    `REPORT_TEXT_LIMIT`, more than `REPORT_FILES` of them, or anything there
    that is not a plain file under a plain name.
    """
    files = report_files(folder)
    if "verdict" not in files:
        raise ReportError("has no verdict")
    written = files.pop("verdict").strip()
    verdict = written.decode("ascii", "replace")
    if verdict not in ("0", "1", *VERDICT_WORDS):
        raise ReportError(
            f"has a verdict {shown_bytes(written)}, not 0, 1 or an outcome word"
        )
    outputs: dict[str, object] = {}
    for name in TEXTS:
        if name in files:
            outputs[name] = report_text(name, files.pop(name))
    if verdict == "0":
        for name, port in manifest.items():
            if name not in files:
                if not port.optional:
                    raise ReportError(f"has no {name}, which the manifest declares")
                continue
            content = files.pop(name)
            if port.kind == "text":
                outputs[name] = report_text(name, content)
            else:
                outputs[name] = report_number(name, content, port)
    for name, content in files.items():
        report_text(name, content)
    return Report(verdict, outputs, files)


def report_files(folder: Path) -> dict[str, bytes]:
    """Every file in a report folder by name, each read whole, none through a
    link: a run that left a link there to a file it may not read, the next
    run's input, would have exec read that file for it. The folder itself is
    opened without following a link, since the box's user owns `/box` and
    could put a link in its place, and everything in it is opened through
    that descriptor. The folder is listed only as far as one file past
    `REPORT_FILES`, so a run that left a million costs exec no more.
    """
    try:
        directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise ReportError(f"could not be opened: {error.strerror}") from None
    try:
        return folder_files(directory)
    finally:
        os.close(directory)


def folder_files(directory: int) -> dict[str, bytes]:
    """Every plain file in the report folder open on `directory`, by name,
    each opened through that descriptor, never through a link."""
    entries: list[str] = []
    try:
        with os.scandir(directory) as listing:
            for entry in listing:
                entries.append(entry.name)
                if len(entries) > REPORT_FILES:
                    raise ReportError(f"holds more than {REPORT_FILES} files")
    except OSError as error:
        raise ReportError(f"could not be listed: {error.strerror}") from None
    files: dict[str, bytes] = {}
    for name in sorted(entries):
        if len(name) > 255 or not FILE_NAME.fullmatch(name):
            shown = shown_bytes(os.fsencode(name))
            raise ReportError(f"holds {shown}, not a plain name")
        try:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except OSError:
            raise ReportError(f"holds {name}, not a plain file") from None
        try:
            if not S_ISREG(os.fstat(descriptor).st_mode):
                raise ReportError(f"holds {name}, not a plain file")
            content = read_capped(descriptor, REPORT_TEXT_LIMIT + 1)
        except OSError as error:
            raise ReportError(
                f"holds {name}, which could not be read: {error.strerror}"
            ) from None
        finally:
            os.close(descriptor)
        if len(content) > REPORT_TEXT_LIMIT:
            raise ReportError(f"holds {name}, over {REPORT_TEXT_LIMIT // 1024} KB")
        files[name] = content
    return files


def read_capped(descriptor: int, limit: int) -> bytes:
    """Up to `limit` bytes of an open file, read to its end or the limit."""
    chunks: list[bytes] = []
    read = 0
    while read < limit:
        chunk = os.read(descriptor, limit - read)
        if not chunk:
            break
        chunks.append(chunk)
        read += len(chunk)
    return b"".join(chunks)


def report_text(name: str, content: bytes) -> str:
    """One reported text: UTF-8 with no NUL, which no text the forge keeps may
    hold, its trailing newlines left off, as `echo` and `print` add one."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ReportError(f"has {name} not in UTF-8") from None
    if "\0" in text:
        raise ReportError(f"has {name} holding a NUL character")
    return text.rstrip("\n")


def report_number(name: str, content: bytes, port: Declared) -> int | float:
    """One reported number: a decimal as JSON writes one, with spaces and
    newlines around it, finite as a double and within the declared bounds,
    compared as the decimal written. A whole number is given as one.
    """
    text = content.strip()
    if not DECIMAL.fullmatch(text.decode("ascii", "replace")):
        raise ReportError(f"has {name} {shown_bytes(text)}, not a decimal number")
    try:
        value = Decimal(text.decode("ascii"))
        finite = math.isfinite(float(value))
    except ArithmeticError:
        finite = False
    if not finite:
        raise ReportError(f"has {name} {shown_bytes(text)}, not a finite number")
    if port.at_least is not None and value < port.at_least:
        raise ReportError(f"has {name} {value}, below its least, {port.at_least}")
    if port.at_most is not None and value > port.at_most:
        raise ReportError(f"has {name} {value}, above its most, {port.at_most}")
    return int(value) if WHOLE.fullmatch(text.decode("ascii")) else float(value)


def shown_bytes(content: bytes) -> str:
    """What a run wrote, for an error staff read: quoted, and cut short."""
    text = content[:24].decode("utf-8", "replace")
    return repr(text + "...") if len(content) > 24 else repr(text)


def judge(run: Run, kind: str) -> str:
    """Give a run its outcome, from what isolate and the box's tmpfs say, in
    this order.

    A run the kernel killed for the box's memory is `memory_limit`, though
    isolate reads that as a signal too, and so is one that left its tmpfs no
    file or folder to give: it was stopped by what its files held, as every
    one of them takes memory. Then `TO`, isolate's for the CPU time and the
    wall clock both, is `time_limit`. Then output over `OUTPUT_LIMIT` is
    `output_limit`, before the signal, since a run that printed on died of
    `SIGPIPE` when exec stopped reading. A jar that ran out of Java heap
    exits with code 3, `memory_limit`. Any other signal or failure is a
    runtime error.
    """
    if run.out_of_memory or run.no_inodes:
        return "memory_limit"
    if run.status == "TO":
        return "time_limit"
    if run.output_bytes > OUTPUT_LIMIT:
        return "output_limit"
    if kind == "java" and run.returncode == JAVA_OUT_OF_MEMORY:
        return "memory_limit"
    if run.status in ("RE", "SG") or run.returncode != 0:
        return "runtime_error"
    return "accepted"


def write_json(path: Path, document: Mapping[str, object]) -> None:
    """Write a JSON file whole: to a temporary name first, then renamed. The
    temporary file is made afresh, never opened through a link a run left.
    """
    partial = path.with_name(path.name + ".partial")
    partial.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(partial, flags, 0o644), "w", encoding="utf-8") as stream:
        stream.write(json.dumps(document, ensure_ascii=False))
    partial.replace(path)
