import asyncio
import contextlib
import queue

import numpy as np

import httpx
import pytest

from voice_runtime.api_client import (
    ConversationResponse, VoiceAPIClient, VoiceConversationConnectionError,
    VoiceConversationServerError, VoiceConversationValidationError,
)
from voice_runtime.main import VoiceRuntimeRunner
from voice_runtime.state_machine import State


class FakeAudioIO:
    def __init__(self, *, playback_error: Exception | None = None):
        self.queue = queue.Queue()
        self.playback_error = playback_error
        self.recording_starts = 0
        self.recording_stops = 0
        self.recording = False
        self.chimes = 0
        self.played_audio: list[bytes] = []

    @property
    def is_recording(self):
        return self.recording

    def start_recording(self):
        if not self.recording:
            self.recording_starts += 1
            self.recording = True

    def stop_recording(self):
        if self.recording:
            self.recording_stops += 1
            self.recording = False

    def play_chime(self):
        self.chimes += 1

    def play_tts(self, audio: bytes):
        if self.playback_error:
            raise self.playback_error
        self.played_audio.append(audio)
        return 1.0

    @staticmethod
    def float32_to_wav_bytes(_frames):
        return b'fake-wav'


class FakeVAD:
    def reset(self):
        pass


class FakeWakeword:
    def process_audio(self, _frame):
        return False


class FakeVoiceClient:
    def __init__(self, response: ConversationResponse, *, tts_error: Exception | None = None):
        self.response = response
        self.tts_error = tts_error
        self.voice_calls: list[tuple[str, bytes]] = []
        self.tts_texts: list[str] = []

    async def send_voice_conversation(self, session_id: str, audio: bytes) -> ConversationResponse:
        self.voice_calls.append((session_id, audio))
        return self.response

    async def get_tts_audio(self, text: str) -> bytes:
        self.tts_texts.append(text)
        if self.tts_error:
            raise self.tts_error
        return b'fake-mp3'

    async def close(self):
        pass


async def _run_turn(
    response: ConversationResponse,
    *,
    tts_error: Exception | None = None,
    playback_error: Exception | None = None,
    autoplay: bool = True,
):
    audio = FakeAudioIO(playback_error=playback_error)
    client = FakeVoiceClient(response, tts_error=tts_error)
    runner = VoiceRuntimeRunner(
        audio_io=audio,
        wakeword_detector=FakeWakeword(),
        vad=FakeVAD(),
        api_client=client,
        tts_autoplay=autoplay,
    )
    runner.state_machine.handle_wake_detected()
    session_id = runner.state_machine.session_id
    runner.state_machine.handle_speech_ended(b'user-wav')
    assert runner._api_task is not None
    await runner._api_task
    if runner._tts_task is not None:
        await runner._tts_task
    return runner, client, audio, session_id


@pytest.mark.parametrize(
    ('response', 'expected_state'),
    [
        (ConversationResponse('s', 'WAITING_ROOF_OPTION', '지붕을 선택해주세요. 1번 평지붕, 2번 경사지붕입니다.', False, 'CREATE_PRODUCTION_REQUEST'), State.LISTENING),
        (ConversationResponse('s', 'AWAITING_CONFIRMATION', 'A형 주택 한 채를 1번 평지붕으로 제작하는 것이 맞습니까?', False, 'CREATE_PRODUCTION_REQUEST'), State.LISTENING),
        (ConversationResponse('s', 'CONFIRMED', '생산 요청이 확인되어 생산 작업 1건이 등록되었습니다.', False, None, (101,)), State.WAKEWORD_READY),
        (ConversationResponse('s', 'REJECTED', '생산 요청을 진행하지 않겠습니다.', False), State.WAKEWORD_READY),
        (ConversationResponse('s', 'REJECTED', '현재 A형 주택 생산에 필요한 외벽 1개가 부족하여 생산할 수 없습니다.', False), State.WAKEWORD_READY),
        (ConversationResponse('s', None, '현재 A형 주택을 생산 중이며 외벽 설치 공정을 진행하고 있습니다.', False, 'QUERY_JOB_STATUS'), State.WAKEWORD_READY),
        (ConversationResponse('s', None, '지원하는 생산 시스템 명령으로 다시 말씀해 주세요.', True, 'UNKNOWN'), State.LISTENING),
    ],
)
def test_authoritative_voice_responses_are_spoken_exactly_once(response, expected_state):
    runner, client, audio, session_id = asyncio.run(_run_turn(response))
    assert client.voice_calls == [(session_id, b'user-wav')]
    assert client.tts_texts == [response.message]
    assert audio.played_audio == [b'fake-mp3']
    assert runner.state_machine.state is expected_state


