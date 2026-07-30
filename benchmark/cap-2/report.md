# Model-Capability Benchmark — `cap-2`

28 model(s), by param count. Chain = deterministic tool-chain pass-rate; Judge = Opus quality mean (0–3). A tier's line falling off below some size is the shelf.

| model | params (B) | mode | scaffold | disclosure | max_tok | solved | answered | abort | T1 chain / judge | T2 chain / judge | T3 chain / judge | T4 chain / judge | overall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| gemma-4-E2B-it | 2 | native | on | off | 16384 | 66% | 79% | — | 100% / 3.0 | 83% / 2.7 | 71% / 2.3 | 29% / 1.5 | 69% / 2.4 |
| gemma-4-E2B-it | 2 | manual | on | off | 16384 | 48% | 61% | — | 100% / 2.8 | 75% / 2.6 | 21% / 1.2 | 7% / 0.7 | 48% / 1.9 |
| gemma-4-E2B-it | 2 | native | off | off | 16384 | 55% | 61% | n/a | 100% / 3.0 | 92% / 2.8 | 21% / 1.3 | 14% / 0.6 | 54% / 1.9 |
| gemma-4-E2B-it | 2 | manual | off | off | 16384 | 52% | 59% | n/a | 100% / 2.8 | 83% / 2.9 | 29% / 1.3 | 7% / 0.3 | 52% / 1.8 |
| Qwen2.5-3B-Instruct | 3 | native | on | off | 2048 | 43% | 52% | — | 83% / 2.6 | 83% / 2.3 | 14% / 1.0 | 14% / 0.7 | 46% / 1.7 |
| Qwen2.5-3B-Instruct | 3 | manual | on | off | 2048 | 41% | 45% | — | 100% / 2.5 | 83% / 1.9 | 14% / 1.0 | 14% / 0.9 | 50% / 1.6 |
| Qwen2.5-3B-Instruct | 3 | native | off | off | 2048 | 39% | 41% | n/a | 100% / 2.6 | 67% / 2.2 | 0% / 0.4 | 14% / 0.4 | 42% / 1.4 |
| Qwen2.5-3B-Instruct | 3 | manual | off | off | 2048 | 38% | 38% | n/a | 100% / 2.4 | 75% / 2.0 | 21% / 0.4 | 0% / 0.0 | 46% / 1.2 |
| gemma-4-E4B-it | 4 | native | on | off | 16384 | 75% | 91% | — | 100% / 3.0 | 75% / 2.8 | 79% / 2.6 | 57% / 2.2 | 77% / 2.6 |
| gemma-4-E4B-it | 4 | manual | on | off | 16384 | 71% | 88% | — | 100% / 3.0 | 83% / 2.7 | 79% / 2.6 | 57% / 2.3 | 79% / 2.6 |
| gemma-4-E4B-it | 4 | native | off | off | 16384 | 55% | 64% | n/a | 92% / 2.8 | 75% / 2.8 | 36% / 1.4 | 21% / 0.9 | 54% / 2.0 |
| gemma-4-E4B-it | 4 | manual | off | off | 16384 | 59% | 66% | n/a | 100% / 3.0 | 83% / 2.7 | 43% / 1.4 | 7% / 0.7 | 56% / 1.9 |
| gemma-4-26B-A4B-it | 26 | native | on | off | 16384 | 86% | 93% | — | 100% / 2.9 | 100% / 2.9 | 93% / 2.7 | 71% / 2.5 | 90% / 2.7 |
| gemma-4-26B-A4B-it | 26 | manual | on | off | 16384 | 86% | 93% | — | 100% / 3.0 | 92% / 3.0 | 93% / 2.8 | 71% / 2.4 | 88% / 2.8 |
| gemma-4-26B-A4B-it | 26 | native | off | off | 16384 | 64% | 66% | n/a | 100% / 3.0 | 100% / 2.9 | 50% / 1.3 | 21% / 0.8 | 65% / 2.0 |
| gemma-4-26B-A4B-it | 26 | manual | off | off | 16384 | 64% | 66% | n/a | 100% / 3.0 | 100% / 3.0 | 43% / 1.3 | 21% / 0.7 | 63% / 2.0 |
| Qwen3.6-27B-FP8 | 27 | native | off | off | 16384 | 59% | 64% | n/a | 100% / 2.6 | 83% / 2.9 | 50% / 1.3 | 14% / 0.9 | 60% / 1.9 |
| Qwen3.6-27B-FP8 | 27 | manual | off | off | 16384 | 59% | 66% | n/a | 92% / 2.6 | 92% / 2.8 | 43% / 1.3 | 21% / 0.9 | 60% / 1.9 |
| Qwen3.6-27B-FP8 | 27 | native | on | off | 16384 | 62% | 66% | 0% | 92% / 2.5 | 75% / 2.7 | 57% / 1.8 | 43% / 1.1 | 65% / 2.0 |
| Qwen3.6-27B-FP8 | 27 | manual | on | off | 16384 | 68% | 77% | 0% | 83% / 2.7 | 75% / 2.4 | 86% / 2.5 | 50% / 1.8 | 73% / 2.4 |
| Agents-A1-FP8 | 35 | native | on | off | 16384 | 75% | 91% | 0% | 83% / 2.9 | 83% / 3.0 | 86% / 2.8 | 57% / 2.2 | 77% / 2.7 |
| Agents-A1-FP8 | 35 | manual | on | off | 16384 | 77% | 95% | 0% | 83% / 2.9 | 83% / 2.9 | 86% / 2.8 | 57% / 2.4 | 77% / 2.7 |
| Agents-A1-FP8 | 35 | native | off | off | 16384 | 61% | 66% | n/a | 100% / 2.9 | 83% / 2.9 | 43% / 1.4 | 21% / 1.0 | 60% / 2.0 |
| Agents-A1-FP8 | 35 | manual | off | off | 16384 | 54% | 70% | n/a | 83% / 2.9 | 83% / 3.0 | 36% / 1.4 | 21% / 1.0 | 54% / 2.1 |
| Qwen3.6-35B-A3B-FP8 | 35 | native | off | off | 16384 | 57% | 70% | n/a | 92% / 2.8 | 92% / 3.0 | 29% / 1.4 | 21% / 1.1 | 56% / 2.1 |
| Qwen3.6-35B-A3B-FP8 | 35 | native | on | off | 16384 | 68% | 91% | 0% | 92% / 2.8 | 83% / 2.9 | 79% / 3.0 | 36% / 2.4 | 71% / 2.8 |
| Qwen3.6-35B-A3B-FP8 | 35 | manual | off | off | 16384 | 59% | 62% | n/a | 100% / 2.9 | 100% / 2.9 | 29% / 1.3 | 21% / 0.7 | 60% / 1.9 |
| Qwen3.6-35B-A3B-FP8 | 35 | manual | on | off | 16384 | 75% | 91% | 4% | 92% / 2.8 | 100% / 2.7 | 86% / 2.8 | 43% / 2.3 | 79% / 2.7 |

