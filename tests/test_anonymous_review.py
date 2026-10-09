"""Guard author-controlled defaults and documentation used in review mirrors."""
import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_review_documentation_does_not_link_to_author_owned_repository():
    repository_link = re.compile(
        r"https://github\.com/[^/\s)]+/"
        r"Beyond-Coverage-and-Mutation-A-Blind-Spot-Analysis-of-LLM-Generated-Unit-Tests"
        r"(?:[/#?]|\b)"
    )
    paths = list(ROOT.glob('*.md'))
    paths += list((ROOT / 'docs').glob('*.md'))
    paths += list((ROOT / 'analysis/final-mixed-runs/release').glob('*.md'))
    for path in paths:
        assert not repository_link.search(path.read_text()), path.relative_to(ROOT)


def test_panta_api_key_default_is_provider_neutral():
    tree = ast.parse((ROOT / 'scripts/run_panta_benchmark.py').read_text())
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == 'add_argument' and node.args
             and isinstance(node.args[0], ast.Constant)
             and node.args[0].value == '--api-key-env']
    assert len(calls) == 1
    defaults = [keyword.value for keyword in calls[0].keywords if keyword.arg == 'default']
    assert len(defaults) == 1
    assert isinstance(defaults[0], ast.Constant)
    assert defaults[0].value == 'LLM_API_KEY'
