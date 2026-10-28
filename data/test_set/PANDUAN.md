# Panduan Mengisi Test Set SAKSI

Pemeriksaan silang dilakukan dengan pelabelan ulang buta oleh AI (lihat bagian
*Pemeriksaan silang*). Isi dua file di folder ini: `pesan.csv` dan `klaim.csv`. Template kosongnya sudah ada.

## Cara kerja yang dipakai (opsi c, keputusan 28 Okt)

| Langkah | Siapa | Yang dikerjakan |
|---|---|---|
| 1 | Pemilik proyek | Memilih pesan: isi `sumber_pilihan.csv` (kategori + URL sumber, atau ide singkat untuk `tidak_terverifikasi`). Cari kandidat dengan `eval.cari_sumber` |
| 2 | Claude | Membaca setiap sumber, lalu menyusun draf `pesan.csv` dan `klaim.csv`: teks pesan bergaya WA, klaim, label awal, `sudah_dicek_media`. Sumber yang tidak bisa dibaca bot (mis. Tempo, Tirto, TribunNews) ditandai di `catatan` |
| 3 | Pemilik proyek | Memeriksa setiap baris: apakah klaim dan label sesuai sumbernya. Ubah bila perlu; label akhir adalah keputusan pemilik proyek. Isi `pemeriksa_silang` dengan namamu |
| 4 | Pemilik proyek | `eval.test_set cek` → `bagi` → `blocklist` |

Isi `sumber_pilihan.csv`:

| Kolom | Isi |
|---|---|
| `no` | 1, 2, 3, … |
| `kategori` | `hoaks` / `fakta` / `campuran` / `tidak_terverifikasi` |
| `url` | URL sumber; lebih dari satu dipisah `;`. Untuk `campuran`: minimal satu sumber hoaks dan satu sumber fakta. Untuk `tidak_terverifikasi`: kosongkan |
| `catatan` | Opsional: bagian mana yang dipakai, atau ide singkat untuk `tidak_terverifikasi` |

Target: 15 hoaks, 10 fakta, 10 campuran, 5 tidak terverifikasi (contoh C01–C04 boleh dihitung). Utamakan sumber yang
bisa dibuka biasa (TurnBackHoax, detik, situs pemerintah), supaya isinya bisa dibaca saat menyusun draf.

Dengan cara ini, **pelabelan ulang buta oleh AI tidak dipakai**: Claude tidak bisa memeriksa label buatannya sendiri.
Pemeriksaan silangnya adalah langkah 3. Di laporan ditulis: pemilihan pesan dan label akhir oleh pemilik proyek;
penyusunan teks dan label awal oleh AI (Claude) dari sumber yang dipilih pemilik proyek.

---

## Mulai cepat

1. **Lihat contoh terisi**: `contoh_pesan.csv` dan `contoh_klaim.csv` berisi 4 pesan (1 per kategori) dengan
   sumber nyata yang sudah dibaca isinya. Cek dengan `.venv\Scripts\python.exe -m eval.test_set cek --contoh`.
   Contoh ini boleh disalin sebagai 4 pesan pertama test set setelah kamu membaca sendiri sumbernya (lihat catatan
   di bawah tentang pemeriksaan silang).
2. **Cari kandidat sumber** tanpa membuka browser satu per satu:
   ```powershell
   .venv\Scripts\python.exe -m eval.cari_sumber bansos              # artikel cek fakta + usulan label
   .venv\Scripts\python.exe -m eval.cari_sumber "beasiswa" --berita  # + berita 30 hari terakhir (kuota GNews)
   ```
   Tetap buka dan baca artikelnya sebelum memakai labelnya.
3. **Kolom yang boleh dikosongkan** karena dilengkapi otomatis:
   - `putusan_pesan`: dihitung dari label klaim. Kalau diisi dan ternyata tidak cocok, validator memberi error.
   - `id_klaim`: diberi nomor urut per pesan (P07-1, P07-2, …).

**Hindari** hoaks berupa prediksi ("Indonesia bakal diguncang gempa dahsyat akhir 2026"). Sistem sengaja tidak
memeriksa prediksi (ditandai BUKAN KLAIM FAKTUAL), jadi pesan seperti itu pasti dinilai salah. Rating "menyesatkan" /
"sesat" / "sebagian benar" juga perlu dibaca dulu: kadang klaimnya benar tetapi konteksnya keliru, jadi labelnya
belum tentu HOAKS.

