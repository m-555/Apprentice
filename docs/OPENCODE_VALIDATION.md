# OpenCode migration validation — 2026-09-05

This validation covers Apprentice 0.3 / its VS Code extension 0.2, not a release
validation of the separate Local Agents extension or Unreal PromptToGame plugin.

- Existing Python suite: **73/73** passed.
- New migration contracts: **24/24**, including a real OpenCode 1.18.25 tool loop
  against a scripted HTTP model (no GPU required).
- Extension Node tests: **32/32**, including renderer state and safe code formatting.
- TypeScript typecheck and extension bundling passed.
- Actual VS Code extension host: activation, catalog/model selection, multiline Ask,
  Build using native read/edit tools, independent acceptance command, native Resume,
  task diff documents, pending approvals across panel reload, denial, Stop aborting
  upstream HTTP, and concurrent New Session requests all passed.

## Live local-model smoke tests

Both models used the existing shared llama.cpp supervisor on the user's machine.
No offload, model weight, context-size, or idle-unload settings were retuned.
Tasks ran in a disposable Git repository containing a deliberately incorrect
`add(a,b)` function. These are small functional probes, not representative project
benchmarks or tokens-per-second measurements.

| Probe | Qwen 3.8 Q8 | DeepSeek V4 Flash IQ2_M |
| --- | --- | --- |
| Read-only explanation | Passed, 25.67 s | Passed, 321.80 s |
| One-line fix + independent tests | Passed, 16.58 s | Passed, 322.92 s |
| Cancel active model request | Released in 0.39 s | Released in 0.34 s |
| Manual unload after tests | Passed | Passed |

Both gave short, relevant final answers with no code edits in Ask. Build changed the
subtraction to addition and passed numeric assertions. No repeated no-answer loop
occurred in these probes. End-to-end times include task startup, prompt processing,
tool turns and checks; the first request also includes cold model loading. Other
local validation work was running, so these are observations, not throughput promises.

After testing, supervisor health reported `model=null`, `embeddings=idle`, and
`activeRequests=0`. GPU memory was approximately 1.4 GiB for the remaining desktop/apps,
not a loaded test model. Live JSON reports are local under `outputs/live-opencode-*.json`.

Not established: long-context quality on arbitrary production repositories, equivalent
capability to frontier coding models, Qwen Coder live performance, paid-provider live
behavior, Unreal compilation, or security isolation from approved shell commands.
