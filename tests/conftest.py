# -- AI GENERATED CODE (Qwen3.8 Flash Next) :: (2026-10-26) harness:openlumara status:unreviewed
#
# pi test suite for the OpenLumara pi backend module
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
make the module's tests runnable straight from an OpenLumara checkout:

    cd <openlumara> && pytest user_modules/pi/tests

the module changes nothing outside its own folder, so the repo's shared
test harness (tests/harness.py) and its fixtures - above all the autouse
isolate_lumara_paths fixture in tests/conftest.py that redirects data/,
config.yml and the module schema cache into tmp_path - are loaded by path
under a private module name and re-exported here (conftest fixtures only
apply under their own directory, so they must be re-exported, and they are
referenced, never copied).

when the module is installed somewhere without the repo's tests/ folder,
its tests opt out of collection instead of erroring the whole run.
"""

import importlib.util
import pathlib
import sys

import pytest

PI_DIR = pathlib.Path(__file__).resolve().parent.parent
REPO_ROOT = PI_DIR.parent.parent
SHARED_TESTS = REPO_ROOT / "tests"

collect_ignore_glob = []


def pytest_configure(config):
    """refuse to run when pytest was started inside the module: then the
    repo's tests/conftest.py is never loaded by pytest itself, and these
    tests would run against a live install"""
    started = pathlib.Path(str(config.rootdir)).resolve()
    if started != REPO_ROOT and REPO_ROOT not in started.parents:
        pytest.exit(
            "run the pi module's tests from the OpenLumara repo root "
            "(pytest user_modules/pi/tests) so the shared setup applies",
            returncode=1,
        )


def _load_shared_conftest():
    spec = importlib.util.spec_from_file_location(
        "_openlumara_shared_conftest", SHARED_TESTS / "conftest.py"
    )
    shared = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = shared
    spec.loader.exec_module(shared)
    return shared


for _path in (str(REPO_ROOT), str(SHARED_TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

if (SHARED_TESTS / "conftest.py").is_file():
    try:
        _shared = _load_shared_conftest()
        # re-exported fixtures: isolate_lumara_paths is autouse and keeps the
        # tests away from a live install's data/ and config
        isolate_lumara_paths = _shared.isolate_lumara_paths
        repo_snapshot = _shared.repo_snapshot
        fake_api = _shared.fake_api
        fake_manager = _shared.fake_manager
        fake_channel = _shared.fake_channel
    except Exception:
        collect_ignore_glob = ["test_*.py"]
else:
    collect_ignore_glob = ["test_*.py"]
