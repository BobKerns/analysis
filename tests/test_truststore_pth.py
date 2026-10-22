"""The venv's ``analysis_truststore.pth`` puts every process on the OS trust store (#86)."""

import subprocess
import sys
import sysconfig
from pathlib import Path


def test_pth_is_installed_in_site_packages():
    pth = Path(sysconfig.get_paths()["purelib"]) / "analysis_truststore.pth"
    assert pth.read_text().strip() == "import truststore; truststore.inject_into_ssl()"


def test_fresh_interpreter_uses_truststore():
    # A new process, so the check sees what startup did, not anything this
    # test session imported or patched.
    out = subprocess.run(
        [sys.executable, "-c", "import ssl, truststore; print(ssl.SSLContext is truststore.SSLContext)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "True"
