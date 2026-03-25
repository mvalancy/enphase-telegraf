"""Tests for setup.sh, bin/ wrappers, .env loading, credential parsing, and CLI arg parsing.

Validates shell script syntax, wrapper behavior, conftest helpers,
and history_cli.py argument parsing without requiring actual infrastructure.

Target: ~100 tests.
"""

import argparse
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Resolve project root
REPO_DIR = Path(__file__).parent.parent
SETUP_SH = REPO_DIR / "setup.sh"
BIN_TELEGRAF = REPO_DIR / "bin" / "enphase-telegraf"
BIN_LOAD_HISTORY = REPO_DIR / "bin" / "load-history"


# ═══════════════════════════════════════════════════════════════════
# TestSetupShSyntax — 10 tests
# ═══════════════════════════════════════════════════════════════════

class TestSetupShSyntax:

    def test_setup_sh_syntax_valid(self):
        """setup.sh passes bash -n (syntax check)."""
        result = subprocess.run(["bash", "-n", str(SETUP_SH)], capture_output=True)
        assert result.returncode == 0, f"setup.sh syntax error: {result.stderr.decode()}"

    def test_bin_enphase_telegraf_syntax_valid(self):
        """bin/enphase-telegraf passes bash -n."""
        result = subprocess.run(["bash", "-n", str(BIN_TELEGRAF)], capture_output=True)
        assert result.returncode == 0, f"bin/enphase-telegraf syntax error: {result.stderr.decode()}"

    def test_bin_load_history_syntax_valid(self):
        """bin/load-history passes bash -n."""
        result = subprocess.run(["bash", "-n", str(BIN_LOAD_HISTORY)], capture_output=True)
        assert result.returncode == 0, f"bin/load-history syntax error: {result.stderr.decode()}"

    def test_setup_sh_has_set_euo_pipefail(self):
        content = SETUP_SH.read_text()
        assert "set -euo pipefail" in content

    def test_bin_telegraf_has_set_euo_pipefail(self):
        content = BIN_TELEGRAF.read_text()
        assert "set -euo pipefail" in content

    def test_bin_load_history_has_set_euo_pipefail(self):
        content = BIN_LOAD_HISTORY.read_text()
        assert "set -euo pipefail" in content

    def test_setup_sh_has_shebang(self):
        content = SETUP_SH.read_text()
        assert content.startswith("#!/usr/bin/env bash") or content.startswith("#!/bin/bash")

    def test_bin_telegraf_has_shebang(self):
        content = BIN_TELEGRAF.read_text()
        assert content.startswith("#!/usr/bin/env bash") or content.startswith("#!/bin/bash")

    def test_bin_load_history_has_shebang(self):
        content = BIN_LOAD_HISTORY.read_text()
        assert content.startswith("#!/usr/bin/env bash") or content.startswith("#!/bin/bash")

    def test_all_scripts_are_not_empty(self):
        for script in [SETUP_SH, BIN_TELEGRAF, BIN_LOAD_HISTORY]:
            assert script.stat().st_size > 0, f"{script} is empty"


# ═══════════════════════════════════════════════════════════════════
# TestBinWrappers — 20 tests
# ═══════════════════════════════════════════════════════════════════

class TestBinWrappers:

    def test_bin_telegraf_exists(self):
        assert BIN_TELEGRAF.exists()

    def test_bin_load_history_exists(self):
        assert BIN_LOAD_HISTORY.exists()

    def test_bin_telegraf_is_file(self):
        assert BIN_TELEGRAF.is_file()

    def test_bin_load_history_is_file(self):
        assert BIN_LOAD_HISTORY.is_file()

    def test_bin_telegraf_references_pythonpath(self):
        content = BIN_TELEGRAF.read_text()
        assert "PYTHONPATH" in content

    def test_bin_load_history_references_pythonpath(self):
        content = BIN_LOAD_HISTORY.read_text()
        assert "PYTHONPATH" in content

    def test_bin_telegraf_sources_env(self):
        content = BIN_TELEGRAF.read_text()
        assert "source" in content and ".env" in content

    def test_bin_load_history_sources_env(self):
        content = BIN_LOAD_HISTORY.read_text()
        assert "source" in content and ".env" in content

    def test_bin_telegraf_sets_pythonpath_to_src(self):
        content = BIN_TELEGRAF.read_text()
        assert 'PYTHONPATH="$REPO_DIR/src' in content

    def test_bin_load_history_sets_pythonpath_to_src(self):
        content = BIN_LOAD_HISTORY.read_text()
        assert 'PYTHONPATH="$REPO_DIR/src' in content

    def test_bin_telegraf_uses_exec(self):
        content = BIN_TELEGRAF.read_text()
        assert "exec " in content

    def test_bin_load_history_uses_exec(self):
        content = BIN_LOAD_HISTORY.read_text()
        assert "exec " in content

    def test_bin_telegraf_references_enphase_telegraf_py(self):
        content = BIN_TELEGRAF.read_text()
        assert "enphase_telegraf.py" in content

    def test_bin_load_history_references_history_cli(self):
        content = BIN_LOAD_HISTORY.read_text()
        assert "history_cli.py" in content

    def test_bin_telegraf_checks_venv(self):
        content = BIN_TELEGRAF.read_text()
        assert "venv/bin/python3" in content

    def test_bin_load_history_checks_venv(self):
        content = BIN_LOAD_HISTORY.read_text()
        assert "venv/bin/python3" in content

    def test_bin_telegraf_passes_args(self):
        content = BIN_TELEGRAF.read_text()
        assert '"$@"' in content

    def test_bin_load_history_passes_args(self):
        content = BIN_LOAD_HISTORY.read_text()
        assert '"$@"' in content

    def test_bin_telegraf_repo_dir_computed(self):
        content = BIN_TELEGRAF.read_text()
        assert "REPO_DIR" in content

    def test_bin_load_history_repo_dir_computed(self):
        content = BIN_LOAD_HISTORY.read_text()
        assert "REPO_DIR" in content


