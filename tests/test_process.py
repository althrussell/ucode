"""Tests for cross-platform process helpers (PATH refresh + arg resolution)."""

from __future__ import annotations

import os
import sys
import types

import ucode.process as process_mod
from ucode.process import refresh_windows_path, windows_safe_args


class TestWindowsSafeArgs:
    def test_noop_off_windows(self, monkeypatch):
        monkeypatch.setattr(process_mod.platform, "system", lambda: "Darwin")
        args = ["databricks", "auth", "login"]
        assert windows_safe_args(args) is args

    def test_noop_on_empty(self, monkeypatch):
        monkeypatch.setattr(process_mod.platform, "system", lambda: "Windows")
        assert windows_safe_args([]) == []

    def test_resolves_exe_to_absolute_path(self, monkeypatch):
        monkeypatch.setattr(process_mod.platform, "system", lambda: "Windows")
        monkeypatch.setattr(
            process_mod.shutil,
            "which",
            lambda name: r"C:\Users\me\AppData\Local\Microsoft\WinGet\Links\databricks.exe",
        )
        out = windows_safe_args(["databricks", "auth", "login"])
        assert out == [
            r"C:\Users\me\AppData\Local\Microsoft\WinGet\Links\databricks.exe",
            "auth",
            "login",
        ]

    def test_routes_cmd_shim_through_comspec(self, monkeypatch):
        monkeypatch.setattr(process_mod.platform, "system", lambda: "Windows")
        monkeypatch.setattr(
            process_mod.shutil,
            "which",
            lambda name: r"C:\Program Files\nodejs\npm.cmd",
        )
        monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
        out = windows_safe_args(["npm", "install", "-g", "claude"])
        assert out == [
            r"C:\Windows\System32\cmd.exe",
            "/d",
            "/s",
            "/c",
            r"C:\Program Files\nodejs\npm.cmd",
            "install",
            "-g",
            "claude",
        ]

    def test_unresolved_name_left_untouched(self, monkeypatch):
        monkeypatch.setattr(process_mod.platform, "system", lambda: "Windows")
        monkeypatch.setattr(process_mod.shutil, "which", lambda name: None)
        args = ["nonexistent-tool", "--help"]
        assert windows_safe_args(args) == args

    def test_existing_path_is_not_re_resolved(self, monkeypatch):
        monkeypatch.setattr(process_mod.platform, "system", lambda: "Windows")

        def boom(_name):  # which must not be consulted for an explicit path
            raise AssertionError("shutil.which should not be called for a path")

        monkeypatch.setattr(process_mod.shutil, "which", boom)
        args = [r"C:\tools\databricks.exe", "--version"]
        assert windows_safe_args(args) == args


class TestRefreshWindowsPath:
    def test_noop_off_windows(self, monkeypatch):
        monkeypatch.setattr(process_mod.platform, "system", lambda: "Linux")
        before = os.environ.get("PATH")
        refresh_windows_path()
        assert os.environ.get("PATH") == before

    def test_merges_registry_path_on_windows(self, monkeypatch):
        monkeypatch.setattr(process_mod.platform, "system", lambda: "Windows")

        # Build a fake winreg that returns a machine + user PATH.
        fake = types.SimpleNamespace()
        fake.HKEY_LOCAL_MACHINE = "HKLM"
        fake.HKEY_CURRENT_USER = "HKCU"
        values = {
            ("HKLM", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"): (
                r"C:\Windows;C:\Windows\System32"
            ),
            ("HKCU", "Environment"): r"C:\Users\me\AppData\Local\Microsoft\WinGet\Links",
        }
        # Windows joins PATH entries with ';'; the helper uses that regardless of host.

        class _Key:
            def __init__(self, root, sub):
                self._id = (root, sub)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def open_key(root, sub):
            if (root, sub) not in values:
                raise OSError("missing key")
            return _Key(root, sub)

        def query_value_ex(key, name):
            assert name == "Path"
            return values[key._id], 2  # 2 == REG_EXPAND_SZ

        fake.OpenKey = open_key
        fake.QueryValueEx = query_value_ex
        monkeypatch.setitem(sys.modules, "winreg", fake)

        monkeypatch.setenv("PATH", r"C:\uv\tools\bin")
        refresh_windows_path()
        parts = os.environ["PATH"].split(";")
        # Registry machine + user entries are merged in, plus the pre-existing one.
        assert r"C:\Windows" in parts
        assert r"C:\Users\me\AppData\Local\Microsoft\WinGet\Links" in parts
        assert r"C:\uv\tools\bin" in parts

    def test_windows_without_winreg_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(process_mod.platform, "system", lambda: "Windows")
        # Simulate winreg import failing.
        monkeypatch.setitem(sys.modules, "winreg", None)
        before = os.environ.get("PATH")
        refresh_windows_path()  # must not raise
        assert os.environ.get("PATH") == before
