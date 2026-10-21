# Sample data

Only synthetic data is used. Nothing in this directory is a real image, and generated files are not
committed (`sample-data/generated/` is git-ignored).

```
uv run python scripts/gen_images.py            # -> sample-data/generated/ + manifest.json
```

| File pattern | What it is | Expected outcome |
|---|---|---|
| `good-NNN.jpg/png` | random shapes on a gradient (deterministic per seed) | `analyzed` |
| `bad-truncated_jpeg.jpg` | first bytes of a JPEG | `rejected:corrupt` |
| `bad-text_as_jpeg.jpg` | text file declared as JPEG | `rejected:unsupported_type` |
| `bad-png_declared_jpeg.jpg` | PNG bytes declared as JPEG | `rejected:type_mismatch` |
| `bad-bomb_png.png` | tiny PNG declaring a huge canvas | `rejected:decompression_bomb` |
| `bad-tiny_png.png` | 8x8 image | `rejected:dimensions_out_of_range` |
| `bad-empty.jpg` | empty file (local mode only; the presigned POST refuses empty bodies) | `rejected:empty` |

The same generator (`src/imaging/synthetic.py`) feeds the tests, the offline demo, the cloud end-to-end
checks and the load test. The model's ImageNet class IDs are meaningless for these shapes: the project
demonstrates the architecture, not model accuracy.
