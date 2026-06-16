# Technical Advisory

Architecture notes and open questions for the project maintainer.

---

## 1. Supabase (free plan) — Not worth adopting now

**Verdict: skip it for this project's current scope.**

The app's main selling point is that nothing leaves the user's machine. Adding Supabase would introduce a hard cloud dependency that contradicts that promise and creates real friction: users need an account, a project, and credentials configured before the app does anything. The free plan's 1 GB storage limit would also be chewed through fast once receipt images start accumulating — a single batch of 50 high-resolution JPEGs can hit 150 MB.

**Future path, if multi-user or multi-device ever matters:**

The reasonable migration would be:

- Store extraction results (vendor, date, amount, category, flags) as rows in a Postgres table. These are tiny — thousands of receipts cost kilobytes.
- Store receipt images in Supabase Storage, with one bucket per user. Accept that images do leave the machine — make that a deliberate product decision, not an accident.
- Keep the LLM inference local. Supabase has no inference product. The LM Studio/Ollama endpoint stays the same; only persistence moves to the cloud.
- Use Row Level Security so each user only sees their own data. The app currently has no auth layer, so that would need to be added before any cloud storage makes sense.

The work is not trivial. Do not reach for Supabase until you have a concrete multi-user requirement.

---

## 2. Best local model and wrapper (mid-2026)

### Wrapper recommendation

**Stay on LM Studio for non-technical users.** It has a GUI model browser, handles quantized model downloads gracefully, exposes an OpenAI-compatible endpoint (the code already targets `LMSTUDIO_BASE_URL`), and loads models on demand. Non-technical users can follow the tutorial in this repo and be running in under 30 minutes.

**Ollama** is the best alternative if you want headless or scriptable deployments. It is a single binary, models are pulled with `ollama pull <name>`, and the API is also OpenAI-compatible — changing `LMSTUDIO_BASE_URL` to `http://localhost:11434/v1` is the entire migration. No GUI, which makes it unsuitable as the default for non-technical users but ideal for server or CI deployments.

**llama.cpp server** and **vLLM** are only worth considering for advanced or multi-user server deployments where you need fine-grained control over batching and quantization. Both require significantly more setup.

"Slothstudio" does not appear to exist as a notable model wrapper. If someone mentioned it to you, they were likely thinking of LM Studio, Ollama, Jan, or GPT4All. Jan is the closest LM Studio alternative with a desktop GUI; GPT4All is simpler but has a narrower model selection and less reliable vision support.

### Model guidance

A good starting point is a two-stage setup: a dedicated document-OCR model (such as `allenai/olmOCR-2-7B`) for the first pass, plus any 7–12B vision/instruction model to distill the structure. That two-stage configuration (the OCR model transcribes text, the instruction model distills structure) consistently outperforms a single large vision model on receipt images, particularly for blurry or low-contrast scans. The app ships no hard-coded default model — it auto-detects whatever you load in LM Studio.

Model recommendations go stale fast. Rather than chasing specific names, use these selection criteria:

- **Vision-capable** (multimodal): the model must accept image inputs.
- **VRAM fit**: for a typical contractor laptop with 8 GB VRAM, look for quantized (QAT or GGUF Q4/Q5) builds of 7–12B parameter models. 26B models require 16+ GB VRAM to run at usable speed.
- **QAT builds preferred over post-training quantization**: QAT (quantization-aware training) models retain more accuracy at lower bit depths.
- **Dedicated OCR model for the first stage**: a model trained specifically on document OCR (olmOCR-class) will extract raw text more reliably than a general vision model. The distillation model then only needs to parse clean text, which any 7–12B instruction model handles well.

---

## 3. Edge AI (Raspberry Pi 5, NPU-class devices)

