### Changed (lazy conditionals and the `if` call in derived expressions, GEN-TYPES E4, 2026-11-11)

`derived` expressions accept `if(condition, value, other)`, the call form of the ternary
`value if condition else other`. Conditionals now evaluate lazily: `ternary`, `if(...)`,
`case_when`, `or` and `and` run only the branch they choose, so `if(a == 0, 0, 100 // a)` yields 0
for `a == 0` instead of failing. A row that evaluated before gives the same value; a row that
failed only because of an unchosen branch now evaluates. Mask-side `derived` and `case_when` share
the evaluator and change the same way. A missing column inside an unchosen branch is still refused
before any row runs. No registry code is added.
