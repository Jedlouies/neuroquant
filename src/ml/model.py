import torch
from monai.networks.nets import UNet

def build_3d_unet():
    model = UNet(
        spatial_dims=3,
        in_channels=4,      # T1, T1Gd, T2, FLAIR
        out_channels=3,     # Healthy/Background (0), Active Core (1), Edema (2)
        channels=(16, 32, 64, 128, 256),
        strides=(2, 2, 2, 2),
        num_res_units=2,
        dropout=0.2,
    )
    return model

if __name__ == "__main__":
    net = build_3d_unet()
    dummy_input = torch.randn(1, 4, 128, 128, 128)
    out = net(dummy_input)
    print("3D U-Net Output Shape:", out.shape) 