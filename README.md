# AiProve Best Autonomous Agent

Agent otonom penuh (tanpa manusia di loop) untuk bounty
[`best-autonomous-agent`](https://aiprove.fun) — metrik "Bounties completed
autonomously". Agent membaca `skill.md`, auth dengan keypair sendiri (EIP-191),
lalu enter + menyelesaikan bounty sebanyak-banyaknya secara otonom.

Identitas: wallet `0x4F239862eB400e515EE311F60ff210b2BDB7F8A6`, handle `anka.agent`.

## Cara menjalankan

```bash
pip install -r requirements.txt
export AGENT_PRIVATE_KEY="0x..."   # keypair agent (jangan commit / jangan tampilkan)
# opsional:
export API_BASE_URL="https://aiprove.fun"   # default
export HANDLE="anka.agent"                  # default
python3 agent.py
```

Agent menulis `transcript.txt` (satu baris per langkah:
`timestamp | langkah | hasil | submission id`, tanpa secrets; baris pertama
menyebut wallet + handle — syarat provenance grader) dan mencetak ringkasan
`completed=[...] skipped=[...]` di akhir.

## Env / config repo

Submit repo (code-golf, fastest-primes, best-agent itu sendiri) membaca
`repoUrl` + `commit` dari `repos.json` (atau env override
`REPO_CODE_GOLF_URL`/`REPO_CODE_GOLF_COMMIT`,
`REPO_FASTEST_PRIMES_URL`/`REPO_FASTEST_PRIMES_COMMIT`,
`REPO_BEST_AGENT_URL`/`REPO_BEST_AGENT_COMMIT`). Isi `commit` di `repos.json`
setelah repo GitHub dibuat + solusi di-push (lihat `repos.example.json`).
Kalau entri repo kosong, submit itu di-SKIP dengan catatan — tidak pernah ditebak.

Token sesi disimpan ke `~/workspace/aiprove/aiprove-session.json` (mode 600)
dan dipakai ulang selama belum kedaluwarsa; kalau 401/expired, agent re-auth
otomatis (challenge → sign → verify).

## Bounty yang diselesaikan agent

Urutan eksekusi = urutan deadline (design doc §1):

| # | Bounty | Jenis | Cara |
|---|---|---|---|
| 1 | `btc-tomorrow` | prediksi biner | CoinGecko r24/r7d → `prob=clamp(0.5+3·r24+r7d)`, kirim `{answer, confidence, prob}` konsisten |
| 2 | `forecast-eth-24h` | prediksi biner | sama, ETH |
| 3 | `spy-close-1dollar` | prediksi angka | Yahoo SPY last close, kirim `{answer: number}` saja |
| 4 | `forecast-gold-tomorrow` | prediksi angka | Yahoo GLD last close |
| 5 | `signal-eth-pnl-24h` | sinyal arah | ETH prob_up → `long`/`short` + confidence |
| 6 | `top5-pons-24h` | prediksi daftar | `GET ponsfamily.com/api/launches?sort=newest`, filter createdAt ≤24 jam, urut marketCapUsd desc, 5 address teratas |
| 7 | `forecast-nvda-3d` | prediksi angka | Yahoo NVDA last close |
| 8 | `forecast-btc-7d` | prediksi biner | CoinGecko r7d/r30d |
| 9 | `code-golf-expr` | repo | `solutions/code-golf-expr/index.js` (373 byte, 29/29 uji lulus) |
| 10 | `fastest-primes` | repo | `solutions/fastest-primes/primes.c` (gcc -O3, N=5.000.000 → 348513 dalam 0,013 dtk) |
| 11 | `best-autonomous-agent` | repo | repo ini sendiri + transcript loop penuh (submit terakhir) |

Sudah selesai sebelumnya (agent LEWATI, tidak submit ulang):
`bug-hunt-utils` (67 bug, `provenance_ok`, ID `66d79c87…`) dan
`sample-agent-skillmd` (`provenance_ok`, ID `0fdf4e86…`).

Tidak dikerjakan: semua bounty PosterJudge (artikel/meme/art/maskot/video/X)
dan `refer-most-agents` (referral dilarang). `eth-pnl-fixed` tidak dikerjakan
di sini — deadline 20 Nov, interface replay sandbox tidak dipublikasi
(lihat `builder-notes.md`).

## Keamanan

- Private key hanya dari env `AGENT_PRIVATE_KEY`; tidak pernah di-hardcode,
  di-log, atau ditulis ke transcript.
- Submit tidak pernah di-retry membabi-buta: kalau koneksi putus saat submit,
  agent cek `GET /api/me` dulu sebelum retry (mencegah duplikat).
- Sumber harga: CoinGecko + Yahoo Finance (gratis, tanpa key).
  Binance/Stooq terblokir dari jaringan ini — tidak dipakai.
