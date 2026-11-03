import asyncio
import logging
import os
import re
import subprocess
import time
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.config import (
    EXPORTS_DIR,
    TEMP_DIR,
    calibrate_word_times,
    download_clip_segment,
    download_full_source,
    generate_ass_file,
    has_emoji,
    is_valid_mp4,
    logger,
    render_clip_to_mp4,
    render_title_overlay_png,
    transcribe_clip_words,
)
from backend.schemas.render import RenderBatchRequest, RenderSettingsModel

try:
    from backend.video_engine import resolve_ffmpeg_exe as _resolve_ffmpeg_exe
    from backend.video_engine import run_tool as _run_tool
    from backend.video_engine import format_live_download_detail as _format_live_download_detail
    from backend.video_engine import format_live_render_detail as _format_live_render_detail
except ImportError:
    try:
        from video_engine import resolve_ffmpeg_exe as _resolve_ffmpeg_exe
        from video_engine import run_tool as _run_tool
        from video_engine import format_live_download_detail as _format_live_download_detail
        from video_engine import format_live_render_detail as _format_live_render_detail
    except ImportError:
        import shutil as _shutil
        import subprocess as _sp

        def _resolve_ffmpeg_exe() -> str:
            return _shutil.which("ffmpeg") or "ffmpeg"

        def _run_tool(cmd, **kwargs):
            return _sp.run(cmd, **kwargs)

        def _format_live_download_detail(info):
            pct = float((info or {}).get("percent") or 0.0)
            return f"Mengunduh sumber lengkap: {pct:.0f}%"

        def _format_live_render_detail(percent, elapsed_text, total_text, speed):
            speed_part = f", {speed}" if (speed or "").strip() else ""
            return f"Merender: {percent:.0f}% ({elapsed_text}/{total_text}{speed_part})"

RENDER_BATCHES: Dict[str, Dict[str, Any]] = {}
BATCH_REQUESTS: Dict[str, RenderBatchRequest] = {}
# Concurrency gate: serialize video rendering (1 active render job at a time) to prevent CPU starvation on low-spec servers
_RENDER_SEMAPHORE = asyncio.Semaphore(1)


def _append_batch_log(batch: Dict[str, Any], text: str) -> None:
    """v0.0.56: one timestamped LOG OUTPUT line for the batch.

    The batch dict is serialized whole by the /api/render-progress SSE
    stream, so appending here is all it takes for the frontend panel to
    see the line. Lines are capped at 500 so a long batch cannot grow
    the SSE payload without bound. Logging must never break a render,
    so every failure here is swallowed.
    """
    try:
        lines = batch.setdefault("log_lines", [])
        lines.append({"time": datetime.now().strftime("%H:%M:%S"), "text": str(text)})
        del lines[:-500]
    except Exception:
        pass


def _make_download_logger(batch: Dict[str, Any], start_mono: float):
    """v0.0.56: throttled 'Still downloading' log line (screenshot format).

    yt-dlp prints progress lines far more often than a readable log
    wants, so at most one line per 15 seconds is appended, plus the
    100% line. The first progress line always logs (state starts at 0).
    """
    state = {"last": 0.0}

    def _log_dl(info: Dict[str, Any]) -> None:
        try:
            pct = float((info or {}).get("percent") or 0.0)
            now = time.monotonic()
            if pct < 100 and now - state["last"] < 15:
                return
            state["last"] = now
            downloaded = (info or {}).get("downloaded") or ""
            total = (info or {}).get("total") or ""
            speed = (info or {}).get("speed") or ""
            eta = (info or {}).get("eta") or ""
            size_part = f"{downloaded}/{total}" if (downloaded and total) else downloaded
            at_part = f" @ {speed}" if speed else ""
            eta_part = f" eta {eta}" if eta else ""
            _append_batch_log(
                batch,
                f"⏳ Still downloading: {pct:.1f}% ({size_part}{at_part}{eta_part}), {int(now - start_mono)}s elapsed",
            )
        except Exception:
            pass

    return _log_dl


def _batch_output_tier(settings: RenderSettingsModel) -> str:
    """Normalized output-resolution tier for a batch ('1080p' default)."""
    tier = (getattr(settings, "output_resolution", None) or "1080p").strip().lower()
    return tier if tier in ("720p", "480p") else "1080p"


def _resolve_batch_source(
    target_url: str,
    settings: RenderSettingsModel,
    cookies_content: Optional[str] = None,
    progress_callback: Optional[Any] = None
) -> tuple:
    """Resolve the per-clip slice source for a whole batch (v0.0.46).

    Returns (effective_url, force_local). For YouTube URLs the full source
    is downloaded ONCE at the batch tier into the source cache; every clip
    then slices locally from it (no network per clip). On any failure the
    old behaviour is kept: (target_url, False) so each clip downloads its
    own section as before. Local/upload sources are returned unchanged.
    """
    if not target_url.startswith("http") or target_url.startswith("/api/video/"):
        return target_url, False
    # v0.0.55 fast mode: skip the full-source download entirely so each clip
    # downloads only its own segment (the pre-v0.0.46 path). This early
    # return is the single choke point — both batch and retry flows go
    # through here — so no caller can accidentally trigger a full download
    # while fast mode is on.
    if settings is not None and getattr(settings, "fast_mode", False):
        return target_url, False
    try:
        cached = download_full_source(target_url, _batch_output_tier(settings), cookies_content, progress_callback=progress_callback)
        if cached and cached != target_url and os.path.exists(cached):
            return cached, True
    except Exception as ex:
        logger.warning(f"Full-source download failed, falling back to per-clip section downloads: {ex}")
    return target_url, False


