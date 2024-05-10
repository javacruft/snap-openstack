# Copyright 2023 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from snaphelpers import Snap, SnapConfig, SnapServices


@pytest.fixture
def snap_env(tmp_path: Path, mocker):
    """Environment variables defined in the snap.

    This is primarily used to setup the snaphelpers bit.
    """
    snap_name = "sunbeam-test"
    real_home = tmp_path / "home/ubuntu"
    snap_user_common = real_home / f"snap/{snap_name}/common"
    snap_user_data = real_home / f"snap/{snap_name}/2"
    snap_path = tmp_path / f"snap/2/{snap_name}"
    snap_common = tmp_path / f"var/snap/{snap_name}/common"
    snap_data = tmp_path / f"var/snap/{snap_name}/2"
    env = {
        "SNAP": str(snap_path),
        "SNAP_COMMON": str(snap_common),
        "SNAP_DATA": str(snap_data),
        "SNAP_USER_COMMON": str(snap_user_common),
        "SNAP_USER_DATA": str(snap_user_data),
        "SNAP_REAL_HOME": str(real_home),
        "SNAP_INSTANCE_NAME": "",
        "SNAP_NAME": snap_name,
        "SNAP_REVISION": "2",
        "SNAP_VERSION": "1.2.3",
    }
    mocker.patch("os.environ", env)
    yield env


@pytest.fixture
def snap(snap_env):
    snap = Snap(environ=snap_env)
    snap.config = MagicMock(SnapConfig)
    snap.services = MagicMock(SnapServices)
    yield snap


@pytest.fixture
def run():
    with patch("subprocess.run") as p:
        yield p


@pytest.fixture
def check_call():
    with patch("subprocess.check_call") as p:
        yield p


@pytest.fixture
def check_output():
    with patch("subprocess.check_output") as p:
        yield p


@pytest.fixture
def environ():
    with patch("os.environ") as p:
        yield p


@pytest.fixture
def copytree():
    with patch("shutil.copytree") as p:
        yield p
