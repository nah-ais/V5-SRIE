"""
config.py
---------
Konfigurasi terpusat: mapping kolom KoboToolbox, bobot scoring,
threshold, dan konstanta lain yang dipakai di seluruh aplikasi.

Memusatkan konfigurasi di sini memudahkan maintenance jika suatu saat
struktur form Kobo berubah (tambah field, ganti nama field, dsb).
"""

# =========================================================
# 1. MAPPING KOLOM KOBOTOOLBOX -> NAMA KOLOM INTERNAL
# =========================================================

LOGIN_COLUMN_MAP = {
    "_id": "id_kobo",
    "_submission_time": "timestamp_submit",
    "nama_child": "nama",
    "tgl_lahir_child": "tanggal_lahir",
    "Kelurahan": "kelurahan",
    "Tuliskan_Kelurahannya": "kelurahan_lainnya",
    "group_ys5yz58/Area_Program": "area_program",
    "group_ys5yz58/Judul_Kegiatan": "judul_kegiatan",
    "group_ys5yz58/Tanggal_Kegiatan": "tanggal_kegiatan",
    # ID hasil lookup otomatis form "Login: Verifikasi Peserta" — dikonfirmasi
    # langsung dari API live (asset_uid aE3xS8zXQsU9KQsiT9T7PA).
    "cek_ID": "custom_id",
}

REGISTER_COLUMN_MAP = {
    "_id": "id_kobo",
    "_submission_time": "timestamp_submit",
    "group_digital_absensi/group_informasi_respondent/nama_lengkap_parent": "nama",
    "group_digital_absensi/group_informasi_respondent/tgl_lahir_parent": "tanggal_lahir",
    "group_digital_absensi/group_informasi_respondent/Jenis_Kelamin": "jenis_kelamin",
    "group_digital_absensi/group_informasi_respondent/usia": "usia",
    "group_digital_absensi/group_informasi_respondent/Nama_Kepala_Keluarga": "nama_kepala_keluarga",
    "group_digital_absensi/group_informasi_respondent/Kelurahan": "kelurahan",
    "group_digital_absensi/group_informasi_respondent/Tuliskan_Kelurahannya": "kelurahan_lainnya",
    "group_digital_absensi/group_informasi_respondent/RT": "rt",
    "group_digital_absensi/group_informasi_respondent/RW": "rw",
    "group_digital_absensi/group_informasi_respondent/Nomor_WA_HP": "nomor_hp",
    "group_digital_absensi/group_wx4wq68/Area_Program": "area_program",
    "group_digital_absensi/group_wx4wq68/Judul_Kegiatan": "judul_kegiatan",
    "group_digital_absensi/group_wx4wq68/Tanggal_Kegiatan": "tanggal_kegiatan",
    "group_digital_absensi/group_informasi_respondent/Apakah_Anda_memiliki_kebutuhan": "tipe_disabilitas",
    "group_digital_absensi/group_informasi_respondent/Kategori_Peserta": "kategori_peserta",
    # ID unik peserta — dikonfirmasi langsung dari API live
    # (asset_uid aRVadKAkpz2PYMaZH2gXKU). Contoh nilai: 'SBY-260820-40d'.
    "group_digital_absensi/group_informasi_respondent/custom_id": "custom_id",
}

