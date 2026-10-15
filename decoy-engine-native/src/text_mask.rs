//! Native splice for `text_mask` (C6b-ii slice b): the redact / passthrough case.
//!
//! The Python oracle (`transforms/_text_mask_splice.py::splice_spans` over `_mask_span`) rebuilds
//! a cell from its resolved spans: each unmatched gap takes the `unmatched_span_policy`, each
//! span becomes the redact token (strategy `redact`) or its own text (strategy `passthrough`).
//! This module is that rebuild for cells whose every span strategy is one of those two and whose
//! unmatched policy is `redact` or `passthrough`, so the per-span Python dispatch and string
//! joins leave the hot path. Anything it cannot reproduce byte-for-byte returns `None` and the
//! caller runs the Python splice.
//!
//! Two entry points share one `splice_cell`:
//! - `text_mask_splice_spans` takes the caller's RESOLVED MERGED spans (so Python-detected
//!   lookaround spans such as `ssn` are included) and only splices.
//! - `text_mask_detect_splice` runs detection, the oracle's merge (stable sort by
//!   `(start, -(end - start))`, then a leftmost-longest sweep), and the splice in one call. It is
//!   used only when every requested detector is one of the eight Rust detectors, so no
//!   Python-detected span can be missed.
//!
//! Gap rule (`apply_unmatched`): an empty gap emits nothing under any policy, so two adjacent
//! spans never gain a stray token between them; a non-empty gap is the token (`redact`) or
//! itself (`passthrough`). The gap token and the span token are the same string only because the
//! caller passes one `token`; they stay separate decisions here.
//!
//! Established methodology: no new algorithm. Detection is `text_redact::candidates_for_cell`
//! (the `regex` crate); the merge is the stable-sort plus sweep that `merge_text_redact_spans`
//! uses. Cells are ASCII-only by construction, where byte offsets equal the code-point offsets
//! Python slices with.

use crate::text_redact::{candidates_for_cell, is_eligible};

#[cfg(feature = "extension-module")]
use crate::text_redact::{CATALOG_VERSION, SUPPORTED_IDS};
#[cfg(feature = "extension-module")]
use pyo3::exceptions::PyValueError;
#[cfg(feature = "extension-module")]
use pyo3::prelude::*;
#[cfg(feature = "extension-module")]
use pyo3::types::{PyList, PyString};

/// One resolved span: byte range plus whether it is kept verbatim (`passthrough`) or replaced by
/// the redact token (`redact`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SpliceSpan {
    pub start: usize,
    pub end: usize,
    pub verbatim: bool,
}

/// Append one unmatched gap under the policy; an empty gap emits nothing (`apply_unmatched`).
fn push_gap(out: &mut String, gap: &str, token: &str, passthrough_unmatched: bool) {
    if gap.is_empty() {
        return;
    }
    out.push_str(if passthrough_unmatched { gap } else { token });
}

/// Rebuild `text` from start-ordered, non-overlapping `spans`. Returns `None` when the input is
/// not what the splice can prove byte-exact (non-ASCII text, or spans out of order / out of
/// range), so the caller falls back to the Python splice instead of emitting a guess.
pub fn splice_cell(
    text: &str,
    spans: &[SpliceSpan],
    token: &str,
    passthrough_unmatched: bool,
) -> Option<String> {
    if !text.is_ascii() {
        return None;
    }
    if text.is_empty() {
        return Some(String::new());
    }
    let mut out = String::with_capacity(text.len());
    let mut cursor = 0usize;
    for span in spans {
        if span.start < cursor || span.end < span.start || span.end > text.len() {
            return None;
        }
        push_gap(
            &mut out,
            &text[cursor..span.start],
            token,
            passthrough_unmatched,
        );
        out.push_str(if span.verbatim {
            &text[span.start..span.end]
        } else {
            token
        });
        cursor = span.end;
    }
    push_gap(&mut out, &text[cursor..], token, passthrough_unmatched);
    Some(out)
}

