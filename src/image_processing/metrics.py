import numpy as np

def calculate_volumes(mask: np.ndarray, voxel_spacing: tuple) -> dict:
    # Calculate physical volume of a single voxel in mm³ (dx * dy * dz)
    voxel_volume_mm3 = float(voxel_spacing[0] * voxel_spacing[1] * voxel_spacing[2])
    
    # Count voxels per label class
    core_voxels = int(np.count_nonzero(mask == 1))
    edema_voxels = int(np.count_nonzero(mask == 2))
    total_voxels = core_voxels + edema_voxels

    return {
        "active_tumor_core_mm3": round(core_voxels * voxel_volume_mm3, 2),
        "peritumoral_edema_mm3": round(edema_voxels * voxel_volume_mm3, 2),
        "total_tumor_volume_mm3": round(total_voxels * voxel_volume_mm3, 2),
        "voxel_counts": {
            "core": core_voxels,
            "edema": edema_voxels,
            "total": total_voxels
        }
    }