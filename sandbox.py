"""Running Python that JARVIS did not ship: evaluated code, learned skills.

Every execution is a separate `python -I -S` child in an empty temporary
directory with a scrubbed environment, a wall-clock timeout, CPU / memory /
file-size limits where the platform enforces them, and capped output. The
child is never this process: an infinite loop, a segfault or a `sys.exit`
in generated code costs one child and nothing else.

What this is NOT is a security boundary against hostile code. The child runs
as the user, can read their files and can open sockets — there is no portable
way to stop that without a dependency this project will not take. The gate
against hostile code is upstream of here: only the user's own turn can ask
for an execution, and a learned skill only runs once the user has approved it
(see learning.py). This module makes honest mistakes cheap; it does not make
malicious ones safe, and nothing should describe it as if it did.

`-I` isolates from the user's environment and site directory; `-S` drops
`site` altogether, so only the standard library is importable — a skill that
needs a package is a skill this system cannot evaluate, which is the point.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SEC = 10.0
MAX_TIMEOUT_SEC = 15.0          # inside the MCP child's 20s budget, always
OUTPUT_CAP = 8000
MEMORY_MB = 512
FILE_MB = 16

# The harness prints its verdict on a line of its own, behind this marker, so
# whatever the code under test prints cannot be mistaken for it.
_MARKER = "\x1e__JARVIS_SANDBOX__"


@dataclass
class SandboxResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool
    duration: float

    def to_dict(self) -> dict:
        return asdict(self)


def _cap(text: str) -> str:
    return text if len(text) <= OUTPUT_CAP else text[:OUTPUT_CAP] + "\n… (truncated)"


def _limit_child() -> None:   # pragma: no cover - runs in the child
    """POSIX resource limits. Each one is best-effort: macOS refuses
    RLIMIT_AS outright, and a limit that cannot be set is not a reason to
    refuse to run."""
    import resource
    for which, value in ((resource.RLIMIT_CPU, (int(MAX_TIMEOUT_SEC) + 1,) * 2),
                         (resource.RLIMIT_AS, (MEMORY_MB * 2 ** 20,) * 2),
                         (resource.RLIMIT_FSIZE, (FILE_MB * 2 ** 20,) * 2),
                         (resource.RLIMIT_CORE, (0, 0))):
        try:
            resource.setrlimit(which, value)
        except (ValueError, OSError):
            pass


def _clean_env(home: str) -> dict:
    """Nothing of the server's environment: no API keys, no tool token path,
    no proxy credentials. Only what an interpreter needs to start."""
    return {"PATH": "/usr/bin:/bin", "HOME": home, "TMPDIR": home,
            "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0"}


def run_python(code: str, *, timeout: float = DEFAULT_TIMEOUT_SEC,
               stdin: str = "", files: dict[str, str] | None = None,
               script: str | None = None) -> SandboxResult:
    """Execute `code` (or `script`, with `code` written beside it as
    `subject.py`) and report what happened. Blocking — call it through
    `asyncio.to_thread` from the event loop."""
    timeout = max(0.5, min(float(timeout), MAX_TIMEOUT_SEC))
    with tempfile.TemporaryDirectory(prefix="jarvis-sandbox-") as work:
        root = Path(work)
        (root / "subject.py").write_text(code)
        for name, content in (files or {}).items():
            (root / Path(name).name).write_text(content)
        entry = root / "main.py"
        entry.write_text(script if script is not None else code)
        started = time.monotonic()
        kwargs = dict(cwd=work, env=_clean_env(work), input=stdin,
                      capture_output=True, text=True, timeout=timeout,
                      start_new_session=True)
        if os.name == "posix":
            kwargs["preexec_fn"] = _limit_child
        try:
            proc = subprocess.run([sys.executable, "-I", "-S", str(entry)], **kwargs)
        except subprocess.TimeoutExpired as e:
            return SandboxResult(False, _cap(_text(e.stdout)), _cap(_text(e.stderr)),
                                 None, True, time.monotonic() - started)
        except OSError as e:
            return SandboxResult(False, "", f"could not start the interpreter: {e}",
                                 None, False, time.monotonic() - started)
        return SandboxResult(proc.returncode == 0, _cap(proc.stdout), _cap(proc.stderr),
                             proc.returncode, False, time.monotonic() - started)


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


# ---------------------------------------------------------------------------
# Skills: a module defining `run(**kwargs)`, plus test cases for it
# ---------------------------------------------------------------------------

_TEST_HARNESS = f"""
import json, traceback
spec = json.load(open("cases.json"))
ns = {{"__name__": "skill"}}
results = []
try:
    exec(compile(open("subject.py").read(), "skill.py", "exec"), ns)
    loaded = callable(ns.get("run"))
    load_error = "" if loaded else "the code defines no run() function"
