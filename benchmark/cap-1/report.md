# Model-Capability Benchmark — `cap-1`

18 model(s), by param count. Chain = deterministic tool-chain pass-rate; Judge = Opus quality mean (0–3). A tier's line falling off below some size is the shelf.

| model | params (B) | mode | scaffold | disclosure | max_tok | solved | answered | abort | T1 chain / judge | T2 chain / judge | T3 chain / judge | T4 chain / judge | overall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Qwen2.5-3B-Instruct | 3 | manual | off | off | 2048 | 48% | 56% | n/a | 100% / 3.0 | 40% / 1.8 | 29% / 0.2 | 60% / 1.8 | 58% / 1.7 |
| Qwen2.5-3B-Instruct | 3 | manual | on | off | 2048 | 67% | 74% | — | 100% / 3.0 | 40% / 2.0 | 57% / 1.6 | 60% / 1.8 | 67% / 2.1 |
| Qwen2.5-3B-Instruct | 3 | native | on | off | 2048 | 59% | 63% | — | 100% / 3.0 | 40% / 1.6 | 43% / 1.0 | 100% / 2.0 | 71% / 1.9 |
| Qwen2.5-3B-Instruct | 3 | native | off | off | 2048 | 63% | 63% | n/a | 100% / 3.0 | 40% / 1.7 | 29% / 0.8 | 60% / 2.0 | 58% / 1.9 |
| Qwen2.5-3B-Instruct | 3 | manual | on | off | — | 56% | 63% | — | 100% / 3.0 | 40% / 1.6 | 29% / 1.2 | 60% / 2.0 | 58% / 2.0 |
| gemma-4-E4B-it | 4 | native | on | off | 16384 | 81% | 93% | — | 86% / 2.7 | 60% / 2.7 | 86% / 2.2 | 80% / 2.7 | 79% / 2.6 |
| gemma-4-E4B-it | 4 | manual | on | off | 16384 | 81% | 89% | — | 100% / 2.7 | 60% / 2.7 | 86% / 2.6 | 80% / 2.7 | 83% / 2.7 |
| gemma-4-E4B-it | 4 | native | off | off | 16384 | 44% | 52% | n/a | 86% / 2.5 | 40% / 2.0 | 43% / 0.0 | 60% / 2.3 | 58% / 1.7 |
| gemma-4-E4B-it | 4 | manual | off | off | 16384 | 67% | 74% | n/a | 100% / 2.9 | 60% / 2.7 | 29% / 0.1 | 100% / 2.9 | 71% / 2.1 |
| gemma-4-26B-A4B-it | 26 | native | on | off | 16384 | 78% | 78% | — | 100% / 3.0 | 60% / 2.3 | 71% / 1.6 | 80% / 2.4 | 79% / 2.3 |
| gemma-4-26B-A4B-it | 26 | manual | on | off | 16384 | 93% | 96% | — | 100% / 3.0 | 80% / 3.0 | 86% / 2.5 | 100% / 3.0 | 92% / 2.9 |
| gemma-4-26B-A4B-it | 26 | native | off | off | 16384 | 78% | 81% | n/a | 100% / 3.0 | 80% / 2.8 | 86% / 1.0 | 100% / 3.0 | 92% / 2.4 |
| gemma-4-26B-A4B-it | 26 | manual | off | off | 16384 | 70% | 74% | n/a | 100% / 3.0 | 80% / 2.8 | 86% / 0.5 | 100% / 2.6 | 92% / 2.2 |
| gemma-4-26B-A4B-it | 26 | native | on | off | 16384 | 78% | 81% | — | 100% / 3.0 | 60% / 2.5 | 71% / 1.6 | 80% / 2.4 | 79% / 2.4 |
| gemma-4-26B-A4B-it | 26 | native | off | off | 16384 | 74% | 78% | n/a | 100% / 3.0 | 80% / 3.0 | 86% / 1.0 | 100% / 2.7 | 92% / 2.4 |
| Qwen3.6-27B-FP8 | 27 | native | on | off | 16384 | 78% | 89% | — | 71% / 2.8 | 60% / 1.7 | 86% / 2.5 | 100% / 2.9 | 79% / 2.5 |
| Agents-A1-FP8 | 35 | native | on | off | 16384 | 89% | 96% | — | 86% / 2.9 | 80% / 3.0 | 86% / 2.3 | 100% / 2.9 | 88% / 2.8 |
| Agents-A1-FP8 | 35 | native | on | off | — | 81% | 93% | — | 86% / 2.8 | 80% / 3.0 | 86% / 2.3 | 100% / 2.5 | 88% / 2.6 |

`max_tok` = the completion-token cap for the run. `—` = not recorded (runs predating this field; the standard-config default is 2048). Runs at different caps are not strictly comparable — a reasoning model can exhaust a low cap mid-thought, so a higher cap is a fairer read of its capability but a looser comparison across sizes.
`answered` = the judge accepted the answer (>= 2), whether or not the declared tools were used; `solved` additionally requires the tool chain. The GAP between `answered` and `solved` is the share of tasks the model got RIGHT WITHOUT using the declared tools — a reasoning model answering inside its reasoning pass. `—` = no judge verdict (unjudged tasks are not counted as vacuously answered).
`abort` = share of runs the loop itself ended on `stuck_repeating` (an exact-repeat tool-call thrash); diagnostic only, not part of `solved`. `—` = predates the metric (no observer data was ever recorded for that result). `n/a` = observer disabled (baseline arm — `--baseline` turns `enable_observer` off, so it can't fire).