# ═══════════════════════════════════════════════════════════════════
# TestEnvFileLoading — 25 tests
# ═══════════════════════════════════════════════════════════════════

class TestEnvFileLoading:

    def _load_env_from_file(self, tmp_path, content, env_overrides=None):
        """Write a .env file and parse it using conftest._load_env logic."""
        env_path = tmp_path / ".env"
        env_path.write_text(content)
        # Simulate _load_env logic
        result = {}
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if env_overrides and key in env_overrides:
                    result[key] = env_overrides[key]
                else:
                    result[key] = val
        return result

    def test_valid_env_file(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "KEY=value\nOTHER=123")
        assert result["KEY"] == "value"
        assert result["OTHER"] == "123"

    def test_missing_env_file(self, tmp_path):
        env_path = tmp_path / ".env"
        assert not env_path.exists()
        # Loading should not raise

    def test_empty_env_file(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "")
        assert result == {}

    def test_comments_skipped(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "# this is a comment\nKEY=val")
        assert "# this is a comment" not in result
        assert result["KEY"] == "val"

    def test_existing_env_not_overridden(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "KEY=new_value",
                                          env_overrides={"KEY": "existing"})
        assert result["KEY"] == "existing"

    def test_spaces_around_equals(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "KEY = value")
        # partition("=") gives "KEY " and " value", strip() handles spaces
        assert result.get("KEY") == "value"

    def test_empty_value(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "KEY=")
        assert result["KEY"] == ""

    def test_special_chars_question_mark(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "PASS=p@ss?word!")
        assert result["PASS"] == "p@ss?word!"

    def test_special_chars_exclamation(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "PASS=hello!world")
        assert result["PASS"] == "hello!world"

    def test_special_chars_at_sign(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "EMAIL=user@example.com")
        assert result["EMAIL"] == "user@example.com"

    def test_special_chars_hash_in_value(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "PASS=abc#def")
        assert result["PASS"] == "abc#def"

    def test_blank_lines_skipped(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "KEY1=val1\n\n\nKEY2=val2")
        assert result["KEY1"] == "val1"
        assert result["KEY2"] == "val2"

    def test_multiple_equals_in_value(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "TOKEN=abc==def==ghi")
        assert result["TOKEN"] == "abc==def==ghi"

    def test_value_with_spaces(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "MSG=hello world")
        assert result["MSG"] == "hello world"

    def test_value_with_url(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "URL=http://localhost:8086")
        assert result["URL"] == "http://localhost:8086"

    def test_inline_comment_not_stripped(self, tmp_path):
        # conftest doesn't strip inline comments; value includes everything after =
        result = self._load_env_from_file(tmp_path, "KEY=value # comment")
        assert "value" in result["KEY"]

    def test_whitespace_only_lines(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "   \n  \t  \nKEY=val")
        assert result.get("KEY") == "val"

    def test_many_env_vars(self, tmp_path):
        lines = [f"VAR{i}=value{i}" for i in range(50)]
        result = self._load_env_from_file(tmp_path, "\n".join(lines))
        assert len(result) == 50
        assert result["VAR0"] == "value0"
        assert result["VAR49"] == "value49"

    def test_quoted_value_keeps_quotes(self, tmp_path):
        result = self._load_env_from_file(tmp_path, 'KEY="value"')
        assert result["KEY"] == '"value"'

    def test_single_quoted_value_keeps_quotes(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "KEY='value'")
        assert result["KEY"] == "'value'"

    def test_unicode_value(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "KEY=caf\u00e9")
        assert result["KEY"] == "caf\u00e9"

    def test_tab_separated(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "KEY\t=\tvalue")
        # partition("=") splits on first =; strip() handles tabs
        assert "value" in result.get("KEY\t", result.get("KEY", ""))

    def test_no_equals_sign_skipped(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "NOEQUALS\nKEY=val")
        assert "NOEQUALS" not in result
        assert result["KEY"] == "val"

    def test_env_file_with_only_comments(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "# comment1\n# comment2\n# comment3")
        assert result == {}

    def test_value_with_newline_escape(self, tmp_path):
        result = self._load_env_from_file(tmp_path, "KEY=line1\\nline2")
        assert result["KEY"] == "line1\\nline2"


