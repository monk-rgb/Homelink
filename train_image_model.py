"""Train the image-based house price model.

Run with:

    python train_image_model.py

Inputs
------
data.csv          - the structured listing records. The price distribution here
                    defines the market band the visual model must stay inside.
image_features.py - the feature pipeline. Training and inference share it, so
                    the model can never see features the app cannot reproduce.

Why synthetic photos?
---------------------
The repository ships no labelled photo-to-price dataset. Rather than invent
fictional "real" numbers, this script *renders* property photos procedurally and
records the exact visual premium each render was built with. The renders are
composed from the same cues the feature pipeline measures (lighting, contrast,
surface finish, clutter, colour palette, framing), so the model learns a genuine
mapping from measurable image statistics to a bounded price multiplier.

That keeps the model honest: it does not claim to know Nigerian prices from
pixels. It predicts a *visual adjustment* applied to the structured valuation,
which is exactly how the app uses it.

Outputs
-------
model_image.joblib  - the fitted regressor plus its metadata.
image_metrics.json  - cross-validated quality metrics.
"""

from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import KFold, cross_val_predict

from image_features import (
    MAX_IMAGES,
    MIN_IMAGES,
    aggregate_features,
    extract_image_features,
)

BASE = Path(__file__).resolve().parent
SEED = 20260101
RENDER_SIZE = (224, 168)
SAMPLES = 700

# Surface palettes keyed by finishing quality. The colour balance and hue
# distribution of a render are driven by these numbers.
PALETTES = {
    'poor': ((126, 112, 96), (98, 92, 84), (74, 70, 66)),
    'fair': ((168, 156, 138), (140, 132, 120), (110, 104, 96)),
    'good': ((206, 198, 184), (176, 168, 154), (146, 140, 130)),
    'premium': ((232, 226, 214), (206, 200, 188), (176, 172, 164)),
}

QUALITY_PREMIUM = {
    'poor': -0.085,
    'fair': -0.035,
    'good': 0.010,
    'premium': 0.075,
}


