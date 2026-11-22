"""Crowd counter: DM-Count + CSRNet fusion, with models and weights taken from CCTV_MONITOR.

Same blend CCTV_MONITOR uses: a fixed 80 % DM-Count / 20 % CSRNet average, or the learned
fusion head when a fusion_head.pth exists. Weights are read from the CCTV_MONITOR folder
(sibling of this one by default) so the ~150 MB of checkpoints are not duplicated.
"""
import os
from pathlib import Path

import cv2
import numpy as np
import torch

from fusion.models import FusionCountingModel

CCTV = Path(os.getenv("CCTV_MONITOR_DIR", str(Path(__file__).resolve().parent.parent / "CCTV_MONITOR")))
DM_WEIGHTS = Path(os.getenv("DM_COUNT_WEIGHTS", str(CCTV / "dm_count/pretrained_models/model_nwpu.pth")))
CSR_WEIGHTS = Path(os.getenv("CSRNET_WEIGHTS", str(CCTV / "csrnet/pretrained_models/csrnet_shtechA.pth")))
HEAD_WEIGHTS = Path(os.getenv("FUSION_HEAD_WEIGHTS", str(CCTV / "fusion/pretrained_models/fusion_head.pth")))
DM_SHARE = float(os.getenv("FUSION_DM_WEIGHT", "0.8"))
CSR_SHARE = float(os.getenv("FUSION_CSR_WEIGHT", "0.2"))
MAX_W = int(os.getenv("CROWD_INFER_WIDTH", "512"))    # same CPU size CCTV_MONITOR uses; ~0.9 s per frame
MAX_H = int(os.getenv("CROWD_INFER_HEIGHT", "288"))
MEAN = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
STD = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)


class CrowdCounter:
    def __init__(self):
        for path in (DM_WEIGHTS, CSR_WEIGHTS):
            if not path.is_file():
                raise FileNotFoundError(f"Crowd model weights not found: {path} (set CCTV_MONITOR_DIR)")
        if os.getenv("CROWD_THREADS"):  # process-wide: also slows the YOLO tracker, so off by default
            torch.set_num_threads(int(os.environ["CROWD_THREADS"]))
        self.model = FusionCountingModel(dm_pretrained=False, csr_pretrained=False, freeze_backbones=True,
                                         dm_weight=DM_SHARE, csr_weight=CSR_SHARE)
        self.model.load_backbone_weights(str(DM_WEIGHTS), str(CSR_WEIGHTS), device="cpu")
        self.mode = "static"
        if HEAD_WEIGHTS.is_file():
            self.model.load_fusion_head(str(HEAD_WEIGHTS), device="cpu")
            self.mode = "learned"
        self.model.eval().to(memory_format=torch.channels_last)   # ~9 % faster on CPU, identical counts

    def count(self, bgr):
        """Return (people, density_map) for one BGR frame. The map is at the model's 1/4 resolution."""
        h, w = bgr.shape[:2]
        scale = min(1.0, MAX_W / w, MAX_H / h)
        size = (max(32, round(w * scale / 8) * 8), max(32, round(h * scale / 8) * 8))  # stride-8 multiples
        rgb = cv2.cvtColor(cv2.resize(bgr, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
        x = ((torch.from_numpy(rgb).permute(2, 0, 1).float().unsqueeze(0) / 255 - MEAN) / STD).contiguous(memory_format=torch.channels_last)
        with torch.inference_mode():
            fused, _ = self.model(x, mode=self.mode)
        dmap = fused.squeeze().numpy().astype(np.float32)
        return float(dmap.sum()), dmap
