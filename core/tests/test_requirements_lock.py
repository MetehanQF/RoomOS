import importlib.util
from pathlib import Path

import pytest

CORE_DIR = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_lock", CORE_DIR / "tools" / "check_lock.py")
check_lock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_lock)

PYPROJECT = """
[project]
dependencies = ["fastapi>=0.115,<1", "uvicorn[standard]>=0.34,<1"]
"""
HASH = "--hash=sha256:" + "a" * 64


def test_committed_lock_matches_pyproject():
    errors = check_lock.check_static(check_lock.LOCK_PATH.read_text(), check_lock.PYPROJECT_PATH.read_text())
    assert errors == []


def test_dockerfile_installs_only_from_lock():
    dockerfile = (CORE_DIR / "Dockerfile").read_text()
    assert "--require-hashes" in dockerfile
    assert "--no-index" in dockerfile
    assert "check_lock.py --installed" in dockerfile


def test_lock_out_of_range_is_reported():
    lock = f"fastapi==1.0.0 {HASH}\nuvicorn==0.54.0 {HASH}\n"
    assert check_lock.check_static(lock, PYPROJECT) == [
        "fastapi==1.0.0 violates pyproject.toml range '<1,>=0.115'"
    ]


def test_missing_declared_dependency_is_reported():
    lock = f"fastapi==0.142.4 {HASH}\n"
    assert check_lock.check_static(lock, PYPROJECT) == [
        "uvicorn is declared in pyproject.toml but missing from the lock"
    ]


@pytest.mark.parametrize(
    "lock",
    [
        "fastapi>=0.142\n",
        "fastapi==0.142.4\n",
        f"fastapi==0.142.4 --hash=md5:{'a' * 32}\n",
        f"uvicorn==0.54.0 {HASH}\nfastapi==0.142.4 {HASH}\n",
        f"fastapi==0.142.4 {HASH}\nFastAPI==0.142.4 {HASH}\n",
        f"pip==25.0.1 {HASH}\n",
        "# comments only\n",
    ],
)
def test_malformed_lock_is_rejected(lock):
    with pytest.raises(ValueError):
        check_lock.parse_lock(lock)


def test_multiline_hashes_parse():
    lock = f"# header\nfastapi==0.142.4 \\\n    {HASH} \\\n    --hash=sha256:{'b' * 64}\n"
    assert check_lock.parse_lock(lock) == {"fastapi": "0.142.4"}
