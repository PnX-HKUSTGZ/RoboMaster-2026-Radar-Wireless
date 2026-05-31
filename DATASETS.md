# Wireless Field Recordings

The raw wireless recordings are published as GitHub Release assets, not as
regular Git blobs. This keeps the repository lightweight while still making the
original data publicly downloadable.

## Release Assets

Upload these local files to a GitHub Release, for example `dataset-20260530`:

| Recording | Asset |
| --- | --- |
| `20260530_175801` | `20260530_175801_recordings.tar.zst` |
| `20260530_180914` | `20260530_180914_recordings.tar.zst` |
| `20260530_182117` | `20260530_182117_recordings.tar.zst` |

Each archive contains:

```text
<recording_id>/
  wireless/   # original IQ recordings and online decode metadata
  data/       # decoded keys and decoded info-wave tables
```

## Extract

```bash
tar --use-compress-program=zstd -xf 20260530_175801_recordings.tar.zst
```

## Checksums

See `SHA256SUMS.txt`.