**Local OCR on edge hardware: yes, works fine.** The bundled offline OCR fallback is RapidOCR (PaddleOCR's PP-OCR models run on onnxruntime) and runs on CPU — a Pi 5 handles it at reasonable speed for the batch sizes a single user generates.

**7B+ vision LLMs on edge hardware: not practical today.** A Pi 5 (4 GB or 8 GB RAM, no discrete VRAM) takes several minutes per image with a quantized 7B model. That is not a usable receipt-processing experience.

**Realistic edge path:**

- Run the local OCR (RapidOCR) on-device to extract raw text from receipt images.
- Send that text (not the image) to a model for structured extraction. Text is tiny compared to images, so this works well over a local network connection to a more capable machine running LM Studio or Ollama.
- Alternatively, a 1–3B parameter text-only instruction model can run on a Pi 5 at usable speed and can handle the distillation step if the OCR text quality is good.

**Hardware accelerators (Hailo-8, Google Coral):** These accelerators are built for CNN-style inference — image classification, object detection, semantic segmentation. They do not accelerate transformer-based LLMs in any meaningful way. The OCR detection stage (a CNN) does benefit from a Coral or Hailo; the recognition and distillation stages do not. Unless you are deploying at scale on embedded hardware, this is not worth pursuing.

---

## 4. Turn-key strategy for non-technical users

The launch wizard (the three-question folder setup in `launch.sh` / `launch.bat`) has eliminated the biggest friction point. The next improvements in order of practical impact:

**1. Publish a pre-built image to GHCR.**
The current flow builds the Docker image locally on first run. Swapping the heavy paddlepaddle stack for RapidOCR (pure onnxruntime wheels, ONNX models bundled, no first-run download) made that build dramatically lighter and more reliable, but it can still take a few minutes and depends on the user's internet. Publishing a pre-built image to GitHub Container Registry means `launch.sh` just does `docker compose pull` and starts immediately. This is the single highest-value thing you can do for non-technical user onboarding.

**2. One-file installer scripts.**
A PowerShell script for Windows and a shell script for Mac that check for Docker, check for LM Studio, download the ZIP, and run launch.bat/launch.sh would eliminate the GitHub ZIP step entirely. This is a meaningful improvement but lower priority than the pre-built image.

**3. Honest assessment of hosted web UI.**
Hosting the web UI on a server does not meaningfully simplify the user experience, because the LLM must run on the user's machine regardless. A hosted frontend would still need a tunnel (e.g., ngrok, Tailscale funnel, Cloudflare Tunnel) from the user's machine to the hosted server so LM Studio is reachable. You would be adding infrastructure complexity without removing the hard part.

The only path to a true zero-install experience is a **cloud-LLM variant**: replace LM Studio calls with a paid vision API (e.g., a Claude or GPT-4o endpoint). That eliminates the local AI requirement entirely and makes a hosted web UI practical. It also changes the privacy story — receipts leave the user's machine. That is a product decision, not a technical one.

---

## 5. Image format rationale

The pipeline stores processed receipt images as JPEG at quality 85 and resizes them to a maximum of 1568 pixels on the long edge before encoding for the model.

**Why JPEG over PNG for receipt photos:**

Receipt photos are photographic content — captured by a phone camera under ambient light. JPEG's lossy compression is designed for this type of content. At quality 85, a typical receipt photo compresses to 3–10× smaller than the equivalent PNG with no perceptible degradation in the fields the model needs to read (text, numbers, logos). PNG lossless compression is appropriate for screenshots, diagrams, and synthetic images with hard edges; it does not help with photographic material.

Vision models do not benefit from lossless encoding. The model's tokenizer samples the image at a fixed grid resolution regardless of the original format. The quality difference between JPEG q85 and PNG at the model's effective resolution is indistinguishable.

The 1568 px resize and auto-crop (which trims blank borders from receipt photos) together bound the base64 payload size that gets sent to LM Studio. Keeping payloads small improves throughput when `MAX_PARALLEL_REQUESTS` is greater than 1 and reduces memory pressure on LM Studio.

---

## 6. Function and feature scrutiny

The following findings came out of a code review of the current codebase. They are documented here so future contributors understand what changed and why.

**`receipt_gui.py` — unrunnable in the shipped container.**
`customtkinter` is not in `requirements.txt` and is not installed in the Docker image. The file was intentionally kept out of the container build because it requires a display. It has been moved to `extras/` to avoid confusion; users who want the desktop GUI are instructed to install `customtkinter` separately before running it.

**`process_receipts_batch` — dead `template_path` parameter removed.**
The function signature included a `template_path` parameter that was never wired to anything inside the function body. It was removed to avoid misleading callers.

**CLI `--spreadsheet` argument — removed.**
The command-line interface in `process_receipts.py` accepted a positional argument for an Excel template path, but the code never read it after parsing. Removed.

**`generate_spreadsheet` — dead `host_output_path` parameter removed.**
This parameter was accepted but never used inside the function. Removed.

**`/generate-spreadsheet/{job_id}` — legacy alias kept.**
This endpoint is a legacy URL pattern from an earlier version of the API. It is kept for backward compatibility with any scripts that may reference it, but new code should use `POST /generate-spreadsheet`.

**Duplicate `"sunoco"` in `FUEL_VENDORS` — removed.**
The set literal in `process_receipts.py` contained `"sunoco"` twice (lines 71 and 74 in the original). Python sets silently deduplicate, so this had no runtime effect, but the duplicate was removed for clarity.

**`--folder-structure` CLI mode — candidate for future removal.**
This mode reorganizes an existing output folder into the category-prefixed filename convention. It is CLI-only and has no web UI exposure. It is not broken, but it adds surface area for testing. If no one on the team uses it, remove it in a future cleanup pass.
