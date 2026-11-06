# Sealed verdicts

The frozen evaluation configuration (commit 2778e2a) judged the 30 statements of
`pilot/uses.jsonl` once each, on 2026-11-06, with gpt-6-luna through chat completions:
criteria v5, and criteria v4 as the baseline on the same statements. 60 calls, 0
errors, none from cache.

The statements and the answer key (`answer_key.jsonl`) were committed (7e0f834) before
either run. The verdicts are kept outside the repository (`out/v5_fresh/`,
git-ignored); their sha256:

    criteria v5  7268e7eabdd5ee5fbf2d02fb01c123e33d37d3340dfbedfe4a8c1371073c2e7c
    criteria v4  903c3c9f04ad13118fb4da56146c4886b9ef30461c84844090d0ba60a92b574e

`score.py` refuses a verdicts file that does not match one of them.
