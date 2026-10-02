"""Exercise batch boundaries with local command stubs; never install packages."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows startup scripts")


@pytest.mark.parametrize("inspector_exit_code", [0, 7])
def test_mcp_inspector_uses_service_config_and_inherits_environment(
    tmp_path: Path, inspector_exit_code: int,
) -> None:
    project_root = tmp_path / "project directory"
    script_root = project_root / "user_scripts"
    script_root.mkdir(parents=True)
    service_root = project_root / "mcp_servers"
    service_root.mkdir()
    shutil.copyfile(ROOT / "user_scripts/inspect-mcp.cmd", script_root / "inspect-mcp.cmd")
    config_text = '{"mcpServers":{"fake":{"command":"python","cwd":"fake_mcp"}}}'
    (service_root / "mcp.json").write_text(config_text, encoding="utf-8")
    (service_root / "npx.cmd").write_text(
        '@echo off\necho %CD%\necho %*\necho %PYTHONUTF8%\necho %CONDA_PREFIX%\n'
        f'exit /b {inspector_exit_code}\n', encoding="ascii",
    )
    (tmp_path / "inspect-from-outside.cmd").write_text(
        f'@echo off\ncall "{script_root / "inspect-mcp.cmd"}"\n'
        'set "inspector_exit_code=%errorlevel%"\necho caller=%CD%\n'
        'exit /b %inspector_exit_code%\n', encoding="ascii",
    )
    result = run_script(tmp_path, {
        **os.environ,
        "CONDA_PREFIX": "selected-python-environment",
        "PATH": os.pathsep.join([str(service_root), os.environ["PATH"]]),
    }, "inspect-from-outside.cmd")
    assert result.returncode == inspector_exit_code, result.stdout + result.stderr
    assert str(service_root) in result.stdout
    assert "@modelcontextprotocol/inspector@latest" in result.stdout
    assert '--config "mcp.json"' in result.stdout
    assert "\n1\nselected-python-environment\n" in result.stdout
    assert f"caller={tmp_path}" in result.stdout
    assert (service_root / "mcp.json").read_text(encoding="utf-8") == config_text


def script_environment(root: Path, *, python_failure: str = "", npm_failure: str = "") -> dict[str, str]:
    script_root = root / "user_scripts"
    script_root.mkdir()
    for name in ("check-dependencies.cmd", "install.cmd"):
        shutil.copyfile(ROOT / "user_scripts" / name, script_root / name)
    (root / "python.cmd").write_text(
        '@echo off\necho cwd=%CD%>>"%~dp0calls.txt"\necho python %*>>"%~dp0calls.txt"\n'
        'if "%~1"=="--version" (\n'
        f'  exit /b {1 if python_failure == "environment" else 0}\n)\n'
        'if "%~1"=="-c" (\n'
        f'  exit /b {1 if python_failure == "dependencies" else 0}\n)\n'
        f'exit /b {1 if python_failure == "install" else 0}\n', encoding="ascii",
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


def run_script(root: Path, env: dict[str, str], script: str, *arguments: str,
               cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [os.environ["COMSPEC"], "/d", "/c", str(root / script), *arguments],
        cwd=cwd or root, env=env, capture_output=True, text=True, errors="replace", timeout=20,
    )


def test_basic_check_aggregates_failures_and_rejects_arguments(tmp_path: Path) -> None:
    env = script_environment(tmp_path, python_failure="dependencies", npm_failure="dependencies")
    result = run_script(tmp_path, env, "user_scripts/check-dependencies.cmd")
    assert result.returncode == 1, result.stdout
    assert "[backend] FAIL" in result.stdout and "[frontend] FAIL" in result.stdout
    calls = (tmp_path / "calls.txt").read_text()
    assert " install " not in calls and "pip check" not in calls and "--all" not in calls
    for arguments in [("unknown",), ("backend", "extra")]:
        result = run_script(tmp_path, env, "user_scripts/check-dependencies.cmd", *arguments)
        assert result.returncode == 2 and "Usage:" in result.stdout


@pytest.mark.parametrize("failure", ["environment", "dependencies"])
def test_backend_check_explains_repair(tmp_path: Path, failure: str) -> None:
    env = script_environment(tmp_path, python_failure=failure)
    result = run_script(tmp_path, env, "user_scripts/check-dependencies.cmd", "backend")
    assert result.returncode == 1, result.stdout
    assert ("repair your Python environment" if failure == "environment" else "Run user_scripts\\install.cmd") in result.stdout
    assert "[frontend]" not in result.stdout


def test_missing_tools_are_not_reported_as_missing_project_packages(tmp_path: Path) -> None:
    env = script_environment(tmp_path)
    (tmp_path / "python.cmd").unlink()
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    result = run_script(tmp_path, env, "user_scripts/check-dependencies.cmd")
    assert result.returncode == 1
    assert "Python is unavailable" in result.stdout and "Node.js is unavailable" in result.stdout
    assert "Run user_scripts\\install.cmd" not in result.stdout


@pytest.mark.parametrize("caller_directory", [".", "user_scripts", "other directory"])
def test_install_always_delegates_incremental_updates_then_checks(
    tmp_path: Path, caller_directory: str,
) -> None:
    env = script_environment(tmp_path)
    caller_path = tmp_path / caller_directory
    caller_path.mkdir(exist_ok=True)
    result = run_script(tmp_path, env, "user_scripts/install.cmd", cwd=caller_path)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "calls.txt").read_text().splitlines()
    installation = next(index for index, line in enumerate(calls) if "pip install" in line)
    check = next(index for index, line in enumerate(calls) if "import alembic" in line)
    assert installation < check
    assert f"cwd={tmp_path}" in calls
    assert all(line == f"cwd={tmp_path}" for line in calls if line.startswith("cwd="))
    assert any("install --include=dev" in line for line in calls)
    assert "start.cmd" in result.stdout


def test_install_failure_stops_before_frontend_and_preserves_stage(tmp_path: Path) -> None:
    env = script_environment(tmp_path, python_failure="install")
    result = run_script(tmp_path, env, "user_scripts/install.cmd")
    assert result.returncode == 1 and "Backend installation failed" in result.stdout
    assert "npm " not in (tmp_path / "calls.txt").read_text()


def test_frontend_install_failure_reports_its_stage(tmp_path: Path) -> None:
    env = script_environment(tmp_path, npm_failure="install")
    result = run_script(tmp_path, env, "user_scripts/install.cmd")
    assert result.returncode == 1 and "Frontend installation failed" in result.stdout
    assert "Start with:" not in result.stdout


def test_start_stops_after_failed_basic_check(tmp_path: Path) -> None:
    script = "start.cmd"
    shutil.copyfile(ROOT / script, tmp_path / script)
    (tmp_path / "user_scripts").mkdir()
    (tmp_path / "user_scripts/check-dependencies.cmd").write_text("@echo off\necho fixture check failed\nexit /b 1\n", encoding="ascii")
    result = subprocess.run(
        [os.environ["COMSPEC"], "/d", "/c", str(tmp_path / script)],
        capture_output=True, text=True, errors="replace", timeout=20,
    )
    assert result.returncode == 1
    assert "fixture check failed" in result.stdout and "[startup] FAIL" not in result.stdout


def test_start_uses_python_from_path_without_conda(tmp_path: Path) -> None:
    script = "start.cmd"
    env = script_environment(tmp_path)
    env.pop("CONDA_PREFIX", None)
    shutil.copyfile(ROOT / script, tmp_path / script)
    result = subprocess.run(
        [os.environ["COMSPEC"], "/d", "/c", str(tmp_path / script)],
        cwd=tmp_path, env=env, capture_output=True, text=True, errors="replace", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "calls.txt").read_text().splitlines()
    assert any("import alembic" in call for call in calls)
    assert any(call.startswith("python ") and "user_scripts\\start_app.py" in call and "--cmd-entry" in call for call in calls)


def test_cmd_start_passes_encoding_and_preserves_launcher_exit_code(tmp_path: Path) -> None:
    env = script_environment(tmp_path)
    shutil.copyfile(ROOT / "start.cmd", tmp_path / "start.cmd")
    (tmp_path / "user_scripts/check-dependencies.cmd").write_text("@echo off\nexit /b 0\n", encoding="ascii")
    (tmp_path / "python.cmd").write_text(
        '@echo off\necho encoding=%PYTHONUTF8%,%PYTHONIOENCODING%\nexit /b 7\n', encoding="ascii",
    )
    result = run_script(tmp_path, env, "start.cmd")
    assert result.returncode == 7
    assert "encoding=1,utf-8" in result.stdout
