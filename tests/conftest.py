import glob
import os
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from jobbot.config import load_profile  # noqa: E402
from jobbot.models import Job  # noqa: E402


@pytest.fixture
def profile():
    return load_profile(demo=True)


@pytest.fixture
def job():
    return Job("greenhouse", "123", "Initech", "Senior Backend Engineer", "Remote - United States",
               "https://boards.greenhouse.io/initech/jobs/123",
               "We need a backend engineer fluent in Python, PostgreSQL, Kafka and Kubernetes. Rust and Elixir are a plus.",
               remote=True, board="initech")


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright
    exe = os.environ.get("JOBBOT_CHROMIUM")
    if not exe:  # sandbox images ship the browser outside Playwright's default cache
        found = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")
        exe = found[0] if found and not glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium*")) else None
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=exe)
        yield b
        b.close()
