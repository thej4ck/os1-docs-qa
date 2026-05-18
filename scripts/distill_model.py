#!/usr/bin/env python3
"""Distill a compact static (model2vec) embedding model — LOCAL/BUILD ONLY.

Run this on a dev machine BEFORE pushing. The resulting model directory is
committed to the repo and shipped to Railway as-is; Railway never runs this
script and never installs torch.

The full subword tokenizer of the source model is kept (vocabulary=None) so
there are no OOV tokens — maximum robustness on badly-phrased queries. Size is
controlled via --pca-dims and --quantize, not by capping the vocabulary.

Usage:
    pip install "model2vec[distill]"
    python scripts/distill_model.py [--out searchdata/static_model]
                                    [--source SENTENCE_TRANSFORMER]
                                    [--pca-dims 256]
                                    [--quantize int8|float16|float32]
                                    [--device cpu|xpu|cuda]
"""

import argparse
import sys
from pathlib import Path

# sentence-transformers multilingual encoder with good Italian and NO
# query/passage prefix requirement.
DEFAULT_SOURCE = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def main():
    parser = argparse.ArgumentParser(description="Distill compact model2vec model")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "searchdata" / "static_model"),
        help="Output directory for the distilled model",
    )
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        help="Source sentence-transformer model to distill from")
    parser.add_argument("--pca-dims", type=int, default=256,
                        help="Output embedding dimensionality (quality/size trade-off)")
    parser.add_argument("--quantize", default="int8",
                        choices=["int8", "float16", "float32"],
                        help="Weight quantization: int8 ~64MB, float16 ~128MB")
    parser.add_argument("--device", default=None,
                        help="Torch device for distillation (cpu/xpu/cuda). "
                             "Optional — distill is light, CPU is fine.")
    args = parser.parse_args()

    try:
        from model2vec.distill import distill
    except ImportError:
        print(
            "ERROR: model2vec[distill] not installed.\n"
            '       Run: pip install "model2vec[distill]"',
            file=sys.stderr,
        )
        sys.exit(1)

    out_dir = Path(args.out)
    out_dir.parent.mkdir(parents=True, exist_ok=True)

    print(f"Source model:   {args.source}")
    print(f"Output dir:     {out_dir}")
    print(f"PCA dims:       {args.pca_dims}")
    print(f"Quantize to:    {args.quantize}")
    print(f"Device:         {args.device or 'auto'}")
    print(f"Vocabulary:     full subword (no OOV)")
    print("\nDistilling (one-time, ~minutes; downloads the source model once)...")

    distill_kwargs = dict(
        model_name=args.source,
        vocabulary=None,          # keep full subword tokenizer → no OOV
        pca_dims=args.pca_dims,
        quantize_to=args.quantize,
    )
    if args.device:
        distill_kwargs["device"] = args.device

    try:
        model = distill(**distill_kwargs)
    except TypeError:
        # Older/newer model2vec without quantize_to in distill(): quantize on save.
        distill_kwargs.pop("quantize_to", None)
        distill_kwargs.pop("device", None)
        model = distill(**distill_kwargs)

    model.save_pretrained(str(out_dir))

    # Report on-disk size
    total = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file())
    print(f"\nSaved. On-disk size: {total / 1024 / 1024:.1f} MB")
    print("Done. Commit this directory:  git add", out_dir)


if __name__ == "__main__":
    main()
