"""The only modules that hand a policy-issued capability to a third-party file API.

Each function accepts a `FileHandle`, `DirHandle`, `OutputDir` or `OutputTxn` and passes a
library only `handle.library_path()`, `handle.fileno()` or a descriptor from an output
transaction. Under a strict policy a library error or warning is replaced by the one denial
(`_errors.guarded`), so a locator or path never reaches a message.
"""
