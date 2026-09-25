"""
attendance_dashboard.py
-------------------------
Fitur "Data Visualisasi" — dashboard cek kehadiran REAL-TIME/INDIKATIF,
TERPISAH dari wizard BTT resmi (sengaja lebih simpel & cepat, bukan
pengganti proses cleaning manual BTT).

PRINSIP DESAIN (hasil diskusi):
  - "Hadir" = ada di Login ATAU ada di Register untuk kombinasi
    (Tanggal + Judul Kegiatan) yang dipilih — beda dari BTT yang menganggap
    Register-tanpa-Login sebagai "belum lengkap", bukan "belum hadir".
  - Pencocokan Register<->Login MURNI pakai custom_id EXACT MATCH — TIDAK
    ADA fuzzy matching, TIDAK ADA fallback nama. Baris tanpa custom_id
    DIKELUARKAN dari perhitungan (bukan ditebak).
  - Dedup (baik Login maupun Register) juga murni by custom_id, exact.
  - Usia diambil dari 'usia' (field usia_final di Register), BUKAN dihitung
    dari tanggal lahir.
  - Hasil ditampilkan DIPECAH: berapa dari Login vs berapa tambahan
    auto-append dari Register — supaya panitia tahu komposisinya, bukan
    cuma 1 angka gabungan.

Modul ini TIDAK mengubah/memanggil apa pun dari alur wizard BTT (app.py
main()) — cuma pakai ulang kobo_api.fetch_kobo_data,
data_processor.resolve_custom_id_column, dan config column maps, supaya
konsisten (bukan reimplementasi field-mapping dari nol).
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

import config
from kobo_api import fetch_kobo_data, KoboAPIError
from data_processor import resolve_custom_id_column


# =========================================================
# PENGAMBILAN & PERSIAPAN DATA
# =========================================================
def _fetch_and_prepare(ap_config: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Tarik Login & Register untuk 1 AP, terapkan mapping kolom + resolusi
    custom_id — TIDAK pakai canonicalize_custom_ids (sengaja, supaya
    dashboard ini murni custom_id apa adanya dari tiap form, tanpa
    penyelarasan lintas-form yang lebih rumit)."""
    df_login = fetch_kobo_data(
        asset_uid=ap_config["login"], api_token=ap_config["token"],
        base_url=ap_config.get("base_url") or config.KOBO_ENDPOINT,
        column_map=config.LOGIN_COLUMN_MAP,
    )
    df_register = fetch_kobo_data(
        asset_uid=ap_config["register"], api_token=ap_config["token"],
        base_url=ap_config.get("base_url") or config.KOBO_ENDPOINT,
        column_map=config.REGISTER_COLUMN_MAP,
    )
    df_login = resolve_custom_id_column(df_login)
    df_register = resolve_custom_id_column(df_register)
    return df_login, df_register