Untuk contoh C01–C04, label dibuat oleh Claude dari sumber yang sudah dibaca, sehingga pemeriksaan silangnya
dibalik: **kamu** yang memeriksa sumber dan labelnya. Kalau setuju, isi `pemeriksa_silang` dengan namamu.

---

Paling mudah lewat **Google Sheets**: buat dua sheet, isi, lalu *File → Download → Comma-separated values (.csv)*.
Excel juga boleh, asal disimpan sebagai **CSV UTF-8**. CSV biasa dari Excel merusak huruf non-Latin.

Setelah diisi, cek dengan:

```powershell
.venv\Scripts\python.exe -m eval.test_set cek
```

---

## Target jumlah (RENCANA §7)

| Kategori | Jumlah | Isi pesan |
|---|---|---|
| `hoaks` | 15 | Pesan yang klaim utamanya sudah dinyatakan salah oleh media cek fakta |
| `fakta` | 10 | Pesan yang semua klaimnya benar menurut sumber resmi (kementerian, BMKG, media besar) |
| `campuran` | 10 | Pesan berisi klaim benar **dan** klaim salah (menguji pemecahan klaim) |
| `tidak_terverifikasi` | 5 | Klaim lokal/pribadi yang memang tidak bisa dicek publik (mis. "Pak RT di kampung X membagikan sembako") |

Total 40. Nanti 10 dipakai untuk *dev* (memperbaiki sistem) dan 30 untuk *held-out* (dibekukan, hanya dijalankan
sekali di akhir). Pembagian dibuat oleh skrip, bukan dipilih manual.

---

## File 1: `pesan.csv` (satu baris = satu pesan)

| Kolom | Isi | Contoh |
|---|---|---|
| `id_pesan` | Kode unik: P01, P02, … | `P07` |
| `kategori` | `hoaks` / `fakta` / `campuran` / `tidak_terverifikasi` | `campuran` |
| `teks_pesan` | Isi pesan persis seperti yang akan dikirim ke sistem | `Info dari grup: Jakarta ternyata ada di Pulau Sumatra! BMKG itu lembaga pemerintah. Sebarkan!` |
| `putusan_pesan` | `FAKTA` / `HOAKS` / `SEBAGIAN BENAR` / `TIDAK TERVERIFIKASI` | `SEBAGIAN BENAR` |
| `ditulis_ulang` | `ya` bila teks diubah dari aslinya (lihat di bawah), `tidak` bila disalin persis | `ya` |
| `sumber_asli` | URL tempat pesan asli ditemukan (boleh kosong untuk kategori `tidak_terverifikasi`) | `https://turnbackhoax.id/…` |
| `pengumpul` | Nama yang mengumpulkan | (namamu) |
| `pemeriksa_silang` | Siapa yang memeriksa ulang label; isi `Claude (AI)` setelah langkah perbandingan selesai | `Claude (AI)` |
| `catatan` | Bebas | |

## File 2: `klaim.csv` (satu baris = satu klaim faktual)

Tulis **hanya klaim faktual**, yaitu pernyataan yang bisa dibuktikan benar/salah. Opini ("pemerintah tidak becus"),
prediksi ("tahun depan pasti gempa"), dan ajakan ("sebarkan!") **tidak** ditulis.

| Kolom | Isi | Contoh |
|---|---|---|
| `id_pesan` | Sama dengan di `pesan.csv` | `P07` |
| `id_klaim` | P07-1, P07-2, … | `P07-1` |
| `teks_klaim` | Klaim yang berdiri sendiri: ganti kata ganti dengan nama yang dimaksud | `Jakarta terletak di Pulau Sumatra` |
| `putusan_klaim` | `FAKTA` / `HOAKS` / `TIDAK TERVERIFIKASI` | `HOAKS` |
| `sumber_label` | URL yang membuktikan label (artikel cek fakta, situs resmi). Lebih dari satu: pisahkan dengan `;`. Kosong hanya untuk `TIDAK TERVERIFIKASI` | `https://id.wikipedia.org/wiki/Jakarta` |
| `sudah_dicek_media` | `ya` bila klaim ini sudah pernah dibahas artikel cek fakta (Tempo, Kompas, turnbackhoax, AFP, dll.), `tidak` bila belum. Penting: hanya `sumber_label` klaim yang `ya` (dan `sumber_asli` pesannya) yang diblokir saat evaluasi | `tidak` |