async def render_single_batch_clip(
    batch_id: str,
    idx: int,
    clip: Dict[str, Any],
    settings: RenderSettingsModel,
    target_url: str,
    transcript: Optional[List[Dict[str, Any]]] = None,
    total_clips: int = 1,
    cookies_content: Optional[str] = None,
    force_local_slice: bool = False
):
    batch = RENDER_BATCHES.get(batch_id)
    if not batch:
        return

    clip_status = batch["clips"][idx]
    clip_status["status"] = "downloading"
    clip_status["progress_percent"] = 15
    clip_status["detail"] = "Mengunduh & memotong segmen…"
    clip_status["error_message"] = None
    clip_status["error"] = None

    raw_path = None
    ass_path = None
    title_overlay_path = None

    try:
        # 1. Download
        start_t = float(clip.get("start_time", 0.0))
        end_t = float(clip.get("end_time", start_t + 30.0))
        seg_filename = f"{batch_id}_clip_{idx}_raw.mp4"

        # v0.0.56: LOG OUTPUT segment lines (screenshot format). The
        # segment download itself now streams yt-dlp progress (see
        # video_engine.download_clip_segment) so Mode Cepat downloads
        # are no longer log-silent; local/cache slices have no yt-dlp
        # progress, so the cutting line is the visible step there.
        _seg_dl_logger = _make_download_logger(batch, time.monotonic())
        if force_local_slice:
            _append_batch_log(batch, f"[{idx + 1}/{total_clips}] Cutting cached source to requested section...")
        elif target_url.startswith("http"):
            _append_batch_log(batch, f"[{idx + 1}/{total_clips}] Downloading clip segment...")
        else:
            _append_batch_log(batch, f"[{idx + 1}/{total_clips}] Cutting local video to requested section...")
        raw_path = await asyncio.to_thread(
            download_clip_segment,
            target_url,
            start_t,
            end_t,
            seg_filename,
            cookies_content,
            force_local_slice,
            _seg_dl_logger,
        )

        if not raw_path or not os.path.exists(raw_path) or not is_valid_mp4(raw_path):
            raise RuntimeError(
                "Source video segment is incomplete or corrupted ('moov atom not found'). "
                "Network lag interrupted the download. Please retry rendering this clip."
            )
        _append_batch_log(batch, f"[{idx + 1}/{total_clips}] Section ready: {raw_path}")

        # 2. Transcribe & Generate Subtitles / Title (.ass)
        clip_status["status"] = "transcribing"
        clip_status["progress_percent"] = 40
        clip_status["detail"] = "Membuat caption…"
        _append_batch_log(batch, f"[{idx + 1}/{total_clips}] Transcribing clip...")

        display_title = None
        base_title = (
            clip.get("custom_title")
            or clip.get("title_suggestion")
            or clip.get("title")
            or f"Clip {idx+1}"
        ).strip()

        if settings.title_position != "none":
            pfx = settings.title_prefix or ""
            sfx = settings.title_suffix or ""
            if pfx or sfx:
                display_title = f"{pfx}{base_title}{sfx}".strip()
            elif total_clips == 1 and settings.title_text and settings.title_text.strip() and not (clip.get("custom_title") or clip.get("title_suggestion")):
                display_title = settings.title_text.strip()
            else:
                display_title = base_title

        clip_status["title"] = display_title or base_title
        clip_status["base_title"] = base_title

        skip_ass_title = False
        duration_sec = max(1.0, end_t - start_t)

        # Check if title has emoji -> render transparent color emoji PNG overlay
        if display_title and settings.title_position != "none" and has_emoji(display_title):
            title_png_filename = f"{batch_id}_clip_{idx}_title.png"
            title_png_path = str(TEMP_DIR / title_png_filename)
            try:
                canvas_w = 1920 if (settings.aspect_ratio == "16:9_landscape") else 1080
                canvas_h = 1080 if (settings.aspect_ratio == "16:9_landscape") else 1920
                rendered_overlay = await asyncio.to_thread(
                    render_title_overlay_png,
                    title_text=display_title,
                    output_png_path=title_png_path,
                    font_name=settings.title_font or settings.caption_font or "Outfit",
                    target_aspect_ratio=settings.aspect_ratio or "9:16",
                    font_size_preset=settings.title_font_size or settings.font_size or "medium",
                    text_case=settings.title_text_case or settings.text_case or "uppercase",
                    title_position=settings.title_position or "auto",
                    title_y_percent=settings.title_y_percent,
                    canvas_w=canvas_w,
                    canvas_h=canvas_h,
                    title_font_size_preset=settings.title_font_size or settings.font_size or "medium",
                    streamer_preset=settings.streamer_preset or "none",
                    title_font_name=settings.title_font or settings.caption_font or "Outfit",
                    title_font_size_px=settings.title_font_size_px,
                    title_text_case=settings.title_text_case or settings.text_case or "uppercase"
                )
                if rendered_overlay and os.path.exists(rendered_overlay):
                    title_overlay_path = rendered_overlay
                    skip_ass_title = True
                    logger.info(f"Rendered full-color emoji title overlay: {title_overlay_path}")
            except Exception as ex:
                logger.warning(f"Could not render color emoji title overlay: {ex}")
                skip_ass_title = False

        # Detect language from transcript or video context
        target_lang = None
        if transcript:
            try:
                from backend.utils.text import detect_transcript_language
                detected = detect_transcript_language(transcript[:50], display_title or "")
                if detected and detected.get("code") in ("id", "en"):
                    target_lang = detected["code"]
            except Exception:
                pass

        if settings.caption_style != "none" or (display_title and settings.title_position != "none" and not skip_ass_title):
            words = []
            if settings.caption_style != "none":
                words = await asyncio.to_thread(
                    transcribe_clip_words,
                    raw_path,
                    transcript,
                    start_t,
                    end_t,
                    target_lang
                )
                if words:
                    # Auto-calibrate caption timing against the slice audio
                    # (corrects a systematic caption delay, no-op if synced).
                    words, _cal_delta = await asyncio.to_thread(
                        calibrate_word_times,
                        raw_path,
                        words
                    )
            ass_filename = f"{batch_id}_clip_{idx}.ass"
            ass_path = str(TEMP_DIR / ass_filename)
            await asyncio.to_thread(
                generate_ass_file,
                words=words,
                style_preset=settings.caption_style,
                font_name=settings.caption_font or "Outfit",
                output_ass_path=ass_path,
                target_aspect_ratio=settings.aspect_ratio,
                font_size_preset=settings.font_size,
                text_case=settings.text_case,
                title_text=display_title if not skip_ass_title else None,
                title_position=settings.title_position,
                title_duration=settings.title_duration if settings.title_duration else "entire",
                duration_seconds=duration_sec,
                title_y_percent=settings.title_y_percent,
                subtitle_y_percent=settings.subtitle_y_percent,
                subtitle_position_mode=settings.subtitle_position_mode if settings.subtitle_position_mode else "bottom",
                subtitle_center_y_percent=settings.subtitle_center_y_percent if settings.subtitle_center_y_percent is not None else 50.0,
                skip_title=skip_ass_title,
                title_font_size_preset=settings.title_font_size or settings.font_size or "medium",
                streamer_preset=settings.streamer_preset or "none",
                title_font_name=settings.title_font or settings.caption_font or "Outfit",
                font_size_px=settings.font_size_px,
                title_font_size_px=settings.title_font_size_px,
                title_text_case=settings.title_text_case or settings.text_case or "uppercase"
            )

        # v0.0.56: LOG OUTPUT caption line. Transcribing has no honest
        # percent (v0.0.49 rule), so only the finished word count is
        # logged, never a fabricated progress number.
        try:
            if words:
                _append_batch_log(batch, f"[{idx + 1}/{total_clips}] Caption ready ({len(words)} kata)")
        except NameError:
            pass

        # 3. Render Final Vertical MP4
        clip_status["status"] = "rendering"
        clip_status["progress_percent"] = 70
        clip_status["detail"] = "Merender video…"
        _append_batch_log(batch, f"[{idx + 1}/{total_clips}] Rendering clip: {settings.aspect_ratio}, {_batch_output_tier(settings)}")

        # v0.0.49: live render detail (ffmpeg time/speed) into the mini log.
        def _render_progress(info):
            try:
                pct = float((info or {}).get("percent") or 0.0)
                clip_status["progress_percent"] = 70 + int(round(pct * 0.3))
                clip_status["detail"] = _format_live_render_detail(
                    pct,
                    (info or {}).get("downloaded") or "00:00",
                    (info or {}).get("total") or "00:00",
                    (info or {}).get("speed") or "",
                )
            except Exception:
                pass

        out_tier = _batch_output_tier(settings)
        out_suffix = "_720p" if out_tier == "720p" else ("_480p" if out_tier == "480p" else "")
        out_filename = f"clip_{idx+1}_{batch_id}{out_suffix}.mp4"
        out_path = str(EXPORTS_DIR / out_filename)

        await asyncio.to_thread(
            render_clip_to_mp4,
            video_path=raw_path,
            output_mp4_path=out_path,
            aspect_ratio=settings.aspect_ratio,
            background_style=settings.background_style,
            enable_face_tracking=settings.enable_face_tracking,
            streamer_preset=settings.streamer_preset,
            facecam_position=getattr(settings, "facecam_position", "auto") or "auto",
            title_text=display_title if not skip_ass_title else None,
            title_position=settings.title_position,
            ass_subtitles_path=ass_path,
            clip_duration=duration_sec,
            title_overlay_path=title_overlay_path,
            title_duration=settings.title_duration if settings.title_duration else "entire",
            watermark_enabled=bool(settings.watermark_enabled),
            watermark_type=settings.watermark_type or "image",
            watermark_image_path=settings.watermark_file_path,
            watermark_text=settings.watermark_text,
            watermark_size=float(settings.watermark_size if settings.watermark_size is not None else 20.0),
            watermark_opacity=float((settings.watermark_opacity if settings.watermark_opacity is not None else 80.0) / 100.0),
            watermark_x_percent=float(settings.watermark_x if settings.watermark_x is not None else 90.0),
            watermark_y_percent=float(settings.watermark_y if settings.watermark_y is not None else 8.0),
            bgm_enabled=bool(settings.bgm_enabled),
            bgm_path=settings.bgm_file_path,
            bgm_volume=float((settings.bgm_volume if settings.bgm_volume is not None else 25.0) / 100.0),
            bgm_start_offset=float(settings.bgm_start_offset or 0.0),
            hook_sfx_enabled=bool(settings.hook_sfx_enabled),
            hook_sfx_path=settings.hook_sfx_file_path,
            hook_sfx_volume=float((settings.hook_sfx_volume if settings.hook_sfx_volume is not None else 100.0) / 100.0),
            original_audio_volume=float((settings.original_audio_volume if settings.original_audio_volume is not None else 100.0) / 100.0),
            hardware_accel=settings.hardware_accel or "auto",
            title_y_percent=settings.title_y_percent,
            facecam_video_path=getattr(settings, 'facecam_video_path', None),
            facecam_crop_size=getattr(settings, 'facecam_crop_size', (320, 240)),
            facecam_x=getattr(settings, 'facecam_x', 736),
            facecam_y=getattr(settings, 'facecam_y', 120),
            split_screen_7030=bool(getattr(settings, 'split_screen_7030', False)),
            split_bottom_video_path=getattr(settings, 'split_bottom_video_path', None),
            split_bottom_volume=float((settings.split_bottom_volume if settings.split_bottom_volume is not None else 100.0) / 100.0),
            output_resolution=_batch_output_tier(settings),
            progress_callback=_render_progress,
            # v0.0.56: encoder / face-trajectory lines go to LOG OUTPUT
            # via the same batch log channel; the callback only appends,
            # so a logging failure can never break the render.
            log_callback=lambda text: _append_batch_log(batch, f"[{idx + 1}/{total_clips}] {text}"),
        )

        if not os.path.exists(out_path) or not is_valid_mp4(out_path):
            raise RuntimeError("Rendered MP4 file is incomplete or missing. Please retry rendering.")
        _append_batch_log(batch, f"[{idx + 1}/{total_clips}] Render selesai: {out_path}")

        clip_status["status"] = "completed"
        clip_status["progress_percent"] = 100
        clip_status["detail"] = ""
        clip_status["download_url"] = f"/api/download-rendered/{out_filename}"
        clip_status["output_path"] = out_path

    except Exception as e:
        logger.error(f"Error rendering clip {idx} in batch {batch_id}: {e}")
        clip_status["status"] = "error"
        clip_status["detail"] = ""
        err_msg = str(e)
        if "moov atom not found" in err_msg.lower():
            err_msg = "Download interrupted by internet lag ('moov atom not found'). Click Retry to re-download."
        elif "timed out" in err_msg.lower() or "timeout" in err_msg.lower():
            if clip_status.get("status") == "rendering":
                err_msg = "Video rendering timed out. Try switching to 'Universal CPU (libx264)' in Studio Settings or retry."
            else:
                err_msg = "Video download timed out due to slow/laggy internet connection. Click Retry to try again."
        clip_status["error_message"] = err_msg
        clip_status["error"] = err_msg
        _append_batch_log(batch, f"[{idx + 1}/{total_clips}] Gagal: {err_msg}")

        # Clean up any partial raw video
        if raw_path and os.path.exists(raw_path):
            try:
                os.unlink(raw_path)
            except Exception:
                pass


