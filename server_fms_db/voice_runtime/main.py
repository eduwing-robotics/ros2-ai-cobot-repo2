import asyncio
import logging
import os
import queue
import time
import uuid
import inspect

import numpy as np

from voice_runtime.api_client import (
    VoiceAPIClient, VoiceConversationConnectionError, VoiceConversationServerError,
    VoiceConversationValidationError,
)
from voice_runtime.audio_io import AudioIO
from voice_runtime.audio_playback_queue import AudioPlaybackQueue
from voice_runtime.production_event_subscriber import ProductionEventAnnouncementSubscriber
from voice_runtime.state_machine import State, VoiceRuntimeStateMachine
from voice_runtime.vad import EnergyVAD
from voice_runtime.wakeword import WakewordDetector


logger = logging.getLogger(__name__)


def _env_enabled(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


class VoiceRuntimeRunner:
    def __init__(
        self,
        *,
        audio_io: AudioIO | None = None,
        wakeword_detector: WakewordDetector | None = None,
        vad: EnergyVAD | None = None,
        api_client: VoiceAPIClient | None = None,
        tts_autoplay: bool | None = None,
    ):
        self.audio_io = audio_io or AudioIO()
        self.wakeword_detector = wakeword_detector or WakewordDetector()
        self.vad = vad or EnergyVAD()
        self.api_client = api_client or VoiceAPIClient(
            base_url=os.getenv("VOICE_API_BASE_URL", "http://127.0.0.1:8000")
        )
        self.tts_autoplay = (
            _env_enabled("VOICE_TTS_AUTOPLAY", default=True)
            if tts_autoplay is None
            else tts_autoplay
        )
        self._api_task: asyncio.Task | None = None
        self._tts_task: asyncio.Task | None = None
        self._playback_queue = AudioPlaybackQueue(api_client=self.api_client, audio_io=self.audio_io)
        async def _no_production_snapshot(_job_id: int):
            return None
        self._production_announcements_available = callable(
            getattr(self.api_client, "get_production_announcement_snapshot", None)
        )
        snapshot_reader = getattr(
            self.api_client, "get_production_announcement_snapshot", _no_production_snapshot
        )
        self._production_subscriber = ProductionEventAnnouncementSubscriber(
            snapshot_reader=snapshot_reader,
            job_ids_reader=getattr(self.api_client, "list_production_jobs", None),
            fault_reader=getattr(self.api_client, "get_execution_attempt_error", None),
            queue=self._playback_queue,
        )
        self._diagnostic_tasks: set[asyncio.Task] = set()
        self._turn_id: str | None = None
        self._speech_detected = False
        self._continuation_active = False

        self.state_machine = VoiceRuntimeStateMachine(callbacks={
            'on_chime': self.play_chime,
            'on_record_start': self.start_recording,
            'on_record_stop': self.stop_recording,
            'on_api_call': self.trigger_api_call,
            'on_tts_play': self.trigger_tts_play,
        })

        self.recorded_frames = []
        self.listening_start_time = 0.0
        self.no_speech_timeout_sec = 8.0
        self.max_speech_duration_sec = 15.0

    def _record_diagnostic(self, *, state: str, **fields: object) -> None:
        """Best-effort diagnostic reporting; unavailable API must not alter voice flow."""
        reporter = getattr(self.api_client, "record_runtime_event", None)
        if not callable(reporter):
            return
        event = {"turn_id": self._turn_id, "state": state, **fields}
        try:
            result = reporter(event)
            if not inspect.isawaitable(result):
                return
            task = asyncio.get_running_loop().create_task(result)
        except Exception:
            return
        self._diagnostic_tasks.add(task)

        def _consume_diagnostic_result(done: asyncio.Task) -> None:
            self._diagnostic_tasks.discard(done)
            try:
                done.result()
            except Exception:
                logger.debug("Voice diagnostic event delivery failed", exc_info=True)

        task.add_done_callback(_consume_diagnostic_result)

    def play_chime(self):
        print("[CHIME] 🔔")
        self.audio_io.play_chime()

    def _discard_queued_microphone_frames(self) -> None:
        """Prevent pre-transition speech/TTS frames from entering the next state."""
        while not self.audio_io.queue.empty():
            self.audio_io.queue.get_nowait()

    def _mic_is_active(self) -> bool:
        """AudioIO stream ownership is the diagnostic authority, not state alone."""
        return bool(getattr(self.audio_io, "is_recording", False))

    def _resume_wakeword_listening(self) -> None:
        """Re-arm the real microphone only after a terminal turn reaches ready."""
        if self.state_machine.state is not State.WAKEWORD_READY:
            return
        self._discard_queued_microphone_frames()
        # AudioIO.start_recording() is idempotent while an InputStream exists.
        self.audio_io.start_recording()

    def start_recording(self):
        print("[LISTENING] 🎤 Waiting for speech...")
        self.recorded_frames = []
        self.vad.reset()
        self.listening_start_time = time.time()
        # Drop stale microphone frames before each user turn. Recording is
        # stopped while the TTS state is active, so system speech is never
        # offered to STT as a new turn.
        self._discard_queued_microphone_frames()
        self.audio_io.start_recording()
        self._record_diagnostic(
            state="LISTENING", runtime_status="READY", mic_active=self._mic_is_active(),
            session_id=self.state_machine.session_id,
        )

    def stop_recording(self):
        self.audio_io.stop_recording()
        self._record_diagnostic(state="TRANSCRIBING", runtime_status="PROCESSING", mic_active=self._mic_is_active(), session_id=self.state_machine.session_id)

    def trigger_api_call(self, session_id: str, audio_data: bytes):
        self._api_task = asyncio.create_task(self._do_api_call(session_id, audio_data))

    def trigger_tts_play(self, text: str):
        self._record_diagnostic(state="RESPONDING", runtime_status="RESPONDING", response_text=text)
        if not self.tts_autoplay:
            logger.info("Voice TTS autoplay disabled; authoritative message was not played.")
            print(f"[TTS DISABLED] {text}")
            self.state_machine.handle_tts_completed()
            self._finish_or_continue_turn()
            return
        self._tts_task = asyncio.create_task(self._do_tts_play(text))

    async def _do_api_call(self, session_id: str, audio_data: bytes):
        print("[PROCESSING] 🔄 Calling /ai/voice-conversation")
        started = time.perf_counter()
        try:
            setter = getattr(self.api_client, "set_runtime_turn_id", None)
            if callable(setter):
                setter(self._turn_id)
            response = await self.api_client.send_voice_conversation(session_id, audio_data)
            print(f"[LATENCY] Voice API response in {(time.perf_counter() - started):.2f}s")
            logger.info(
                "Voice response received intent=%s state=%s job_ids=%s",
                response.intent,
                response.conversation_state,
                list(response.production_job_ids),
            )
            print(
                f"[VOICE RESPONSE] state={response.conversation_state}, "
                f"intent={response.intent}, message={response.message}"
            )
            self._continuation_active = response.conversation_state in {
                "COLLECTING_DETAILS", "WAITING_ROOF_OPTION", "AWAITING_CONFIRMATION"
            }
            self.state_machine.handle_api_response(
                response.conversation_state, response.clarification_needed, response.message,
            )
        except VoiceConversationValidationError as exc:
            logger.info("Recoverable Voice STT validation failure: %s", exc)
            self._record_diagnostic(state="ERROR", runtime_status="READY", error="STT_HTTP_422")
            self.state_machine.handle_api_error(
                "음성을 정확히 인식하지 못했습니다. 다시 말씀해 주세요.",
                continue_listening=self._continuation_active,
            )
        except VoiceConversationConnectionError as exc:
            logger.warning("Voice API connection failed: %s", exc)
            self._record_diagnostic(state="ERROR", runtime_status="READY", error="API_CONNECTION_ERROR")
            self.state_machine.handle_api_error("서버에 연결할 수 없습니다.")
        except VoiceConversationServerError as exc:
            logger.warning("Voice API server failure: %s", exc)
            self._record_diagnostic(state="ERROR", runtime_status="READY", error="API_SERVER_ERROR")
            self.state_machine.handle_api_error("서버 처리 중 오류가 발생했습니다. 다시 말씀해 주세요.")
        except Exception as exc:
            logger.warning("Voice API request failed: %s", exc)
            self._record_diagnostic(state="ERROR", runtime_status="READY", error=f"API_ERROR: {type(exc).__name__}")
            self.state_machine.handle_api_error("서버 처리 중 오류가 발생했습니다. 다시 말씀해 주세요.")

    async def _do_tts_play(self, text: str):
        print(f"[SPEAKING] 🔊 TTS: {text}")
        try:
            await self._playback_queue.enqueue(text)
        except Exception as exc:
            logger.warning("Voice TTS queue failed: %s", exc)
            self._record_diagnostic(state="RESPONDING", error=f"TTS_QUEUE_ERROR: {type(exc).__name__}")
        finally:
            self.state_machine.handle_tts_completed()
            self._finish_or_continue_turn()
            if self.state_machine.state == State.WAKEWORD_READY:
                print("[WAKEWORD_READY] 👀 Waiting for wake word...")

    def _finish_or_continue_turn(self) -> None:
        if self.state_machine.state == State.WAKEWORD_READY:
            self._resume_wakeword_listening()
            self._record_diagnostic(state="COMPLETED", runtime_status="READY", mic_active=self._mic_is_active())
            self._turn_id = None
        elif self.state_machine.state == State.LISTENING:
            self._record_diagnostic(state="WAITING_CONFIRMATION", runtime_status="READY", mic_active=self._mic_is_active(), session_id=self.state_machine.session_id)

    async def run_loop(self):
        print("[Voice Runtime Started]")
        print("[WAKEWORD_READY] 👀 Waiting for wake word...")
        self._playback_queue.start()
        if self._production_announcements_available:
            self._production_subscriber.start()
        self._resume_wakeword_listening()
        self._record_diagnostic(state="IDLE", runtime_status="READY", mic_active=self._mic_is_active())

        try:
            while True:
                try:
                    frame = self.audio_io.queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue

                if self.state_machine.state in (State.WAKEWORD_READY, State.WAKE_ACK):
                    if self.wakeword_detector.process_audio(frame):
                        print("[WAKE_DETECTED]")
                        self._turn_id = str(uuid.uuid4())
                        self._speech_detected = False
                        self._record_diagnostic(state="WAKE_DETECTED", runtime_status="READY", mic_active=self._mic_is_active(), wake_detected=True)
                        self.state_machine.handle_wake_detected()

                elif self.state_machine.state == State.LISTENING:
                    self.recorded_frames.append(frame)
                    is_ended = self.vad.process_frame(frame)
                    if self.vad.is_speech_started:
                        if not self._speech_detected:
                            self._speech_detected = True
                            self._record_diagnostic(state="SPEECH_DETECTED", runtime_status="READY", mic_active=self._mic_is_active(), speech_detected=True, session_id=self.state_machine.session_id)
                        if is_ended or (time.time() - self.listening_start_time > self.max_speech_duration_sec):
                            print("[UTTERANCE_COMPLETE]")
                            all_audio = np.concatenate(self.recorded_frames)
                            wav_bytes = self.audio_io.float32_to_wav_bytes(all_audio)
                            self.state_machine.handle_speech_ended(wav_bytes)
                    elif time.time() - self.listening_start_time > self.no_speech_timeout_sec:
                        print("[TIMEOUT] No speech detected.")
                        self._record_diagnostic(state="ERROR", runtime_status="READY", mic_active=self._mic_is_active(), error="VAD_TIMEOUT", session_id=self.state_machine.session_id)
                        self.state_machine.handle_no_speech_timeout()
                        self._turn_id = None
                        self._resume_wakeword_listening()
                        self._record_diagnostic(state="IDLE", runtime_status="READY", mic_active=self._mic_is_active())
                        print("[WAKEWORD_READY] 👀 Waiting for wake word...")
                # In PROCESSING or SPEAKING, microphone frames are dropped. The
                # stream is stopped for each turn as an additional echo guard.
        except KeyboardInterrupt:
            print("\nShutting down.")
        finally:
            self.audio_io.stop_recording()
            await self._production_subscriber.stop()
            await self._playback_queue.stop()
            await self.api_client.close()


if __name__ == "__main__":
    runner = VoiceRuntimeRunner()
    asyncio.run(runner.run_loop())
