import onnxruntime as ort
import numpy as np

class NeuroQuantInferenceEngine:
    def __init__(self, model_path: str = "data/models/neuroquant_3dunet.onnx"):
        # Explicitly configure CPU execution for standalone offline usage
        providers = ["CPUExecutionProvider"]
        # Use DirectML / CUDA if available locally
        available_providers = ort.get_available_providers()
        if "DmlExecutionProvider" in available_providers:
            providers.insert(0, "DmlExecutionProvider")
        elif "CUDAExecutionProvider" in available_providers:
            providers.insert(0, "CUDAExecutionProvider")

        self.session = ort.InferenceSession(model_path, providers=providers)
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name

    def run_inference(self, preprocessed_tensor: np.ndarray) -> np.ndarray:
        """
        preprocessed_tensor: 4D array with shape (4, 128, 128, 128)
        Returns: 3D discrete label mask with shape (128, 128, 128)
        """
        # Add batch dimension -> (1, 4, 128, 128, 128)
        model_input = np.expand_dims(preprocessed_tensor, axis=0).astype(np.float32)

        # Execute model via ONNX Runtime
        raw_outputs = self.session.run([self.output_name], {self.input_name: model_input})[0]

        # raw_outputs shape: (1, 3, 128, 128, 128)
        # Apply argmax across channel axis (1) to get discrete class labels (0, 1, 2)[cite: 1]
        predicted_mask = np.argmax(raw_outputs[0], axis=0).astype(np.uint8)
        return predicted_mask