def update_batch_summary_and_zip(batch_id: str, settings: RenderSettingsModel):
    """
    Updates the batch ZIP archive and computes overall status and friendly messages.
    """
    batch = RENDER_BATCHES.get(batch_id)
    if not batch:
        return

    # Generate/update ZIP bundle for the batch with title-based filenames and duplicate handling
    try:
        completed_clips = [c for c in batch["clips"] if c.get("status") == "completed" and c.get("download_url")]
        if completed_clips:
            zip_filename = f"cheat_clip_pro_{batch_id}.zip"
            zip_path = EXPORTS_DIR / zip_filename
            title_counts: Dict[str, int] = {}
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for c in completed_clips:
                    fname = c["download_url"].split("?")[0].split("/")[-1]
                    fpath = EXPORTS_DIR / fname
                    if fpath.exists():
                        raw_title = (c.get("base_title") or c.get("title") or "").strip()
                        clean_title = re.sub(r'[\\/*?:"<>|]', "", raw_title) or f"clip_{c.get('clip_index', 1)}"
                        fn_pfx = re.sub(r'[\\/*?:"<>|]', "", settings.file_name_prefix or "")
                        fn_sfx = re.sub(r'[\\/*?:"<>|]', "", settings.file_name_suffix or "")
                        formatted_name = f"{fn_pfx}{clean_title}{fn_sfx}".strip() or clean_title
                        count = title_counts.get(formatted_name, 0)
                        title_counts[formatted_name] = count + 1
                        arc_name = f"{formatted_name}.mp4" if count == 0 else f"{formatted_name} ({count}).mp4"
                        zipf.write(fpath, arcname=arc_name)
            batch["zip_url"] = f"/api/download-batch-zip/{batch_id}"
        else:
            batch["zip_url"] = None
    except Exception as e:
        logger.warning(f"Failed to create batch zip: {e}")

    # Determine overall status and error messaging
    failed_clips = [c for c in batch["clips"] if c.get("status") == "error"]
    running_clips = [c for c in batch["clips"] if c.get("status") in ["downloading", "transcribing", "rendering", "pending"]]

    if running_clips:
        batch["overall_status"] = "running"
    elif len(failed_clips) == len(batch["clips"]):
        batch["overall_status"] = "error"
        batch["error_message"] = f"All {len(batch['clips'])} clip(s) failed. You can click 'Retry' to try again."
    elif len(failed_clips) > 0:
        batch["overall_status"] = "completed"
        batch["warning_message"] = f"{len(failed_clips)} of {len(batch['clips'])} clips encountered errors. You can retry failed clips anytime."
    else:
        batch["overall_status"] = "completed"
        batch["warning_message"] = None


