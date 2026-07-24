# Model-Capability Benchmark — `cap-2`

8 model(s), by param count. Chain = deterministic tool-chain pass-rate; Judge = Opus quality mean (0–3). A tier's line falling off below some size is the shelf.

| model | params (B) | mode | scaffold | max_tok | solved | T1 chain / judge | T2 chain / judge | T3 chain / judge | T4 chain / judge | overall |
|---|---|---|---|---|---|---|---|---|---|---|
| Qwen2.5-3B-Instruct | 3 | native | on | 2048 | 41% | 83% / 2.5 | 83% / 2.2 | 14% / 1.0 | 14% / 0.8 | 46% / 1.6 |
| Qwen2.5-3B-Instruct | 3 | manual | on | 2048 | 43% | 100% / 2.5 | 83% / 2.0 | 14% / 1.0 | 14% / 0.7 | 50% / 1.5 |
| Qwen2.5-3B-Instruct | 3 | native | off | 2048 | 41% | 100% / 2.6 | 67% / 2.2 | 0% / 0.4 | 14% / 0.4 | 42% / 1.4 |
| Qwen2.5-3B-Instruct | 3 | manual | off | 2048 | 38% | 100% / 2.3 | 75% / 2.0 | 21% / 0.4 | 0% / 0.0 | 46% / 1.2 |
| gemma-4-26B-A4B-it | 26 | native | on | 16384 | 88% | 100% / 2.9 | 100% / 2.9 | 93% / 2.7 | 71% / 2.4 | 90% / 2.8 |
| gemma-4-26B-A4B-it | 26 | manual | on | 16384 | 86% | 100% / 3.0 | 92% / 2.8 | 93% / 2.9 | 71% / 2.5 | 88% / 2.8 |
| gemma-4-26B-A4B-it | 26 | native | off | 16384 | 64% | 100% / 3.0 | 100% / 2.9 | 50% / 1.4 | 21% / 0.8 | 65% / 2.0 |
| gemma-4-26B-A4B-it | 26 | manual | off | 16384 | 64% | 100% / 3.0 | 100% / 3.0 | 43% / 1.3 | 21% / 0.6 | 63% / 2.0 |

`max_tok` = the completion-token cap for the run. `—` = not recorded (runs predating this field; the standard-config default is 2048). Runs at different caps are not strictly comparable — a reasoning model can exhaust a low cap mid-thought, so a higher cap is a fairer read of its capability but a looser comparison across sizes.
