import logging
logger = logging.getLogger(__name__)

from turndetect import strip_ending_punctuation
from difflib import SequenceMatcher
from colors import Colors
from text_similarity import TextSimilarity
from scipy import signal
import numpy as np
import threading
import textwrap
import torch
import json
import copy
import time
import re
from typing import Optional, Callable, Any, Dict, List

# --- Configuration Flags ---
USE_TURN_DETECTION = True
START_STT_SERVER = False # Set to True to use the client/server version of RealtimeSTT

# --- Recorder Configuration (Moved here for clarity, can be externalized) ---
# Default config if none provided to constructor
DEFAULT_RECORDER_CONFIG: Dict[str, Any] = {
    "use_microphone": False,
    "spinner": False,
    "model": "base.en",
    "realtime_model_type": "base.en",
    "use_main_model_for_realtime": False,
    "language": "en", # Default, will be overridden by source_language in __init__
    "silero_sensitivity": 0.05,
    "webrtc_sensitivity": 3,
    "post_speech_silence_duration": 0.7,
    "min_length_of_recording": 0.5,
    "min_gap_between_recordings": 0,
    "enable_realtime_transcription": True,
    "realtime_processing_pause": 0.03,
    "silero_use_onnx": True,
    "silero_deactivity_detection": True,
    "early_transcription_on_silence": 0,
    "beam_size": 3,
    "beam_size_realtime": 3,
    "no_log_file": True,
    "wake_words": "jarvis",
    "wakeword_backend": "pvporcupine",
    "allowed_latency_limit": 500,
    # Callbacks will be added dynamically in _create_recorder
    "debug_mode": True,
    "initial_prompt_realtime": "The sky is blue. When the sky... She walked home. Because he... Today is sunny. If only I...",
    "faster_whisper_vad_filter": False,
}


if START_STT_SERVER:
    from RealtimeSTT import AudioToTextRecorderClient
else:
    from RealtimeSTT import AudioToTextRecorder

if USE_TURN_DETECTION:
    from turndetect import TurnDetection


INT16_MAX_ABS_VALUE: float = 32768.0
SAMPLE_RATE: int = 16000


