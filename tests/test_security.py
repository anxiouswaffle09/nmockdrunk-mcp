"""Tests for security module."""

import pytest
import tempfile
from pathlib import Path

from jdocmunch_mcp.security import (
    validate_path,
    is_symlink_escape,
    is_secret_file,
    is_binary_extension,
    is_binary_content,
    should_exclude_file,
    DEFAULT_MAX_FILE_SIZE,
)


class TestValidatePath:
    def test_valid_path(self, tmp_path):
        child = tmp_path / "sub" / "file.txt"
        assert validate_path(tmp_path, child) is True

    def test_path_traversal(self, tmp_path):
        evil = tmp_path / ".." / "outside"
        assert validate_path(tmp_path, evil) is False

    def test_same_path(self, tmp_path):
        assert validate_path(tmp_path, tmp_path) is True


class TestIsSecretFile:
    def test_env_file(self):
        assert is_secret_file(".env") is True

    def test_pem_file(self):
        assert is_secret_file("server.pem") is True

    def test_normal_file(self):
        assert is_secret_file("README.md") is False

    def test_credentials_json(self):
        assert is_secret_file("credentials.json") is True


class TestIsBinaryExtension:
    def test_png(self):
        assert is_binary_extension("image.png") is True

    def test_md(self):
        assert is_binary_extension("README.md") is False

    def test_txt(self):
        assert is_binary_extension("notes.txt") is False

    def test_exe(self):
        assert is_binary_extension("program.exe") is True


class TestIsBinaryContent:
    def test_text_content(self):
        assert is_binary_content(b"Hello, world!\n") is False

    def test_binary_content(self):
        assert is_binary_content(b"Hello\x00world") is True

    def test_empty(self):
        assert is_binary_content(b"") is False


class TestShouldExcludeFile:
    def test_normal_file(self, tmp_path):
        f = tmp_path / "README.md"
        f.write_text("# Hello")
        result = should_exclude_file(f, tmp_path)
        assert result is None

    def test_too_large(self, tmp_path):
        f = tmp_path / "big.md"
        f.write_bytes(b"x" * (DEFAULT_MAX_FILE_SIZE + 1))
        result = should_exclude_file(f, tmp_path)
        assert result == "file_too_large"

    def test_secret_file(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("SECRET=abc")
        result = should_exclude_file(f, tmp_path)
        assert result == "secret_file"

    def test_binary_content_doc_file(self, tmp_path):
        f = tmp_path / "weird.md"
        f.write_bytes(b"# Title\n\x00\nBody\n")
        result = should_exclude_file(f, tmp_path)
        assert result == "binary_content"
