-- ============================================================================
-- create_table_data_monitoring.sql
-- ----------------------------------------------------------------------------
-- Skema tabel MariaDB untuk menampung hasil export BTT dari aplikasi SRIE.
-- Kolom & tipe data PERSIS mengikuti BTT_TO_SQL_MAP di data_processor.py —
-- kalau nanti ada kolom BTT baru ditambahkan di kode, tabel ini juga perlu
-- di-ALTER TABLE menambahkan kolom yang sesuai (lihat catatan di akhir file).
--
-- CARA PAKAI (di phpMyAdmin XAMPP):
--   1. Buka phpMyAdmin (http://localhost/phpmyadmin)
--   2. Klik "SQL" di menu atas (atau pilih database dulu kalau sudah dibuat)
--   3. Copy-paste seluruh isi file ini, klik "Go"
-- ============================================================================

CREATE DATABASE IF NOT EXISTS db_belajar
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE db_belajar;

CREATE TABLE IF NOT EXISTS data_monitoring (
    row_id                  INT AUTO_INCREMENT PRIMARY KEY,

    -- Metadata Project
    implementor             VARCHAR(255),
    sector                  VARCHAR(255),
    cpm                     VARCHAR(255),
    project                 VARCHAR(255),
    project_category        VARCHAR(255),
    output_code             VARCHAR(50),
    activity_code           VARCHAR(50),
    activity                VARCHAR(255),
    activity_detail         TEXT,

    -- Waktu & Fiskal
    activity_date           DATE,
    month                   VARCHAR(20),
    month_first             VARCHAR(20),
    fiscal_year             VARCHAR(10),

    -- Identitas Peserta
    id                      VARCHAR(100),
    full_name               VARCHAR(255),
    household_name          VARCHAR(255),
    sex                     VARCHAR(20),
    age                     INT NULL,               -- NULL = data tidak ada (BUKAN 0)
    age_group               VARCHAR(20),
    category                VARCHAR(20),

    -- Lokasi
    ap                      VARCHAR(100),
    province                VARCHAR(100),
    district                VARCHAR(100),
    zonal                   VARCHAR(100),
    sub_district             VARCHAR(100),
    village                 VARCHAR(150),
    sub_village_1           VARCHAR(20),            -- RW
    sub_village_2           VARCHAR(20),            -- RT

    -- Disabilitas & RC
    disability_category     VARCHAR(150),
    disability_status       VARCHAR(20),
    rc                      VARCHAR(20),
    rc_status                VARCHAR(50),
    idn                     INT NULL,

    -- MVC (Most Vulnerable Children)
    mvc_dimensi_1           VARCHAR(10),
    mvc_dimensi_2           VARCHAR(10),
    mvc_dimensi_3           VARCHAR(10),
    mvc_dimensi_4           VARCHAR(10),
    mvc                     VARCHAR(10),

    -- Social Protection
    social_protection                    VARCHAR(10),
    sp_cash_transfers_food_assistance    VARCHAR(10),
    sp_health_assistance                 VARCHAR(10),
    sp_education_assistance              VARCHAR(10),

    -- Institusi & Kontak
    institution              VARCHAR(255),
    position                 VARCHAR(255),
    no_handphone             VARCHAR(30),

    -- Jumlah Anak
    child_under_5            INT NULL,
    child_6_11               INT NULL,
    child_12_17              INT NULL,

    -- Jejak audit
    exported_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Kombinasi (id, activity_date) dipakai sistem untuk cek duplikat sebelum
    -- insert (lihat export_btt_to_mariadb di data_processor.py) — index ini
    -- mempercepat pengecekan itu, TIDAK dibuat UNIQUE (dibiarkan fleksibel
    -- kalau suatu saat mau menampung riwayat/revisi data).
    INDEX idx_id_activity_date (id, activity_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================================
-- CATATAN UNTUK PERUBAHAN SKEMA DI MASA DEPAN:
-- Kalau nanti ada kolom BTT baru ditambahkan di BTT_TO_SQL_MAP
-- (data_processor.py), tabel ini perlu di-ALTER TABLE juga, contoh:
--
--   ALTER TABLE data_monitoring ADD COLUMN nama_kolom_baru VARCHAR(255);
--
-- Ini berlaku SAMA baik untuk database lokal (XAMPP) maupun MariaDB di AWS
-- nanti — jangan lupa jalankan ALTER TABLE yang sama di kedua tempat.
-- ============================================================================