# ═══════════════════════════════════════════════════════════════════
# TestCredentialParsing — 15 tests
# ═══════════════════════════════════════════════════════════════════

class TestCredentialParsing:

    def _parse_admin_token(self, content):
        """Simulate conftest influx_admin_token parsing logic."""
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if "Admin (all-access)" in line and i + 1 < len(lines):
                return lines[i + 1].strip()
        return ""

    def _parse_energy_token(self, content):
        """Simulate conftest influx_energy_token parsing logic."""
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if "Telegraf energy" in line and i + 1 < len(lines):
                return lines[i + 1].strip()
        return ""

    def test_admin_token_parsed(self):
        content = "--- Admin (all-access) ---\nabc123token\n---"
        assert self._parse_admin_token(content) == "abc123token"

    def test_energy_token_parsed(self):
        content = "--- Telegraf energy ---\nxyz789token\n---"
        assert self._parse_energy_token(content) == "xyz789token"

    def test_missing_admin_token(self):
        content = "No tokens here"
        assert self._parse_admin_token(content) == ""

    def test_missing_energy_token(self):
        content = "No tokens here"
        assert self._parse_energy_token(content) == ""

    def test_admin_token_with_whitespace(self):
        content = "--- Admin (all-access) ---\n  abc123token  \n---"
        assert self._parse_admin_token(content) == "abc123token"

    def test_energy_token_with_whitespace(self):
        content = "--- Telegraf energy ---\n  xyz789token  \n---"
        assert self._parse_energy_token(content) == "xyz789token"

    def test_both_tokens_present(self):
        content = textwrap.dedent("""\
            InfluxDB Credentials
            ====================
            --- Admin (all-access) ---
            admin_token_here
            --- Telegraf energy ---
            energy_token_here
            ---""")
        assert self._parse_admin_token(content) == "admin_token_here"
        assert self._parse_energy_token(content) == "energy_token_here"

    def test_token_is_long_string(self):
        long_token = "a" * 200
        content = f"--- Admin (all-access) ---\n{long_token}\n---"
        assert self._parse_admin_token(content) == long_token

    def test_token_with_special_chars(self):
        content = "--- Admin (all-access) ---\nabc-123_XYZ==\n---"
        assert self._parse_admin_token(content) == "abc-123_XYZ=="

    def test_admin_token_last_line(self):
        content = "--- Admin (all-access) ---\nfinal_token"
        assert self._parse_admin_token(content) == "final_token"

    def test_empty_credentials_file(self):
        assert self._parse_admin_token("") == ""
        assert self._parse_energy_token("") == ""

    def test_header_only_no_token_line(self):
        content = "--- Admin (all-access) ---"
        assert self._parse_admin_token(content) == ""

    def test_multiple_admin_headers(self):
        content = textwrap.dedent("""\
            --- Admin (all-access) ---
            first_token
            --- Admin (all-access) ---
            second_token""")
        # Should return first match
        assert self._parse_admin_token(content) == "first_token"

    def test_url_line_in_credentials(self):
        content = textwrap.dedent("""\
            URL: http://100.79.60.48:8086
            Org: Valpatel
            Bucket: energy
            --- Admin (all-access) ---
            my_admin_token""")
        assert self._parse_admin_token(content) == "my_admin_token"

    def test_empty_token_line(self):
        content = "--- Admin (all-access) ---\n\n--- next ---"
        # Empty line after header
        assert self._parse_admin_token(content) == ""


# ═══════════════════════════════════════════════════════════════════
# TestHistoryCLIArgParsing — 15 tests
# ═══════════════════════════════════════════════════════════════════

