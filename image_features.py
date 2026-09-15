"""Visual feature extraction for the image-based house price model.

Every uploaded photo is reduced to a fixed-length, deterministic feature vector.
The vector captures three families of evidence a human valuer would also use:

1. Geometry / framing  - aspect ratio, resolution, orientation.
2. Photometric record  - brightness, contrast, saturation, colour balance,
                         warmth. Well lit, well balanced photos correlate with
                         better presented (and better finished) homes.
3. Structure / texture - edge density, gradient energy and per-channel
                         histograms, which carry the rough surface finish,
                         clutter level and layout density of a room or facade.

The per-image vectors are aggregated across the whole upload set (5 to 20
photos). The aggregate is what the price model consumes, and the aggregate also
reports a ``coverage``/``detail`` signal so more photos genuinely produce a
firmer estimate.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

import numpy as np
from PIL import Image, ImageOps

# The order of this list is the contract with the trained model. Never reorder
# or remove an entry without retraining; append only.
FEATURE_NAMES: tuple[str, ...] = (
    # --- photometric record -------------------------------------------------
    'brightness',
    'contrast',
    'saturation',
    'warmth',
    'colour_balance',
    # --- structure / texture ------------------------------------------------
    'edge_density',
    'gradient_energy',
    'detail_score',
    'horizontal_edge_ratio',
    'vertical_edge_ratio',
    # --- geometry -----------------------------------------------------------
    'aspect_ratio',
    'resolution_score',
    # --- per-image hue histogram (structure of the surface palette) ---------
    'hue_r',
    'hue_g',
    'hue_b',
    'hue_low',
    'hue_mid',
    'hue_high',
)

# Aggregate statistics computed over one or more images.
STAT_NAMES: tuple[str, ...] = ('mean', 'std', 'min', 'max')

FEATURE_DIM = len(FEATURE_NAMES) * len(STAT_NAMES)  # 72

MIN_IMAGES = 5
MAX_IMAGES = 20


def _feature_matrix(image: Image.Image) -> dict[str, float]:
    """Reduce one RGB image to the raw (non-aggregated) feature dictionary."""
    rgb = ImageOps.exif_transpose(image).convert('RGB')
    arr = np.asarray(rgb, dtype=np.float32) / 255.0
    h, w = arr.shape[0], arr.shape[1]

    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]

    # --- photometric --------------------------------------------------------
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    brightness = float(luminance.mean())
    contrast = float(luminance.std())

    max_channel = arr.max(axis=2)
    min_channel = arr.min(axis=2)
    # Saturation in the HSV sense, vectorised.
    saturation = float(np.mean(np.where(max_channel > 0,
                                        (max_channel - min_channel) / np.maximum(max_channel, 1e-6),
                                        0.0)))
    warmth = float(r.mean() - b.mean())
    colour_balance = float(1.0 - min(1.0, abs(r.mean() - g.mean()) + abs(g.mean() - b.mean())))

    # --- structure / texture -----------------------------------------------
    # Central differences give a cheap, deterministic gradient field.
    gx = np.zeros_like(luminance)
    gy = np.zeros_like(luminance)
    gx[:, 1:-1] = luminance[:, 2:] - luminance[:, :-2]
    gy[1:-1, :] = luminance[2:, :] - luminance[:-2, :]
    magnitude = np.sqrt(gx * gx + gy * gy)

    edge_density = float(np.mean(magnitude > 0.12))
    gradient_energy = float(np.mean(magnitude))
    detail_score = float(np.percentile(magnitude, 90))
    horizontal_edge_ratio = float(np.mean(np.abs(gx) > np.abs(gy)))
    vertical_edge_ratio = 1.0 - horizontal_edge_ratio

    # --- geometry -----------------------------------------------------------
    aspect_ratio = float(w / h) if h else 1.0
    # 640x480 already carries plenty of signal; cap so 40MP uploads do not dominate.
    resolution_score = float(min(1.0, (w * h) / (640.0 * 480.0)))

    # --- hue distribution ---------------------------------------------------
    # Six coarse buckets: one per dominant channel plus three brightness bands.
    total_pixels = float(h * w)
    hue_r = float(np.sum((r >= g) & (r >= b)) / total_pixels)
    hue_g = float(np.sum((g > r) & (g >= b)) / total_pixels)
    hue_b = float(np.sum((b > r) & (b > g)) / total_pixels)
    hue_low = float(np.mean(luminance < 0.30))
    hue_mid = float(np.mean((luminance >= 0.30) & (luminance < 0.70)))
    hue_high = float(np.mean(luminance >= 0.70))

    return {
        'brightness': brightness,
        'contrast': contrast,
        'saturation': saturation,
        'warmth': warmth,
        'colour_balance': colour_balance,
        'edge_density': edge_density,
        'gradient_energy': gradient_energy,
        'detail_score': detail_score,
        'horizontal_edge_ratio': horizontal_edge_ratio,
        'vertical_edge_ratio': vertical_edge_ratio,
        'aspect_ratio': aspect_ratio,
        'resolution_score': resolution_score,
        'hue_r': hue_r,
        'hue_g': hue_g,
        'hue_b': hue_b,
        'hue_low': hue_low,
        'hue_mid': hue_mid,
        'hue_high': hue_high,
    }


def extract_image_features(image: Image.Image) -> list[float]:
    """Fixed-length vector for a single image, in FEATURE_NAMES order."""
    raw = _feature_matrix(image)
    return [float(raw[name]) for name in FEATURE_NAMES]


def aggregate_features(per_image: Sequence[Sequence[float]]) -> dict[str, Any]:
    """Aggregate 5-20 per-image vectors into the model's input vector.

    Returns the flat 72-length vector together with the diagnostics the API and
    the UI need: how many photos were used, whether that is inside the supported
    range, and a 0-1 ``coverage`` score that rises with the number of photos.
    """
    count = len(per_image)
    if count == 0:
        raise ValueError('At least one image is required.')

    matrix = np.asarray(per_image, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[1] != len(FEATURE_NAMES):
        raise ValueError('Unexpected feature matrix shape.')

    stats = {
        'mean': matrix.mean(axis=0),
        'std': matrix.std(axis=0),
        'min': matrix.min(axis=0),
        'max': matrix.max(axis=0),
    }
    vector: list[float] = []
    for stat in STAT_NAMES:
        vector.extend(float(v) for v in stats[stat])

    # Coverage grows with photo count and saturates at MAX_IMAGES: 5 photos give
    # a usable reading, 20 give the firmest one. Below 5 the estimate is coarse.
    coverage = float(min(1.0, max(0.0, (count - 1) / (MAX_IMAGES - 1))))
    if count < MIN_IMAGES:
        coverage *= count / float(MIN_IMAGES)

    return {
        'vector': vector,
        'count': count,
        'coverage': coverage,
        'within_supported_range': MIN_IMAGES <= count <= MAX_IMAGES,
        'features': {name: float(stats['mean'][i]) for i, name in enumerate(FEATURE_NAMES)},
    }


def aggregate_from_images(images: Iterable[Image.Image]) -> dict[str, Any]:
    """Convenience wrapper: PIL images in, aggregate features out."""
    return aggregate_features([extract_image_features(image) for image in images])


def visual_quality_score(features: dict[str, float]) -> int:
    """Turn the averaged visual features into a 0-100 quality score.

    This is a transparent, bounded index (not a model output): bright, well
    contrasted, balanced and detailed photos with a moderate edge density score
    highest, which is what a careful valuer's photo set looks like.
    """
    brightness = features.get('brightness', 0.5)
    contrast = features.get('contrast', 0.2)
    saturation = features.get('saturation', 0.3)
    balance = features.get('colour_balance', 0.5)
    edges = features.get('edge_density', 0.1)
    detail = features.get('detail_score', 0.2)
    resolution = features.get('resolution_score', 0.5)

    def bell(value: float, centre: float, width: float) -> float:
        return math.exp(-((value - centre) ** 2) / (2 * width * width))

    score = 100.0 * (
        0.22 * bell(brightness, 0.55, 0.22)
        + 0.16 * bell(contrast, 0.20, 0.12)
        + 0.10 * bell(saturation, 0.35, 0.22)
        + 0.10 * min(1.0, max(0.0, balance))
        + 0.14 * bell(edges, 0.16, 0.12)
        + 0.12 * bell(detail, 0.28, 0.18)
        + 0.16 * min(1.0, max(0.0, resolution))
    )
    return int(round(max(0.0, min(100.0, score))))
