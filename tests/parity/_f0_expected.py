"""Checked-in expected fingerprints for the Phase F F0 generation oracle (data only).

Captured ONCE from the pre-F1 generation code at engine commit 4da2bc0a (plan-only tip of
`feat/phase-f-f0-contract`; `src/` identical to main d11097f9), with the inputs in
`_f0_fixtures.py` and these dependency versions:

    faker 40.23.0   pyarrow 25.0.1 / 26.0.0   numpy 2.2.6 / 2.4.6   pandas 2.3.3

The values were identical under both the companion venv (pyarrow 26, numpy 2.4) and the
no-companion CI-mirror venv (pyarrow 25, numpy 2.2), and across PYTHONHASHSEED values. Nothing here
is regenerated at test time. A mismatch means generation output changed: fix the code, or, if the
change is deliberate and approved, re-capture with `python -m tests.parity._f0_capture` and review
the diff as a behavior change. Never edit a digest to make a test pass.

Re-baselined 2026-10-18 (Phase F FR0): the unified generation core declares each column's Arrow
type from the configuration, so the `nulls` fixture's all-null categorical column and the
zero-row `empty` table are `string` instead of Arrow's inferred `null`. Only the schema and table
digests of those two tables moved; every column value digest and every other fixture is unchanged.
The change passed the semantic gate in `_fr0_refreeze.py`; its manifest is
`tests/fixtures/phase_f_fr0/manifest.json`.

Re-captured (column identity joined the seed): every fixture that draws random values moved
because a column's stream now derives from (table, column) as well as its strategy config. Sequence,
derived and other draw-free tables are unchanged. Captured with `python -m tests.parity._f0_capture`.

Added 2026-11-10 (GEN-TYPES E1): the `source_free` fixture (constant and distribution columns).
Captured once under numpy 2.2.6 (identical on arm64 and x86); no existing entry moved.

Re-baselined 2026-11-08 (generated numeric typing): an unformatted `sequence` and a numeric faker
provider (annotated int or float) now declare and emit int64/float64 instead of string. Each affected
column prints the same text per row with nulls at the same rows. Manifest and reasons:
`tests/fixtures/phase_f_fr0/manifest.json`.

Shape: ``EXPECTED[fixture_id][table_name]`` is a `_f0_canonical.table_fingerprint` dict.
``POOL_SEAM`` is the generation pooled-faker seam (`_f0_seams.PoolCall.signature`) for the
`pooled_faker` fixture, one entry per pooled column in declared order.
"""

from __future__ import annotations

from typing import Any