def _clean_str_col(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series([""] * len(df), index=df.index)
    return df[col].fillna("").astype(str).str.strip()


def build_attendance(
    df_login: pd.DataFrame, df_register: pd.DataFrame, tanggal: str, judul: str,
) -> dict:
    """
    Hitung kehadiran untuk 1 kombinasi (Tanggal, Judul) — algoritma sesuai
    kesepakatan: filter ke acara -> buang baris tanpa custom_id -> dedup by
    custom_id -> cocokkan Register ke Login by custom_id -> Register yang
    TIDAK ketemu di Login -> auto-append.

    Returns dict: {"combined": DataFrame, "n_login": int, "n_appended": int}
    """
    # --- Filter ke acara yang dipilih ---
    login_mask = (_clean_str_col(df_login, "tanggal_kegiatan") == tanggal) & \
                 (_clean_str_col(df_login, "judul_kegiatan") == judul)
    register_mask = (_clean_str_col(df_register, "tanggal_kegiatan") == tanggal) & \
                     (_clean_str_col(df_register, "judul_kegiatan") == judul)

    login_event = df_login[login_mask].copy()
    register_event = df_register[register_mask].copy()

    # --- Buang baris tanpa custom_id (TIDAK ditebak lewat nama) ---
    login_event["custom_id"] = _clean_str_col(login_event, "custom_id")
    register_event["custom_id"] = _clean_str_col(register_event, "custom_id")
    login_event = login_event[login_event["custom_id"] != ""]
    register_event = register_event[register_event["custom_id"] != ""]

    # --- Dedup by custom_id (exact, ambil baris pertama) ---
    login_dedup = login_event.drop_duplicates(subset="custom_id", keep="first").copy()
    register_dedup = register_event.drop_duplicates(subset="custom_id", keep="first").copy()

    # --- Cocokkan: Register yang custom_id-nya TIDAK ada di Login -> auto-append ---
    login_ids = set(login_dedup["custom_id"])
    only_in_register = register_dedup[~register_dedup["custom_id"].isin(login_ids)].copy()

    login_dedup["Sumber"] = "Login"
    only_in_register["Sumber"] = "Register (auto-append)"

    # --- Lookup profil (Usia/Jenis Kelamin) dari SELURUH Register AP ini
    # (bukan cuma yang di acara ini) — supaya peserta yang hadir lewat Login
    # tetap dapat Usia/Jenis Kelamin-nya dari Register manapun dia pernah isi. ---
    profile_cols = [c for c in ["custom_id", "usia", "jenis_kelamin", "nama", "kategori_peserta"] if c in df_register.columns]
    profile_lookup = (
        df_register[profile_cols][_clean_str_col(df_register, "custom_id") != ""]
        .drop_duplicates(subset="custom_id", keep="first")
        .set_index("custom_id")
    )

    def _enrich(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        for col in ["usia", "jenis_kelamin", "kategori_peserta"]:
            if col in profile_lookup.columns:
                df[col] = df["custom_id"].map(profile_lookup[col]).fillna(df.get(col, ""))
            elif col not in df.columns:
                df[col] = ""
        if "nama" not in df.columns or df["nama"].eq("").all():
            if "nama" in profile_lookup.columns:
                df["nama"] = df["custom_id"].map(profile_lookup["nama"]).fillna(df.get("nama", ""))
        return df

    login_dedup = _enrich(login_dedup)
    only_in_register = _enrich(only_in_register)

    keep_cols = ["custom_id", "nama", "usia", "jenis_kelamin", "kategori_peserta", "Sumber"]
    for df_ in (login_dedup, only_in_register):
        for col in keep_cols:
            if col not in df_.columns:
                df_[col] = ""

    combined = pd.concat([login_dedup[keep_cols], only_in_register[keep_cols]], ignore_index=True)

    return {
        "combined": combined,
        "n_login": len(login_dedup),
        "n_appended": len(only_in_register),
    }


def _age_group_label(usia_num) -> str:
    """Kelompok usia, konsisten dengan kolom 'Age group' di BTT."""
    if pd.isna(usia_num):
        return "Tidak diketahui"
    usia_num = int(usia_num)
    if usia_num <= 5:
        return "0-5"
    if usia_num <= 11:
        return "06-11"
    if usia_num <= 17:
        return "12-17"
    return "18+"


def render_charts(filtered: pd.DataFrame, n_login: int, n_appended: int) -> None:
    """3 visualisasi sederhana: Sumber (donut), Jenis Kelamin (bar), Kelompok Usia (bar)."""
    st.subheader("📈 Visualisasi")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown("**Sumber Kehadiran**")
        if n_login + n_appended > 0:
            df_sumber = pd.DataFrame({
                "Sumber": ["Login", "Register (auto-append)"],
                "Jumlah": [n_login, n_appended],
            })
            fig = px.pie(
                df_sumber, names="Sumber", values="Jumlah", hole=0.5,
                color="Sumber",
                color_discrete_map={"Login": "#1E3A8A", "Register (auto-append)": "#F59E0B"},
            )
            fig.update_traces(textinfo="value+percent")
            fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), showlegend=True, height=280)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("Tidak ada data.")

    with col2:
        st.markdown("**Distribusi Jenis Kelamin**")
        gender_counts = filtered["jenis_kelamin"].replace("", "Tidak diketahui").value_counts()
        if not gender_counts.empty:
            fig = px.bar(
                x=gender_counts.index, y=gender_counts.values,
                labels={"x": "Jenis Kelamin", "y": "Jumlah"},
                color=gender_counts.index,
                color_discrete_sequence=px.colors.qualitative.Set2,
            )
            fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), showlegend=False, height=280)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("Tidak ada data.")

    with col3:
        st.markdown("**Distribusi Kelompok Usia**")
        age_groups = filtered["usia_num"].apply(_age_group_label)
        order = ["0-5", "06-11", "12-17", "18+", "Tidak diketahui"]
        age_counts = age_groups.value_counts().reindex(order).dropna()
        if not age_counts.empty:
            fig = px.bar(
                x=age_counts.index, y=age_counts.values,
                labels={"x": "Kelompok Usia", "y": "Jumlah"},
                color=age_counts.index,
                color_discrete_sequence=px.colors.qualitative.Pastel,
            )
            fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), showlegend=False, height=280)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("Tidak ada data.")


