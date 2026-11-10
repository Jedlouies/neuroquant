import glob
import os
from monai.transforms import (
    Compose,
    LoadImaged,
    EnsureChannelFirstd,
    NormalizeIntensityd,
    CropForegroundd,
    RandSpatialCropd,
    ConcatItemsd,
    ToTensord,
)
from monai.data import Dataset, DataLoader

def get_brats_data_dicts(data_dir: str):
    patient_dirs = sorted([os.path.join(data_dir, d) for d in os.listdir(data_dir) if os.path.isdir(os.path.join(data_dir, d))])
    data_dicts = []
    for p_dir in patient_dirs:
        p_name = os.path.basename(p_dir)
        data_dicts.append({
            "t1": os.path.join(p_dir, f"{p_name}_t1.nii.gz"),
            "t1ce": os.path.join(p_dir, f"{p_name}_t1ce.nii.gz"),
            "t2": os.path.join(p_dir, f"{p_name}_t2.nii.gz"),
            "flair": os.path.join(p_dir, f"{p_name}_flair.nii.gz"),
            "seg": os.path.join(p_dir, f"{p_name}_seg.nii.gz"),
        })
    return data_dicts

# Pipeline to stack 4 modalities into a (4, D, H, W) tensor
train_transforms = Compose([
    LoadImaged(keys=["t1", "t1ce", "t2", "flair", "seg"]),
    EnsureChannelFirstd(keys=["t1", "t1ce", "t2", "flair", "seg"]),
    ConcatItemsd(keys=["t1", "t1ce", "t2", "flair"], name="image"),
    NormalizeIntensityd(keys=["image"], nonzero=True, channel_wise=True),
    CropForegroundd(keys=["image", "seg"], source_key="image"),
    RandSpatialCropd(keys=["image", "seg"], roi_size=[128, 128, 128], random_size=False),
    ToTensord(keys=["image", "seg"]),
])