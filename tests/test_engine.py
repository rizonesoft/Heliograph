"""The engine's own tests run as a Windows console program."""
import os
import platform
import subprocess
import unittest
from pathlib import Path

BUILD = Path(os.environ["IMPOSSIBLE_BUILD"])


@unittest.skipUnless(os.name == "nt" or "microsoft" in platform.release().lower(), "needs Windows, directly or through WSL")
class Engine(unittest.TestCase):
    def test_engine(self):
        result = subprocess.run([str(BUILD / "engine-tests.exe")], capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
