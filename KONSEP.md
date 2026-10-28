# Konsep SAKSI

**SAKSI**: *Sistem Agen Kolaboratif untuk Pemeriksaan Informasi*. Pemeriksa hoaks berbasis multi-agent untuk mata
kuliah PKI. Acuan teori: H. Roitman, *The Hitchhiker's Guide to Agentic AI*.

Detail rencana ada di `RENCANA_SAKSI.txt`; status dan hasil tiap fase ada di `ROADMAP.md`.

---

## Masalah

Pesan berantai (WhatsApp, media sosial) sering mencampur fakta, hoaks, opini, dan ajakan dalam satu pesan. Untuk
memeriksanya, orang harus memecah pesan, mencari sumber untuk setiap pernyataan, lalu menilai apakah sumbernya
benar-benar mendukung. Pekerjaan ini lama dan jarang dilakukan.

## Apa yang dilakukan SAKSI

Pengguna menempelkan sebuah pesan. SAKSI lalu:

1. memecah pesan menjadi klaim yang bisa diperiksa,
2. memeriksa setiap klaim faktual secara paralel dengan mencari bukti dari sumber eksternal,
3. meminta agent kritikus terpisah memverifikasi kutipan dan menilai buktinya,
4. memberi putusan per klaim dan untuk pesan secara keseluruhan:
   **FAKTA / HOAKS / SEBAGIAN BENAR / TIDAK TERVERIFIKASI**, beserta kutipan dan tautan sumber.

**TIDAK TERVERIFIKASI adalah jawaban yang sah.** Sistem lebih baik mengaku tidak tahu daripada memberi putusan tanpa
bukti.

---

## Arsitektur

```
                 Pengguna (CLI / UI Streamlit)
                            │ A2A
                            ▼
                    ┌───────────────┐
                    │ ORCHESTRATOR  │  ekstraksi klaim, delegasi paralel, agregasi putusan
                    └───────────────┘
                 A2A │             │ A2A
                     ▼             ▼
             ┌────────────┐   ┌────────────┐
             │   WORKER   │   │   CRITIC   │
             │ (per klaim)│   │            │
             └────────────┘   └────────────┘
               MCP │                 │ MCP
                   ▼                 ▼
   cekfakta · wiki · berita · fetch   fetch
```

Tiga agent, masing-masing proses terpisah yang berkomunikasi lewat **A2A** (Bab 25). Tool eksternal disediakan
lewat **MCP** (Bab 23).

| Agent | Tugas | Tool |
|---|---|---|
| Orchestrator | Memecah pesan menjadi klaim (LLM), mengirim klaim faktual ke Worker secara paralel, meminta penilaian Critic, menyusun putusan dengan aturan di kode | – (tanpa API eksternal) |
| Worker | Memeriksa satu klaim dengan loop ReAct: berpikir → panggil tool → baca hasil → ulangi → jawab | semua server MCP |
| Critic | Memverifikasi bahwa setiap kutipan benar-benar ada di sumbernya, lalu menilai apakah bukti cukup: TERIMA / TOLAK | mcp-fetch saja |

| Server MCP | Sumber |
|---|---|
| mcp-cekfakta | Google Fact Check Tools API (artikel cek fakta yang sudah terbit) |
| mcp-wiki | Wikipedia id/en |
| mcp-berita | GNews (berita 30 hari terakhir) |
| mcp-fetch | Isi halaman web + cek kutipan |

LLM: `gemma-4-26b-a4b-it` lewat Gemini API (gratis).

### Kenapa multi-agent

Uji kewajaran: *kalau agent ini diganti fungsi biasa, apa yang hilang?*

- **Worker**: hilang pencarian berulang (cari, baca, ganti kata kunci, cari lagi).
- **Critic**: hilang pemeriksaan independen dan putaran revisi. Agent yang menilai hasilnya sendiri cenderung bias
  (Bab 14.7).