EXPECTED: dict[str, dict[str, dict[str, Any]]] = {
    "sequence": {
        "seq": {
            "schema": "2e516e0de60d68531db9b2a95c93b72ff0d4f1dbfef8095010adc85d3a917fd2",
            "rows": 12,
            "columns": [
                ("plain", "1ec5b45351654227288d8573b2a2f8abbdf3833928c098506c6218347709e20d"),
                ("padded", "182a14b18053384fae0fd7c2f0e0eb23a08e5492da8d6d96e445c9e4504859a6"),
                ("neg", "778d292de22dfb187def46d18ddffb157a66cef6b22e4883f3e3556a71719c68"),
            ],
            "table": "60c7d93fd8cea3692dc4b842110a50054bd993a02ee6c55eddd5caabacf69e82",
        }
    },
    "categorical": {
        "cat": {
            "schema": "8fcbb11a0ef84792f501201461c155a60257ba6a2d3b5c25cbde6a07b6e54492",
            "rows": 40,
            "columns": [
                ("weighted", "b7d25a49668c68dbe948e25456d5d2876615d8653deead7905b97dabceffb7a1"),
                ("uniform", "321620d2594e8ed2195356b8b632ec262cfaa33038117753c027c40f139d08b0"),
                ("ints", "a5ecccf8d3f267dff95b22d71c9413661c688761d4d7c70985da4d38fb8c3a63"),
            ],
            "table": "ea82adeaa2af1ebc7aecb6cab8636442b4e15bd46d43cd10ee3642e59eb8f91a",
        }
    },
    "pooled_faker": {
        "pooled": {
            "schema": "95da74638c7a0e193c0ae6cc86b32f3668f20ef216543e59ad0b144f5ce7d95e",
            "rows": 50000,
            "columns": [
                ("city", "3eb7648cd2f080c68bd90570dd6ca45e9b5c150dbc9ef84e35388e87123f936a"),
                ("last", "1879972639f3dffb1de434d79b00fd28d4a6466793d6387d846fdfd897b66f19"),
                ("street", "f128ccff37f7fbc652ca09964d309d3b310eeb43defc0eaacbca3af3c9b742b1"),
            ],
            "table": "fb96cde1164db481af66db9100f66236973ccfce9d9f75c2a73401c3054d7666",
        }
    },
    "live_faker": {
        "live": {
            "schema": "1434a50bfef0ac821ea753928ab82960061f2496cd5ff8f92faa0d76b8ee742e",
            "rows": 30,
            "columns": [
                ("city", "3c662c698ec2248412e490fc3e0b2869fed7a452a2b91ecc50bdf5dbc124e379"),
                ("email", "b503077a15137cc8ee43fb4c777d2798b684ba0863e0547ff7ffbd8e9621bcb9"),
                ("de_name", "41f77afe03d3215973a433ce52a942a938a45db63c7c160e8b332b702808d441"),
                ("num", "bbe70cef490a524527d1b4aed6042cb1ba24324d607b39d3a84e43a4d2ef4b8d"),
                ("fallback", "cfc1f9a7fb56d5b16e2f6a211c1ca6932310d05d7c4fb54c7a69f9458bf61845"),
                ("opt_out", "0639140d18601078b0f58cb03bb06d3b6fd000f54a3253671709be262ce4f969"),
            ],
            "table": "61de7a2800710c2f26ffc95d8dd245aed4eeada6c25cc4303dd0160af254dd0d",
        }
    },
    "live_faker_default_locale": {
        "live_loc": {
            "schema": "09f0a6050023432e75bfcbae5f0121b76ea56bc406498c4c19fc2dd94028d5a9",
            "rows": 20,
            "columns": [
                ("city", "af4cac94e630df9f31b9eabe57603d36b508d8771bae6fc9c37f57146dada8fe"),
                ("own", "35d5f7f409aab83458b103df037ae8bc3771da011a8f68bd228f393f756907c0"),
            ],
            "table": "e8f47e28ec52dad4cd488b3192634a844b344516e078d724fb887b163293dc6c",
        }
    },
    "keyed_derive": {
        "keyed": {
            "schema": "7dc1348a71c3a3dee4e6f5b28ec843b2358814b881fffb898ce23cb4fbc4c30f",
            "rows": 24,
            "columns": [
                ("cat", "dc2ebfd6703b2014d796a35ad3e94778958dbab300dfe798ccf792d73d079ef9"),
                ("city", "6c72ca71ab12d6ea8b91f64fbbc019f2b1991ec9e009b32066bd2eaa4a61be71"),
                ("rnd", "7576d180739f9428f0a17fd34f6e2fe9277c6dbdd37a67fced2d2a7fbbe8a029"),
                ("maybe", "5d6b2f907229442f9a1de3ca125ea6c3a4e60470a4658bb2960045dee29c681f"),
            ],
            "table": "804855ce5453a6123e93f42d1c394abd4824705604fa4204c8b88b0e029a4bf9",
        }
    },
    "formula": {
        "formula": {
            "schema": "427da4841cb278415e1bc4e010bc1773e64568481996a167d357d94bab58628d",
            "rows": 25,
            "columns": [
                ("rnd", "ea057c28fbc73800ef7f8171667cf5f5b768bfdb182fd5770fcea8935ba66e6c"),
                ("gau", "6308e35a93d0583919a164284bdd42c6fcbee3aef127a2a7b121e195866ae0e8"),
                ("pick", "fe9ec9389425c523b5ad8a2ceff211f604bc2893735c16f692cb6e87397faa29"),
                ("row", "edad610e72498c0ef171e1e3c65c9a6aa4e8f8956ab50eb44472e56761dfabf9"),
                ("hsh", "2a6e80c753bfedbd93ba3620a27a7ef115a54cf91691c53155d994709258fb66"),
                ("fixed_date", "adeb952fc6c10a08cca27885fdde742836bb291b49c18ad3bfae9e4aecb40477"),
                ("empty", "1a7ced7720a8194fe783853ecc62232e944ec5e3efdf03282c78364f241005a3"),
                ("bad", "1a7ced7720a8194fe783853ecc62232e944ec5e3efdf03282c78364f241005a3"),
            ],
            "table": "1e934ccf90c1bc2211fdfe9f6171fdde380146d65a8f0d10aefe274e312dfdcb",
        }
    },
    "referenced_formula": {
        "rf": {
            "schema": "3316be6306f0cfe1d84ff6a1e720262f38ebd55b12c07ba035e4714502439a89",
            "rows": 16,
            "columns": [
                ("pid", "2e3568117b7268589daf6b44944f7c0f10dfa8d069cdac450f0142138f7bc5bc"),
                ("grp", "54ed909af9960976d0ad151f6c9e3f1922e873f0dfb96c321728bbcafd1161f2"),
                ("rf_a", "990c6d8c471ba8370ad782d76b38b51354ab87210e835b63480951c31457449e"),
                ("rf_b", "33e17523ef5050e4cd4b4aa1cd57e0e64a3b65b42aa40aa978f4de39d59ebb02"),
                ("rf_c", "ed6a8b1f067a2cb50f4d972ec89377af73351081176b94f38cba98d93db58fb5"),
                ("rf_d", "bea96eed82fa1bfc53363f3bf046714a5f2f4c278862e7fbf21491d53885056a"),
            ],
            "table": "02138b8efd76de7c3e85835886ddcb1bb6a044e2fe1d6f89146c0a2a0d86b533",
        }
    },
    "derived": {
        "derived": {
            "schema": "5e40da1fe4552d4b5e7f139691b3e1aba216dcb1e30b1db45357a87bfc97fb5d",
            "rows": 14,
            "columns": [
                ("base", "91472db99376f15b424ecdfe591d347a96d9463fe2c68c9a29bd7a505ecf12fc"),
                ("maybe", "6471ff708935345b13d5016771047ee6529d065f62c142158c6574a9dd851237"),
                ("plus", "a4a5ca3c2a8919836d46c0d385f6dca48b38660ee73f2ca1489c1c6b0d68bb20"),
                (
                    "from_nullable",
                    "b290794a67f127e0b62e8923e0bf5ee2411cc4a324399220804f48adef38b472",
                ),
            ],
            "table": "5783979b2c135d3a9671de24d11226ad3564a97bb31d42870a1efed8dc2948d7",
        }
    },
    "reference": {
        "ref_parent": {
            "schema": "abd9d5a2cbbc42ae3009aaaff8a8ab85ed3c8ec8638968e89af709f108418ad2",
            "rows": 12,
            "columns": [
                ("pkey", "994e7d8fe130ca24ccb34959464438ed7de7673b44360aad5f367ed631518f02")
            ],
            "table": "e61822720e498b0467325d7ecc3bbe360ddc05f0fb7fd0c81c63f3591c1a0f12",
        },
        "ref_child": {
            "schema": "caf87e5f6df18b1b93f08ea35b5f19c1afa80ea27aadbec91438c3925488c0e0",
            "rows": 14,
            "columns": [
                ("cid", "646b88e4574ca08f606c4055c214e53579235a0f19b04859f98f93a8f538e68d"),
                ("r_random", "16c68d66e25b1a6b398ddd8653d8d6c1385f429405bdcfe4b451e1208a438f6c"),
                ("r_seq", "7473de76d925e72f2e1e17752bb7ec7e68b6ad55ac758a766ab670a9ed6b0eba"),
                ("r_weighted", "9e4118839347533759f5621cce525941d1f96db60497d3ab41dc023dfa014598"),
                ("r_bounded", "bf74ff55758cee1caa28837d735b09c71b177f49bb423470654485ec356aec31"),
            ],
            "table": "da6831948c5b95121f50ada166d22ccb7188661de0c811fa435e6c0a0ea75d2f",
        },
    },
    "derived_aggregate": {
        "agg": {
            "schema": "3f75e66c95c14a4d4e3d68e2892d0564a6d4d2f822d51f6c742052c286a9d567",
            "rows": 10,
            "columns": [
                ("v", "60039219453e590118bb126893fa88aecffba07164a398135515767f9c8a10d6"),
                ("v_null", "d3ccc68944c9ff4068657ca818ea8f68d31aa045825dc5f3ba21b9715a0e3bcd"),
                ("a_sum", "247acfb1d1a1b675d5773c0c94acbef01fc5c9de01b805c9de92caae1e324316"),
                ("a_mean", "f15f9890327937348f4598a0fca90e33b447bfb114c6801d235810e025f239ce"),
                ("a_min", "59bb70ad168c7c51a1bfaaa4e8c1fe7ad51d3e9d9af6ff365d03e91470808617"),
                ("a_max", "5e9a99933569d70c2acb9961a5536ad9a31a57c5bc4c66d005b631018b43763b"),
                ("a_count", "06dd649c27ef0ccc4cbb86de086d60b1eace801b9bded9c67628ee35a0e590ed"),
            ],
            "table": "7a01939e2db67304f0e22f38d97538ad597e87ecbea46574652653b28aea0c1a",
        }
    },
    "statistical": {
        "stat": {
            "schema": "0c9a2a03e9543ea0d484acd5cc1e20715989d239f3675ab94fea26a4780b8a05",
            "rows": 60,
            "columns": [
                ("amount", "3df4ef7088105526701e165762860edb9d28bb8715f6ff7758781a4693b7ce9c"),
                ("age", "e5b623d2ec266ac4fc929a0bce9e53e8b1a61846f59149730125061b3c08319d"),
                ("joined", "7d5186eba43886c55b6a5c597edacc94f5aa033dadb74cebd7f2ffbf95197250"),
                ("state", "3d608bfaadc1fc017bfc0387160d4bef513862c9b4642dcc97094fd0f9a9c88d"),
            ],
            "table": "f37b5fca801949f050665dbb0def7a9e7ea1dbcf54b6ba2982dff10b6042c8b0",
        }
    },
    "conditional": {
        "cond": {
            "schema": "45c71929f1d230cd6f5ed73c3332eb410d0e2b94774e5d93f4e5a7384589c76c",
            "rows": 60,
            "columns": [
                ("state", "18be90a9421c915da33cb8f1928db16b90e074765064c022806d35ef82a6da37"),
                ("tier", "bf9fca540f9df6a6bb071e167529cea68bd819803ace05cc1444031bb47ca6d7"),
            ],
            "table": "cbceefabccf5a1ad482c8dcf8a61c33f9dcc757f777dccdbbd7adf0e3ec939f0",
        }
    },
    "grouped_windowed": {
        "gw": {
            "schema": "dd8dc8a8ae0a7584f7f6078af92bfc4b606fecefe56de863fd7dc51446b4c37d",
            "rows": 18,
            "columns": [
                ("grp", "ea3e6a9169087f4a86166d961a8e8c1832c1582d5f8a71907852729ab17649ec"),
                ("ord", "dd5c54086e0030fb006952593017b963b1b04980be96f55335e1136a13c1f916"),
                ("anchor", "eed25f4fd8063d542f6d10619f45145e89d2dfa995a8cd32a3184392997a98a8"),
                ("cum", "28e18779a86e630d1d972b3094e3529723e6ffcd43726fb603cd09eb4fe0dc6a"),
                ("walk", "467a155068aa427583b4970e79cd1c921a2954bdf58ed76791650a62da96be22"),
                ("win", "9607068222953d25090fce72edfc0c05f7780ef138388c74266caeca850cb2c6"),
                ("gkey", "1f1257b96d35a13ef2a21e12d6d7c865cf1644d1002736a512b09e182c2e2e7c"),
            ],
            "table": "42e36d728df63493caf355c0e9603f09aed5f35961d251fd1a94bd671651ee34",
        }
    },
    "source_free": {
        "source_free": {
            "schema": "48111c9dfcfdc276b065341e1c9ac03122348d8890d2ef4579fccfffbd11437b",
            "rows": 40,
            "columns": [
                ("country", "fd7fcb94ae747840f9cdb66640f13400b759dc453490a836f13f23f2e6aad91e"),
                ("age", "6684419c1cfb7568b630f9b083e345a9f8f86ac215e48f9ad728cf36b02521c9"),
                ("score", "a5020b86563aacb7505087a05ee2daf3226e4ac3b0e042b0f9eb2dbdb0194053"),
                ("visits", "d726639941f9d983f2f7da1bd5d772d3d019ccb425f038ea38f788774092ddad"),
                ("rank", "880624c3ece553eac098fc67c4c9c7c852e68aa17295da9b10b17770c085643c"),
            ],
            "table": "69ff8f39c02ee733acf8d93185e336e3a3ac6595c4d753ef513c39a4a2a4eea7",
        }
    },
    "nulls": {
        "nulls": {
            "schema": "afa05e79b1f71aff04817653984fa103c0290616100f2970a75bee269e48973a",
            "rows": 30,
            "columns": [
                ("seq_partial", "1658001bd49fa6efc4790b2a94833407f98e528840127eec8ae8f0a6f9732736"),
                ("cat_partial", "47527e9e4692908bceba22f2a651e2e5d20c6c3dd9a097d368bec48226023ae4"),
                (
                    "faker_partial",
                    "bf951ff776ac1324d40ab7407e6dbebd5211e4f61b3cc147f2fbe94422f13163",
                ),
                (
                    "cat_all_null",
                    "265822d774b2309b02cae4e9497a6d4d157cf60aa317e02cc6b04a3fa94a81e6",
                ),
            ],
            "table": "b50fc72af868f3b4939885213bdabc243221ba63dda6df3b3be46e9e46edebc8",
        },
        "empty": {
            "schema": "767f26506b2eda1f3f581fdf1e36590ef25bec3fcaab771816c61ffb14aee70c",
            "rows": 0,
            "columns": [("id", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")],
            "table": "ce940f92e39e5478efccdb62a91b30ed86cc61e167838bc2ceda3539859a3626",
        },
    },
    "mixed_fk_generated_parent": {
        "people": {
            "schema": "9a31618979c4ed6d06e64e9f49b1f3257b81cb47daf4cd9a4cbec30d93b3a871",
            "rows": 5,
            "columns": [
                ("id", "8d357c1dda98e9feca23fc8e3f4dccc3807731a7815cc4c7732af69bc649d1e1"),
                ("nm", "14408bf93c4f9bd63f915fa085c510fe949c9035b9da57b225e8be7e78b3fa90"),
            ],
            "table": "382896f15e9848571bd6e6754b796d2bb8380e6fe7872aa3827aaffd220010e5",
        },
        "accounts": {
            "schema": "d5655e336eed516a1c449bef81b0160f0e6abc5d0ae81a3e824099d0ed55db65",
            "rows": 15,
            "columns": [
                ("person_id", "05a01c7f1d6ce7372a9911c668a38b24f9fe56f9b38cda064c3708a6941f978f"),
                ("note", "6928b76be31bbf3bf519c1ed58b94262297019f717d0fd951c239783c309afd9"),
            ],
            "table": "94230a1acef28d9c830db109c97ca19acbf471d4c6bf9e08c3ff2e3b59d9c57a",
        },
        "warnings": {
            "schema": "a360d170d36146376d01ba8804f44fccddc72c95431aa2b59e243d2697268e2f",
            "rows": 1,
            "columns": [
                ("code", "1baa12215435da99765211c24194bb15b4a6b333fa6edf6fe2392494b5b33735"),
                ("column", "8fb9598bfb9d7db465da2ecf20f71c9af6ef1dd5aed424eec13d9c56f6608aa8"),
                ("orphan_rows", "5b8efbb6b2909b1ab335ad3ff9bb8d5d00031363a8cd1a0578af4b79977f4838"),
            ],
            "table": "10053c6e5c1faf58b1e5199bd71fa48dceba5afeca8bc6e06f24d2baa9acef20",
        },
    },
    "composite_person_deterministic": {
        "person_det": {
            "schema": "606472901e931708d25fb1110fbbbe1527cce38e708a4f2d4ea6ee778fa4856d",
            "rows": 6,
            "columns": [
                ("dob", "a1502567da1f0ab6d44a71d2700254393ffc579944cc6a8930f976b014b95a35"),
                ("email", "2d2fc8707419d45bedb6415dd632e86f9ee2ead06291b54ef6e193311df443f7"),
                ("first_name", "48cde8bff091dc9c485a233b1f1394924e531a42d3213fd7c01cfba823a03876"),
                ("last_name", "0c5b05243104ab574a913af9c94ef81c091adfcc9a427dbc765ca9a47a6777a6"),
            ],
            "table": "e116616249312d9386c0810e4df5bf48fedc883c02a18c2ebbb7297f2f9bfa66",
        }
    },
    "composite_person_seeded_pool": {
        "person_pool": {
            "schema": "606472901e931708d25fb1110fbbbe1527cce38e708a4f2d4ea6ee778fa4856d",
            "rows": 20,
            "columns": [
                ("dob", "3f7f9ec86a64785feb0054cddf2bc83b775f2092524197c148fbf51e1e8dc8df"),
                ("email", "1cafcc7aadc32cacf3c89586a323855fa13f3ae9c869348b258c071f9acc2ab4"),
                ("first_name", "ce36dba76e6dc54257182ea86f5d7990e3be1ac96286c0a07db08ec0b289a66c"),
                ("last_name", "845b9c160b5d5f3cb2f8144bcfb7aacea2f4bcb12587d74fd1af41b959addd5b"),
            ],
            "table": "b1fd31e95196b0ba5ca57698956e84af4ceffc84ab826d039b14f7f43664edfe",
        }
    },
    "composite_name_email_deterministic": {
        "name_email_det": {
            "schema": "a4904edd6e7fe724c843322148d89516018dc6cb7ecac18e53b299ac3487baa1",
            "rows": 5,
            "columns": [
                ("email", "72d66edb3cbada1805bd0a478a7475c310e5aab7b1551258002c42af97340a71"),
                ("first_name", "7d9d7596b15cc0c8981ec5764c77dac12e7fcf104e0a74e26ba0549f53b79efd"),
                ("last_name", "7a074a1b3364ed69430f7fd9a233fe4fdaddbadb71fda70ddf86eead99a782a6"),
            ],
            "table": "2cf91c1d64fac29142f3a53252cedc19af79e7ccf10dab1b82812a5e0dee66a8",
        }
    },
    "composite_name_email_seeded_pool": {
        "name_email_pool": {
            "schema": "a4904edd6e7fe724c843322148d89516018dc6cb7ecac18e53b299ac3487baa1",
            "rows": 20,
            "columns": [
                ("email", "1cafcc7aadc32cacf3c89586a323855fa13f3ae9c869348b258c071f9acc2ab4"),
                ("first_name", "ce36dba76e6dc54257182ea86f5d7990e3be1ac96286c0a07db08ec0b289a66c"),
                ("last_name", "845b9c160b5d5f3cb2f8144bcfb7aacea2f4bcb12587d74fd1af41b959addd5b"),
            ],
            "table": "e6f6f8d7c8a4d717ac12fc092255a8a9927c2fa92f4571110355a04ef5f59057",
        }
    },
}

POOL_SEAM: list[dict[str, Any]] = [
    {
        "faker_type": "city",
        "faker_kwargs": {},
        "n": 50000,
        "locale": None,
        "pool_size": 10000,
        "build_seed": "a94b15c8ba386c9c",
        "selection_seed": "e7587ea770b3b50a",
        "pool_digest": "33f05591a98c33fb3b5aaeb268085cb9d5fc4a386202be8353150ef936c684a4",
        "sampled_digest": "40b5d3a7b21b305e4723bedb4a2dc37f6a5333d634fec3672e971fd607c6a1fe",
    },
    {
        "faker_type": "last_name",
        "faker_kwargs": {},
        "n": 50000,
        "locale": "de_DE",
        "pool_size": 10000,
        "build_seed": "aeca1eca5a28a58f",
        "selection_seed": "eb73b9fc4006c48f",
        "pool_digest": "bfaf853b400c7d465f2a3d35ced59af5263d7de0a37050c75657d63794ddc830",
        "sampled_digest": "8fe49c2ba6e4f1dc2c982eeffe6ac29be9842fb6821fa1f694c513275bbba629",
    },
    {
        "faker_type": "street_name",
        "faker_kwargs": {},
        "n": 50000,
        "locale": "fr_FR",
        "pool_size": 10000,
        "build_seed": "77af8a192a9e8901",
        "selection_seed": "be38b482710f12e5",
        "pool_digest": "e054b89f1299f41c01277f9c220c5600d30c1a216691c37ff6807e7ff7589645",
        "sampled_digest": "365e195fab6d23ad7c73cf452988cf624a499dd6a3c9bd905fccc126381bddca",
    },
]
