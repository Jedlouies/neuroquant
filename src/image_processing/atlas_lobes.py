"""
atlas_lobes.py

Anatomically accurate brain lobe segmentation using a standard neuroanatomical
atlas, registered onto the patient's own scan -- instead of hand-tuned
coordinate thresholds (y_norm >= 0.50, z_norm >= 0.38, etc.), which only ever
produce flat, plane-cut regions.

The Talairach "lobe" level atlas (bundled via nilearn) already has real,
irregular lobe boundaries drawn on a standard template brain (MNI152 space).
We register that template onto the patient's scan, then warp the atlas
labels along with it, so the same irregular boundaries land correctly on
the patient's own anatomy.

Requires:
    pip install nilearn antspyx nibabel --break-system-packages

Usage:
    from atlas_lobes import get_lobe_masks

    lobe_masks = get_lobe_masks(raw_volume, spacing=(sz, sy, sx))
    if lobe_masks is not None:
        frontal_bin = lobe_masks["frontal"]
        parietal_bin = lobe_masks["parietal"]
        # etc.
    else:
        # libraries missing, or registration failed -- fall back to the
        # existing coordinate-threshold method.
        ...

Notes:
    - Registration (SyN) is the expensive step, typically 30s-2min per
      volume on CPU. Pass cache_transform_path to reuse a computed
      transform for the same patient across app runs.
    - The cerebellum and spinal cord are NOT reliably covered by cortical
      lobe atlases -- keep your existing coordinate-based logic for those
      two regions and only replace frontal/parietal/temporal/occipital.
    - nilearn's exact label spelling can vary slightly by version. Run
      `python atlas_lobes.py` standalone once to print the label list for
      your installed version and adjust _LOBE_NAME_MAP if needed.
"""

import os
import numpy as np
from scipy import ndimage

_ATLAS_CACHE = {}  # keeps fetched atlas + template in memory across calls

# Talairach "lobe" level label names -> this app's internal lobe keys.
# Print `label_names` (see __main__ below) to confirm exact spelling for
# your nilearn version before relying on this in production.
_LOBE_NAME_MAP = {
    "Frontal Lobe": "frontal",
    "Parietal Lobe": "parietal",
    "Temporal Lobe": "temporal",
    "Occipital Lobe": "occipital",
    # "Limbic Lobe" / "Sub-lobar" / cerebellar "Anterior/Posterior Lobe"
    # also exist in this atlas but aren't mapped -- add them here if you
    # want the atlas driving those regions too.
}


def _load_atlas_and_template():
    """Fetch (once, cached) the Talairach lobe atlas and its MNI152 template."""
    if "atlas" in _ATLAS_CACHE:
        return _ATLAS_CACHE["atlas"], _ATLAS_CACHE["template"], _ATLAS_CACHE["labels"]

    from nilearn import datasets

    talairach = datasets.fetch_atlas_talairach("lobe")
    template = datasets.load_mni152_template(resolution=2)

    _ATLAS_CACHE["atlas"] = talairach.maps            # label volume, atlas space
    _ATLAS_CACHE["template"] = template               # the space the atlas is defined on
    _ATLAS_CACHE["labels"] = list(talairach.labels)    # index -> region name

    return _ATLAS_CACHE["atlas"], _ATLAS_CACHE["template"], _ATLAS_CACHE["labels"]


def _to_ants_image(image_like, spacing=None):
    """Convert a nibabel image, file path, or numpy array into an ANTsImage."""
    import ants

    if isinstance(image_like, str):
        return ants.image_read(image_like)

    if hasattr(image_like, "get_fdata"):  # nibabel Nifti1Image
        return ants.from_nibabel(image_like)

    if isinstance(image_like, np.ndarray):
        img = ants.from_numpy(image_like.astype(np.float32))
        if spacing is not None:
            img.set_spacing(tuple(float(s) for s in spacing))
        return img

    raise TypeError(f"Unsupported image type: {type(image_like)}")


def get_lobe_masks(raw_volume: np.ndarray, spacing=(1.0, 1.0, 1.0), cache_transform_path=None):
    """
    Register the Talairach lobe atlas onto `raw_volume` and return a dict of
    boolean masks (frontal/parietal/temporal/occipital), each the same shape
    as raw_volume, with real irregular lobe boundaries.

    Never raises -- returns None if antspyx/nilearn aren't installed or if
    registration fails, so callers can fall back to a simpler method.
    """
    try:
        import ants  # noqa: F401  (import check only)
    except ImportError:
        return None

    try:
        atlas_labels_img, template_img, label_names = _load_atlas_and_template()

        moving = _to_ants_image(template_img)          # template space (source)
        fixed = _to_ants_image(raw_volume, spacing)     # patient space (target)
        atlas_ants = _to_ants_image(atlas_labels_img)

        import ants

        transform_list = None
        if cache_transform_path and os.path.exists(cache_transform_path + "1Warp.nii.gz"):
            transform_list = [
                cache_transform_path + "1Warp.nii.gz",
                cache_transform_path + "0GenericAffine.mat",
            ]
        else:
            reg = ants.registration(
                fixed=fixed,
                moving=moving,
                type_of_transform="SyN",
                outprefix=cache_transform_path or "",
            )
            transform_list = reg["fwdtransforms"]

        # Nearest-neighbor only -- label IDs must never be interpolated.
        warped_labels = ants.apply_transforms(
            fixed=fixed,
            moving=atlas_ants,
            transformlist=transform_list,
            interpolator="nearestNeighbor",
        )

        labels_arr = warped_labels.numpy()
        if labels_arr.shape != raw_volume.shape:
            return None

        lobe_masks = {}
        for idx, name in enumerate(label_names):
            key = _LOBE_NAME_MAP.get(name)
            if key is None:
                continue
            mask = labels_arr == idx
            if mask.sum() > 30:
                lobe_masks[key] = mask

        return lobe_masks if lobe_masks else None

    except Exception:
        return None


