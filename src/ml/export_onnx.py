import os
import torch
from monai.networks.nets import UNet

def export():
    # 1. Project-root relative paths
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    weights_path = os.path.join(base_dir, "data", "models", "best_metric_model.pth")
    output_path = os.path.join(base_dir, "data", "models", "neuroquant_3dunet.onnx")

    if not os.path.exists(weights_path):
        raise FileNotFoundError(f"Missing weights file at: {weights_path}")

    # 2. Recreate architecture
    model = UNet(
        spatial_dims=3,
        in_channels=4,
        out_channels=3,
        channels=(16, 32, 64, 128),
        strides=(2, 2, 2),
        num_res_units=2,
        norm="batch",
    )

    print(f"Loading weights from {weights_path}...")
    model.load_state_dict(torch.load(weights_path, map_location="cpu"))
    model.eval()

    # 3. Clean up any existing broken or split files
    for path in [output_path, output_path + ".data"]:
        if os.path.exists(path):
            os.remove(path)

    # 4. Export self-contained ONNX model
    dummy_input = torch.randn(1, 4, 128, 128, 128)
    print("Exporting ONNX model directly to data/models/ ...")
    torch.onnx.export(
        model,
        dummy_input,
        output_path,
        export_params=True,
        opset_version=14,
        do_constant_folding=True,
        input_names=["input_mri"],
        output_names=["output_segmentation"],
        dynamic_axes={
            "input_mri": {0: "batch_size"},
            "output_segmentation": {0: "batch_size"},
        },
    )
    print(f"Success! Model exported to: {output_path}")

if __name__ == "__main__":
    export()