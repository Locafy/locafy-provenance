# Locafy provenance ledger

A public, append-only record of what [www.locafy.com](https://www.locafy.com) published and when, maintained by Locafy Limited (Nasdaq: LCFY).

Every day a GitHub Action ([`provenance.yml`](.github/workflows/provenance.yml)) does five things:

1. Fetches [`/okf/manifest.json`](https://www.locafy.com/okf/manifest.json). This is the SHA-256 and byte size of every file in Locafy's [Open Knowledge Format bundle](https://www.locafy.com/okf/): company facts, leadership, products, customer stories and every published guide, hashed over the exact bytes served.
2. Saves the full manifest in [`manifests/`](manifests/) and records what was added, changed or removed in [`ledger/`](ledger/).
3. Re-fetches a sample of files and checks that they still hash to the manifest values.
4. Timestamps both files with [OpenTimestamps](https://opentimestamps.org/), producing `*.ots` proofs anchored in the Bitcoin blockchain.
5. Submits changed URLs to the [Internet Archive](https://web.archive.org/) and logs the snapshot links in [`archive/`](archive/).

## Verifying a page

```sh
pip install opentimestamps-client
curl -s https://www.locafy.com/okf/company/locafy.md | shasum -a 256   # hash what is served today
grep company/locafy.md ledger/*.jsonl                                    # find when that hash was recorded
ots verify ledger/2026-10-06.jsonl.ots                                   # prove the ledger file existed at its Bitcoin-attested time
```

A fresh proof says "Pending confirmation" for a few hours, until a calendar commits it to a Bitcoin block. The next day's run upgrades it to a complete proof. A file that has been altered fails verification with "File does not match original!".

## What this proves, and what it doesn't

- **Proves:** these exact bytes existed no later than the attested time, and they were served at the recorded URL.
- **Doesn't prove:** that the content is true, who wrote it, or legal ownership.

Hashes are unsigned; anyone can recompute them from the public URLs.
