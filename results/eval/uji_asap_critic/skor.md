| Metrik | uji_asap_critic |
|---|---|
| Critic | ya |
| Jumlah pesan | 2 |
| Sidik jari kode | fde3dd75d4327060 |
| Akurasi pesan | 50.0% (1/2) |
| Macro-F1 pesan | 0.333 |
| Akurasi klaim (ujung ke ujung) | 25.0% (1/4) |
| Macro-F1 klaim | 0.333 |
| Akurasi klaim yang cocok | 100.0% (1/1) |
| — sudah dicek media | – |
| — belum dicek media | 25.0% (1/4) |
| Dekomposisi: recall | 25.0% (1/4) |
| Dekomposisi: presisi | 100.0% (1/1) |
| Klaim emas dianggap bukan faktual | 0 |
| Sitasi valid | – |
| Halusinasi (TV → FAKTA/HOAKS) | 0.0% (0/1) |
| Over-abstain (FAKTA/HOAKS → TV) | – |
| Critic: hasil Worker ditolak | – |
| Critic: hasil revisi | – |
| Rata-rata token total / pesan | 3673.0 |
| Rata-rata token orchestrator / pesan | 234.5 |
| Rata-rata token worker / pesan | 3438.5 |
| Rata-rata token critic / pesan | 0.0 |
| Rata-rata waktu detik / pesan | 387.1 |
| Rata-rata pesan a2a / pesan | 5.0 |
| Rata-rata retry llm / pesan | 0.0 |
| Rata-rata tunggu backoff detik / pesan | 0.0 |

**uji_asap_critic: matriks pesan (emas -> sistem)**
- SEBAGIAN BENAR -> TIDAK TERVERIFIKASI: 1
- TIDAK TERVERIFIKASI -> TIDAK TERVERIFIKASI: 1

**uji_asap_critic: matriks klaim (emas -> sistem)**
- FAKTA -> TIDAK DITEMUKAN: 2
- HOAKS -> TIDAK DITEMUKAN: 1
- TIDAK TERVERIFIKASI -> TIDAK TERVERIFIKASI: 1
