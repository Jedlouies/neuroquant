import numpy as np
import onnxruntime as ort
from PySide6.QtCore import QThread, Signal

class SegmentationWorker(QThread):
    finished = Signal(dict)
    error = Signal(str)
    progress = Signal(int, str)  # (percentage, status_text)

    def __init__(self, model_path: str, full_volume: np.ndarray, voxel_spacing: tuple = (1.0, 1.0, 1.0)):
        super().__init__()
        self.model_path = model_path
        self.full_volume = full_volume  # Shape: (4, D, H, W)
        self.voxel_spacing = voxel_spacing

    def run(self):
        try:
            self.progress.emit(5, "Initializing ONNX neural engine...")
            
            sess_options = ort.SessionOptions()
            sess_options.intra_op_num_threads = 4
            
            # Safely check available providers in the current environment
            available = ort.get_available_providers()
            providers = []
            if "CUDAExecutionProvider" in available:
                providers.append("CUDAExecutionProvider")
            if "DmlExecutionProvider" in available:
                providers.append("DmlExecutionProvider")
            providers.append("CPUExecutionProvider")

            session = ort.InferenceSession(self.model_path, sess_options, providers=providers)
            
            input_name = session.get_inputs()[0].name
            output_name = session.get_outputs()[0].name

            channels, D, H, W = self.full_volume.shape
            patch_size = 128

            # Output prediction accumulator buffers
            num_classes = 3  # 0: Background, 1: Active Core, 2: Edema
            prob_map = np.zeros((num_classes, D, H, W), dtype=np.float32)
            count_map = np.zeros((D, H, W), dtype=np.float32)

            def get_coordinates(dim_size, patch_dim):
                if dim_size <= patch_dim:
                    return [0]
                mid = (dim_size - patch_dim) // 2
                return [0, mid, dim_size - patch_dim]

            d_coords = get_coordinates(D, patch_size)
            h_coords = get_coordinates(H, patch_size)
            w_coords = get_coordinates(W, patch_size)

            total_patches = len(d_coords) * len(h_coords) * len(w_coords)
            patch_idx = 0

            # 2. Multi-patch overlapping sliding-window inference
            for ds in d_coords:
                for hs in h_coords:
                    for ws in w_coords:
                        patch_idx += 1
                        pct = int(10 + (patch_idx / total_patches) * 75)
                        self.progress.emit(pct, f"Evaluating patch {patch_idx}/{total_patches} ({pct}%)")

                        # Extract patch
                        patch = self.full_volume[:, ds:ds+patch_size, hs:hs+patch_size, ws:ws+patch_size]
                        
                        pad_d = max(0, patch_size - patch.shape[1])
                        pad_h = max(0, patch_size - patch.shape[2])
                        pad_w = max(0, patch_size - patch.shape[3])

                        if pad_d > 0 or pad_h > 0 or pad_w > 0:
                            patch = np.pad(patch, ((0, 0), (0, pad_d), (0, pad_h), (0, pad_w)), mode="constant")

                        # Skip background patches
                        if np.count_nonzero(patch[3] > 0.1) < 20:
                            continue

                        inp = np.ascontiguousarray(patch[np.newaxis, ...], dtype=np.float32)
                        logits = session.run([output_name], {input_name: inp})[0][0]

                        # Softmax normalization
                        exp_logits = np.exp(logits - np.max(logits, axis=0, keepdims=True))
                        patch_probs = exp_logits / np.sum(exp_logits, axis=0, keepdims=True)

                        valid_d = patch_size - pad_d
                        valid_h = patch_size - pad_h
                        valid_w = patch_size - pad_w
                        valid_probs = patch_probs[:, :valid_d, :valid_h, :valid_w]

                        prob_map[:, ds:ds+valid_d, hs:hs+valid_h, ws:ws+valid_w] += valid_probs
                        count_map[ds:ds+valid_d, hs:hs+valid_h, ws:ws+valid_w] += 1.0

            # 3. Aggregate full-volume mask
            self.progress.emit(90, "Reconstructing 3D volume mask...")
            count_map = np.maximum(count_map, 1e-5)
            prob_map /= count_map
            full_mask = np.argmax(prob_map, axis=0).astype(np.uint8)

            # 4. Volumetric calculations
            self.progress.emit(96, "Calculating volumetric quantifications...")
            vx_vol = float(self.voxel_spacing[0] * self.voxel_spacing[1] * self.voxel_spacing[2])
            core_mm3 = float((full_mask == 1).sum() * vx_vol)
            edema_mm3 = float((full_mask == 2).sum() * vx_vol)
            total_mm3 = core_mm3 + edema_mm3

            self.progress.emit(100, "Inference completed (100%)")

            result = {
                "mask": full_mask,
                "metrics": {
                    "active_tumor_core_mm3": core_mm3,
                    "peritumoral_edema_mm3": edema_mm3,
                    "total_tumor_volume_mm3": total_mm3
                }
            }
            self.finished.emit(result)

        except Exception as e:
            self.error.emit(str(e))