def _render_photo(rng: np.random.Generator, quality: str, lighting: float,
                  clutter: float, framing: float) -> Image.Image:
    """Render one synthetic property photo with a known visual character.

    lighting high  -> brighter, higher contrast, more high-luminance pixels
    clutter high   -> more edges, higher gradient energy, denser detail
    framing high   -> better resolution/aspect framing (square-on shot)
    """
    base, mid, dark = PALETTES[quality]
    width, height = RENDER_SIZE
    # Framing controls how much of the frame the building occupies.
    inset = int((1.0 - framing) * 24)
    img = Image.new('RGB', (width, height), dark)
    draw = ImageDraw.Draw(img)

    # Sky / ambient band: brighter with better lighting.
    sky = tuple(int(min(255, c + lighting * 70)) for c in mid)
    draw.rectangle([0, 0, width, int(height * (0.42 - 0.12 * lighting))], fill=sky)

    # Facade: base colour lifted by lighting, warmed slightly with quality.
    facade = tuple(int(min(255, c * (0.72 + 0.42 * lighting))) for c in base)
    draw.rectangle([inset, int(height * 0.30), width - inset, height - inset], fill=facade)

    # Windows: dark voids, more of them as clutter rises (more visible detail).
    window_count = 2 + int(clutter * 6)
    step = max(12, (width - 2 * inset) // (window_count + 1))
    for i in range(window_count):
        x = inset + step * (i + 1)
        y = int(height * (0.38 + 0.30 * (i % 2)))
        w = max(8, int(step * 0.45))
        h = max(8, int(step * 0.55 * (0.8 + 0.6 * lighting)))
        draw.rectangle([x, y, x + w, y + h], fill=dark)

    # Structure lines: the more clutter, the more surface break-up (edges).
    lines = int(2 + clutter * 22)
    for _ in range(lines):
        x0 = int(rng.integers(0, width))
        y0 = int(rng.integers(int(height * 0.30), height))
        length = int(rng.integers(8, 46))
        shade = tuple(int(max(0, min(255, c + rng.integers(-26, 27)))) for c in facade)
        draw.line([x0, y0, x0 + length, y0 + int(rng.integers(-3, 4))], fill=shade, width=1)

    arr = np.asarray(img, dtype=np.float32) / 255.0
    # Lighting raises overall luminance and contrast multiplicatively.
    arr = np.clip(arr * (0.68 + 0.52 * lighting), 0.0, 1.0)
    arr = np.clip((arr - 0.5) * (0.80 + 0.55 * lighting) + 0.5, 0.0, 1.0)
    img = Image.fromarray((arr * 255).astype(np.uint8))

    # A modest blur for low quality finish (flat, cheap surfaces read softer).
    if quality == 'poor':
        img = img.filter(ImageFilter.GaussianBlur(0.6))
    elif quality == 'fair':
        img = img.filter(ImageFilter.GaussianBlur(0.3))

    # Real uploads are never clean renders: sensor noise, uneven exposure, a
    # cropped frame and JPEG artefacts all perturb the measured statistics. The
    # model must stay robust to that, or it would only work on synthetic input.
    noisy = np.asarray(img, dtype=np.float32) / 255.0
    noisy += rng.normal(0.0, 0.055, noisy.shape).astype(np.float32)
    # Uneven exposure: a smooth vignette-style falloff across the frame.
    yy, xx = np.mgrid[0:noisy.shape[0], 0:noisy.shape[1]].astype(np.float32)
    falloff = 1.0 - 0.16 * (((xx / max(1, noisy.shape[1] - 1)) - 0.5) ** 2
                            + ((yy / max(1, noisy.shape[0] - 1)) - 0.5) ** 2)
    noisy *= falloff[..., None]
    # JPEG-style blockiness at moderate quality.
    block = 8
    for channel in range(3):
        plane = noisy[..., channel]
        coarse = plane[::block, ::block]
        upscaled = np.repeat(np.repeat(coarse, block, axis=0), block, axis=1)
        blended = 0.72 * plane + 0.28 * upscaled[:plane.shape[0], :plane.shape[1]]
        noisy[..., channel] = blended
    img = Image.fromarray((np.clip(noisy, 0.0, 1.0) * 255).astype(np.uint8))

    # Framing is not always square-on: a tilted or off-centre shot reads lower.
    if framing < 0.45:
        img = img.crop((int((1.0 - framing) * 18), 0,
                        width - int((1.0 - framing) * 10), height)).resize(RENDER_SIZE)
    return img


def _render_property(rng: np.random.Generator) -> tuple[list[Image.Image], float]:
    """Render a full upload set (5-20 photos) for one property."""
    quality = str(rng.choice(['poor', 'fair', 'good', 'premium'],
                             p=[0.12, 0.28, 0.38, 0.22]))
    # Lighting and clutter are correlated with quality in real listings: a
    # premium home is usually shot well and presented tidily.
    quality_index = ['poor', 'fair', 'good', 'premium'].index(quality)
    lighting = float(np.clip(rng.normal(0.35 + 0.15 * quality_index, 0.16), 0.05, 1.0))
    clutter = float(np.clip(rng.normal(0.60 - 0.10 * quality_index, 0.18), 0.02, 1.0))
    framing = float(np.clip(rng.normal(0.62 + 0.08 * quality_index, 0.15), 0.15, 1.0))

    count = int(rng.integers(MIN_IMAGES, MAX_IMAGES + 1))

    # More photos means a more thorough inspection, so the observed character
    # converges on the property's true character and the estimate firms up.
    certainty = 0.62 + 0.38 * (count - MIN_IMAGES) / max(1, (MAX_IMAGES - MIN_IMAGES))
    observed_lighting = lighting * certainty + 0.5 * (1 - certainty)
    observed_clutter = clutter * certainty + 0.5 * (1 - certainty)
    observed_framing = framing * certainty + 0.5 * (1 - certainty)

    photos = [
        _render_photo(rng, quality, float(np.clip(observed_lighting + rng.normal(0, 0.05), 0.02, 1.0)),
                      float(np.clip(observed_clutter + rng.normal(0, 0.05), 0.02, 1.0)),
                      float(np.clip(observed_framing + rng.normal(0, 0.04), 0.10, 1.0)))
        for _ in range(count)
    ]

    # Ground truth: the quality premium, damped by how much evidence the photo
    # set actually carries. A shaky 5-photo set justifies a smaller move than a
    # thorough 20-photo set.
    premium = QUALITY_PREMIUM[quality] * (0.55 + 0.45 * certainty)
    premium += (observed_lighting - 0.5) * 0.030
    premium -= (observed_clutter - 0.5) * 0.012
    premium = float(np.clip(premium, -0.10, 0.10))
    return photos, premium


def build_dataset(samples: int = SAMPLES, seed: int = SEED) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    rows: list[list[float]] = []
    targets: list[float] = []
    counts: list[int] = []
    for _ in range(samples):
        photos, premium = _render_property(rng)
        agg = aggregate_features([extract_image_features(p) for p in photos])
        rows.append(agg['vector'])
        targets.append(premium)
        counts.append(agg['count'])
        # The photo count itself is a legitimate predictor (more evidence, more
        # confident adjustment), so it is appended after the aggregate vector.
        rows[-1].append(agg['coverage'])
    return np.asarray(rows, dtype=np.float64), np.asarray(targets, dtype=np.float64), np.asarray(counts)


def main() -> None:
    X, y, _ = build_dataset()
    feature_names = [f'aggregate_{i}' for i in range(X.shape[1] - 1)] + ['coverage']

    model = GradientBoostingRegressor(
        n_estimators=420,
        learning_rate=0.05,
        max_depth=3,
        min_samples_leaf=12,
        subsample=0.9,
        random_state=SEED,
    )

    cv = KFold(n_splits=5, shuffle=True, random_state=SEED)
    cv_pred = cross_val_predict(model, X, y, cv=cv)
    metrics = {
        'r2': float(r2_score(y, cv_pred)),
        'mae': float(mean_absolute_error(y, cv_pred)),
        'samples': int(X.shape[0]),
        'feature_dim': int(X.shape[1]),
        'min_images': MIN_IMAGES,
        'max_images': MAX_IMAGES,
        'target': 'visual_price_multiplier_delta',
    }

    model.fit(X, y)
    in_sample = model.predict(X)
    metrics['in_sample_r2'] = float(r2_score(y, in_sample))
    metrics['in_sample_mae'] = float(mean_absolute_error(y, in_sample))

    joblib.dump({'model': model, 'feature_names': feature_names, 'metrics': metrics},
                BASE / 'model_image.joblib')
    (BASE / 'image_metrics.json').write_text(json.dumps(metrics, indent=2), encoding='utf-8')

    print(json.dumps(metrics, indent=2))


if __name__ == '__main__':
    main()
