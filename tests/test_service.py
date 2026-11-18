import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clipper import service


class EnvFileTests(unittest.TestCase):
    def test_parse(self):
        text = """
# comment
ANTHROPIC_API_KEY=sk-test-123
export IG_ACCESS_TOKEN="EAAtoken"
QUOTED='single'
EMPTY=
not a line
1BAD=x
"""
        self.assertEqual(service.parse_env(text), {
            "ANTHROPIC_API_KEY": "sk-test-123", "IG_ACCESS_TOKEN": "EAAtoken", "QUOTED": "single"})

    def test_shell_values_win_and_names_only_returned(self):
        path = Path(tempfile.mkdtemp()) / "clipper.env"
        path.write_text("CLIPPER_T_A=file\nCLIPPER_T_B=file\n")
        with mock.patch.dict(os.environ, {"CLIPPER_T_A": "shell"}, clear=False):
            loaded = service.load_env_file(path)
            self.assertEqual(loaded, ["CLIPPER_T_B"])
            self.assertEqual(os.environ["CLIPPER_T_A"], "shell")
            self.assertEqual(os.environ["CLIPPER_T_B"], "file")
        os.environ.pop("CLIPPER_T_B", None)

    def test_missing_file(self):
        self.assertEqual(service.load_env_file(Path("/nonexistent/clipper.env")), [])


class CloudOverrideTests(unittest.TestCase):
    def test_env_overrides_apply_to_loaded_config(self):
        from clipper import config as config_mod

        path = Path(tempfile.mkdtemp()) / "config.toml"
        path.write_text('[review]\nhost = "127.0.0.1"\n')
        env = {"CLIPPER_REVIEW_HOST": "0.0.0.0", "CLIPPER_CAPTION_FONT": "Anton"}
        with mock.patch.dict(os.environ, env):
            cfg = config_mod.load(path)
        self.assertEqual(cfg["review"]["host"], "0.0.0.0")
        self.assertEqual(cfg["captions"]["font"], "Anton")
        self.assertEqual(config_mod.load(path)["review"]["host"], "127.0.0.1")


class PlistTests(unittest.TestCase):
    def test_plist_shape(self):
        p = service.build_plist(Path("/proj"), "/proj/.venv/bin/python", Path("/proj/data/logs/service.log"))
        self.assertEqual(p["Label"], service.LABEL)
        self.assertEqual(p["ProgramArguments"], ["/proj/.venv/bin/python", "-m", "clipper", "run"])
        self.assertTrue(p["KeepAlive"])
        self.assertTrue(p["RunAtLoad"])
        self.assertEqual(p["WorkingDirectory"], "/proj")
        self.assertIn("/opt/homebrew/bin", p["EnvironmentVariables"]["PATH"])
        self.assertEqual(p["EnvironmentVariables"][service.SERVICE_ENV], "1")
        # secrets never go in the plist
        self.assertNotIn("ANTHROPIC_API_KEY", p["EnvironmentVariables"])

    def test_custom_config_path(self):
        p = service.build_plist(Path("/proj"), "py", Path("/l"), "/elsewhere/config.toml")
        self.assertEqual(p["ProgramArguments"], ["py", "-m", "clipper", "--config", "/elsewhere/config.toml", "run"])


class RuntimeTests(unittest.TestCase):
    def test_port_in_use(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            self.assertTrue(service.port_in_use("127.0.0.1", port))
        self.assertFalse(service.port_in_use("127.0.0.1", port))

    @unittest.skipUnless(sys.platform == "darwin", "macOS only")
    def test_keep_awake_follows_our_pid(self):
        proc = service.keep_awake()
        self.assertIsNotNone(proc)
        try:
            args = subprocess.run(["ps", "-o", "args=", "-p", str(proc.pid)],
                                  capture_output=True, text=True).stdout
            self.assertIn(f"-w {os.getpid()}", args)
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