- **Orchestrator**: hilang dekomposisi pesan, delegasi paralel, dan agregasi.

---

## Alur satu pesan

1. Orchestrator mengekstrak klaim: `{teks, jenis: fakta/opini/prediksi}`. Opini dan prediksi tidak diperiksa.
2. Setiap klaim faktual dikirim ke Worker sebagai task A2A, semuanya bersamaan.
3. Worker mengembalikan `{sikap: MENDUKUNG/MEMBANTAH/TIDAK_CUKUP, bukti: [{url, kutipan}], keyakinan, ringkasan}`.
4. Critic memverifikasi kutipan lalu menjawab TERIMA atau TOLAK.
5. Bila TOLAK, Worker mengulang dengan umpan balik Critic, maksimal 2 kali. Setelah itu klaim menjadi
   TIDAK TERVERIFIKASI.
6. Orchestrator menyusun putusan pesan dengan aturan tetap:

| # | Kondisi (dari klaim faktual) | Putusan pesan |
|---|---|---|
| 1 | tidak ada klaim faktual | TIDAK TERVERIFIKASI |
| 2 | ada HOAKS dan ada FAKTA | SEBAGIAN BENAR |
| 3 | ada HOAKS | HOAKS |
| 4 | semua FAKTA | FAKTA |
| 5 | selain itu | TIDAK TERVERIFIKASI |

Setiap langkah dicatat di audit log dengan `correlation_id` yang sama, sehingga satu pesan bisa ditelusuri melintasi
semua agent.

---

## Prinsip desain

- **LLM hanya untuk bagian yang butuh penilaian**: memecah pesan, mencari dan menilai bukti. Alur kerja dan
  agregasi putusan ditulis sebagai kode, supaya mudah diaudit.
- **Bukti harus bisa dibuktikan.** Kutipan diperiksa oleh kode, bukan oleh LLM. Kutipan yang tidak ditemukan di
  sumbernya langsung ditolak.
- **Gagal dengan aman.** Agent yang error, timeout, atau mati tidak menjatuhkan sistem; klaimnya menjadi TIDAK
  TERVERIFIKASI dengan catatan penyebab.
- **Hak akses minimum.** Setiap agent hanya mendapat tool yang ia butuhkan.
- **Isi web adalah data, bukan instruksi** (pertahanan prompt injection).

---

## Evaluasi (rencana)

- Test set 40 pesan (15 hoaks, 10 fakta, 10 campuran, 5 tidak terverifikasi), dilabeli per klaim, dibagi 10 dev
  dan 30 held-out. Dikumpulkan dan dilabeli sendiri, lalu diperiksa silang lewat pelabelan ulang buta oleh AI.
- Metrik: akurasi dan macro-F1 putusan, akurasi dekomposisi klaim, validitas sitasi, tingkat halusinasi,
  over-abstain, statistik Critic, serta biaya (token dan waktu per agent).
- Pembanding: sistem yang sama tanpa Critic (`--tanpa-critic`), untuk menunjukkan manfaat Critic.
- Kontrol kebocoran: artikel cek fakta yang dipakai sebagai sumber label diblokir saat evaluasi, supaya sistem tidak
  sekadar menemukan kunci jawabannya. Sumber resmi untuk klaim FAKTA tidak diblokir. Hasil dilaporkan terpisah untuk
  klaim yang sudah dan belum pernah dicek media.

---

## Batasan yang diketahui

- Kuota Gemma gratis membuat pemeriksaan satu pesan butuh beberapa menit. Paralelisme ada di desain, tetapi
  percepatan nyatanya terbatas.
- Berita hanya 30 hari ke belakang (paket gratis GNews).
- Sebagian situs (mis. Tempo, AFP) menolak bot, sehingga kutipannya diverifikasi terhadap hasil API cek fakta, bukan
  halamannya.
- Hoaks lokal yang belum pernah diberitakan atau dicek media kemungkinan besar berakhir TIDAK TERVERIFIKASI.
