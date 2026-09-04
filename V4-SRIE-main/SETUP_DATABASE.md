# Setup Database — Simulasi Lokal (XAMPP) → Nanti Pindah ke AWS MariaDB

## Konsep Penting
Kode aplikasi (`data_processor.py`, `config.py`) **sudah generik** — koneksi database
(host/port/user/password/nama database) semuanya dibaca dari `secrets.toml`, BUKAN
di-hardcode di kode. Jadi nanti di kantor, pindah dari XAMPP lokal ke MariaDB AWS
**cukup ganti isi `secrets.toml`**, tidak perlu ubah satu baris kode pun.

## ⚠️ Batasan Penting Selama Simulasi
Karena XAMPP jalan di laptop Anda (bukan di internet), **Streamlit Cloud tidak bisa
menjangkaunya**. Selama fase simulasi ini, jalankan Streamlit **secara lokal**:
```bash
streamlit run app.py
```
Bukan lewat Streamlit Cloud. Nanti kalau sudah pindah ke MariaDB AWS (yang punya
endpoint publik), baru boleh pakai Streamlit Cloud lagi.

---

## Langkah 1 — Nyalakan XAMPP
1. Buka **XAMPP Control Panel**.
2. Klik **Start** pada modul **MySQL** (MariaDB terintegrasi di dalamnya).
3. Pastikan statusnya hijau/"Running".

## Langkah 2 — Buat Database & Tabel
1. Buka **phpMyAdmin**: `http://localhost/phpmyadmin`
2. Klik menu **SQL** di bagian atas.
3. Copy-paste **seluruh isi** file `create_table_data_monitoring.sql` (terlampir), klik **Go**.
4. Setelah selesai, Anda akan punya:
   - Database: `db_belajar`
   - Tabel: `data_monitoring` (46 kolom, sudah tervalidasi jalan — saya sudah tes
     langsung ke MariaDB sungguhan sebelum kasih ke Anda)

## Langkah 3 — (Opsional tapi disarankan) Buat User Khusus, Jangan Pakai `root`
Di phpMyAdmin → SQL, jalankan:
```sql
CREATE USER 'srie_app'@'localhost' IDENTIFIED BY 'password_anda_sendiri';
GRANT ALL PRIVILEGES ON db_belajar.* TO 'srie_app'@'localhost';
FLUSH PRIVILEGES;
```
Ganti `password_anda_sendiri` dengan password pilihan Anda. Ini praktik lebih aman
daripada connect pakai `root` tanpa password (default XAMPP).

## Langkah 4 — Isi `secrets.toml` Lokal
Buat folder `.streamlit/` di root proyek Anda (kalau belum ada), lalu buat/edit
`.streamlit/secrets.toml`, tambahkan bagian ini (boleh digabung dengan bagian
`[kobo]` yang sudah ada):

```toml
[database]
host = "localhost"
port = 3306
user = "srie_app"
password = "password_anda_sendiri"
name = "db_belajar"
table_name = "data_monitoring"
```

## Langkah 5 — Jalankan & Tes
```bash
streamlit run app.py
```
Lanjutkan sampai Langkah ⑥ Export, klik **"🚀 Simpan Data ke MariaDB"**. Cek hasilnya
di phpMyAdmin → tabel `data_monitoring`.

---

## Nanti di Kantor: Pindah ke MariaDB AWS

Cukup ganti isi `secrets.toml` (baik file lokal, atau lewat menu Secrets di
Streamlit Cloud kalau sudah deploy), jadi seperti ini:

```toml
[database]
host = "nama-endpoint-rds-anda.xxxxxxxxxx.ap-southeast-1.rds.amazonaws.com"
port = 3306
user = "user_dari_tim_infra"
password = "password_dari_tim_infra"
name = "nama_database_produksi"
table_name = "data_monitoring"
```

**Tidak ada perubahan kode sama sekali.**

### Kalau nanti pakai Streamlit Cloud + AWS MariaDB (bukan cuma lokal)
Ada 1 hal tambahan yang perlu dikoordinasikan dengan tim infra/AWS Anda:
- **Security Group** RDS/MariaDB AWS harus mengizinkan koneksi masuk (inbound)
  di port 3306 dari internet (atau minimal dari IP Streamlit Cloud, kalau tim
  infra ingin membatasi). Kalau security group-nya cuma izinkan dari IP kantor/VPN,
  Streamlit Cloud (yang jalan dari server Google Cloud) **tidak akan bisa connect**.
- Sebaiknya tanyakan ke tim infra: apakah endpoint MariaDB AWS ini memang didesain
  untuk diakses dari luar (public-facing), atau cuma dari jaringan internal kantor
  saja (kalau begitu, opsi paling aman adalah Anda tetap jalankan Streamlit-nya
  dari server/komputer yang ada di jaringan kantor juga, bukan Streamlit Cloud).

## Catatan Skema
Kalau nanti ada kolom BTT baru ditambahkan ke kode (`BTT_TO_SQL_MAP` di
`data_processor.py`), tabel `data_monitoring` — baik yang di XAMPP maupun di AWS —
perlu di-`ALTER TABLE` menambahkan kolom yang sesuai. Lihat catatan di bagian
bawah `create_table_data_monitoring.sql`.