except BaseException as e:
    loaded, load_error = False, "".join(traceback.format_exception_only(type(e), e)).strip()
for case in spec["tests"]:
    if not loaded:
        results.append({{"name": case.get("name", "?"), "ok": False, "error": load_error}})
        continue
    try:
        exec(compile(case["code"], "test_" + str(case.get("name", "case")), "exec"), dict(ns))
        results.append({{"name": case.get("name", "?"), "ok": True, "error": ""}})
    except BaseException as e:
        results.append({{"name": case.get("name", "?"), "ok": False,
                        "error": "".join(traceback.format_exception_only(type(e), e)).strip()[:400]}})
print({_MARKER!r} + json.dumps(results))
"""

_CALL_HARNESS = f"""
import json
ns = {{"__name__": "skill"}}
exec(compile(open("subject.py").read(), "skill.py", "exec"), ns)
kwargs = json.load(open("kwargs.json"))
value = ns["run"](**kwargs)
try:
    encoded = json.dumps(value)
except TypeError:
    encoded = json.dumps(repr(value))
print({_MARKER!r} + encoded)
"""


def _verdict(stdout: str):
    """The harness line, parsed, and the stdout with it removed."""
    head, sep, tail = stdout.rpartition(_MARKER)
    if not sep:
        return None, stdout
    line, _, _ = tail.partition("\n")
    try:
        return json.loads(line), head
    except ValueError:
        return None, stdout


@dataclass
class SuiteReport:
    passed: int
    total: int
    failures: list[dict]
    timed_out: bool
    stderr: str

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.passed == self.total and not self.timed_out


def run_tests(code: str, tests: list[dict], *,
              timeout: float = DEFAULT_TIMEOUT_SEC) -> SuiteReport:
    """Each test is `{"name": ..., "code": "assert run(x=1) == 2"}`, executed
    in a fresh copy of the skill's namespace."""
    tests = [t for t in tests if isinstance(t, dict) and isinstance(t.get("code"), str)]
    if not tests:
        return SuiteReport(0, 0, [{"name": "-", "error": "no tests"}], False, "")
    result = run_python(code, timeout=timeout, script=_TEST_HARNESS,
                        files={"cases.json": json.dumps({"tests": tests})})
    verdict, _ = _verdict(result.stdout)
    if not isinstance(verdict, list):
        error = "timed out" if result.timed_out else (result.stderr.strip()[-400:]
                                                      or "the harness did not report")
        return SuiteReport(0, len(tests), [{"name": t.get("name", "?"), "error": error}
                                          for t in tests], result.timed_out, result.stderr)
    failures = [r for r in verdict if not r.get("ok")]
    return SuiteReport(len(verdict) - len(failures), len(tests), failures,
                      result.timed_out, result.stderr)


@dataclass
class CallResult:
    ok: bool
    value: object
    output: str
    error: str
    timed_out: bool


def call_skill(code: str, kwargs: dict, *,
               timeout: float = DEFAULT_TIMEOUT_SEC) -> CallResult:
    """Call the skill's `run(**kwargs)` and return its JSON-able value."""
    result = run_python(code, timeout=timeout, script=_CALL_HARNESS,
                        files={"kwargs.json": json.dumps(kwargs)})
    verdict, printed = _verdict(result.stdout)
    if result.ok and result.stdout.count(_MARKER):
        return CallResult(True, verdict, printed.strip(), "", False)
    error = "timed out" if result.timed_out else (
        result.stderr.strip().splitlines()[-1] if result.stderr.strip() else
        f"exited {result.returncode}")
    return CallResult(False, None, printed.strip(), error, result.timed_out)
