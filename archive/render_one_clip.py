"""
Proof-of-$0: render ONE image->video clip on Modal's free-tier GPU.

This is the crux step of the whole workflow (agent 3b). If this renders a clip
for $0, the free path is real and we build the 4-agent pipeline around it.
The Mac only orchestrates; the GPU lives in Modal's cloud.

Run:
    modal setup                      # one-time browser login (do this yourself)
    modal run render_one_clip.py                         # uses a generated test frame
    modal run render_one_clip.py --image path/to/frame.png --prompt "camera pushes in slowly"

Output: clip.mp4 saved next to this file.

ponytail: Wan2.1-I2V-14B-480P on an A100 = the reliable, well-documented path
for a first proof. Cheaper-per-clip upgrade once proven = Wan2.2 TI2V-5B on an
L4/A10G (fits 24GB), swap MODEL_ID + GPU below.
"""

import io
import modal

MODEL_ID = "Wan-AI/Wan2.1-I2V-14B-480P-Diffusers"
GPU = "A100"  # 40GB; 480p I2V fits. Burns free credit faster than a 5B on L4.

app = modal.App("aivgl-render-one-clip")

# Weights are cached in a Volume so repeat runs skip the multi-GB download.
weights = modal.Volume.from_name("aivgl-hf-cache", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "torch",
        "diffusers>=0.32",
        "transformers",
        "accelerate",
        "ftfy",
        "imageio",
        "imageio-ffmpeg",
        "Pillow",
    )
    .env({"HF_HOME": "/cache", "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"})
)


@app.function(gpu=GPU, image=image, volumes={"/cache": weights}, timeout=60 * 60)
def render(image_bytes: bytes | None, prompt: str, num_frames: int, fps: int) -> bytes:
    import torch
    from diffusers import WanImageToVideoPipeline, AutoencoderKLWan
    from diffusers.utils import export_to_video
    from transformers import CLIPVisionModel
    from PIL import Image

    # No frame supplied -> make a placeholder so the render path is provable
    # with zero local input. Real frames come from agent 3a (image gen).
    if image_bytes is None:
        frame = Image.new("RGB", (832, 480))
        px = frame.load()
        for y in range(480):
            for x in range(832):
                px[x, y] = (x % 256, y % 256, (x + y) % 256)
    else:
        frame = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    image_encoder = CLIPVisionModel.from_pretrained(
        MODEL_ID, subfolder="image_encoder", torch_dtype=torch.float32
    )
    vae = AutoencoderKLWan.from_pretrained(
        MODEL_ID, subfolder="vae", torch_dtype=torch.float32
    )
    pipe = WanImageToVideoPipeline.from_pretrained(
        MODEL_ID, vae=vae, image_encoder=image_encoder, torch_dtype=torch.bfloat16
    )
    weights.commit()  # persist the multi-GB download NOW, before the render can OOM
    # ponytail: 14B in bf16 won't fit A100-40GB all-on-GPU (OOM'd at .to cuda).
    # cpu_offload streams components on/off GPU: fits, ~slower. Upgrade path = 5B on L4.
    pipe.enable_model_cpu_offload()

    result = pipe(
        image=frame,
        prompt=prompt,
        num_frames=num_frames,
        guidance_scale=5.0,
    ).frames[0]

    out = "/tmp/clip.mp4"
    export_to_video(result, out, fps=fps)
    weights.commit()  # persist any newly downloaded weights
    with open(out, "rb") as f:
        return f.read()


@app.local_entrypoint()
def main(
    image: str = "",
    prompt: str = "cinematic, the scene comes alive, camera slowly pushes in, soft light",
    num_frames: int = 49,
    fps: int = 16,
):
    img_bytes = None
    if image:
        with open(image, "rb") as f:
            img_bytes = f.read()

    mp4 = render.remote(img_bytes, prompt, num_frames, fps)

    out_path = "clip.mp4"
    with open(out_path, "wb") as f:
        f.write(mp4)
    print(f"OK: wrote {out_path} ({len(mp4)} bytes) — {num_frames} frames @ {fps}fps")
