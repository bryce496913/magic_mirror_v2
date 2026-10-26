"""Run the browser clock's pure formatting functions when Node is available."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is optional on Raspberry Pi")
def test_clock_formats_and_date():
    script = Path(__file__).resolve().parents[1] / "magic_mirror/dashboard/static/clock.js"
    code = """
const assert = require('node:assert/strict');
const {formatTime, formatDate} = require(process.argv[1]);
const day = new Date(2026, 9, 26, 13, 5);
assert.equal(formatTime(day, '24', 'en-US'), '13:05');
assert.equal(formatTime(day, '12', 'en-US'), '01:05 PM');
assert.match(formatDate(day, 'en-US'), /Monday, October 26/);
"""
    subprocess.run(["node", "-e", code, str(script)], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is optional on Raspberry Pi")
def test_missing_module_data_uses_explicit_fallback():
    script = Path(__file__).resolve().parents[1] / "magic_mirror/dashboard/static/modules.js"
    code = """
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const fields = {status: {textContent: ''}, detail: {textContent: ''}};
const region = {
  classList: {contains: name => name === 'module'}, dataset: {},
  querySelector: selector => fields[selector.includes('status') ? 'status' : 'detail']
};
const context = {window: {}, document: {getElementById: id => id === 'module-assistant' ? region : null}};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
assert.equal(context.window.MirrorDashboard.setModule('assistant', null), true);
assert.equal(fields.status.textContent, 'Unavailable');
assert.equal(fields.detail.textContent, 'No data available.');
assert.equal(region.dataset.state, 'unavailable');
assert.equal(context.window.MirrorDashboard.setModule('missing', null), false);
"""
    subprocess.run(["node", "-e", code, str(script)], check=True, capture_output=True, text=True)
