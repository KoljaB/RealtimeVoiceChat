import torch

print("PyTorch 版本:", torch.__version__)
print("是否支持 CUDA:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("CUDA 版本:", torch.version.cuda)
    print("当前 GPU:", torch.cuda.get_device_name(0))