@pytest.mark.parametrize(
    ('intent', 'message'),
    [
        ('CANCEL_JOB', '현재 작업 취소 요청을 확인했습니다. 실제 적용 전 승인이 필요합니다.'),
        ('PAUSE_JOB', '현재 작업 일시정지 요청을 확인했습니다. 실제 적용 전 승인이 필요합니다.'),
        ('RESUME_JOB', '현재 작업 재개 요청을 확인했습니다. 실제 적용 전 승인이 필요합니다.'),
    ],
)
def test_control_intent_only_speaks_authoritative_non_completion_message(intent, message):
    response = ConversationResponse('s', None, message, False, intent)
    _, client, _, _ = asyncio.run(_run_turn(response))
    assert client.tts_texts == [message]
    assert '작업을 취소했습니다.' not in message
    assert '작업을 일시정지했습니다.' not in message
    assert '작업을 재개했습니다.' not in message


def test_tts_generation_failure_does_not_resubmit_authoritative_business_turn():
    response = ConversationResponse('s', 'CONFIRMED', '생산 요청이 확인되어 생산 작업 1건이 등록되었습니다.', False, None, (101,))
    runner, client, audio, _ = asyncio.run(_run_turn(response, tts_error=RuntimeError('provider down')))
    assert len(client.voice_calls) == 1
    assert client.tts_texts == [response.message]
    assert audio.played_audio == []
    assert runner.state_machine.state is State.WAKEWORD_READY


def test_playback_failure_does_not_duplicate_voice_submission():
    response = ConversationResponse('s', 'CONFIRMED', '생산 요청이 확인되어 생산 작업 1건이 등록되었습니다.', False, None, (101,))
    runner, client, audio, _ = asyncio.run(_run_turn(response, playback_error=RuntimeError('speaker down')))
    assert len(client.voice_calls) == 1
    assert client.tts_texts == [response.message]
    assert audio.played_audio == []
    assert runner.state_machine.state is State.WAKEWORD_READY


def test_speaking_state_drops_echo_turn_and_never_creates_a_second_request():
    async def run():
        response = ConversationResponse('s', 'CONFIRMED', '생산 요청이 확인되어 생산 작업 1건이 등록되었습니다.', False)
        audio = FakeAudioIO()
        client = FakeVoiceClient(response)
        runner = VoiceRuntimeRunner(audio_io=audio, wakeword_detector=FakeWakeword(), vad=FakeVAD(), api_client=client)
        runner.state_machine.handle_wake_detected()
        runner.state_machine.handle_speech_ended(b'user-wav')
        await runner._api_task
        assert runner.state_machine.state is State.SPEAKING
        runner.state_machine.handle_speech_ended(b'tts-echo-wav')
        assert len(client.voice_calls) == 1
        await runner._tts_task
        return runner, client

    runner, client = asyncio.run(run())
    assert len(client.voice_calls) == 1
    assert runner.state_machine.state is State.WAKEWORD_READY


def test_tts_autoplay_can_be_disabled_without_playback_or_duplicate_submission():
    response = ConversationResponse('s', 'REJECTED', '생산 요청을 진행하지 않겠습니다.', False)
    runner, client, audio, _ = asyncio.run(_run_turn(response, autoplay=False))
    assert len(client.voice_calls) == 1
    assert client.tts_texts == []
    assert audio.played_audio == []
    assert runner.state_machine.state is State.WAKEWORD_READY