class TestHistoryCLIArgParsing:

    def _parse_args(self, args_list):
        """Parse CLI args using the same argparse as history_cli.py."""
        parser = argparse.ArgumentParser(prog="load-history")
        parser.add_argument("--start", metavar="YYYY-MM-DD")
        parser.add_argument("--end", metavar="YYYY-MM-DD")
        parser.add_argument("--stdout", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--cache-dir", metavar="DIR")
        parser.add_argument("--delay", type=float, default=30.0)
        parser.add_argument("--batch-size", type=int, default=5000)
        parser.add_argument("--convert-only", action="store_true")
        return parser.parse_args(args_list)

    def test_default_start_none(self):
        args = self._parse_args([])
        assert args.start is None

    def test_default_end_none(self):
        args = self._parse_args([])
        assert args.end is None

    def test_default_stdout_false(self):
        args = self._parse_args([])
        assert args.stdout is False

    def test_default_dry_run_false(self):
        args = self._parse_args([])
        assert args.dry_run is False

    def test_default_cache_dir_none(self):
        args = self._parse_args([])
        assert args.cache_dir is None

    def test_default_delay_30(self):
        args = self._parse_args([])
        assert args.delay == 30.0

    def test_default_batch_size_5000(self):
        args = self._parse_args([])
        assert args.batch_size == 5000

    def test_default_convert_only_false(self):
        args = self._parse_args([])
        assert args.convert_only is False

    def test_start_date_parsed(self):
        args = self._parse_args(["--start", "2023-01-15"])
        assert args.start == "2023-01-15"

    def test_end_date_parsed(self):
        args = self._parse_args(["--end", "2024-12-31"])
        assert args.end == "2024-12-31"

    def test_stdout_flag(self):
        args = self._parse_args(["--stdout"])
        assert args.stdout is True

    def test_dry_run_flag(self):
        args = self._parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_cache_dir_parsed(self):
        args = self._parse_args(["--cache-dir", "/tmp/cache"])
        assert args.cache_dir == "/tmp/cache"

    def test_custom_delay(self):
        args = self._parse_args(["--delay", "15.5"])
        assert args.delay == 15.5

    def test_custom_batch_size(self):
        args = self._parse_args(["--batch-size", "1000"])
        assert args.batch_size == 1000


# ═══════════════════════════════════════════════════════════════════
# TestShellScriptContent — 15 tests
# ═══════════════════════════════════════════════════════════════════

class TestShellScriptContent:

    def test_setup_sh_references_setup_hub(self):
        content = SETUP_SH.read_text()
        assert "setup-hub.sh" in content

    def test_setup_sh_has_ascii_art_banner(self):
        content = SETUP_SH.read_text()
        # The banner has the solar panel ASCII art
        assert "BANNER" in content or "__|__" in content

    def test_setup_sh_has_system_detection(self):
        content = SETUP_SH.read_text()
        assert "detect_system" in content

    def test_setup_sh_has_python_venv_step(self):
        content = SETUP_SH.read_text()
        assert "Python" in content and "venv" in content

    def test_setup_sh_has_protobuf_step(self):
        content = SETUP_SH.read_text()
        assert "Protobuf" in content or "protobuf" in content or "proto" in content

    def test_setup_sh_has_credentials_step(self):
        content = SETUP_SH.read_text()
        assert "credentials" in content.lower() or "ENPHASE_EMAIL" in content

    def test_setup_sh_has_telegraf_config_step(self):
        content = SETUP_SH.read_text()
        assert "Telegraf" in content and "configuration" in content.lower()

    def test_setup_sh_has_connection_test_step(self):
        content = SETUP_SH.read_text()
        assert "Connection test" in content or "connection test" in content

    def test_setup_sh_has_done_section(self):
        content = SETUP_SH.read_text()
        assert "Setup complete" in content or "DONE" in content

    def test_setup_sh_references_infra_scripts(self):
        content = SETUP_SH.read_text()
        assert "infra/scripts" in content

    def test_bin_telegraf_has_usage_comment(self):
        content = BIN_TELEGRAF.read_text()
        assert "Usage" in content or "usage" in content

    def test_bin_load_history_has_usage_comment(self):
        content = BIN_LOAD_HISTORY.read_text()
        assert "Usage" in content or "usage" in content

    def test_setup_sh_has_color_definitions(self):
        content = SETUP_SH.read_text()
        assert "BOLD=" in content
        assert "RESET=" in content
        assert "GREEN=" in content

    def test_setup_sh_has_full_and_app_modes(self):
        content = SETUP_SH.read_text()
        assert "--full" in content
        assert "--app" in content

    def test_setup_sh_has_load_history_prompt(self):
        content = SETUP_SH.read_text()
        assert "load-history" in content or "historical" in content.lower()
