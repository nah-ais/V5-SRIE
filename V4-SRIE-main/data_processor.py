"""
data_processor.py
------------------
Helper untuk operasi data level-aplikasi:
  - Menyiapkan dataset gabungan (Login + Register) untuk matching.
  - Menerapkan keputusan reviewer (Simpan A/B, Keep keduanya).
  - Append data Register -> Login.
  - Export hasil akhir ke CSV/Excel.

Modul ini murni memanipulasi pandas DataFrame; tidak ada elemen UI di sini
supaya mudah di-unit-test terpisah dari Streamlit.
"""

from __future__ import annotations

import io
import re
import pandas as pd

import config
from matching import check_person_logged_in_for_event
# find_earliest_register_date_for_person (fuzzy match) TIDAK dipakai lagi sejak
# custom_id tersedia di kedua form — lihat add_month_first_column() di bawah.
# Fungsinya tetap dipertahankan di matching.py sebagai cadangan/referensi.



def _norm_identity(value) -> str:
    """Normalisasi field identitas untuk canonicalisasi custom_id."""
    if pd.isna(value):
        return ""
    return " ".join(str(value).strip().lower().split())


def canonicalize_custom_ids(
    df_login: pd.DataFrame,
    df_register: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Menyatukan custom_id yang berbeda untuk orang yang sama tanpa menggabungkan
    record kegiatannya.

    Prinsip bisnis:
      - `custom_id` = identitas peserta, sehingga satu orang mempertahankan satu
        ID lintas kegiatan.
      - Satu baris Login/Register tetap merupakan record event tersendiri.
      - Jika peserta yang sama memiliki beberapa custom_id, ID dari submission
        pertama (berdasarkan timestamp_submit) menjadi canonical ID.
      - Identitas peserta ditentukan dari nama + tanggal lahir + kelurahan, bukan
        dari judul/tanggal kegiatan, sehingga orang yang sama di kegiatan berbeda
        tetap memakai custom_id yang sama.

    Fungsi ini TIDAK melakukan deduplikasi row.
    """
    login = df_login.copy()
    register = df_register.copy()

    frames = []
    for label, frame in (("Login", login), ("Register", register)):
        if frame.empty:
            continue
        work = frame.copy()
        for col in ("nama", "tanggal_lahir", "kelurahan"):
            if col not in work.columns:
                work[col] = ""
        if "custom_id" not in work.columns:
            work["custom_id"] = ""
        work["_source_dataset"] = label
        work["_source_order"] = range(len(work))
        work["_identity_key"] = (
            work["nama"].map(_norm_identity) + "||" +
            work["tanggal_lahir"].map(_norm_identity) + "||" +
            work["kelurahan"].map(_norm_identity)
        )
        work["_submit_dt"] = work.get("timestamp_submit", pd.Series(index=work.index)).map(_parse_event_date)
        frames.append(work)

    if not frames:
        return login, register

    combined = pd.concat(frames, ignore_index=True, sort=False)
    canonical = {}

    for key, group in combined.groupby("_identity_key", dropna=False):
        if not key or key == "||||":
            continue
        valid = group[group["custom_id"].notna() & (group["custom_id"].astype(str).str.strip() != "")].copy()
        if valid.empty:
            continue
        valid = valid.sort_values(
            by=["_submit_dt", "_source_order"],
            na_position="last",
            kind="stable",
        )
        canonical_id = str(valid.iloc[0]["custom_id"]).strip()
        canonical[key] = canonical_id

    combined["custom_id"] = combined.apply(
        lambda row: canonical.get(row["_identity_key"], row["custom_id"]),
        axis=1,
    )

    # Kembalikan ke dataset asal dan pertahankan urutan baris.
    login_out = combined[combined["_source_dataset"] == "Login"].sort_values("_source_order", kind="stable").drop(
        columns=["_source_dataset", "_source_order", "_identity_key", "_submit_dt"], errors="ignore"
    ).reset_index(drop=True)
    register_out = combined[combined["_source_dataset"] == "Register"].sort_values("_source_order", kind="stable").drop(
        columns=["_source_dataset", "_source_order", "_identity_key", "_submit_dt"], errors="ignore"
    ).reset_index(drop=True)

    # Pastikan schema kosong tetap sama seperti input.
    if login.empty:
        login_out = login
    if register.empty:
        register_out = register

    return login_out, register_out

def _parse_event_date(value):
    """
    Coba parse tanggal_kegiatan ke datetime untuk perbandingan 'acara terbaru'.

    Mencoba format umum secara berurutan agar robust terhadap variasi input
    dari Kobo (ISO 'YYYY-MM-DD', atau format lokal 'DD-MM-YYYY'/'DD/MM/YYYY'):
      1. Parse tanpa asumsi dayfirst (menangani ISO 'YYYY-MM-DD' dengan benar).
      2. Jika gagal (NaT), coba ulang dengan dayfirst=True (menangani 'DD-MM-YYYY').
    """
    if pd.isna(value):
        return pd.NaT
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=False)
    if pd.isna(parsed):
        parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    return parsed


# =========================================================
# KOLOM FISKAL UNTUK SHEET BTT (Date, Month, Fiscal Year)
# =========================================================
# Tahun fiskal organisasi dimulai Oktober, bukan Januari:
#   Bulan Oktober = bulan fiskal ke-1 ... September = bulan fiskal ke-12.
# Contoh: Agustus -> bulan fiskal ke-11 -> "11|Aug"
#         Juli     -> bulan fiskal ke-10 -> "10|Jul"
_MONTH_ABBR_EN = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


def _fiscal_month_label(date: pd.Timestamp) -> str:
    """
    Format 'Month' ala fiskal: '{nomor_bulan_fiskal}|{singkatan_bulan}'.
    Oktober=1 ... September=12. Contoh: Agustus -> '11|Aug'.
    """
    fiscal_month_number = ((date.month - 10) % 12) + 1
    return f"{fiscal_month_number}|{_MONTH_ABBR_EN[date.month]}"


def _fiscal_year_label(date: pd.Timestamp) -> str:
    """
    Format 'Fiscal Year': 'FY' + 2 digit tahun.
    Bulan < Oktober -> tahun kalender sama. Bulan >= Oktober -> tahun + 1.
    Contoh: Agustus 2025 -> FY25. Oktober 2025 -> FY26.
    """
    fiscal_year_full = date.year + 1 if date.month >= 10 else date.year
    return f"FY{fiscal_year_full % 100:02d}"


def add_fiscal_columns(df_login: pd.DataFrame, date_column: str = "tanggal_kegiatan") -> pd.DataFrame:
    """
    Menambahkan 3 kolom turunan ke salinan df_login untuk kebutuhan sheet BTT:
      - 'Date'        : tanggal acara (dari kolom `date_column`), diformat 'YYYY-MM-DD'.
      - 'Month'       : label bulan fiskal, format '{no_bulan_fiskal}|{singkatan}'.
      - 'Fiscal Year' : format 'FYxx' (lihat _fiscal_year_label).

    Baris dengan tanggal yang tidak bisa di-parse akan dikosongkan (bukan error),
    supaya satu baris data bermasalah tidak menggagalkan seluruh proses export.

    Catatan: kolom 'Month (First)' (bulan pertama kali join, dari Register)
    SENGAJA TIDAK dihitung di sini — akan diimplementasikan setelah sistem ID
    peserta siap, supaya pencocokan Register -> Login akurat (bukan fuzzy-match
    yang berisiko salah pasang untuk keperluan pelaporan resmi seperti BTT).

    Returns
    -------
    pd.DataFrame
        Salinan df_login + kolom 'Date', 'Month', 'Fiscal Year'.
    """
    df = df_login.copy()

    if date_column not in df.columns or df.empty:
        df["Date"] = ""
        df["Month"] = ""
        df["Fiscal Year"] = ""
        return df

    parsed_dates = df[date_column].apply(_parse_event_date)

    df["Date"] = parsed_dates.apply(lambda d: d.strftime("%Y-%m-%d") if pd.notna(d) else "")
    df["Month"] = parsed_dates.apply(lambda d: _fiscal_month_label(d) if pd.notna(d) else "")
    df["Fiscal Year"] = parsed_dates.apply(lambda d: _fiscal_year_label(d) if pd.notna(d) else "")

    return df


def add_month_first_column(
    df_login: pd.DataFrame,
    df_register: pd.DataFrame,
    date_column: str = "tanggal_kegiatan",
) -> pd.DataFrame:
    """Add Month (First) using the earliest valid Register event date per participant.

    Primary lookup: custom_id. Fallback: normalized full name. This is intentionally
    independent of the Login event date because the value represents the participant's
    first registration/joining month.
    """
    df = df_login.copy()
    df["Month (First)"] = ""
    if df.empty or df_register.empty or date_column not in df_register.columns:
        return df

    reg = df_register.copy()
    reg["_parsed_date"] = reg[date_column].apply(_parse_event_date)
    reg = reg.dropna(subset=["_parsed_date"]).copy()
    if reg.empty:
        return df

    def clean_series(series):
        return series.fillna("").astype(str).str.strip()

    # Lookup tables by custom_id and, independently, by full name.
    id_map = {}
    if "custom_id" in reg.columns:
        for key, g in reg[clean_series(reg["custom_id"]) != ""].groupby(clean_series(reg["custom_id"]), sort=False):
            id_map[key] = g["_parsed_date"].min()

    name_map = {}
    if "nama" in reg.columns:
        names = clean_series(reg["nama"]).map(_norm_identity)
        for key, g in reg[names != ""].groupby(names, sort=False):
            name_map[key] = g["_parsed_date"].min()

    login_ids = clean_series(df.get("custom_id", pd.Series(index=df.index, dtype=object)))
    login_names = df.get("nama", pd.Series(index=df.index, dtype=object)).map(_norm_identity)

    def lookup(row):
        cid = str(row.get("_cid", "")).strip()
        name = str(row.get("_name", "")).strip()
        if cid and cid in id_map:
            return _fiscal_month_label(id_map[cid])
        if name and name in name_map:
            return _fiscal_month_label(name_map[name])
        return ""

    temp = pd.DataFrame({"_cid": login_ids, "_name": login_names}, index=df.index)
    df["Month (First)"] = temp.apply(lookup, axis=1)
    return df


# =========================================================
# KOLOM PROFIL / PROGRAM UNTUK SHEET BTT (dari Form Register)
# =========================================================
_AGE_GROUP_BINS = [
    (0, 5, "0-5"),
    (6, 11, "06-11"),
    (12, 17, "12-17"),
]

# Alias internal + variasi nama field yang umum keluar dari Kobo.
# Jika form berubah, tambahkan alias di sini tanpa mengubah logic BTT.
BTT_REGISTER_FIELD_ALIASES = {
    "Household Name": [
        "nama_kepala_keluarga", "Nama Kepala Keluarga", "household_name",
        "nama kepala keluarga", "kepala_keluarga",
    ],
    "Sex": [
        "jenis_kelamin", "Jenis Kelamin", "sex", "gender",
    ],
    "Age": [
        "usia", "Age", "age", "umur",
    ],
    "Disability Status": [
        "disability_status", "Disability Status", "status_disabilitas",
        "status disabilitas", "disability status",
        "tipe_disabilitas", "Apakah_Anda_memiliki_kebutuhan",
        "apakah anda memiliki kebutuhan",
    ],
    # --- Disability Category: 6 pertanyaan skrining terpisah (Washington Group
    # style), masing-masing di grup group_digital_absensi/group_eb6jo83/group_mg5uv10.
    # DITURUNKAN (bukan dipetakan langsung) di _compute_disability_category() —
    # lihat _DISABILITY_CATEGORY_RULES di bawah.
    "_Disability_Vision_Raw": [
        "group_digital_absensi/group_eb6jo83/group_mg5uv10/Apakah_Anda_mengalam_menggunakan_kacamata",
    ],
    "_Disability_Hearing_Raw": [
        "group_digital_absensi/group_eb6jo83/group_mg5uv10/Apakah_Anda_kesulita_an_alat_bantu_dengar",
    ],
    "_Disability_Physical_Raw": [
        "group_digital_absensi/group_eb6jo83/group_mg5uv10/Apakah_Anda_kesulita_atau_menaiki_tangga",
    ],
    "_Disability_Intellectual_Raw": [
        "group_digital_absensi/group_eb6jo83/group_mg5uv10/Apakah_Anda_mengalam_gat_atau_konsentrasi",
    ],
    "_Disability_Mental_Raw": [
        "group_digital_absensi/group_eb6jo83/group_mg5uv10/Apakah_Anda_kesulitan_untuk_me",
    ],
    "_Disability_Speech_Raw": [
        "group_digital_absensi/group_eb6jo83/group_mg5uv10/Apakah_Anda_kesulita_tau_memahami_sesuatu",
    ],
    # --- Disability Category UNTUK ANAK (usia < 18) — instrumen BEDA dari
    # dewasa, di grup group_digital_absensi/group_ys2ge24/group_xq1az57.
    # Beberapa kategori punya lebih dari 1 field (relasi OR) karena pertanyaan
    # child-disability sering bercabang ("Jika YA..." vs "Jika TIDAK...").
    "_ChildDis_VisionA_Raw": [
        "group_digital_absensi/group_ys2ge24/group_xq1az57/Jika_YA_Ketika_mem_ak_kesulita",
    ],
    "_ChildDis_VisionB_Raw": [
        "group_digital_absensi/group_ys2ge24/group_xq1az57/Jika_TIDAK_Jika_an_ia_kesulita",
    ],
    "_ChildDis_HearingA_Raw": [
        "group_digital_absensi/group_ys2ge24/group_xq1az57/Jika_YA_Ketika_ana_kesulitan_m",
    ],
    "_ChildDis_HearingB_Raw": [
        "group_digital_absensi/group_ys2ge24/group_xq1az57/Jika_TIDAK_Tanpa_a_kesulitan_m",
    ],
    "_ChildDis_HearingC_Raw": [
        "group_digital_absensi/group_ys2ge24/group_xq1az57/Apakah_anak_menggunakan_Bahasa",
    ],
    "_ChildDis_MobilityA_Raw": [
        "group_digital_absensi/group_ys2ge24/group_xq1az57/Dibandingkan_dengan_k_kesulita",
    ],
    "_ChildDis_MobilityB_Raw": [
        "group_digital_absensi/group_ys2ge24/group_xq1az57/Apakah_anak_mengguna_untuk_bis",
    ],
    "_ChildDis_CommA_Raw": [
        "group_digital_absensi/group_ys2ge24/group_xq1az57/Ketika_berbicara_ap_tinggal_be_001",
    ],
    "_ChildDis_CommB_Raw": [
        "group_digital_absensi/group_ys2ge24/group_xq1az57/Ketika_berbicara_ap_tinggal_be",
    ],
    "_ChildDis_SelfcareA_Raw": [
        "group_digital_absensi/group_ys2ge24/group_xq1az57/Dibandingkan_dengan_cil_dengan",
    ],
    "_ChildDis_SelfcareB_Raw": [
        "group_digital_absensi/group_ys2ge24/group_xq1az57/Apakah_anak_memiliki_n_pakaian",
    ],
    "RC": [
        "rc", "RC", "relational_capital", "relational capital",
        "Apakah_wakil_anak_Ya_Tidak", "apakah_wakil_anak_ya_tidak",
        "wakil_anak",
        # Path lengkap terkonfirmasi dari scan explore_kobo_schema.py.
        "group_digital_absensi/group_ys2ge24/group_cn7re50/Apakah_wakil_anak_Ya_Tidak",
    ],
    "RC Status": [
        "rc_status", "RC Status", "status_rc", "status rc", "rc status", "IDN RC Status",
    ],
    "IDN": [
        "idn", "IDN", "idn_status", "IDN Status", "idn status",
    ],
    # 4 kolom "MVC- Dimensi ..." SENGAJA TIDAK dicari sebagai kolom terpisah di
    # sini — nilainya dihitung dari jawaban gabungan "_MVC_Raw" via substring
    # match (MVC_MARKERS), sama seperti pola SP - Raw. Field mentah aktual:
    # SATU pertanyaan multi-select "Apakah anak/keluarga ini termasuk rentan
    # atau membutuhkan perhatian khusus?" (nama kolom: Apakah_anak_keluarga_i_kerenta).
    "_MVC_Raw": [
        "Apakah_anak_keluarga_i_kerenta", "apakah_anak_keluarga_i_kerenta",
        "mvc_raw", "kerentanan_anak", "kategori_kerentanan",
        # Nama kolom mentah PERSIS dikonfirmasi pengguna (path grup Kobo lengkap).
        "group_digital_absensi/group_ys2ge24/group_cn7re50/Apakah_anak_keluarga_i_kerenta",
        # Header breadcrumb PERSIS (format ekspor LABEL Kobo).
        "DIGITAL ABSENSI / ANAK / INFORMASI LAINNYA / Apakah anak/keluarga Anda mengalami salah satu kondisi kerentanan berikut?",
    ],
    "SP - Cash transfers/food assistance": [
        "sp_cash_transfers", "SP - Cash transfers/food assistance",
        "cash_transfers", "food_assistance", "sp_cash", "sp_cash_transfer",
    ],
    "SP - Health assistance": [
        "sp_health_assistance", "SP - Health assistance", "health_assistance",
        "sp_health", "sp_healthcare",
    ],
    "SP - Education Assistance": [
        "sp_education_assistance", "SP - Education Assistance", "education_assistance",
        "sp_education", "education assistance",
    ],
    # Field mentah Kobo AKTUAL: SATU pertanyaan multi-select ("Apakah Anda/keluarga
    # menerima bantuan sosial berikut?") yang jawabannya berisi GABUNGAN label
    # pilihan yang dicentang, dipisah koma, mis.:
    #   "Layanan kesehatan (KIS, BPJS Kesehatan, Kartu Disabilitas), Bantuan
    #    pendidikan (PIP, Sekolah Rakyat, KJP)"
    # Karena label opsi itu SENDIRI mengandung koma di dalam kurung, split by
    # comma TIDAK BISA dipakai untuk memisahkan antar pilihan — deteksi per
    # kategori dilakukan dengan substring match (lihat _sp_flag / SP_MARKERS).
    "_SP_Raw": [
        "Bantuan_Proteksi_Sos_erima_dal",
        "bantuan_sosial", "proteksi_sosial", "social_protection",
        "bantuan_yang_diterima", "jenis_bantuan_sosial", "sp_raw",
        "apakah_anda_atau_keluarga_menerima_bantuan",
        # Path lengkap terkonfirmasi dari scan explore_kobo_schema.py.
        "group_digital_absensi/group_eb6jo83/group_gj4hg92/Bantuan_Proteksi_Sos_erima_dal",
    ],
    "Institution": [
        "institution", "Institution", "instansi",
        # Path lengkap terkonfirmasi dari scan explore_kobo_schema.py.
        "group_digital_absensi/group_eb6jo83/group_pw2gv45/Asal_Instansi",
    ],
    "Position": [
        "position", "Position", "jabatan",
        # Path lengkap terkonfirmasi dari scan explore_kobo_schema.py.
        "group_digital_absensi/group_eb6jo83/group_pw2gv45/Posisi_Jabatan",
    ],
    "No.Handphone (WA)": [
        "nomor_hp", "no_handphone", "No.Handphone (WA)", "nomor_handphone",
        "nomor wa", "no hp", "phone", "whatsapp", "wa",
    ],
    "# Child <5": [
        "child_under_5", "# Child <5", "jumlah_child_under_5", "jumlah anak <5",
        "child <5", "child_0_5", "jumlah_anak_0_5",
        "group_digital_absensi/group_eb6jo83/group_gj4hg92/Jumlah_anak_yang_diasuh_0_5_ta",
    ],
    "# Child 6-11": [
        "child_6_11", "# Child 6-11", "jumlah_child_6_11", "jumlah anak 6-11",
        "child 6-11", "child_6_11_count", "jumlah_anak_6_11",
        "group_digital_absensi/group_eb6jo83/group_gj4hg92/Jumlah_anak_yang_diasuh_6_11_t",
    ],
    "# Child 12-17": [
        "child_12_17", "# Child 12-17", "jumlah_child_12_17", "jumlah anak 12-17",
        "child 12-17", "child_12_17_count", "jumlah_anak_12_17",
        "group_digital_absensi/group_eb6jo83/group_gj4hg92/Jumlah_anak_yang_diasuh_12_17_",
    ],
    # --- Kolom lokasi (Village + Sub-Village) — diambil dari Register ---
    # "Village" utamanya = Kelurahan pilihan dropdown. "_Village_Other" =
    # jawaban isian bebas "Tuliskan Kelurahannya" (dipakai kalau field
    # Kelurahan dropdown-nya kosong/menunjuk opsi "lainnya"). Dikombinasikan
    # di add_participant_profile_columns() (bukan di sini) karena butuh
    # logika prioritas antar 2 kolom, bukan cuma 1 alias resolusi biasa.
    "Village": ["kelurahan", "Kelurahan"],
    "_Village_Other": [
        "kelurahan_lainnya", "Tuliskan_Kelurahannya", "kelurahan lainnya",
        "tuliskan kelurahannya",
    ],
    "Sub-Village 1": ["rw", "RW"],
    "Sub-Village 2": ["rt", "RT"],
}

# =========================================================
# LOOKUP LOKASI: Village -> Sub-District -> Zonal, dan AP -> Province/District
# =========================================================
# Kunci dinormalisasi (huruf kecil, underscore/strip jadi spasi, spasi
# dirapikan) sebelum dicocokkan — supaya tahan terhadap slug mentah Kobo
# (mis. "tanah_kali_kedinding") MAUPUN label berspasi biasa
# (mis. "Tanah Kali Kedinding").
VILLAGE_TO_SUBDISTRICT = {
    "simolawang": "Simokerto",
    "sidodadi": "Simokerto",
    "tambakrejo": "Simokerto",
    "tanah kali kedinding": "Kenjeran",
    "bulak banteng": "Kenjeran",
}

SUBDISTRICT_TO_ZONAL = {
    "simokerto": "Surabaya Pusat",
    "kenjeran": "Surabaya Utara",
}

# HANYA berisi AP yang aturannya sudah pasti (AP-Simokerto). AP lain yang
# belum diatur SENGAJA dikosongkan (bukan ditebak) — lihat keputusan bisnis
# terkait, supaya data tidak salah diasumsikan. Tambah baris baru di sini
# kalau nanti ada AP lain (mis. AP-Kenjeran) yang aturannya sudah dipastikan.
AP_TO_PROVINCE_DISTRICT = {
    "AP-Simokerto": ("Jawa Timur", "Kota Surabaya"),
}

# Override Zonal PER-AP: kalau AP terpilih ada di sini, SEMUA baris untuk AP
# itu dipaksa pakai nilai Zonal ini (menimpa hasil turunan Sub-District di
# atas). AP yang TIDAK terdaftar di sini tetap pakai Zonal hasil lookup
# SUBDISTRICT_TO_ZONAL seperti biasa (fallback, tidak berubah).
AP_TO_ZONAL_OVERRIDE = {
    "AP-Simokerto": "Sambawa",
}


def _normalize_place_name(value) -> str:
    """Normalisasi nama tempat untuk pencocokan lookup: lower, underscore/strip
    jadi spasi, spasi ganda dirapikan jadi satu. Supaya 'tanah_kali_kedinding'
    (slug mentah Kobo) dan 'Tanah Kali Kedinding' (label rapi) sama-sama cocok."""
    if pd.isna(value):
        return ""
    text = str(value).strip().lower().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", text).strip()


def _lookup_place(mapping: dict, value) -> str:
    """Cari `value` (dinormalisasi) di `mapping` (key HARUS sudah huruf kecil
    apa adanya, tanpa underscore). Kembalikan '' kalau tidak ketemu (bukan
    menebak)."""
    key = _normalize_place_name(value)
    return mapping.get(key, "")


# =========================================================
# DISABILITY CATEGORY — 6 pertanyaan skrining (Washington Group style)
# =========================================================
# Setiap tuple: (nama field raw di BTT_REGISTER_FIELD_ALIASES, set nilai
# jawaban yang dianggap "mengalami kesulitan", label kategori hasil).
#
# CATATAN: pertanyaan "Vision" pakai nilai jawaban yang BEDA dari 5 pertanyaan
# lain ("ymb_kacamata"/"tbs_kacamata" vs "ya__mengalami_banyak_kesulitan"/
# "option_4") — ini sudah dikonfirmasi bukan typo, cuma choice-list Kobo-nya
# memang beda per pertanyaan.
_DISABILITY_CATEGORY_RULES = [
    ("_Disability_Vision_Raw", {"ymb_kacamata", "tbs_kacamata"}, "Vision"),
    ("_Disability_Hearing_Raw", {"ya__mengalami_banyak_kesulitan", "option_4"}, "Hearing"),
    ("_Disability_Physical_Raw", {"ya__mengalami_banyak_kesulitan", "option_4"}, "Physical"),
    ("_Disability_Intellectual_Raw", {"ya__mengalami_banyak_kesulitan", "option_4"}, "Intellectual"),
    ("_Disability_Mental_Raw", {"ya__mengalami_banyak_kesulitan", "option_4"}, "Mental"),
    ("_Disability_Speech_Raw", {"ya__mengalami_banyak_kesulitan", "option_4"}, "Speech"),
]

# Nilai jawaban skala kesulitan yang dianggap "mengalami kesulitan" untuk
# instrumen ANAK (beda field values dari instrumen dewasa di atas).
_CHILD_DIFFICULTY_TRIGGERS = {"tidak_bisa_sama_sekali", "banyak_kesulitan"}

# Setiap kategori anak bisa dipicu oleh LEBIH DARI SATU field (relasi OR,
# karena pertanyaan child-disability sering bercabang "Jika YA..."/"Jika
# TIDAK..."). Kalau SALAH SATU field dalam daftar cocok -> kategori itu match.
_CHILD_DISABILITY_CATEGORY_RULES = [
    ("Penglihatan", [
        ("_ChildDis_VisionA_Raw", _CHILD_DIFFICULTY_TRIGGERS),
        ("_ChildDis_VisionB_Raw", _CHILD_DIFFICULTY_TRIGGERS),
    ]),
    ("Pendengaran", [
        ("_ChildDis_HearingA_Raw", _CHILD_DIFFICULTY_TRIGGERS),
        ("_ChildDis_HearingB_Raw", _CHILD_DIFFICULTY_TRIGGERS),
        ("_ChildDis_HearingC_Raw", {"ya"}),
    ]),
    ("Mobilitas", [
        ("_ChildDis_MobilityA_Raw", _CHILD_DIFFICULTY_TRIGGERS),
        ("_ChildDis_MobilityB_Raw", {"ya"}),
    ]),
    ("Komunikasi", [
        ("_ChildDis_CommA_Raw", _CHILD_DIFFICULTY_TRIGGERS),
        ("_ChildDis_CommB_Raw", _CHILD_DIFFICULTY_TRIGGERS),
    ]),
    ("Mengurus Diri", [
        ("_ChildDis_SelfcareA_Raw", _CHILD_DIFFICULTY_TRIGGERS),
        ("_ChildDis_SelfcareB_Raw", _CHILD_DIFFICULTY_TRIGGERS),
    ]),
]


def _raw_answer_matches(profile: dict, raw_field: str, trigger_values: set) -> bool:
    """Cek 1 field mentah di `profile`: True kalau nilainya (dinormalisasi
    strip+lower) ada di `trigger_values`."""
    raw_value = profile.get(raw_field, "")
    raw_answer = "" if pd.isna(raw_value) else str(raw_value).strip().lower()
    return raw_answer in trigger_values


def _compute_disability_category(age_value, profile: dict) -> str:
    """
    Menentukan 'Disability Category' dari usia + jawaban skrining:
      - Usia >= 18: pakai instrumen DEWASA (_DISABILITY_CATEGORY_RULES, 6
        pertanyaan, masing-masing 1 field).
      - Usia < 18: pakai instrumen ANAK (_CHILD_DISABILITY_CATEGORY_RULES,
        5 kategori, masing-masing bisa dipicu >1 field via OR).
      - Usia tidak valid/kosong: 'None' (tidak bisa dipastikan pakai
        instrumen yang mana).
    Kategori yang cocok digabung dengan ', ' kalau lebih dari satu. Kalau
    tidak ada satupun yang cocok -> 'None'.
    """
    try:
        age_num = int(float(str(age_value).strip()))
    except (ValueError, TypeError):
        return "None"

    matched_categories = []

    if age_num >= 18:
        for raw_field, trigger_values, label in _DISABILITY_CATEGORY_RULES:
            if _raw_answer_matches(profile, raw_field, trigger_values):
                matched_categories.append(label)
    else:
        for label, field_trigger_pairs in _CHILD_DISABILITY_CATEGORY_RULES:
            for raw_field, trigger_values in field_trigger_pairs:
                if _raw_answer_matches(profile, raw_field, trigger_values):
                    matched_categories.append(label)
                    break  # 1 field sudah cukup memicu kategori ini, cek kategori berikutnya

    return ", ".join(matched_categories) if matched_categories else "None"


# Penanda (substring, dicek case-insensitive) untuk tiap kategori bantuan
# proteksi sosial di dalam jawaban gabungan (_SP_Raw). Dicocokkan dengan
# `in` (containment), BUKAN comma-split, karena label opsi sendiri
# mengandung koma di dalam tanda kurung.
MVC_MARKERS = {
    "MVC- Dimensi 1": [
        "kesulitan ekonomi", "sulit makan", "bayar sekolah", "permukiman padat",
        "bantaran kali", "anak harus bekerja", "tidak punya pengasuh",
    ],
    "MVC- Dimensi 2": [
        "diperlakukan berbeda", "dikucilkan", "dijauhi", "suku/agama",
        "ktp/akta lahir", "akta lahir", "anak bermasalah",
    ],
    "MVC- Dimensi 3": [
        "mengalami kekerasan", "dimanfaatkan orang lain", "dipukul", "disakiti",
        "dilecehkan", "dipaksa bekerja", "dinikahkan dini", "narkoba", "zat adiktif",
    ],
    "MVC- Dimensi 4": [
        "terdampak bencana", "lokasi berisiko", "kena banjir", "kebakaran",
        "mengungsi", "konflik", "pengusiran", "sering kena bencana",
    ],
}

# BENTUK DATA ASLI (dikonfirmasi pengguna): field
# "group_digital_absensi/group_ys2ge24/group_cn7re50/Apakah_anak_keluarga_i_kerenta"
# adalah pertanyaan select_multiple KOBO — nilainya berupa TOKEN PENDEK
# dipisah SPASI (bukan frasa label), contoh: "Dimensi1 Dimensi3 Dimensi4"
# (artinya peserta ini tercentang di Dimensi 1, 3, dan 4; Dimensi 2 = No).
# Dicocokkan sebagai TOKEN UTUH (whole-word), bukan substring sembarangan,
# supaya "Dimensi1" tidak salah ikut mencocokkan "Dimensi10" dkk.
MVC_DIMENSION_TOKENS = {
    "MVC- Dimensi 1": "dimensi1",
    "MVC- Dimensi 2": "dimensi2",
    "MVC- Dimensi 3": "dimensi3",
    "MVC- Dimensi 4": "dimensi4",
}


def _mvc_flag(raw_text, dimension_field: str) -> str:
    """
    'Yes' jika dimensi ini tercentang di jawaban gabungan _MVC_Raw, selain
    itu 'No'. Mendukung DUA kemungkinan bentuk data sekaligus (robust
    terhadap variasi export Kobo):
      1. Token pendek dipisah spasi, format API asli — mis. "Dimensi1
         Dimensi3 Dimensi4" (dicocokkan sebagai token utuh/whole-word).
      2. Frasa label panjang (format export LABEL/CSV lama) — mis.
         "kesulitan ekonomi..." (dicocokkan sebagai substring, seperti
         sebelumnya, untuk kompatibilitas mundur).
    """
    if _is_empty(raw_text):
        return "No"
    text = str(raw_text).strip().lower()

    # 1) Cocokkan token utuh "dimensiN" (bentuk data asli terkonfirmasi).
    dimension_token = MVC_DIMENSION_TOKENS.get(dimension_field)
    if dimension_token:
        tokens = text.replace(",", " ").split()
        if dimension_token in tokens:
            return "Yes"

    # 2) Fallback: cocokkan frasa label panjang (kompatibilitas mundur).
    phrase_markers = MVC_MARKERS.get(dimension_field, [])
    if any(marker.lower() in text for marker in phrase_markers):
        return "Yes"

    return "No"


SP_MARKERS = {
    "SP - Cash transfers/food assistance": [
        "transfer tunai", "bantuan pangan", "pkh", "blt", "bpnt", "sembako desa",
    ],
    "SP - Health assistance": [
        "layanan kesehatan", "kis", "bpjs kesehatan", "kartu disabilitas",
    ],
    "SP - Education Assistance": [
        "bantuan pendidikan", "pip", "sekolah rakyat", "kjp",
    ],
}


def _sp_flag(raw_text, markers: list[str]) -> str:
    """
    'Yes' jika salah satu marker (substring, case-insensitive) ditemukan di
    dalam jawaban gabungan _SP_Raw, selain itu 'No'. Aman terhadap jawaban
    yang label opsinya digabung dengan koma (tidak pakai comma-split).
    """
    if _is_empty(raw_text):
        return "No"
    text = str(raw_text).strip().lower()
    return "Yes" if any(marker.lower() in text for marker in markers) else "No"


_SEX_LABEL_MAP = {
    "laki-laki": "Male",
    "laki laki": "Male",
    "laki_laki": "Male",  # nilai XML asli Kobo pakai underscore, bukan strip/spasi
    "male": "Male",
    "perempuan": "Female",
    "female": "Female",
}

# Standarisasi jawaban Ya/Tidak generik (dipakai untuk 'Disability Status' dari
# field 'Apakah Anda memiliki kebutuhan...' dan 'RC' dari field
# 'Apakah wakil anak Ya/Tidak') menjadi Yes/No untuk BTT.
_YA_TIDAK_LABEL_MAP = {
    "ya": "Yes",
    "yes": "Yes",
    "y": "Yes",
    "tidak": "No",
    "no": "No",
    "n": "No",
}


def _ya_tidak_label(raw_value) -> str:
    """'Ya' -> 'Yes', 'Tidak' -> 'No'. Nilai lain/tak dikenal dikembalikan apa adanya."""
    if _is_empty(raw_value):
        return ""
    key = str(raw_value).strip().lower()
    return _YA_TIDAK_LABEL_MAP.get(key, str(raw_value).strip())


# Alias lama dipertahankan supaya kode lain yang mungkin masih memanggilnya
# (mis. referensi eksternal) tetap berfungsi tanpa perubahan.
_disability_status_label = _ya_tidak_label


def _age_group_label(age) -> str:
    if pd.isna(age):
        return ""
    try:
        age_int = int(float(age))
    except (ValueError, TypeError):
        return ""
    if age_int < 0:
        return ""
    for low, high, label in _AGE_GROUP_BINS:
        if low <= age_int <= high:
            return label
    return "18+"


def _category_label(age) -> str:
    if pd.isna(age):
        return ""
    try:
        age_int = int(float(age))
    except (ValueError, TypeError):
        return ""
    if age_int < 0:
        return ""
    return "Child" if age_int < 18 else "Adult"


def _sex_label(raw_value) -> str:
    if pd.isna(raw_value):
        return ""
    key = str(raw_value).strip().lower()
    return _SEX_LABEL_MAP.get(key, str(raw_value).strip())


def _normalize_field_name(value) -> str:
    """Normalisasi nama kolom untuk resolver field Register."""
    import re
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def _is_empty(value) -> bool:
    if pd.isna(value):
        return True
    text = str(value).strip().lower()
    return text in {"", "nan", "none", "null", "na", "n/a", "-"}


def _first_non_empty(values):
    for value in values:
        if not _is_empty(value):
            return value
    return ""


def _column_has_data(df: pd.DataFrame, col: str) -> bool:
    """True jika kolom `col` punya minimal satu nilai yang TIDAK kosong."""
    return not df[col].map(_is_empty).all()


def _resolve_register_column(df: pd.DataFrame, aliases: list[str]) -> str | None:
    """
    Cari field Register dengan exact normalized match lalu token match.

    PENTING: kobo_api.py selalu membuat kolom placeholder KOSONG (NA) untuk
    setiap target di REGISTER_COLUMN_MAP yang headernya tidak ditemukan persis
    di file/response asli (mis. 'tipe_disabilitas' kalau header CSV memakai
    nama pendek 'Apakah_Anda_memiliki_kebutuhan' tanpa path group lengkap).
    Placeholder kosong ini TIDAK BOLEH "menutupi" kolom lain yang alias-nya
    lebih belakangan di daftar tapi justru punya data asli — karena itu,
    exact-match TIDAK langsung berhenti di alias pertama yang ketemu; ia akan
    lanjut mencari alias berikutnya jika kolom yang ketemu ternyata kosong
    semua, dan baru fallback ke kolom kosong itu di akhir jika memang tidak
    ada satu pun kandidat yang berisi data.
    """
    if df.empty:
        return None

    normalized = {_normalize_field_name(col): col for col in df.columns}
    alias_norms = [_normalize_field_name(alias) for alias in aliases]

    # 1) exact match — prioritaskan kandidat yang benar-benar punya data;
    #    simpan kandidat kosong sebagai fallback terakhir.
    empty_fallback = None
    for alias in alias_norms:
        if alias in normalized:
            col = normalized[alias]
            if _column_has_data(df, col):
                return col
            if empty_fallback is None:
                empty_fallback = col

    # 2) contains all token kelompok alias (berguna untuk path Kobo panjang)
    #    — sama, prioritaskan kandidat yang punya data.
    #    PENTING: token BENAR-BENAR pendek (1 huruf, mis. "i") DIBUANG dari
    #    syarat pencocokan — token sependek itu hampir pasti muncul sebagai
    #    substring di HAMPIR SEMUA nama kolom lain, sehingga menyebabkan
    #    false-positive match ke kolom yang salah sama sekali.
    #
    #    TAPI token 2 huruf TETAP DIPERTAHANKAN (mis. "id", "rc", "wa") —
    #    ini sengaja beda dari sebelumnya (yang sempat membuang token 2
    #    huruf juga): membuang "id" dari alias seperti "id_peserta" membuat
    #    syarat cocoknya cuma tersisa "peserta" sendirian, yang gampang salah
    #    nyantol ke kolom TIDAK TERKAIT sama sekali (mis. "kategori_peserta"
    #    — kolom kategori peserta, BUKAN kolom ID), sehingga custom_id jadi
    #    terisi nilai yang salah/berantakan. Karena SEMUA token (termasuk
    #    yang 2 huruf) tetap harus match BERSAMAAN, risiko false-positive
    #    tetap rendah selama kombinasi token-nya cukup spesifik.
    candidates = []
    for alias in alias_norms:
        tokens = [t for t in alias.split("_") if len(t) >= 2]
        if len(tokens) < 1:
            continue
        for norm_col, original in normalized.items():
            if all(token in norm_col for token in tokens):
                candidates.append((len(norm_col), original))
    if candidates:
        candidates.sort(key=lambda x: x[0])
        for _, col in candidates:
            if _column_has_data(df, col):
                return col
        if empty_fallback is None:
            empty_fallback = candidates[0][1]

    # 3) tidak ada kandidat berisi data -> kembalikan placeholder kosong
    #    (kalau ada) supaya skema tetap konsisten, atau None kalau memang
    #    tidak ada satu pun kandidat ditemukan.
    return empty_fallback


def _find_column_by_content_markers(df: pd.DataFrame, all_markers: list[str]) -> str | None:
    """
    Cari kolom Register berdasarkan ISI DATA-nya (bukan nama kolom), dengan
    menghitung berapa baris yang mengandung salah satu frasa penanda
    (all_markers, dicek case-insensitive, substring).

    PENTING: dipakai untuk field seperti "_MVC_Raw" / "_SP_Raw" ketika file
    Register diekspor Kobo dalam FORMAT LABEL — header kolomnya berupa
    breadcrumb pertanyaan panjang (mis. "DIGITAL ABSENSI / ANAK / INFORMASI
    LAINNYA / Apakah anak/keluarga Anda mengalami salah satu kondisi
    kerentanan berikut?"), BUKAN nama field XML pendek. Karena breadcrumb ini
    bisa berbeda-beda tergantung struktur form, pencarian berbasis nama kolom
    saja tidak cukup andal — mencari berdasarkan ISI jawaban (yang formatnya
    konsisten, sudah dikonfirmasi oleh pengguna) jauh lebih tahan terhadap
    variasi header ini.

    Mengembalikan kolom dengan jumlah baris cocok TERBANYAK (mengandung
    minimal 1 marker), atau None kalau tidak ada kolom yang cocok sama sekali.
    """
    if df.empty:
        return None

    markers_lower = [m.lower() for m in all_markers]
    best_col = None
    best_hits = 0

    for col in df.columns:
        series = df[col]
        if series.dtype != object and not pd.api.types.is_string_dtype(series):
            continue
        text_series = series.fillna("").astype(str).str.lower()
        hits = text_series.apply(lambda t: any(m in t for m in markers_lower)).sum()
        if hits > best_hits:
            best_hits = hits
            best_col = col

    return best_col if best_hits > 0 else None


# Alias field ID kustom mentah dari Kobo (mis. hasil dynamic data attachment
# `instance()` pada form Login yang mengambil ID dari Register). Nama field
# asli di Kobo bisa bervariasi tergantung setup form, jadi dicari via alias.
# Alias field ID kustom mentah dari Kobo. Nama field ASLI sudah dikonfirmasi
# langsung dari API live:
#   - Login    : "cek_ID"    (hasil lookup otomatis form "Login: Verifikasi Peserta")
#   - Register : "group_digital_absensi/group_informasi_respondent/custom_id"
# Keduanya SUDAH langsung dipetakan ke kolom 'custom_id' lewat
# LOGIN_COLUMN_MAP / REGISTER_COLUMN_MAP di config.py — jadi dalam kondisi
# normal kolom 'custom_id' semestinya SUDAH terisi benar sebelum resolver
# ini bahkan dijalankan. Daftar alias di bawah HANYA berfungsi sebagai
# jaring pengaman kalau suatu saat field-nya berganti nama di Kobo.
#
# PENTING (bug fix, jangan tambah alias generik lagi): field ID SENGAJA
# dicari dengan EXACT MATCH SAJA (lihat _resolve_custom_id_column_exact),
# TIDAK memakai fallback token/substring seperti field BTT lain — karena
# salah tebak kolom ID berakibat fatal (identitas peserta bisa tertukar
# masal). Ini pernah terjadi: alias generik seperti "id_peserta" pernah
# ke-fallback-match ke kolom "kategori_peserta" yang sama sekali bukan ID.
CUSTOM_ID_FIELD_ALIASES = [
    "custom_id", "cek_ID", "cek_id", "CekID", "id_kustom", "participant_id", "kode_id",
]


def _resolve_custom_id_column_exact(df: pd.DataFrame, aliases: list[str]) -> str | None:
    """
    Cari kolom ID di `df` dengan EXACT NORMALIZED MATCH SAJA — TIDAK ADA
    fallback token/substring seperti _resolve_register_column. Sengaja
    lebih ketat karena field ID terlalu sensitif untuk ditebak.

    Mengembalikan kolom pertama (sesuai urutan `aliases`) yang cocok exact
    DAN punya minimal satu nilai berisi. Jika tidak ada yang berisi,
    kembalikan kandidat exact-match pertama yang ditemukan (meski kosong)
    sebagai fallback netral, atau None jika tidak ada satu pun yang cocok.
    """
    if df.empty:
        return None

    normalized = {_normalize_field_name(col): col for col in df.columns}
    empty_fallback = None
    for alias in aliases:
        key = _normalize_field_name(alias)
        if key in normalized:
            col = normalized[key]
            if _column_has_data(df, col):
                return col
            if empty_fallback is None:
                empty_fallback = col
    return empty_fallback


def resolve_custom_id_column(df: pd.DataFrame) -> pd.DataFrame:
    """
    Memastikan kolom 'custom_id' terisi dari field ID mentah Kobo (apa pun nama
    aslinya, dicari via CUSTOM_ID_FIELD_ALIASES) SEBELUM proses matching/BTT.

    Dipanggil sekali segera setelah fetch/upload data (lihat app.py), baik untuk
    df_login maupun df_register. Kolom mentah aslinya TETAP dipertahankan untuk
    audit — hanya disalin nilainya ke kolom standar 'custom_id'.

    Jika kolom 'custom_id' sudah ada dan sudah terisi, tidak ada perubahan.
    Jika kosong tapi ditemukan kolom alias mentah, nilainya diisi dari sana.

    CATATAN: pencarian kolom di sini EXACT MATCH SAJA (lihat
    _resolve_custom_id_column_exact) — TIDAK memakai fallback token-substring
    seperti field BTT lain, supaya custom_id tidak pernah tertukar dengan
    kolom lain yang kebetulan mengandung kata serupa.
    """
    if df.empty:
        return df

    df = df.copy()
    resolved_col = _resolve_custom_id_column_exact(df, CUSTOM_ID_FIELD_ALIASES)

    if "custom_id" not in df.columns:
        df["custom_id"] = ""

    if resolved_col and resolved_col != "custom_id":
        raw_values = df[resolved_col].fillna("").astype(str).str.strip()
        current = df["custom_id"].fillna("").astype(str).str.strip()
        # Isi custom_id dari kolom mentah HANYA pada baris yang custom_id-nya
        # masih kosong, supaya tidak menimpa custom_id yang sudah benar.
        df["custom_id"] = current.where(current != "", raw_values)

    return df


def _build_register_profile_lookup(df_register: pd.DataFrame):
    """Lookup profil Register by custom_id then full name, memakai semua row Register."""
    if df_register is None or df_register.empty:
        return {}, {}

    reg = df_register.copy()
    id_col = _resolve_register_column(reg, ["custom_id", "cek_ID", "id", "ID"])
    name_col = _resolve_register_column(reg, ["nama", "nama_lengkap_parent", "nama_child", "full_name", "Full Name"])
    date_col = _resolve_register_column(reg, ["tanggal_kegiatan", "tanggal_register", "registration_date", "_submission_time", "timestamp_submit"])

    field_source = {
        field: _resolve_register_column(reg, aliases)
        for field, aliases in BTT_REGISTER_FIELD_ALIASES.items()
    }

    # "_MVC_Raw" & "_SP_Raw": PRIORITASKAN deteksi berbasis ISI DATA (bukan
    # nama kolom). Alasan: file Register sering diekspor Kobo dalam FORMAT
    # LABEL — header kolom berupa breadcrumb pertanyaan panjang yang berbeda-
    # beda (mis. "DIGITAL ABSENSI / ANAK / INFORMASI LAINNYA / Apakah
    # anak/keluarga Anda mengalami salah satu kondisi kerentanan berikut?"),
    # sehingga pencarian berbasis nama kolom (alias) berisiko salah menangkap
    # kolom lain yang sekadar mengandung potongan kata serupa (mis. "anak",
    # "keluarga"). Isi jawaban jauh lebih spesifik/unik dan sudah dikonfirmasi
    # formatnya oleh pengguna, sehingga jadi sumber kebenaran utama di sini.
    # Nama kolom (field_source dari alias di atas) hanya dipakai sebagai
    # fallback TERAKHIR kalau deteksi berbasis isi data sama sekali tidak
    # menemukan kolom yang cocok.
    all_mvc_markers = [m for markers in MVC_MARKERS.values() for m in markers] + list(MVC_DIMENSION_TOKENS.values())
    mvc_content_col = _find_column_by_content_markers(reg, all_mvc_markers)
    field_source["_MVC_Raw"] = mvc_content_col or field_source.get("_MVC_Raw")

    all_sp_markers = [m for markers in SP_MARKERS.values() for m in markers]
    sp_content_col = _find_column_by_content_markers(reg, all_sp_markers)
    field_source["_SP_Raw"] = sp_content_col or field_source.get("_SP_Raw")

    reg["_profile_id_key"] = reg[id_col].map(_norm_identity) if id_col else ""
    reg["_profile_name_key"] = reg[name_col].map(_norm_identity) if name_col else ""
    if date_col:
        reg["_profile_date"] = reg[date_col].map(_parse_event_date)
    else:
        reg["_profile_date"] = pd.NaT

    def aggregate(group: pd.DataFrame) -> dict:
        record = {}
        for field, source_col in field_source.items():
            record[field] = _first_non_empty(group[source_col].tolist()) if source_col else ""
        valid_dates = group["_profile_date"].dropna()
        record["_first_registration_date"] = valid_dates.min() if not valid_dates.empty else pd.NaT
        return record

    by_id = {}
    id_mask = reg["_profile_id_key"] != ""
    for key, group in reg.loc[id_mask].groupby("_profile_id_key", sort=False):
        by_id[key] = aggregate(group)

    by_name = {}
    name_mask = reg["_profile_name_key"] != ""
    for key, group in reg.loc[name_mask].groupby("_profile_name_key", sort=False):
        by_name[key] = aggregate(group)

    return by_id, by_name


def _profile_for_row(row: pd.Series, by_id: dict, by_name: dict) -> dict:
    cid = _norm_identity(row.get("custom_id", ""))
    name = _norm_identity(row.get("nama", ""))
    if cid and cid in by_id:
        return by_id[cid]
    if name and name in by_name:
        return by_name[name]
    return {}


def _yes(value) -> bool:
    if pd.isna(value):
        return False
    return str(value).strip().lower() in {"yes", "y", "true", "1"}


def add_participant_profile_columns(df_login: pd.DataFrame, df_register: pd.DataFrame) -> pd.DataFrame:
    """Enrich BTT dari Register menggunakan ID, fallback Full Name."""
    df = df_login.copy()
    df["ID"] = df.get("custom_id", pd.Series(index=df.index, dtype=object)).fillna("")
    df["Full Name"] = df.get("nama", pd.Series(index=df.index, dtype=object)).fillna("")

    by_id, by_name = _build_register_profile_lookup(df_register)
    profiles = [_profile_for_row(row, by_id, by_name) for _, row in df.iterrows()]

    # Field source from Register
    register_output_fields = [
        "Household Name", "Sex", "Age",
        "Disability Status",
        "RC", "RC Status", "IDN",
        "Institution", "Position", "No.Handphone (WA)",
        "# Child <5", "# Child 6-11", "# Child 12-17",
        "Village", "Sub-Village 1", "Sub-Village 2",
    ]
    # NOTE: "Disability Category" SENGAJA TIDAK ada di daftar di atas — nilainya
    # dihitung terpisah di bawah (_compute_disability_category), dari 6
    # pertanyaan skrining mentah + Age, bukan 1 kolom langsung.
    # NOTE: 3 kolom "SP - ..." dan 4 kolom "MVC- Dimensi ..." SENGAJA TIDAK ada
    # di daftar di atas — nilainya dihitung terpisah di bawah dari jawaban
    # gabungan "_SP_Raw" / "_MVC_Raw" via _sp_flag()/_mvc_flag(), karena field
    # mentahnya SATU pertanyaan multi-select (label pilihan digabung koma),
    # bukan kolom yes/no terpisah per kategori.

    for field in register_output_fields:
        df[field] = [_first_non_empty([profile.get(field, "")]) for profile in profiles]

    # "Disability Category" dihitung dari Age (kolom yang baru saja diisi di
    # atas) + 6 jawaban skrining mentah (profile["_Disability_*_Raw"]).
    df["Disability Category"] = [
        _compute_disability_category(age_val, profile)
        for age_val, profile in zip(df["Age"], profiles)
    ]

    # Deteksi 3 kategori bantuan proteksi sosial dari jawaban gabungan
    # "_SP_Raw" via substring match (SP_MARKERS) — bukan comma-split, karena
    # label opsi itu sendiri mengandung koma di dalam kurung.
    sp_raw_values = [profile.get("_SP_Raw", "") for profile in profiles]
    for field, markers in SP_MARKERS.items():
        df[field] = [_sp_flag(raw, markers) for raw in sp_raw_values]

    # Deteksi 4 dimensi kerentanan (MVC) dari jawaban gabungan "_MVC_Raw" via
    # substring match (MVC_MARKERS) — sama alasannya dengan SP di atas.
    mvc_raw_values = [profile.get("_MVC_Raw", "") for profile in profiles]
    for field in MVC_MARKERS:
        df[field] = [_mvc_flag(raw, field) for raw in mvc_raw_values]

    # "Village" utama = Kelurahan (pilihan dropdown). Kalau ada isian bebas
    # "Tuliskan Kelurahannya" (biasanya cuma terisi kalau dropdown Kelurahan
    # menunjuk opsi "Lainnya"), PRIORITASKAN isian bebas itu di atas nilai
    # dropdown — asumsi: field isian bebas hanya terisi kalau memang dropdown
    # tidak mencakup pilihan yang sesuai.
    village_other_values = pd.Series(
        [profile.get("_Village_Other", "") for profile in profiles], index=df.index
    ).fillna("").astype(str).str.strip()
    village_primary_values = df["Village"].fillna("").astype(str).str.strip()
    df["Village"] = village_other_values.where(village_other_values != "", village_primary_values)

    # Sub-District & Zonal DITURUNKAN dari Village (lookup tabel, lihat
    # VILLAGE_TO_SUBDISTRICT / SUBDISTRICT_TO_ZONAL) — bukan field mentah
    # tersendiri dari Kobo. Village yang tidak dikenali -> dikosongkan
    # (bukan ditebak).
    df["Sub-District"] = df["Village"].apply(lambda v: _lookup_place(VILLAGE_TO_SUBDISTRICT, v))
    df["Zonal"] = df["Sub-District"].apply(lambda v: _lookup_place(SUBDISTRICT_TO_ZONAL, v))

    # Standarisasi Sex: Laki-laki -> Male, Perempuan -> Female.
    df["Sex"] = df["Sex"].apply(_sex_label)

    # Standarisasi Disability Status: Ya -> Yes, Tidak -> No.
    df["Disability Status"] = df["Disability Status"].apply(_ya_tidak_label)

    # Standarisasi RC: Ya -> Yes, Tidak -> No (field 'Apakah wakil anak Ya/Tidak').
    df["RC"] = df["RC"].apply(_ya_tidak_label)

    # Derived fields
    df["Age group"] = df["Age"].apply(_age_group_label)
    df["Category"] = df["Age"].apply(_category_label)

    # MVC = Yes jika total dimensi Yes (MVC- Dimensi 1..4) >= 2.
    # ATURAN TAMBAHAN: peserta PEREMPUAN (Sex == Female) dapat +1 mark bonus,
    # TAPI HANYA jika minimal 1 dimensi sudah Yes (bonus tidak berlaku kalau
    # tidak ada satu pun dimensi yang tercentang). Jadi perempuan dengan
    # 1 dimensi Yes -> 1 (asli) + 1 (bonus perempuan) = 2 -> MVC jadi Yes.
    mvc_fields = [
        "MVC- Dimensi 1", "MVC- Dimensi 2", "MVC- Dimensi 3", "MVC- Dimensi 4"
    ]

    def _mvc_final(row) -> str:
        dimension_count = sum(_yes(row.get(field, "")) for field in mvc_fields)
        if dimension_count >= 1 and str(row.get("Sex", "")).strip().lower() == "female":
            dimension_count += 1
        return "Yes" if dimension_count >= 2 else "No"

    df["MVC"] = df.apply(_mvc_final, axis=1)

    sp_fields = [
        "SP - Cash transfers/food assistance",
        "SP - Health assistance",
        "SP - Education Assistance",
    ]
    df["Social Protection"] = df.apply(
        lambda row: "Yes" if any(_yes(row.get(field, "")) for field in sp_fields) else "No",
        axis=1,
    )

    return df


def add_btt_register_columns(df_login: pd.DataFrame, df_register: pd.DataFrame) -> pd.DataFrame:
    """Alias publik untuk enrichment Register ke BTT."""
    return add_participant_profile_columns(df_login, df_register)


def add_location_context_columns(df: pd.DataFrame, ap_label: str) -> pd.DataFrame:
    """
    Menambahkan kolom 'AP', 'Province', 'District' berdasarkan Area Program
    yang DIPILIH PANITIA di dropdown langkah Muat Data — BUKAN dari field
    form Kobo mana pun (baik Login maupun Register).

    'Province'/'District' hanya terisi untuk AP yang aturannya sudah pasti
    (lihat AP_TO_PROVINCE_DISTRICT). AP lain (termasuk "(Manual)"/kosong)
    akan menghasilkan ketiga kolom ini kosong — SENGAJA tidak ditebak.

    Kolom 'Zonal' JUGA di-override di sini kalau AP terpilih ada di
    AP_TO_ZONAL_OVERRIDE — nilai override ini MENIMPA hasil turunan
    Sub-District (dari add_participant_profile_columns) untuk SEMUA baris
    AP tersebut. AP yang tidak terdaftar di override tetap pakai Zonal hasil
    lookup Sub-District seperti biasa (tidak diubah oleh fungsi ini).

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame BTT yang sedang dibangun (baris = peserta/kehadiran Login).
        HARUS sudah punya kolom 'Zonal' (dari add_participant_profile_columns)
        sebelum fungsi ini dipanggil, supaya override bisa menimpanya.
    ap_label : str
        Nama AP yang dipilih panitia di dropdown (mis. "AP-Simokerto"),
        atau "(Manual)"/"" kalau tidak ada AP spesifik yang dipilih.

    Returns
    -------
    pd.DataFrame
        Salinan `df` + kolom 'AP', 'Province', 'District' (dan 'Zonal' yang
        sudah di-override kalau berlaku).
    """
    df = df.copy()
    ap_clean = (ap_label or "").strip()

    df["AP"] = ap_clean if ap_clean and ap_clean != "(Manual)" else ""
    province, district = AP_TO_PROVINCE_DISTRICT.get(ap_clean, ("", ""))
    df["Province"] = province
    df["District"] = district

    if ap_clean in AP_TO_ZONAL_OVERRIDE:
        df["Zonal"] = AP_TO_ZONAL_OVERRIDE[ap_clean]

    return df


def resolve_register_duplicate(
    df_login: pd.DataFrame,
    df_register: pd.DataFrame,
    pair_row: pd.Series,
    decision: str,
    threshold: float = config.DUPLICATE_THRESHOLD,
) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """
    Implementasi lengkap flowchart penanganan duplikat Register:

        Duplikat?
          -> Ya: cek acaranya sama? (informasional saja, keputusan sama)
               -> cek apakah di absensi (Login) sudah ada untuk ACARA TERBARU
                  dari pasangan duplikat ini?
                     -> Ya: tidak perlu menambahkan apa-apa.
                     -> Tidak: append data acara terbaru itu ke Login,
                               lalu hapus salah satu data duplikat di Register.
          -> Tidak (decision == "keep_both"): aman, tidak ada tindakan otomatis
             (dianggap dua orang berbeda).

    Parameters
    ----------
    df_login, df_register : pd.DataFrame
        Dataset saat ini (SEBELUM perubahan apa pun untuk pair ini).
    pair_row : pd.Series
        Satu baris dari hasil find_duplicate_pairs_register (harus punya
        id_a, id_b, judul_kegiatan_a/b, tanggal_kegiatan_a/b, timestamp_submit_a/b).
    decision : str
        "keep_a", "keep_b", atau "keep_both".
    threshold : float
        Ambang batas similarity untuk cek keberadaan di Login.

    Returns
    -------
    (df_login_baru, df_register_baru, info_message)
        info_message : penjelasan tindakan otomatis yang terjadi, untuk
        ditampilkan ke panitia di UI (transparansi keputusan sistem).
    """
    row_uid_a = pair_row.get("row_uid_a")
    row_uid_b = pair_row.get("row_uid_b")
    id_a, id_b = pair_row.get("id_a"), pair_row.get("id_b")

    if decision == "keep_both":
        # "aman" — dianggap dua orang berbeda, tidak ada tindakan otomatis.
        return df_login, df_register, (
            "Kedua data dianggap peserta yang BERBEDA — tidak ada perubahan otomatis. "
            "Masing-masing tetap akan dicek kelengkapan Login-nya secara normal via Auto-Append."
        )

    if decision not in ("keep_a", "keep_b"):
        raise ValueError(f"Decision tidak dikenal: {decision}")

    # ---- STEP 1: Hapus salah satu duplikat di Register sesuai keputusan panitia ----
    if "_row_uid" in df_register.columns and row_uid_a is not None and row_uid_b is not None:
        drop_uid = row_uid_b if decision == "keep_a" else row_uid_a
        df_register_new = df_register[df_register["_row_uid"] != drop_uid].reset_index(drop=True)
    else:
        drop_id = id_b if decision == "keep_a" else id_a
        df_register_new = df_register[df_register["id_kobo"] != drop_id].reset_index(drop=True)

    # ---- STEP 2: Tentukan "acara terbaru" di antara A dan B ----
    # Prioritas pembanding: tanggal_kegiatan (tanggal acara sebenarnya).
    # Fallback: timestamp_submit (waktu submit form) jika tanggal_kegiatan kosong/tidak valid.
    date_a = _parse_event_date(pair_row.get("tanggal_kegiatan_a"))
    date_b = _parse_event_date(pair_row.get("tanggal_kegiatan_b"))

    if pd.notna(date_a) and pd.notna(date_b):
        latest_id = id_b if date_b >= date_a else id_a
    elif pd.notna(date_a):
        latest_id = id_a
    elif pd.notna(date_b):
        latest_id = id_b
    else:
        ts_a, ts_b = pair_row.get("timestamp_submit_a"), pair_row.get("timestamp_submit_b")
        latest_id = id_b if (pd.notna(ts_b) and (pd.isna(ts_a) or ts_b >= ts_a)) else id_a

    # Ambil data lengkap baris "acara terbaru" dari df_register ASLI (sebelum dihapus),
    # supaya datanya tetap tersedia untuk di-append meskipun baris itu yang akan dihapus.
    latest_row_uid = row_uid_b if latest_id == id_b else row_uid_a
    if "_row_uid" in df_register.columns and latest_row_uid is not None:
        latest_row_df = df_register[df_register["_row_uid"] == latest_row_uid]
    else:
        latest_row_df = df_register[df_register["id_kobo"] == latest_id]

    if latest_row_df.empty:
        return df_login, df_register_new, "⚠️ Data acara terbaru tidak ditemukan, tidak ada tindakan otomatis."
    latest_row = latest_row_df.iloc[0].to_dict()
    event_label = latest_row.get("judul_kegiatan", "-")

    # ---- STEP 3: Cek apakah sudah ada di Login untuk acara terbaru tsb ----
    already_logged = check_person_logged_in_for_event(latest_row, df_login, threshold)

    if already_logged:
        info = (
            f"✅ Data untuk kegiatan **'{event_label}'** SUDAH ada di Login — "
            "tidak perlu menambahkan apa-apa (sesuai flowchart)."
        )
        return df_login, df_register_new, info

    # ---- STEP 4: Belum ada di Login -> append otomatis ----
    append_key = latest_row_uid if "_row_uid" in df_register.columns and latest_row_uid is not None else latest_id
    df_login_new = append_register_to_login(df_login, df_register, [append_key])
    info = (
        f"➕ Data untuk kegiatan **'{event_label}'** BELUM ada di Login — "
        "otomatis di-append ke dataset Login."
    )
    return df_login_new, df_register_new, info


def apply_review_decision(
    df: pd.DataFrame,
    pair_row: pd.Series,
    decision: str,
) -> pd.DataFrame:
    """
    Menerapkan keputusan panitia terhadap satu pasangan duplikat.

    Bersifat generik: dipakai untuk dataset LOGIN (dedup sederhana, tanpa
    logika auto-append lanjutan — karena Login memang sudah menandakan
    kehadiran, tidak perlu di-append ke mana pun).

    Untuk dataset REGISTER, gunakan `resolve_register_duplicate()` sebagai
    gantinya, karena Register butuh logika tambahan (cek & append ke Login
    untuk acara terbaru) sesuai flowchart bisnis.

    Parameters
    ----------
    df : pd.DataFrame
        Dataset asal pasangan (biasanya df_login).
    decision : str
        Salah satu dari: "keep_a", "keep_b", "keep_both"

    Returns
    -------
    df yang sudah diperbarui.
    """
    row_uid_a = pair_row.get("row_uid_a")
    row_uid_b = pair_row.get("row_uid_b")
    id_a = pair_row.get("id_a")
    id_b = pair_row.get("id_b")

    if decision == "keep_a":
        if "_row_uid" in df.columns and row_uid_b is not None:
            df = df[df["_row_uid"] != row_uid_b]
        else:
            df = df[df["id_kobo"] != id_b]

    elif decision == "keep_b":
        if "_row_uid" in df.columns and row_uid_a is not None:
            df = df[df["_row_uid"] != row_uid_a]
        else:
            df = df[df["id_kobo"] != id_a]

    elif decision == "keep_both":
        pass  # tidak ada perubahan, keduanya dipertahankan (misal beda kegiatan/valid)

    else:
        raise ValueError(f"Decision tidak dikenal: {decision}")

    return df.reset_index(drop=True)


def append_register_to_login(
    df_login: pd.DataFrame,
    df_register: pd.DataFrame,
    row_uids_to_append: list,
) -> pd.DataFrame:
    """
    Append submission Register tertentu ke Login.

    `_row_uid` diprioritaskan karena `id_kobo` dapat sama pada kegiatan berbeda.
    """
    if not row_uids_to_append:
        return df_login

    if "_row_uid" in df_register.columns:
        rows_to_append = df_register[df_register["_row_uid"].isin(row_uids_to_append)].copy()
    else:
        rows_to_append = df_register[df_register["id_kobo"].isin(row_uids_to_append)].copy()

    if rows_to_append.empty:
        return df_login

    # Selaraskan skema kolom dengan df_login; kolom hilang diisi NA
    login_cols = df_login.columns.tolist()
    for col in login_cols:
        if col not in rows_to_append.columns:
            rows_to_append[col] = pd.NA
    rows_to_append = rows_to_append[login_cols]

    df_login_new = pd.concat([df_login, rows_to_append], ignore_index=True)
    return df_login_new


def to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Konversi DataFrame ke bytes CSV (siap dipakai st.download_button)."""
    buffer = io.StringIO()
    df.to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8-sig")  # utf-8-sig agar aman dibuka Excel


def to_excel_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    """
    Konversi beberapa DataFrame ke satu file Excel multi-sheet.

    Parameters
    ----------
    sheets : dict
        {"NamaSheet": dataframe, ...}
    """
    buffer = io.BytesIO()
    try:
        with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
            for sheet_name, df in sheets.items():
                safe_name = sheet_name[:31]  # batas nama sheet Excel
                df.to_excel(writer, sheet_name=safe_name, index=False)
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "Library 'xlsxwriter' belum terpasang. Jalankan: pip install xlsxwriter"
        ) from e
    return buffer.getvalue()

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

# Mapping kolom BTT resmi -> nama kolom tabel MariaDB
BTT_TO_SQL_MAP = {
    "Implementor": "implementor",
    "Sector": "sector",
    "CPM": "cpm",
    "Project": "project",
    "Project Category": "project_category",
    "Output Code": "output_code",
    "Activity Code": "activity_code",
    "Activity": "activity",
    "Activity Detail": "activity_detail",
    "Date": "activity_date",
    "Month": "month",
    "Month (First)": "month_first",
    "Fiscal Year": "fiscal_year",
    "ID": "id",
    "Full Name": "full_name",
    "Household Name": "household_name",
    "Sex": "sex",
    "Age": "age",
    "Age group": "age_group",
    "Category": "category",
    "Disability Category": "disability_category",
    "Disability Status": "disability_status",
    "RC": "rc",
    "RC Status": "rc_status",
    "IDN": "idn",
    "MVC- Dimensi 1": "mvc_dimensi_1",
    "MVC- Dimensi 2": "mvc_dimensi_2",
    "MVC- Dimensi 3": "mvc_dimensi_3",
    "MVC- Dimensi 4": "mvc_dimensi_4",
    "MVC": "mvc",
    "Social Protection": "social_protection",
    "SP - Cash transfers/food assistance": "sp_cash_transfers_food_assistance",
    "SP - Health assistance": "sp_health_assistance",
    "SP - Education Assistance": "sp_education_assistance",
    "Institution": "institution",
    "Position": "position",
    "No.Handphone (WA)": "no_handphone",
    "# Child <5": "child_under_5",
    "# Child 6-11": "child_6_11",
    "# Child 12-17": "child_12_17",
    # --- Kolom lokasi (ditambahkan belakangan — lihat add_location_context_columns
    # & add_participant_profile_columns di data_processor.py) ---
    "AP": "ap",
    "Province": "province",
    "District": "district",
    "Zonal": "zonal",
    "Sub-District": "sub_district",
    "Village": "village",
    "Sub-Village 1": "sub_village_1",
    "Sub-Village 2": "sub_village_2",
}


def test_database_connection(host: str, port: int, user: str, password: str, db_name: str) -> tuple[bool, str]:
    """
    Coba terhubung ke database TANPA mengubah/menyimpan data apa pun — dipakai
    tombol "🔌 Tes Koneksi Database" supaya panitia bisa cek dulu apakah
    pengaturan (host/port/user/password/nama database) sudah benar, sebelum
    mencoba export data sungguhan.

    Mengembalikan tuple (berhasil: bool, pesan: str).
    """
    try:
        conn_url = URL.create(
            "mysql+pymysql", username=user, password=password, host=host, port=port, database=db_name,
        )
        engine = create_engine(conn_url, connect_args={"connect_timeout": 8})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, f"Berhasil terhubung ke database '{db_name}' di {host}:{port}."
    except Exception as e:
        return False, f"Gagal terhubung: {e}"


def export_btt_to_mariadb(df_btt: pd.DataFrame, host: str, port: int, user: str, password: str, db_name: str, table_name: str) -> tuple[bool, str]:
    """
    Mengekspor DataFrame BTT ke tabel MariaDB/MySQL lokal.
    Mengembalikan tuple (status_sukses, pesan_keterangan).
    """
    if df_btt.empty:
        return False, "Data BTT masih kosong, tidak ada yang dapat diekspor."

    try:
        # 1. Salin dan ubah nama kolom sesuai skema database SQL
        df_sql = df_btt.copy()
        df_sql = df_sql.rename(columns=BTT_TO_SQL_MAP)

        # Ambil hanya kolom yang terdaftar di database
        valid_cols = [col for col in BTT_TO_SQL_MAP.values() if col in df_sql.columns]
        df_sql = df_sql[valid_cols]

        # 2. Penyesuaian tipe data angka & tanggal.
        # PENTING: pakai dtype nullable "Int64" (bukan .fillna(0).astype(int))
        # supaya nilai kosong/tidak terisi (mis. Age belum diisi) tersimpan
        # sebagai NULL di database — BUKAN angka 0, karena 0 bisa disalahartikan
        # sebagai "usia 0 tahun" alih-alih "data tidak ada".
        for int_col in ["age", "idn", "child_under_5", "child_6_11", "child_12_17"]:
            if int_col in df_sql.columns:
                df_sql[int_col] = pd.to_numeric(df_sql[int_col], errors="coerce").astype("Int64")

        if "activity_date" in df_sql.columns:
            df_sql["activity_date"] = pd.to_datetime(df_sql["activity_date"], errors="coerce").dt.date

        # Ganti nilai kosong/NA menjadi None agar masuk sebagai NULL di database
        # (mencakup juga pd.NA dari kolom Int64 nullable di atas).
        df_sql = df_sql.replace({pd.NA: None, float("nan"): None, "": None})

        # 3. Buat engine koneksi.
        # PENTING: pakai URL.create() (bukan f-string manual) supaya user/password
        # yang mengandung karakter spesial (@, :, /, %, dst.) di-escape otomatis —
        # f-string manual akan RUSAK/salah parsing host-port kalau password
        # mengandung karakter seperti itu (sudah terbukti lewat pengujian).
        conn_url = URL.create(
            "mysql+pymysql",
            username=user,
            password=password,
            host=host,
            port=port,
            database=db_name,
        )
        engine = create_engine(conn_url)

        # 4. Cegah duplikat: skip baris yang kombinasi (id, activity_date)-nya
        # SUDAH ADA di tabel tujuan. Ini penting karena tombol export bisa
        # diklik berkali-kali (mis. tidak sengaja double click, atau re-export
        # data yang sama) — tanpa pengecekan ini, baris yang sama akan
        # dobel terus setiap kali tombol diklik.
        skipped_count = 0
        if "id" in df_sql.columns and "activity_date" in df_sql.columns:
            try:
                existing = pd.read_sql(
                    f"SELECT `id`, `activity_date` FROM `{table_name}`", con=engine
                )
                existing_pairs = set(
                    zip(existing["id"].astype(str), existing["activity_date"].astype(str))
                )
            except Exception:
                # Tabel belum ada sama sekali (export pertama kali) -> anggap
                # belum ada data lama, semua baris baru boleh masuk.
                existing_pairs = set()

            if existing_pairs:
                row_keys = list(zip(df_sql["id"].astype(str), df_sql["activity_date"].astype(str)))
                is_new_row = [key not in existing_pairs for key in row_keys]
                skipped_count = len(df_sql) - sum(is_new_row)
                df_sql = df_sql[is_new_row]

        if df_sql.empty:
            return True, (
                f"Tidak ada baris baru yang ditambahkan — seluruh {skipped_count} baris "
                f"sudah pernah di-export sebelumnya (kombinasi ID + Tanggal Kegiatan sama)."
            )

        # 5. Simpan ke database
        df_sql.to_sql(
            name=table_name,
            con=engine,
            if_exists="append",
            index=False,
            chunksize=500
        )

        info = f"Berhasil menyimpan {len(df_sql)} baris baru ke tabel `{table_name}` di database `{db_name}`."
        if skipped_count:
            info += f" ({skipped_count} baris dilewati karena sudah pernah di-export sebelumnya.)"
        return True, info

    except Exception as e:
        return False, f"Gagal mengekspor ke MariaDB: {str(e)}"
