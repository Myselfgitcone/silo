"""Questions with known answers, graded without a second model.

Each case pairs a question with SQL that computes the right answer from the
lake at eval time, so expectations follow the data as Grainflow refreshes it.
The agent passes a case when its answer mentions every expected value: numbers
within the rounding the answer itself uses, state codes or their names, dates
in ISO or "October 18" form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from silo.agent import Answer, answer
from silo.budget import Budget, BudgetExceeded
from silo.lake import Lake
from silo.server import build_server
from silo.settings import Settings
from silo.tools import Toolbox

STATES = {
    "AL": "Alabama", "AR": "Arkansas", "CO": "Colorado", "DE": "Delaware", "GA": "Georgia",
    "IA": "Iowa", "IL": "Illinois", "IN": "Indiana", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "MD": "Maryland", "MI": "Michigan", "MN": "Minnesota", "MO": "Missouri",
    "MS": "Mississippi", "NC": "North Carolina", "ND": "North Dakota", "NE": "Nebraska",
    "NJ": "New Jersey", "NY": "New York", "OH": "Ohio", "OK": "Oklahoma", "PA": "Pennsylvania",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "VA": "Virginia",
    "WI": "Wisconsin", "WV": "West Virginia",
}  # fmt: skip

_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    truth: str | None = None
    mentions: tuple[str, ...] = ()
    mentions_any: tuple[str, ...] = ()


@dataclass
class Result:
    case: Case
    answer: Answer
    expected: list[str]
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def load_cases(path: Path) -> list[Case]:
    raw = yaml.safe_load(path.read_text())["cases"]
    return [
        Case(
            id=c["id"],
            question=" ".join(c["question"].split()),
            truth=c.get("truth"),
            mentions=tuple(c.get("mentions", [])),
            mentions_any=tuple(c.get("mentions_any", [])),
        )
        for c in raw
    ]


def expected_values(case: Case, lake: Lake, timeout_s: float = 10) -> list[str]:
    """Fill each `mentions` template from the first row of the case's truth SQL."""
    if not case.mentions:
        return []
    result = lake.execute(case.truth, timeout_s)
    if not result.rows:
        raise ValueError(f"{case.id}: the truth query returned no rows")
    row = dict(zip(result.columns, result.rows[0], strict=True))
    return [template.format(**row) for template in case.mentions]


async def run_cases(
    cases: list[Case], toolbox: Toolbox, llm, settings: Settings, budget: Budget, on_result=None
) -> list[Result]:
    """Ask every question and grade it. Expected values are computed first, so a
    broken truth query fails fast instead of after paying for answers."""
    expected = {c.id: expected_values(c, toolbox.lake, settings.query_timeout_s) for c in cases}
    results = []
    for case in cases:
        try:
            reply = await answer(case.question, build_server(toolbox), llm, settings, budget)
            failures = grade(reply.text, expected[case.id], case)
        except BudgetExceeded:
            raise
        except Exception as error:
            # One broken case shouldn't hide the results of the rest.
            reply = Answer(question=case.question, text="", model=settings.model)
            failures = [f"error: {type(error).__name__}: {error}"]
        result = Result(case, reply, expected[case.id], failures)
        results.append(result)
        if on_result:
            on_result(result)
    return results


def grade(answer_text: str, expected: list[str], case: Case) -> list[str]:
    """What the answer got wrong; an empty list is a pass."""
    text = answer_text.replace("\u2212", "-")  # typographic minus
    failures = [f"missing {e!r}" for e in expected if not _mentions(text, e)]
    if case.mentions_any and not any(m.lower() in text.lower() for m in case.mentions_any):
        failures.append(f"none of {list(case.mentions_any)}")
    return failures


def _mentions(text: str, expected: str) -> bool:
    """Any of the |-separated alternatives appears in the text."""
    return any(_one(text, alt.strip()) for alt in expected.split("|"))


def _one(text: str, expected: str) -> bool:
    if expected.upper() in STATES:
        names = (expected, STATES[expected.upper()])
        return any(re.search(rf"\b{re.escape(n)}\b", text, re.IGNORECASE) for n in names)
    try:
        value = float(expected.replace(",", ""))
    except ValueError:
        return expected.lower() in text.lower()

    wanted = _decimals(expected)
    for match in _NUMBER.findall(text):
        found = float(match.replace(",", ""))
        places = _decimals(match)
        # Compare at the coarser of the two precisions, allowing the answer one
        # decimal place less than the template. The sign is often said in words
        # ("fell 9.2 points"), so magnitudes are compared.
        tolerance = 0.5 * 10 ** -min(places, wanted) + 1e-9
        if places >= wanted - 1 and abs(abs(found) - abs(value)) <= tolerance:
            return True
    return False


def _decimals(number: str) -> int:
    return len(number.split(".")[1]) if "." in number else 0


def summary_markdown(results: list[Result]) -> str:
    passed = sum(r.passed for r in results)
    cost = sum(r.answer.cost_usd for r in results)
    lines = [
        f"### Silo evals: {passed}/{len(results)} passed",
        "",
        f"Total cost ${cost:.4f}, model `{results[0].answer.model if results else '-'}`.",
        "",
        "| Case | Result | Tool calls | Cost | Notes |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        notes = "; ".join(r.failures) or ""
        lines.append(
            f"| {r.case.id} | {'pass' if r.passed else 'FAIL'} | {len(r.answer.steps)} "
            f"| ${r.answer.cost_usd:.4f} | {notes} |"
        )
    return "\n".join(lines) + "\n"


def report_json(results: list[Result]) -> dict[str, Any]:
    return {
        "passed": sum(r.passed for r in results),
        "total": len(results),
        "cases": [
            {
                "id": r.case.id,
                "passed": r.passed,
                "expected": r.expected,
                "failures": r.failures,
                "answer": r.answer.to_json(),
            }
            for r in results
        ],
    }
