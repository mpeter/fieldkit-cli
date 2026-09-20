# close-date-countdown Specification

## Purpose
Define the current behavioral contract for close-date-countdown, including required behavior, failure modes, and observable outcomes.
## Requirements
### Requirement: Countdown stakeholder gaps use canonical qualification floors

The close-date countdown watcher SHALL derive champion and economic-buyer minimum scores from the canonical propose-to-negotiate gate. Every emitted countdown alert SHALL list either element as a critical gap when its score is absent or below the canonical minimum, and SHALL omit it when the score meets the minimum.

#### Scenario: suspected stakeholder scores remain gaps

- **GIVEN** an alert-eligible pursuit with champion 1 and economic-buyer 1
- **WHEN** the watcher builds its countdown result
- **THEN** `critical_gaps` contains both `champion` and `economic_buyer`

#### Scenario: confirmed stakeholder scores meet the boundary

- **GIVEN** an alert-eligible pursuit with champion 2 and economic-buyer 2
- **WHEN** the watcher builds its countdown result
- **THEN** `critical_gaps` contains neither stakeholder element

### Requirement: Paper process is critical in the configured red tier

The watcher SHALL derive the paper-process minimum from the same canonical propose-to-negotiate gate. A red-tier countdown result SHALL list `paper_process` when its score is absent or below that minimum. Yellow and green results SHALL omit `paper_process` from critical gaps regardless of its score. Red-tier applicability SHALL use the tier already produced from configured countdown thresholds, not a separate hard-coded day cutoff.

#### Scenario: reported near-close case exposes both gaps

- **GIVEN** a pursuit classified red by the configured thresholds with economic-buyer 1 and paper-process 0
- **WHEN** the watcher renders the alert
- **THEN** `Critical gaps` includes `economic_buyer` and `paper_process`

#### Scenario: paper process at the minimum is not a gap

- **GIVEN** a red-tier pursuit with paper-process 2
- **WHEN** the watcher builds its countdown result
- **THEN** `critical_gaps` does not contain `paper_process`

#### Scenario: non-red pursuit does not escalate paper process

- **GIVEN** a pursuit classified yellow or green with paper-process 0
- **WHEN** the watcher builds its countdown result
- **THEN** `critical_gaps` does not contain `paper_process`
