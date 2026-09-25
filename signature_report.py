"""
signature_report.py
----------------------
Fitur "Signature Report per Kegiatan" — bagian TERPISAH dari alur BTT
utama, cuma digabung ke aplikasi yang sama supaya panitia tinggal pilih
kebutuhan di layar awal. TIDAK menyentuh/memanggil fungsi apa pun dari
app.py, config.py (kecuali baca AP_ASSET_MAP), data_processor.py, atau
matching.py — supaya alur BTT yang sudah ada TIDAK TERPENGARUH sama sekali.

Semua session_state di sini pakai prefix "sig_" supaya tidak bentrok
dengan session_state punya alur BTT (yang sama sekali tidak pakai prefix
ini).

ALUR (disederhanakan — tidak ada lagi pilihan "Jenis Form"):
  1. Panitia pilih Area Program, Tanggal Kegiatan, Judul Kegiatan.
  2. Sistem otomatis tarik Login DAN Register sekaligus.
  3. Auto-append: peserta yang sudah Register tapi belum Login untuk
     kegiatan ini ditambahkan ke daftar (data Usia/Kelurahan diambil dari
     Register-nya).
  4. Baru SETELAH auto-append selesai, cleaning duplikat otomatis jalan.
  5. PDF final: Nomor, Nama, Usia, Kelurahan, Tanda Tangan.
"""

from __future__ import annotations

import io
import re
from datetime import datetime

import requests
import streamlit as st
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, HRFlowable

DEFAULT_BASE_URL = "https://kf.kobotoolbox.org/api/v2"

# =========================================================
# NAMA FIELD yang dicari (BUKAN path lengkap) — suffix match, lihat
# _find_field_key(). SAMA untuk Login & Register (dikonfirmasi dari XLSForm).
# =========================================================
JUDUL_KEGIATAN_FIELD = "Judul_Kegiatan"
TANGGAL_KEGIATAN_FIELD = "Tanggal_Kegiatan"
SIGNATURE_FIELD = "Silahkan_tanda_tangan_disini"

LOGIN_NAMA_CANDIDATES = ["nama_child", "nama_pulldata_anak"]
LOGIN_TGL_LAHIR_CANDIDATES = ["tgl_lahir_child", "tgl_lahir_pulldata_anak"]
# Usia LANGSUNG (tanpa hitung dari tanggal lahir) — dipakai untuk peserta
# BUKAN dampingan WVI (form kasih pilihan isi usia manual, bukan tgl lahir).
LOGIN_USIA_LANGSUNG_CANDIDATES = ["usia_child"]
LOGIN_KELURAHAN_CANDIDATES = ["Kelurahan", "kelurahan_pulldata_anak"]

REGISTER_NAMA_CANDIDATES = ["nama_lengkap", "nama_lengkap_parent"]
# Dipakai KHUSUS untuk auto-append Register->Login (ambil Usia/Kelurahan dari
# Register untuk ditampilkan dengan "bentuk" kolom Login: Nama/Usia/Kelurahan).
REGISTER_USIA_CANDIDATES = ["usia_final", "usia", "usia_manual"]
REGISTER_KELURAHAN_CANDIDATES = ["Kelurahan_Final", "Kelurahan_001", "Kelurahan"]
# custom_id — dicoba beberapa kandidat, "custom_id" top-level diprioritaskan
# (konsisten dengan resolve_custom_id_column di data_processor.py).
CUSTOM_ID_CANDIDATES = ["custom_id", "cek_ID", "group_dewasa/cek_ID"]

# Kolom PDF final — TUNGGAL, tidak ada lagi varian per jenis form.
PDF_COLUMNS = [("Nama", "nama"), ("Usia", "usia"), ("Kelurahan", "kelurahan")]


# =========================================================
# UTILITAS TEKS
# =========================================================
def humanize_label(raw_text: str) -> str:
    if not raw_text:
        return raw_text
    text = str(raw_text).replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


