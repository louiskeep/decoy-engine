# Crypto-PAn reference trace

`sample_trace_raw.dat` and `sample_trace_sanitized.dat` are the 100-packet sample trace shipped
with the Crypto-PAn 1.0 reference implementation (Jinliang Fan, Georgia Tech, 2002) and its
anonymised output under the reference key
`21 34 23 141 51 164 207 128 19 10 91 22 73 144 125 16 216 152 143 131 121 121 101 39 98 87 76 45 42 132 34 2`.
Columns are tab-separated: timestamp, packet size, IPv4 address. Only the third column is used.

Copied unchanged from the `testdata/` directory of yacryptopan
(`github.com/diekmann/yacryptopan`, BSD licence), whose test suite checks against the same pairs.
They pin `decoy_engine.transforms.network._cryptopan` to the published algorithm (Xu, Fan, Ammar and
Moon, "Prefix-Preserving IP Address Anonymization", ICNP 2002).