`max_tok` = the completion-token cap for the run. `—` = not recorded (runs predating this field; the standard-config default is 2048). Runs at different caps are not strictly comparable — a reasoning model can exhaust a low cap mid-thought, so a higher cap is a fairer read of its capability but a looser comparison across sizes.
`answered` = the judge accepted the answer (>= 2), whether or not the declared tools were used; `solved` additionally requires the tool chain. The GAP between `answered` and `solved` is the share of tasks the model got RIGHT WITHOUT using the declared tools — a reasoning model answering inside its reasoning pass. `—` = no judge verdict (unjudged tasks are not counted as vacuously answered).
`abort` = share of runs the loop itself ended on `stuck_repeating` (an exact-repeat tool-call thrash); diagnostic only, not part of `solved`. `—` = predates the metric (no observer data was ever recorded for that result). `n/a` = observer disabled (baseline arm — `--baseline` turns `enable_observer` off, so it can't fire).
**A NOTE ON SERVING CONFIGURATION.** Two cells in this table were once badly wrong — `Qwen3.6-35B-A3B native/off` at 112/168 runs producing no usable tool call, and `Qwen3.6-27B native/on` at 95/168 — and both are now re-generated and valid. The cause was vLLM **speculative decoding** (`--speculative-config {"method":"mtp",...}`): draft tokens accepted that should not have been, corrupting output at unchanged latency. Removing that one flag took the 35B cell from 112/168 broken to 0/168 and its `solved` from 0.250 to 0.571, and the 27B cell from 95/168 to 9/168 and 0.357 to 0.625. It was NOT the chat template and NOT the tool-call parser — the payloads were token-corrupted, not mis-formatted, so no parser could recover them. Flat latency across the onset is the diagnostic tell: a resource problem slows down; speculative decoding does not. Manual-mode answers were degraded too, just less visibly. If a future row looks inexplicably bad in native mode, check the serving flags before anything else.