---

## Aturan label

**Label klaim**
- `HOAKS`: ada artikel cek fakta atau sumber resmi yang menyatakan klaim itu salah.
- `FAKTA`: ada sumber resmi/terpercaya yang menyatakan klaim itu benar.
- `TIDAK TERVERIFIKASI`: tidak ada sumber publik yang bisa membuktikan benar atau salah.

**Label pesan** harus mengikuti aturan yang dipakai sistem (dicek otomatis oleh validator):

| Klaim di pesan | Label pesan |
|---|---|
| ada HOAKS dan ada FAKTA | `SEBAGIAN BENAR` |
| ada HOAKS, tanpa FAKTA | `HOAKS` |
| semua FAKTA | `FAKTA` |
| selain itu (ada yang tidak terverifikasi, tanpa hoaks) | `TIDAK TERVERIFIKASI` |

---

## Menulis ulang pesan (`ditulis_ulang = ya`)

Kalau pesan disalin persis dari turnbackhoax, sistem bisa "menang" hanya karena menemukan artikel yang sama, padahal
belum memeriksa apa-apa. Karena itu **sebagian pesan ditulis ulang** dalam gaya pesan WA:
- ubah susunan kalimat dan kata-kata, tambahkan salam, emoji, atau ajakan menyebarkan;
- **isi klaimnya tidak boleh berubah** (angka, nama, tempat, waktu tetap sama);
- usul: kira-kira separuh pesan ditulis ulang.

Contoh: *"[SALAH] Moderna Mengakui Vaksin Covid-19 Menyebabkan Kanker"* →
*"Assalamualaikum bapak ibu 🙏 ternyata Moderna sendiri sudah ngaku vaksin covid bikin kanker!! tolong sebarkan ke
keluarga"*

---

## Pemeriksaan silang (pelabelan ulang buta oleh AI)

Karena tidak ada anggota lain, label diperiksa dengan cara ini:

1. Isi `pesan.csv` dan `klaim.csv`, lalu jalankan `cek` sampai tidak ada error.
2. Buat file **tanpa label** untuk pemeriksa:
   ```powershell
   .venv\Scripts\python.exe -m eval.test_set ekspor-buta
   ```
   Hasilnya `label_buta.csv`: teks pesan, teks klaim, dan URL sumber, **tanpa** kolom `putusan_klaim` milikmu.
3. Minta Claude melabeli `label_buta.csv` tanpa membuka `klaim.csv`. Claude membaca sumbernya sendiri, lalu menulis
   `klaim_pemeriksa.csv` (kolom `id_klaim`, `putusan_klaim`, `alasan`).
4. Bandingkan:
   ```powershell
   .venv\Scripts\python.exe -m eval.test_set bandingkan
   ```
   Hasilnya: persentase setuju, Cohen's kappa, dan daftar klaim yang labelnya berbeda beserta alasan pemeriksa.
5. Untuk setiap perbedaan, **kamu** yang memutuskan label akhir di `klaim.csv`. Tulis keputusannya di kolom
   `catatan` pesan terkait (mis. "P07-2: pemeriksa AI menilai TIDAK TERVERIFIKASI; tetap HOAKS karena …").
6. Isi `pemeriksa_silang` dengan `Claude (AI)`.

Batasan yang wajib ditulis di laporan: pemeriksa kedua adalah AI, bukan manusia independen. Angka kesepakatan
menunjukkan konsistensi label terhadap sumbernya, bukan inter-annotator agreement antar-manusia. Pemeriksa AI juga
memakai model yang berbeda dari Gemma (yang menjalankan SAKSI), tetapi tetap sama-sama LLM.

Dalam laporan, pengumpulan dan pelabelan dicatat atas nama orang yang benar-benar mengerjakannya.

---

## Yang tidak boleh

- Mengarang pesan atau label tanpa sumber (kecuali kategori `tidak_terverifikasi`, yang memang dibuat agar tidak bisa
  dicek).
- Memakai pesan dari contoh uji di `ROADMAP.md` (Jakarta–Sumatra, vaksin "kanker turbo", BMKG). Pesan itu sudah
  dipakai saat mengembangkan sistem.
- Mengubah file setelah pembagian dev/held-out dibekukan, kecuali sudah dibicarakan. Bila terpaksa, catat di
  `catatan` apa yang diubah dan kenapa.
