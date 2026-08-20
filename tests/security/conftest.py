"""Fixtures for the cross-cutting security suite.

This suite runs against real containers through a real Docker daemon. It is
deliberately separate from `apps/api/tests` — the judge is a separate deployable
with a separate blast radius, and a green API suite must never be mistaken for
evidence that the sandbox holds.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "workers" / "judge"))


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30, check=False
        ).returncode
        == 0
    )


@pytest.fixture(scope="session")
def judge_image() -> str:
    """The sandbox image the tests execute inside.

    Defaults to the image the compose stack builds. Overridable so the suite can
    run against a locally-assembled image on a machine that cannot pull from
    Docker Hub — the controls under test are runtime flags, not properties of
    the base image, so the evidence is the same either way.
    """
    image = os.getenv("JUDGE_TEST_IMAGE", "sentinel-judge-python311:local")
    if not _docker_available():
        pytest.skip(
            "No Docker daemon; the sandbox suite cannot prove anything without one."
        )
    exists = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    )
    if exists.returncode != 0:
        pytest.skip(
            f"Image {image!r} is not present. Build it with "
            "`docker compose build judge-python311`, or set JUDGE_TEST_IMAGE."
        )
    return image


@pytest.fixture(scope="session")
def sandbox(judge_image: str):
    from sentinel_judge.sandbox import Sandbox

    return Sandbox(image=judge_image)


@pytest.fixture(scope="session")
def cpp_image() -> str:
    """The C++ toolchain image.

    Separate from `judge_image` because the compiled path exercises code the
    interpreted path never touches — artefact export, the execute bit, a real
    compiler running candidate input inside the sandbox.
    """
    image = os.getenv("JUDGE_TEST_IMAGE_CPP", "sentinel-judge-cpp20:local")
    if not _docker_available():
        pytest.skip("No Docker daemon.")
    exists = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    )
    if exists.returncode != 0:
        pytest.skip(
            f"Image {image!r} is not present; skipping the compiled-language tests."
        )
    return image
