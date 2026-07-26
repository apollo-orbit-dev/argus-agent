# Model-Capability Benchmark — `cap-2`

28 model(s), by param count. Chain = deterministic tool-chain pass-rate; Judge = Opus quality mean (0–3). A tier's line falling off below some size is the shelf.

| model | params (B) | mode | scaffold | disclosure | max_tok | solved | answered | abort | T1 chain / judge | T2 chain / judge | T3 chain / judge | T4 chain / judge | overall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| gemma-4-E2B-it | 2 | native | on | off | 16384 | 66% | 79% | — | 100% / 3.0 | 83% / 2.7 | 71% / 2.1 | 29% / 1.6 | 69% / 2.4 |
| gemma-4-E2B-it | 2 | manual | on | off | 16384 | 48% | 57% | — | 100% / 2.8 | 75% / 2.5 | 21% / 1.3 | 7% / 0.7 | 48% / 1.8 |
| gemma-4-E2B-it | 2 | native | off | off | 16384 | 54% | 59% | n/a | 100% / 3.0 | 92% / 2.7 | 21% / 1.0 | 14% / 0.6 | 54% / 1.8 |
| gemma-4-E2B-it | 2 | manual | off | off | 16384 | 52% | 57% | n/a | 100% / 2.8 | 83% / 2.7 | 29% / 1.3 | 7% / 0.4 | 52% / 1.8 |
| Qwen2.5-3B-Instruct | 3 | native | on | off | 2048 | 41% | 46% | — | 83% / 2.5 | 83% / 2.2 | 14% / 1.0 | 14% / 0.8 | 46% / 1.6 |
| Qwen2.5-3B-Instruct | 3 | manual | on | off | 2048 | 43% | 45% | — | 100% / 2.5 | 83% / 2.0 | 14% / 1.0 | 14% / 0.7 | 50% / 1.5 |
| Qwen2.5-3B-Instruct | 3 | native | off | off | 2048 | 41% | 41% | n/a | 100% / 2.6 | 67% / 2.2 | 0% / 0.4 | 14% / 0.4 | 42% / 1.4 |
| Qwen2.5-3B-Instruct | 3 | manual | off | off | 2048 | 38% | 38% | n/a | 100% / 2.3 | 75% / 2.0 | 21% / 0.4 | 0% / 0.0 | 46% / 1.2 |
| gemma-4-E4B-it | 4 | native | on | off | 16384 | 75% | 89% | — | 100% / 3.0 | 75% / 2.5 | 79% / 2.6 | 57% / 2.2 | 77% / 2.6 |
| gemma-4-E4B-it | 4 | manual | on | off | 16384 | 73% | 89% | — | 100% / 3.0 | 83% / 2.6 | 79% / 2.6 | 57% / 2.3 | 79% / 2.6 |
| gemma-4-E4B-it | 4 | native | off | off | 16384 | 55% | 64% | n/a | 92% / 2.8 | 75% / 2.7 | 36% / 1.3 | 21% / 0.9 | 54% / 1.9 |
| gemma-4-E4B-it | 4 | manual | off | off | 16384 | 59% | 64% | n/a | 100% / 3.0 | 83% / 2.7 | 43% / 1.3 | 7% / 0.6 | 56% / 1.9 |
| gemma-4-26B-A4B-it | 26 | native | on | off | 16384 | 88% | 93% | — | 100% / 2.9 | 100% / 2.9 | 93% / 2.7 | 71% / 2.4 | 90% / 2.8 |
| gemma-4-26B-A4B-it | 26 | manual | on | off | 16384 | 86% | 89% | — | 100% / 3.0 | 92% / 2.8 | 93% / 2.9 | 71% / 2.5 | 88% / 2.8 |
| gemma-4-26B-A4B-it | 26 | native | off | off | 16384 | 64% | 66% | n/a | 100% / 3.0 | 100% / 2.9 | 50% / 1.4 | 21% / 0.8 | 65% / 2.0 |
| gemma-4-26B-A4B-it | 26 | manual | off | off | 16384 | 64% | 66% | n/a | 100% / 3.0 | 100% / 3.0 | 43% / 1.3 | 21% / 0.6 | 63% / 2.0 |
| Qwen3.6-27B-FP8 | 27 | native | on | off | 16384 | 36% | 43% | 0% | 75% / 2.2 | 42% / 1.8 | 29% / 0.8 | 0% / 0.0 | 35% / 1.2 |
| Qwen3.6-27B-FP8 | 27 | manual | on | off | 16384 | 77% | 84% | 0% | 92% / 2.5 | 83% / 2.3 | 86% / 2.6 | 79% / 2.6 | 85% / 2.5 |
| Qwen3.6-27B-FP8 | 27 | native | off | off | 16384 | 59% | 70% | n/a | 92% / 2.8 | 83% / 2.9 | 57% / 1.5 | 21% / 1.0 | 62% / 2.1 |
| Qwen3.6-27B-FP8 | 27 | manual | off | off | 16384 | 36% | 45% | n/a | 83% / 2.6 | 92% / 1.9 | 14% / 0.3 | 7% / 0.2 | 46% / 1.3 |
| Agents-A1-FP8 | 35 | native | on | off | 16384 | 75% | 89% | 0% | 83% / 2.7 | 83% / 3.0 | 86% / 2.7 | 57% / 2.2 | 77% / 2.7 |
| Agents-A1-FP8 | 35 | manual | on | off | 16384 | 79% | 95% | 0% | 83% / 2.7 | 83% / 2.8 | 86% / 2.7 | 57% / 2.5 | 77% / 2.7 |
| Agents-A1-FP8 | 35 | native | off | off | 16384 | 61% | 68% | n/a | 100% / 2.7 | 83% / 2.8 | 43% / 1.3 | 21% / 1.1 | 60% / 2.0 |
| Agents-A1-FP8 | 35 | manual | off | off | 16384 | 54% | 66% | n/a | 83% / 2.8 | 83% / 3.0 | 36% / 1.3 | 21% / 1.0 | 54% / 2.0 |
| Qwen3.6-35B-A3B-FP8 | 35 | native | on | off | 16384 | 70% | 84% | 0% | 83% / 2.9 | 83% / 2.9 | 64% / 2.1 | 57% / 1.8 | 71% / 2.4 |
| Qwen3.6-35B-A3B-FP8 | 35 | manual | on | off | 16384 | 79% | 89% | 2% | 100% / 3.0 | 100% / 2.8 | 79% / 2.5 | 57% / 2.1 | 83% / 2.6 |
| Qwen3.6-35B-A3B-FP8 | 35 | native | off | off | 16384 | 25% | 29% | n/a | 83% / 2.6 | 0% / 0.8 | 0% / 0.0 | 0% / 0.1 | 19% / 0.9 |
| Qwen3.6-35B-A3B-FP8 | 35 | manual | off | off | 16384 | 48% | 59% | n/a | 100% / 2.9 | 75% / 2.4 | 21% / 1.0 | 21% / 0.9 | 52% / 1.8 |

`max_tok` = the completion-token cap for the run. `—` = not recorded (runs predating this field; the standard-config default is 2048). Runs at different caps are not strictly comparable — a reasoning model can exhaust a low cap mid-thought, so a higher cap is a fairer read of its capability but a looser comparison across sizes.
`answered` = the judge accepted the answer (>= 2), whether or not the declared tools were used; `solved` additionally requires the tool chain. The GAP between `answered` and `solved` is the share of tasks the model got RIGHT WITHOUT using the declared tools — a reasoning model answering inside its reasoning pass. `—` = no judge verdict (unjudged tasks are not counted as vacuously answered).
`abort` = share of runs the loop itself ended on `stuck_repeating` (an exact-repeat tool-call thrash); diagnostic only, not part of `solved`. `—` = predates the metric (no observer data was ever recorded for that result). `n/a` = observer disabled (baseline arm — `--baseline` turns `enable_observer` off, so it can't fire).