async def render_merged_batch_clips(
    batch_id: str,
    clips: List[Dict[str, Any]],
    settings: RenderSettingsModel,
    target_url: str,
    transcript: Optional[List[Dict[str, Any]]] = None,
    cookies_content: Optional[str] = None,
    force_local_slice: bool = False
):
    batch = RENDER_BATCHES.get(batch_id)
    if not batch:
        return

    clip_status = batch["clips"][0]
    clip_status["status"] = "downloading"
    clip_status["progress_percent"] = 10
    clip_status["detail"] = "Mengunduh segmen…"
    clip_status["error_message"] = None
    clip_status["error"] = None

    part_paths: List[str] = []
    raw_merged_path: Optional[str] = None
    ass_path: Optional[str] = None
    title_overlay_path: Optional[str] = None

    try:
        total_segments = len(clips)
        # 1. Download / slice each segment
        for i, seg in enumerate(clips):
            clip_status["progress_percent"] = 10 + int((i / max(1, total_segments)) * 25)
            clip_status["detail"] = f"Mengunduh segmen {i + 1}/{total_segments}…"
            s_t = float(seg.get("start_time", 0.0))
            e_t = float(seg.get("end_time", s_t + 30.0))
            part_filename = f"{batch_id}_part_{i}_raw.mp4"
            p_path = await asyncio.to_thread(
                download_clip_segment,
                target_url,
                s_t,
                e_t,
                part_filename,
                cookies_content,
                force_local_slice
            )
            if not p_path or not os.path.exists(p_path) or not is_valid_mp4(p_path):
                raise RuntimeError(
                    f"Segment #{i+1} ({s_t:.1f}s - {e_t:.1f}s) is incomplete or corrupted. Please retry rendering."
                )
            part_paths.append(p_path)

        # 2. Concatenate raw video segments
        clip_status["status"] = "transcribing"
        clip_status["progress_percent"] = 40
        clip_status["detail"] = "Membuat caption…"

        raw_merged_filename = f"{batch_id}_merged_raw.mp4"
        raw_merged_path = str(TEMP_DIR / raw_merged_filename)

        if len(part_paths) == 1:
            raw_merged_path = part_paths[0]
        else:
            inputs = []
            filter_parts = []
            for idx_p, p in enumerate(part_paths):
                inputs.extend(["-i", p])
                filter_parts.append(f"[{idx_p}:v][{idx_p}:a]")
            filter_str = f"{''.join(filter_parts)}concat=n={len(part_paths)}:v=1:a=1[v][a]"
            concat_cmd = [
                _resolve_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
                *inputs,
                "-filter_complex", filter_str,
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                raw_merged_path
            ]
            concat_timeout = max(180, total_segments * 60)
            logger.info(f"Concatenating {len(part_paths)} segments into {raw_merged_path}")
            await asyncio.to_thread(_run_tool, concat_cmd, check=True, timeout=concat_timeout)

        # 3. Transcribe & generate time-shifted subtitles
        target_lang = None
        if transcript:
            try:
                from backend.utils.text import detect_transcript_language
                detected = detect_transcript_language(transcript[:50])
                if detected and detected.get("code") in ("id", "en"):
                    target_lang = detected["code"]
            except Exception:
                pass

        total_duration_sec = 0.0
        merged_words = []
        for i, seg in enumerate(clips):
            s_t = float(seg.get("start_time", 0.0))
            e_t = float(seg.get("end_time", s_t + 30.0))
            seg_dur = max(0.1, e_t - s_t)
            if settings.caption_style != "none":
                seg_words = await asyncio.to_thread(
                    transcribe_clip_words,
                    part_paths[i],
                    transcript,
                    s_t,
                    e_t,
                    target_lang
                )
                for w in seg_words:
                    w_copy = dict(w)
                    w_start = float(w.get("start", 0.0))
                    w_end = float(w.get("end", w_start + 0.15))
                    w_copy["start"] = round(total_duration_sec + max(0.0, w_start), 3)
                    w_copy["end"] = round(max(w_copy["start"] + 0.1, total_duration_sec + max(0.0, w_end)), 3)
                    merged_words.append(w_copy)
            total_duration_sec += seg_dur

        total_duration_sec = max(1.0, total_duration_sec)

        if settings.caption_style != "none" and merged_words:
            # Auto-calibrate caption timing against the merged clip audio
            # (same per-clip correction as the single-clip path).
            merged_words, _cal_delta = await asyncio.to_thread(
                calibrate_word_times,
                raw_merged_path,
                merged_words
            )

        display_title = clip_status.get("title") or clip_status.get("base_title") or "Highlight Video"
        skip_ass_title = False

        if display_title and settings.title_position != "none" and has_emoji(display_title):
            title_png_filename = f"{batch_id}_merged_title.png"
            title_png_path = str(TEMP_DIR / title_png_filename)
            try:
                canvas_w = 1920 if (settings.aspect_ratio == "16:9_landscape") else 1080
                canvas_h = 1080 if (settings.aspect_ratio == "16:9_landscape") else 1920
                rendered_overlay = await asyncio.to_thread(
                    render_title_overlay_png,
                    title_text=display_title,
                    output_png_path=title_png_path,
                    font_name=settings.title_font or settings.caption_font or "Outfit",
                    target_aspect_ratio=settings.aspect_ratio or "9:16",
                    font_size_preset=settings.title_font_size or settings.font_size or "medium",
                    text_case=settings.title_text_case or settings.text_case or "uppercase",
                    title_position=settings.title_position or "auto",
                    title_y_percent=settings.title_y_percent,
                    canvas_w=canvas_w,
                    canvas_h=canvas_h,
                    title_font_size_preset=settings.title_font_size or settings.font_size or "medium",
                    streamer_preset=settings.streamer_preset or "none",
                    title_font_name=settings.title_font or settings.caption_font or "Outfit",
                    title_font_size_px=settings.title_font_size_px,
                    title_text_case=settings.title_text_case or settings.text_case or "uppercase"
                )
                if rendered_overlay and os.path.exists(rendered_overlay):
                    title_overlay_path = rendered_overlay
                    skip_ass_title = True
            except Exception as ex:
                logger.warning(f"Could not render color emoji title overlay for merged clip: {ex}")
                skip_ass_title = False

        if settings.caption_style != "none" or (display_title and settings.title_position != "none" and not skip_ass_title):
            ass_filename = f"{batch_id}_merged.ass"
            ass_path = str(TEMP_DIR / ass_filename)
            await asyncio.to_thread(
                generate_ass_file,
                words=merged_words,
                style_preset=settings.caption_style,
                font_name=settings.caption_font or "Outfit",
                output_ass_path=ass_path,
                target_aspect_ratio=settings.aspect_ratio,
                font_size_preset=settings.font_size,
                text_case=settings.text_case,
                title_text=display_title if not skip_ass_title else None,
                title_position=settings.title_position,
                title_duration=settings.title_duration if settings.title_duration else "entire",
                duration_seconds=total_duration_sec,
                title_y_percent=settings.title_y_percent,
                subtitle_y_percent=settings.subtitle_y_percent,
                subtitle_position_mode=settings.subtitle_position_mode if settings.subtitle_position_mode else "bottom",
                subtitle_center_y_percent=settings.subtitle_center_y_percent if settings.subtitle_center_y_percent is not None else 50.0,
                skip_title=skip_ass_title,
                title_font_size_preset=settings.title_font_size or settings.font_size or "medium",
                streamer_preset=settings.streamer_preset or "none",
                title_font_name=settings.title_font or settings.caption_font or "Outfit",
                font_size_px=settings.font_size_px,
                title_font_size_px=settings.title_font_size_px,
                title_text_case=settings.title_text_case or settings.text_case or "uppercase"
            )

        # 4. Render Final Single MP4
        clip_status["status"] = "rendering"
        clip_status["progress_percent"] = 65
        clip_status["detail"] = "Merender video…"

        # v0.0.49: live render detail (ffmpeg time/speed) into the mini log.
        def _merged_render_progress(info):
            try:
                pct = float((info or {}).get("percent") or 0.0)
                clip_status["progress_percent"] = 65 + int(round(pct * 0.35))
                clip_status["detail"] = _format_live_render_detail(
                    pct,
                    (info or {}).get("downloaded") or "00:00",
                    (info or {}).get("total") or "00:00",
                    (info or {}).get("speed") or "",
                )
            except Exception:
                pass

        clean_safe_title = re.sub(r'[\\/*?:"<>|]', "", clip_status.get("base_title") or "highlight_compilation")
        clean_safe_title = clean_safe_title.replace(" ", "_")[:40] or "compilation"
        out_tier = _batch_output_tier(settings)
        out_suffix = "_720p" if out_tier == "720p" else ("_480p" if out_tier == "480p" else "")
        out_filename = f"compilation_{clean_safe_title}_{batch_id}{out_suffix}.mp4"
        out_path = str(EXPORTS_DIR / out_filename)

        await asyncio.to_thread(
            render_clip_to_mp4,
            video_path=raw_merged_path,
            output_mp4_path=out_path,
            aspect_ratio=settings.aspect_ratio,
            background_style=settings.background_style,
            enable_face_tracking=settings.enable_face_tracking,
            streamer_preset=settings.streamer_preset,
            facecam_position=getattr(settings, "facecam_position", "auto") or "auto",
            title_text=display_title if not skip_ass_title else None,
            title_position=settings.title_position,
            ass_subtitles_path=ass_path,
            clip_duration=total_duration_sec,
            title_overlay_path=title_overlay_path,
            title_duration=settings.title_duration if settings.title_duration else "entire",
            watermark_enabled=bool(settings.watermark_enabled),
            watermark_type=settings.watermark_type or "image",
            watermark_image_path=settings.watermark_file_path,
            watermark_text=settings.watermark_text,
            watermark_size=float(settings.watermark_size if settings.watermark_size is not None else 20.0),
            watermark_opacity=float((settings.watermark_opacity if settings.watermark_opacity is not None else 80.0) / 100.0),
            watermark_x_percent=float(settings.watermark_x if settings.watermark_x is not None else 90.0),
            watermark_y_percent=float(settings.watermark_y if settings.watermark_y is not None else 8.0),
            bgm_enabled=bool(settings.bgm_enabled),
            bgm_path=settings.bgm_file_path,
            bgm_volume=float((settings.bgm_volume if settings.bgm_volume is not None else 25.0) / 100.0),
            bgm_start_offset=float(settings.bgm_start_offset or 0.0),
            hook_sfx_enabled=bool(settings.hook_sfx_enabled),
            hook_sfx_path=settings.hook_sfx_file_path,
            hook_sfx_volume=float((settings.hook_sfx_volume if settings.hook_sfx_volume is not None else 100.0) / 100.0),
            original_audio_volume=float((settings.original_audio_volume if settings.original_audio_volume is not None else 100.0) / 100.0),
            hardware_accel=settings.hardware_accel or "auto",
            title_y_percent=settings.title_y_percent,
            facecam_video_path=getattr(settings, 'facecam_video_path', None),
            facecam_crop_size=getattr(settings, 'facecam_crop_size', (320, 240)),
            facecam_x=getattr(settings, 'facecam_x', 736),
            facecam_y=getattr(settings, 'facecam_y', 120),
            split_screen_7030=bool(getattr(settings, 'split_screen_7030', False)),
            split_bottom_video_path=getattr(settings, 'split_bottom_video_path', None),
            split_bottom_volume=float((settings.split_bottom_volume if settings.split_bottom_volume is not None else 100.0) / 100.0),
            output_resolution=_batch_output_tier(settings),
            progress_callback=_merged_render_progress,
        )

        if not os.path.exists(out_path) or not is_valid_mp4(out_path):
            raise RuntimeError("Merged MP4 file is incomplete or missing. Please retry rendering.")

        clip_status["status"] = "completed"
        clip_status["progress_percent"] = 100
        clip_status["detail"] = ""
        clip_status["download_url"] = f"/api/download-rendered/{out_filename}"
        clip_status["output_path"] = out_path

    except Exception as e:
        logger.error(f"Error rendering merged batch {batch_id}: {e}")
        clip_status["status"] = "error"
        clip_status["detail"] = ""
        err_msg = str(e)
        clip_status["error_message"] = err_msg
        clip_status["error"] = err_msg

    finally:
        for p in part_paths:
            if p and os.path.exists(p) and p != raw_merged_path:
                try:
                    os.unlink(p)
                except Exception:
                    pass
        if raw_merged_path and os.path.exists(raw_merged_path):
            try:
                os.unlink(raw_merged_path)
            except Exception:
                pass


