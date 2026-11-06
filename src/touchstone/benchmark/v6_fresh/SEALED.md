# Sealed findings

The frozen configuration (`touchstone check`, criteria v6) judged the 26 cases of this
test once, on 2026-11-06, with gpt-6-luna through chat completions: 26 calls, no cache.
The expectations and their checks were committed first (6e5956e).

The findings stay outside the repository (`out/v6_fresh/findings.SEALED.jsonl`,
git-ignored) and were not opened before this hash was committed:

    8e4fa05136b1178d8848528e58e3aed4e6916c70a03b625599607c34c64152fe

`../harness.py score` refuses findings that do not match it.
