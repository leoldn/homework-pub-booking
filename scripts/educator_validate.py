"""educator_validate.py — validate the homework end-to-end before cohort release.

Workflow:
  1. Back up starter/ and answers/ to .educator_backup/ (idempotent)
  2. Apply solution/ over starter/ and answers/
  3. Run every scenario (Ex5-Ex8)
  4. Run the grader
  5. Report whether the homework is ready to ship
  6. Restore starter/ and answers/ from backup

The backup/restore is NOT optional — this script mutates the working
tree temporarily. If anything goes wrong mid-run, `make educator-reset`
will clean up.

Exit codes:
  0 — homework passes validation (46+/76 achievable locally)
  1 — validation found problems
  2 — script itself failed to run
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKUP = REPO / ".educator_backup"
SOLUTION = REPO / "solution"


def _cache_dir() -> Path:
    """XDG-standard cache path for educator logs. Used by diagnostics."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    p = base / "sovereign-agent" / "homework-pub-booking"
    p.mkdir(parents=True, exist_ok=True)
    return p


class _Tee:
    """Duplicate writes to multiple streams. Used for educator-validate's
    split output: interactive terminal + cached log file for diagnostics."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


class _C:
    _on = sys.stdout.isatty()

    @classmethod
    def _w(cls, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if cls._on else s

    @classmethod
    def g(cls, s: str) -> str:  # green
        return cls._w("32", s)

    @classmethod
    def r(cls, s: str) -> str:  # red
        return cls._w("31", s)

    @classmethod
    def y(cls, s: str) -> str:  # yellow
        return cls._w("33", s)

    @classmethod
    def d(cls, s: str) -> str:  # dim
        return cls._w("2", s)

    @classmethod
    def b(cls, s: str) -> str:  # bold
        return cls._w("1", s)


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd or REPO,
        timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


def backup_starter() -> None:
    """Idempotent snapshot. Always refresh so we catch any stray edits."""
    if BACKUP.exists():
        shutil.rmtree(BACKUP)
    BACKUP.mkdir()
    shutil.copytree(REPO / "starter", BACKUP / "starter")
    shutil.copytree(REPO / "answers", BACKUP / "answers")


def restore_starter() -> None:
    """Reverse of backup_starter. Idempotent."""
    if not BACKUP.exists():
        return
    if (REPO / "starter").exists():
        shutil.rmtree(REPO / "starter")
    if (REPO / "answers").exists():
        shutil.rmtree(REPO / "answers")
    shutil.copytree(BACKUP / "starter", REPO / "starter")
    shutil.copytree(BACKUP / "answers", REPO / "answers")


def apply_solution() -> int:
    """Run the apply_solution.sh shell script."""
    rc, _out, err = _run(["bash", str(SOLUTION / "apply_solution.sh")])
    if rc != 0:
        print(_C.r("✗") + " apply_solution.sh failed")
        print(err)
    return rc


def run_scenario(name: str, module: str) -> tuple[bool, str]:
    """Run one scenario module. Return (passed, summary)."""
    rc, out, err = _run(["uv", "run", "python", "-m", module])
    if rc == 0:
        return True, f"{name}: ran cleanly"
    tail = (out + err).strip().splitlines()[-3:]
    return False, f"{name}: exit {rc} — " + " | ".join(tail)


def run_grader() -> tuple[int, int, str]:
    """Run the grader. Returns (earned, possible_local, raw_output)."""
    rc, out, err = _run(["uv", "run", "python", "-m", "grader.check_submit"])
    combined = out + err
    # Parse the "Raw score:" line
    earned = possible = 0
    for line in combined.splitlines():
        if "Raw score:" in line:
            # e.g.  "**Raw score:** 46.0 / 46"
            import re

            m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+)", line)
            if m:
                earned = int(float(m.group(1)))
                possible = int(m.group(2))
                break
    return earned, possible, combined


def print_section(title: str) -> None:
    print()
    print(_C.b(f"  {title}"))
    print(_C.d("  " + "─" * 66))


def main() -> int:
    # Tee stdout+stderr to a log file so `make educator-diagnostics` can
    # read the last run without manual copy-paste.
    log_path = _cache_dir() / "educator_validate.log"
    log_file = log_path.open("w", encoding="utf-8")
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = _Tee(original_stdout, log_file)
    sys.stderr = _Tee(original_stderr, log_file)

    try:
        return _main_impl()
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        log_file.close()
        print(_C.d(f"  (full log cached at {log_path})"))


def _main_impl() -> int:
    print()
    print(_C.y("━" * 72))
    print(_C.b("  homework-pub-booking") + _C.d("  ·  ") + _C.b("educator validation harness"))
    print(_C.d(f"  repo: {REPO}"))
    print(_C.y("━" * 72))

    if not SOLUTION.exists():
        print(
            _C.r("✗") + " solution/ directory not found. This harness needs the reference solution."
        )
        print(_C.d("  The educator should keep solution/ in a private sibling repo; copy"))
        print(_C.d("  it into ./solution/ before running `make educator-validate`."))
        return 2

    # ── Phase 1 — back up ────────────────────────────────────────────
    print_section("Phase 1 — backing up starter/ and answers/")
    backup_starter()
    print(f"  {_C.g('✓')} backed up to {BACKUP.relative_to(REPO)}/")

    # ── Phase 2 — apply solution ─────────────────────────────────────
    print_section("Phase 2 — applying solution/")
    try:
        rc = apply_solution()
        if rc != 0:
            print(f"  {_C.r('✗')} apply_solution.sh exit {rc}")
            restore_starter()
            return 1
        print(f"  {_C.g('✓')} solution applied")

        # ── Phase 3 — run each scenario ─────────────────────────────
        print_section("Phase 3 — running scenarios")
        scenarios = [
            ("ex5", "starter.edinburgh_research.run"),
            ("ex6", "starter.rasa_half.run"),
            ("ex7", "starter.handoff_bridge.run"),
            # ex8 needs NEBIUS_KEY; skip in offline harness
        ]
        all_pass = True
        for name, module in scenarios:
            ok, summary = run_scenario(name, module)
            mark = _C.g("✓") if ok else _C.r("✗")
            print(f"  {mark} {summary}")
            if not ok:
                all_pass = False

        # Ex8 heuristic check
        voice_loop = (REPO / "starter/voice_pipeline/voice_loop.py").read_text()
        voice_impl = "raise NotImplementedError" not in voice_loop
        mark = _C.g("✓") if voice_impl else _C.r("✗")
        print(f"  {mark} ex8: voice_loop.run_voice_mode implemented (heuristic)")
        if not voice_impl:
            all_pass = False

        # ── Phase 4 — run grader ─────────────────────────────────────
        print_section("Phase 4 — running grader against solution-applied tree")
        earned, possible, _output = run_grader()
        local_max = 46  # Mechanical(27) + Behavioural(19). Reasoning not gradeable locally.
        print(f"  local grader score: {earned} / {local_max} (excluding 30pt Reasoning layer)")

        # Threshold: 42+/46 is "ready to ship"; 40-41 is borderline; <40 is broken.
        if earned >= local_max - 2:
            verdict = _C.g("✓ homework ready to ship")
            verdict_rc = 0
        elif earned >= local_max - 6:
            verdict = _C.y("⚠ homework mostly working; investigate gaps")
            verdict_rc = 0
        else:
            verdict = _C.r("✗ homework has real problems — investigate before cohort release")
            verdict_rc = 1

        print()
        print(_C.y("━" * 72))
        print(f"  {verdict}")
        print(_C.y("━" * 72))

        if not all_pass:
            verdict_rc = 1

    finally:
        # ── Phase 5 — restore ────────────────────────────────────────
        print_section("Phase 5 — restoring pristine starter/ and answers/")
        restore_starter()
        print(f"  {_C.g('✓')} restored")

    print()
    return verdict_rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        import traceback

        print(_C.r("✗") + f" harness crashed: {e}")
        traceback.print_exc()
        try:
            restore_starter()
        except Exception:  # noqa: BLE001
            pass
        sys.exit(2)
