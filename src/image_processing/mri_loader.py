import os
import glob
import SimpleITK as sitk


def read_image_source(source):
    """Read one supported volume file or an explicitly assigned DICOM series."""
    if isinstance(source, dict) and source.get("kind") == "dicom":
        files = list(source.get("files") or [])
        if not files:
            raise ValueError("The selected DICOM series has no readable image files.")
        reader = sitk.ImageSeriesReader()
        reader.SetFileNames(files)
        return reader.Execute()
    return sitk.ReadImage(str(source))

class MultiParametricMRILoader:
    MODALITY_ALIASES = {
        "flair": ["flair", "t2_flair", "flair_space", "t2-flair"],
        "t1": ["t1", "t1_axial", "t1_mprage", "t1_iso"],
        "t1ce": ["t1ce", "t1gd", "t1_ce", "t1_gd", "t1post", "t1_contrast", "t1+c"],
        "t2": ["t2", "t2_tse", "t2_axial", "t2_turbo"]
    }

    @classmethod
    def resolve_folder(cls, folder_path: str) -> dict:
        all_files = sorted(
            [
                path for path in glob.glob(os.path.join(folder_path, "**", "*"), recursive=True)
                if path.lower().endswith((".nii", ".nii.gz", ".nrrd", ".mha", ".mhd", ".hdr"))
            ],
            key=str.lower,
        )
        resolved_paths = {}

        for canonical_name, aliases in cls.MODALITY_ALIASES.items():
            for file_path in all_files:
                filename = os.path.basename(file_path).lower()
                
                # Ignore mask/segmentation files
                if "seg" in filename or "mask" in filename:
                    continue

                # T1 is a substring of T1ce; exclude contrast-enhanced scans
                # from the plain T1 slot even if the filesystem lists them first.
                if canonical_name == "t1" and any(token in filename for token in ("t1ce", "t1gd", "t1post", "t1_contrast", "t1+c")):
                    continue
                if canonical_name == "t2" and any(token in filename for token in ("t2_flair", "t2-flair")):
                    continue
                if any(alias in filename for alias in aliases):
                    resolved_paths[canonical_name] = file_path
                    break

        return resolved_paths

    @classmethod
    def load_nifti_modalities(cls, input_source):
        if isinstance(input_source, str) and os.path.isdir(input_source):
            modality_paths = cls.resolve_folder(input_source)
        elif isinstance(input_source, dict):
            normalized = {}
            for key, path in input_source.items():
                k_lower = key.lower()
                matched = False
                for canonical, aliases in cls.MODALITY_ALIASES.items():
                    if k_lower == canonical or k_lower in aliases:
                        normalized[canonical] = path if isinstance(path, dict) else str(path)
                        matched = True
                        break
                if not matched:
                    normalized[k_lower] = str(path)
            modality_paths = normalized
        else:
            raise TypeError("input_source must be a directory path or a dict of file paths.")

        required = {"t1", "t1ce", "t2", "flair"}
        missing = required - modality_paths.keys()
        if missing:
            raise ValueError(
                f"Missing required modalities: {list(missing)}. "
                f"Detected: {list(modality_paths.keys())}"
            )

        images = {}
        reference_image = None

        for mod in ["t1", "t1ce", "t2", "flair"]:
            path = modality_paths[mod]
            img = read_image_source(path)

            if reference_image is None:
                reference_image = img
            else:
                same_geometry = (
                    img.GetSize() == reference_image.GetSize()
                    and img.GetSpacing() == reference_image.GetSpacing()
                    and img.GetOrigin() == reference_image.GetOrigin()
                    and img.GetDirection() == reference_image.GetDirection()
                )
                if not same_geometry:
                    img = sitk.Resample(img, reference_image, sitk.Transform(), sitk.sitkLinear)

            images[mod] = img

        return images