/// Detection + the oracle's merge + the splice for one eligible cell. `indices` are catalog
/// indices in the oracle's requested order (duplicates preserved: a repeated id re-emits its
/// spans and the sweep collapses them, as the Python replay does); `verbatim[i]` is the strategy
/// for catalog index `i`. `None` means the cell is outside the ASCII-safe domain.
pub fn detect_splice_cell(
    text: &str,
    indices: &[usize],
    verbatim: &[bool; 8],
    token: &str,
    passthrough_unmatched: bool,
) -> Option<String> {
    if !is_eligible(text) {
        return None;
    }
    let mut cands = candidates_for_cell(text, indices);
    // Stable, like Python's list.sort: equal (start, length) keeps requested-detector order.
    cands.sort_by(|a, b| a.1.cmp(&b.1).then((b.2 - b.1).cmp(&(a.2 - a.1))));
    let mut spans: Vec<SpliceSpan> = Vec::with_capacity(cands.len());
    let mut last_end: i64 = -1;
    for (idx, start, end) in cands {
        if i64::from(start) >= last_end {
            spans.push(SpliceSpan {
                start: start as usize,
                end: end as usize,
                verbatim: verbatim[idx as usize],
            });
            last_end = i64::from(end);
        }
    }
    splice_cell(text, &spans, token, passthrough_unmatched)
}

// ── PyO3 boundary ───────────────────────────────────────────────────────────────────────────

/// The cell as `&str`, or `None` for a null, a non-string, or a lone-surrogate string.
#[cfg(feature = "extension-module")]
fn cell_str<'a>(item: &'a Bound<'_, PyAny>) -> Option<&'a str> {
    item.cast::<PyString>().ok()?.to_str().ok()
}

#[cfg(feature = "extension-module")]
fn push_result(out: &Bound<'_, PyList>, py: Python<'_>, value: Option<String>) -> PyResult<()> {
    match value {
        Some(s) => out.append(PyString::new(py, &s)),
        None => out.append(py.None()),
    }
}

/// `text_mask_splice_spans(values, spans, token, passthrough_unmatched) -> list[str | None]`
///
/// `spans[i]` is the resolved merged span list `[(start, end, verbatim), ...]` for `values[i]`.
/// A result of `None` means "decline": the cell is not a string, is non-ASCII, or its spans are
/// not usable; the caller runs the Python splice for it.
#[cfg(feature = "extension-module")]
#[pyfunction]
fn text_mask_splice_spans(
    py: Python<'_>,
    values: &Bound<'_, PyAny>,
    spans: &Bound<'_, PyAny>,
    token: &str,
    passthrough_unmatched: bool,
) -> PyResult<Py<PyAny>> {
    let out = PyList::empty(py);
    let mut span_iter = spans.try_iter()?;
    for item in values.try_iter()? {
        let item = item?;
        let cell_spans = span_iter
            .next()
            .ok_or_else(|| PyValueError::new_err("spans_shorter_than_values"))??;
        let Some(text) = cell_str(&item) else {
            out.append(py.None())?;
            continue;
        };
        let mut parsed: Vec<SpliceSpan> = Vec::new();
        for s in cell_spans.try_iter()? {
            let (start, end, verbatim): (usize, usize, bool) = s?.extract()?;
            parsed.push(SpliceSpan {
                start,
                end,
                verbatim,
            });
        }
        push_result(
            &out,
            py,
            splice_cell(text, &parsed, token, passthrough_unmatched),
        )?;
    }
    Ok(out.into_any().unbind())
}

/// `text_mask_detect_splice(values, detector_ids, verbatim, token, passthrough_unmatched,
/// catalog_version) -> list[str | None]`
///
/// `detector_ids` are Rust-supported ids in the oracle's requested order (duplicates kept);
/// `verbatim[i]` says whether `detector_ids[i]` is `passthrough` (true) or `redact` (false). A
/// result of `None` means the cell is ineligible (non-ASCII, 0x1c-0x1f, non-string) or the value
/// was null; the caller tells those apart from its own input and runs the Python path.
#[cfg(feature = "extension-module")]
#[pyfunction]
fn text_mask_detect_splice(
    py: Python<'_>,
    values: &Bound<'_, PyAny>,
    detector_ids: Vec<String>,
    verbatim: Vec<bool>,
    token: &str,
    passthrough_unmatched: bool,
    catalog_version: i64,
) -> PyResult<Py<PyAny>> {
    if catalog_version != CATALOG_VERSION {
        return Err(PyValueError::new_err(format!(
            "catalog_version_mismatch: caller sent {catalog_version}, kernel is {CATALOG_VERSION}"
        )));
    }
    if detector_ids.len() != verbatim.len() {
        return Err(PyValueError::new_err("verbatim_length_mismatch"));
    }
    let mut indices = Vec::with_capacity(detector_ids.len());
    let mut verbatim_by_index = [false; 8];
    for (id, v) in detector_ids.iter().zip(&verbatim) {
        let Some(i) = SUPPORTED_IDS.iter().position(|s| s == id) else {
            return Err(PyValueError::new_err(format!(
                "unknown_detector_id: {id:?} is not a Rust-supported text_redact detector"
            )));
        };
        indices.push(i);
        verbatim_by_index[i] = *v;
    }
    let out = PyList::empty(py);
    for item in values.try_iter()? {
        let item = item?;
        let result = cell_str(&item).and_then(|text| {
            detect_splice_cell(
                text,
                &indices,
                &verbatim_by_index,
                token,
                passthrough_unmatched,
            )
        });
        push_result(&out, py, result)?;
    }
    Ok(out.into_any().unbind())
}

