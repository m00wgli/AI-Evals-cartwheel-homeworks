"""Fixtures and constants for the Module 3 evaluation tests.

The three test files in this directory check different parts of the application:

  - test_unit.py:        deterministic checks that guard YOUR CODE. No LLM,
                         no database. Run on every commit.
  - test_integration.py: one bounded piece of a trajectory, the rest mocked
                         (FakeModel drives the real Runner). No API call.
                         Run on every push.
  - test_e2e.py:         the real agent on evaluation cases, k runs each,
                         checked against the CI rule. Costs money and samples, so
                         it runs on pull requests and nightly, and it skips
                         locally unless an API key is present.
"""

from __future__ import annotations

import os

import pytest

from replay.rollout import load_cases

# The course k for end-to-end cases (Artifact G): five runs per case.
# A clean 5-of-5 bounds the per-run pass rate above 0.55 at 95 percent
# confidence, which catches collapses per case; small drifts show up in the
# suite-level aggregate instead.
EVAL_K = 5

# Pin the agent and judge models by ids that name exactly one version, so a
# provider update cannot move CI overnight. The judge pin lives in the frozen
# judge file; this is the agent pin the complete agent test uses.
PINNED_AGENT_MODEL = os.environ.get("CARTWHEEL_EVAL_MODEL", "gpt-5.5")


@pytest.fixture(autouse=True)
def offline_env() -> None:
    """Keep the configured credentials, overriding tests/conftest.py.

    test_e2e.py runs the real agent and the frozen judges against a live
    model, so this directory needs the environment as the developer (or CI)
    set it. test_unit.py and test_integration.py stay offline by
    construction: no key means no call.
    """
    return None


@pytest.fixture(scope="session")
def evaluation_cases() -> list[dict]:
    """Every evaluation case in the Module 3 CI set."""
    return load_cases()
