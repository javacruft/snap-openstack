# Copyright 2024 Canonical Ltd.
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

import click
import pytest

from sunbeam.plugins.validation import plugin as validation_plugin


class TestValidatorFunction:
    """Test validator functions."""

    @pytest.mark.parametrize(
        "input_schedule",
        [
            "",
            "5 4 * * *",
            "5 4 * * mon",
            "*/30 * * * *",
        ],
    )
    def test_valid_cron_expressions(self, input_schedule):
        """Verify valid cron expressions."""
        config = validation_plugin.Config(schedule=input_schedule)
        assert config.schedule == input_schedule

    @pytest.mark.parametrize(
        "test_input,expected_msg",
        [
            ("*/5 * * * *", "Cannot schedule periodic check"),
            ("*/30 * * * * 6", "This cron does not support"),
            ("*/30 * *", "Exactly 5 columns must"),
            ("*/5 * * * xyz", "not acceptable"),
        ],
    )
    def test_invalid_cron_expressions(self, test_input, expected_msg):
        """Verify invalid cron expressions."""
        with pytest.raises(click.ClickException) as e:
            validation_plugin.Config(schedule=test_input)
            assert expected_msg in str(e)

    @pytest.mark.parametrize(
        "test_args",
        [
            ["option_a 1"],
            ["option_b=1", "option_c 2"],
        ],
    )
    def test_parse_config_args_syntax_error(self, test_args):
        """Test if parse_config_args handles syntax error."""
        with pytest.raises(click.ClickException):
            validation_plugin.parse_config_args(test_args)

    @pytest.mark.parametrize(
        "test_args",
        [
            (["option_a=1", "option_a=2", "option_b=3"]),
        ],
    )
    def test_parse_config_args_duplicated_params(self, test_args):
        """Test if parse_config_args handles duplicated parameters."""
        with pytest.raises(click.ClickException):
            validation_plugin.parse_config_args(test_args)

    @pytest.mark.parametrize(
        "test_args,expected_output",
        [
            (["option_a=1"], {"option_a": "1"}),
            (["option_b = 2"], {"option_b ": " 2"}),
            (["option_a=1", "option_b = 2"], {"option_a": "1", "option_b ": " 2"}),
        ],
    )
    def test_valid_parse_config_args(self, test_args, expected_output):
        """Test if parse_config_args handles duplicated parameters."""
        output = validation_plugin.parse_config_args(test_args)
        assert set(output.keys()) == set(expected_output.keys())
        for k, v in output.items():
            assert expected_output[k] == v

    @pytest.mark.parametrize(
        "input_args",
        [
            {"schedule": ""},
            {"schedule": "5 4 * * *"},
            {"schedule": "5 4 * * mon"},
            {"schedule": "*/30 * * * *"},
        ],
    )
    def test_valid_schedule_validated_config_args(self, input_args):
        """Test validated_config_args handles valid key correctly."""
        config = validation_plugin.validated_config_args(input_args)
        assert config.schedule == input_args["schedule"]

    @pytest.mark.parametrize(
        "input_args",
        [
            {"schedule": "*/5 * * * *"},
            {"schedule": "*/30 * * * * 6"},
            {"schedule": "*/30 * *"},
            {"schedule": "*/5 * * * xyz"},
        ],
    )
    def test_invalid_schedule_validated_config_args(self, input_args):
        """Test validated_config_args handles valid key but invalid value correctly."""
        # This is raise by `validated_schedule`
        with pytest.raises(click.ClickException):
            validation_plugin.validated_config_args(input_args)

    @pytest.mark.parametrize(
        "input_args",
        [
            {"schedules": "*/5 * * * *"},  # e.g. typo
            {"scehdule": "*/30 * * * * 6"},  # e.g. typo
        ],
    )
    def test_invalid_key_validated_config_args(self, input_args):
        """Test validated_config_args handles invalid key correctly."""
        # This is raise by `validated_config_args`
        with pytest.raises(click.ClickException):
            validation_plugin.validated_config_args(input_args)
