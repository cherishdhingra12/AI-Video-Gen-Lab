"""
Modal GPU app for AI Video Gen Lab — the two GPU steps of the pipeline.

  gen_frame(prompt) -> PNG bytes         (agent 3a: keyframes, Flux.1-schnell)
  gen_video(first, last, prompt) -> MP4  (agent 3b: Wan2.1 FLF2V, interpolates
                                          first frame -> last frame)

Deploy once, then the orchestrator calls these by name:
    modal deploy modal_gpu.py
Smoke test (proves real frames -> real clip, saved to ./_smoke/):
    modal run modal_gpu.py

ponytail: first+last-frame control has no lightweight model — FLF2V is 14B, so
cpu_offload (fits A100-40GB) is mandatory, not a shortcut. Cost levers are
resolution + num_frames + steps, exposed as args. Measure real burn, don't guess.
"""

import io
import modal

FRAME_MODEL = "black-forest-labs/FLUX.1-schnell"          # Apache-2.0, ungated
VIDEO_MODEL = "Wan-AI/Wan2.1-FLF2V-14B-720P-diffusers"    # first+last frame -> video

app = modal.App("aivgl")
weights = modal.Volume.from_name("aivgl-hf-cache", create_if_missing=True)
outputs = modal.Volume.from_name("aivgl-outputs", create_if_missing=True)  # final videos land here
# HF_TOKEN lands in the container env; diffusers/huggingface_hub auto-use it.
# Create once:  modal secret create huggingface HF_TOKEN=hf_xxx
hf_secret = modal.Secret.from_name("huggingface")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "torch",
        "diffusers>=0.32",
        "transformers",
        "accelerate",
        "sentencepiece",
        "ftfy",
        "imageio",
        "imageio-ffmpeg",
        "Pillow",
        "numpy",
    )
    .env({"HF_HOME": "/cache", "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"})
)


