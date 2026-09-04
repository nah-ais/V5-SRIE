"""
app.py
------
Aplikasi Streamlit: SRIE Data Cleaning System.

UI berbentuk wizard 6-langkah supaya mudah diikuti panitia tanpa perlu
memahami detail teknis fuzzy matching di baliknya:

    ① Muat Data -> ② Pemeriksaan -> ③ Review Login -> ④ Review Register
    -> ⑤ Finalisasi -> ⑥ Export

Logika inti (matching, dedup, append) ada di matching.py & data_processor.py
dan TIDAK diubah oleh modul ini — app.py murni orkestrasi alur UI per langkah.

Jalankan dengan: streamlit run app.py
"""

from __future__ import annotations

import re

import pandas as pd
import streamlit as st

import config
from kobo_api import fetch_kobo_data, load_csv_fallback, KoboAPIError
from matching import find_duplicate_pairs, find_duplicate_pairs_register, find_registered_not_logged_in
from data_processor import (
    apply_review_decision,
    canonicalize_custom_ids,
    resolve_custom_id_column,
    resolve_register_duplicate,
    append_register_to_login,
    add_fiscal_columns,
    add_month_first_column,
    add_participant_profile_columns,
    add_location_context_columns,
    to_csv_bytes,
    to_excel_bytes,
    export_btt_to_mariadb,
    test_database_connection,
)
from services.workflow_service import (
    STEPS,
    init_workflow_state,
    reset_after_new_data,
    go_to,
    all_reviewed,
    reviewed_count,
    step_status,
)
from ui.components import inject_css, page_header, workflow_bar, metric_row, empty_state
from ui.reviewer import render_pair_reviewer

# =========================================================
# KONSTANTA
# =========================================================
ACTIVITY_CODE_PATTERN = r"^\d{3}\.\d{2}\.\d{2}$"  # format wajib: xxx.xx.xx

st.set_page_config(
    page_title="SRIE Data Cleaning System",
    page_icon="🧹",
    layout="wide",
)
inject_css()


# =========================================================
# SESSION STATE
# =========================================================
def ensure_row_uids(df: pd.DataFrame, dataset_label: str) -> pd.DataFrame:
    """Memberi key unik pada setiap submission agar id_kobo berulang tetap aman."""
    df = df.reset_index(drop=True).copy()
    if "_row_uid" not in df.columns:
        df["_row_uid"] = [f"{dataset_label}__{i}" for i in range(len(df))]
    return df


