from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to verify the pairing script")
def test_pairing_script_refresh_expiry_connection_and_errors():
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["node", str(root / "tests/relay_pairing_harness.cjs"), str(root / "app/static/js/relay-pairing.js")],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
