# RFC 0001 — remitmatch: payment and invoice reconciliation (Rust + Python)

**Status:** draft · **Type:** RFC 0001 · **Reviewers:** pending · **Date:** 2026-11-22

## Problem and success metrics

Reconciling payments with invoices is still manual work at many SMBs and finance teams: someone opens the bank statement and searches by hand for which invoice each transaction pays.

What makes it hard is that bank descriptions are noisy free text, payer names do not match those in the ERP, fees alter the amount, and a single payment can cover several invoices (or the other way around). Python solutions are usually pandas loops that do not scale; the fast ones have no Python API.

**Users:** developers building invoicing, accounting or treasury software in Python; in InvoiceHub, the end user is whoever reviews the reconciliation queue.

| Metric | v1 target | How it is measured |
| --- | --- | --- |
| Payments matched as `auto` | ≥ 85 % | Generator data with realistic noise |
| Precision of the `auto` matches | ≥ 99 % | Against the generator's known solution |
| Time with 100 thousand payments | < 5 s on an 8-core laptop | Reproducible benchmark in the repo |
| Improvement over pandas + rapidfuzz | ≥ 20× | Same dataset and machine |

The target figures are initial estimates: they are revised after the first round of benchmarks.

## Goal and positioning

A Python library with a Rust core (PyO3) that matches payments to invoices, including split and grouped payments, and explains each match with a broken-down score. It is called `remitmatch`: *remittance matching* is the accounts receivable term for linking a payment to the invoices it settles. It is free on PyPI and crates.io, with no products under that name and 3 repos on GitHub.

`ledgermatch` was rejected: it is already used by a commercial Google Sheets add-on with the same features (exact, approximate, split and grouped), an enterprise reconciliation platform and about 20 GitHub repos.

The gap it fills: today nothing combines a Python API, a Rust core, 1:N and N:1 payments, explainable matches and exact money.