# =========================================================
# 1b. FIELD REGISTER TAMBAHAN UNTUK BTT
# =========================================================
# Alias digunakan saat response Kobo menyimpan nama field yang panjang
# (misalnya group_xxx/.../nama_field). Resolver di kobo_api.py akan
# mencari exact normalized name lalu token yang terkandung pada nama field.
BTT_REGISTER_COLUMN_ALIASES = {
    "disability_category": ["disability_category", "Disability Category", "kategori_disabilitas", "kategori disability"],
    "disability_status": ["disability_status", "Disability Status", "status_disabilitas", "status disability"],
    "rc": ["rc", "RC", "relational_capital"],
    "rc_status": ["rc_status", "RC Status", "status_rc", "idn rc status"],
    "idn": ["idn", "IDN", "idn_status"],
    "mvc_dimensi_1": ["mvc_dimensi_1", "MVC- Dimensi 1", "MVC Dimensi 1", "mvc1", "mvc_1"],
    "mvc_dimensi_2": ["mvc_dimensi_2", "MVC- Dimensi 2", "MVC Dimensi 2", "mvc2", "mvc_2"],
    "mvc_dimensi_3": ["mvc_dimensi_3", "MVC- Dimensi 3", "MVC Dimensi 3", "mvc3", "mvc_3"],
    "mvc_dimensi_4": ["mvc_dimensi_4", "MVC- Dimensi 4", "MVC Dimensi 4", "mvc4", "mvc_4"],
    "sp_cash_transfers": ["sp_cash_transfers", "SP - Cash transfers/food assistance", "cash_transfers", "food_assistance", "sp_cash"],
    "sp_health_assistance": ["sp_health_assistance", "SP - Health assistance", "health_assistance", "sp_health"],
    "sp_education_assistance": ["sp_education_assistance", "SP - Education Assistance", "education_assistance", "sp_education"],
    "institution": ["institution", "Institution", "instansi"],
    "position": ["position", "Position", "jabatan"],
    "nomor_hp": ["nomor_hp", "No.Handphone (WA)", "nomor_handphone", "nomor wa", "whatsapp", "phone"],
    "child_under_5": ["child_under_5", "# Child <5", "jumlah_child_under_5", "child 0 5", "child <5"],
    "child_6_11": ["child_6_11", "# Child 6-11", "jumlah_child_6_11", "child 6 11"],
    "child_12_17": ["child_12_17", "# Child 12-17", "jumlah_child_12_17", "child 12 17"],
}

# Kolom minimal yang WAJIB ada supaya proses matching LOGIN bisa berjalan
REQUIRED_MATCH_COLUMNS = ["nama", "tanggal_lahir", "kelurahan", "area_program"]

# Kolom minimal yang WAJIB ada supaya proses matching REGISTER bisa berjalan
REQUIRED_MATCH_COLUMNS_REGISTER = ["nama", "tanggal_lahir", "nama_kepala_keluarga"]

# Kolom yang dipakai sebagai GATE (filter wajib) sebelum similarity dihitung
# — HANYA berlaku untuk dataset LOGIN. Duplikasi Login hanya dicek antar baris
# yang memiliki judul_kegiatan SAMA PERSIS (setelah dinormalisasi: strip + lower).
# Jika judul_kegiatan berbeda, pasangan tersebut TIDAK dibandingkan sama sekali
# (otomatis dianggap unik / di-keep), karena satu orang wajar hadir di banyak kegiatan.
#
# Dataset REGISTER TIDAK memakai gate ini — pengecekan Register dilakukan
# GLOBAL lintas seluruh baris (tanpa peduli judul_kegiatan), karena identitas
# peserta di form Register seharusnya unik secara keseluruhan, bukan per kegiatan.
DUPLICATE_GATE_COLUMN = "judul_kegiatan"

# Kolom yang dipakai untuk ditampilkan di UI reviewer (urutan tampil)
DISPLAY_COLUMNS = [
    "id_kobo",
    "nama",
    "tanggal_lahir",
    "kelurahan",
    "area_program",
    "judul_kegiatan",
    "tanggal_kegiatan",
    "timestamp_submit",
]

# =========================================================
# 2. BOBOT SCORING (WEIGHTED SIMILARITY)
# =========================================================
# --- Skema untuk dataset LOGIN/ABSENSI ---
# Dicek per judul_kegiatan (gate), karena orang boleh hadir di banyak kegiatan.
WEIGHT_NAMA = 0.45
WEIGHT_DOB = 0.25
WEIGHT_KELURAHAN = 0.20
WEIGHT_AREA = 0.10

# Validasi total bobot = 1.0 (100%)
assert abs((WEIGHT_NAMA + WEIGHT_DOB + WEIGHT_KELURAHAN + WEIGHT_AREA) - 1.0) < 1e-6, \
    "Total bobot scoring Login harus = 1.0"

# --- Skema untuk dataset REGISTER ---
# Register harus benar-benar UNIK secara global (lintas kegiatan, TIDAK di-gate
# oleh judul_kegiatan), karena satu orang seharusnya hanya register SEKALI saja
# meskipun dia nantinya ikut banyak kegiatan. Fokus pembanding: Nama, Tanggal
# Lahir, dan Nama Kepala Keluarga (identitas keluarga membantu memastikan
# apakah ini benar orang/keluarga yang sama).
WEIGHT_NAMA_REG = 0.45
WEIGHT_DOB_REG = 0.30
WEIGHT_KEPALA_KELUARGA_REG = 0.25

