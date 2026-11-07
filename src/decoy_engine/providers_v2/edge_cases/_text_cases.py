"""String catalogues: ``edge_case_string`` (ASCII) and ``edge_case_unicode``.

Categories follow the Big List of Naughty Strings sections (reference only; every value here is
written for this catalogue) and, for Unicode, UAX #15 normalization, UAX #9 bidi and UTS #39
confusables. The rules every value passes are in ``_spec.py``.
"""

from __future__ import annotations

from decoy_engine.providers_v2.edge_cases._spec import EdgeCaseSpec, cases


def _pattern(length: int) -> str:
    # A repeating, readable pattern makes truncation visible in the output.
    return ("abcdefghij" * (length // 10 + 1))[:length]


STRING_SPEC = EdgeCaseSpec(
    provider="edge_case_string",
    kind="ascii_text",
    categories=(
        "empty",
        "whitespace",
        "line_breaks",
        "quotes_and_delimiters",
        "null_like_tokens",
        "injection_text",
        "length_boundaries",
    ),
    cases=(
        *cases("empty", ""),
        *cases("whitespace", " ", "   ", "\t", "  leading", "trailing  "),
        *cases("line_breaks", "a\nb", "a\r\nb", "\n", "trailing newline\n", "a\tb"),
        *cases(
            "quotes_and_delimiters",
            "O'Brien",
            '"quoted"',
            "it''s",
            "\\",
            "C:\\path\\file",
            "a,b",
        ),
        *cases(
            "null_like_tokens",
            "null",
            "NULL",
            "None",
            "nil",
            "undefined",
            "NaN",
            "N/A",
            "NA",
            "#N/A",
            "-",
            "\\N",
            "true",
            "FALSE",
            "0",
            "-0",
            "1e3",
            "0x1A",
            "007",
            "1,000",
            "+1",
        ),
        *cases(
            "injection_text",
            # Always-false predicates: a spliced value narrows a WHERE, never widens it.
            "' AND '1'='2",
            '" AND "1"="2',
            "1 AND 1=2",
            # No src or srcset: the image selects no source, fetches nothing and fires no error.
            "<img onerror=alert(1)>",
            "{{7*7}}",
            "${7*7}",
            "#{7*7}",
            "100%%",
            "../../../../decoy-nonexistent",
            "=1+1",
            "+1+1",
            "-1+1",
            "@SUM(1,1)",
        ),
        *cases(
            "length_boundaries",
            *(_pattern(n) for n in (1, 255, 256, 4000, 4001, 32767, 32768)),
        ),
    ),
)

UNICODE_SPEC = EdgeCaseSpec(
    provider="edge_case_unicode",
    kind="unicode_text",
    categories=(
        "accented_latin",
        "normalization_variants",
        "combining_marks",
        "case_mapping",
        "bidi",
        "invisible",
        "emoji_and_astral",
        "scripts",
        "homoglyphs",
        "replacement",
    ),
    cases=(
        *cases("accented_latin", "Zo\u00eb", "\u0141\u00f3d\u017a", "na\u00efve", "Fran\u00e7ois"),
        *cases(
            "normalization_variants",
            "caf\u00e9",  # NFC
            "cafe\u0301",  # NFD of the same text
            "\u00c5",  # A WITH RING ABOVE
            "A\u030a",  # its decomposition
            "\u212b",  # ANGSTROM SIGN: canonically equal to U+00C5
            "\ufb01le",  # fi ligature: NFKC gives "file"
            "\uff21\uff22\uff23\uff11\uff12\uff13",  # full-width ABC123
            "\uac00",  # Hangul syllable GA
            "\u1100\u1161",  # its conjoining jamo
            "x\u00b2",  # superscript two: NFKC gives "x2"
        ),
        *cases(
            "combining_marks",
            "e\u0301\u0302\u0303\u0304",
            "Z\u0351\u0342\u0300a\u0310\u0301l\u0300\u0306g\u0303\u0308o\u0304\u0327",
            "\u0915\u094d\u0937",  # Devanagari conjunct KSSA
        ),
        *cases(
            "case_mapping",
            "Stra\u00dfe",  # upper() is STRASSE: longer
            "\u0130",  # lower() is two code points
            "\u0131",  # dotless i
            "\u03c3\u03b1\u03c2",  # final sigma
            "\u01c5",  # titlecase digraph
        ),
        *cases(
            "bidi",
            "\u05e9\u05dc\u05d5\u05dd",
            "\u0645\u0631\u062d\u0628\u0627",
            "abc \u05e9\u05dc\u05d5\u05dd 123",
            "\u202eabc\u202c",  # override, closed so the rest of the line is unaffected
            "a\u200fb",
            "\u2067abc\u2069",
            "\u0661\u0662\u0663",
        ),
        *cases(
            "invisible",
            "a\u200bb",
            "\u200b",
            "a\u200cb",
            "a\u200db",
            "\ufeffbom",
            "a\u2060b",
            "soft\u00adhyphen",
            "\u00a0",
            "\u3000",
            "a\u2028b",
            "a\u2029b",
        ),
        *cases(
            "emoji_and_astral",
            "\U0001f600",
            "\U0001f44d\U0001f3fd",
            "\U0001f468\u200d\U0001f469\u200d\U0001f467",
            "\U0001f1ec\U0001f1e7",
            "\U0001d573\U0001d58a\U0001d591\U0001d591\U0001d594",
            "\U0002070e",
            "\U00013000",
            "a\U0001f600b",
        ),
        *cases(
            "scripts",
            "\u65e5\u672c\u8a9e",
            "\u4e2d\u6587",
            "\ud55c\uad6d\uc5b4",
            "\u0e44\u0e17\u0e22",
            "\u0395\u03bb\u03bb\u03b7\u03bd\u03b9\u03ba\u03ac",
            "\u0420\u0443\u0441\u0441\u043a\u0438\u0439",
            "\u0939\u093f\u0928\u094d\u0926\u0940",
        ),
        *cases(
            "homoglyphs",
            "\u0430dmin",  # Cyrillic a
            "\u0391BC",  # Greek capital alpha
            "\u22125",  # minus sign
            "a\u2010b",  # hyphen
            "O\u2019Brien",
            "it\uff07s",  # full-width apostrophe
            "\u201cquoted\u201d",
        ),
        *cases("replacement", "\ufffd"),
    ),
)