# =========================================================
# UI
# =========================================================
def render_attendance_dashboard(ap_asset_map: dict) -> None:
    st.title("📊 Data Visualisasi — Cek Kehadiran")
    st.caption(
        "Dashboard indikatif/real-time — BUKAN pengganti proses cleaning resmi BTT. "
        "\"Hadir\" = ada di Login ATAU Register untuk kegiatan ini (pencocokan murni by custom_id, tanpa fuzzy matching)."
    )

    if not ap_asset_map:
        st.error("⚠️ Belum ada Area Program terdaftar di secrets.toml.")
        return

    selected_ap = st.selectbox("1️⃣ Pilih Area Program", sorted(ap_asset_map.keys()), key="viz_ap")
    ap_config = ap_asset_map[selected_ap]

    if not ap_config.get("login") or not ap_config.get("register"):
        st.error(f"⚠️ Asset UID Login/Register untuk '{selected_ap}' belum lengkap di secrets.toml.")
        return

    load_key = "viz_data"
    loaded_ap_key = "viz_loaded_ap"

    if st.button("🔄 Tarik Data Login & Register", use_container_width=True, key="viz_fetch_btn"):
        with st.spinner("Mengambil data dari KoboToolbox..."):
            try:
                df_login, df_register = _fetch_and_prepare(ap_config)
                st.session_state[load_key] = (df_login, df_register)
                st.session_state[loaded_ap_key] = selected_ap
                st.success(f"✅ Login: {len(df_login):,} baris | Register: {len(df_register):,} baris.")
            except KoboAPIError as e:
                st.error(f"❌ Gagal mengambil data: {e}")
            except Exception as e:
                st.error(f"❌ Terjadi kesalahan tak terduga: {e}")

    if load_key not in st.session_state or st.session_state.get(loaded_ap_key) != selected_ap:
        st.info("📭 Klik tombol di atas untuk menarik data dulu.")
        return

    df_login, df_register = st.session_state[load_key]

    # --- Pilih acara: Tanggal -> Judul (bertingkat) ---
    tanggal_list = sorted({
        t for t in pd.concat([_clean_str_col(df_login, "tanggal_kegiatan"), _clean_str_col(df_register, "tanggal_kegiatan")])
        if t
    })
    if not tanggal_list:
        st.warning("⚠️ Tidak ada data dengan Tanggal Kegiatan yang terisi.")
        return

    selected_tanggal = st.selectbox("2️⃣ Pilih Tanggal Kegiatan", tanggal_list, key="viz_tanggal")

    judul_list = sorted({
        j for j in pd.concat([
            _clean_str_col(df_login[_clean_str_col(df_login, "tanggal_kegiatan") == selected_tanggal], "judul_kegiatan"),
            _clean_str_col(df_register[_clean_str_col(df_register, "tanggal_kegiatan") == selected_tanggal], "judul_kegiatan"),
        ])
        if j
    })
    if not judul_list:
        st.warning(f"⚠️ Tidak ada Judul Kegiatan untuk tanggal {selected_tanggal}.")
        return

    selected_judul = st.selectbox("3️⃣ Pilih Judul Kegiatan", judul_list, key="viz_judul")

    result = build_attendance(df_login, df_register, selected_tanggal, selected_judul)
    combined = result["combined"]

    st.divider()
    st.subheader("4️⃣ Filter (Opsional)")
    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        combined["usia_num"] = pd.to_numeric(combined["usia"], errors="coerce")
        has_age = combined["usia_num"].notna().any()
        if has_age:
            min_age, max_age = int(combined["usia_num"].min()), int(combined["usia_num"].max())
            if min_age == max_age:
                st.caption(f"Semua peserta berusia {min_age} tahun (tidak ada rentang untuk difilter).")
                age_range = (min_age, max_age)
            else:
                age_range = st.slider("Rentang Usia", min_age, max_age, (min_age, max_age), key="viz_age_filter")
        else:
            age_range = None
            st.caption("Tidak ada data usia untuk difilter.")
    with col_f2:
        gender_options = sorted({g for g in combined["jenis_kelamin"] if g})
        selected_genders = st.multiselect("Jenis Kelamin", gender_options, default=gender_options, key="viz_gender_filter")
    with col_f3:
        # Kategori Peserta (Peserta/Fasilitator/Pendamping/Staff WVI) di-traceback
        # dari Register via custom_id (Login tidak punya field ini). Berbeda dari
        # BTT, di sini SEKADAR FILTER pilihan panitia — bukan exclude otomatis.
        kategori_options = sorted({k for k in combined["kategori_peserta"] if k})
        if kategori_options:
            selected_kategori = st.multiselect("Kategori Peserta", kategori_options, default=kategori_options, key="viz_kategori_filter")
        else:
            selected_kategori = None
            st.caption("Field Kategori Peserta tidak ada di form ini.")

    filtered = combined.copy()
    if age_range:
        filtered = filtered[filtered["usia_num"].isna() | filtered["usia_num"].between(age_range[0], age_range[1])]
    if selected_genders:
        filtered = filtered[filtered["jenis_kelamin"].isin(selected_genders) | (filtered["jenis_kelamin"] == "")]
    if selected_kategori:
        filtered = filtered[filtered["kategori_peserta"].isin(selected_kategori) | (filtered["kategori_peserta"] == "")]

    st.divider()
    st.subheader("📊 Ringkasan Kehadiran")
    n_login_filtered = (filtered["Sumber"] == "Login").sum()
    n_appended_filtered = (filtered["Sumber"] == "Register (auto-append)").sum()

    col1, col2, col3 = st.columns(3)
    col1.metric("Dari Login", n_login_filtered)
    col2.metric("Tambahan dari Register", n_appended_filtered)
    col3.metric("Total Hadir", n_login_filtered + n_appended_filtered)

    st.divider()
    render_charts(filtered, n_login_filtered, n_appended_filtered)

    st.divider()
    st.subheader("📋 Detail Data")
    st.dataframe(
        filtered[["custom_id", "nama", "usia", "jenis_kelamin", "Sumber"]].rename(columns={
            "custom_id": "ID", "nama": "Nama", "usia": "Usia", "jenis_kelamin": "Jenis Kelamin",
        }),
        use_container_width=True, hide_index=True,
    )