def _snap(x: int, mult: int = 16) -> int:
    """Wan/VAE need dims divisible by 16."""
    return max(mult, (x // mult) * mult)


# ponytail: Flux's 12B transformer is ~24GB in bf16 — one offload chunk overflows
# a 22GB L4. A100-40GB fits it with cpu_offload; keyframes are 4-step so still quick.
@app.function(gpu="A100", image=image, volumes={"/cache": weights}, secrets=[hf_secret], timeout=30 * 60)
def gen_frame(prompt: str, width: int = 1280, height: int = 720, seed: int = 0) -> bytes:
    import torch
    from diffusers import FluxPipeline

    pipe = FluxPipeline.from_pretrained(FRAME_MODEL, torch_dtype=torch.bfloat16)
    weights.commit()
    pipe.enable_model_cpu_offload()  # Flux ~24GB in bf16; offload keeps it on L4

    img = pipe(
        prompt=prompt,
        width=_snap(width),
        height=_snap(height),
        num_inference_steps=4,      # schnell is a 4-step distilled model
        guidance_scale=0.0,
        generator=torch.Generator("cpu").manual_seed(seed),
    ).images[0]

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@app.function(gpu="A100", image=image, volumes={"/cache": weights}, secrets=[hf_secret], timeout=60 * 60)
def gen_video(
    first_png: bytes,
    last_png: bytes,
    prompt: str,
    negative_prompt: str = "",
    resolution: int = 480,      # 480 (cheap) or 720 (quality)
    num_frames: int = 81,       # 81 @ 16fps ~= 5s
    steps: int = 40,
    fps: int = 16,
    portrait: bool = False,     # True = 9:16 vertical (reels), False = 16:9
) -> bytes:
    import torch
    from diffusers import WanImageToVideoPipeline, AutoencoderKLWan, UniPCMultistepScheduler
    from diffusers.utils import export_to_video
    from transformers import CLIPVisionModel
    from PIL import Image

    if resolution >= 720:
        long_, short_, flow_shift = 1280, 720, 5.0
    else:
        long_, short_, flow_shift = 832, 480, 3.0
    w, h = (short_, long_) if portrait else (long_, short_)  # swap for 9:16
    w, h = _snap(w), _snap(h)

    def prep(b: bytes) -> "Image.Image":
        return Image.open(io.BytesIO(b)).convert("RGB").resize((w, h))

    first, last = prep(first_png), prep(last_png)

    image_encoder = CLIPVisionModel.from_pretrained(
        VIDEO_MODEL, subfolder="image_encoder", torch_dtype=torch.float32
    )
    vae = AutoencoderKLWan.from_pretrained(
        VIDEO_MODEL, subfolder="vae", torch_dtype=torch.float32
    )
    pipe = WanImageToVideoPipeline.from_pretrained(
        VIDEO_MODEL, vae=vae, image_encoder=image_encoder, torch_dtype=torch.bfloat16
    )
    pipe.scheduler = UniPCMultistepScheduler.from_config(
        pipe.scheduler.config, flow_shift=flow_shift
    )
    weights.commit()
    pipe.enable_model_cpu_offload()  # 14B won't fit 40GB all-on-GPU

    frames = pipe(
        image=first,
        last_image=last,          # <-- the first->last interpolation
        prompt=prompt,
        negative_prompt=negative_prompt or None,
        height=h,
        width=w,
        num_frames=num_frames,
        num_inference_steps=steps,
        guidance_scale=5.5,
    ).frames[0]

    out = "/tmp/scene.mp4"
    export_to_video(frames, out, fps=fps)
    with open(out, "rb") as f:
        return f.read()


@app.function(image=image, volumes={"/out": outputs}, timeout=6 * 60 * 60)
def render_project(
    shotlist: dict,
    project: str = "project",
    resolution: int = 480,
    scene_frames: int = 49,
    steps: int = 40,
    xfade: float = 0.4,         # crossfade seconds between scenes (0 = hard cut)
    fps: int = 16,
) -> str:
    """Fire-and-forget cloud render: keyframes -> per-scene FLF2V clips -> stitch.
    Runs entirely on Modal so the Mac can be off. Fans out GPU work in parallel.
    Final lands in the aivgl-outputs volume at /<project>/final.mp4."""
    import os
    import subprocess
    import imageio_ffmpeg

    kfs = sorted(shotlist["keyframes"], key=lambda k: k["id"])
    style = shotlist.get("style", "")
    world = shotlist.get("world", "")          # locked environment/atmosphere anchor
    negative = shotlist.get("negative", "")    # what must NOT drift in
    portrait = shotlist.get("aspect", "16:9").replace(" ", "") in ("9:16", "vertical", "portrait")
    fw, fh = (720, 1280) if portrait else (1280, 720)  # keyframe canvas

    # 1) all keyframes in parallel (Flux). World anchor first = it dominates the frame.
    def kf_prompt(k):
        parts = [world, k["prompt"], style]
        return ", ".join(p for p in parts if p)
    frame_args = [(kf_prompt(k), fw, fh, i) for i, k in enumerate(kfs)]  # seed=i = stable, distinct
    frames = list(gen_frame.starmap(frame_args))
    id2png = {k["id"]: png for k, png in zip(kfs, frames)}

    # 2) all scenes in parallel — each interpolates keyframe[from] -> keyframe[to].
    #    World anchor rides on the motion prompt too, so the video model can't wander.
    scenes = shotlist["scenes"]
    def motion_prompt(sc):
        parts = [world, sc.get("motion", "")]
        return ", ".join(p for p in parts if p)
    clip_args = [
        (id2png[sc["from"]], id2png[sc["to"]], motion_prompt(sc), negative,
         resolution, scene_frames, steps, fps, portrait)
        for sc in scenes
    ]
    clips = list(gen_video.starmap(clip_args))

    # 3) write clips + stitch (re-encode so independently-coded clips join cleanly)
    d = f"/out/{project}"
    os.makedirs(d, exist_ok=True)
    paths = []
    for i, mp4 in enumerate(clips):
        p = f"{d}/scene_{i:03d}.mp4"
        with open(p, "wb") as f:
            f.write(mp4)
        paths.append(p)

    final = f"{d}/final.mp4"
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    dur = scene_frames / fps                       # each clip's length in seconds
    if len(paths) == 1 or xfade <= 0 or xfade >= dur:
        # nothing to blend (or blend wider than a clip) -> plain concat re-encode
        listtxt = f"{d}/list.txt"
        with open(listtxt, "w") as fh_:
            fh_.write("".join(f"file '{p}'\n" for p in paths))
        subprocess.run(
            [ff, "-y", "-f", "concat", "-safe", "0", "-i", listtxt,
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps), final],
            check=True,
        )
    else:
        # chain xfades: each transition starts xfade secs before the running end.
        # scenes share the same frame at their boundary, so the blend just softens
        # the FLF2V motion easing (velocity dip) rather than papering over a jump-cut.
        inputs = []
        for p in paths:
            inputs += ["-i", p]
        fc, prev, off = [], "0:v", 0.0
        for i in range(1, len(paths)):
            off += dur - xfade                     # running end minus overlap
            out = f"x{i}"
            fc.append(f"[{prev}][{i}:v]xfade=transition=fade:"
                      f"duration={xfade}:offset={off:.3f}[{out}]")
            prev = out
        subprocess.run(
            [ff, "-y", *inputs, "-filter_complex", ";".join(fc),
             "-map", f"[{prev}]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-r", str(fps), final],
            check=True,
        )
    outputs.commit()
    print(f"DONE: {final} ({len(paths)} scenes)")
    return final


# ponytail: audio/narration deferred. Shotlist carries per-scene narration text;
# TTS overlay + music is a follow-up (free TTS needs its own provider). Ship video first.


@app.local_entrypoint()
def main():
    """Smoke test: two real Flux keyframes -> one FLF2V clip between them."""
    import os

    os.makedirs("_smoke", exist_ok=True)
    p1 = "cinematic wide shot, a lone red sailboat on a calm turquoise sea at dawn, soft golden light, film grain"
    p2 = "cinematic wide shot, the same red sailboat now close to a rocky lighthouse island, midday sun, film grain"

    print("generating keyframes...")
    f1 = gen_frame.remote(p1, seed=1)
    f2 = gen_frame.remote(p2, seed=2)
    open("_smoke/key1.png", "wb").write(f1)
    open("_smoke/key2.png", "wb").write(f2)
    print(f"  key1.png {len(f1)}b, key2.png {len(f2)}b")

    print("interpolating first->last into video (480p, 49 frames)...")
    mp4 = gen_video.remote(
        f1, f2,
        prompt="the sailboat glides across the water toward the lighthouse, gentle waves, slow cinematic camera",
        resolution=480, num_frames=49, steps=30,
    )
    open("_smoke/scene.mp4", "wb").write(mp4)
    print(f"OK: _smoke/scene.mp4 ({len(mp4)} bytes)")