# =========================================================
# PENGAMBILAN DATA KOBO (suffix-match field, robust terhadap grup)
# =========================================================
def _find_field_key(submission: dict, field_name: str) -> str | None:
    for key in submission.keys():
        if key == field_name or key.endswith("/" + field_name):
            return key
    return None


def _get_value(submission: dict, field_name: str) -> str:
    key = _find_field_key(submission, field_name)
    return str(submission.get(key, "")).strip() if key else ""


def _get_first_nonempty(submission: dict, candidates: list[str]) -> str:
    for field_name in candidates:
        value = _get_value(submission, field_name)
        if value:
            return value
    return ""


@st.cache_data(show_spinner=False, ttl=300)
def fetch_submissions(asset_uid: str, api_token: str, base_url: str) -> list[dict]:
    headers = {"Authorization": f"Token {api_token}"}
    url = f"{base_url.rstrip('/')}/assets/{asset_uid}/data.json"
    params = {"limit": 3000}
    results: list[dict] = []
    while url:
        response = requests.get(url, headers=headers, params=params, timeout=60)
        if response.status_code != 200:
            raise RuntimeError(f"Status HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        results.extend(payload.get("results", []))
        url = payload.get("next")
        params = None
    return results


def calculate_age(tgl_lahir: str, tanggal_acara: str) -> str:
    try:
        born = datetime.strptime(tgl_lahir[:10], "%Y-%m-%d")
        event_date = datetime.strptime(tanggal_acara[:10], "%Y-%m-%d")
        age = event_date.year - born.year - ((event_date.month, event_date.day) < (born.month, born.day))
        return str(age) if age >= 0 else ""
    except (ValueError, TypeError):
        return ""


def download_signature_bytes(submission: dict, api_token: str) -> bytes | None:
    attachments = submission.get("_attachments", [])
    signature_attachment = next(
        (att for att in attachments if str(att.get("question_xpath", "")).endswith(SIGNATURE_FIELD)),
        None,
    )
    if not signature_attachment:
        return None
    download_url = signature_attachment.get("download_url")
    try:
        response = requests.get(download_url, headers={"Authorization": f"Token {api_token}"}, timeout=30)
        if response.status_code == 200:
            return response.content
    except requests.exceptions.RequestException:
        pass
    return None


def build_login_row_from_register(register_submission: dict, tanggal_kegiatan: str) -> dict:
    """
    Bentuk 1 baris "ala Login" (Nama/Usia/Kelurahan) dari submission RAW
    Register — dipakai saat auto-append (orang yang Register tapi belum
    Login untuk kegiatan ini). Usia diambil LANGSUNG dari field Register
    (usia_final/usia/usia_manual) — BUKAN dihitung dari tanggal lahir,
    karena kita tidak selalu punya tanggal lahir Login-nya.
    """
    return {
        "nama": _get_first_nonempty(register_submission, REGISTER_NAMA_CANDIDATES),
        "usia": _get_first_nonempty(register_submission, REGISTER_USIA_CANDIDATES),
        "kelurahan": _get_first_nonempty(register_submission, REGISTER_KELURAHAN_CANDIDATES),
        "submission": register_submission,
    }


def auto_append_register_to_login(
    login_submissions: list[dict], register_submissions: list[dict],
) -> tuple[list[dict], int]:
    """
    Auto-append Register->Login untuk Signature Report per Kegiatan:
    orang yang sudah Register untuk kegiatan ini tapi custom_id-nya TIDAK
    ketemu di Login manapun, ditambahkan sebagai baris tambahan (ala Login).

    Pencocokan MURNI by custom_id (exact) — submission Login/Register tanpa
    custom_id TIDAK ikut proses pencocokan ini (tetap dipertahankan apa
    adanya di sisi Login, tapi tidak bisa dijadikan acuan "sudah ada").

    Returns
    -------
    (submissions_gabungan, jumlah_yang_di_auto_append)
    """
    login_ids = {
        _get_first_nonempty(s, CUSTOM_ID_CANDIDATES).strip()
        for s in login_submissions
        if _get_first_nonempty(s, CUSTOM_ID_CANDIDATES).strip()
    }

    appended = []
    seen_register_ids = set()
    for s in register_submissions:
        rid = _get_first_nonempty(s, CUSTOM_ID_CANDIDATES).strip()
        if not rid or rid in login_ids or rid in seen_register_ids:
            continue
        seen_register_ids.add(rid)
        appended.append(s)

    return login_submissions + appended, len(appended)


def extract_rows(submissions: list[dict], tanggal_kegiatan: str) -> list[dict]:
    """
    Bentuk baris (Nama/Usia/Kelurahan) dari submission — baik yang genuine
    Login MAUPUN hasil auto-append dari Register (submission itu tidak
    punya field Login sama sekali, makanya selalu FALLBACK ke field
    Register kalau field Login-nya kosong).
    """
    rows = []
    for s in submissions:
        nama = _get_first_nonempty(s, LOGIN_NAMA_CANDIDATES) or _get_first_nonempty(s, REGISTER_NAMA_CANDIDATES)
        kelurahan = _get_first_nonempty(s, LOGIN_KELURAHAN_CANDIDATES) or _get_first_nonempty(s, REGISTER_KELURAHAN_CANDIDATES)

        usia_langsung = _get_first_nonempty(s, LOGIN_USIA_LANGSUNG_CANDIDATES)
        if usia_langsung:
            usia = usia_langsung
        else:
            tgl_lahir = _get_first_nonempty(s, LOGIN_TGL_LAHIR_CANDIDATES)
            usia = calculate_age(tgl_lahir, tanggal_kegiatan) if tgl_lahir else ""
            if not usia:
                usia = _get_first_nonempty(s, REGISTER_USIA_CANDIDATES)

        rows.append({
            "nama": nama,
            "usia": usia,
            "kelurahan": kelurahan,
            "submission": s,
        })
    return rows


def remove_duplicate_rows(rows: list[dict]) -> tuple[list[dict], int]:
    seen = set()
    cleaned = []
    for row in rows:
        key = row.get("nama", "").strip().lower()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        cleaned.append(row)
    return cleaned, len(rows) - len(cleaned)


# =========================================================
# PEMBUATAN PDF
# =========================================================
def make_signature_flowable(image_bytes: bytes | None, max_width_pt: float = 90, max_height_pt: float = 32):
    placeholder_style = ParagraphStyle("Placeholder", fontSize=8, textColor=colors.grey, alignment=TA_CENTER)
    if not image_bytes:
        return Paragraph("(tidak ada)", placeholder_style)
    try:
        pil_img = PILImage.open(io.BytesIO(image_bytes))
        w, h = pil_img.size
        scale = min(max_width_pt / w, max_height_pt / h, 1.0)
        return RLImage(io.BytesIO(image_bytes), width=w * scale, height=h * scale)
    except Exception:
        return Paragraph("(gagal dimuat)", placeholder_style)


def build_attendance_pdf(
    judul_kegiatan_display: str,
    tanggal_kegiatan: str,
    rows: list[dict],
    api_token: str,
) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=1.8 * cm, bottomMargin=1.8 * cm, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("JudulKegiatan", parent=styles["Title"], fontSize=17, textColor=colors.HexColor("#1E3A8A"), spaceAfter=2)
    subtitle_style = ParagraphStyle("TanggalKegiatan", parent=styles["Normal"], fontSize=11, textColor=colors.grey, alignment=TA_CENTER, spaceAfter=4)
    footer_style = ParagraphStyle("Footer", parent=styles["Normal"], fontSize=8, textColor=colors.grey, alignment=TA_CENTER)
    total_style = ParagraphStyle("Total", parent=styles["Normal"], fontSize=10, fontName="Helvetica-Bold", spaceBefore=10)

    story = [
        Paragraph(judul_kegiatan_display or "(Tanpa Judul Kegiatan)", title_style),
        Paragraph(f"Tanggal Kegiatan: {tanggal_kegiatan or '-'}", subtitle_style),
        HRFlowable(width="100%", thickness=1, color=colors.HexColor("#1E3A8A"), spaceAfter=14),
    ]

    header_row = ["No"] + [label for label, _ in PDF_COLUMNS] + ["Tanda Tangan"]
    table_data = [header_row]
    cell_style = ParagraphStyle("Cell", parent=styles["Normal"], fontSize=9, leading=11)
    cell_center_style = ParagraphStyle("CellCenter", parent=cell_style, alignment=TA_CENTER)

    for i, row in enumerate(rows, start=1):
        signature_bytes = download_signature_bytes(row["submission"], api_token)
        data_row = [Paragraph(str(i), cell_center_style)]
        for _, key in PDF_COLUMNS:
            data_row.append(Paragraph(str(row.get(key, "") or "-"), cell_style))
        data_row.append(make_signature_flowable(signature_bytes))
        table_data.append(data_row)

    total_width = 18 * cm
    no_width = 1.2 * cm
    sig_width = 4 * cm
    middle_width = (total_width - no_width - sig_width) / len(PDF_COLUMNS)
    col_widths = [no_width] + [middle_width] * len(PDF_COLUMNS) + [sig_width]

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F4F6")]),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(table)

    story.append(Paragraph(f"Total Peserta: {len(rows)} orang", total_style))
    story.append(Spacer(1, 24))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CBD5E1")))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"Dicetak melalui Sistem Laporan Absensi pada {datetime.now().strftime('%d %B %Y, %H:%M')}",
        footer_style,
    ))

    doc.build(story)
    return buffer.getvalue()


