"""scripts/test.sh keeps going when SIMULATOR_NAME is not installed.

The stubs stand in for xcodegen, xcodebuild and xcrun so the selection logic
runs without Xcode: the fallback to the first available iPhone has to reach the
build, and the message about the missing simulator has to be printed.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "test.sh"

WANTED_UDID = "22222222-2222-2222-2222-222222222222"
FALLBACK_UDID = "11111111-1111-1111-1111-111111111111"

STUBS = {
    "xcodegen": """#!/bin/bash
if [ "$1" = "--version" ]; then echo "XcodeGen 2.38.0"; fi
exit 0
""",
    "xcodebuild": """#!/bin/bash
echo "** TEST SUCCEEDED **"
exit 0
""",
}


def devices(*lines: str) -> str:
    """simctl's real format, which indents every device by four spaces."""
    body = [f"    {line}" for line in lines]
    return "\n".join(["== Devices ==", "-- iOS 18.0 --", *body]) + "\n"


class TestScriptSimulatorTests(unittest.TestCase):
    def run_script(
        self, *, simulator_name: str, simctl_output: str
    ) -> subprocess.CompletedProcess[str]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = root / "repo"
        (repo / "ios-app").mkdir(parents=True)
        (repo / "ios-app" / "project.yml").write_text("name: Hustler\n", encoding="utf-8")

        bin_dir = root / "bin"
        bin_dir.mkdir()
        stubs = dict(STUBS)
        stubs["xcrun"] = "#!/bin/bash\ncat <<'SIMCTL'\n" + simctl_output + "SIMCTL\n"
        for name, body in stubs.items():
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)

        env = dict(
            os.environ,
            PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            REPO_DIR=str(repo),
            SIMULATOR_NAME=simulator_name,
            XCODE_PROJECT="ios-app/Hustler.xcodeproj",
        )
        return subprocess.run(
            ["/bin/bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def test_a_missing_simulator_falls_back_and_says_so(self) -> None:
        result = self.run_script(
            simulator_name="iPhone 99",
            simctl_output=devices(f"iPhone 16 ({FALLBACK_UDID}) (Shutdown)"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "simulator 'iPhone 99' not found; using the first available iPhone",
            result.stdout,
        )
        self.assertIn(f"[test] simulator: {FALLBACK_UDID}", result.stdout)
        self.assertIn(f"tests passed on simulator {FALLBACK_UDID}", result.stdout)

    def test_an_installed_simulator_is_used_and_not_reported_missing(self) -> None:
        result = self.run_script(
            simulator_name="iPhone 99",
            simctl_output=devices(
                f"iPhone 99 ({WANTED_UDID}) (Shutdown)",
                f"iPhone 16 ({FALLBACK_UDID}) (Shutdown)",
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("not found; using the first available iPhone", result.stdout)
        self.assertIn(f"[test] simulator: {WANTED_UDID}", result.stdout)


if __name__ == "__main__":
    unittest.main()
