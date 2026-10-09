import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import run_java_pilot as pilot


def test_pilot_references_only_current_prompt():
    assert set(pilot.PROMPTS) == {'zero-shot'}
    assert pilot.PROMPTS['zero-shot'].is_file()


@pytest.mark.parametrize('retired', ['structured-intent', 'access-aware', 'both', 'all'])
def test_pilot_rejects_retired_prompt_choices(retired):
    result = subprocess.run(
        [sys.executable, str(ROOT / 'scripts/run_java_pilot.py'), '--prompt', retired],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert 'invalid choice' in result.stderr


def test_default_pilot_prompt_is_zero_shot(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['run_java_pilot'])
    observed = []
    original_parse = pilot.argparse.ArgumentParser.parse_args

    def parse_args(parser, *args, **kwargs):
        parsed = original_parse(parser, *args, **kwargs)
        observed.append(parsed.prompt)
        return parsed

    monkeypatch.setattr(pilot.argparse.ArgumentParser, 'parse_args', parse_args)
    monkeypatch.setattr(pilot, 'select_tasks', lambda *args: [])
    with pytest.raises(SystemExit, match='No tasks selected'):
        pilot.main()
    assert observed == ['zero-shot']
