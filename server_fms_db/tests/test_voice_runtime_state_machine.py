import pytest
from voice_runtime.state_machine import VoiceRuntimeStateMachine, State

def test_wake_detected():
    events = []
    sm = VoiceRuntimeStateMachine(callbacks={
        'on_chime': lambda: events.append('chime'),
        'on_record_start': lambda: events.append('record_start'),
    })

    assert sm.state == State.WAKEWORD_READY
    sm.handle_wake_detected()
    assert sm.state == State.LISTENING
    assert events == ['chime', 'record_start']
    assert sm.session_id is not None

def test_no_speech_timeout():
    events = []
    sm = VoiceRuntimeStateMachine(callbacks={
        'on_record_stop': lambda: events.append('record_stop'),
    })

    sm.handle_wake_detected()
    assert sm.state == State.LISTENING

    sm.handle_no_speech_timeout()
    assert sm.state == State.WAKEWORD_READY
    assert events == ['record_stop']
    assert sm.session_id is None

def test_speech_ended():
    events = []
    sm = VoiceRuntimeStateMachine(callbacks={
        'on_record_stop': lambda: events.append('record_stop'),
        'on_api_call': lambda sid, data: events.append(f'api_call:{data.decode()}'),
    })

    sm.handle_wake_detected()
    sm.handle_speech_ended(b"audio")
    assert sm.state == State.PROCESSING
    assert events == ['record_stop', 'api_call:audio']


@pytest.mark.parametrize("clarification_needed,conversation_state", [
    (True, None),                     # 1. WAITING_PRODUCT_MODEL or similar (clarification needed)
    (True, "ANY_STATE"),              # Even if state is there, clarification takes precedence
    (False, "WAITING_ROOF_OPTION"),   # 3. WAITING_ROOF_OPTION
    (False, "AWAITING_CONFIRMATION")  # 4. AWAITING_CONFIRMATION
])
def test_api_response_continuation(clarification_needed, conversation_state):
    events = []
    sm = VoiceRuntimeStateMachine(callbacks={
        'on_tts_play': lambda text: events.append(f'tts:{text}'),
        'on_chime': lambda: events.append('chime'),
        'on_record_start': lambda: events.append('record_start'),
    })

    sm.handle_wake_detected()
    session_id = sm.session_id
    sm.handle_speech_ended(b"audio")
    events.clear()

    sm.handle_api_response(conversation_state, clarification_needed, "TTS Message")
    assert sm.state == State.SPEAKING
    assert events == ['tts:TTS Message']

    # 9. TTS 재생 중 recording 비활성
    # By definition of state SPEAKING, microphone loop should drop frames.
    # The state machine itself doesn't record, it just changes state.

    sm.handle_tts_completed()
    # 10. follow-up chime 이후에만 LISTENING 활성
    assert sm.state == State.LISTENING
    assert events == ['tts:TTS Message', 'chime', 'record_start']

    # 8. non-terminal turn 간 same session_id 유지
    assert sm.session_id == session_id


@pytest.mark.parametrize("conversation_state", [
    "CONFIRMED", # 5
    "REJECTED",  # 6
    "EXPIRED",   # 7
])
def test_api_response_completion(conversation_state):
    events = []
    sm = VoiceRuntimeStateMachine(callbacks={
        'on_tts_play': lambda text: events.append(f'tts:{text}'),
    })

    sm.handle_wake_detected()
    sm.handle_speech_ended(b"audio")

    sm.handle_api_response(conversation_state, False, "TTS Message")
    assert sm.state == State.SPEAKING

    sm.handle_tts_completed()
    assert sm.state == State.WAKEWORD_READY
    assert sm.session_id is None

def test_api_error_recovery():
    events = []
    sm = VoiceRuntimeStateMachine(callbacks={
        'on_tts_play': lambda text: events.append(f'tts:{text}'),
    })

    sm.handle_wake_detected()
    sm.handle_speech_ended(b"audio")

    sm.handle_api_error("서버에 연결할 수 없습니다.")
    assert sm.state == State.SPEAKING

    sm.handle_tts_completed()
    assert sm.state == State.WAKEWORD_READY
    assert sm.session_id is None

def test_unknown_state_fallback():
    # 12. Unknown state test (safe fallback)
    events = []
    sm = VoiceRuntimeStateMachine(callbacks={
        'on_tts_play': lambda text: events.append(f'tts:{text}'),
    })

    sm.handle_wake_detected()
    sm.handle_speech_ended(b"audio")

    # Unknown state with no clarification needed
    sm.handle_api_response("WEIRD_NEW_STATE", False, "Weird state")
    assert sm.state == State.SPEAKING

    sm.handle_tts_completed()
    # Should fallback to WAKEWORD_READY safely
    assert sm.state == State.WAKEWORD_READY
    assert sm.session_id is None

def test_tts_completed_ignores_if_not_speaking():
    sm = VoiceRuntimeStateMachine(callbacks={})
    sm.handle_tts_completed()
    assert sm.state == State.WAKEWORD_READY
