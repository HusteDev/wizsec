"""Tests for the wizsec CLI (config and credential management)."""

import configparser

import pytest
import yaml

from wizsec._cli import main


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "wiz.config"
    path.write_text(
        yaml.safe_dump(
            {
                "app": {"name": "wizsec", "config_schema": 2},
                "api": {"timeout": 120},
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def creds_file(tmp_path):
    path = tmp_path / "wiz.credentials"
    parser = configparser.ConfigParser()
    parser["default"] = {
        "client_id": "abcd1234efgh",
        "client_secret": "s3cr3t",
        "environment": "gov",
    }
    with open(path, "w", encoding="utf-8") as fh:
        parser.write(fh)
    return path


class TestCliBasics:
    def test_no_command_prints_help(self, capsys):
        assert main([]) == 2
        assert "usage: wizsec" in capsys.readouterr().out

    def test_version(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert "wizsec" in capsys.readouterr().out


class TestConfigCommands:
    def test_config_path(self, config_file, capsys):
        assert main(["config", "--file", str(config_file), "path"]) == 0
        assert str(config_file) in capsys.readouterr().out

    def test_config_show(self, config_file, capsys):
        assert main(["config", "--file", str(config_file), "show"]) == 0
        out = capsys.readouterr().out
        assert "timeout: 120" in out

    def test_config_show_missing_file(self, tmp_path, capsys):
        missing = tmp_path / "nope.config"
        assert main(["config", "--file", str(missing), "show"]) == 1
        assert "no config file" in capsys.readouterr().err

    def test_config_get(self, config_file, capsys):
        assert main(["config", "--file", str(config_file), "get", "api.timeout"]) == 0
        assert capsys.readouterr().out.strip() == "120"

    def test_config_get_missing_key(self, config_file, capsys):
        assert main(["config", "--file", str(config_file), "get", "api.nope"]) == 1
        assert "key not found" in capsys.readouterr().err

    def test_config_set_creates_nested_keys_with_yaml_typing(self, config_file, capsys):
        assert (
            main(
                [
                    "config",
                    "--file",
                    str(config_file),
                    "set",
                    "rate_limit.headroom",
                    "0.5",
                ]
            )
            == 0
        )
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert data["rate_limit"]["headroom"] == 0.5
        # existing keys preserved
        assert data["api"]["timeout"] == 120

    def test_config_set_bool_typing(self, config_file):
        main(
            ["config", "--file", str(config_file), "set", "api.auto_paginate", "false"]
        )
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert data["api"]["auto_paginate"] is False

    def test_config_unset(self, config_file):
        assert main(["config", "--file", str(config_file), "unset", "api.timeout"]) == 0
        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert "timeout" not in data["api"]

    def test_config_unset_missing_key(self, config_file):
        assert main(["config", "--file", str(config_file), "unset", "api.nope"]) == 1

    def test_config_init_refuses_overwrite(self, config_file, capsys):
        assert main(["config", "--file", str(config_file), "init"]) == 1
        assert "already exists" in capsys.readouterr().err

    def test_config_init_force(self, config_file):
        assert main(["config", "--file", str(config_file), "init", "--force"]) == 0
        content = config_file.read_text(encoding="utf-8")
        assert "config_schema" in content


class TestCredsCommands:
    def test_creds_list_masks_client_id_and_hides_secret(self, creds_file, capsys):
        assert main(["creds", "--file", str(creds_file), "list"]) == 0
        out = capsys.readouterr().out
        assert "default" in out
        assert "abcd" in out and "abcd1234efgh" not in out
        assert "s3cr3t" not in out
        assert "environment=gov" in out

    def test_creds_list_empty(self, tmp_path, capsys):
        missing = tmp_path / "none.credentials"
        assert main(["creds", "--file", str(missing), "list"]) == 0
        assert "no profiles" in capsys.readouterr().out

    def test_creds_set_writes_profile(self, tmp_path, monkeypatch):
        path = tmp_path / "new.credentials"
        monkeypatch.setattr("wizsec._cli.getpass.getpass", lambda prompt: "secret-x")
        assert (
            main(
                [
                    "creds",
                    "--file",
                    str(path),
                    "set",
                    "--profile",
                    "staging",
                    "--environment",
                    "app",
                    "--client-id",
                    "cid-123",
                ]
            )
            == 0
        )
        parser = configparser.ConfigParser()
        parser.read(path)
        assert parser["staging"]["client_id"] == "cid-123"
        assert parser["staging"]["client_secret"] == "secret-x"
        assert parser["staging"]["environment"] == "app"

    def test_creds_set_rejects_empty_secret(self, tmp_path, monkeypatch):
        path = tmp_path / "new.credentials"
        monkeypatch.setattr("wizsec._cli.getpass.getpass", lambda prompt: "")
        assert (
            main(["creds", "--file", str(path), "set", "--client-id", "cid-123"]) == 1
        )
        assert not path.exists()

    def test_creds_remove_with_yes(self, creds_file):
        assert (
            main(["creds", "--file", str(creds_file), "remove", "default", "--yes"])
            == 0
        )
        parser = configparser.ConfigParser()
        parser.read(creds_file)
        assert "default" not in parser

    def test_creds_remove_missing_profile(self, creds_file):
        assert (
            main(["creds", "--file", str(creds_file), "remove", "ghost", "--yes"]) == 1
        )