def test_voice_client_uses_single_authoritative_multipart_endpoint():
    async def run():
        seen_paths: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.path)
            assert b'name="session_id"' in request.content
            assert b'demo-session' in request.content
            assert b'voice-wav' in request.content
            return httpx.Response(200, json={
                'session_id': 'demo-session',
                'message': '지붕을 선택해주세요. 1번 평지붕, 2번 경사지붕입니다.',
                'conversation_state': 'WAITING_ROOF_OPTION',
                'command': {'intent': 'CREATE_PRODUCTION_REQUEST', 'clarification_needed': False},
                'production_job_ids': [],
            })

        raw_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url='http://voice-test',
        )
        client = VoiceAPIClient('http://voice-test', client=raw_client)
        response = await client.send_voice_conversation('demo-session', b'voice-wav')
        await raw_client.aclose()
        return seen_paths, response

    paths, response = asyncio.run(run())
    assert paths == ['/ai/voice-conversation']
    assert response.message == '지붕을 선택해주세요. 1번 평지붕, 2번 경사지붕입니다.'
    assert response.intent == 'CREATE_PRODUCTION_REQUEST'


def test_terminal_tts_rearms_microphone_for_a_second_wakeword_turn():
    async def run():
        response = ConversationResponse('s', 'CONFIRMED', '완료했습니다.', False)
        audio = FakeAudioIO()
        client = FakeVoiceClient(response)
        runner = VoiceRuntimeRunner(
            audio_io=audio, wakeword_detector=FakeWakeword(), vad=FakeVAD(), api_client=client,
        )
        runner.state_machine.handle_wake_detected()
        runner.state_machine.handle_speech_ended(b'first-turn')
        await runner._api_task
        await runner._tts_task
        assert runner.state_machine.state is State.WAKEWORD_READY
        assert audio.is_recording is True
        assert audio.recording_starts == 2  # first listening + re-armed wakeword stream

        # A second wake transition proves the re-armed stream can enter a new turn.
        runner.state_machine.handle_wake_detected()
        assert runner.state_machine.state is State.LISTENING
        assert audio.recording_starts == 2  # LISTENING reuses the active stream
        return runner, audio

    runner, audio = asyncio.run(run())
    assert runner.state_machine.state is State.LISTENING
    assert audio.is_recording is True


@pytest.mark.parametrize('autoplay,tts_error,playback_error', [
    (False, None, None),
    (True, RuntimeError('tts unavailable'), None),
    (True, None, RuntimeError('speaker unavailable')),
])
def test_terminal_turn_rearms_microphone_after_tts_disabled_or_failure(autoplay, tts_error, playback_error):
    response = ConversationResponse('s', 'CONFIRMED', '완료했습니다.', False)
    runner, _client, audio, _session = asyncio.run(
        _run_turn(response, autoplay=autoplay, tts_error=tts_error, playback_error=playback_error)
    )
    assert runner.state_machine.state is State.WAKEWORD_READY
    assert audio.is_recording is True
    assert audio.recording_starts == 2


def test_confirmation_continuation_does_not_double_rearm_microphone():
    response = ConversationResponse('s', 'AWAITING_CONFIRMATION', '확인해 주세요.', False)
    runner, _client, audio, _session = asyncio.run(_run_turn(response))
    assert runner.state_machine.state is State.LISTENING
    assert audio.is_recording is True
    # StateMachine's existing LISTENING callback is the only restart authority.
    assert audio.recording_starts == 2


class FrameWakeword:
    def __init__(self):
        self.detected = 0

    def process_audio(self, frame):
        if float(frame.reshape(-1)[0]) == 1.0:
            self.detected += 1
            return True
        return False


