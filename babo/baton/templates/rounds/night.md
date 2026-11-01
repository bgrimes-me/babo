---
title: The night round
when: model-free
runs: wait
steps:
  - office: archivist
    or: mayor
    duty: Curate the notes waiting in the promotion queue into the commons, so it stays small, current and easy to find your way around.
    material: promotions
  - office: inspector
    duty: Review today's changes against the residents' checkpoints, and the pending tools and open proposals, for risk.
    material: inspection
---
The town's work that used to be part of upkeep. While the town has no Archivist, the Mayor curates; while it has no Inspector, tools and proposals still reach {{ owner }}, without its review. It runs in each upkeep pass that has work for it, and starts one itself when there's work waiting.
