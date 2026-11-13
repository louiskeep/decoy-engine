### Fixed (a memory error behind an interrupt is classified out of memory, 2026-11-13)

An isolated run whose child hits its memory cap and surfaces the failure as a `KeyboardInterrupt` chained from a `MemoryError` (an Arrow cancellation can do this) is now classified `oom_killed`, so the governor can reroute it, instead of `crashed`. Only exception types along the `__cause__`/`__context__` chain are read, never their text. A bare interrupt stays `crashed`. A self-reported crash sampled near its cap now carries `suspected_memory_pressure`.