#[cfg(feature = "extension-module")]
pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(text_mask_splice_spans, m)?)?;
    m.add_function(wrap_pyfunction!(text_mask_detect_splice, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sp(start: usize, end: usize, verbatim: bool) -> SpliceSpan {
        SpliceSpan {
            start,
            end,
            verbatim,
        }
    }

    #[test]
    fn empty_gap_emits_nothing_between_adjacent_spans() {
        let got = splice_cell("abcd", &[sp(0, 2, false), sp(2, 4, false)], "[X]", false);
        assert_eq!(got.as_deref(), Some("[X][X]"));
    }

    #[test]
    fn gap_policy_and_span_strategy_are_independent() {
        // gap redact + span verbatim, and the reverse.
        assert_eq!(
            splice_cell("a b c", &[sp(2, 3, true)], "[X]", false).as_deref(),
            Some("[X]b[X]")
        );
        assert_eq!(
            splice_cell("a b c", &[sp(2, 3, false)], "[X]", true).as_deref(),
            Some("a [X] c")
        );
    }

    #[test]
    fn zero_span_cell_is_one_gap() {
        assert_eq!(
            splice_cell("hello", &[], "[X]", false).as_deref(),
            Some("[X]")
        );
        assert_eq!(
            splice_cell("hello", &[], "[X]", true).as_deref(),
            Some("hello")
        );
        assert_eq!(splice_cell("", &[], "[X]", false).as_deref(), Some(""));
    }

    #[test]
    fn unusable_input_declines() {
        assert_eq!(splice_cell("café", &[], "[X]", false), None);
        assert_eq!(splice_cell("abc", &[sp(2, 9, false)], "[X]", false), None);
        assert_eq!(
            splice_cell("abcd", &[sp(2, 3, false), sp(1, 2, false)], "[X]", false),
            None
        );
    }

    #[test]
    fn detect_splice_merges_like_the_oracle() {
        let verbatim = [false; 8];
        let email = 0usize;
        let got = detect_splice_cell("mail a@b.com now", &[email], &verbatim, "[X]", true);
        assert_eq!(got.as_deref(), Some("mail [X] now"));
        // A duplicated id re-emits the same span; the sweep keeps one.
        let dup = detect_splice_cell("a@b.com", &[email, email], &verbatim, "[X]", false);
        assert_eq!(dup.as_deref(), Some("[X]"));
        assert_eq!(
            detect_splice_cell("café a@b.com", &[email], &verbatim, "[X]", false),
            None
        );
    }

    #[test]
    fn same_start_keeps_the_longest_and_adjacent_spans_both_survive() {
        let (email, ipv4) = (0usize, 4usize);
        let redact = [false; 8];
        // email and ipv4 both start at 0; the longer email wins and swallows the ipv4.
        let tie = detect_splice_cell("10.0.0.1a@b.com", &[email, ipv4], &redact, "[X]", true);
        assert_eq!(tie.as_deref(), Some("[X]"));
        // Requested order must not decide it: longest-first holds with ipv4 listed first too.
        let reversed = detect_splice_cell("10.0.0.1a@b.com", &[ipv4, email], &redact, "[X]", true);
        assert_eq!(reversed.as_deref(), Some("[X]"));
        // Adjacent, not overlapping: start == previous end must keep the second span.
        let adjacent = detect_splice_cell("a@b.com10.0.0.1", &[email, ipv4], &redact, "[X]", true);
        assert_eq!(adjacent.as_deref(), Some("[X][X]"));
    }

    #[test]
    fn verbatim_flag_is_per_detector() {
        let mut verbatim = [false; 8];
        verbatim[0] = true; // email passes through, ipv4 is redacted
        let got = detect_splice_cell("a@b.com 10.0.0.1", &[0, 4], &verbatim, "[X]", true);
        assert_eq!(got.as_deref(), Some("a@b.com [X]"));
    }
}
