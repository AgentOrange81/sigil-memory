---
name: Task
about: A unit of work with an owner and an acceptance gate
title: "[task] "
labels: ""
assignees: ""
---

## Goal

<!-- one sentence: what exists after this that did not before -->

## Acceptance criteria

<!-- the gate. Each line must be checkable by someone who did not write the code -->

- [ ]
- [ ]

## Owner

<!-- agent:cipher (builder) / agent:vector (QA) / agent:glyph (docs) / agent:grok (research) / glitch (orchestrator) -->

## Path ownership

<!-- files/dirs this task may touch. Anything else is out of scope for this branch. -->

- `quipu/...`
- `tests/...`

## Do NOT

- commit, push, or merge (the orchestrator does that)
- add runtime dependencies
- touch files outside the ownership list
- claim success without the gate output
