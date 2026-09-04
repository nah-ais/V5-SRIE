"""
kobo_api.py
-----------
Modul untuk komunikasi dengan KoboToolbox API.
Bertanggung jawab untuk:
  1. Mengambil data submission dari sebuah form (asset) Kobo.
  2. Melakukan rename kolom sesuai mapping di config.py.
  3. Penanganan error dasar (auth gagal, timeout, koneksi, format asing).

Catatan:
- Membutuhkan API Token dari akun KoboToolbox (Account Settings > Security > API Token).
- Endpoint yang dipakai: GET /assets/{asset_uid}/data.json
"""

from __future__ import annotations

import requests
import pandas as pd
from typing import Optional

import config


class KoboAPIError(Exception):
    """Exception khusus untuk error yang berasal dari interaksi KoboToolbox API."""
    pass


def fetch_kobo_data(
    asset_uid: str,
    api_token: str,
    column_map: dict,
    base_url: str = config.KOBO_API_BASE_URL,
    timeout: int = config.KOBO_REQUEST_TIMEOUT,
) -> pd.DataFrame:
    """
    Mengambil seluruh data submission dari satu form KoboToolbox
    dan mengembalikannya sebagai DataFrame dengan kolom yang sudah di-rename.

    Parameters
    ----------
    asset_uid : str
        UID/ID dari form (asset) di KoboToolbox, contoh: 'aXXXXXXXXXXXXXXXX'
    api_token : str
        Token API pribadi pengguna KoboToolbox.
    column_map : dict
        Mapping nama kolom asli Kobo -> nama kolom internal (dari config.py).
    base_url : str
        Base URL API Kobo (beda jika server EU / self-hosted).
    timeout : int
        Timeout request dalam detik.

    Returns
    -------
    pd.DataFrame
        Data hasil fetch, sudah di-rename sesuai column_map, kolom yang
        tidak ada di response akan diisi kosong (NaN) agar skema tetap konsisten.

    Raises
    ------
    KoboAPIError
        Jika terjadi kegagalan autentikasi, koneksi, timeout, atau format
        response yang tidak sesuai ekspektasi.
    """
    if not asset_uid or not api_token:
        raise KoboAPIError("asset_uid dan api_token wajib diisi.")

    url = f"{base_url.rstrip('/')}/assets/{asset_uid}/data.json"
    headers = {"Authorization": f"Token {api_token}"}

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        raise KoboAPIError(f"Request timeout ({timeout}s) saat menghubungi KoboToolbox.")
    except requests.exceptions.ConnectionError:
        raise KoboAPIError("Gagal terhubung ke server KoboToolbox. Periksa koneksi internet / URL.")
    except requests.exceptions.RequestException as e:
        raise KoboAPIError(f"Terjadi kesalahan request: {e}")

    if response.status_code == 401:
        raise KoboAPIError("Autentikasi gagal (401). Periksa kembali API Token Anda.")
    if response.status_code == 403:
        raise KoboAPIError("Akses ditolak (403). Pastikan token memiliki izin ke asset ini.")
    if response.status_code == 404:
        raise KoboAPIError("Asset (form) tidak ditemukan (404). Periksa kembali Asset UID.")
    if response.status_code != 200:
        raise KoboAPIError(f"KoboToolbox mengembalikan status {response.status_code}: {response.text[:200]}")

    try:
        payload = response.json()
        results = payload.get("results", [])
    except ValueError:
        raise KoboAPIError("Response dari KoboToolbox bukan JSON yang valid.")

    if not results:
        # Bukan error fatal — kembalikan DataFrame kosong dengan skema kolom internal
        empty_cols = list(column_map.values())
        return pd.DataFrame(columns=empty_cols)

    df_raw = pd.json_normalize(results)

    # Rename hanya kolom yang memang ada di response (hindari KeyError)
    existing_map = {k: v for k, v in column_map.items() if k in df_raw.columns}
    df = df_raw.rename(columns=existing_map)

    # Pastikan semua kolom target (dari column_map) tersedia, walau kosong
    for target_col in column_map.values():
        if target_col not in df.columns:
            df[target_col] = pd.NA

    # PENTING: kolom mentah Kobo yang TIDAK ada di column_map (mis. custom_id/
    # cek_ID hasil dynamic data attachment, atau field tambahan untuk BTT seperti
    # Disability/RC/IDN/MVC/SP) TETAP DIPERTAHANKAN apa adanya (tidak dibuang),
    # supaya bisa dikenali belakangan oleh resolver alias (lihat
    # data_processor.resolve_custom_id_column & BTT_REGISTER_FIELD_ALIASES).
    # Sebelumnya kolom-kolom ini dibuang di sini sehingga selalu kosong di BTT.
    mapped_targets = list(dict.fromkeys(column_map.values()))
    other_cols = [c for c in df.columns if c not in mapped_targets]
    df = df[mapped_targets + other_cols]

    return df.reset_index(drop=True)


def load_csv_fallback(uploaded_file, column_map: dict) -> pd.DataFrame:
    """
    Fallback loader: membaca CSV hasil export manual dari KoboToolbox
    (berguna kalau API tidak bisa diakses / untuk testing offline).

    Parameters
    ----------
    uploaded_file : UploadedFile (Streamlit) atau path str
    column_map : dict
        Mapping nama kolom asli Kobo -> nama kolom internal.

    Returns
    -------
    pd.DataFrame
    """
    try:
        df_raw = pd.read_csv(uploaded_file)
    except Exception as e:
        raise KoboAPIError(f"Gagal membaca file CSV: {e}")

    existing_map = {k: v for k, v in column_map.items() if k in df_raw.columns}
    df = df_raw.rename(columns=existing_map)

    for target_col in column_map.values():
        if target_col not in df.columns:
            df[target_col] = pd.NA

    # Sama seperti fetch_kobo_data: kolom mentah di luar column_map TETAP
    # dipertahankan (tidak dibuang) supaya custom_id & field BTT tambahan
    # tetap bisa dikenali oleh resolver alias.
    mapped_targets = list(dict.fromkeys(column_map.values()))
    other_cols = [c for c in df.columns if c not in mapped_targets]
    df = df[mapped_targets + other_cols]
    return df.reset_index(drop=True)
