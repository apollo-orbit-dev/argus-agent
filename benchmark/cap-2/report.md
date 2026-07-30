# Model-Capability Benchmark — `cap-2`

32 model(s), by param count. Chain = deterministic tool-chain pass-rate; Judge = Opus quality mean (0–3). A tier's line falling off below some size is the shelf.

| model | params (B) | mode | scaffold | disclosure | max_tok | valid | solved | answered | gap | no-tool shape | abort | T1 chain / judge | T2 chain / judge | T3 chain / judge | T4 chain / judge | overall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| gemma-4-E2B-it | 2 | native | on | off | 16384 | ok | 66% | 79% | 12% | `▁▁▁▂▁▁▁▁▁▁` | — | 100% / 3.0 | 83% / 2.7 | 71% / 2.3 | 29% / 1.5 | 69% / 2.4 |
| gemma-4-E2B-it | 2 | manual | on | off | 16384 | ok | 48% | 61% | 12% | `▁▁▂▁▅▁▂▄▃▂` | — | 100% / 2.8 | 75% / 2.6 | 21% / 1.2 | 7% / 0.7 | 48% / 1.9 |
| gemma-4-E2B-it | 2 | native | off | off | 16384 | ok | 55% | 61% | 5% | `▁▁▁▂▂▁▁▁▁▁` | n/a | 100% / 3.0 | 92% / 2.8 | 21% / 1.3 | 14% / 0.6 | 54% / 1.9 |
| gemma-4-E2B-it | 2 | manual | off | off | 16384 | ok | 52% | 59% | 7% | `▁▁▁▂▂▁▁▁▁▁` | n/a | 100% / 2.8 | 83% / 2.9 | 29% / 1.3 | 7% / 0.3 | 52% / 1.8 |
| Qwen2.5-3B-Instruct | 3 | native | on | off | 2048 | ok | 43% | 52% | 9% | `▂▂▁▁▁▁▁▁▁▂` | — | 83% / 2.6 | 83% / 2.3 | 14% / 1.0 | 14% / 0.7 | 46% / 1.7 |
| Qwen2.5-3B-Instruct | 3 | manual | on | off | 2048 | ok | 41% | 45% | 4% | `▁▁▁▁▁▁▁▁▁▁` | — | 100% / 2.5 | 83% / 1.9 | 14% / 1.0 | 14% / 0.9 | 50% / 1.6 |
| Qwen2.5-3B-Instruct | 3 | native | off | off | 2048 | ok | 39% | 41% | 2% | `▁▁▁▁▃▁▃▁▁▃` | n/a | 100% / 2.6 | 67% / 2.2 | 0% / 0.4 | 14% / 0.4 | 42% / 1.4 |
| Qwen2.5-3B-Instruct | 3 | manual | off | off | 2048 | ok | 38% | 38% | 0% | `▁▁▁▁▁▁▁▁▁▁` | n/a | 100% / 2.4 | 75% / 2.0 | 21% / 0.4 | 0% / 0.0 | 46% / 1.2 |
| gemma-4-E4B-it | 4 | native | on | off | 16384 | ok | 75% | 91% | 16%&nbsp;⚠ | `▁▁▁▂▃▁▁▁▁▁` | — | 100% / 3.0 | 75% / 2.8 | 79% / 2.6 | 57% / 2.2 | 77% / 2.6 |
| gemma-4-E4B-it | 4 | manual | on | off | 16384 | ok | 71% | 88% | 16%&nbsp;⚠ | `▁▁▁▁▁▁▁▁▁▁` | — | 100% / 3.0 | 83% / 2.7 | 79% / 2.6 | 57% / 2.3 | 79% / 2.6 |
| gemma-4-E4B-it | 4 | native | off | off | 16384 | ok | 55% | 64% | 9% | `▁▁▁▂▃▁▁▁▂▁` | n/a | 92% / 2.8 | 75% / 2.8 | 36% / 1.4 | 21% / 0.9 | 54% / 2.0 |
| gemma-4-E4B-it | 4 | manual | off | off | 16384 | ok | 59% | 66% | 7% | `▁▁▁▂▁▁▁▁▁▁` | n/a | 100% / 3.0 | 83% / 2.7 | 43% / 1.4 | 7% / 0.7 | 56% / 1.9 |
| gemma-4-26B-A4B-it | 26 | native | on | off | 16384 | ok | 86% | 93% | 7% | `▁▁▁▁▁▁▁▁▁▁` | — | 100% / 2.9 | 100% / 2.9 | 93% / 2.7 | 71% / 2.5 | 90% / 2.7 |
| gemma-4-26B-A4B-it | 26 | manual | on | off | 16384 | ok | 86% | 93% | 7% | `▁▁▁▁▁▁▁▁▁▁` | — | 100% / 3.0 | 92% / 3.0 | 93% / 2.8 | 71% / 2.4 | 88% / 2.8 |
| gemma-4-26B-A4B-it | 26 | native | off | off | 16384 | ok | 64% | 66% | 2% | `▁▁▁▁▁▁▁▁▁▁` | n/a | 100% / 3.0 | 100% / 2.9 | 50% / 1.3 | 21% / 0.8 | 65% / 2.0 |
| gemma-4-26B-A4B-it | 26 | manual | off | off | 16384 | ok | 64% | 66% | 2% | `▁▁▁▁▁▁▁▁▁▁` | n/a | 100% / 3.0 | 100% / 3.0 | 43% / 1.3 | 21% / 0.7 | 63% / 2.0 |
| Qwen3.6-27B-FP8 | 27 | native | off | off | 16384 | ok | 59% | 64% | 5% | `▃▁▁▁▃▁▁▁▁▁` | n/a | 100% / 2.6 | 83% / 2.9 | 50% / 1.3 | 14% / 0.9 | 60% / 1.9 |
| Qwen3.6-27B-FP8 | 27 | manual | off | off | 16384 | ok | 59% | 66% | 7% | `▃▁▁▁▂▁▁▁▁▁` | n/a | 92% / 2.6 | 92% / 2.8 | 43% / 1.3 | 21% / 0.9 | 60% / 1.9 |
| Qwen3.6-27B-FP8 | 27 | native | on | off | 16384 | ok | 62% | 66% | 4% | `▂▁▁▁▄▁▁▁▁▁` | 0% | 92% / 2.5 | 75% / 2.7 | 57% / 1.8 | 43% / 1.1 | 65% / 2.0 |
| Qwen3.6-27B-FP8 | 27 | manual | on | off | 16384 | ok | 68% | 77% | 9% | `▃▁▂▁▃▁▁▁▁▁` | 0% | 83% / 2.7 | 75% / 2.4 | 86% / 2.5 | 50% / 1.8 | 73% / 2.4 |
| Agents-A1-FP8 | 35 | native | on | off | 16384 | ok | 75% | 91% | 16%&nbsp;⚠ | `▃▂▁▁▃▁▁▁▁▁` | 0% | 83% / 2.9 | 83% / 3.0 | 86% / 2.8 | 57% / 2.2 | 77% / 2.7 |
| Agents-A1-FP8 | 35 | manual | on | off | 16384 | ok | 77% | 95% | 18%&nbsp;⚠ | `▂▂▁▁▃▁▁▁▁▁` | 0% | 83% / 2.9 | 83% / 2.9 | 86% / 2.8 | 57% / 2.4 | 77% / 2.7 |
| Agents-A1-FP8 | 35 | native | off | off | 16384 | ok | 61% | 66% | 5% | `▂▂▁▁▃▁▁▁▁▁` | n/a | 100% / 2.9 | 83% / 2.9 | 43% / 1.4 | 21% / 1.0 | 60% / 2.0 |
| Agents-A1-FP8 | 35 | manual | off | off | 16384 | ok | 54% | 70% | 16%&nbsp;⚠ | `▃▁▁▁▃▁▁▁▁▁` | n/a | 83% / 2.9 | 83% / 3.0 | 36% / 1.4 | 21% / 1.0 | 54% / 2.1 |
| Qwen3.6-35B-A3B-FP8 | 35 | native | off | off | 16384 | ok | 57% | 70% | 12% | `▂▁▁▁▂▁▁▁▁▁` | n/a | 92% / 2.8 | 92% / 3.0 | 29% / 1.4 | 21% / 1.1 | 56% / 2.1 |
| Qwen3.6-35B-A3B-FP8 | 35 | native | on | off | 16384 | ok | 68% | 91% | 23%&nbsp;⚠ | `▂▁▁▁▃▁▁▁▁▁` | 0% | 92% / 2.8 | 83% / 2.9 | 79% / 3.0 | 36% / 2.4 | 71% / 2.8 |
| Qwen3.6-35B-A3B-FP8 | 35 | manual | off | off | 16384 | ok | 59% | 62% | 4% | `▁▁▁▁▂▁▁▁▁▁` | n/a | 100% / 2.9 | 100% / 2.9 | 29% / 1.3 | 21% / 0.7 | 60% / 1.9 |
| Qwen3.6-35B-A3B-FP8 | 35 | manual | on | off | 16384 | ok | 75% | 91% | 16%&nbsp;⚠ | `▁▁▁▁▁▁▁▁▁▁` | 4% | 92% / 2.8 | 100% / 2.7 | 86% / 2.8 | 43% / 2.3 | 79% / 2.7 |
| deepseek-v4-flash | 284 | native | on | off | 2048 | ok | 75% | 94% | 20%&nbsp;⚠ | `▃▁▁▁▂▁▁▁▁▁` | 0% | 82% / 3.0 | 100% / 3.0 | 92% / 2.9 | 46% / 2.6 | 79% / 2.9 |
| deepseek-v4-flash | 284 | manual | on | off | 2048 | ok | 82% | 94% | 12% | `▁▁▁▁▁▁▁▁▁▁` | 0% | 100% / 3.0 | 100% / 2.9 | 92% / 2.8 | 46% / 2.7 | 83% / 2.9 |
| deepseek-v4-flash | 284 | native | off | off | 2048 | ok | 59% | 69% | 10% | `▁▁▁▁▃▁▁▁▁▁` | n/a | 100% / 2.9 | 82% / 2.9 | 25% / 1.2 | 23% / 0.9 | 55% / 2.0 |
| deepseek-v4-flash | 284 | manual | off | off | 2048 | ok | 61% | 69% | 8% | `▁▁▁▁▁▁▁▁▁▁` | n/a | 100% / 2.8 | 100% / 2.8 | 25% / 1.2 | 23% / 1.2 | 60% / 2.0 |