class TranscriptionProcessor:
    """
    Manages audio transcription using RealtimeSTT, handling real-time and final
    transcription callbacks, silence detection, turn detection (optional),
    and potential sentence end detection.

    This class acts as a bridge between raw audio input and transcription results,
    coordinating the RealtimeSTT recorder, processing callbacks, and managing
    internal state related to silence, potential sentences, and turn timing.
    """
    # --- Constants for Silence Monitor Logic ---
    # Reserve time to ensure pipeline doesn't start too early or late
    _PIPELINE_RESERVE_TIME_MS: float = 0.02 # 20 ms
    # Offset from end of silence_waiting_time to start considering "hot" state
    _HOT_THRESHOLD_OFFSET_S: float = 0.35
    # Minimum duration for the "hot" condition to be meaningful
    _MIN_HOT_CONDITION_DURATION_S: float = 0.15
    # Time before full silence duration when TTS synthesis might be allowed
    _TTS_ALLOWANCE_OFFSET_S: float = 0.25
    # Minimum time for potential sentence end detection relative to silence start
    _MIN_POTENTIAL_END_DETECTION_TIME_MS: float = 0.02 # 20 ms
    # Maximum age for cached sentence end timestamps (ms)
    _SENTENCE_CACHE_MAX_AGE_MS: float = 0.2
    # Number of detections within the cache age required to trigger potential end
    _SENTENCE_CACHE_TRIGGER_COUNT: int = 3


    def __init__(
            self,
            source_language: str = "en",
            realtime_transcription_callback: Optional[Callable[[str], None]] = None,
            full_transcription_callback: Optional[Callable[[str], None]] = None,
            potential_full_transcription_callback: Optional[Callable[[str], None]] = None,
            potential_full_transcription_abort_callback: Optional[Callable[[], None]] = None,
            potential_sentence_end: Optional[Callable[[str], None]] = None,
            before_final_sentence: Optional[Callable[[Optional[np.ndarray], Optional[str]], bool]] = None,
            silence_active_callback: Optional[Callable[[bool], None]] = None,
            on_recording_start_callback: Optional[Callable[[], None]] = None,
            on_speech_start_utterance_callback: Optional[Callable[[], None]] = None, # New callback
            on_tts_allowed_to_synthesize: Optional[Callable[[], None]] = None, # For TTS allowance
            is_orpheus: bool = False,
            local: bool = True,
            tts_allowed_event: Optional[threading.Event] = None, # Note: This seems unused in the original code provided
            pipeline_latency: float = 0.5,
            recorder_config: Optional[Dict[str, Any]] = None, # Allow passing custom config
    ) -> None:
        """
        Initializes the TranscriptionProcessor.

        Args:
            source_language: Language code for transcription (e.g., "en").
            realtime_transcription_callback: Callback for real-time transcription updates. Receives partial text.
            full_transcription_callback: Callback for final transcription result. Receives final text.
            potential_full_transcription_callback: Callback triggered when a full transcription is likely imminent (in "hot" state). Receives current real-time text.
            potential_full_transcription_abort_callback: Callback triggered when the "hot" state ends before final transcription.
            potential_sentence_end: Callback triggered when a potential sentence end is detected. Receives the potentially complete sentence text.
            before_final_sentence: Callback triggered just before the recorder finalizes transcription. Receives audio copy and current real-time text. Return True to potentially influence recorder behavior (if supported).
            silence_active_callback: Callback triggered when silence detection state changes. Receives boolean (True if silence is active).
            on_recording_start_callback: Callback triggered when the recorder starts recording after silence or wake word.
            on_speech_start_utterance_callback: Callback triggered when VAD detects start of speech after silence. # New callback doc
            on_tts_allowed_to_synthesize: Callback triggered when TTS synthesis can proceed.
            is_orpheus: Flag indicating if specific timing adjustments for 'Orpheus' mode should be used.
            local: Flag used by TurnDetection (if enabled) to indicate local vs remote processing context.
            tts_allowed_event: An event that might be set when TTS synthesis is allowed (currently unused in provided logic).
            pipeline_latency: Estimated latency of the downstream processing pipeline in seconds. Used for timing calculations.
            recorder_config: Optional dictionary to override default RealtimeSTT recorder configuration.
        """
        self.source_language = source_language
        self.realtime_transcription_callback = realtime_transcription_callback
        self.full_transcription_callback = full_transcription_callback
        self.potential_full_transcription_callback = potential_full_transcription_callback
        self.potential_full_transcription_abort_callback = potential_full_transcription_abort_callback
        self.potential_sentence_end = potential_sentence_end
        self.before_final_sentence = before_final_sentence
        self.silence_active_callback = silence_active_callback
        self.on_recording_start_callback = on_recording_start_callback
        self.on_speech_start_utterance_callback = on_speech_start_utterance_callback # Store new callback
        self.is_orpheus = is_orpheus
        self.pipeline_latency = pipeline_latency
        self.recorder: Optional[AudioToTextRecorder | AudioToTextRecorderClient] = None
        self.is_silero_speech_active: bool = False # Note: Seems unused
        self.silero_working: bool = False         # Note: Seems unused
        self.on_wakeword_detection_start: Optional[Callable] = None # Note: Seems unused
        self.on_wakeword_detection_end: Optional[Callable] = None   # Note: Seems unused
        self.realtime_text: Optional[str] = None
        self.sentence_end_cache: List[Dict[str, Any]] = []
        self.potential_sentences_yielded: List[Dict[str, Any]] = []
        self.stripped_partial_user_text: str = ""
        self.final_transcription: Optional[str] = None
        self.shutdown_performed: bool = False
        self.silence_time: float = 0.0
        self.silence_active: bool = False
        self.last_audio_copy: Optional[np.ndarray] = None

        self.on_tts_allowed_to_synthesize: Optional[Callable] = on_tts_allowed_to_synthesize

        self.text_similarity = TextSimilarity(focus='end', n_words=5)

        # Use provided config or default
        self.recorder_config = copy.deepcopy(recorder_config if recorder_config else DEFAULT_RECORDER_CONFIG)
        self.recorder_config['language'] = self.source_language # Ensure language is set

        if USE_TURN_DETECTION:
            logger.info(f"👂🔄 {Colors.YELLOW}Turn detection enabled{Colors.RESET}")
            self.turn_detection = TurnDetection(
                on_new_waiting_time=self.on_new_waiting_time,
                local=local,
                pipeline_latency=pipeline_latency
            )

        self._create_recorder()
        self._start_silence_monitor()

    # --- Recorder Parameter Abstraction ---

    def _get_recorder_param(self, param_name: str, default: Any = None) -> Any:
        """
        Internal helper to get a parameter from the recorder instance,
        abstracting client/server differences.

        Args:
            param_name: The name of the parameter to retrieve.
            default: The value to return if the recorder is not initialized or
                     the parameter doesn't exist.

        Returns:
            The value of the recorder parameter or the default value.
        """
        if not self.recorder:
            return default
        if START_STT_SERVER:
            # Assuming get_parameter exists and handles potential errors
            return self.recorder.get_parameter(param_name) # type: ignore
        else:
            # Access attribute directly for the local version
            return getattr(self.recorder, param_name, default)

    def _set_recorder_param(self, param_name: str, value: Any) -> None:
        """
        Internal helper to set a parameter on the recorder instance,
        abstracting client/server differences.

        Args:
            param_name: The name of the parameter to set.
            value: The value to set the parameter to.
        """
        if not self.recorder:
            return
        if START_STT_SERVER:
            # Assuming set_parameter exists and handles potential errors
            self.recorder.set_parameter(param_name, value) # type: ignore
        else:
            # Set attribute directly for the local version
            setattr(self.recorder, param_name, value)

    def _is_recorder_recording(self) -> bool:
        """
        Internal helper to check if the recorder is currently recording,
        abstracting client/server differences.

        Returns:
            True if the recorder is active and recording, False otherwise.
        """
        if not self.recorder:
            return False
        if START_STT_SERVER:
            return self.recorder.get_parameter("is_recording") # type: ignore
        else:
            # Ensure the attribute exists before accessing
            return getattr(self.recorder, "is_recording", False)

    # --- Silence Monitor ---
    def _start_silence_monitor(self) -> None:
        """
        Starts a background thread to monitor silence duration and trigger
        events like potential sentence end detection, TTS synthesis allowance,
        and potential full transcription ("hot") state changes.
        """
        def monitor():
            hot = False
            # Initialize silence_time using the abstracted getter
            self.silence_time = self._get_recorder_param("speech_end_silence_start", 0.0)

            while not self.shutdown_performed:
                speech_end_silence_start = self.silence_time # Use cached value updated by callback

                if self.recorder and speech_end_silence_start is not None and speech_end_silence_start != 0:
                    silence_waiting_time = self._get_recorder_param("post_speech_silence_duration", 0.0)
                    time_since_silence = time.time() - speech_end_silence_start

                    # Calculate latest time pipeline can start without exceeding silence duration
                    latest_pipe_start_time = silence_waiting_time - self.pipeline_latency - self._PIPELINE_RESERVE_TIME_MS

                    # Calculate the target time to trigger potential sentence end detection
                    potential_sentence_end_time = latest_pipe_start_time
                    # Ensure it doesn't trigger too early
                    if potential_sentence_end_time < self._MIN_POTENTIAL_END_DETECTION_TIME_MS:
                        potential_sentence_end_time = self._MIN_POTENTIAL_END_DETECTION_TIME_MS

                    # Determine the threshold time to enter the "hot" state
                    start_hot_condition_time = silence_waiting_time - self._HOT_THRESHOLD_OFFSET_S
                    # Ensure the hot condition has a minimum meaningful duration
                    if start_hot_condition_time < self._MIN_HOT_CONDITION_DURATION_S:
                        start_hot_condition_time = self._MIN_HOT_CONDITION_DURATION_S

                    # Adjust potential_sentence_end_time based on Orpheus mode
                    if self.is_orpheus:
                         # For Orpheus, ensure potential end detection doesn't happen too early relative to hot state
                        orpheus_potential_end_time = silence_waiting_time - self._HOT_THRESHOLD_OFFSET_S
                        if potential_sentence_end_time < orpheus_potential_end_time:
                             potential_sentence_end_time = orpheus_potential_end_time


                    # --- Trigger Actions Based on Timing ---

                    # 1. Force potential sentence end detection if time has passed
                    if time_since_silence > potential_sentence_end_time:
                        # Check if realtime_text exists before logging/detecting
                        current_text = self.realtime_text if self.realtime_text else ""
                        logger.info(f"👂🔚 {Colors.YELLOW}Potential sentence end detected (timed out){Colors.RESET}: {current_text}")
                        # Use force_yield=True because this is triggered by timeout, not punctuation detection
                        self.detect_potential_sentence_end(current_text, force_yield=True, force_ellipses=True) # Force ellipses if timeout occurs

                    # 2. Allow TTS synthesis shortly before the final silence duration elapses
                    tts_allowance_time = silence_waiting_time - self._TTS_ALLOWANCE_OFFSET_S
                    if time_since_silence > tts_allowance_time:
                        if self.on_tts_allowed_to_synthesize: # Check if callback exists
                            self.on_tts_allowed_to_synthesize()

                    # 3. Handle "Hot" state (potential full transcription)
                    hot_condition_met = time_since_silence > start_hot_condition_time
                    if hot_condition_met and not hot:
                        hot = True
                        print(f"{Colors.MAGENTA}HOT{Colors.RESET}")
                        if self.potential_full_transcription_callback:
                            self.potential_full_transcription_callback(self.realtime_text)
                    elif not hot_condition_met and hot:
                        # Transitioning from Hot to Cold while still in silence period (e.g., silence_waiting_time changed)
                        if self._is_recorder_recording(): # Check if still recording before aborting
                            print(f"{Colors.CYAN}COLD (during silence){Colors.RESET}")
                            if self.potential_full_transcription_abort_callback:
                                self.potential_full_transcription_abort_callback()
                        hot = False

                elif hot: # Exited silence period (speech_end_silence_start is 0 or None)
                    # If we were hot, but silence ended (e.g., new speech started), transition to cold
                    if self._is_recorder_recording(): # Check if recording actually restarted
                         print(f"{Colors.CYAN}COLD (silence ended){Colors.RESET}")
                         if self.potential_full_transcription_abort_callback:
                             self.potential_full_transcription_abort_callback()
                    hot = False

                time.sleep(0.001) # Short sleep to prevent busy-waiting dominating CPU

        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()

    def on_new_waiting_time(
            self,
            waiting_time: float,
            text: Optional[str] = None,
        ) -> None:
        """
        Callback handler for when TurnDetection calculates a new waiting time.
        Updates the recorder's post_speech_silence_duration parameter.

        Args:
            waiting_time: The new calculated silence duration in seconds.
            text: The text used by TurnDetection to calculate the waiting time (for logging).
        """
        if self.recorder:
            current_duration = self._get_recorder_param("post_speech_silence_duration")
            if current_duration != waiting_time:
                log_text = text if text else "(No text provided)"
                logger.info(f"👂⏳ {Colors.GRAY}New waiting time: {Colors.RESET}{Colors.YELLOW}{waiting_time:.2f}{Colors.RESET}{Colors.GRAY} for text: {log_text}{Colors.RESET}")
                self._set_recorder_param("post_speech_silence_duration", waiting_time)
        else:
            logger.warning("👂⚠️ Recorder not initialized, cannot set new waiting time.")

    def transcribe_loop(self) -> None:
        """
        Sets up the final transcription callback mechanism with the recorder.

        This method defines the `on_final` callback that will be invoked by the
        recorder when a complete utterance transcription is available. It then
        registers this callback with the recorder instance.
        """
        def on_final(text: Optional[str]):
            if text is None or text == "":
                logger.warning("👂❓ Final transcription received None or empty string.")
                return

            self.final_transcription = text
            logger.info(f"👂✅ {Colors.apply('Final user text: ').green} {Colors.apply(text).yellow}")
            self.sentence_end_cache.clear()
            self.potential_sentences_yielded.clear()

            if USE_TURN_DETECTION and hasattr(self, 'turn_detection'):
                self.turn_detection.reset()
            if self.full_transcription_callback:
                self.full_transcription_callback(text)

        if self.recorder:
            # The specific method might differ between client/local STT versions
            # Assuming a common 'text' method exists or is adapted
            if hasattr(self.recorder, 'text'):
                self.recorder.text(on_final) # type: ignore # Assume method exists
            elif START_STT_SERVER:
                 logger.warning("👂⚠️ Recorder client does not have a 'text' method. Attempting to set 'on_final_transcription' parameter.")
                 # Attempt to set via parameter for client, might not be the correct API
                 try:
                     self._set_recorder_param('on_final_transcription', on_final)
                 except Exception as e:
                     logger.error(f"👂💥 Failed to set final transcription callback parameter for client: {e}")
            else:
                logger.warning("👂⚠️ Local recorder object does not have a 'text' method for final callback.")
        else:
            logger.error("👂❌ Cannot set final callback: Recorder not initialized.")


    def abort_generation(self) -> None:
        """
        Clears the cache of potentially yielded sentences.

        This effectively stops any further actions that might be triggered based
        on previously detected potential sentence ends, useful if processing needs
        to be reset or interrupted externally.
        """
        self.potential_sentences_yielded.clear()
        logger.info("👂⏹️ Potential sentence yield cache cleared (generation aborted).")

    def perform_final(self, audio_bytes: Optional[bytes] = None) -> None:
        """
        Manually triggers the final transcription process using the last known
        real-time text.

        This bypasses the recorder's natural end-of-speech detection and immediately
        invokes the final transcription callback with the `self.realtime_text` content.
        Useful for scenarios where transcription needs to be finalized externally.

        Args:
            audio_bytes: Optional audio data (currently unused in this method's logic
                         but kept for potential future use or API consistency).
        """
        if self.recorder: # Check if recorder exists, primarily as a gatekeeper
            if self.realtime_text is None:
                logger.warning(f"👂❓ {Colors.RED}Forcing final transcription, but realtime_text is None. Using empty string.{Colors.RESET}")
                current_text = ""
            else:
                current_text = self.realtime_text

            self.final_transcription = current_text # Update internal state
            logger.info(f"👂❗ {Colors.apply('Forced Final user text: ').green} {Colors.apply(current_text).yellow}")
            self.sentence_end_cache.clear()
            self.potential_sentences_yielded.clear()

            if USE_TURN_DETECTION and hasattr(self, 'turn_detection'):
                self.turn_detection.reset()
            if self.full_transcription_callback:
                self.full_transcription_callback(current_text)
        else:
            logger.warning("👂⚠️ Cannot perform final: Recorder not initialized.")


    def _normalize_text(self, text: str) -> str:
        """
        Internal helper to normalize text for comparison purposes.
        Converts to lowercase, removes non-alphanumeric characters (except spaces),
        and collapses extra whitespace.

        Args:
            text: The input string to normalize.

        Returns:
            The normalized string.
        """
        text = text.lower()
        # Remove all non-alphanumeric characters (keeping spaces)
        text = re.sub(r'[^a-z0-9\s]', '', text) # Keep spaces for SequenceMatcher
        # Remove extra whitespace and trim
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def is_basically_the_same(
            self,
            text1: str,
            text2: str,
            similarity_threshold: float = 0.96, # Consider making this configurable
        ) -> bool:
        """
        Checks if two text strings are highly similar, focusing on the ending words.
        Uses the internal TextSimilarity instance.

        Args:
            text1: The first text string.
            text2: The second text string.
            similarity_threshold: The minimum similarity score (0 to 1) to consider
                                  the texts the same.

        Returns:
            True if the similarity score exceeds the threshold, False otherwise.
        """
        # Use the dedicated TextSimilarity class instance
        similarity = self.text_similarity.calculate_similarity(text1, text2)
        return similarity > similarity_threshold

    def detect_potential_sentence_end(self, text: Optional[str], force_yield: bool = False, force_ellipses: bool = False) -> None:
        """
        Detects potential sentence endings based on ending punctuation and timing stability.

        Checks if the provided text ends with sentence-ending punctuation (., !, ?).
        If so, it caches the normalized text and its timestamp. If the same normalized
        text ending appears frequently within a short time window or if `force_yield`
        is True (e.g., due to silence timeout), it triggers the `potential_sentence_end`
        callback, avoiding redundant triggers for the same sentence.

        Args:
            text: The real-time transcription text to check.
            force_yield: If True, bypasses punctuation and timing checks and forces
                         triggering the callback (if text is valid and not already yielded).
            force_ellipses: If True (used with `force_yield`), allows "..." to be
                            considered a sentence end.
        """
        if not text:
            return

        stripped_text_raw = text.strip() # Keep original for callback
        if not stripped_text_raw:
            return

        # Don't consider ellipses as sentence end unless forced
        if stripped_text_raw.endswith("...") and not force_ellipses:
            return

        end_punctuations = [".", "!", "?"]
        now = time.time()

        # Only proceed if text ends with a standard punctuation mark or if forced
        ends_with_punctuation = any(stripped_text_raw.endswith(p) for p in end_punctuations)
        if not ends_with_punctuation and not force_yield:
            return

        normalized_text = self._normalize_text(stripped_text_raw)
        if not normalized_text: # Handle cases where normalization leaves empty string
            return

        # --- Cache Management ---
        entry_found = None
        for entry in self.sentence_end_cache:
            # Check if normalized text matches cached entry
            if self.is_basically_the_same(entry['text'], normalized_text):
                entry_found = entry
                break

        if entry_found:
            entry_found['timestamps'].append(now)
            # Keep only recent timestamps
            entry_found['timestamps'] = [t for t in entry_found['timestamps'] if now - t <= self._SENTENCE_CACHE_MAX_AGE_MS]
        else:
            # Add new entry
            entry_found = {'text': normalized_text, 'timestamps': [now]}
            self.sentence_end_cache.append(entry_found)
            # Optional: Limit cache size if it grows too large over time
            # MAX_CACHE_SIZE = 50
            # if len(self.sentence_end_cache) > MAX_CACHE_SIZE:
            #     self.sentence_end_cache.pop(0) # Remove oldest

        # --- Yielding Logic ---
        should_yield = False
        if force_yield:
            should_yield = True
        # Yield if the same sentence ending appeared multiple times recently
        elif ends_with_punctuation and len(entry_found['timestamps']) >= self._SENTENCE_CACHE_TRIGGER_COUNT:
             should_yield = True


        if should_yield:
            # Check if this *exact* normalized text was already yielded recently
            already_yielded = False
            for yielded_entry in self.potential_sentences_yielded:
                # Using the same similarity check for yielded text
                if self.is_basically_the_same(yielded_entry['text'], normalized_text):
                    already_yielded = True
                    break

            if not already_yielded:
                # Add to yielded list (use normalized text for comparison, keep timestamp)
                self.potential_sentences_yielded.append({'text': normalized_text, 'timestamp': now})
                # Optional: Limit yielded list size
                # MAX_YIELDED_SIZE = 20
                # if len(self.potential_sentences_yielded) > MAX_YIELDED_SIZE:
                #    self.potential_sentences_yielded.pop(0)

                logger.info(f"👂➡️ Yielding potential sentence end: {stripped_text_raw}")
                if self.potential_sentence_end:
                    self.potential_sentence_end(stripped_text_raw) # Callback with original punctuation
            # else: # No need to log this every time, can be noisy
                 # logger.debug(f"👂➡️ Sentence '{normalized_text}' matched yielded '{yielded_entry.get('text', '')}', not yielding again.")


    def set_silence(self, silence_active: bool) -> None:
        """
        Updates the internal silence state and triggers the silence_active_callback.

        Args:
            silence_active: The new silence state (True if silence is now active).
        """
        if self.silence_active != silence_active:
            self.silence_active = silence_active
            logger.info(f"👂🤫 Silence state changed: {'ACTIVE' if silence_active else 'INACTIVE'}")
            if self.silence_active_callback:
                self.silence_active_callback(silence_active)

    def get_last_audio_copy(self) -> Optional[np.ndarray]:
        """
        Returns the last successfully captured audio buffer as a float32 NumPy array.

        Attempts to get the current audio buffer first. If successful, updates the
        internal cache (`last_audio_copy`) and returns the new copy. If getting the
        current buffer fails (e.g., recorder not ready, empty buffer), it returns
        the previously cached buffer.

        Returns:
            A float32 NumPy array representing the audio, normalized to [-1.0, 1.0],
            or None if no audio has been successfully captured yet.
        """
        # Attempt to get current audio first
        audio_copy = self.get_audio_copy()

        if audio_copy is not None and len(audio_copy) > 0:
            # If successful, update and return the new copy
            # self.last_audio_copy is already updated within get_audio_copy() if successful
            return audio_copy
        else:
            # If getting current audio failed, return the last known good copy
            logger.debug("👂💾 Returning last known audio copy as current fetch failed or yielded empty.")
            return self.last_audio_copy

    def get_audio_copy(self) -> Optional[np.ndarray]:
        """
        Copies the current audio buffer from the recorder's frames.

        Retrieves the raw audio frames, concatenates them, converts to a float32
        NumPy array normalized to [-1.0, 1.0], and returns a deep copy. Updates
        `self.last_audio_copy` if successful. If the recorder is unavailable,
        frames are empty, or an error occurs, it returns the `last_audio_copy`.

        Returns:
            A deep copy of the current audio buffer as a float32 NumPy array,
            or the last known good copy if the current fetch fails, or None
            if no audio has ever been successfully captured.
        """
        if not self.recorder:
             logger.warning("👂⚠️ Cannot get audio copy: Recorder not initialized.")
             return self.last_audio_copy # Return last known good copy if available
        if not hasattr(self.recorder, 'frames'):
             logger.warning("👂⚠️ Cannot get audio copy: Recorder has no 'frames' attribute.")
             return self.last_audio_copy

        try:
             # Access frames safely
             # Ensure frames is thread-safe if accessed concurrently
             with self.recorder.frames_lock if hasattr(self.recorder, 'frames_lock') else threading.Lock(): # Use recorder's lock if available
                 frames_data = list(self.recorder.frames) # Create a copy of the deque items

             if not frames_data:
                 logger.debug("👂💾 Recorder frames buffer is currently empty.")
                 return self.last_audio_copy # Return last known if current is empty

             # Process audio buffer
             full_audio_array = np.frombuffer(b''.join(frames_data), dtype=np.int16)
             if full_audio_array.size == 0:
                 logger.debug("👂💾 Recorder frames buffer resulted in empty array after join.")
                 return self.last_audio_copy # Return last known if buffer is empty after join

             full_audio = full_audio_array.astype(np.float32) / INT16_MAX_ABS_VALUE
             # No need for deepcopy here as full_audio is a new array derived from the buffer
             audio_copy = full_audio

             # Update last_audio_copy only if the new copy is valid and has data
             if audio_copy is not None and len(audio_copy) > 0:
                 self.last_audio_copy = audio_copy
                 logger.debug(f"👂💾 Successfully got audio copy (length: {len(audio_copy)} samples).")


             return audio_copy
        except Exception as e:
             logger.error(f"👂💥 Error getting audio copy: {e}", exc_info=True)
             return self.last_audio_copy # Return last known on error

    def _create_recorder(self) -> None:
        """
        Internal helper to initialize the RealtimeSTT recorder instance
        (either local or client) with the specified configuration and callbacks.
        """

        # Define callbacks locally to capture `self`
        def start_silence_detection():
            """Callback triggered when recorder detects start of silence (end of speech)."""
            self.set_silence(True)
            # Capture silence start time immediately. Use recorder's time if available.
            recorder_silence_start = self._get_recorder_param("speech_end_silence_start", None)
            self.silence_time = recorder_silence_start if recorder_silence_start else time.time()
            logger.debug(f"👂🤫 Silence detected (start_silence_detection called). Silence time set to: {self.silence_time}")


        def stop_silence_detection():
            """Callback triggered when recorder detects end of silence (start of speech)."""
            self.set_silence(False)
            self.silence_time = 0.0 # Reset silence time
            logger.debug("👂🗣️ Speech detected (stop_silence_detection called). Silence time reset.")
            if self.on_speech_start_utterance_callback:
                logger.info("👂🚀 Invoking on_speech_start_utterance_callback.")
                self.on_speech_start_utterance_callback()


        def start_recording():
            """Callback triggered when recorder starts a new recording segment."""
            logger.info("👂▶️ Recording started.")
            self.set_silence(False) # Ensure silence is marked inactive
            self.silence_time = 0.0   # Ensure silence timer is reset
            if self.on_recording_start_callback:
                self.on_recording_start_callback()

        def stop_recording() -> bool:
            """
            Callback triggered when recorder stops a recording segment, just
            before final transcription might be generated.
            """
            logger.info("👂⏹️ Recording stopped.")
            # Get audio *before* recorder might clear it for final processing
            audio_copy = self.get_last_audio_copy() # Use get_last_audio_copy for robustness
            if self.before_final_sentence:
                logger.debug("👂➡️ Calling before_final_sentence callback...")
                # Pass the audio and the *current* realtime text
                try:
                    # Return value might influence recorder, pass it through.
                    # Default to False if callback returns None or throws error
                    result = self.before_final_sentence(audio_copy, self.realtime_text)
                    return result if isinstance(result, bool) else False
                except Exception as e:
                    logger.error(f"👂💥 Error in before_final_sentence callback: {e}", exc_info=True)
                    return False # Ensure False is returned on error
            return False # Indicate no action taken if callback doesn't exist or doesn't return True

        def on_partial(text: Optional[str]):
            """Callback triggered for real-time transcription updates."""
            if text is None:
                # logger.warning(f"👂❓ {Colors.RED}Partial text received None{Colors.RESET}") # Can be noisy
                return
            self.realtime_text = text # Update the latest realtime text

            # Detect potential sentence ends based on punctuation stability
            self.detect_potential_sentence_end(text)

            # Process for partial transcription callback and turn detection
            stripped_partial_user_text_new = strip_ending_punctuation(text)
            # Log only significant changes or all partials based on debug level maybe
            if stripped_partial_user_text_new != self.stripped_partial_user_text:
                self.stripped_partial_user_text = stripped_partial_user_text_new
                logger.info(f"👂📝 Partial transcription: {Colors.CYAN}{text}{Colors.RESET}")
                if self.realtime_transcription_callback:
                    self.realtime_transcription_callback(text)
                if USE_TURN_DETECTION and hasattr(self, 'turn_detection'):
                    self.turn_detection.calculate_waiting_time(text=text)
            else: # Log less critical updates differently (optional, uncomment if needed)
                 logger.debug(f"👂📝 Partial transcription (no change after strip): {Colors.GRAY}{text}{Colors.RESET}")


        # --- Prepare Recorder Configuration ---
        # Start with the instance's config (either default or user-provided)
        active_config = self.recorder_config.copy()

        # Add dynamically assigned callbacks using the CORRECT keys for AudioToTextRecorder
        active_config["on_realtime_transcription_update"] = on_partial
        # *** CORRECTED MAPPING ***
        active_config["on_turn_detection_start"] = start_silence_detection # Triggered when silence starts (speech ends)
        active_config["on_turn_detection_stop"] = stop_silence_detection  # Triggered when silence stops (speech starts)
        # *** END CORRECTION ***
        active_config["on_recording_start"] = start_recording
        active_config["on_recording_stop"] = stop_recording # This callback happens before final text

        # Log the configuration being used
        def _pretty(v, max_len=60):
            if callable(v): return f"[callback: {v.__name__}]"
            if isinstance(v, str):
                one_line = v.replace("\n", " ")
                return (one_line[:max_len].rstrip() + " [...]") if len(one_line) > max_len else one_line
            return v

        pretty_cfg = {k: _pretty(v) for k, v in active_config.items()}
        # Ensure sensitive or overly long items are handled if necessary
        # Example: if 'api_key' in pretty_cfg: pretty_cfg['api_key'] = '********'
        padded_cfg = textwrap.indent(json.dumps(pretty_cfg, indent=2), "    ")

        recorder_type = "AudioToTextRecorderClient" if START_STT_SERVER else "AudioToTextRecorder"
        logger.info(f"👂⚙️ Creating {recorder_type} with params:\n{padded_cfg}") # Changed print to logger.info


        # --- Instantiate Recorder ---
        try:
            if START_STT_SERVER:
                # Note: The client might use different callback names, adjust if needed
                # For now, assume it might accept the same or handle internally
                self.recorder = AudioToTextRecorderClient(**active_config)
                # Ensure wake words are disabled if needed (can also be done via config dict)
                self._set_recorder_param("use_wake_words", False)
            else:
                # Instantiate the LOCAL recorder with the corrected active_config
                self.recorder = AudioToTextRecorder(**active_config)
                # Ensure wake words are disabled if needed (double check via param setting)
                self._set_recorder_param("use_wake_words", False) # Uses the helper method

            logger.info(f"👂✅ {recorder_type} instance created successfully.")

        except Exception as e:
            # Log the exception with traceback for detailed debugging
            logger.exception(f"👂🔥 Failed to create recorder: {e}")
            self.recorder = None # Ensure recorder is None if creation failed

    def feed_audio(self, chunk: bytes, audio_meta_data: Optional[Dict[str, Any]] = None) -> None:
        """
        Feeds an audio chunk to the underlying recorder instance for processing.

        Args:
            chunk: A bytes object containing the raw audio data chunk.
            audio_meta_data: Optional dictionary containing metadata about the audio
                             (e.g., sample rate, channels), if required by the recorder.
        """
        # 记录音频块信息和元数据
        chunk_size = len(chunk)
        logger.info(f"👂🎙️ Feeding audio chunk to RealtimeSTT recorder, size: {chunk_size} bytes.") # Refined message
        
        # 记录元数据信息
        if audio_meta_data:
            logger.debug(f"👂📋 Metadata with chunk: {audio_meta_data}") # Changed to debug
            
            # 更新静音时间（如果提供）
            if "speech_end_silence_start" in audio_meta_data:
                old_silence_time = self.silence_time
                self.silence_time = audio_meta_data["speech_end_silence_start"]
                if old_silence_time != self.silence_time:
                    logger.info(f"👂⏱️ 更新静音时间: {old_silence_time} -> {self.silence_time}") # Kept as info - significant event
        
        if self.recorder and not self.shutdown_performed:
            try:
                # Check if feed_audio expects metadata and provide if available
                if START_STT_SERVER:
                     # Client might require metadata in a specific format
                     # logger.info(f"👂➡️ 将音频块传递给STT服务器，大小: {chunk_size} 字节") # Removed
                     self.recorder.feed_audio(chunk) # Assuming client handles metadata internally or doesn't need it per chunk
                else:
                     # Local recorder might use metadata if provided
                     # logger.info(f"👂➡️ 将音频块传递给本地录音器，大小: {chunk_size} 字节") # Removed
                     self.recorder.feed_audio(chunk) # Assuming local handles it similarly for now

                logger.debug(f"👂✅ Chunk successfully fed to recorder, size: {chunk_size} bytes.") # Changed to debug
            except Exception as e:
                logger.error(f"👂💥 Error feeding audio to recorder: {e}")
                import traceback
                logger.error(traceback.format_exc())
        elif not self.recorder:
            logger.warning("👂⚠️ Cannot feed audio: Recorder not initialized.")
        elif self.shutdown_performed:
            logger.debug("👂🚫 Cannot feed audio: Shutdown already performed.")
        # No warning if shutdown_performed is True, as expected

    def shutdown(self) -> None:
        """
        Shuts down the recorder instance, cleans up resources, and prevents
        further processing. Sets the `shutdown_performed` flag.
        """
        if not self.shutdown_performed:
            logger.info("👂🔌 Shutting down TranscriptionProcessor...")
            self.shutdown_performed = True # Set flag early to stop loops/threads

            if self.recorder:
                logger.info("👂🔌 Calling recorder shutdown()...")
                try:
                    self.recorder.shutdown()
                    logger.info("👂🔌 Recorder shutdown() method completed.")
                except Exception as e:
                    logger.error(f"👂💥 Error during recorder shutdown: {e}", exc_info=True)
                finally:
                    self.recorder = None
            else:
                logger.info("👂🔌 No active recorder instance to shut down.")

            # Clean up other resources if necessary (e.g., turn detection?)
            if USE_TURN_DETECTION and hasattr(self, 'turn_detection') and hasattr(self.turn_detection, 'shutdown'):
                logger.info("👂🔌 Shutting down TurnDetection...")
                try:
                    self.turn_detection.shutdown() # Example: Assuming TurnDetection has a shutdown method
                except Exception as e:
                     logger.error(f"👂💥 Error during TurnDetection shutdown: {e}", exc_info=True)

            logger.info("👂🔌 TranscriptionProcessor shutdown process finished.")
        else:
            logger.info("👂ℹ️ Shutdown already performed.")