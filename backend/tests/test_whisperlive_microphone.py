import threading
import unittest

from backend.scripts import whisperlive_microphone as mic_module
from backend.scripts.whisperlive_microphone import (
    RecordingSessionMonitor,
    TranscriptPreviewSyncer,
    choose_audio_device,
    ffmpeg_microphone_command,
    iter_microphone_packets,
    parse_arecord_devices,
    parse_args,
    resolve_or_wait_backend_session_id,
)
from backend.scripts.whisperlive_qwen_markdown import WhisperLiveSegment


class WhisperLiveMicrophoneTests(unittest.TestCase):
    def test_parse_arecord_devices(self) -> None:
        output = """
**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: ALC Analog [ALC Analog]
  Subdevices: 1/1
card 1: Device [USB Audio Device], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
"""

        devices = parse_arecord_devices(output)

        self.assertEqual(
            devices,
            [
                ("PCH [HDA Intel PCH] ALC Analog", "plughw:0,0"),
                ("Device [USB Audio Device] USB Audio", "plughw:1,0"),
            ],
        )

    def test_choose_audio_device_prefers_usb_or_mic(self) -> None:
        self.assertEqual(
            choose_audio_device(
                [
                    ("PCH ALC Analog", "plughw:0,0"),
                    ("Device USB Audio", "plughw:1,0"),
                ]
            ),
            "plughw:1,0",
        )
        self.assertEqual(
            choose_audio_device([("Internal Capture", "plughw:0,0")]),
            "plughw:0,0",
        )
        self.assertEqual(choose_audio_device([]), "default")

    def test_ffmpeg_microphone_command_outputs_float32_mono(self) -> None:
        command = ffmpeg_microphone_command(
            ffmpeg="/usr/bin/ffmpeg",
            audio_device="plughw:1,0",
            sample_rate=16000,
        )

        self.assertIn("-f", command)
        self.assertIn("alsa", command)
        self.assertIn("plughw:1,0", command)
        self.assertIn("-ac", command)
        self.assertIn("1", command)
        self.assertIn("-ar", command)
        self.assertIn("16000", command)
        self.assertEqual(command[-2:], ["f32le", "pipe:1"])

    def test_iter_microphone_packets_treats_requested_stop_as_normal(self) -> None:
        class FakePipe:
            def read(self, _size):  # type: ignore[no-untyped-def]
                return b""

            def readline(self):  # type: ignore[no-untyped-def]
                return b""

            def close(self) -> None:
                pass

        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = FakePipe()
                self.stderr = FakePipe()
                self.return_code: int | None = None

            def poll(self) -> int | None:
                return self.return_code

            def terminate(self) -> None:
                self.return_code = -15

            def wait(self, timeout=None):  # type: ignore[no-untyped-def]
                return self.return_code

            def kill(self) -> None:
                self.return_code = -9

        process = FakeProcess()
        original_popen = mic_module.subprocess.Popen
        mic_module.subprocess.Popen = lambda *_args, **_kwargs: process  # type: ignore[assignment]
        stop_event = threading.Event()
        stop_event.set()
        try:
            packets = list(
                iter_microphone_packets(
                    ffmpeg="/usr/bin/ffmpeg",
                    audio_device="default",
                    packet_seconds=0.25,
                    max_audio_seconds=0.0,
                    sample_rate=16000,
                    stop_event=stop_event,
                )
            )
        finally:
            mic_module.subprocess.Popen = original_popen

        self.assertEqual(packets, [])
        self.assertEqual(process.return_code, -15)

    def test_transcript_preview_syncer_posts_non_persistent_preview(self) -> None:
        calls: list[tuple[str, str, dict, float]] = []
        original_post_json = mic_module.post_json

        def fake_post_json(base_url, path, body, *, timeout):  # type: ignore[no-untyped-def]
            calls.append((base_url, path, body, timeout))
            return {"status": "accepted"}

        mic_module.post_json = fake_post_json
        try:
            syncer = TranscriptPreviewSyncer(
                base_url="http://backend",
                session_id="lec_test",
                http_timeout=3.0,
                enabled=True,
                min_interval_seconds=0.0,
                min_chars=1,
            )
            syncer._post_preview(
                WhisperLiveSegment(
                    start=1.0,
                    end=2.0,
                    text="临时字幕",
                    completed=False,
                )
            )
        finally:
            mic_module.post_json = original_post_json

        self.assertEqual(len(calls), 1)
        base_url, path, body, timeout = calls[0]
        self.assertEqual(base_url, "http://backend")
        self.assertEqual(path, "/events/transcript-preview")
        self.assertEqual(timeout, 3.0)
        self.assertEqual(body["session_id"], "lec_test")
        self.assertEqual(body["payload"]["text"], "临时字幕")
        self.assertFalse(body["payload"]["is_final"])

    def test_transcript_preview_syncer_stops_on_ended_session_conflict(self) -> None:
        original_post_json = mic_module.post_json

        def fake_post_json(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError('POST failed: 409 {"detail":"Session is not recording"}')

        mic_module.post_json = fake_post_json
        try:
            syncer = TranscriptPreviewSyncer(
                base_url="http://backend",
                session_id="lec_test",
                http_timeout=3.0,
                enabled=True,
                min_interval_seconds=0.0,
                min_chars=1,
            )
            syncer._queue.put(
                WhisperLiveSegment(
                    start=1.0,
                    end=2.0,
                    text="临时字幕",
                    completed=False,
                )
            )
            worker = threading.Thread(target=syncer._run)
            worker.start()
            syncer._queue.join()
            syncer._queue.put(None)
            worker.join(timeout=1.0)
        finally:
            mic_module.post_json = original_post_json

        self.assertFalse(worker.is_alive())
        self.assertFalse(syncer.enabled)
        self.assertEqual(syncer.post_count, 0)

    def test_recording_session_monitor_stops_when_session_ended(self) -> None:
        original_get_json = mic_module.get_json

        def fake_get_json(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"status": "ended"}

        stop_event = threading.Event()
        mic_module.get_json = fake_get_json
        try:
            monitor = RecordingSessionMonitor(
                base_url="http://backend",
                session_id="lec_test",
                http_timeout=3.0,
                poll_interval=0.5,
                stop_event=stop_event,
                enabled=True,
            )
            monitor._run()
        finally:
            mic_module.get_json = original_get_json

        self.assertTrue(stop_event.is_set())
        self.assertFalse(monitor.is_recording())

    def test_parse_args_supports_wait_without_session_creation(self) -> None:
        args = parse_args(["--wait-for-session", "--no-create-session"])

        self.assertTrue(args.wait_for_session)
        self.assertTrue(args.no_create_session)
        self.assertEqual(args.session_poll_interval, 2.0)
        self.assertEqual(args.session_wait_timeout, 0.0)
        self.assertEqual(args.language, "auto")
        self.assertEqual(args.no_speech_thresh, 0.3)
        self.assertEqual(args.same_output_threshold, 8)

    def test_resolve_or_wait_retries_until_frontend_session_exists(self) -> None:
        calls = 0
        original_resolve = mic_module.resolve_backend_session_id
        original_sleep = mic_module.time.sleep

        def fake_resolve(**kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            self.assertFalse(kwargs["create_if_missing"])
            if calls == 1:
                raise RuntimeError("No recording backend session is available.")
            return "lec_frontend"

        mic_module.resolve_backend_session_id = fake_resolve
        mic_module.time.sleep = lambda _seconds: None
        try:
            session_id = resolve_or_wait_backend_session_id(
                requested_session_id="auto",
                base_url="http://backend",
                request_timeout=1.0,
                create_if_missing=False,
                wait_for_session=True,
                poll_interval=0.2,
                wait_timeout=5.0,
            )
        finally:
            mic_module.resolve_backend_session_id = original_resolve
            mic_module.time.sleep = original_sleep

        self.assertEqual(session_id, "lec_frontend")
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
