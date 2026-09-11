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
        config.SS_AP_CONTEXT: "(Manual)",
        config.SS_EVENT_PROJECT_DATA: {},  # dict: (tanggal, judul) -> {"codes": [...], "metadata": {...}}
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


def _get_distinct_events(df_login: pd.DataFrame) -> list[tuple[str, str]]:
    """
    Daftar kombinasi (Tanggal Kegiatan, Judul Kegiatan) unik di df_login,
    diurutkan berdasarkan tanggal. Dipakai sebagai sumber dropdown pemilih
    acara & tabel status keterisian Activity Code.
    """
    if df_login.empty or "tanggal_kegiatan" not in df_login.columns or "judul_kegiatan" not in df_login.columns:
        return []
    pairs = (
        df_login[["tanggal_kegiatan", "judul_kegiatan"]]
        .dropna(how="all")
        .astype(str)
        .apply(lambda s: s.str.strip())
        .drop_duplicates()
    )
    pairs = pairs[(pairs["tanggal_kegiatan"] != "") | (pairs["judul_kegiatan"] != "")]
    records = list(pairs.itertuples(index=False, name=None))
    return sorted(records, key=lambda r: (r[0], r[1]))


def _render_event_project_section(df_login: pd.DataFrame) -> None:
    """
    Bagian input PER ACARA — gabungan Activity Code + Info Project untuk BTT
    (Implementor, Sector, CPM, Project, Project Category, Activity, Activity
    Detail). SEMUANYA di-assign PER ACARA (kombinasi Tanggal + Judul
    Kegiatan), BUKAN satu set global untuk semua data — supaya panitia TIDAK
    perlu copy-paste 1 info project ke semua kegiatan yang beda.

    Alasan: 1 form Kobo mencakup 1 Area Program (bukan 1 acara), jadi data
    bisa punya banyak kombinasi tanggal+judul kegiatan berbeda, dan tiap
    acara bisa butuh Activity Code MAUPUN info project yang berbeda pula
    (boleh beda per tanggal, bahkan untuk judul kegiatan yang sama — mis.
    "Posyandu Balita" tiap bulan boleh beda kode/info tiap bulannya).

    Alur: panitia pilih 1 acara (dropdown Tanggal -> Judul, di-filter
    bertingkat), isi Activity Code DAN info project untuk acara itu
    sekaligus, klik Simpan. Diulang satu-per-satu untuk tiap acara yang ada
    di data.

    Data disimpan di session_state[SS_EVENT_PROJECT_DATA], dict:
    (tanggal, judul) -> {"codes": list[str], "metadata": dict[field, value]}.
    Saat export, tiap peserta dicocokkan ke acaranya sendiri lalu digandakan
    sesuai kode acara itu, dengan metadata MILIK ACARA ITU SENDIRI (lihat
    _build_btt_sheet) — peserta di acara berbeda bisa dapat kode & info
    project yang berbeda pula.
    """
    st.subheader("Activity Code & Info Project per Acara")
    st.caption(
        "Activity Code DAN info project (Implementor, Sector, CPM, dst.) di-assign PER ACARA "
        "(Tanggal + Judul Kegiatan) — karena 1 form Kobo mencakup banyak acara berbeda. "
        "Isi satu acara pada satu waktu — boleh beda kode/info untuk tanggal berbeda, "
        "meskipun judul kegiatannya sama."
    )

    events = _get_distinct_events(df_login)
    if not events:
        st.info("📭 Belum ada data Login dengan Tanggal/Judul Kegiatan untuk di-assign datanya.")
        return

    event_data_map = st.session_state[config.SS_EVENT_PROJECT_DATA]

    # --- Ringkasan status keterisian (supaya panitia tahu progres, walau input tetap satu-per-satu) ---
    def _is_assigned(ev) -> bool:
        data = event_data_map.get(ev, {})
        return any(c.strip() for c in data.get("codes", []))

    assigned_count = sum(1 for ev in events if _is_assigned(ev))
    st.progress(
        assigned_count / len(events) if events else 0.0,
        text=f"{assigned_count} dari {len(events)} acara sudah diisi Activity Code",
    )
    with st.expander(f"📋 Lihat status semua acara ({len(events)} acara)"):
        status_rows = []
        for tanggal, judul in events:
            data = event_data_map.get((tanggal, judul), {})
            codes = [c for c in data.get("codes", []) if c.strip()]
            metadata = data.get("metadata", {})
            metadata_filled = any(v.strip() for v in metadata.values())
            status_rows.append({
                "Tanggal Kegiatan": tanggal or "-",
                "Judul Kegiatan": judul or "-",
                "Activity Code": ", ".join(codes) if codes else "⚠️ Belum diisi",
                "Info Project": "✅ Terisi" if metadata_filled else "⚠️ Belum diisi",
            })
        st.dataframe(pd.DataFrame(status_rows), use_container_width=True, hide_index=True)

    st.divider()

    # --- Pemilih acara: Tanggal dulu, Judul mengikuti (cascading) ---
    distinct_tanggal = sorted({ev[0] for ev in events if ev[0]})
    if not distinct_tanggal:
        st.warning("⚠️ Data Login tidak punya Tanggal Kegiatan yang terisi.")
        return

    selected_tanggal = st.selectbox("1️⃣ Pilih Tanggal Kegiatan", distinct_tanggal, key="event_picker_tanggal")

    judul_for_tanggal = sorted({judul for (tgl, judul) in events if tgl == selected_tanggal and judul})
    if not judul_for_tanggal:
        st.warning(f"⚠️ Tidak ada Judul Kegiatan untuk tanggal {selected_tanggal}.")
        return

    selected_judul = st.selectbox("2️⃣ Pilih Judul Kegiatan", judul_for_tanggal, key=f"event_picker_judul_{selected_tanggal}")

    event_key = (selected_tanggal, selected_judul)
    existing_data = event_data_map.get(event_key, {})
    existing_codes = existing_data.get("codes", [""])
    existing_metadata = existing_data.get("metadata", {})
    current_count = len(existing_codes) if existing_codes else 1

    st.markdown(f"**Acara terpilih:** {selected_judul} — {selected_tanggal}")

    # --- 3) Activity Code untuk acara ini ---
    st.markdown("**3️⃣ Activity Code untuk acara ini**")
    count_options = ["1", "2", "3", "Lainnya"]
    default_index = count_options.index(str(current_count)) if str(current_count) in count_options else 3
    # key disertakan event_key supaya widget reset ke default baru saat ganti acara
    selected_option = st.selectbox(
        "Berapa Activity Code untuk acara ini?", count_options, index=default_index,
        key=f"code_count_{selected_tanggal}_{selected_judul}",
    )

    if selected_option == "Lainnya":
        n_codes = st.number_input(
            "Jumlah Activity Code",
            min_value=1, max_value=20,
            value=current_count if current_count > 3 else 4,
            step=1,
            key=f"code_count_custom_{selected_tanggal}_{selected_judul}",
        )
    else:
        n_codes = int(selected_option)

    new_codes = []
    code_cols = st.columns(3)
    for i in range(n_codes):
        default_val = existing_codes[i] if i < len(existing_codes) else ""
        with code_cols[i % 3]:
            val = st.text_input(
                f"Activity Code #{i + 1}",
                value=default_val,
                key=f"activity_code_input_{selected_tanggal}_{selected_judul}_{i}",
                help="Format wajib: xxx.xx.xx (contoh: 001.02.03)",
            )
        new_codes.append(val.strip())

    invalid_codes = [c for c in new_codes if c and not re.match(ACTIVITY_CODE_PATTERN, c)]
    if invalid_codes:
        st.warning(f"⚠️ Format salah (harus xxx.xx.xx): {', '.join(invalid_codes)}.")

    # --- 4) Info Project untuk acara ini ---
    st.markdown("**4️⃣ Info Project untuk acara ini**")
    new_metadata = {}
    meta_cols = st.columns(2)
    for i, field in enumerate(config.PROJECT_METADATA_FIELDS):
        with meta_cols[i % 2]:
            new_metadata[field] = st.text_input(
                field,
                value=existing_metadata.get(field, ""),
                key=f"event_meta_{selected_tanggal}_{selected_judul}_{field}",
            )

    if st.button("💾 Simpan untuk Acara Ini", type="primary"):
        event_data_map[event_key] = {"codes": new_codes, "metadata": new_metadata}
        st.session_state[config.SS_EVENT_PROJECT_DATA] = event_data_map
        st.rerun()

    # Catatan PERSISTEN (bukan toast sekilas yang keburu hilang gara-gara
    # rerun di atas) — selalu ditampilkan kalau acara yang SEDANG DIPILIH
    # memang sudah punya data tersimpan, baik baru saja disimpan maupun
    # sekadar membuka ulang acara yang sudah pernah diisi sebelumnya.
    saved = event_data_map.get(event_key, {})
    saved_codes = [c for c in saved.get("codes", []) if c.strip()]
    saved_metadata_filled = any(v.strip() for v in saved.get("metadata", {}).values())
    if saved_codes or saved_metadata_filled:
        st.caption(
            f"✅ Tersimpan untuk acara ini: "
            f"kode **{', '.join(saved_codes) if saved_codes else '(kosong)'}**, "
            f"info project {'**sudah diisi**' if saved_metadata_filled else '**belum diisi**'}."
        )
    else:
        st.caption("ℹ️ Acara ini belum punya Activity Code / Info Project tersimpan.")


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
    _render_event_project_section(df_login)

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