def _clean_lobe_mask(mask: np.ndarray, closing_iters: int = 2) -> np.ndarray:
    """
    Fill small internal gaps (e.g. a thin low-intensity sulcus briefly
    interrupting the mask) and discard every connected piece except the
    largest one, so a lobe always renders as a single solid blob instead
    of scattered islands.
    """
    if mask.sum() == 0:
        return mask

    struct = ndimage.generate_binary_structure(3, 2)
    closed = ndimage.binary_closing(mask, structure=struct, iterations=closing_iters)

    labeled, num_components = ndimage.label(closed, structure=struct)
    if num_components <= 1:
        return closed

    sizes = ndimage.sum(closed, labeled, index=range(1, num_components + 1))
    largest_label = int(np.argmax(sizes)) + 1
    return labeled == largest_label


def get_all_lobe_masks(raw_volume: np.ndarray, spacing=(1.0, 1.0, 1.0), cache_transform_path=None):
    """
    Single source of truth for ALL 6 anatomical regions used across the app
    (three_d_viewer.py's mesh rendering AND main_window.py's lobar-involvement
    text both call this) -- so the two never drift out of sync again.

    Cerebellum and spinal cord are always computed geometrically (cortical
    atlases don't reliably cover them). The 4 cerebral lobes use the atlas
    when available, falling back to coordinate thresholds otherwise.

    Returns a dict with keys: frontal, parietal, occipital, temporal,
    cerebellum, spinal_cord -- each a boolean array shaped like raw_volume.
    """
    nz, ny, nx = raw_volume.shape
    mean_val = float(np.mean(raw_volume[raw_volume > 0])) if np.any(raw_volume > 0) else 10.0
    brain_bin = raw_volume > (mean_val * 0.35)

    z_grid, y_grid, x_grid = np.indices((nz, ny, nx))
    z_norm = z_grid / float(nz)
    y_norm = y_grid / float(ny)
    x_norm = x_grid / float(nx)

    spinal_cord_bin = (
        brain_bin & (z_norm < 0.28) &
        (y_norm >= 0.38) & (y_norm <= 0.62) &
        (x_norm >= 0.38) & (x_norm <= 0.62)
    )
    cerebellum_bin = (
        brain_bin & (z_norm < 0.38) & (y_norm < 0.50) & (~spinal_cord_bin)
    )

    atlas_masks = get_lobe_masks(raw_volume, spacing=spacing, cache_transform_path=cache_transform_path)

    if atlas_masks is not None:
        occipital_bin = atlas_masks.get("occipital", np.zeros_like(brain_bin)) & (~spinal_cord_bin) & (~cerebellum_bin)
        frontal_bin = atlas_masks.get("frontal", np.zeros_like(brain_bin)) & (~spinal_cord_bin) & (~cerebellum_bin)
        temporal_bin = atlas_masks.get("temporal", np.zeros_like(brain_bin)) & (~spinal_cord_bin) & (~cerebellum_bin)
        parietal_bin = atlas_masks.get("parietal", np.zeros_like(brain_bin)) & (~spinal_cord_bin) & (~cerebellum_bin)
    else:
        occipital_bin = (
            brain_bin & (y_norm < 0.32) & (z_norm >= 0.38) & (~cerebellum_bin) & (~spinal_cord_bin)
        )

        # Sylvian-fissure-like ceiling for the temporal lobe. Held flat and
        # fairly high through the posterior-to-mid brain (where parietal
        # sits directly above temporal), then descends toward the anterior
        # tip (where frontal sits above and the fissure runs lower) --
        # much closer to the real diagonal shape of the fissure than a
        # single flat cutoff, and continuous across the frontal/parietal
        # seam at y_norm = 0.50 so neither lobe gets starved.
        fissure_height = np.where(
            y_norm < 0.50,
            0.55,
            0.55 - 0.40 * np.clip((y_norm - 0.50) / 0.50, 0.0, 1.0),
        )

        frontal_bin = (
            brain_bin & (~spinal_cord_bin) &
            (y_norm >= 0.50) & (z_norm >= fissure_height)
        )
        temporal_bin = (
            brain_bin &
            (y_norm >= 0.30) & (z_norm < fissure_height) &
            (~cerebellum_bin) & (~spinal_cord_bin) & (~occipital_bin)
        )
        parietal_bin = (
            brain_bin &
            (~frontal_bin) & (~occipital_bin) & (~temporal_bin) &
            (~cerebellum_bin) & (~spinal_cord_bin)
        )

    return {
        "frontal": _clean_lobe_mask(frontal_bin),
        "parietal": _clean_lobe_mask(parietal_bin),
        "occipital": _clean_lobe_mask(occipital_bin),
        "temporal": _clean_lobe_mask(temporal_bin),
        "cerebellum": _clean_lobe_mask(cerebellum_bin),
        "spinal_cord": _clean_lobe_mask(spinal_cord_bin),
    }


if __name__ == "__main__":
    # Run this file directly once to confirm the exact label names your
    # installed nilearn version returns, in case _LOBE_NAME_MAP needs tweaking.
    _, _, names = _load_atlas_and_template()
    for i, n in enumerate(names):
        print(i, n)