`max_tok` = the completion-token cap for the run. `—` = not recorded (runs predating this field; the standard-config default is 2048). Runs at different caps are not strictly comparable — a reasoning model can exhaust a low cap mid-thought, so a higher cap is a fairer read of its capability but a looser comparison across sizes.
`answered` = the judge accepted the answer (>= 2), whether or not the declared tools were used; `solved` additionally requires the tool chain. The GAP between `answered` and `solved` is the share of tasks the model got RIGHT WITHOUT using the declared tools — a reasoning model answering inside its reasoning pass. `—` = no judge verdict (unjudged tasks are not counted as vacuously answered).
`valid` = does this row measure the MODEL or the INSTRUMENT? `ok` = trustworthy. `DEGRADED` = the no-tool rate in the last third of runs is >= 3x the first third AND above 50% — a step change that never recovers, which is what a serving regression looks like and is NOT what a weak model looks like (a weak model fails *flat*). `ABORTED` = two consecutive canary failures stopped the run early; the partial numbers are kept only to preserve the onset for diagnosis. **A non-ok row must never be read as a measurement of the model.**
`PARTIAL n/m` = the run wrote n of m tasks and stopped (a crash, or a canary abort). The result is checkpointed after every task so completed work survives, but the battery is ordered T1..T4, which makes a partial arm biased EASY — its rates are NOT comparable to a full arm's, and it is excluded from the size curve entirely.
`gap` = `answered` - `solved`: the share of tasks answered correctly by a path the rubric did not prescribe. ⚠ marks >= 15%. This is NOT a failure — the run is real and so is the model. It is a warning that `solved` is measuring path-COMPLIANCE for this model, and that `answered` is the fairer number to compare across sizes. The bias grows with capability: a weak model cannot shortcut, a strong one does it constantly, so `solved` systematically understates the strong end of the curve.
`no-tool shape` = per-decile share of runs that called no tool, in run order, over tasks whose `expect` actually requires one (restraint tasks are excluded — for them calling nothing is correct). A rising staircase is an instrument failing mid-run; the flat aggregate that preceded this column concealed exactly that.
`abort` = share of runs the loop itself ended on `stuck_repeating` (an exact-repeat tool-call thrash); diagnostic only, not part of `solved`. `—` = predates the metric (no observer data was ever recorded for that result). `n/a` = observer disabled (baseline arm — `--baseline` turns `enable_observer` off, so it can't fire).
**A NOTE ON SERVING CONFIGURATION.** Two cells in this table were once badly wrong — `Qwen3.6-35B-A3B native/off` at 112/168 runs producing no usable tool call, and `Qwen3.6-27B native/on` at 95/168 — and both are now re-generated and valid. The cause was vLLM **speculative decoding** (`--speculative-config {"method":"mtp",...}`): draft tokens accepted that should not have been, corrupting output at unchanged latency. Removing that one flag took the 35B cell from 112/168 broken to 0/168 and its `solved` from 0.250 to 0.571, and the 27B cell from 95/168 to 9/168 and 0.357 to 0.625. It was NOT the chat template and NOT the tool-call parser — the payloads were token-corrupted, not mis-formatted, so no parser could recover them. Flat latency across the onset is the diagnostic tell: a resource problem slows down; speculative decoding does not. Manual-mode answers were degraded too, just less visibly. If a future row looks inexplicably bad in native mode, check the serving flags before anything else.
