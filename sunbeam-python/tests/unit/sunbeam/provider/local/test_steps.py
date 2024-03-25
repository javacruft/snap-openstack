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

import asyncio
import io
from unittest.mock import AsyncMock, Mock, patch

import pytest
from rich.console import Console

import sunbeam.jobs.questions
import sunbeam.provider.local.steps as local_steps
import sunbeam.utils


@pytest.fixture(autouse=True)
def mock_run_sync(mocker):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()

    def run_sync(coro):
        return loop.run_until_complete(coro)

    mocker.patch("sunbeam.commands.configure.run_sync", run_sync)
    yield
    loop.close()


@pytest.fixture()
def cclient():
    yield Mock()


@pytest.fixture()
def load_answers():
    with patch.object(sunbeam.jobs.questions, "load_answers") as p:
        yield p


@pytest.fixture()
def write_answers():
    with patch.object(sunbeam.jobs.questions, "write_answers") as p:
        yield p


@pytest.fixture()
def question_bank():
    with patch.object(sunbeam.jobs.questions, "QuestionBank") as p:
        yield p


@pytest.fixture()
def jhelper():
    yield AsyncMock()


@pytest.fixture()
def get_nic_macs():
    with patch.object(sunbeam.utils, "get_nic_macs") as p:
        p.return_value = ["00:16:3e:01:6e:75"]
        yield p


@pytest.fixture()
def get_free_nics():
    with patch.object(sunbeam.utils, "get_free_nics") as p:
        p.return_value = ["eth1", "eth2"]
        yield p


@pytest.fixture()
def is_nic_up():
    with patch.object(sunbeam.utils, "is_nic_up") as p:
        p.side_effect = lambda x: {"eth1": True, "eth2": True}[x]
        yield p


@pytest.fixture()
def is_configured():
    with patch.object(sunbeam.utils, "is_configured") as p:
        p.side_effect = lambda x: {"eth1": False, "eth2": False}[x]
        yield p


@pytest.fixture()
def is_nic_connected():
    with patch.object(sunbeam.utils, "is_nic_connected") as p:
        p.side_effect = lambda x: {"eth1": True, "eth2": True}[x]
        yield p


class TestLocalSetHypervisorUnitsOptionsStep:
    def test_has_prompts(self, cclient, jhelper):
        step = local_steps.LocalSetHypervisorUnitsOptionsStep(
            cclient, "maas0.local", jhelper, "test-model"
        )
        assert step.has_prompts()

    def test_prompt_remote(
        self,
        cclient,
        jhelper,
        load_answers,
        question_bank,
        get_nic_macs,
        get_free_nics,
        is_configured,
        is_nic_up,
        is_nic_connected,
    ):
        get_free_nics.return_value = ["eth2"]
        load_answers.return_value = {"user": {"remote_access_location": "remote"}}
        local_hypervisor_bank_mock = Mock()
        question_bank.return_value = local_hypervisor_bank_mock
        local_hypervisor_bank_mock.nic.ask.return_value = "eth2"
        step = local_steps.LocalSetHypervisorUnitsOptionsStep(
            cclient, "maas0.local", jhelper, "test-model"
        )
        step.prompt()
        assert step.nics["maas0.local"] == "eth2"

    def test_prompt_remote_join(
        self,
        cclient,
        jhelper,
        load_answers,
        question_bank,
        get_nic_macs,
        get_free_nics,
        is_configured,
        is_nic_up,
        is_nic_connected,
    ):
        load_answers.return_value = {"user": {"remote_access_location": "remote"}}
        local_hypervisor_bank_mock = Mock()
        question_bank.return_value = local_hypervisor_bank_mock
        local_hypervisor_bank_mock.nic.ask.return_value = "eth2"
        step = local_steps.LocalSetHypervisorUnitsOptionsStep(
            cclient, "maas0.local", jhelper, "test-model", join_mode=True
        )
        step.prompt()
        assert step.nics["maas0.local"] == "eth2"

    def test_prompt_local(self, cclient, jhelper, load_answers, question_bank):
        load_answers.return_value = {"user": {"remote_access_location": "local"}}
        local_hypervisor_bank_mock = Mock()
        question_bank.return_value = local_hypervisor_bank_mock
        local_hypervisor_bank_mock.nic.ask.return_value = "eth12"
        step = local_steps.LocalSetHypervisorUnitsOptionsStep(
            cclient, "maas0.local", jhelper, "tes-model"
        )
        step.prompt()
        assert len(step.nics) == 0

    def test_prompt_local_join(
        self,
        cclient,
        jhelper,
        load_answers,
        question_bank,
        get_nic_macs,
        get_free_nics,
        is_configured,
        is_nic_up,
        is_nic_connected,
    ):
        load_answers.return_value = {"user": {"remote_access_location": "local"}}
        local_hypervisor_bank_mock = Mock()
        question_bank.return_value = local_hypervisor_bank_mock
        local_hypervisor_bank_mock.nic.ask.return_value = "eth2"
        step = local_steps.LocalSetHypervisorUnitsOptionsStep(
            cclient, "maas0.local", jhelper, "test-model", join_mode=True
        )
        step.prompt()
        assert step.nics["maas0.local"] == "eth2"


class TestNicPrompt:
    short_question = "Short Question [eth1/eth2] (eth1): "

    def test_good_choice(
        self, get_free_nics, is_nic_up, is_configured, is_nic_connected
    ):
        console = Console(file=io.StringIO())
        INPUT = "eth1\n"
        name = local_steps.NicPrompt.ask(
            "Short Question",
            console=console,
            stream=io.StringIO(INPUT),
        )
        assert name == "eth1"
        output = console.file.getvalue()
        assert output == self.short_question

    def test_good_choice_default(
        self, get_free_nics, is_nic_up, is_configured, is_nic_connected
    ):
        console = Console(file=io.StringIO())
        INPUT = ""
        name = local_steps.NicPrompt.ask(
            "Short Question",
            default="eth2",
            console=console,
            stream=io.StringIO(INPUT),
        )
        assert name == "eth2"
        output = console.file.getvalue()
        expected = "Short Question [eth1/eth2] (eth2): "
        assert output == expected

    def test_default_missing_from_machine(
        self, get_free_nics, is_nic_up, is_configured, is_nic_connected
    ):
        console = Console(file=io.StringIO())
        INPUT = ""
        name = local_steps.NicPrompt.ask(
            "Short Question",
            default="eth3",
            console=console,
            stream=io.StringIO(INPUT),
        )
        # The default eth3 does not exist so it was discarded.
        assert name == "eth1"
        output = console.file.getvalue()
        assert output == self.short_question