| Project | Language | What it lacks compared to this library |
| --- | --- | --- |
| [hyperswitch-reconciliation](https://github.com/juspay/hyperswitch-reconciliation) | Rust | Closest in features (1:1, 1:N, N:M), but it is a service with an API and Postgres that must be deployed; it is not called from your process |
| [recon-kit](https://github.com/WyattAu/recon-kit) | Rust | 1:1-only crate with no tolerances, by design; no Python |
| [OpenRec](https://github.com/GrandmasterTash/OpenRec) | Rust | It is a CLI with rules in YAML and Lua, not a Python library |
| [reconPy](https://pypi.org/project/reconPy) | Python | Pure Python and one-to-one only |
| [florecon](https://docs.rs/crate/florecon/0.1.2/source/README.md) | Rust + Wasm | To define the strategy you have to write Rust |
| [Reconify](https://pkg.go.dev/github.com/reconifyhq/reconify) | Go | It has no Python interface |
| [ReconProof](https://apify.com/zahi90/reconproof.md) | Closed | Commercial; it does cover split and grouped payments |

For the portfolio it demonstrates three things: useful Rust in production, public API design and knowledge of the fintech domain.

## v1 scope

v1 includes split and grouped payments; what is left out is what adds nothing to the demo or the benchmark.

**In v1**

- 1:1, 1:N (one payment covers several invoices) and N:1 (one invoice paid in several payments) matching
- Configurable tolerances: amount (absolute and %), date window in days
- Extraction of references from the bank description (invoice number inside free text)
- Counterparty name similarity (normalization + Jaro-Winkler / token set)
- Score broken down by signal and status: `auto`, `suggested`, `ambiguous`
- Partitions by scope and currency: different currencies or companies are never matched
- Exact money in integer cents (`i64`), no floats
- Input: lists of Python objects or Arrow tables (Polars/pandas via pyarrow); output: objects + `to_arrow()`
- Parallelism with rayon, releasing the GIL

**Left for v2**

- N:M matching (several payments against several invoices at once)
- Currency conversion
- Bank statement parsers (CAMT.053, MT940, OFX)
- Learning from user corrections
- CLI and streaming processing

## Data model and Python API

A single entry function, `reconcile()`, with immutable types and a result that can be inspected or exported to Arrow. The types are defined in Rust (`#[pyclass]`) and published with `.pyi` stubs; this is the contract as seen from Python:

```python
from dataclasses import dataclass, field
from datetime import date
from enum import Enum


@dataclass(frozen=True)
class Invoice:
    """An open receivable that payments can settle.

    Attributes:
        id: Unique invoice identifier in the caller's system.
        number: Human-facing invoice number searched for in payment references.
        amount_minor: Outstanding amount in minor units (e.g. cents).
        currency: ISO 4217 code; invoices only match payments in the same currency.
        issue_date: Date the invoice was issued.
        due_date: Date payment is expected.
        counterparty: Customer name as stored in the ledger.
        scope: Partition key (company, account); different scopes never match.
    """

    id: str
    number: str
    amount_minor: int
    currency: str
    issue_date: date
    due_date: date
    counterparty: str
    scope: str = "default"


@dataclass(frozen=True)
class Payment:
    """A bank movement received that may settle one or more invoices.

    Attributes:
        id: Unique movement identifier from the bank feed.
        amount_minor: Received amount in minor units.
        currency: ISO 4217 code.
        value_date: Date the funds were credited.
        reference: Free-text remittance info / description from the bank.
        counterparty: Payer name as reported by the bank.
        scope: Partition key; must equal the invoice scope to match.
    """

    id: str
    amount_minor: int
    currency: str
    value_date: date
    reference: str
    counterparty: str
    scope: str = "default"


@dataclass(frozen=True)
class MatchConfig:
    """Tolerances and thresholds that drive matching.

    Attributes:
        amount_tolerance_minor: Absolute amount difference accepted (e.g. bank fees).
        date_window_days: Max days between due date and value date.
        max_group_size: Max invoices (1:N) or payments (N:1) in one group.
        auto_threshold: Score at or above which a match is accepted automatically.
        suggest_threshold: Score at or above which a match is proposed for review.
        ambiguity_margin: If the best two candidates differ by less than this, flag ambiguous.
        weights: Relative weight of each signal in the final score.
    """

    amount_tolerance_minor: int = 0
    date_window_days: int = 30
    max_group_size: int = 5
    auto_threshold: float = 0.90
    suggest_threshold: float = 0.60
    ambiguity_margin: float = 0.05
    weights: dict[str, float] = field(
        default_factory=lambda: {"reference": 0.45, "amount": 0.30, "name": 0.15, "date": 0.10}
    )


class MatchKind(Enum):
    """Cardinality of a match."""

    ONE_TO_ONE = "1:1"
    ONE_TO_MANY = "1:N"
    MANY_TO_ONE = "N:1"


class MatchStatus(Enum):
    """How confident the engine is in a proposed match."""

    AUTO = "auto"
    SUGGESTED = "suggested"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Match:
    """A proposed link between payments and invoices.

    Attributes:
        kind: 1:1, 1:N or N:1.
        status: auto, suggested or ambiguous.
        payment_ids: Payments in the match.
        invoice_ids: Invoices in the match.
        score: Final weighted score in [0, 1].
        breakdown: Per-signal scores that explain the final score.
        residual_minor: Amount difference left after matching (fees, rounding).
    """

    kind: MatchKind
    status: MatchStatus
    payment_ids: tuple[str, ...]
    invoice_ids: tuple[str, ...]
    score: float
    breakdown: dict[str, float]
    residual_minor: int


@dataclass(frozen=True)
class ReconciliationResult:
    """Output of a reconciliation run.

    Attributes:
        matches: Proposed matches, best score first.
        unmatched_payments: Payment ids with no acceptable candidate.
        unmatched_invoices: Invoice ids with no acceptable candidate.
    """

    matches: list[Match]
    unmatched_payments: list[str]
    unmatched_invoices: list[str]

    def to_arrow(self) -> "pyarrow.Table":
        """Return matches as a flat Arrow table (one row per payment-invoice pair)."""
        ...


def reconcile(
    invoices: "list[Invoice] | pyarrow.Table",
    payments: "list[Payment] | pyarrow.Table",
    config: MatchConfig | None = None,
) -> ReconciliationResult:
    """Match payments against open invoices.

    Runs in Rust and releases the GIL. Each invoice and payment appears in at
    most one match. Inputs may be Python objects or Arrow tables with the same
    column names as the dataclass fields.

    Args:
        invoices: Open invoices to settle.
        payments: Bank movements received.
        config: Tolerances and thresholds; defaults to ``MatchConfig()``.

    Returns:
        Matches with per-signal breakdown, plus unmatched ids on each side.
    """
    ...
```

Usage example: `result = reconcile(invoices, payments, MatchConfig(amount_tolerance_minor=150))`.

## Matching algorithm

The engine works in passes from safest to most expensive: whatever a pass matches does not enter the next one, so the group search only sees what is left unmatched.

1. **Normalize and partition.** References in uppercase without spaces or hyphens; names without accents or company suffixes (S.L., LLC, Inc.). Records are grouped by `(scope, currency)` and each partition is processed in parallel.
2. **Reference pass.** An Aho-Corasick automaton with the numbers of the open invoices finds, in a single read, all the invoices each description mentions. If it mentions one and the amount matches → 1:1. If it mentions several and the sum matches → 1:N.
3. **1:1 candidates.** An index sorted by amount returns only the invoices within tolerance; the result is then filtered by date window and scored. This avoids comparing everything against everything.
4. **1:N groups.** For each unmatched payment: open invoices of the same counterparty (name above the threshold) and within the window. A subset that sums to the payment amount ± tolerance is searched for, with at most `max_group_size` invoices.
   1. It is a *subset-sum* variant: it is bounded with a maximum number of candidates (e.g. 24) and *meet-in-the-middle*; if there are more, the oldest invoices are prioritized.
   2. If more than one subset is valid, the match is marked `ambiguous`.
5. **N:1 groups.** The same in reverse: unmatched payments of the same counterparty whose sum covers an invoice (partial payments).
6. **Global assignment.** All candidates are sorted by score and accepted greedily if none of their ids is already used. If a payment's second-best candidate is within `ambiguity_margin`, the status is `ambiguous` and never `auto`.
7. **Final status.** `auto` if the score exceeds `auto_threshold` and it is not ambiguous; `suggested` if it exceeds `suggest_threshold`; the rest stays unmatched.

**Scoring.** Weighted sum of four signals in [0, 1], also returned separately in `breakdown`:

| Signal | How it is calculated | Default weight |
| --- | --- | --- |
| Reference | 1 if the exact number appears; partial if it appears with high similarity | 0.45 |
| Amount | 1 if exact; drops linearly to 0 at the tolerance limit | 0.30 |
| Name | Jaro-Winkler over the normalized names | 0.15 |
| Date | 1 within the first days; drops linearly to the end of the window | 0.10 |

Greedy assignment is enough for v1; the Hungarian algorithm for optimal 1:1 assignment remains as an improvement measurable in the benchmarks.

## Rust architecture, packaging and CI

Two crates in a workspace: the core knows nothing about Python, so it is tested and measured with Rust tools and also published on crates.io.

```text
remitmatch/
├── Cargo.toml              # workspace
├── pyproject.toml          # maturin as build backend
├── crates/
│   ├── remitmatch/         # Pure Rust (core), published on crates.io as remitmatch
│   │   └── benches/        # criterion
│   └── remitmatch-py/      # PyO3: #[pyclass], Arrow conversion, GIL release; publish = false
├── python/remitmatch/      # __init__.py, .pyi stubs, py.typed
├── tests/                  # pytest + hypothesis
├── benchmarks/             # comparison against Python, pandas and reconPy
└── docs/                   # mkdocs-material
```

| Component | Choice | Reason |
| --- | --- | --- |
| Bindings | PyO3 + maturin | De facto standard; generates wheels for Linux, macOS and Windows |
| ABI | `abi3` (Python ≥ 3.10) | One wheel per platform works for all Python versions |
| Parallelism | rayon, one task per partition | Scales with cores; the GIL is released during the whole computation |
| References | `aho-corasick` crate | Search for thousands of invoice numbers in one pass |
| Similarity | `strsim` crate (Jaro-Winkler) | Simple and without heavy dependencies |
| Arrow | `arrow-rs` + PyCapsule interface | Zero-copy input and output from Polars or pandas |
| CI | GitHub Actions + `maturin-action` | Rust and Python tests, wheels per platform |
| Publishing | Trusted Publishing on PyPI | No tokens; same flow you already use in your packages |

Quality: `clippy` and `rustfmt` in Rust, `ruff` and `mypy` in Python, and coverage with Codecov as in django-graphex.

## Tests and benchmarks

A synthetic data generator with a known solution serves both the accuracy tests and the benchmarks.

**Generator.** It creates N invoices and their payments with controlled noise: bank fees, misspelled references, abbreviated names, partial payments and grouped payments. It stores the correct solution so that hits can be measured.

**Tests**

- Properties (proptest in Rust, hypothesis in Python): no id appears in two matches; the sum of each group respects the tolerance; currencies and scopes are never matched across.
- Golden cases: small hand-written files for each match type and each edge case.
- Precision and recall against the generator's solution, per match type.

**Benchmarks**

| Dimension | Values |
| --- | --- |
| Size | 10 thousand, 100 thousand and 1 million payments |
| Compared against | Pure Python, pandas + rapidfuzz, reconPy |
| Metrics | Total time, peak memory, precision and recall |
| Tools | criterion (Rust), pytest-benchmark (Python) |

The results go in the README as a table and chart, with the script to reproduce them. It is what will be shared the most on LinkedIn.

## Integration with InvoiceHub

InvoiceHub is the library's first real customer: the user uploads a bank statement and the system marks invoices as paid (see [RFC 0005](0005-reconciliation.md)).

1. The user uploads the statement (CSV in v1) and it is stored in file storage.
2. A Celery task parses it, loads the tenant's open invoices and calls `reconcile()`.
3. `auto` matches are applied directly; `suggested` and `ambiguous` go to a review queue.
4. In the queue, the UI (HTMX) shows the score breakdown and the user accepts, rejects or reassigns.
5. For `ambiguous` ones, an LLM can explain in natural language why it is unsure and which one it proposes. The AI only steps in on the hard cases, and never decides on its own.

This way InvoiceHub showcases Rust, Celery, multi-tenancy and applied AI together in a single flow.

## Milestones and open decisions

The library takes weeks 3 to 6 of the 12-week plan, in parallel with the InvoiceHub foundation.

```mermaid
gantt
    title The library is published on PyPI at the end of week 5
    dateFormat  X
    axisFormat  S%s
    section Weeks 3–6
    1:1 core (model, normalization, reference, scoring, generator)       :2, 3
    Grouped payments (1:N, N:1, subset-sum, assignment, properties)      :3, 4
    Bindings and PyPI (PyO3, stubs, Arrow, wheels, 0.1.0)                :4, 5
    Benchmarks and demo (README, docs, InvoiceHub, LinkedIn)             :5, 6
    Gate: 0.1.0 published on PyPI                                         :milestone, 5, 0
```

If week 4 runs long, 0.1.0 is published with only 1:1 and 1:N, and N:1 goes into 0.2.0.

**Open decisions**

- [ ] Reserve the name by publishing an empty 0.0.1 on PyPI and crates.io
- [ ] Default score weights: tune them with the generator data
- [ ] Candidate limit for the group search (24 is an initial estimate)
- [ ] License: MIT like your other packages, or Apache-2.0 as is usual in Rust

## Alternatives considered

Rust with PyO3 is chosen because it is the only option that gives performance, a comfortable Python API and learning that transfers to the current ecosystem (Polars, Pydantic, Ruff) all at once.

| Alternative | In favor | Why it was rejected |
| --- | --- | --- |
| Pure Python + rapidfuzz | Faster to write; rapidfuzz is already C++ | The expensive part (indexes, groups, assignment) stays in Python and neither scales nor parallelizes |
| Polars without custom Rust | Vectorized, nothing to compile | 1:N and N:1 groups are not well expressed as dataframe operations |
| Cython or Numba | Direct integration with Python | Worse ergonomics for complex data structures and less value in the portfolio |
| Deploy hyperswitch-reconciliation | Rust engine from a payments company, with 1:1, 1:N and N:M | It is a separate service with its own API and Postgres; for InvoiceHub it would be one more piece to operate, and it is not a library called from Python |
| Use recon-kit from Rust | Careful design: integers and ambiguous matches sent to review | 1:1 only and no tolerances, by design; the groups and the bindings would have to be added anyway |
| Reuse OpenRec | Already proven Rust engine | It is a CLI with rules in Lua, not a library; no grouped invoice payments |
| Own service in Go or Rust via HTTP | Language independent | Network latency and extra deployment for something that is a pure function |

## Risks and mitigations

The main risk is the combinatorial explosion of the groups; it is bounded by design.

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Combinatorial explosion in subset-sum | Unpredictable times | `max_group_size`, candidate limit, meet-in-the-middle and time budget per payment |
| False positives in `auto` | Invoices wrongly marked as paid | High threshold, ambiguity margin and `auto` never for groups with several solutions |
| Poorly calibrated weights | Many unnecessary `suggested` | Calibrate with the generator and expose the weights in `MatchConfig` |
| Wheels per platform | Broken installation on some system | `abi3`, CI matrix with maturin-action and an install test of each wheel |
| 4-week scope | 0.1.0 is delayed | Plan B from the milestones section: N:1 moves to 0.2.0 |
| Real data differs from synthetic data | Unconvincing benchmarks | Add an anonymized or public dataset before 0.2.0 |