assert abs((WEIGHT_NAMA_REG + WEIGHT_DOB_REG + WEIGHT_KEPALA_KELUARGA_REG) - 1.0) < 1e-6, \
    "Total bobot scoring Register harus = 1.0"

# =========================================================
# 3. THRESHOLD ATURAN
# =========================================================
DUPLICATE_THRESHOLD = 95.0  # >= nilai ini => "Potensi Double Count"

# =========================================================
# 4. KOBOTOOLBOX API — DIBACA DARI st.secrets (BUKAN HARDCODE)
# =========================================================
# Semua kredensial (token & Asset UID) SEKARANG dibaca dari st.secrets, bukan
# ditulis langsung di file ini — supaya token TIDAK ikut ter-commit ke Git,
# dan supaya menambah Area Program (AP) baru cukup edit secrets.toml TANPA
# perlu mengubah kode Python sama sekali.
#
# CARA MENGISI (lihat juga secrets.toml.example di root proyek):
#   1. Buat file .streamlit/secrets.toml (LOKAL) — atau isi lewat menu
#      "Settings -> Secrets" di Streamlit Community Cloud (DEPLOY).
#   2. Isi strukturnya seperti ini:
#
#        [kobo]
#        default_token = "token_utama_anda"
#        default_login_uid = "asset_uid_form_login_default"
#        default_register_uid = "asset_uid_form_register_default"
#
#        [kobo.ap.AP-Simokerto]
#        token = "token_khusus_AP_Simokerto"          # boleh dikosongkan -> pakai default_token
#        login_uid = "asset_uid_login_AP_Simokerto"
#        register_uid = "asset_uid_register_AP_Simokerto"
#
#        [kobo.ap."AP-Kalimantan Barat"]
#        token = "token_khusus_AP_Kalbar"
#        login_uid = "asset_uid_login_AP_Kalbar"
#        register_uid = "asset_uid_register_AP_Kalbar"
#
#   3. Mau tambah AP baru? Tinggal tambah blok [kobo.ap.NamaAP_Baru] lagi di
#      secrets.toml — dropdown di sidebar app.py OTOMATIS menyesuaikan,
#      tidak perlu edit config.py atau app.py sama sekali.
#
# CATATAN: file .streamlit/secrets.toml TIDAK BOLEH ikut di-commit ke Git
# (masukkan ke .gitignore). Untuk deploy di Streamlit Community Cloud, isi
# lewat menu Secrets di dashboard, bukan file fisik di repo.
try:
    import streamlit as st
    _SECRETS_AVAILABLE = True
except ImportError:  # config.py bisa saja diimpor di luar konteks Streamlit (mis. testing)
    _SECRETS_AVAILABLE = False


def _get_secret(path: list, default=None):
    """Ambil nilai bersarang dari st.secrets dengan aman; fallback ke `default`
    kalau secrets.toml belum diisi/tidak ditemukan, supaya app tetap bisa
    jalan (dengan field kosong) alih-alih crash saat pertama kali setup."""
    if not _SECRETS_AVAILABLE:
        return default
    try:
        node = st.secrets
        for key in path:
            node = node[key]
        return node
    except Exception:
        return default


KOBO_TOKEN = _get_secret(["kobo", "default_token"], "")
KOBO_ENDPOINT = _get_secret(["kobo", "default_base_url"], "https://kf.kobotoolbox.org/api/v2")
KOBO_API_BASE_URL = KOBO_ENDPOINT  # alias, dipakai oleh kobo_api.py

FORM_UID_REGISTRASI = _get_secret(["kobo", "default_register_uid"], "")
FORM_UID_LOGIN = _get_secret(["kobo", "default_login_uid"], "")

KOBO_REQUEST_TIMEOUT = 30  # detik

