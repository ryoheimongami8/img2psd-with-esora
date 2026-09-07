"""Put a generated image back into the source image's frame.

The image models redraw rather than repaint. GPT Image 2 in particular returns
the same composition at a slightly different size and offset -- measured on a
2048px square: 2.2% scale, 12px shift -- and since the line art and the cutout
are both derived from the source square, they stay registered to each other
while only the generated layer drifts. In a PSD that reads as a ghosted double
edge on every contour.

The drift is a similarity transform, so it can be measured and undone. Matching
runs on gradient magnitude rather than on colour: the source may be a flat line
drawing or a photo while the generated image is fully painted, and the shape of
the edges is the only thing the two reliably share.
"""

import cv2
import numpy as np

#: Scale range searched. The models reframe by a few percent, not by half, and
#: a wider range only invites a confident match against the wrong feature.
SCALE_MIN, SCALE_MAX = 0.90, 1.10
COARSE_STEP = 0.005
FINE_STEP = 0.001
#: Long edge the search runs on. Full resolution buys no accuracy for a
#: three-parameter transform and costs a warp per candidate.
WORK_SIZE = 512
#: Correlation has to improve by this much before the warp is applied. A model
#: that already framed the image correctly must not be nudged by noise.
MIN_GAIN = 0.02


def _gradient(rgb: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    return mag / (float(mag.max()) + 1e-6)


def _work_pair(moving_rgb: np.ndarray, reference_rgb: np.ndarray):
    h, w = reference_rgb.shape[:2]
    scale = WORK_SIZE / max(h, w)
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    ref = cv2.resize(_gradient(reference_rgb), size, interpolation=cv2.INTER_AREA)
    mov = cv2.resize(_gradient(moving_rgb), size, interpolation=cv2.INTER_AREA)
    return ref, mov, scale


def _score(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.sqrt(float((a * a).sum()) * float((b * b).sum())))
    return float((a * b).sum() / denom) if denom > 1e-9 else -1.0


def _similarity(center, scale: float, tx: float, ty: float) -> np.ndarray:
    m = cv2.getRotationMatrix2D(center, 0.0, scale)
    m[0, 2] += tx
    m[1, 2] += ty
    return m


def _search(ref: np.ndarray, mov: np.ndarray, scales):
    """Best (score, scale, tx, ty) over `scales`, in working-resolution pixels."""
    h, w = ref.shape
    center = (w / 2.0, h / 2.0)
    best = None
    for s in scales:
        warped = cv2.warpAffine(mov, _similarity(center, float(s), 0.0, 0.0), (w, h),
                                flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        # Phase correlation resolves the residual shift for this scale in one
        # step, which keeps the search one-dimensional instead of a 3-D grid.
        (dx, dy), _ = cv2.phaseCorrelate(ref.astype(np.float64), warped.astype(np.float64))
        shifted = cv2.warpAffine(warped, np.float32([[1, 0, -dx], [0, 1, -dy]]), (w, h),
                                 flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        score = _score(ref, shifted)
        if best is None or score > best[0]:
            best = (score, float(s), -float(dx), -float(dy))
    return best


def _border_color(rgb: np.ndarray) -> tuple:
    border = np.concatenate([rgb[0, :, :], rgb[-1, :, :], rgb[:, 0, :], rgb[:, -1, :]], axis=0)
    return tuple(int(v) for v in np.median(border, axis=0))


def to_reference(moving_rgb: np.ndarray, reference_rgb: np.ndarray):
    """Warp ``moving_rgb`` onto ``reference_rgb``. -> ``(aligned_rgb, info)``.

    ``info`` carries the measured transform and the correlation before and
    after, so the caller can log what was corrected. The image comes back
    untouched when the match does not improve: a wrong warp is worse than the
    small offset it was trying to remove.
    """
    if moving_rgb.shape[:2] != reference_rgb.shape[:2]:
        moving_rgb = cv2.resize(moving_rgb, (reference_rgb.shape[1], reference_rgb.shape[0]),
                                interpolation=cv2.INTER_LANCZOS4)

    ref, mov, work_scale = _work_pair(moving_rgb, reference_rgb)
    before = _score(ref, mov)

    coarse = _search(ref, mov, np.arange(SCALE_MIN, SCALE_MAX + 1e-9, COARSE_STEP))
    lo = max(SCALE_MIN, coarse[1] - COARSE_STEP)
    hi = min(SCALE_MAX, coarse[1] + COARSE_STEP)
    best = _search(ref, mov, np.arange(lo, hi + 1e-9, FINE_STEP))
    if coarse[0] > best[0]:
        best = coarse

    score, scale, tx, ty = best
    info = {"scale": scale, "tx": tx / work_scale, "ty": ty / work_scale,
            "corr_before": before, "corr_after": score, "applied": False}
    if score < before + MIN_GAIN:
        return moving_rgb, info

    h, w = reference_rgb.shape[:2]
    m = _similarity((w / 2.0, h / 2.0), scale, info["tx"], info["ty"])
    aligned = cv2.warpAffine(moving_rgb, m, (w, h), flags=cv2.INTER_LANCZOS4,
                             borderMode=cv2.BORDER_CONSTANT,
                             borderValue=_border_color(moving_rgb))
    info["applied"] = True
    return aligned, info
