# V4 SRIE — Final Interactive

## Workflow
1. Muat Data
2. Fuzzy Pairwise Login + Register
3. Review Login
4. Review Register
5. Finalisasi / Register yang Belum Login
6. Generate & Export BTT

## Matching
- Threshold dapat diubah langsung di Streamlit.
- Login dan Register menggunakan fuzzy pairwise global.
- Judul kegiatan dan tanggal kegiatan tidak menjadi gate sebelum fuzzy score.
- Pada hasil review, `same_event` menunjukkan apakah judul + tanggal kegiatan sama.
- Custom ID dikonsolidasikan ke ID canonical berdasarkan submission pertama yang valid.
- `_row_uid` dipakai untuk operasi per-baris sehingga `id_kobo` yang berulang tidak menghapus event lain.

## BTT
- Date = tanggal kegiatan dari Login.
- Month = fiscal month: Oct=1 ... Sep=12.
- Month (First) = bulan fiskal earliest Register per participant.
- Fiscal Year = FY calendar year, +1 untuk Oct-Dec.
- ID / Full Name = Login.
- Household Name / Sex / Age = Register via custom_id, fallback Full Name.
- Age group dan Category dihitung dari Age.

## Run
```bash
pip install -r requirements.txt
streamlit run app.py
```
