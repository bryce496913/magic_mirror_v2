"""The browser's presentation rounds both units only after conversion."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is optional on Raspberry Pi")
def test_temperature_formatting():
    script = Path(__file__).resolve().parents[1] / "magic_mirror/dashboard/static/data.js"
    code = """
const assert = require('node:assert/strict');
const {formatTemperature} = require(process.argv[1]);
assert.equal(formatTemperature(11, 'C'), '11°C');
assert.equal(formatTemperature(51.8, 'F'), '52°F');
assert.equal(formatTemperature(-10, 'C'), '-10°C');
assert.equal(formatTemperature(14, 'F'), '14°F');
assert.equal(formatTemperature(-14.5, 'F'), '-15°F');
assert.equal(formatTemperature(-0.1, 'C'), '0°C');
assert.equal(formatTemperature(null, 'C'), '—');
"""
    subprocess.run(["node", "-e", code, str(script)], check=True, capture_output=True, text=True)
