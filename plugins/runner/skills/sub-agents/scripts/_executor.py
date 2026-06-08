"""Subprocess driver: spawn the CLI, consume its stream, shape the response."""

from __future__ import annotations

import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from _builder import AgentInvocation, build_invocation_args
from _stream import StreamProcessor

_SUCCESS_EXIT_CODES = (0, 143, -15)  # 0 ok, 143/-15 = SIGTERM (we asked it to stop)

# Exit code for a failed environment preflight (config dir not writable etc.).
# 126 = "command found but not executable / permission problem" by convention;
# distinct from 127 (not found) and 124 (timeout) so callers can branch on it.
_PREFLIGHT_EXIT_CODE = 126


def _partial_response(cli: str, result: dict | None, exit_code: int, error: str) -> dict:
    return {
        "result": result.get("result", "") if result else "",
        "exit_code": exit_code,
        "status": "partial" if result else "error",
        "cli": cli,
        "error": error,
    }


def _error_response(
    cli: str,
    exit_code: int,
    error: str,
    partial_result: dict | None = None,
    root_cause: str | None = None,
) -> dict:
    response = {
        "result": partial_result.get("result", "") if partial_result else "",
        "exit_code": exit_code,
        "status": "error",
        "cli": cli,
        "error": error,
    }
    if root_cause:
        response["root_cause"] = root_cause
    return response


def _summarize_stderr(stderr: str) -> tuple[str, str | None]:
    """Collapse repeated stderr lines and extract the terminal error.

    Sub-agent CLIs can spew many identical diagnostic lines (e.g. cursor-agent
    repeats ``failed to copy trust settings of system certificate`` ~18×) and
    bury the one actionable line (``EPERM ... cli-config.json.tmp``) at the very
    end. Runs of consecutive duplicate lines are collapsed with a ``(×N)``
    count, and the last error-shaped line is surfaced as the root cause so
    callers can branch on the real failure instead of parsing the noise.

    Returns ``(deduped_text, root_cause_or_None)``.
    """
    lines = [ln for ln in (stderr or "").splitlines() if ln.strip()]
    if not lines:
        return "", None

    # Collapse consecutive duplicates, annotating the repeat count.
    collapsed: list[list] = []
    for ln in lines:
        if collapsed and collapsed[-1][0] == ln:
            collapsed[-1][1] += 1
        else:
            collapsed.append([ln, 1])
    deduped = "\n".join(
        f"{text} (×{count})" if count > 1 else text for text, count in collapsed
    )

    # Root cause: the last line that looks like a terminal error, falling back
    # to the final line when nothing matches the error-shaped heuristics.
    _ERROR_MARKERS = ("error", "eperm", "eacces", "permission denied", "fatal")
    root_cause = next(
        (text for text, _ in reversed(collapsed) if any(m in text.lower() for m in _ERROR_MARKERS)),
        collapsed[-1][0],
    )
    return deduped, root_cause


def build_final_response(
    cli: str,
    returncode: int | None,
    result: dict | None,
    stdout_lines: list,
    stderr: str,
    terminated_by_us: bool = False,
) -> dict:
    """Assemble the response dict from process exit state and parsed result.

    ``returncode is None`` means the process has not actually finished — that
    is treated as a failure (the original ``or 0`` masked this).

    ``terminated_by_us`` records that we called ``terminate()`` after parsing a
    complete result. The exit code is then irrelevant: POSIX reports SIGTERM as
    143 / -15, but Windows' TerminateProcess yields 1. Trusting the intent
    rather than the platform-specific exit code keeps success detection
    consistent across OSes.
    """
    exit_code = returncode if returncode is not None else 1

    if result and (terminated_by_us or exit_code in _SUCCESS_EXIT_CODES):
        status = "success"
    elif result:
        status = "partial"
    else:
        status = "error"

    response = {
        "result": result.get("result", "") if result else "".join(stdout_lines),
        "exit_code": exit_code,
        "status": status,
        "cli": cli,
    }
    if status == "error":
        msg = f"CLI exited with code {exit_code}"
        deduped, root_cause = _summarize_stderr(stderr or "")
        if root_cause:
            # Lead with the actionable cause instead of burying it under
            # repeated diagnostic spam.
            msg += f": {root_cause}"
            response["root_cause"] = root_cause
        if deduped and deduped != root_cause:
            # Keep the full (deduped) stderr available for debugging.
            response["stderr"] = deduped
        response["error"] = msg
    return response


