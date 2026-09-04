"""
ui/reviewer.py
---------------
Komponen UI untuk menampilkan SATU pasangan duplikat (Data A vs Data B)
beserta rincian skor kemiripan dan tombol keputusan panitia. Dipakai baik
oleh langkah "③ Review Login" maupun "④ Review Register" di app.py —
perbedaan kolom yang ditampilkan diatur lewat parameter `fields`.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

# Label ramah untuk kode keputusan, dipakai saat menampilkan status "sudah direview".
DECISION_LABELS = {
    "keep_a": "🅰️ Data A dipilih sebagai data benar",
    "keep_b": "🅱️ Data B dipilih sebagai data benar",
    "keep_both": "↔️ Kedua data dianggap peserta berbeda",
}


def _safe(value) -> str:
    """Tampilkan '-' untuk nilai kosong/NaN alih-alih 'nan' atau string kosong membingungkan."""
    if pd.isna(value):
        return "-"
    return str(value)


def _field_table(pair: pd.Series, fields: list[tuple[str, str]]) -> pd.DataFrame:
    """Susun tabel perbandingan Data A vs Data B dari daftar (label, nama_kolom)."""
    rows = [
        {"Field": label, "Data A": _safe(pair.get(f"{key}_a")), "Data B": _safe(pair.get(f"{key}_b"))}
        for label, key in fields
    ]
    return pd.DataFrame(rows)


def render_pair_reviewer(
    pair: pd.Series,
    index: int,
    total: int,
    dataset_name: str,
    fields: list[tuple[str, str]],
    decisions: dict,
    on_decision,
    extra_message: str = "",
) -> None:
    """
    Render satu kartu review lengkap: skor kemiripan, tabel perbandingan
    field, dan tombol keputusan panitia.

    Parameters
    ----------
    pair : pd.Series
        Satu baris hasil deteksi duplikat (dari find_duplicate_pairs / _register).
    index, total : int
        Posisi pasangan saat ini di antara sisa pasangan yang belum direview
        (dipakai untuk progress bar "pasangan ke-N dari M").
    dataset_name : str
        Label dataset untuk ditampilkan ("Login" / "Register").
    fields : list of (label, nama_kolom)
        Field yang ditampilkan di tabel perbandingan, berbeda antara Login & Register.
    decisions : dict
        Peta pair_id -> info keputusan yang sudah tersimpan sebelumnya.
    on_decision : callable(decision: str)
        Callback yang dipanggil saat panitia menekan salah satu tombol keputusan.
    extra_message : str, optional
        Catatan tambahan yang ditampilkan di samping skor (mis. nama kegiatan).
    """
    pair_id = pair["pair_id"]
    decided = decisions.get(pair_id)

    st.subheader(f"Review {dataset_name}: pasangan {index + 1} dari {total}")
    st.progress((index + 1) / max(total, 1))
    st.caption("Sistem hanya memberikan kandidat berdasarkan kemiripan data. Keputusan akhir tetap di tangan panitia.")

    col_score, col_note = st.columns([1, 2])
    with col_score:
        st.metric("Skor Kemiripan Akhir", f"{pair.get('final_score', 0):.2f}%")
    with col_note:
        st.caption(extra_message)

    # Rincian skor per-field — hanya field yang memang ada di hasil pairing ini
    # (Login vs Register punya skema skor berbeda).
    score_field_labels = [
        ("score_nama", "Nama"),
        ("score_dob", "Tanggal Lahir"),
        ("score_kelurahan", "Kelurahan"),
        ("score_area", "Area Program"),
        ("score_kepala_keluarga", "Kepala Keluarga"),
    ]
    available_scores = [(label, pair.get(key)) for key, label in score_field_labels if key in pair.index]

    if available_scores:
        cols = st.columns(len(available_scores))
        for col, (label, score) in zip(cols, available_scores):
            with col:
                st.metric(label, f"{float(score):.1f}%")

    st.markdown("### Bandingkan Data A dan Data B")
    st.dataframe(_field_table(pair, fields), use_container_width=True, hide_index=True)

    if decided:
        decision_label = DECISION_LABELS.get(decided.get("decision"), str(decided)) if isinstance(decided, dict) else str(decided)
        st.success(f"Keputusan tersimpan: {decision_label}")

    st.markdown("### Keputusan Panitia")
    btn_a, btn_b, btn_both, btn_skip = st.columns(4)

    with btn_a:
        if st.button("🅰️ Data A Benar", key=f"keep_a_{pair_id}", use_container_width=True):
            on_decision("keep_a")
    with btn_b:
        if st.button("🅱️ Data B Benar", key=f"keep_b_{pair_id}", use_container_width=True):
            on_decision("keep_b")
    with btn_both:
        if st.button("↔️ Keduanya Berbeda", key=f"keep_both_{pair_id}", use_container_width=True):
            on_decision("keep_both")
    with btn_skip:
        if st.button("⏭️ Lewati Dulu", key=f"skip_{pair_id}", use_container_width=True):
            st.toast("Pasangan ini belum diputuskan.")

    st.caption("🅰️/🅱️ berarti salah satu data adalah duplikat. ↔️ berarti kedua data dianggap peserta yang berbeda.")
