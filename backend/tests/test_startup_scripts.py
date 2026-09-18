"""Exercise batch boundaries with local command stubs; never install packages."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows startup scripts")


def script_environment(root: Path, *, conda_failure: str = "", npm_failure: str = "") -> dict[str, str]:
    for name in ("check-dependencies.cmd", "install.cmd"):
        shutil.copyfile(ROOT / name, root / name)
    (root / "conda.cmd").write_text(
        '@echo off\necho conda %*>>"%~dp0calls.txt"\n'
        'if "%~6"=="--version" (\n'
        f'  exit /b {1 if conda_failure == "environment" else 0}\n)\n'
        'if "%~6"=="-c" (\n'
        f'  exit /b {1 if conda_failure == "dependencies" else 0}\n)\n'
        f'exit /b {1 if conda_failure == "install" else 0}\n', encoding="ascii",
    )
    (root / "npm.cmd").write_text(
        '@echo off\necho npm %*>>"%~dp0calls.txt"\n'
        f'exit /b {1 if npm_failure else 0}\n', encoding="ascii",
    )
    vite = root / "frontend/node_modules/vite/bin/vite.js"
    vite.parent.mkdir(parents=True)
    vite.write_text('console.log("fixture vite");', encoding="ascii")
    node = shutil.which("node")
    assert node is not None
    return {**os.environ, "PATH": os.pathsep.join([
        str(root), str(Path(os.environ["SystemRoot"]) / "System32"), str(Path(node).parent),
    ])}


def run_script(root: Path, env: dict[str, str], script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [os.environ["COMSPEC"], "/d", "/c", str(root / script), *arguments],
        cwd=root, env=env, capture_output=True, text=True, errors="replace", timeout=20,
    )


def test_basic_check_aggregates_failures_and_rejects_arguments(tmp_path: Path) -> None:
    env = script_environment(tmp_path, conda_failure="dependencies", npm_failure="dependencies")
    result = run_script(tmp_path, env, "check-dependencies.cmd")
    assert result.returncode == 1, result.stdout
    assert "[backend] FAIL" in result.stdout and "[frontend] FAIL" in result.stdout
    calls = (tmp_path / "calls.txt").read_text()
    assert " install " not in calls and "pip check" not in calls and "--all" not in calls
    for arguments in [("unknown",), ("backend", "extra")]:
        result = run_script(tmp_path, env, "check-dependencies.cmd", *arguments)
        assert result.returncode == 2 and "Usage:" in result.stdout


@pytest.mark.parametrize("failure", ["environment", "dependencies"])
def test_backend_check_explains_repair(tmp_path: Path, failure: str) -> None:
    env = script_environment(tmp_path, conda_failure=failure)
    result = run_script(tmp_path, env, "check-dependencies.cmd", "backend")
    assert result.returncode == 1, result.stdout
    assert ("repair that Conda environment" if failure == "environment" else "Run install.cmd") in result.stdout
    assert "[frontend]" not in result.stdout


def test_missing_tools_are_not_reported_as_missing_project_packages(tmp_path: Path) -> None:
    env = script_environment(tmp_path)
    (tmp_path / "conda.cmd").unlink()
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    result = run_script(tmp_path, env, "check-dependencies.cmd")
    assert result.returncode == 1
    assert "Conda is unavailable" in result.stdout and "Node.js is unavailable" in result.stdout
    assert "Run install.cmd" not in result.stdout


def test_install_always_delegates_incremental_updates_then_checks(tmp_path: Path) -> None:
    env = script_environment(tmp_path)
    result = run_script(tmp_path, env, "install.cmd")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "calls.txt").read_text().splitlines()
    installation = next(index for index, line in enumerate(calls) if "pip install" in line)
    check = next(index for index, line in enumerate(calls) if "import alembic" in line)
    assert installation < check
    assert any("install --include=dev" in line for line in calls)
    assert "start.ps1" in result.stdout


def test_install_failure_stops_before_frontend_and_preserves_stage(tmp_path: Path) -> None:
    env = script_environment(tmp_path, conda_failure="install")
    result = run_script(tmp_path, env, "install.cmd")
    assert result.returncode == 1 and "Backend installation failed" in result.stdout
    assert "npm " not in (tmp_path / "calls.txt").read_text()


def test_frontend_install_failure_reports_its_stage(tmp_path: Path) -> None:
    env = script_environment(tmp_path, npm_failure="install")
    result = run_script(tmp_path, env, "install.cmd")
    assert result.returncode == 1 and "Frontend installation failed" in result.stdout
    assert "start.ps1" not in result.stdout


def test_start_stops_after_failed_basic_check(tmp_path: Path) -> None:
    shutil.copyfile(ROOT / "start.ps1", tmp_path / "start.ps1")
    (tmp_path / "check-dependencies.cmd").write_text("@echo off\necho fixture check failed\nexit /b 1\n", encoding="ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tmp_path / "start.ps1")],
        capture_output=True, text=True, errors="replace", timeout=20,
    )
    assert result.returncode == 1
    assert "fixture check failed" in result.stdout and "[startup] FAIL" not in result.stdout
