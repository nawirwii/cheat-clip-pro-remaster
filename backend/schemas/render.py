from typing import List, Optional, Dict, Any
from pydantic import BaseModel

class RenderSettingsModel(BaseModel):
    aspect_ratio: str = "9:16"
    background_style: str = "black"
    enable_face_tracking: bool = True
    streamer_preset: str = "none"
    facecam_position: Optional[str] = "auto"
    title_text: Optional[str] = None
    title_prefix: Optional[str] = ""
    title_suffix: Optional[str] = ""
    file_name_prefix: Optional[str] = ""
    file_name_suffix: Optional[str] = ""
    title_position: str = "auto"
    title_duration: Optional[str] = "entire"
    subtitles_enabled: Optional[bool] = True
    caption_style: str = "viral_pop"
    caption_font: str = "Outfit"
    title_font: Optional[str] = "Outfit"
    font_size: str = "medium"
    title_font_size: Optional[str] = "medium"
    font_size_px: Optional[int] = None
    title_font_size_px: Optional[int] = None
    text_case: str = "uppercase"
    title_text_case: Optional[str] = "uppercase"
    title_y_percent: Optional[float] = None
    subtitle_y_percent: Optional[float] = None
    subtitle_position_mode: Optional[str] = "bottom"
    subtitle_center_y_percent: Optional[float] = 50.0
    # Background Music
    bgm_enabled: Optional[bool] = False
    bgm_file_path: Optional[str] = None
    bgm_volume: Optional[float] = 25.0
    bgm_start_offset: Optional[float] = 0.0
    # Hook SFX
    hook_sfx_enabled: Optional[bool] = False
    hook_sfx_file_path: Optional[str] = None
    hook_sfx_volume: Optional[float] = 100.0
    # Video Hook (v0.0.61): an intro segment from an uploaded recording,
    # prepended to every clip in the batch with a 0.5 s fade into the
    # main clip. Distinct from Hook SFX, which is only a sound.
    # Effective only when enabled AND a file path is supplied.
    # v0.0.68: the hook is the full uploaded recording — no fixed
    # duration choice any more, so there is no duration field.
    video_hook_enabled: Optional[bool] = False
    video_hook_file_path: Optional[str] = None
    # Raw Audio / Voice Boost
    original_audio_volume: Optional[float] = 100.0
    # Watermark
    watermark_enabled: Optional[bool] = False
    watermark_type: Optional[str] = "image"
    watermark_file_path: Optional[str] = None
    watermark_text: Optional[str] = None
    watermark_size: Optional[float] = 20.0
    watermark_opacity: Optional[float] = 80.0
    watermark_x: Optional[float] = 90.0
    watermark_y: Optional[float] = 8.0
    hardware_accel: Optional[str] = "auto"
    # Multi-Segment Merged Highlight Video
    render_mode: Optional[str] = "separate"  # "separate" | "merged"
    compilation_title: Optional[str] = None
    # Facecam / Second Video for Split Screen
    facecam_video_path: Optional[str] = None  # Path to facecam/video2 file for split screen mode
    # Split Screen 70/30 (NEW, independent from split_top_cam): top 70% composed clip,
    # bottom 30% local landscape video (looped). Volume in percent, 0-200, default 100.
    split_screen_7030: Optional[bool] = False
    split_bottom_video_path: Optional[str] = None
    split_bottom_volume: Optional[float] = 100.0
    # Output resolution tier (v0.0.46): '1080p' | '720p' | '480p'. Drives the
    # final render scale and the one-time source download height. Default
    # keeps the old 1080p behaviour exactly.
    output_resolution: Optional[str] = "1080p"
    # Fast mode (v0.0.55): when True, skip the v0.0.46 full-source download
    # entirely — every clip downloads only its own segment (the pre-v0.0.46
    # path). Fastest for a single clip; default False keeps download-once.
    fast_mode: Optional[bool] = False

class RenderBatchRequest(BaseModel):
    video_url: str
    video_id: str
    clips: List[Dict[str, Any]]
    settings: RenderSettingsModel
    transcript: Optional[List[Dict[str, Any]]] = None
    cookies: Optional[str] = None

class RetryBatchRequest(BaseModel):
    clip_indices: Optional[List[int]] = None
    cookies: Optional[str] = None
