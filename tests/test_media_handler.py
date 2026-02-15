"""Tests for media handler."""

import asyncio
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_forge.media_handler import (
    AUDIO_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    MediaHandler,
    MediaType,
)


@pytest.fixture
def handler(tmp_path):
    """Create a MediaHandler with a temp directory."""
    return MediaHandler(temp_dir=str(tmp_path / "media-tmp"))


@pytest.fixture
def worktree(tmp_path):
    """Create a temp worktree directory."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    return str(wt)


class TestDetectType:
    def test_detect_type_image(self, handler):
        assert handler._detect_type("photo.png") == MediaType.IMAGE
        assert handler._detect_type("photo.jpg") == MediaType.IMAGE
        assert handler._detect_type("photo.JPEG") == MediaType.IMAGE
        assert handler._detect_type("image.webp") == MediaType.IMAGE

    def test_detect_type_video(self, handler):
        assert handler._detect_type("clip.mp4") == MediaType.VIDEO
        assert handler._detect_type("clip.mov") == MediaType.VIDEO
        assert handler._detect_type("clip.MKV") == MediaType.VIDEO

    def test_detect_type_audio(self, handler):
        assert handler._detect_type("voice.ogg") == MediaType.AUDIO
        assert handler._detect_type("song.mp3") == MediaType.AUDIO
        assert handler._detect_type("track.WAV") == MediaType.AUDIO

    def test_detect_type_document(self, handler):
        assert handler._detect_type("report.pdf") == MediaType.DOCUMENT
        assert handler._detect_type("notes.txt") == MediaType.DOCUMENT
        assert handler._detect_type("data.csv") == MediaType.DOCUMENT

    def test_detect_type_unknown(self, handler):
        assert handler._detect_type("file.xyz") == MediaType.DOCUMENT
        assert handler._detect_type("noext") == MediaType.DOCUMENT


class TestBuildMediaReference:
    def test_build_media_reference_image(self, handler):
        ref = handler.build_media_reference(
            [".media/123_photo.png"], MediaType.IMAGE
        )
        assert "design mockups/images" in ref
        assert ".media/123_photo.png" in ref

    def test_build_media_reference_video(self, handler):
        ref = handler.build_media_reference(
            [".media/123_frame_001.png", ".media/123_frame_002.png"],
            MediaType.VIDEO,
        )
        assert "keyframes" in ref
        assert ".media/123_frame_001.png" in ref

    def test_build_media_reference_audio_with_transcript(self, handler):
        ref = handler.build_media_reference(
            [".media/123_transcript.txt", ".media/123_voice.ogg"],
            MediaType.AUDIO,
        )
        assert "transcript" in ref.lower()
        assert "audio file" in ref.lower()
        assert ".media/123_transcript.txt" in ref
        assert ".media/123_voice.ogg" in ref

    def test_build_media_reference_audio_without_transcript(self, handler):
        ref = handler.build_media_reference(
            [".media/123_voice.ogg"], MediaType.AUDIO
        )
        assert "audio file" in ref.lower()
        assert "transcript" not in ref.lower()

    def test_build_media_reference_document(self, handler):
        ref = handler.build_media_reference(
            [".media/123_report.pdf"], MediaType.DOCUMENT
        )
        assert "document" in ref.lower()
        assert ".media/123_report.pdf" in ref

    def test_build_media_reference_empty(self, handler):
        assert handler.build_media_reference([], MediaType.IMAGE) == ""


class TestEnsureMediaDir:
    def test_ensure_media_dir(self, handler, worktree):
        media_dir = handler._ensure_media_dir(worktree)
        assert media_dir.exists()
        assert media_dir.is_dir()
        assert media_dir.name == ".media"
        assert str(media_dir) == str(Path(worktree) / ".media")

    def test_ensure_media_dir_idempotent(self, handler, worktree):
        dir1 = handler._ensure_media_dir(worktree)
        dir2 = handler._ensure_media_dir(worktree)
        assert dir1 == dir2
        assert dir1.exists()


class TestProcessAndStage:
    async def test_process_and_stage_document(self, handler, worktree, tmp_path):
        # Create a source document
        source = tmp_path / "report.pdf"
        source.write_text("fake pdf content")

        with patch("agent_forge.media_handler.time") as mock_time:
            mock_time.time.return_value = 1000000
            paths, media_type = await handler.process_and_stage(
                str(source), worktree, MediaType.DOCUMENT
            )

        assert media_type == MediaType.DOCUMENT
        assert len(paths) == 1
        assert paths[0] == ".media/1000000_report.pdf"
        # Verify file was actually copied
        staged_file = Path(worktree) / ".media" / "1000000_report.pdf"
        assert staged_file.exists()
        assert staged_file.read_text() == "fake pdf content"

    async def test_process_and_stage_image(self, handler, worktree, tmp_path):
        # Create a source image
        source = tmp_path / "photo.png"
        source.write_bytes(b"\x89PNG fake image data")

        # Mock _resize_image to return the original path (no resize needed)
        with (
            patch("agent_forge.media_handler.time") as mock_time,
            patch.object(
                handler, "_resize_image", return_value=str(source)
            ) as mock_resize,
        ):
            mock_time.time.return_value = 1000000
            paths, media_type = await handler.process_and_stage(
                str(source), worktree, MediaType.IMAGE
            )

        mock_resize.assert_awaited_once_with(str(source))
        assert media_type == MediaType.IMAGE
        assert len(paths) == 1
        assert paths[0] == ".media/1000000_photo.png"
        # Verify file was actually copied
        staged_file = Path(worktree) / ".media" / "1000000_photo.png"
        assert staged_file.exists()

    async def test_process_and_stage_video(self, handler, worktree, tmp_path):
        # Create fake frame files that _extract_video_frames would produce
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"fake video data")

        frame_dir = handler.temp_dir / "frames_1000000"
        frame_dir.mkdir(parents=True, exist_ok=True)
        frame1 = frame_dir / "frame_001.png"
        frame2 = frame_dir / "frame_002.png"
        frame1.write_bytes(b"frame1")
        frame2.write_bytes(b"frame2")

        with (
            patch("agent_forge.media_handler.time") as mock_time,
            patch.object(
                handler,
                "_extract_video_frames",
                return_value=[str(frame1), str(frame2)],
            ),
        ):
            mock_time.time.return_value = 1000000
            paths, media_type = await handler.process_and_stage(
                str(source), worktree, MediaType.VIDEO
            )

        assert media_type == MediaType.VIDEO
        assert len(paths) == 2
        assert ".media/1000000_frame_001.png" in paths
        assert ".media/1000000_frame_002.png" in paths

    async def test_process_and_stage_audio_with_transcript(
        self, handler, worktree, tmp_path
    ):
        source = tmp_path / "voice.ogg"
        source.write_bytes(b"fake audio data")

        with (
            patch("agent_forge.media_handler.time") as mock_time,
            patch.object(
                handler,
                "_transcribe_audio",
                return_value="Hello, this is a test.",
            ),
        ):
            mock_time.time.return_value = 1000000
            paths, media_type = await handler.process_and_stage(
                str(source), worktree, MediaType.AUDIO
            )

        # Should have transcript + original audio
        assert media_type == MediaType.AUDIO
        assert len(paths) == 2
        assert ".media/1000000_transcript.txt" in paths
        assert ".media/1000000_voice.ogg" in paths
        # Verify transcript content
        transcript_file = Path(worktree) / ".media" / "1000000_transcript.txt"
        assert transcript_file.read_text() == "Hello, this is a test."

    async def test_process_and_stage_audio_without_transcript(
        self, handler, worktree, tmp_path
    ):
        source = tmp_path / "voice.ogg"
        source.write_bytes(b"fake audio data")

        with (
            patch("agent_forge.media_handler.time") as mock_time,
            patch.object(handler, "_transcribe_audio", return_value=None),
        ):
            mock_time.time.return_value = 1000000
            paths, media_type = await handler.process_and_stage(
                str(source), worktree, MediaType.AUDIO
            )

        # Should have only the original audio (no transcript)
        assert media_type == MediaType.AUDIO
        assert len(paths) == 1
        assert ".media/1000000_voice.ogg" in paths

    async def test_process_and_stage_auto_detects_type(
        self, handler, worktree, tmp_path
    ):
        """When media_type is None, auto-detect from file extension."""
        source = tmp_path / "screenshot.png"
        source.write_bytes(b"\x89PNG fake image data")

        with (
            patch("agent_forge.media_handler.time") as mock_time,
            patch.object(
                handler, "_resize_image", return_value=str(source)
            ),
        ):
            mock_time.time.return_value = 1000000
            paths, media_type = await handler.process_and_stage(
                str(source), worktree
            )

        assert media_type == MediaType.IMAGE
        assert len(paths) == 1
        assert ".media/1000000_screenshot.png" in paths


class TestExtractVideoFrames:
    async def test_extract_video_frames_short_video(self, handler, tmp_path):
        output_dir = str(tmp_path / "frames")
        Path(output_dir).mkdir()

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.kill = MagicMock()

        # Create fake frame files that ffmpeg would produce
        (Path(output_dir) / "frame_001.png").write_bytes(b"f1")
        (Path(output_dir) / "frame_002.png").write_bytes(b"f2")

        with (
            patch.object(handler, "_get_video_duration", return_value=5.0),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            frames = await handler._extract_video_frames("/fake/video.mp4", output_dir)

        assert len(frames) == 2
        assert "frame_001.png" in frames[0]

    async def test_extract_video_frames_timeout(self, handler, tmp_path):
        output_dir = str(tmp_path / "frames")
        Path(output_dir).mkdir()

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_proc.kill = MagicMock()

        with (
            patch.object(handler, "_get_video_duration", return_value=5.0),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            frames = await handler._extract_video_frames("/fake/video.mp4", output_dir)

        assert frames == []
        mock_proc.kill.assert_called_once()


class TestGetVideoDuration:
    async def test_get_video_duration_success(self, handler):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"12.5\n", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            duration = await handler._get_video_duration("/fake/video.mp4")

        assert duration == 12.5

    async def test_get_video_duration_invalid_output(self, handler):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"N/A\n", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            duration = await handler._get_video_duration("/fake/video.mp4")

        assert duration is None


class TestTranscribeAudio:
    async def test_transcribe_audio_no_whisper(self, handler):
        with patch("shutil.which", return_value=None):
            result = await handler._transcribe_audio("/fake/audio.ogg", "/tmp")

        assert result is None

    async def test_transcribe_audio_success(self, handler, tmp_path):
        # Create the expected output file
        txt_file = tmp_path / "audio.txt"
        txt_file.write_text("Transcribed text here")

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with (
            patch("shutil.which", return_value="/usr/local/bin/whisper"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            result = await handler._transcribe_audio(
                "/fake/audio.ogg", str(tmp_path)
            )

        assert result == "Transcribed text here"

    async def test_transcribe_audio_timeout(self, handler, tmp_path):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_proc.kill = MagicMock()

        with (
            patch("shutil.which", return_value="/usr/local/bin/whisper"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            result = await handler._transcribe_audio(
                "/fake/audio.ogg", str(tmp_path)
            )

        assert result is None
        mock_proc.kill.assert_called_once()


class TestResizeImage:
    async def test_resize_image_small_enough(self, handler):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"800,600\n", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await handler._resize_image("/fake/photo.png")

        assert result == "/fake/photo.png"

    async def test_resize_image_too_large(self, handler):
        # First call: ffprobe returns large dimensions
        probe_proc = AsyncMock()
        probe_proc.communicate = AsyncMock(return_value=(b"5000,3000\n", b""))

        # Second call: ffmpeg resize
        resize_proc = AsyncMock()
        resize_proc.communicate = AsyncMock(return_value=(b"", b""))

        call_count = 0

        async def mock_subprocess(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return probe_proc
            return resize_proc

        resized_path = handler.temp_dir / "resized_photo.png"
        resized_path.parent.mkdir(parents=True, exist_ok=True)
        resized_path.write_bytes(b"resized image data")

        with patch("asyncio.create_subprocess_exec", side_effect=mock_subprocess):
            result = await handler._resize_image("/fake/photo.png")

        assert result == str(resized_path)

    async def test_resize_image_cant_determine_size(self, handler):
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await handler._resize_image("/fake/photo.png")

        assert result == "/fake/photo.png"
