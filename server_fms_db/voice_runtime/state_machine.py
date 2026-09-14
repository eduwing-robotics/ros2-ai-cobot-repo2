import enum
import uuid
from typing import Callable, Any

class State(enum.Enum):
    WAKEWORD_READY = "WAKEWORD_READY"
    WAKE_ACK = "WAKE_ACK"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"

class VoiceRuntimeStateMachine:
    def __init__(self, callbacks: dict[str, Callable]):
        self.state = State.WAKEWORD_READY
        self.callbacks = callbacks
        self.session_id: str | None = None
        self.next_state_after_tts: State = State.WAKEWORD_READY

    def handle_wake_detected(self) -> None:
        if self.state == State.WAKEWORD_READY:
            self.state = State.WAKE_ACK
            self.session_id = str(uuid.uuid4())
            self.callbacks.get('on_chime', lambda: None)()
            self.state = State.LISTENING
            self.callbacks.get('on_record_start', lambda: None)()

    def handle_no_speech_timeout(self) -> None:
        if self.state == State.LISTENING:
            self.callbacks.get('on_record_stop', lambda: None)()
            self.state = State.WAKEWORD_READY
            self.session_id = None

    def handle_speech_ended(self, audio_data: bytes) -> None:
        if self.state == State.LISTENING:
            self.callbacks.get('on_record_stop', lambda: None)()
            self.state = State.PROCESSING
            self.callbacks.get('on_api_call', lambda s, a: None)(self.session_id, audio_data)

    def handle_api_response(self, conversation_state: str | None, clarification_needed: bool, tts_text: str) -> None:
        NON_TERMINAL_USER_INPUT_STATES = {"COLLECTING_DETAILS", "WAITING_ROOF_OPTION", "AWAITING_CONFIRMATION"}
        TERMINAL_STATES = {"CONFIRMED", "REJECTED", "EXPIRED"}

        if self.state == State.PROCESSING:
            self.state = State.SPEAKING

            if clarification_needed:
                self.next_state_after_tts = State.LISTENING
            elif conversation_state in NON_TERMINAL_USER_INPUT_STATES:
                self.next_state_after_tts = State.LISTENING
            elif conversation_state in TERMINAL_STATES:
                self.next_state_after_tts = State.WAKEWORD_READY
            else:
                # Unknown state or None with no clarification
                self.next_state_after_tts = State.WAKEWORD_READY

            self.callbacks.get('on_tts_play', lambda t: None)(tts_text)

    def handle_api_error(self, error_msg: str, *, continue_listening: bool = False) -> None:
        if self.state == State.PROCESSING:
            self.state = State.SPEAKING
            self.next_state_after_tts = State.LISTENING if continue_listening else State.WAKEWORD_READY
            self.callbacks.get('on_tts_play', lambda t: None)(error_msg)

    def handle_tts_completed(self) -> None:
        if self.state == State.SPEAKING:
            self.state = self.next_state_after_tts
            if self.state == State.LISTENING:
                self.callbacks.get('on_chime', lambda: None)()
                self.callbacks.get('on_record_start', lambda: None)()
            else:
                self.session_id = None