async def process_batch_rendering(batch_id: str, request: RenderBatchRequest):
    async with _RENDER_SEMAPHORE:
        batch = RENDER_BATCHES.get(batch_id)
        if not batch:
            return

        clips = request.clips
        settings = request.settings

        # Normalize video URL for history or direct URL
        target_url = (request.video_url or "").strip()
        if "yt_audio_" in target_url:
            match_yt = re.search(r'yt_audio_([a-zA-Z0-9_-]{11})', target_url)
            if match_yt:
                target_url = f"https://www.youtube.com/watch?v={match_yt.group(1)}"
        elif request.video_id and (request.video_id.startswith("gdrive_") or request.video_id.startswith("upload_")):
            if not target_url.startswith("/api/video/"):
                target_url = f"/api/video/{request.video_id}"
        elif not target_url:
            if request.video_id:
                target_url = f"https://www.youtube.com/watch?v={request.video_id}"
        elif not target_url.startswith("http") and not target_url.startswith("/api/video/"):
            target_url = f"https://www.youtube.com/watch?v={target_url}"

        cookies_content = getattr(request, "cookies", None)
        is_merged = bool(settings and getattr(settings, "render_mode", "separate") == "merged")
        # v0.0.56: LOG OUTPUT start lines (screenshot format). The cookies
        # line reports only whether batch cookies were supplied, never
        # their content; the app stores cookies in its own storage, not
        # as a Cliperpro-style cookies.txt path, so no path is printed.
        _batch_log_start = time.monotonic()
        _append_batch_log(batch, f"Batch started - {len(clips)} clips")
        _append_batch_log(
            batch,
            "Cookies: aktif (tersimpan di aplikasi)" if cookies_content else "Cookies: tidak ada (mode tamu)",
        )
        # v0.0.49: stream the full-source download progress into clip 0's
        # live log detail (status/detail keys already exist, so the SSE
        # stream picks the change up within ~0.5s).
        _dl_logger = _make_download_logger(batch, _batch_log_start)

        def _full_source_progress(info):
            try:
                clip0 = batch["clips"][0]
                clip0["status"] = "downloading"
                pct = float((info or {}).get("percent") or 0.0)
                clip0["progress_percent"] = 15 if pct >= 100 else max(1, min(15, int(round(pct * 0.15))))
                clip0["detail"] = _format_live_download_detail(info or {})
            except Exception:
                pass
            _dl_logger(info or {})
        # v0.0.53: the resolve phase used to be invisible — clips stayed
        # "pending" until yt-dlp printed its first progress line, so a
        # resolving hang looked identical to "doing nothing" for 10+ min.
        # Flip clip 0 to downloading NOW so the user sees the batch is
        # actually contacting YouTube; the progress callback then replaces
        # this text with real numbers as soon as lines arrive.
        # v0.0.55: in fast mode there is no full-source resolve at all, so
        # the detail must not promise one.
        try:
            if batch["clips"]:
                batch["clips"][0]["status"] = "downloading"
                batch["clips"][0]["progress_percent"] = 1
                if settings is not None and getattr(settings, "fast_mode", False):
                    batch["clips"][0]["detail"] = "Mengunduh segmen klip saja… (tanpa unduh video penuh)"
                else:
                    batch["clips"][0]["detail"] = "Menghubungi YouTube… (sumber penuh diunduh sekali, lalu semua klip dipotong dari cache)"
        except Exception:
            pass
        # v0.0.46 download-once: resolve the full source a single time; all
        # clips then slice locally from it. Falls back to per-clip downloads.
        # v0.0.55 fast mode: _resolve_batch_source returns (target_url, False)
        # immediately, so each clip downloads only its own segment.
        slice_url, force_local_slice = await asyncio.to_thread(
            _resolve_batch_source, target_url, settings, cookies_content, _full_source_progress
        )
        if is_merged:
            batch["current_clip_index"] = 0
            await render_merged_batch_clips(
                batch_id=batch_id,
                clips=clips,
                settings=settings,
                target_url=slice_url,
                transcript=request.transcript,
                cookies_content=cookies_content,
                force_local_slice=force_local_slice
            )
            batch["current_clip_index"] = 1
        else:
            for idx, clip in enumerate(clips):
                batch["current_clip_index"] = idx
                await render_single_batch_clip(
                    batch_id=batch_id,
                    idx=idx,
                    clip=clip,
                    settings=settings,
                    target_url=slice_url,
                    transcript=request.transcript,
                    total_clips=len(clips),
                    cookies_content=cookies_content,
                    force_local_slice=force_local_slice
                )
                batch["current_clip_index"] = idx + 1

        # v0.0.56: LOG OUTPUT finish line.
        _failed = sum(1 for c in batch.get("clips", []) if c.get("status") == "error")
        _append_batch_log(
            batch,
            f"Batch selesai - {len(clips) - _failed}/{len(clips)} klip berhasil" if _failed == 0
            else f"Batch selesai - {_failed} dari {len(clips)} klip gagal",
        )
        update_batch_summary_and_zip(batch_id, settings)


