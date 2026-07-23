# Model-Capability Benchmark — `cap-1`

4 model(s), by param count. Chain = deterministic tool-chain pass-rate; Judge = Opus quality mean (0–3). A tier's line falling off below some size is the shelf.

| model | params (B) | mode | max_tok | T1 chain / judge | T2 chain / judge | T3 chain / judge | T4 chain / judge | overall |
|---|---|---|---|---|---|---|---|---|
| Qwen2.5-3B-Instruct | 3 | manual | — | 100% / 3.0 | 40% / 1.6 | 29% / 1.2 | 60% / 2.0 | 58% / 2.0 |
| Qwen3.6-27B-FP8 | 27 | native | 16384 | 71% / 2.8 | 60% / 1.7 | 86% / 2.5 | 100% / 2.9 | 79% / 2.5 |
| Agents-A1-FP8 | 35 | native | 16384 | 86% / 2.9 | 80% / 3.0 | 86% / 2.3 | 100% / 2.9 | 88% / 2.8 |
| Agents-A1-FP8 | 35 | native | — | 86% / 2.8 | 80% / 3.0 | 86% / 2.3 | 100% / 2.5 | 88% / 2.6 |

`max_tok` = the completion-token cap for the run. `—` = not recorded (runs predating this field; the standard-config default is 2048). Runs at different caps are not strictly comparable — a reasoning model can exhaust a low cap mid-thought, so a higher cap is a fairer read of its capability but a looser comparison across sizes.