class ImmediateSpeechVAD:
    def __init__(self):
        self.is_speech_started = False

    def reset(self):
        self.is_speech_started = False

    def process_frame(self, _frame):
        self.is_speech_started = True
        return True


async def _wait_until(predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("runtime state did not reach expected condition")
        await asyncio.sleep(0.005)


def test_run_loop_detects_second_wakeword_frame_after_terminal_turn_rearm():
    async def run():
        audio = FakeAudioIO()
        wakeword = FrameWakeword()
        client = FakeVoiceClient(ConversationResponse('s', 'CONFIRMED', '완료했습니다.', False))
        runner = VoiceRuntimeRunner(
            audio_io=audio, wakeword_detector=wakeword, vad=ImmediateSpeechVAD(),
            api_client=client, tts_autoplay=False,
        )
        task = asyncio.create_task(runner.run_loop())
        try:
            await _wait_until(lambda: audio.is_recording)
            audio.queue.put(np.array([[1.0]], dtype=np.float32))
            await _wait_until(lambda: runner.state_machine.state is State.LISTENING)
            audio.queue.put(np.array([[0.25]], dtype=np.float32))
            await _wait_until(lambda: runner.state_machine.state is State.WAKEWORD_READY and audio.is_recording)

            # This frame is consumed by the re-armed wake-word listener, not a
            # stale queue from the first turn.
            audio.queue.put(np.array([[1.0]], dtype=np.float32))
            await _wait_until(lambda: wakeword.detected == 2)
            assert runner.state_machine.state is State.LISTENING
            assert audio.is_recording is True
            assert len(client.voice_calls) == 1
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(run())


class ErrorVoiceClient(FakeVoiceClient):
    def __init__(self, error):
        super().__init__(ConversationResponse('s', None, 'unused', False))
        self.error = error
    async def send_voice_conversation(self, _session_id, _audio):
        raise self.error


async def _run_api_error(error, *, continuation=False):
    audio = FakeAudioIO(); client = ErrorVoiceClient(error)
    runner = VoiceRuntimeRunner(audio_io=audio, wakeword_detector=FakeWakeword(), vad=FakeVAD(), api_client=client)
    runner._continuation_active = continuation
    runner.state_machine.handle_wake_detected()
    runner.state_machine.handle_speech_ended(b'user-wav')
    await runner._api_task
    assert runner._tts_task is not None
    await runner._tts_task
    return runner, client


def test_stt_422_reprompts_and_preserves_immediate_clarification_listening():
    runner, client = asyncio.run(_run_api_error(VoiceConversationValidationError('short audio'), continuation=True))
    assert client.tts_texts == ['음성을 정확히 인식하지 못했습니다. 다시 말씀해 주세요.']
    assert runner.state_machine.state is State.LISTENING


def test_connection_and_server_errors_have_distinct_safe_voice_messages():
    connection, client = asyncio.run(_run_api_error(VoiceConversationConnectionError('down')))
    assert client.tts_texts == ['서버에 연결할 수 없습니다.']
    assert connection.state_machine.state is State.WAKEWORD_READY
    server, client = asyncio.run(_run_api_error(VoiceConversationServerError('500')))
    assert client.tts_texts == ['서버 처리 중 오류가 발생했습니다. 다시 말씀해 주세요.']
    assert server.state_machine.state is State.WAKEWORD_READY


def test_voice_api_client_classifies_422_and_5xx_without_speaking_http_detail():
    async def run():
        validation = VoiceAPIClient("http://voice-test", client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(422, text="short audio")), base_url="http://voice-test"
        ))
        with pytest.raises(VoiceConversationValidationError):
            await validation.send_voice_conversation("s", b"wav")
        await validation.close()
        server = VoiceAPIClient("http://voice-test", client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(500, text="internal")), base_url="http://voice-test"
        ))
        with pytest.raises(VoiceConversationServerError):
            await server.send_voice_conversation("s", b"wav")
        await server.close()
    asyncio.run(run())
