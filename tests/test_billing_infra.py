"""The dangerous provisioning path must never execute during dry-run or bad input."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "bootstrap_infra.sh"


@pytest.mark.parametrize("ssh_port", [None, "22", "00222"])
def test_infrastructure_dry_run_is_offline_and_nonmutating(tmp_path, ssh_port):
    target = tmp_path / "deploy" / "bootstrap_infra.sh"
    target.parent.mkdir()
    shutil.copyfile(SCRIPT, target)
    args = [
        "/bin/bash", str(target), "--domain", "hivemind.test", "--email", "ops@hivemind.test",
        "--image", "ghcr.io/hivemind/scale@sha256:" + "a" * 64, "--dry-run",
    ]
    if ssh_port:
        args.extend(["--ssh-port", ssh_port])
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = subprocess.run(  # noqa: S603 -- fixed executable and test-owned arguments
        args, cwd=tmp_path, capture_output=True, text=True,
        env={"PATH": os.environ["PATH"]}, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "no files, packages, firewall rules or containers changed" in result.stdout
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("domain", ["https://hivemind.test", "hivemind.test;touch /tmp/proof", "x\n.test"])
def test_invalid_domain_stops_before_provisioning(tmp_path, domain):
    result = subprocess.run(  # noqa: S603 -- fixed executable and deliberately hostile test input
        ["/bin/bash", str(SCRIPT), "--domain", domain],
        cwd=tmp_path, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 1
    assert "DNS hostname is required" in result.stderr