# =========================================================
# UI (dipanggil dari app.py, TIDAK menyentuh alur BTT sama sekali)
# =========================================================
def render_signature_app(ap_asset_map: dict) -> None:
    """
    Render seluruh UI fitur Signature Report per Kegiatan. Dipanggil dari
    app.py HANYA kalau panitia memilih fitur ini di layar awal.

    Parameters
    ----------
    ap_asset_map : dict
        Sama persis config.AP_ASSET_MAP (dari secrets.toml) — dipakai ULANG,
        BUKAN baca secrets terpisah, supaya 1 sumber AP untuk semua fitur.
    """
    st.title("📋 Signature Report per Kegiatan")
    st.caption("Pilih 1 kegiatan, sistem otomatis gabungkan Login + Register (auto-append), bersihkan duplikat, lalu hasilkan PDF absensi bertanda tangan.")

    if not ap_asset_map:
        st.error(
            "⚠️ Belum ada Area Program terdaftar di secrets.toml. "
            "Tambahkan blok `[kobo.ap.NamaAP]` untuk memakai fitur ini."
        )
        return

    selected_ap = st.selectbox("1️⃣ Pilih Area Program", sorted(ap_asset_map.keys()), key="sig_selected_ap")
    ap_config = ap_asset_map[selected_ap]
    login_uid = ap_config.get("login", "")
    register_uid = ap_config.get("register", "")

    if not login_uid or not register_uid:
        st.error(f"⚠️ Asset UID Login/Register untuk '{selected_ap}' belum lengkap di secrets.toml.")
        return

    load_key = "sig_login_submissions"
    register_key = "sig_register_submissions"
    loaded_ap_key = "sig_loaded_ap"

    if st.button("🔄 Tarik Data", use_container_width=True, key="sig_fetch_btn"):
        with st.spinner("Mengambil data Login & Register dari KoboToolbox..."):
            try:
                base_url = ap_config.get("base_url") or DEFAULT_BASE_URL
                login_submissions = fetch_submissions(login_uid, ap_config["token"], base_url)
                register_submissions = fetch_submissions(register_uid, ap_config["token"], base_url)
                st.session_state[load_key] = login_submissions
                st.session_state[register_key] = register_submissions
                st.session_state[loaded_ap_key] = selected_ap
                st.success(f"✅ {len(login_submissions)} data Login + {len(register_submissions)} data Register berhasil ditarik.")
            except Exception as e:
                st.error(f"❌ Gagal mengambil data: {e}")

    if load_key not in st.session_state or st.session_state.get(loaded_ap_key) != selected_ap:
        st.info("📭 Klik tombol di atas untuk menarik data dulu.")
        return

    login_submissions = st.session_state[load_key]
    register_submissions = st.session_state.get(register_key, [])

    all_submissions_for_dropdown = login_submissions + register_submissions
    tanggal_list = sorted({
        _get_value(s, TANGGAL_KEGIATAN_FIELD) for s in all_submissions_for_dropdown if _get_value(s, TANGGAL_KEGIATAN_FIELD)
    })
    if not tanggal_list:
        st.warning("⚠️ Tidak ada data dengan Tanggal Kegiatan yang terisi.")
        return

    selected_tanggal = st.selectbox("2️⃣ Pilih Tanggal Kegiatan", tanggal_list, key="sig_tanggal")

    raw_judul_list = sorted({
        _get_value(s, JUDUL_KEGIATAN_FIELD) for s in all_submissions_for_dropdown
        if _get_value(s, TANGGAL_KEGIATAN_FIELD) == selected_tanggal and _get_value(s, JUDUL_KEGIATAN_FIELD)
    })
    if not raw_judul_list:
        st.warning(f"⚠️ Tidak ada Judul Kegiatan untuk tanggal {selected_tanggal}.")
        return

    display_to_raw = {humanize_label(j): j for j in raw_judul_list}
    selected_judul_display = st.selectbox("3️⃣ Pilih Judul Kegiatan", sorted(display_to_raw.keys()), key="sig_judul")
    selected_judul_raw = display_to_raw[selected_judul_display]

    matching_login = [
        s for s in login_submissions
        if _get_value(s, TANGGAL_KEGIATAN_FIELD) == selected_tanggal and _get_value(s, JUDUL_KEGIATAN_FIELD) == selected_judul_raw
    ]
    matching_register = [
        s for s in register_submissions
        if _get_value(s, TANGGAL_KEGIATAN_FIELD) == selected_tanggal and _get_value(s, JUDUL_KEGIATAN_FIELD) == selected_judul_raw
    ]

    # --- Auto-append Register->Login DULU, baru cleaning duplikat ---
    matching_submissions, n_appended = auto_append_register_to_login(matching_login, matching_register)
    rows = extract_rows(matching_submissions, selected_tanggal)
    rows, n_removed = remove_duplicate_rows(rows)

    st.divider()
    st.subheader("4️⃣ Pengecekan Data")
    col1, col2, col3 = st.columns(3)
    col1.metric("Dari Login", len(matching_login))
    col2.metric("Auto-append dari Register", n_appended)
    col3.metric("Duplikat Dihapus", n_removed)
    if not rows:
        st.warning("⚠️ Tidak ada peserta untuk kombinasi Tanggal + Judul ini.")

    st.divider()

    if st.button("🚀 Mulai", type="primary", use_container_width=True, key="sig_mulai_btn"):
        if not rows:
            st.warning("⚠️ Tidak ada peserta untuk kombinasi Tanggal + Judul ini.")
            return

        with st.spinner("Membuat PDF (termasuk mengunduh gambar tanda tangan)..."):
            pdf_bytes = build_attendance_pdf(selected_judul_display, selected_tanggal, rows, ap_config["token"])

        st.success("✅ PDF berhasil dibuat.")
        st.download_button(
            "⬇️ Unduh PDF Laporan Absensi",
            data=pdf_bytes,
            file_name=f"absensi_{selected_judul_display}_{selected_tanggal}.pdf".replace(" ", "_"),
            mime="application/pdf",
            type="primary",
            use_container_width=True,
            key="sig_download_btn",
        )