def init_session_state() -> None:
    """Inisialisasi seluruh key session_state (data + wizard) — aman dipanggil berulang."""
    defaults = {
        config.SS_LOGIN_DF: pd.DataFrame(),
        config.SS_REGISTER_DF: pd.DataFrame(),
        config.SS_DUPLICATE_PAIRS_LOGIN: pd.DataFrame(),
        config.SS_DUPLICATE_PAIRS_REGISTER: pd.DataFrame(),
        config.SS_NOT_LOGIN_YET: pd.DataFrame(),
        config.SS_REVIEW_DECISIONS_LOGIN: {},
        config.SS_REVIEW_DECISIONS_REGISTER: {},
        config.SS_APPENDED_IDS: set(),
        config.SS_PROJECT_METADATA: {field: "" for field in config.PROJECT_METADATA_FIELDS},
        config.SS_METADATA_APPLIED: False,
        config.SS_AP_CONTEXT: "(Manual)",
        config.SS_ACTIVITY_CODES: [""],  # default: 1 kotak Activity Code kosong
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    init_workflow_state()


# =========================================================
# SIDEBAR
# =========================================================
def render_sidebar() -> float:
    """
    Sidebar berisi: progres wizard, status jumlah data, dan threshold deduplikasi.
    Threshold SELALU bisa diatur (tidak menunggu pemeriksaan pertama selesai)
    supaya panitia bisa menyesuaikan sebelum menjalankan pemeriksaan.
    """
    with st.sidebar:
        st.title("🧹 SRIE")
        st.caption("Panduan proses panitia")

        for key, label in STEPS:
            state = step_status(key)
            icon = "🟢" if state == "done" else "🔵" if state == "active" else "⚪"
            st.write(f"{icon} {label}")

        st.divider()
        st.subheader("📊 Status Data")
        st.write(f"Login: **{len(st.session_state[config.SS_LOGIN_DF]):,}**")
        st.write(f"Register: **{len(st.session_state[config.SS_REGISTER_DF]):,}**")

        st.divider()
        st.subheader("⚙️ Pengaturan Matching")
        threshold = st.slider(
            "Threshold (%)",
            min_value=50.0,
            max_value=100.0,
            value=float(config.DUPLICATE_THRESHOLD),
            step=0.5,
            help="Ambang batas skor kemiripan untuk dianggap 'Potensi Double Count'. "
                 "Berlaku untuk pemeriksaan berikutnya yang dijalankan.",
        )

        st.divider()
        if st.button("↩️ Kembali ke Muat Data", use_container_width=True):
            go_to("load")
            st.rerun()

    return threshold


# =========================================================
# LANGKAH ①: MUAT DATA
# =========================================================
def _render_kobo_api_form(selected_ap: str) -> None:
    """Form untuk menarik data langsung dari KoboToolbox API, dengan preset dari AP terpilih."""
    default_token = config.KOBO_TOKEN
    default_login = config.FORM_UID_LOGIN
    default_register = config.FORM_UID_REGISTRASI
    if selected_ap != "(Manual)":
        ap_cfg = config.AP_ASSET_MAP[selected_ap]
        default_token = ap_cfg.get("token") or config.KOBO_TOKEN
        default_login = ap_cfg["login"]
        default_register = ap_cfg["register"]

    if not config.AP_ASSET_MAP:
        st.caption(
            "ℹ️ Belum ada Area Program yang terdaftar di secrets.toml. "
            "Isi kredensial secara manual di bawah, atau tambahkan blok "
            "`[kobo.ap.NamaAP]` di secrets.toml supaya muncul di dropdown ini."
        )

    with st.form("load_kobo_form"):
        col1, col2 = st.columns(2)
        with col1:
            api_token = st.text_input(
                "API Token", value=default_token, type="password", key=f"token_{selected_ap}"
            )
            base_url = st.text_input("Base URL", value=config.KOBO_ENDPOINT)
        with col2:
            asset_login = st.text_input("Asset UID Login", value=default_login, key=f"uid_login_{selected_ap}")
            asset_register = st.text_input("Asset UID Register", value=default_register, key=f"uid_register_{selected_ap}")

        submitted = st.form_submit_button("📥 Muat Data dari KoboToolbox", use_container_width=True)

    if not submitted:
        return

    if not api_token:
        st.error("❌ API Token wajib diisi.")
        return
    if not asset_login or not asset_register:
        st.error("❌ Asset UID Login dan Register wajib diisi.")
        return

    try:
        with st.spinner(f"Mengambil data ({selected_ap}) dari KoboToolbox..."):
            df_login = fetch_kobo_data(asset_login, api_token, config.LOGIN_COLUMN_MAP, base_url)
            df_register = fetch_kobo_data(asset_register, api_token, config.REGISTER_COLUMN_MAP, base_url)

        df_login = resolve_custom_id_column(df_login)
        df_register = resolve_custom_id_column(df_register)
        df_login, df_register = canonicalize_custom_ids(
            ensure_row_uids(df_login, "Login"),
            ensure_row_uids(df_register, "Register"),
        )
        st.session_state[config.SS_LOGIN_DF] = df_login
        st.session_state[config.SS_REGISTER_DF] = df_register
        reset_after_new_data()
        st.success(f"✅ Data berhasil dimuat. Login: {len(df_login):,} | Register: {len(df_register):,}")
        st.rerun()
    except KoboAPIError as e:
        st.error(f"❌ Gagal mengambil data: {e}")
    except Exception as e:
        st.error(f"❌ Terjadi kesalahan tak terduga: {e}")


def _render_csv_upload_form() -> None:
    """Form fallback untuk memuat data dari file CSV hasil export manual KoboToolbox."""
    col1, col2 = st.columns(2)
    with col1:
        file_login = st.file_uploader("CSV Form Login", type=["csv"])
    with col2:
        file_register = st.file_uploader("CSV Form Register", type=["csv"])

    if not st.button("📥 Muat Data CSV", type="primary", use_container_width=True):
        return

    if file_login is None or file_register is None:
        st.error("❌ Upload kedua file CSV terlebih dahulu.")
        return

    try:
        df_login = ensure_row_uids(load_csv_fallback(file_login, config.LOGIN_COLUMN_MAP), "Login")
        df_register = ensure_row_uids(load_csv_fallback(file_register, config.REGISTER_COLUMN_MAP), "Register")
        df_login = resolve_custom_id_column(df_login)
        df_register = resolve_custom_id_column(df_register)
        df_login, df_register = canonicalize_custom_ids(df_login, df_register)
        st.session_state[config.SS_LOGIN_DF] = df_login
        st.session_state[config.SS_REGISTER_DF] = df_register
        reset_after_new_data()
        st.success("✅ Data CSV berhasil dimuat.")
        st.rerun()
    except Exception as e:
        st.error(f"❌ Gagal memuat CSV: {e}")


def render_load_step() -> None:
    st.header("① Muat Data")
    st.caption("Mulai dengan mengambil dataset Login dan Register. Data lama akan diganti setelah proses berhasil.")

    # Dropdown AP berlaku untuk KEDUA mode (API maupun CSV) — ini SUMBER TUNGGAL
    # untuk kolom "AP" (dan turunannya: Province/District) di sheet BTT nanti,
    # BUKAN diambil dari field form Kobo mana pun. Tersimpan di session_state
    # supaya tetap "diingat" sepanjang sesi, dipakai lagi saat build BTT.
    ap_options = ["(Manual)"] + list(config.AP_ASSET_MAP.keys())
    default_ap_index = 0
    current_ap = st.session_state.get(config.SS_AP_CONTEXT, "(Manual)")
    if current_ap in ap_options:
        default_ap_index = ap_options.index(current_ap)
    selected_ap = st.selectbox(
        "Area Program",
        ap_options,
        index=default_ap_index,
        help=(
            "Menentukan Asset UID default (mode API) DAN kolom 'AP'/'Province'/'District' "
            "di sheet BTT nanti — berlaku untuk mode API maupun Upload CSV."
        ),
    )
    st.session_state[config.SS_AP_CONTEXT] = selected_ap

    mode = st.radio("Pilih sumber data", ["KoboToolbox API", "Upload CSV"], horizontal=True)

    if mode == "KoboToolbox API":
        _render_kobo_api_form(selected_ap)
    else:
        _render_csv_upload_form()

    df_login = st.session_state[config.SS_LOGIN_DF]
    df_register = st.session_state[config.SS_REGISTER_DF]
    if df_login.empty and df_register.empty:
        return

    st.divider()
    st.subheader("Ringkasan Data Saat Ini")
    metric_row([
        ("Total Login", f"{len(df_login):,}"),
        ("Total Register", f"{len(df_register):,}"),
        ("Total Data", f"{len(df_login) + len(df_register):,}"),
    ])
    if st.button("Lanjut ke Pemeriksaan →", type="primary"):
        go_to("check")
        st.rerun()


# =========================================================
# LANGKAH ②: PEMERIKSAAN
# =========================================================
def _estimate_login_pairs(df: pd.DataFrame) -> int:
    """Global pairwise count: every Login row can be compared with every other row."""
    n = len(df)
    return n * (n - 1) // 2 if n > 1 else 0


def _run_full_check(df_login: pd.DataFrame, df_register: pd.DataFrame, threshold: float) -> None:
    """Jalankan deteksi duplikat Login, duplikat Register, dan cek Register-belum-Login sekaligus."""
    progress = st.progress(0, text="Menyiapkan pemeriksaan...")
    status = st.empty()

    try:
        status.info("Memeriksa potensi duplikat Login...")

        def _progress_login(current, total):
            progress.progress(min(45, int(current / max(total, 1) * 45)), text=f"Memeriksa Login: {current:,}/{total:,} pasangan")

        dup_login = find_duplicate_pairs(df_login, threshold=threshold, progress_callback=_progress_login, dataset_label="Login")

        status.info("Memeriksa potensi duplikat Register...")

        def _progress_register(current, total):
            progress.progress(45 + int(current / max(total, 1) * 45), text=f"Memeriksa Register: {current:,}/{total:,} pasangan")

        dup_register = find_duplicate_pairs_register(df_register, threshold=threshold, progress_callback=_progress_register)

        status.info("Memeriksa peserta Register yang belum memiliki Login...")
        progress.progress(95, text="Memeriksa kelengkapan Login...")
        not_logged = find_registered_not_logged_in(df_login, df_register, threshold=threshold)

        st.session_state[config.SS_DUPLICATE_PAIRS_LOGIN] = dup_login
        st.session_state[config.SS_DUPLICATE_PAIRS_REGISTER] = dup_register
        st.session_state[config.SS_NOT_LOGIN_YET] = not_logged
        st.session_state[config.SS_REVIEW_DECISIONS_LOGIN] = {}
        st.session_state[config.SS_REVIEW_DECISIONS_REGISTER] = {}
        st.session_state["login_review_index"] = 0
        st.session_state["register_review_index"] = 0
        st.session_state["matching_completed"] = True

        progress.progress(100, text="Pemeriksaan selesai")
        status.success("✅ Pemeriksaan selesai.")
    except Exception as e:
        status.empty()
        st.error(f"❌ Pemeriksaan gagal: {e}")


def render_check_step(threshold: float) -> None:
    st.header("② Pemeriksaan Data")
    df_login = st.session_state[config.SS_LOGIN_DF]
    df_register = st.session_state[config.SS_REGISTER_DF]

    if df_login.empty and df_register.empty:
        empty_state("Belum ada data", "Kembali ke langkah Muat Data terlebih dahulu.")
        if st.button("← Kembali ke Muat Data"):
            go_to("load")
            st.rerun()
        return

    st.markdown("### Apa yang akan diperiksa?")
    col1, col2, col3 = st.columns(3)
    col1.info("**Login**\n\nFuzzy pairwise global; kegiatan/tanggal dipakai sebagai konteks review.")
    col2.info("**Register**\n\nFuzzy pairwise global; kegiatan/tanggal dipakai sebagai konteks review.")
    col3.info("**Kelengkapan Login**\n\nMencari peserta Register yang belum tercatat Login pada kegiatan terkait.")

    n_reg = len(df_register)
    est_register_pairs = n_reg * (n_reg - 1) // 2 if n_reg > 1 else 0
    st.caption(
        f"Threshold aktif: **{threshold:.1f}%** · "
        f"Estimasi perbandingan: Login **{_estimate_login_pairs(df_login):,} pasangan**, "
        f"Register **{est_register_pairs:,} pasangan**."
    )

    if st.button("🔍 Mulai Pemeriksaan Data", type="primary", use_container_width=True):
        _run_full_check(df_login, df_register, threshold)

    if not st.session_state["matching_completed"]:
        return

    dup_login = st.session_state[config.SS_DUPLICATE_PAIRS_LOGIN]
    dup_register = st.session_state[config.SS_DUPLICATE_PAIRS_REGISTER]
    not_logged = st.session_state[config.SS_NOT_LOGIN_YET]

    st.divider()
    st.subheader("Hasil Pemeriksaan")
    metric_row([
        ("Potensi Duplikat Login", len(dup_login)),
        ("Potensi Duplikat Register", len(dup_register)),
        ("Register Belum Login", len(not_logged)),
    ])

    if st.button("Lanjut ke Review Login →", type="primary"):
        go_to("review_login")
        st.rerun()


# =========================================================
# LANGKAH ③ & ④: REVIEW LOGIN / REGISTER
# =========================================================
def render_review_login_step() -> None:
    st.header("③ Review Login")
    pairs = st.session_state[config.SS_DUPLICATE_PAIRS_LOGIN]
    decisions = st.session_state[config.SS_REVIEW_DECISIONS_LOGIN]

    if pairs.empty:
        st.success("✅ Tidak ada potensi duplikat Login yang perlu direview.")
        if st.button("Lanjut ke Review Register →", type="primary"):
            go_to("review_register")
            st.rerun()
        return

    remaining = pairs[~pairs["pair_id"].isin(decisions.keys())]
    st.caption(f"Sudah direview: {len(pairs) - len(remaining)} dari {len(pairs)} pasangan.")

    if remaining.empty:
        st.success("✅ Semua pasangan Login sudah memiliki keputusan.")
        if st.button("Lanjut ke Review Register →", type="primary"):
            go_to("review_register")
            st.rerun()
        return

    idx = min(st.session_state["login_review_index"], len(remaining) - 1)
    pair = remaining.iloc[idx]

    def decide(decision: str) -> None:
        current_df = st.session_state[config.SS_LOGIN_DF]
        st.session_state[config.SS_LOGIN_DF] = apply_review_decision(current_df, pair, decision)
        decisions[pair["pair_id"]] = {"decision": decision, "id_a": pair["id_a"], "id_b": pair["id_b"]}
        st.session_state[config.SS_REVIEW_DECISIONS_LOGIN] = decisions
        st.session_state["login_review_index"] = 0
        st.rerun()

    render_pair_reviewer(
        pair, idx, len(remaining), "Login",
        fields=[
            ("Nama", "nama"),
            ("Tanggal Lahir", "tanggal_lahir"),
            ("Kelurahan", "kelurahan"),
            ("Area Program", "area_program"),
            ("Judul Kegiatan", "judul_kegiatan"),
            ("Tanggal Kegiatan", "tanggal_kegiatan"),
            ("Waktu Submit", "timestamp_submit"),
        ],
        decisions=decisions,
        on_decision=decide,
        extra_message=(f"Event A: {pair.get('judul_kegiatan_a', '-')} | {pair.get('tanggal_kegiatan_a', '-')} · "
                       f"Event B: {pair.get('judul_kegiatan_b', '-')} | {pair.get('tanggal_kegiatan_b', '-')} · "
                       f"{'EVENT SAMA → fokus double count' if bool(pair.get('same_event')) else 'EVENT BERBEDA → Keep Both bila keduanya valid'}"),
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Pasangan Sebelumnya", disabled=idx == 0):
            st.session_state["login_review_index"] = idx - 1
            st.rerun()
    with col2:
        if st.button("Pasangan Berikutnya →", disabled=idx >= len(remaining) - 1):
            st.session_state["login_review_index"] = idx + 1
            st.rerun()


def render_review_register_step() -> None:
    st.header("④ Review Register")
    pairs = st.session_state[config.SS_DUPLICATE_PAIRS_REGISTER]
    decisions = st.session_state[config.SS_REVIEW_DECISIONS_REGISTER]

    if pairs.empty:
        st.success("✅ Tidak ada potensi duplikat Register yang perlu direview.")
        if st.button("Lanjut ke Finalisasi →", type="primary"):
            go_to("finalize")
            st.rerun()
        return

    remaining = pairs[~pairs["pair_id"].isin(decisions.keys())]
    st.caption(f"Sudah direview: {len(pairs) - len(remaining)} dari {len(pairs)} pasangan.")

    if st.session_state.get("last_register_decision_info"):
        st.info(st.session_state["last_register_decision_info"])

    if remaining.empty:
        st.success("✅ Semua pasangan Register sudah memiliki keputusan.")
        if st.button("Lanjut ke Finalisasi →", type="primary"):
            go_to("finalize")
            st.rerun()
        return

    idx = min(st.session_state["register_review_index"], len(remaining) - 1)
    pair = remaining.iloc[idx]

    def decide(decision: str) -> None:
        df_login_new, df_register_new, info = resolve_register_duplicate(
            st.session_state[config.SS_LOGIN_DF],
            st.session_state[config.SS_REGISTER_DF],
            pair,
            decision,
        )
        st.session_state[config.SS_LOGIN_DF] = df_login_new
        st.session_state[config.SS_REGISTER_DF] = df_register_new
        decisions[pair["pair_id"]] = {"decision": decision, "id_a": pair["id_a"], "id_b": pair["id_b"]}
        st.session_state[config.SS_REVIEW_DECISIONS_REGISTER] = decisions
        st.session_state["last_register_decision_info"] = info
        st.session_state["register_review_index"] = 0
        st.rerun()

    render_pair_reviewer(
        pair, idx, len(remaining), "Register",
        fields=[
            ("Nama", "nama"),
            ("Tanggal Lahir", "tanggal_lahir"),
            ("Nama Kepala Keluarga", "nama_kepala_keluarga"),
            ("Kegiatan", "judul_kegiatan"),
            ("Tanggal Kegiatan", "tanggal_kegiatan"),
            ("Waktu Submit", "timestamp_submit"),
        ],
        decisions=decisions,
        on_decision=decide,
        extra_message=(
            f"Event A: {pair.get('judul_kegiatan_a', '-')} | {pair.get('tanggal_kegiatan_a', '-')} · "
            f"Event B: {pair.get('judul_kegiatan_b', '-')} | {pair.get('tanggal_kegiatan_b', '-')} · "
            f"{'EVENT SAMA → fokus double register' if bool(pair.get('same_event')) else 'EVENT BERBEDA → review lalu Keep Both bila valid'}"
        ),
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Pasangan Sebelumnya", disabled=idx == 0):
            st.session_state["register_review_index"] = idx - 1
            st.rerun()
    with col2:
        if st.button("Pasangan Berikutnya →", disabled=idx >= len(remaining) - 1):
            st.session_state["register_review_index"] = idx + 1
            st.rerun()


# =========================================================
# LANGKAH ⑤: FINALISASI
# =========================================================
def _render_pending_login_section(df_login: pd.DataFrame, df_register: pd.DataFrame, threshold: float) -> None:
    """Bagian 'Peserta Register yang Belum Login' pada langkah Finalisasi."""
    st.subheader("Peserta Register yang Belum Login")

    if st.button("🔄 Perbarui Daftar Belum Login"):
        st.session_state[config.SS_NOT_LOGIN_YET] = find_registered_not_logged_in(
            df_login, df_register, threshold=threshold
        )
        st.rerun()

    pending = st.session_state[config.SS_NOT_LOGIN_YET]
    metric_row([
        ("Login Final Saat Ini", len(df_login)),
        ("Register Final Saat Ini", len(df_register)),
        ("Belum Login", len(pending)),
    ])

    if pending.empty:
        st.success("✅ Tidak ada peserta Register yang perlu ditambahkan ke Login.")
        return

    display_cols = [
        c for c in ["custom_id", "id_kobo", "nama", "judul_kegiatan", "tanggal_kegiatan"]
        if c in pending.columns
    ]
    st.dataframe(pending[display_cols], use_container_width=True, hide_index=True)

    pending = pending.copy()
    if "_row_uid" not in pending.columns:
        pending["_row_uid"] = [f"pending__{i}" for i in range(len(pending))]

    row_uid_to_label = {
        r["_row_uid"]: f"{r.get('nama', '-')} | {r.get('tanggal_kegiatan', '-')} | {r.get('judul_kegiatan', '-')}"
        for _, r in pending.iterrows()
    }
    selected_row_uids = st.multiselect(
        "Pilih submission yang ingin ditambahkan ke Login",
        options=pending["_row_uid"].tolist(),
        format_func=lambda uid: row_uid_to_label.get(uid, uid),
        default=[],
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("➕ Tambahkan Peserta Terpilih", disabled=not selected_row_uids, use_container_width=True):
            st.session_state[config.SS_LOGIN_DF] = append_register_to_login(
                st.session_state[config.SS_LOGIN_DF],
                st.session_state[config.SS_REGISTER_DF],
                selected_row_uids,
            )
            st.session_state[config.SS_APPENDED_IDS].update(selected_row_uids)
            st.session_state[config.SS_NOT_LOGIN_YET] = pending[
                ~pending["_row_uid"].isin(selected_row_uids)
            ].reset_index(drop=True)
            st.success(f"✅ {len(selected_row_uids)} submission berhasil ditambahkan.")
            st.rerun()
    with col2:
        if st.button("➕ Tambahkan Semua", use_container_width=True):
            row_uids = pending["_row_uid"].tolist()
            st.session_state[config.SS_LOGIN_DF] = append_register_to_login(
                st.session_state[config.SS_LOGIN_DF],
                st.session_state[config.SS_REGISTER_DF],
                row_uids,
            )
            st.session_state[config.SS_APPENDED_IDS].update(row_uids)
            st.session_state[config.SS_NOT_LOGIN_YET] = pd.DataFrame()
            st.success(f"✅ {len(row_uids)} submission berhasil ditambahkan ke Login.")
            st.rerun()


def _render_activity_code_section() -> None:
    """
    Bagian input Activity Code — TERPISAH dari metadata biasa karena 1 kegiatan
    bisa masuk ke BEBERAPA Activity Code sekaligus. Panitia pilih jumlah kode
    (dropdown 1/2/3/Lainnya), sistem generate kotak input sejumlah itu.

    Setiap kode disimpan di session_state[SS_ACTIVITY_CODES] (list[str]).
    Saat export, df BTT akan DIGANDAKAN 1 baris per peserta PER kode di sini
    (lihat _build_btt_sheet) — bukan digabung jadi 1 kolom/1 baris saja.
    """
    st.subheader("Activity Code")
    st.caption(
        "Satu kegiatan bisa masuk ke beberapa Activity Code sekaligus. "
        "Data peserta akan **digandakan otomatis** di sheet BTT — 1 baris per peserta, "
        "PER Activity Code yang dipilih di sini."
    )

    current_codes = st.session_state[config.SS_ACTIVITY_CODES]
    current_count = len(current_codes) if current_codes else 1

    count_options = ["1", "2", "3", "Lainnya"]
    default_index = count_options.index(str(current_count)) if str(current_count) in count_options else 3
    selected_option = st.selectbox("Berapa Activity Code?", count_options, index=default_index)

    if selected_option == "Lainnya":
        n_codes = st.number_input(
            "Jumlah Activity Code",
            min_value=1, max_value=20,
            value=current_count if current_count > 3 else 4,
            step=1,
        )
    else:
        n_codes = int(selected_option)

    new_codes = []
    cols = st.columns(3)
    for i in range(n_codes):
        default_val = current_codes[i] if i < len(current_codes) else ""
        with cols[i % 3]:
            val = st.text_input(
                f"Activity Code #{i + 1}",
                value=default_val,
                key=f"activity_code_input_{i}",
                help="Format wajib: xxx.xx.xx (contoh: 001.02.03)",
            )
        new_codes.append(val.strip())
    st.session_state[config.SS_ACTIVITY_CODES] = new_codes

    invalid_codes = [c for c in new_codes if c and not re.match(ACTIVITY_CODE_PATTERN, c)]
    if invalid_codes:
        st.warning(
            f"⚠️ Format salah (harus xxx.xx.xx): {', '.join(invalid_codes)}. "
            "Output Code untuk kode ini akan dikosongkan sampai formatnya diperbaiki."
        )

    filled_codes = [c for c in new_codes if c]
    if filled_codes:
        st.caption(f"📋 Preview: setiap peserta akan muncul **{len(filled_codes)}x** di BTT (1x per kode terisi).")


def _render_project_metadata_section() -> None:
    """Bagian 'Informasi Project untuk BTT' pada langkah Finalisasi (TIDAK termasuk Activity Code)."""
    st.subheader("Informasi Project untuk BTT")
    st.caption("Nilai di bawah akan diterapkan ke seluruh baris sheet BTT saat export (Langkah ⑥).")

    metadata = st.session_state[config.SS_PROJECT_METADATA]
    cols = st.columns(2)
    for i, field in enumerate(config.PROJECT_METADATA_FIELDS):
        with cols[i % 2]:
            metadata[field] = st.text_input(field, value=metadata.get(field, ""), key=f"meta_{field}")
    st.session_state[config.SS_PROJECT_METADATA] = metadata


def render_finalize_step(threshold: float) -> None:
    st.header("⑤ Finalisasi")
    df_login = st.session_state[config.SS_LOGIN_DF]
    df_register = st.session_state[config.SS_REGISTER_DF]

    login_pairs = st.session_state[config.SS_DUPLICATE_PAIRS_LOGIN]
    register_pairs = st.session_state[config.SS_DUPLICATE_PAIRS_REGISTER]
    login_done = all_reviewed(login_pairs, st.session_state[config.SS_REVIEW_DECISIONS_LOGIN])
    register_done = all_reviewed(register_pairs, st.session_state[config.SS_REVIEW_DECISIONS_REGISTER])

    st.subheader("Checklist Proses")
    st.write(("🟢" if login_done else "🟡") + " Review Login selesai")
    st.write(("🟢" if register_done else "🟡") + " Review Register selesai")
    if not login_done or not register_done:
        st.warning("⚠️ Masih ada pasangan yang belum direview. Anda tetap dapat kembali ke langkah sebelumnya untuk menyelesaikannya.")

    st.divider()
    _render_pending_login_section(df_login, df_register, threshold)

    st.divider()
    _render_activity_code_section()

    st.divider()
    _render_project_metadata_section()

    if st.button("Lanjut ke Export →", type="primary"):
        go_to("export")
        st.rerun()


# =========================================================
# LANGKAH ⑥: EXPORT
# =========================================================
def _extract_output_code(activity_code: str) -> str:
    """Ambil bagian 'xxx.xx' dari format 'xxx.xx.xx' pada Activity Code. Contoh: '001.02.03' -> '001.02'."""
    activity_code = activity_code.strip()
    match = re.match(r"^(\d{3}\.\d{2})\.\d{2}$", activity_code)
    return match.group(1) if match else ""


def _build_btt_sheet(df_login: pd.DataFrame, df_register: pd.DataFrame, metadata: dict) -> pd.DataFrame:
    """
    Sheet BTT = data Login + kolom fiskal (Date, Month, Fiscal Year, Month (First))
    + kolom profil peserta (ID, Full Name, Household Name, Sex, Age, Age group,
    Category, Village, dst — diambil dari Register via custom_id) + kolom lokasi
    (AP/Province/District dari dropdown panitia; Zonal/Sub-District dari Village)
    + metadata project (di-duplicate ke seluruh baris) + Output Code.

    PENTING: kalau ada LEBIH DARI 1 Activity Code (lihat SS_ACTIVITY_CODES),
    setiap peserta DIGANDAKAN — 1 baris per Activity Code — karena 1 kegiatan
    bisa masuk ke beberapa Activity Code sekaligus. Kolom lain (Nama, Age, dst)
    identik antar baris duplikat, cuma 'Activity Code' & 'Output Code' yang beda.
    """
    df_base = add_fiscal_columns(df_login, date_column="tanggal_kegiatan")
    df_base = add_month_first_column(df_base, df_register, date_column="tanggal_kegiatan")
    df_base = add_participant_profile_columns(df_base, df_register)

    # "AP"/"Province"/"District" DARI DROPDOWN PANITIA (bukan field form Kobo).
    ap_label = st.session_state.get(config.SS_AP_CONTEXT, "(Manual)")
    df_base = add_location_context_columns(df_base, ap_label)

    for field, value in metadata.items():
        df_base[field] = value

    # Ambil daftar Activity Code yang terisi (buang yang kosong). Kalau tidak
    # ada satu pun terisi, tetap 1 baris per peserta dengan kode & Output Code
    # kosong (supaya BTT tidak hilang total cuma karena kode belum diisi).
    activity_codes = [c.strip() for c in st.session_state.get(config.SS_ACTIVITY_CODES, []) if c and c.strip()]
    if not activity_codes:
        activity_codes = [""]

    duplicated_parts = []
    for code in activity_codes:
        df_copy = df_base.copy()
        df_copy["Activity Code"] = code
        df_copy["Output Code"] = _extract_output_code(code)
        duplicated_parts.append(df_copy)

    df_btt = pd.concat(duplicated_parts, ignore_index=True) if len(duplicated_parts) > 1 else duplicated_parts[0]

    BTT_COLUMNS = [
        "Implementor", "Sector", "CPM", "Project", "Project Category",
        "Output Code", "Activity Code", "Activity", "Activity Detail",
        "Date", "Month", "Month (First)", "Fiscal Year",
        "ID", "Full Name", "Household Name", "Sex", "Age", "Age group", "Category",
        "Level Beneficiary",  # TODO: masih kosong sementara, belum ada sumber/aturan datanya
        "AP", "Province", "District", "Zonal", "Sub-District",
        "Village", "Sub-Village 1", "Sub-Village 2",
        "Disability Category", "Disability Status",
        "RC", "RC Status", "IDN",
        "MVC- Dimensi 1", "MVC- Dimensi 2", "MVC- Dimensi 3", "MVC- Dimensi 4", "MVC",
        "Social Protection",
        "SP - Cash transfers/food assistance",
        "SP - Health assistance",
        "SP - Education Assistance",
        "Institution", "Position", "No.Handphone (WA)",
        "# Child <5", "# Child 6-11", "# Child 12-17",
    ]

    for col in BTT_COLUMNS:
        if col not in df_btt.columns:
            df_btt[col] = pd.NA

    df_btt = df_btt[BTT_COLUMNS].copy()
    df_btt = df_btt.replace(r"^\s*$", pd.NA, regex=True)

    return df_btt


def render_export_step() -> None:
    st.header("⑥ Export")
    df_login = st.session_state[config.SS_LOGIN_DF].copy()
    df_register = st.session_state[config.SS_REGISTER_DF].copy()
    metadata = st.session_state[config.SS_PROJECT_METADATA]

    login_pairs = st.session_state[config.SS_DUPLICATE_PAIRS_LOGIN]
    register_pairs = st.session_state[config.SS_DUPLICATE_PAIRS_REGISTER]
    login_done = all_reviewed(login_pairs, st.session_state[config.SS_REVIEW_DECISIONS_LOGIN])
    register_done = all_reviewed(register_pairs, st.session_state[config.SS_REVIEW_DECISIONS_REGISTER])

    if not register_done:
        pending_count = len(register_pairs) - reviewed_count(register_pairs, st.session_state[config.SS_REVIEW_DECISIONS_REGISTER])
        st.warning(
            f"⚠️ Masih ada **{pending_count} pasangan Register** yang belum direview (lihat langkah ④). "
            "Selama Register belum benar-benar unik, kolom profil di sheet BTT (Household Name, Sex, "
            "Age, MVC, dsb.) bisa mengambil data dari baris yang salah. Sebaiknya selesaikan review "
            "Register dulu sebelum export."
        )
    if not login_done:
        pending_count = len(login_pairs) - reviewed_count(login_pairs, st.session_state[config.SS_REVIEW_DECISIONS_LOGIN])
        st.warning(f"⚠️ Masih ada **{pending_count} pasangan Login** yang belum direview (lihat langkah ③).")

    st.success("✅ Data siap diekspor.")
    metric_row([
        ("Login Final", len(df_login)),
        ("Register Final", len(df_register)),
        ("Total Append dari Register", len(st.session_state[config.SS_APPENDED_IDS])),
    ])

    df_btt = _build_btt_sheet(df_login, df_register, metadata)
    codes_with_bad_format = [
        c for c in st.session_state.get(config.SS_ACTIVITY_CODES, [])
        if c and c.strip() and not re.match(ACTIVITY_CODE_PATTERN, c.strip())
    ]
    if codes_with_bad_format:
        st.warning(
            f"⚠️ Activity Code berikut formatnya belum sesuai xxx.xx.xx: {', '.join(codes_with_bad_format)}. "
            "'Output Code' untuk baris kode ini akan kosong."
        )

    st.subheader("Download")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.download_button(
            "📄 Login Clean CSV", data=to_csv_bytes(df_login),
            file_name="login_clean.csv", mime="text/csv", use_container_width=True,
        )
    with col2:
        st.download_button(
            "📄 Register Clean CSV", data=to_csv_bytes(df_register),
            file_name="register_clean.csv", mime="text/csv", use_container_width=True,
        )
    with col3:
        try:
            excel_data = to_excel_bytes({"Login": df_login, "Register": df_register, "BTT": df_btt})
            st.download_button(
                "📊 Excel Lengkap", data=excel_data,
                file_name="dataset_kehadiran_clean.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary", use_container_width=True,
            )
        except RuntimeError as e:
            st.error(str(e))

    st.divider()
    with st.expander("Preview BTT"):
        st.dataframe(df_btt.head(20), use_container_width=True, hide_index=True)

    # --- Tambahan UI Export Database di render_export_step() ---
    st.divider()
    st.subheader("💾 Export ke Database (MariaDB)")

    st.caption(
        f"Target Server: **{config.DB_HOST}:{config.DB_PORT}** | "
        f"Database: **{config.DB_NAME}** | "
        f"Tabel: **{config.DB_TABLE}**"
    )

    col_db1, col_db2 = st.columns(2)
    with col_db1:
        if st.button("🔌 Tes Koneksi Database", use_container_width=True):
            with st.spinner("Mencoba menghubungkan ke database..."):
                ok, msg = test_database_connection(
                    host=config.DB_HOST,
                    port=config.DB_PORT,
                    user=config.DB_USER,
                    password=config.DB_PASSWORD,
                    db_name=config.DB_NAME,
                )
                if ok:
                    st.success(f"✅ {msg}")
                else:
                    st.error(f"❌ {msg}")
    with col_db2:
        if st.button("🚀 Simpan Data ke MariaDB", type="primary", use_container_width=True):
            with st.spinner("Menghubungkan dan mengekspor ke MariaDB..."):
                success, msg = export_btt_to_mariadb(
                    df_btt=df_btt,
                    host=config.DB_HOST,
                    port=config.DB_PORT,
                    user=config.DB_USER,
                    password=config.DB_PASSWORD,
                    db_name=config.DB_NAME,
                    table_name=config.DB_TABLE,
                )
                if success:
                    st.success(f"✅ {msg}")
                else:
                    st.error(f"❌ {msg}")
    # -----------------------------------------------------------

    if st.button("🔄 Mulai Proses Baru"):
        go_to("load")
        st.rerun()


# =========================================================
# MAIN
# =========================================================
def main() -> None:
    init_session_state()
    threshold = render_sidebar()

    page_header()
    workflow_bar(STEPS, st.session_state["current_step"], step_status)
    st.divider()

    step = st.session_state["current_step"]
    step_renderers = {
        "load": lambda: render_load_step(),
        "check": lambda: render_check_step(threshold),
        "review_login": lambda: render_review_login_step(),
        "review_register": lambda: render_review_register_step(),
        "finalize": lambda: render_finalize_step(threshold),
        "export": lambda: render_export_step(),
    }
    step_renderers[step]()


if __name__ == "__main__":
    main()