# =========================================================
# 4b. MAPPING AREA PROGRAM (AP) -> TOKEN + ASSET UID (LOGIN & REGISTER)
# =========================================================
# Dibangun OTOMATIS dari st.secrets["kobo"]["ap"][...] — lihat contoh format
# di atas. Setiap AP boleh punya token SENDIRI (mis. akun Kobo terpisah per
# wilayah) atau tidak diisi sama sekali (akan pakai default_token).
def _build_ap_asset_map() -> dict:
    ap_secrets = _get_secret(["kobo", "ap"], {})
    result = {}
    try:
        items = ap_secrets.items()
    except AttributeError:
        items = []
    for ap_name, values in items:
        result[ap_name] = {
            "token": values.get("token") or KOBO_TOKEN,
            "login": values.get("login_uid", ""),
            "register": values.get("register_uid", ""),
        }
    return result


AP_ASSET_MAP = _build_ap_asset_map()

# =========================================================
# 5. SESSION STATE KEYS (biar konsisten, hindari typo string literal)
# =========================================================
SS_LOGIN_DF = "df_login"
SS_REGISTER_DF = "df_register"
SS_DUPLICATE_PAIRS_LOGIN = "df_duplicate_pairs_login"
SS_DUPLICATE_PAIRS_REGISTER = "df_duplicate_pairs_register"
SS_REVIEW_DECISIONS_LOGIN = "review_decisions_login"     # dict: pair_id -> keputusan (dataset Login)
SS_REVIEW_DECISIONS_REGISTER = "review_decisions_register"  # dict: pair_id -> keputusan (dataset Register)
SS_NOT_LOGIN_YET = "df_not_login_yet"
SS_APPENDED_IDS = "appended_ids"             # id_kobo dari register yang sudah di-append ke login

# =========================================================
# 6. METADATA PROJECT (INPUT MANUAL PANITIA -> DUPLICATE KE SEMUA BARIS)
# =========================================================
# Field-field ini diketik SEKALI oleh panitia di UI, lalu nilainya
# di-duplicate ke seluruh baris dataset Login & Register sebelum export.
PROJECT_METADATA_FIELDS = [
    "Implementor",
    "Sector",
    "CPM",
    "Project",
    "Project Category",
    "Activity",
    "Activity Detail",
]
# NOTE: "Activity Code" dan SELURUH field metadata di atas SEKARANG DI-ASSIGN
# PER ACARA (kombinasi Tanggal + Judul Kegiatan), BUKAN satu set global untuk
# semua data — supaya panitia TIDAK copy-paste 1 info project ke semua
# kegiatan yang beda. Dikelola lewat SS_EVENT_PROJECT_DATA (dict:
# (tanggal, judul) -> {"codes": list[str], "metadata": dict[field, value]}).
# Lihat _render_event_project_section() di app.py. Data BTT akan DIGANDAKAN
# 1 baris per peserta PER kode yang ditempelkan ke acara peserta itu, dengan
# metadata project MILIK ACARA ITU SENDIRI (bukan metadata global).

# dict: (tanggal_kegiatan, judul_kegiatan) -> {"codes": list[str], "metadata": dict}
# Diisi SATU ACARA PADA SATU WAKTU oleh panitia (lihat _render_event_project_section
# di app.py) — karena 1 form Kobo mencakup 1 Area Program (bukan 1 acara),
# data bisa punya banyak kombinasi tanggal+judul kegiatan berbeda, masing-masing
# butuh Activity Code MAUPUN info project (Implementor/Sector/dst) sendiri-sendiri
# (boleh beda per tanggal, bahkan untuk judul kegiatan yang sama).
SS_EVENT_PROJECT_DATA = "event_project_data"

# Area Program yang dipilih panitia di langkah Muat Data — dipakai sebagai
# SUMBER kolom "AP" (dan turunannya: Province/District) di sheet BTT.
# BUKAN diambil dari field form Kobo mana pun — murni dari pilihan dropdown
# panitia saat memuat data (berlaku untuk mode API maupun Upload CSV).
SS_AP_CONTEXT = "ap_context"

# =========================================================
# 7. KONFIGURASI DATABASE MARIADB (XAMPP)
# =========================================================
DB_HOST = _get_secret(["database", "host"], "localhost")
DB_PORT = _get_secret(["database", "port"], 3306)
DB_USER = _get_secret(["database", "user"], "root")
DB_PASSWORD = _get_secret(["database", "password"], "")
DB_NAME = _get_secret(["database", "name"], "db_belajar")
DB_TABLE = _get_secret(["database", "table_name"], "data_monitoring")