_LINE = "line"
_EOF = "eof"

# Cap on accumulated stdout codepoints per invocation. Protects the broker
# from OOM if a sub-agent emits high-rate non-terminal output for the full
# wall-clock timeout (default 10 minutes). Counted via len(str) since stdout
# is read in text mode — for ASCII CLI output (the common case) this equals
# bytes; for non-ASCII content the actual memory pressure can be up to ~4×
# this number. 64 M codepoints is a safety net far above realistic transcripts.
_MAX_STDOUT_CHARS = 64 * 1024 * 1024


def _spawn_reader(process: subprocess.Popen) -> queue.Queue:
    """Push each stdout line into a queue from a daemon thread.

    Without this, ``readline()`` blocks indefinitely if the CLI hangs without
    closing stdout — the timeout in :func:`_drive_process` only governs queue
    waits, so the reader thread could otherwise outlive the parent's timeout
    deadline. ``daemon=True`` ensures the thread dies with the interpreter.
    """
    line_q: queue.Queue = queue.Queue()

    def reader() -> None:
        try:
            for line in iter(process.stdout.readline, ""):
                line_q.put((_LINE, line))
        finally:
            line_q.put((_EOF, None))

    threading.Thread(target=reader, daemon=True).start()
    return line_q


def _timeout_payload(cli: str, processor: StreamProcessor, timeout_ms: int) -> dict:
    return _partial_response(cli, processor.get_result(), 124, f"Timeout after {timeout_ms}ms")


def _drain_to_eof(line_q: queue.Queue, budget_sec: float = 0.5) -> None:
    """Best-effort: consume the reader queue until _EOF or short budget.

    Used after kill() so that ``communicate()`` reads stderr without racing
    the reader thread on stdout. Safe to call when the reader is already
    done — the queue already holds an _EOF sentinel.
    """
    deadline = time.monotonic() + budget_sec
    while time.monotonic() < deadline:
        try:
            kind, _ = line_q.get(timeout=0.05)
        except queue.Empty:
            return
        if kind == _EOF:
            return


def _drive_process(process: subprocess.Popen, cli: str, timeout_ms: int) -> dict:
    """Read process stdout via StreamProcessor, enforce a wall-clock deadline.

    The wall-clock deadline covers the entire subprocess lifetime — including
    cases where the CLI never produces stdout, blocks on stderr, or stops
    emitting lines. A blocking ``readline()`` in the main thread would never
    reach the timeout check, so reads are delegated to a background thread
    and observed via a queue.

    After a terminal event is parsed we keep draining the queue until the
    reader thread reports EOF before calling ``communicate()`` — that way
    only one consumer ever reads ``process.stdout``.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    processor = StreamProcessor()
    stdout_lines: list = []
    accumulated_chars = 0
    line_q = _spawn_reader(process)
    saw_terminal = False

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                _drain_to_eof(line_q)
                process.communicate()
                return _timeout_payload(cli, processor, timeout_ms)

            try:
                kind, line = line_q.get(timeout=remaining)
            except queue.Empty:
                process.kill()
                _drain_to_eof(line_q)
                process.communicate()
                return _timeout_payload(cli, processor, timeout_ms)

            if kind == _EOF:
                break
            stdout_lines.append(line)
            accumulated_chars += len(line)
            if accumulated_chars > _MAX_STDOUT_CHARS:
                # Defensive cap: a sub-agent emitting unbounded non-terminal
                # output would otherwise grow stdout_lines until the wall-clock
                # deadline (default 10 min). Kill it and report partial.
                process.kill()
                _drain_to_eof(line_q)
                process.communicate()
                return _error_response(
                    cli,
                    1,
                    f"Sub-agent stdout exceeded {_MAX_STDOUT_CHARS} characters; aborted",
                    partial_result=processor.get_result(),
                )
            if not saw_terminal and processor.process_line(line):
                # Processor saw a terminal event; ask the CLI to exit cleanly,
                # but keep looping so the reader thread can drain stdout to EOF.
                process.terminate()
                saw_terminal = True

        # stdout fully drained by reader; communicate() only needs stderr.
        # Floor at 100ms: even if the deadline expired, give the process a
        # brief grace window to exit before we escalate to kill().
        wait_remaining = max(0.1, deadline - time.monotonic())
        try:
            _, stderr = process.communicate(timeout=wait_remaining)
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate()
            return _timeout_payload(cli, processor, timeout_ms)

        return build_final_response(
            cli,
            process.returncode,
            processor.get_result(),
            stdout_lines,
            stderr,
            terminated_by_us=saw_terminal,
        )
    except (OSError, ValueError) as e:
        # OSError covers I/O failures on the pipe; ValueError covers reading
        # from a closed file. Anything else propagates so it's not silently
        # swallowed.
        process.kill()
        return _error_response(
            cli, 1, f"{type(e).__name__}: {e}", partial_result=processor.get_result()
        )


def _cursor_config_dir(cwd: str | None = None) -> Path:
    """The directory cursor-agent writes its CLI config (cli-config.json) into.

    ``CURSOR_CONFIG_DIR`` is honoured (verified empirically: setting it
    redirects where cursor-agent writes cli-config.json). It reaches the child
    by ordinary ``os.environ`` inheritance — ``_build_cursor_args`` only
    explicitly forwards ``CURSOR_API_KEY`` — so this probe and the spawned CLI
    read the same value as long as ``execute_agent`` keeps merging os.environ
    into the child env rather than replacing it. A *relative* override is
    resolved against ``cwd`` (cursor-agent's working dir, where it resolves the
    same relative path) so the probe target matches the real write target.
    """
    override = os.environ.get("CURSOR_CONFIG_DIR")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute() and cwd:
            path = Path(cwd) / path
        return path
    return Path(os.path.expanduser("~")) / ".cursor"


def _probe_writable(path: Path) -> bool:
    """Return True only if ``path`` can actually be created and written to.

    Uses a real mkdir + temp-file write rather than ``os.access(W_OK)``: under a
    macOS seatbelt sandbox the denial is a MAC restriction, which ``access(2)``
    (DAC-only) does not see — it would falsely report the dir as writable. An
    actual write attempt is the only reliable probe for the sandboxed case this
    preflight exists to catch.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".preflight-"):
            pass
        return True
    except OSError:
        return False