async def process_batch_retry(batch_id: str, clip_indices: List[int]):
    async with _RENDER_SEMAPHORE:
        batch = RENDER_BATCHES.get(batch_id)
        request = BATCH_REQUESTS.get(batch_id)
        if not batch or not request:
            logger.error(f"Cannot retry batch {batch_id}: batch or request data not found")
            return

        batch["overall_status"] = "running"
        clips = request.clips
        settings = request.settings
        target_url = (request.video_url or "").strip()
        if "yt_audio_" in target_url:
            match_yt = re.search(r'yt_audio_([a-zA-Z0-9_-]{11})', target_url)
            if match_yt:
                target_url = f"https://www.youtube.com/watch?v={match_yt.group(1)}"
        elif request.video_id and (request.video_id.startswith("gdrive_") or request.video_id.startswith("upload_")):
            if not target_url.startswith("/api/video/"):
                target_url = f"/api/video/{request.video_id}"
        elif not target_url:
            if request.video_id:
                target_url = f"https://www.youtube.com/watch?v={request.video_id}"
        elif not target_url.startswith("http") and not target_url.startswith("/api/video/"):
            target_url = f"https://www.youtube.com/watch?v={target_url}"

        cookies_content = getattr(request, "cookies", None)
        is_merged = bool(batch.get("is_merged")) or bool(settings and getattr(settings, "render_mode", "separate") == "merged")
        # v0.0.56: LOG OUTPUT retry lines; history from the first run is
        # kept (log_lines is never reset), so the retry continues the
        # same panel instead of replacing it.
        _batch_log_start = time.monotonic()
        _append_batch_log(batch, f"Retry started - {len(clip_indices)} klip")
        _dl_logger = _make_download_logger(batch, _batch_log_start)
        # v0.0.49: same live download detail as a fresh batch.
        def _full_source_progress(info):
            try:
                clip0 = batch["clips"][0]
                clip0["status"] = "downloading"
                pct = float((info or {}).get("percent") or 0.0)
                clip0["progress_percent"] = 15 if pct >= 100 else max(1, min(15, int(round(pct * 0.15))))
                clip0["detail"] = _format_live_download_detail(info or {})
            except Exception:
                pass
            _dl_logger(info or {})
        # v0.0.53: same resolve-phase visibility as a fresh batch — see above.
        # v0.0.55: in fast mode there is no full-source resolve at all.
        try:
            if batch["clips"]:
                batch["clips"][0]["status"] = "downloading"
                batch["clips"][0]["progress_percent"] = 1
                if settings is not None and getattr(settings, "fast_mode", False):
                    batch["clips"][0]["detail"] = "Mengunduh segmen klip saja… (tanpa unduh video penuh)"
                else:
                    batch["clips"][0]["detail"] = "Menghubungi YouTube… (sumber penuh diunduh sekali, lalu semua klip dipotong dari cache)"
        except Exception:
            pass
        # v0.0.46 download-once: same single-source resolution as a fresh batch.
        # v0.0.55 fast mode: _resolve_batch_source returns (target_url, False).
        slice_url, force_local_slice = await asyncio.to_thread(
            _resolve_batch_source, target_url, settings, cookies_content, _full_source_progress
        )
        if is_merged:
            batch["current_clip_index"] = 0
            await render_merged_batch_clips(
                batch_id=batch_id,
                clips=clips,
                settings=settings,
                target_url=slice_url,
                transcript=request.transcript,
                cookies_content=cookies_content,
                force_local_slice=force_local_slice
            )
            batch["current_clip_index"] = 1
        else:
            for idx in clip_indices:
                if 0 <= idx < len(clips):
                    batch["current_clip_index"] = idx
                    await render_single_batch_clip(
                        batch_id=batch_id,
                        idx=idx,
                        clip=clips[idx],
                        settings=settings,
                        target_url=slice_url,
                        transcript=request.transcript,
                        total_clips=len(clips),
                        cookies_content=cookies_content,
                        force_local_slice=force_local_slice
                    )

        # v0.0.56: LOG OUTPUT retry finish line.
        _failed = sum(1 for c in batch.get("clips", []) if c.get("status") == "error")
        _append_batch_log(
            batch,
            f"Retry selesai - {len(clip_indices) - _failed}/{len(clip_indices)} klip berhasil" if _failed == 0
            else f"Retry selesai - {_failed} dari {len(clip_indices)} klip gagal",
        )
        update_batch_summary_and_zip(batch_id, settings)