def _build_btt_sheet(df_login: pd.DataFrame, df_register: pd.DataFrame) -> pd.DataFrame:
    """
    Sheet BTT = data Login + kolom fiskal (Date, Month, Fiscal Year, Month (First))
    + kolom profil peserta (ID, Full Name, Household Name, Sex, Age, Age group,
    Category, Village, dst — diambil dari Register via custom_id) + kolom lokasi
    (AP/Province/District dari dropdown panitia; Zonal/Sub-District dari Village)
    + Output Code.

    PENTING: Activity Code DAN info project (Implementor/Sector/CPM/dst)
    SEKARANG di-assign PER ACARA (kombinasi Tanggal + Judul Kegiatan, lihat
    SS_EVENT_PROJECT_DATA) — BUKAN parameter/nilai global untuk semua data.
    Setiap peserta dicocokkan ke acaranya sendiri, lalu DIGANDAKAN sesuai
    jumlah kode yang ditempelkan ke acara itu, dengan info project MILIK
    ACARA ITU SENDIRI (bukan hasil copy-paste dari acara lain). Peserta di
    acara berbeda bisa dapat kode & info project yang berbeda pula. Acara
    yang belum diisi kodenya/info-nya tetap dapat 1 baris dengan Activity
    Code/Output Code/info project kosong (supaya baris peserta tidak hilang
    cuma karena belum sempat diisi).
    """
    df_base = add_fiscal_columns(df_login, date_column="tanggal_kegiatan")
    df_base = add_month_first_column(df_base, df_register, date_column="tanggal_kegiatan")
    df_base = add_participant_profile_columns(df_base, df_register)

    # "AP"/"Province"/"District" DARI DROPDOWN PANITIA (bukan field form Kobo).
    ap_label = st.session_state.get(config.SS_AP_CONTEXT, "(Manual)")
    df_base = add_location_context_columns(df_base, ap_label)

    event_data_map = st.session_state.get(config.SS_EVENT_PROJECT_DATA, {})

    if df_base.empty:
        df_btt = df_base.copy()
        df_btt["Activity Code"] = pd.Series(dtype=object)
        df_btt["Output Code"] = pd.Series(dtype=object)
        for field in config.PROJECT_METADATA_FIELDS:
            df_btt[field] = pd.Series(dtype=object)
    else:
        tanggal_norm = df_base.get("tanggal_kegiatan", pd.Series(index=df_base.index, dtype=object)).astype(str).str.strip()
        judul_norm = df_base.get("judul_kegiatan", pd.Series(index=df_base.index, dtype=object)).astype(str).str.strip()

        duplicated_parts = []
        for (tanggal, judul), group in df_base.groupby([tanggal_norm, judul_norm], dropna=False):
            event_data = event_data_map.get((tanggal, judul), {})
            codes = [c.strip() for c in event_data.get("codes", []) if c and c.strip()]
            event_metadata = event_data.get("metadata", {})
            if not codes:
                codes = [""]  # acara belum diisi kode -> tetap 1 baris, kode kosong

            for code in codes:
                df_copy = group.copy()
                df_copy["Activity Code"] = code
                df_copy["Output Code"] = _extract_output_code(code)
                # Info project MILIK ACARA INI (bukan global) — field yang
                # belum diisi untuk acara ini otomatis kosong, BUKAN ikut
                # nilai acara lain.
                for field in config.PROJECT_METADATA_FIELDS:
                    df_copy[field] = event_metadata.get(field, "")
                duplicated_parts.append(df_copy)

        df_btt = pd.concat(duplicated_parts, ignore_index=True) if duplicated_parts else df_base.copy()

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

    df_btt = _build_btt_sheet(df_login, df_register)

    # Peringatan: ada acara (Tanggal+Judul Kegiatan) yang belum diisi Activity
    # Code sama sekali — sesuai keputusan bisnis, TETAP BOLEH lanjut export
    # (bukan diblokir), baris peserta di acara itu cuma Activity Code/Output
    # Code/info project-nya kosong.
    events = _get_distinct_events(df_login)
    event_data_map = st.session_state.get(config.SS_EVENT_PROJECT_DATA, {})
    unassigned_events = [
        f"{judul} ({tanggal})" for tanggal, judul in events
        if not any(c.strip() for c in event_data_map.get((tanggal, judul), {}).get("codes", []))
    ]
    if unassigned_events:
        preview = ", ".join(unassigned_events[:5])
        more = f" (+{len(unassigned_events) - 5} lainnya)" if len(unassigned_events) > 5 else ""
        st.warning(
            f"⚠️ {len(unassigned_events)} acara belum diisi Activity Code: {preview}{more}. "
            "Export tetap bisa dilanjutkan — baris peserta acara itu Activity Code/Output Code-nya kosong. "
            "Lihat langkah ⑤ Finalisasi untuk mengisinya."
        )

    codes_with_bad_format = sorted({
        c.strip() for data in event_data_map.values() for c in data.get("codes", [])
        if c and c.strip() and not re.match(ACTIVITY_CODE_PATTERN, c.strip())
    })
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