def _preflight(inv: AgentInvocation) -> str | None:
    """Return an actionable error message if the environment can't run the CLI.

    Currently cursor-agent-specific: it refuses to start when its config dir is
    not writable, crashing late with a wall of repeated cert warnings followed
    by a buried ``EPERM ... cli-config.json.tmp``. Catch it up front instead.
    """
    if inv.cli != "cursor-agent":
        return None
    config_dir = _cursor_config_dir(inv.cwd)
    if _probe_writable(config_dir):
        return None
    return (
        f"cursor config dir not writable: {config_dir} — cursor-agent writes "
        "cli-config.json here and cannot start without write access. Allow this "
        "path in the sandbox, or set CURSOR_CONFIG_DIR to a writable location."
    )


def execute_agent(inv: AgentInvocation, timeout_ms: int = 600000) -> dict:
    """Execute agent CLI for the given invocation. Returns a response dict.

    Response shape: ``{result, exit_code, status, cli, error?, root_cause?,
    stderr?}``. ``root_cause`` / ``stderr`` are best-effort and only populated
    on the normal-exit error branch (``build_final_response``); preflight,
    timeout, stdout-cap and spawn-error paths carry just ``error``.
    """
    preflight_error = _preflight(inv)
    if preflight_error:
        return _error_response(inv.cli, _PREFLIGHT_EXIT_CODE, preflight_error)

    command, args, env_override = build_invocation_args(inv)
    proc_env = {**os.environ, **env_override} if env_override else None

    try:
        # stdin=DEVNULL: sub-agent CLIs (notably codex) probe stdin for "additional
        # input" and block reading from a TTY inherited from the parent. We never
        # have stdin to give them.
        process = subprocess.Popen(
            [command, *args],
            cwd=inv.cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Pin UTF-8 explicitly: text mode otherwise decodes with the locale
            # codepage (cp932/cp1252 on Windows), garbling the UTF-8 NDJSON the
            # CLIs emit. errors="replace" degrades malformed bytes gracefully.
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=proc_env,
        )
    except FileNotFoundError:
        return _error_response(inv.cli, 127, f"CLI not found: {command}")
    except OSError as e:
        return _error_response(inv.cli, 1, f"{type(e).__name__}: {e}")

    return _drive_process(process, inv.cli, timeout_ms)
