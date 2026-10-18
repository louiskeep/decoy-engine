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

Shape: ``EXPECTED[fixture_id][table_name]`` is a `_f0_canonical.table_fingerprint` dict.
``POOL_SEAM`` is the generation pooled-faker seam (`_f0_seams.PoolCall.signature`) for the
`pooled_faker` fixture, one entry per pooled column in declared order.
"""

from __future__ import annotations

from typing import Any

EXPECTED: dict[str, dict[str, dict[str, Any]]] = {
    "sequence": {
        "seq": {
            "schema": "78245e41da3e6c183061ef9ef03bbef39ac3f9382c79529a27a3be7455fe50a5",
            "rows": 12,
            "columns": [
                ("plain", "ead623bb7bba1e2d6759460612dc7b16d09ecbef7392fbdc5fbd6185d52e7e05"),
                ("padded", "182a14b18053384fae0fd7c2f0e0eb23a08e5492da8d6d96e445c9e4504859a6"),
                ("neg", "7261f29cb7402f0f484b325fb3d3ffdce24cbada803c01b48798d51db4222b23"),
            ],
            "table": "3df0c6047f7783128b54df894c8f737792708e97a3fb318223c710a621d3698b",
        }
    },
    "categorical": {
        "cat": {
            "schema": "8fcbb11a0ef84792f501201461c155a60257ba6a2d3b5c25cbde6a07b6e54492",
            "rows": 40,
            "columns": [
                ("weighted", "1758720cdf34a626b572af6973b6bb835c6181e38c214fff2075b61b8194cf8f"),
                ("uniform", "d202835d414aae22c5da5820e6136f9e6564552ea1e921bddbe52003637fba43"),
                ("ints", "aa8d85f74d5ce432f8c411e97572bd6a2254bd8855ac7cff30044c62b28ef1e5"),
            ],
            "table": "4b55d8edeba3b78e55c4df7eed549e6b7e7e00a5ec9ca69cd35c843eea6aa317",
        }
    },
    "pooled_faker": {
        "pooled": {
            "schema": "95da74638c7a0e193c0ae6cc86b32f3668f20ef216543e59ad0b144f5ce7d95e",
            "rows": 50000,
            "columns": [
                ("city", "fce445cffb8ae3274e9c1c5f7fc61a3d9b42ab10225e8a4002c6c9127d8aa788"),
                ("last", "b5c2c38e35d3f3ee73719b7cdd9780bae2e7bf6c382d8ee874fb83c7b9a666dc"),
                ("street", "5c0b043324339261ae9c73fb5e4cb4b0be351be5e81df0716bac9b49a2b87494"),
            ],
            "table": "7515cc948560fda6092680426cac4ff73a5cba3047cf79a07c446262654aa0cd",
        }
    },
    "live_faker": {
        "live": {
            "schema": "839b1064d81a7eec3f24e9575ebaf98eb95936e3471ceda1a942866b4ab0d623",
            "rows": 30,
            "columns": [
                ("city", "a5f195876169a518f54dbcabbb45a2099cec9514b3bd0e488797965a3751e9f3"),
                ("email", "d586e2786bd73f56a868080da970f4fab2cd344dcb234bab8b03a9030a8aecdb"),
                ("de_name", "4d9f834ee7d261d5b0d1b13e34a597b12894e86e6984aebdb4e8553d36b92355"),
                ("num", "86600c391e4b9659b8fb7d10ac3cebddfd09567ea89ff7ada38fa27f38752d6d"),
                ("fallback", "973f9a4df74efce253a8a9497b18f790b84cbe28abe9a1a0d04d3e0f4a65a9de"),
                ("opt_out", "8817cbf60eb100f4247a117c00d19285e4399d7965fdce4ecd56c020ac99d015"),
            ],
            "table": "571f2ecd49f43a7cfbb6d37f979bc25008fdef09ac2e9abdc73fdb57c969ed79",
        }
    },
    "live_faker_default_locale": {
        "live_loc": {
            "schema": "09f0a6050023432e75bfcbae5f0121b76ea56bc406498c4c19fc2dd94028d5a9",
            "rows": 20,
            "columns": [
                ("city", "2f2f90e83bd0e612d95219e2ed4b7667447dd383700140249efdfae0d5326419"),
                ("own", "0be72d27267556c2e7fb8a017ad7fa6af813a266e251088c708ad7e12bf43ba8"),
            ],
            "table": "ea3e1d889e9c68f3bcbfe32c5a122ae2961ceca384eede566f53e3669ad03073",
        }
    },
    "keyed_derive": {
        "keyed": {
            "schema": "7dc1348a71c3a3dee4e6f5b28ec843b2358814b881fffb898ce23cb4fbc4c30f",
            "rows": 24,
            "columns": [
                ("cat", "8babe2ee37b11436ee30df4a65003c6c9dc32da82ffe66215dd66ce74f6dfe58"),
                ("city", "376f8c4c8ee70ffec81232a86f572006957584a9a418bbf89654e526ca597f32"),
                ("rnd", "5737847637ff96684310f7a0a8f1ed59482f55102689ad28f03558134bb31776"),
                ("maybe", "1102a25c8fba8a4476dbb04b18e5ce9c91456fc7090f9c6bcdf22026321b59f9"),
            ],
            "table": "5fc5490f068d0d40cd4322f23d7ecc04b13ab84c4e2455483de74d60f6b5f9e1",
        }
    },
    "formula": {
        "formula": {
            "schema": "427da4841cb278415e1bc4e010bc1773e64568481996a167d357d94bab58628d",
            "rows": 25,
            "columns": [
                ("rnd", "877b6483682a3ff3d0e03a5218241911083f81b8ae868e54f19bb32e437e1f3a"),
                ("gau", "6ba157c6064b321204914212ac87abdc1f339236a1af0038d0232b93607ceba1"),
                ("pick", "d915874c1f34e09518229ec27e573a872fb574afee003f4b555f97dfd209dd02"),
                ("row", "edad610e72498c0ef171e1e3c65c9a6aa4e8f8956ab50eb44472e56761dfabf9"),
                ("hsh", "fcc17defa0e7424566023fc24027a4a3addf12ff2fc9f25cc74b80a52842d86d"),
                ("fixed_date", "adeb952fc6c10a08cca27885fdde742836bb291b49c18ad3bfae9e4aecb40477"),
                ("empty", "1a7ced7720a8194fe783853ecc62232e944ec5e3efdf03282c78364f241005a3"),
                ("bad", "1a7ced7720a8194fe783853ecc62232e944ec5e3efdf03282c78364f241005a3"),
            ],
            "table": "3942827b9e13d99d3257e17d0443d2905425d90bd3a35b36c0779e04d5f8b1ba",
        }
    },
    "referenced_formula": {
        "rf": {
            "schema": "3316be6306f0cfe1d84ff6a1e720262f38ebd55b12c07ba035e4714502439a89",
            "rows": 16,
            "columns": [
                ("pid", "2e3568117b7268589daf6b44944f7c0f10dfa8d069cdac450f0142138f7bc5bc"),
                ("grp", "1c4e5f5cc2b99d03a04b23f43e5626d387f411fad384ba40bee87ffb7a91b25f"),
                ("rf_a", "665d89e1c5bab5857aeaf87388948a1fdf7ccdf813ef75401b9c9c1efdc21bde"),
                ("rf_b", "18d9328fec1015c2ad43c9e75ad108c3942011ced98ca97c5f952a2e3dc96c0c"),
                ("rf_c", "ed6a8b1f067a2cb50f4d972ec89377af73351081176b94f38cba98d93db58fb5"),
                ("rf_d", "bea96eed82fa1bfc53363f3bf046714a5f2f4c278862e7fbf21491d53885056a"),
            ],
            "table": "d2c5f13e8900138664e150a18e15363f077729b402afa4ec87e824f50da8e57a",
        }
    },
    "derived": {
        "derived": {
            "schema": "5e40da1fe4552d4b5e7f139691b3e1aba216dcb1e30b1db45357a87bfc97fb5d",
            "rows": 14,
            "columns": [
                ("base", "91472db99376f15b424ecdfe591d347a96d9463fe2c68c9a29bd7a505ecf12fc"),
                ("maybe", "fad0c27009544c0f2647476af54f95571f0133eba85acf1c8ea5d35a5b1911b7"),
                ("plus", "a4a5ca3c2a8919836d46c0d385f6dca48b38660ee73f2ca1489c1c6b0d68bb20"),
                (
                    "from_nullable",
                    "4f4699923c7eccb755dd39bf5418cde668e4f9fef867438755395abc4eef4d55",
                ),
            ],
            "table": "d0e134747083e7e67a66f7d07d706569e5e569d7fe86443d18c404e0b552383f",
        }
    },
    "reference": {
        "ref_parent": {
            "schema": "abd9d5a2cbbc42ae3009aaaff8a8ab85ed3c8ec8638968e89af709f108418ad2",
            "rows": 12,
            "columns": [
                ("pkey", "e43bf983a1958fc28c68bac0fd0c7b39b39aff620eb3e56915140ca003a28ee3")
            ],
            "table": "5bd260410af58db6e28aa25502fe8865da080149765f8f97fa1e6bb839bb2371",
        },
        "ref_child": {
            "schema": "bcdc2b8897e69826f903a27d76c1b30c52b9b51e38eef04d07f0a8f98ca60a06",
            "rows": 14,
            "columns": [
                ("cid", "c15e5ad0b6fc9c50eb2e9dc73c00215b98ed3f0a0dbe1667a596aae05caa0c36"),
                ("r_random", "ca891231af0d93c297081a16ff80597b601b9b6670cd614a55890ef04ad8a2dc"),
                ("r_seq", "f72940eec72acc5b132e9992b9475e8475b77f9f35888a90853850471da1c2f3"),
                ("r_weighted", "b14eb3ca348fa1a96d4c3df9bb8214c58363ec07573424dc0cc8176bb72e8135"),
                ("r_bounded", "29456bb3eebc38287ef16f50aff6dcfdc836060ff2e769bcac23e59bf9a6c801"),
            ],
            "table": "81619e14f5bc9f70a517ca6e21d4ff21080b701dd15494f9123f83f8e98861ef",
        },
    },
    "derived_aggregate": {
        "agg": {
            "schema": "3f75e66c95c14a4d4e3d68e2892d0564a6d4d2f822d51f6c742052c286a9d567",
            "rows": 10,
            "columns": [
                ("v", "60039219453e590118bb126893fa88aecffba07164a398135515767f9c8a10d6"),
                ("v_null", "04f2c43b596bf6edbfdebb3dad8495fc30b5928097927e9c760133ba4bdca587"),
                ("a_sum", "247acfb1d1a1b675d5773c0c94acbef01fc5c9de01b805c9de92caae1e324316"),
                ("a_mean", "f15f9890327937348f4598a0fca90e33b447bfb114c6801d235810e025f239ce"),
                ("a_min", "59bb70ad168c7c51a1bfaaa4e8c1fe7ad51d3e9d9af6ff365d03e91470808617"),
                ("a_max", "5e9a99933569d70c2acb9961a5536ad9a31a57c5bc4c66d005b631018b43763b"),
                ("a_count", "defdf84b0d9e2342770586571b0f3f480370888b895e2a82b4448ffdc073365a"),
            ],
            "table": "61acfeac32544546e11f199764fe152cde035be6c26bbad285c2055ca0f331aa",
        }
    },
    "statistical": {
        "stat": {
            "schema": "0c9a2a03e9543ea0d484acd5cc1e20715989d239f3675ab94fea26a4780b8a05",
            "rows": 60,
            "columns": [
                ("amount", "603e4ef90fc0a5a02cfe9e057757b8690997f14f17166fc6817a1953fdc35b3c"),
                ("age", "616f5f5917e893301753de83dde384f3db7627a6066dac76237c0efb41a2dc5c"),
                ("joined", "3ba94fe61db87f3e9fc1232c0a16d2f02068ebb434ff6d43f9ed2390bcd6541d"),
                ("state", "4a58579cec53284599cc865d5f3e148554120afc6f7e963b1b9714ea75359b1c"),
            ],
            "table": "3543ce7ade1f386db08ba44fa1d3e01a790d27636d65649ed554edcded44937d",
        }
    },
    "conditional": {
        "cond": {
            "schema": "45c71929f1d230cd6f5ed73c3332eb410d0e2b94774e5d93f4e5a7384589c76c",
            "rows": 60,
            "columns": [
                ("state", "4a58579cec53284599cc865d5f3e148554120afc6f7e963b1b9714ea75359b1c"),
                ("tier", "ceddda77264e7458e2ac93aac003fe782560efa46ad3ee52f677fdcfa550f509"),
            ],
            "table": "a256949efcae44979028c9421912a4fb511bbb02cb106d2c54eb57e314011b64",
        }
    },
    "grouped_windowed": {
        "gw": {
            "schema": "dd8dc8a8ae0a7584f7f6078af92bfc4b606fecefe56de863fd7dc51446b4c37d",
            "rows": 18,
            "columns": [
                ("grp", "eb4b78865f4ebca05c8a12fff5e292efb0d62754820b782ad9c84a7bff8ffdcb"),
                ("ord", "dd5c54086e0030fb006952593017b963b1b04980be96f55335e1136a13c1f916"),
                ("anchor", "eed25f4fd8063d542f6d10619f45145e89d2dfa995a8cd32a3184392997a98a8"),
                ("cum", "a8b2defe22f57be4eed48ee91a28ef9e0a9115d2ce593b467efe8040311bebac"),
                ("walk", "6bc92b63fc9a9dbd8cecafbead6c7f59848e8c776e6695b546d751726bbca2c3"),
                ("win", "9ba1c6bdc2b677f204ef916754d1e9be3dc555d283eb1c12a70856c5b50c0e88"),
                ("gkey", "d445cfb65bd10cd7ef6100db3ddf8cc7465da261d54bfbc20e6c63f8804bb251"),
            ],
            "table": "38d60aecec60ba4eec009d36b13b1e7a04ac423fcda1dbe829aa310d04d5117a",
        }
    },
    "nulls": {
        "nulls": {
            "schema": "3eaa2e412ab4937ecee72bce2d43fe1b603db5344d5d02ea60cf3d2aae5a7d63",
            "rows": 30,
            "columns": [
                ("seq_partial", "490f0cbb0e8ac47131fceb444a16c6c677abd8840e30eaa61d24aff9a2b4bdb6"),
                ("cat_partial", "3364fee8d96f7a7150178fbdf46dd7449e0aa763f729be97fc9cf66c80af2248"),
                (
                    "faker_partial",
                    "b4fa6c819d8a337677184f97856ca9cd1b353c24f86f0b8bc9885bb37f1d81d0",
                ),
                (
                    "cat_all_null",
                    "265822d774b2309b02cae4e9497a6d4d157cf60aa317e02cc6b04a3fa94a81e6",
                ),
            ],
            "table": "232902afc3faf49c772ad7bd6cddaba7846f58599e9e1c5b8c7f533d229cd271",
        },
        "empty": {
            "schema": "c0cc89f59ad1792bfd3e6ab9eef47a610f70e3264bfcb4591ced62ec7de299bf",
            "rows": 0,
            "columns": [("id", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")],
            "table": "2445c1c0db89b65b0be8c6c6f88cf5befd76dd3251e7a8f9701fffe9db6872fd",
        },
    },
    "mixed_fk_generated_parent": {
        "people": {
            "schema": "9a31618979c4ed6d06e64e9f49b1f3257b81cb47daf4cd9a4cbec30d93b3a871",
            "rows": 5,
            "columns": [
                ("id", "ae455a2530ee14a2609557a48555d84c5a7706b099c5b1020dcde70604639abc"),
                ("nm", "f94a9fc0b731455aa8af03ce3f45dd167c316c991ac5558be4ce2f8a928c88a4"),
            ],
            "table": "14acfc63f1435997498fa3f7b1e357d4857dcf40213e470ca624b2d346be624b",
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
        "build_seed": "5facd2dcd2a5e53b",
        "selection_seed": "ebdaa835ab9cfd18",
        "pool_digest": "a2d52e514cf59ec73caae48899b3bb96a017f6bb83372ef7b7e4f71555efcb1f",
        "sampled_digest": "5e4b1fd14ca80872f889095cd2d4f5dae245f014ae201aefa22d3e07d642331e",
    },
    {
        "faker_type": "last_name",
        "faker_kwargs": {},
        "n": 50000,
        "locale": "de_DE",
        "pool_size": 10000,
        "build_seed": "82446ead7bf203cc",
        "selection_seed": "dc4929348d54853a",
        "pool_digest": "df902d131f89027059cfa6ec3f488d586bce610272f8d135a7b466df5c5dd16f",
        "sampled_digest": "8b4a5ee6b71a13573843fca0d3f56c5397de23104c8854d046f1d3a832b21d24",
    },
    {
        "faker_type": "street_name",
        "faker_kwargs": {},
        "n": 50000,
        "locale": "fr_FR",
        "pool_size": 10000,
        "build_seed": "029853f11ffd739a",
        "selection_seed": "fae2a64b1d7932ce",
        "pool_digest": "c6d58ec427bb9b510876e110da27cc64c63e986a3fa307ff244a19285252fbb0",
        "sampled_digest": "2e86f4c4dd172070c7c2344f593eb82ee998b97775c6900806473616d2f0b6b0",
    },
]
