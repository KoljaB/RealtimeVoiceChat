# speech_pipeline_manager.py
from typing import Optional, Callable
import threading
import logging
import time

import asyncio 
from queue import Queue, Empty
import sys

# (Make sure real/mock imports are correct)
from audio_module import AudioProcessor
from text_similarity import TextSimilarity
from text_context import TextContext
from llm_module import LLM
from colors import Colors

# (Logging setup)
logger = logging.getLogger(__name__)


# System prompt loading is now handled within the SpeechPipelineManager class based on modes.
USE_ORPHEUS_UNCENSORED = False

orpheus_prompt_addon_normal = """
When expressing emotions, you are ONLY allowed to use the following exact tags (including the spaces):
" <laugh> ", " <chuckle> ", " <sigh> ", " <cough> ", " <sniffle> ", " <groan> ", " <yawn> ", and " <gasp> ".

Do NOT create or use any other emotion tags. Do NOT remove the spaces. Use these tags exactly as shown, and only when appropriate.
""".strip()

orpheus_prompt_addon_uncensored = """
When expressing emotions, you are ONLY allowed to use the following exact tags (including the spaces):
" <moans> ", " <panting> ", " <grunting> ", " <gagging sounds> ", " <chokeing> ", " <kissing noises> ", " <laugh> ", " <chuckle> ", " <sigh> ", " <cough> ", " <sniffle> ", " <groan> ", " <yawn> ", " <gasp> ".
Do NOT create or use any other emotion tags. Do NOT remove the spaces. Use these tags exactly as shown, and only when appropriate.
""".strip()

orpheus_prompt_addon = orpheus_prompt_addon_uncensored if USE_ORPHEUS_UNCENSORED else orpheus_prompt_addon_normal


class PipelineRequest:
    """
    Represents a request to be processed by the SpeechPipelineManager's request queue.

    Holds information about the action to perform (e.g., 'prepare', 'abort'),
    associated data (e.g., text input), and a timestamp for potential de-duplication.
    """
    def __init__(self, action: str, data: Optional[any] = None):
        """
        Initializes a PipelineRequest instance.

        Args:
            action: The type of action requested (e.g., "prepare", "abort").
            data: Optional data associated with the action (e.g., input text for "prepare").
        """
        self.action = action
        self.data = data
        self.timestamp = time.time()

class RunningGeneration:
    """
    Holds the state and resources for a single, ongoing text-to-speech generation process.

    This includes the generation ID, input text, the LLM generator object, flags indicating
    the status of LLM and TTS stages (quick and final), threading events for synchronization,
    queues for audio chunks, and text buffers for partial/complete answers.
    """
    def __init__(self, id: int):
        """
        Initializes a RunningGeneration state object.

        Args:
            id: A unique identifier for this generation attempt.
        """
        self.id: int = id # Store the generation ID
        self.text: Optional[str] = None
        self.timestamp = time.time()

        self.llm_generator = None
        self.llm_finished: bool = False
        self.llm_finished_event = threading.Event()
        self.llm_aborted: bool = False

        self.quick_answer: str = ""
        self.quick_answer_provided: bool = False
        self.quick_answer_first_chunk_ready: bool = False
        self.quick_answer_overhang: str = "" # This is the part of the text that was not used in the context
        self.tts_quick_started: bool = False

        self.tts_quick_allowed_event = threading.Event()
        self.audio_chunks = Queue()
        self.audio_quick_finished: bool = False
        self.audio_quick_aborted: bool = False
        self.tts_quick_finished_event = threading.Event()

        self.abortion_started: bool = False

        self.tts_final_finished_event = threading.Event()
        self.tts_final_started: bool = False
        self.audio_final_aborted: bool = False
        self.audio_final_finished: bool = False
        self.final_answer: str = ""

        self.completed: bool = False


class SpeechPipelineManager:
    """
    Orchestrates the text-to-speech pipeline, managing LLM and TTS workers.

    This class handles incoming text requests, manages the lifecycle of a generation
    (including LLM inference, TTS synthesis for both quick and final parts),
    facilitates aborting ongoing generations, manages conversation history,
    and coordinates worker threads using queues and events.
    """
    def __init__(
            self,
            prompt_modes: dict, # Added
            initial_mode: str, # Added
            tts_engine: str = "kokoro",
            llm_provider: str = "ollama",
            llm_model: str = "hf.co/bartowski/huihui-ai_Mistral-Small-24B-Instruct-2501-abliterated-GGUF:Q4_K_M",
            no_think: bool = False,
            orpheus_model: str = "orpheus-3b-0.1-ft-Q8_0-GGUF/orpheus-3b-0.1-ft-q8_0.gguf",
        ):
        """
        Initializes the SpeechPipelineManager.

        Sets up configuration, instantiates dependencies (AudioProcessor, LLM, etc.),
        initializes state variables (queues, events, flags) based on provided prompt modes,
        measures initial inference latencies, and starts the background worker threads.

        Args:
            prompt_modes: A dictionary mapping mode names to their prompt text.
            initial_mode: The name of the mode to activate initially.
            tts_engine: The TTS engine to use (e.g., "kokoro", "orpheus").
            llm_provider: The LLM backend provider (e.g., "ollama").
            llm_model: The specific LLM model identifier.
            no_think: If True, removes specific thinking tags from LLM output.
            orpheus_model: Path or identifier for the Orpheus TTS model, if used.
        """
        self.tts_engine = tts_engine
        self.llm_provider = llm_provider
        self.llm_model = llm_model
        self.no_think = no_think
        self.orpheus_model = orpheus_model

        # --- Prompt Mode State (Initialized from arguments) ---
        self.prompt_modes = prompt_modes
        self.current_mode = initial_mode
        self.system_prompt = self.prompt_modes.get(self.current_mode, "") # Get initial prompt
        self.prompt_lock = asyncio.Lock()



        # Validate and log initial prompt state
        if not self.system_prompt:
             logger.warning(f"🗣️⚠️ Initial mode '{self.current_mode}' not found in provided prompts or prompt is empty. Using empty system prompt.")
             # Ensure the mode exists in the dict even if empty, for consistency
             if self.current_mode not in self.prompt_modes:
                 self.prompt_modes[self.current_mode] = ""
        else:
            logger.info(f"🗣️📝 Initial system prompt mode set to '{self.current_mode}' ({len(self.system_prompt)} chars)")

        # Append Orpheus addon after setting the initial prompt
        if tts_engine == "orpheus":
            self.system_prompt += f"\n{orpheus_prompt_addon}"
            logger.info("🗣️✨ Appended Orpheus emotion tags addon to system prompt.")
        # --- Instance Dependencies ---
        self.audio = AudioProcessor(
            engine=self.tts_engine,
            orpheus_model=self.orpheus_model
        )
        self.audio.on_first_audio_chunk_synthesize = self.on_first_audio_chunk_synthesize
        self.text_similarity = TextSimilarity(focus='end', n_words=5)
        self.text_context = TextContext()
        self.generation_counter: int = 0
        self.abort_lock = threading.Lock()
        self.llm = LLM(
            backend=self.llm_provider, # Or your backend
            model=self.llm_model,
            system_prompt=self.system_prompt,
            no_think=self.no_think, # Use self.no_think
        )
        self.llm.prewarm()
        self.llm_inference_time = self.llm.measure_inference_time()
        logger.debug(f"🗣️🧠🕒 LLM inference time: {self.llm_inference_time:.2f}ms")

        # --- State ---
        self.history = []
        self.requests_queue = Queue()
        self.running_generation: Optional[RunningGeneration] = None

        # --- Threading Events ---
        self.shutdown_event = threading.Event()
        self.generator_ready_event = threading.Event()
        self.llm_answer_ready_event = threading.Event()
        self.stop_everything_event = threading.Event()
        self.stop_llm_request_event = threading.Event()
        self.stop_llm_finished_event = threading.Event()
        self.stop_tts_quick_request_event = threading.Event()
        self.stop_tts_quick_finished_event = threading.Event()
        self.stop_tts_final_request_event = threading.Event()
        self.stop_tts_final_finished_event = threading.Event()
        self.abort_completed_event = threading.Event()
        self.abort_block_event = threading.Event()
        self.abort_block_event.set()
        self.check_abort_lock = threading.Lock()

        # --- State Flags ---
        self.llm_generation_active = False
        self.tts_quick_generation_active = False
        self.tts_final_generation_active = False
        self.previous_request = None

        # --- Worker Threads ---
        self.request_processing_thread = threading.Thread(target=self._request_processing_worker, name="RequestProcessingThread", daemon=True)
        self.llm_inference_thread = threading.Thread(target=self._llm_inference_worker, name="LLMProcessingThread", daemon=True)
        self.tts_quick_inference_thread = threading.Thread(target=self._tts_quick_inference_worker, name="TTSQuickProcessingThread", daemon=True)
        self.tts_final_inference_thread = threading.Thread(target=self._tts_final_inference_worker, name="TTSFinalProcessingThread", daemon=True)

        self.request_processing_thread.start()
        self.llm_inference_thread.start()
        self.tts_quick_inference_thread.start()
        self.tts_final_inference_thread.start()

        self.on_partial_assistant_text: Optional[Callable[[str], None]] = None

        self.full_output_pipeline_latency = self.llm_inference_time + self.audio.tts_inference_time
        logger.info(f"🗣️⏱️ Full output pipeline latency: {self.full_output_pipeline_latency:.2f}ms (LLM: {self.llm_inference_time:.2f}ms, TTS: {self.audio.tts_inference_time:.2f}ms)")

        logger.info("🗣️🚀 SpeechPipelineManager initialized and workers started.")

    def is_valid_gen(self) -> bool:
        """
        Checks if there is a currently running generation that has not started aborting.

        Returns:
            True if `running_generation` exists and its `abortion_started` flag is False,
            False otherwise.
        """
        return self.running_generation is not None and not self.running_generation.abortion_started

    def _request_processing_worker(self):
        """
        Worker thread target that processes requests from the `requests_queue`.

        Continuously monitors the queue. When a request arrives, it drains the queue
        to process only the most recent one, preventing processing of stale requests.
        It waits for any ongoing abort operation to complete (`abort_block_event`)
        before processing the next request. Handles 'prepare' actions by calling
        `process_prepare_generation`. Runs until `shutdown_event` is set.
        """
        logger.info("🗣️🚀 Request Processor: Starting...")
        while not self.shutdown_event.is_set():
            try:
                # Get the most recent request by emptying the queue first
                request = self.requests_queue.get(block=True, timeout=1)

                if self.previous_request:
                    # Simple timestamp-based deduplication for identical consecutive requests
                    if self.previous_request.data == request.data and isinstance(request.data, str):
                        if request.timestamp - self.previous_request.timestamp < 2:
                            logger.info(f"🗣️🗑️ Request Processor: Skipping duplicate request - {request.action}")
                            continue

                # Drain the queue to get the most recent request
                while not self.requests_queue.empty():
                    skipped_request = self.requests_queue.get(False)  # Non-blocking get
                    logger.debug(f"🗣️🗑️ Request Processor: Skipping older request - {skipped_request.action}")
                    request = skipped_request # Keep the last one we retrieved
                
                self.abort_block_event.wait() # Wait if an abort is in progress
                logger.debug(f"🗣️🔄 Request Processor: Processing most recent request - {request.action}")
                
                if request.action == "prepare":
                    self.process_prepare_generation(request.data)
                    self.previous_request = request
                elif request.action == "finish":
                     # Note: 'finish' action currently has no specific handling logic here.
                     logger.info(f"🗣️🤷 Request Processor: Received 'finish' action (currently no-op).")
                     self.previous_request = request # Still update previous_request
                else:
                    logger.warning(f"🗣️❓ Request Processor: Unknown action '{request.action}'")

            except Empty:
                continue
            except Exception as e:
                logger.exception(f"🗣️💥 Request Processor: Error: {e}")
        logger.info("🗣️🏁 Request Processor: Shutting down.")

    def on_first_audio_chunk_synthesize(self):
        """
        Callback method invoked by AudioProcessor when the first TTS audio chunk is ready.

        Sets the `quick_answer_first_chunk_ready` flag on the current `running_generation`
        if one exists. This flag might be used for fine-grained timing or state checks.
        """
        logger.info("🗣️🎶 First audio chunk synthesized. Setting TTS quick allowed event.")
        if self.running_generation:
            self.running_generation.quick_answer_first_chunk_ready = True

    def preprocess_chunk(self, chunk: str) -> str:
        """
        Preprocesses a text chunk before sending it to the TTS engine.

        Replaces specific characters (em-dashes, quotes, ellipsis) with simpler equivalents
        to potentially improve TTS pronunciation or compatibility.

        Args:
            chunk: The input text chunk.

        Returns:
            The preprocessed text chunk.
        """
        return chunk.replace("—", "-").replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'").replace("…", "...")

    def clean_quick_answer(self, text: str) -> str:
        """
        Removes specific leading patterns (like '<think>', newlines, spaces) from text.

        Intended for cleaning the initial output of the LLM, especially when `no_think`
        is enabled, to remove processing tags before TTS.

        Args:
            text: The input text.

        Returns:
            The text with specified leading patterns removed.
        """
        patterns_to_remove = ["<think>", "</think>", "\n", " "]
        previous_text = None
        current_text = text
        
        while previous_text != current_text:
            previous_text = current_text
            
            # Remove all patterns from the beginning of the string
            for pattern in patterns_to_remove:
                while current_text.startswith(pattern):
                    current_text = current_text[len(pattern):]
        
        return current_text

    def _llm_inference_worker(self):
        """
        Worker thread target that handles LLM inference for a generation.

        Waits for `generator_ready_event`. Once signaled, it iterates through the
        LLM generator provided in `running_generation`. It accumulates the generated
        text, optionally cleans it (`no_think`), checks for a natural sentence boundary
        to define the `quick_answer` using `TextContext`. If a quick answer is found,
        it signals `llm_answer_ready_event`. Handles stop requests (`stop_llm_request_event`)
        and signals completion/abortion via `stop_llm_finished_event` and internal flags.
        Runs until `shutdown_event` is set.
        """
        logger.info("🗣️🧠 LLM Worker: Starting...")
        while not self.shutdown_event.is_set():
            
            ready = self.generator_ready_event.wait(timeout=1.0)
            if not ready:
                continue

            # Check if aborted *while waiting* before clearing the ready event
            if self.stop_llm_request_event.is_set():
                logger.info("🗣️🧠❌ LLM Worker: Abort detected while waiting for generator_ready_event.")
                self.stop_llm_request_event.clear()
                self.stop_llm_finished_event.set()
                self.llm_generation_active = False
                continue # Go back to waiting

            self.generator_ready_event.clear()
            self.stop_everything_event.clear() # Assuming a new generation clears global stop
            current_gen = self.running_generation

            if not current_gen or not current_gen.llm_generator:
                logger.warning("🗣️🧠❓ LLM Worker: No valid generation or generator found after event.")
                self.llm_generation_active = False
                continue # Go back to waiting

            gen_id = current_gen.id
            logger.info(f"🗣️🧠🔄 [Gen {gen_id}] LLM Worker: Processing generation...")

            # Set state for active generation
            self.llm_generation_active = True
            self.stop_llm_finished_event.clear()
            start_time = time.time()
            token_count = 0

            try:
                for chunk in current_gen.llm_generator:
                    # Check for stop *before* processing the chunk
                    if self.stop_llm_request_event.is_set():
                        logger.info(f"🗣️🧠❌ [Gen {gen_id}] LLM Worker: Stop request detected during iteration.")
                        self.stop_llm_request_event.clear()
                        current_gen.llm_aborted = True
                        break # Exit the generator loop

                    chunk = self.preprocess_chunk(chunk)
                    token_count += 1
                    current_gen.quick_answer += chunk
                    if self.no_think:
                        current_gen.quick_answer = self.clean_quick_answer(current_gen.quick_answer)

                    if token_count == 1:
                        logger.info(f"🗣️🧠⏱️ [Gen {gen_id}] LLM Worker: TTFT: {(time.time() - start_time):.4f}s")

                    # Check for quick answer boundary only if not already provided
                    if not current_gen.quick_answer_provided:
                        context, overhang = self.text_context.get_context(current_gen.quick_answer)
                        if context:
                            logger.info(f"🗣️🧠✔️ [Gen {gen_id}] LLM Worker:  {Colors.apply('QUICK ANSWER FOUND:').magenta} {context}, overhang: {overhang}")
                            current_gen.quick_answer = context
                            if self.on_partial_assistant_text:
                                self.on_partial_assistant_text(current_gen.quick_answer)
                            current_gen.quick_answer_overhang = overhang
                            current_gen.quick_answer_provided = True
                            self.llm_answer_ready_event.set() # Signal TTS quick worker
                            break
                            # Do NOT break here, continue iterating to finish the full LLM response


                # Loop finished naturally or broke due to stop request
                logger.info(f"🗣️🧠🏁 [Gen {gen_id}] LLM Worker: Generator loop finished%s" % (" (Aborted)" if current_gen.llm_aborted else ""))

                # If loop finished naturally and no quick answer was ever found (e.g., short response)
                # Set the whole thing as the quick answer.
                if not current_gen.llm_aborted and not current_gen.quick_answer_provided:
                    logger.info(f"🗣️🧠✔️ [Gen {gen_id}] LLM Worker: No context boundary found, using full response as quick answer.")
                    # quick_answer already contains the full text
                    current_gen.quick_answer_provided = True # Mark as provided
                    if self.on_partial_assistant_text:
                        self.on_partial_assistant_text(current_gen.quick_answer)
                    self.llm_answer_ready_event.set() # Signal TTS quick worker

            except Exception as e:
                logger.exception(f"🗣️🧠💥 [Gen {gen_id}] LLM Worker: Error during generation: {e}")
                current_gen.llm_aborted = True # Mark as aborted on error
            finally:
                # Clean up state regardless of how the loop/try block exited
                self.llm_generation_active = False
                self.stop_llm_finished_event.set() # Signal that this worker's processing attempt is done

                if current_gen.llm_aborted:
                    # If LLM was aborted, ensure TTS (both quick and final) is also stopped
                    logger.info(f"🗣️🧠❌ [Gen {gen_id}] LLM Aborted, requesting TTS quick/final stop.")
                    self.stop_tts_quick_request_event.set()
                    self.stop_tts_final_request_event.set()
                    # Wake up TTS quick worker if it's waiting
                    self.llm_answer_ready_event.set()

                logger.info(f"🗣️🧠🏁 [Gen {gen_id}] LLM Worker: Finished processing cycle.")

                current_gen.llm_finished = True
                current_gen.llm_finished_event.set()

    def check_abort(self, txt: str, wait_for_finish: bool = True, abort_reason: str = "unknown") -> bool:
        """
        Checks if the current generation should be aborted based on new input text.

        Compares the provided text (`txt`) with the text of the `running_generation`.
        If a generation is running and not already aborting:
        1. If `txt` is very similar (>= 0.95 similarity) to the running generation's
           input text, it ignores the new request and returns False.
        2. If `txt` is different, it initiates an abort of the current generation
           by calling the public `abort_generation` method.

        If `wait_for_finish` is True, this method waits for the abortion process
        initiated by `abort_generation` to complete before returning.

        If a generation is already in the process of aborting when this method is called,
        it will wait (if `wait_for_finish` is True) for that ongoing abort to finish.

        Args:
            txt: The new text input to check against the current generation's input.
            wait_for_finish: Whether to block until the initiated/ongoing abort completes.
            abort_reason: A string describing why the abort check is being performed.

        Returns:
            True if an abortion was processed (either newly initiated or waited for),
            False if no active generation was found or the new text was too similar.
        """
        with self.check_abort_lock:
            if self.running_generation:
                current_gen_id_str = f"Gen {self.running_generation.id}"
                logger.info(f"🗣️🛑❓ {current_gen_id_str} Abort check requested (reason: {abort_reason})")

                if self.running_generation.abortion_started:
                    logger.info(f"🗣️🛑⏳ {current_gen_id_str} Active generation is already aborting, waiting to finish (if requested).")

                    # Only wait if wait_for_finish is True
                    if wait_for_finish:
                        start_time = time.time()
                        # Wait using the abort_completed_event for better synchronization
                        completed = self.abort_completed_event.wait(timeout=5.0) # Use the event from abort_generation

                        if not completed:
                             logger.error(f"🗣️🛑💥💥 {current_gen_id_str} Timeout waiting for ongoing abortion to complete. State inconsistency possible!")
                             # Force clear it just in case, though this indicates a deeper issue.
                             self.running_generation = None
                        elif self.running_generation is not None:
                            logger.error(f"🗣️🛑💥💥 {current_gen_id_str} Abortion completed event set, but running_generation still exists. State inconsistency likely!")
                            # Force clear it.
                            self.running_generation = None
                        else:
                            logger.info(f"🗣️🛑✅ {current_gen_id_str} Ongoing abortion finished.")
                    else:
                        logger.info(f"🗣️🛑🏃 {current_gen_id_str} Not waiting for ongoing abortion as wait_for_finish=False")

                    return True # An abort was processed (waited for)
                else:
                    # No abortion in progress, check similarity
                    logger.info(f"🗣️🛑🤔 {current_gen_id_str} Found active generation, checking text similarity.")
                    try:
                         # Ensure running_generation.text is not None before comparison
                        if self.running_generation.text is None:
                            logger.warning(f"🗣️🛑❓ {current_gen_id_str} Running generation text is None, cannot compare similarity. Assuming different.")
                            similarity = 0.0
                        else:
                            similarity = self.text_similarity.calculate_similarity(self.running_generation.text, txt)
                    except Exception as e:
                        logger.warning(f"🗣️🛑💥 {current_gen_id_str} Error calculating similarity: {e}. Assuming different.")
                        similarity = 0.0 # Assume different on error

                    if similarity >= 0.95:
                        logger.info(f"🗣️🛑🙅 {current_gen_id_str} Text ('{txt[:30]}...') too similar ({similarity:.2f}) to current '{self.running_generation.text[:30] if self.running_generation.text else 'None'}...'. Ignoring.")
                        return False # No abort needed

                    # Texts are different enough, initiate abort
                    logger.info(f"🗣️🛑🚀 {current_gen_id_str} Text ('{txt[:30]}...') different enough ({similarity:.2f}) from '{self.running_generation.text[:30] if self.running_generation.text else 'None'}...'. Requesting synchronous abort.")
                    start_time = time.time()
                    # Call the synchronous public abort method - THIS IS KEY
                    self.abort_generation(wait_for_completion=wait_for_finish, timeout=7.0, reason=f"check_abort found different text ({abort_reason})")

                    if wait_for_finish:
                         # Check state *after* waiting for the abort call
                        if self.running_generation is not None:
                            logger.error(f"🗣️🛑💥💥 {current_gen_id_str} !!! Abort call completed but running_generation is still not None. State inconsistency likely!")
                            # Force clear it.
                            self.running_generation = None
                        else:
                            logger.info(f"🗣️🛑✅ {current_gen_id_str} Synchronous abort completed in {time.time() - start_time:.2f}s.")

                    return True # An abort was processed (initiated)
            else:
                logger.info("🗣️🛑🤷 No active generation found during abort check.")
                return False # No active generation to abort

    def _tts_quick_inference_worker(self):
        """
        Worker thread target that handles TTS synthesis for the 'quick answer'.

        Waits for `llm_answer_ready_event`. Once signaled, it checks if the generation
        is valid and has a `quick_answer`. It then waits for the `tts_quick_allowed_event`
        (intended for potential rate limiting or timing control, currently seems unused).
        If allowed, it calls `audio.synthesize` with the `quick_answer`, feeding audio
        chunks into the `audio_chunks` queue. Handles stop requests
        (`stop_tts_quick_request_event`) and signals completion/abortion via
        `stop_tts_quick_finished_event` and internal flags. Runs until `shutdown_event` is set.
        """
        logger.info("🗣️👄🚀 Quick TTS Worker: Starting...")
        while not self.shutdown_event.is_set():
            ready = self.llm_answer_ready_event.wait(timeout=1.0)
            if not ready:
                continue

            # Check if aborted *while waiting* before clearing the ready event
            if self.stop_tts_quick_request_event.is_set():
                logger.info("🗣️👄❌ Quick TTS Worker: Abort detected while waiting for llm_answer_ready_event.")
                self.stop_tts_quick_request_event.clear()
                self.stop_tts_quick_finished_event.set()
                self.tts_quick_generation_active = False
                continue # Go back to waiting

            self.llm_answer_ready_event.clear() # Clear the event now that we're processing
            current_gen = self.running_generation

            if not current_gen or not current_gen.quick_answer:
                logger.warning("🗣️👄❓ Quick TTS Worker: No valid generation or quick answer found after event.")
                self.tts_quick_generation_active = False
                continue # Go back to waiting

            # Double-check if this generation was aborted *just* before we got here
            if current_gen.audio_quick_aborted or current_gen.abortion_started:
                logger.info(f"🗣️👄❌ [Gen {current_gen.id}] Quick TTS Worker: Generation already marked as aborted. Skipping.")
                continue

            gen_id = current_gen.id
            logger.info(f"🗣️👄🔄 [Gen {gen_id}] Quick TTS Worker: Processing TTS for quick answer...")

            # Set state for active generation
            self.tts_quick_generation_active = True
            self.stop_tts_quick_finished_event.clear()
            current_gen.tts_quick_finished_event.clear() # Reset TTS finish marker for this attempt
            current_gen.tts_quick_started = True

            # --- tts_quick_allowed_event Wait Logic ---
            # This event seems intended for external control/timing, but isn't set anywhere
            # in the current code. Added a timeout and logging for clarity. If it's meant
            # to be used, something needs to .set() it externally.
            allowed_to_speak = False
            start_wait_time = time.time()
            wait_timeout = 5.0 # Example timeout
            logger.debug(f"🗣️👄⏳ [Gen {gen_id}] Quick TTS Worker: Waiting for tts_quick_allowed_event (timeout: {wait_timeout}s)...")
            # TODO: Determine if this event is actually used/needed. If not, remove the wait.
            # If it IS needed, ensure something sets it. Currently, it might always timeout.
            # For now, we'll proceed even if it times out, assuming it's optional or not yet implemented.
            # allowed_to_speak = current_gen.tts_quick_allowed_event.wait(timeout=wait_timeout)
            allowed_to_speak = True # Temporarily bypass wait for testing/if event is unused.
            # if not allowed_to_speak:
            #    logger.warning(f"🗣️👄⏱️ [Gen {gen_id}] Quick TTS Worker: Timed out waiting for tts_quick_allowed_event after {time.time() - start_wait_time:.2f}s. Proceeding anyway.")
            # else:
            #    logger.debug(f"🗣️👄✔️ [Gen {gen_id}] Quick TTS Worker: tts_quick_allowed_event received or bypassed.")
            # --- End tts_quick_allowed_event Wait Logic ---


            try:
                # Check again for aborts right before synthesis call
                if self.stop_tts_quick_request_event.is_set() or current_gen.abortion_started:
                     logger.info(f"🗣️👄❌ [Gen {gen_id}] Quick TTS Worker: Aborting TTS synthesis due to stop request or abortion flag.")
                     current_gen.audio_quick_aborted = True
                else:
                    logger.info(f"🗣️👄🎶 [Gen {gen_id}] Quick TTS Worker: Synthesizing: '{current_gen.quick_answer[:50]}...'")
                    completed = self.audio.synthesize(
                        current_gen.quick_answer,
                        current_gen.audio_chunks,
                        self.stop_tts_quick_request_event # Pass the event for the synthesizer to check
                    )

                    if not completed:
                        # Synthesis was stopped by the stop_tts_quick_request_event
                        logger.info(f"🗣️👄❌ [Gen {gen_id}] Quick TTS Worker: Synthesis stopped via event.")
                        current_gen.audio_quick_aborted = True
                    else:
                        logger.info(f"🗣️👄✅ [Gen {gen_id}] Quick TTS Worker: Synthesis completed successfully.")


            except Exception as e:
                logger.exception(f"🗣️👄💥 [Gen {gen_id}] Quick TTS Worker: Error during synthesis: {e}")
                current_gen.audio_quick_aborted = True # Mark as aborted on error
            finally:
                # Clean up state regardless of how the try block exited
                self.tts_quick_generation_active = False
                self.stop_tts_quick_finished_event.set() # Signal that this worker's processing attempt is done
                logger.info(f"🗣️👄🏁 [Gen {gen_id}] Quick TTS Worker: Finished processing cycle.")

                # Check if synthesis completed naturally or was stopped/aborted
                if current_gen.audio_quick_aborted or self.stop_tts_quick_request_event.is_set():
                    logger.info(f"🗣️👄❌ [Gen {gen_id}] Quick TTS Marked as Aborted/Incomplete.")
                    self.stop_tts_quick_request_event.clear() # Clear the request if it was set
                    current_gen.audio_quick_aborted = True # Ensure flag is set
                else:
                    logger.info(f"🗣️👄✅ [Gen {gen_id}] Quick TTS Finished Successfully.")
                    current_gen.tts_quick_finished_event.set() # Signal natural completion

                current_gen.audio_quick_finished = True # Mark quick audio phase as done (even if aborted)

    def _tts_final_inference_worker(self):
        """
        Worker thread target that handles TTS synthesis for the 'final' part of the answer.

        Continuously checks the `running_generation`. It waits until the 'quick' TTS
        phase (`tts_quick_started` and `audio_quick_finished`) is complete and was not
        aborted (`audio_quick_aborted`). It also requires that a `quick_answer` was
        actually identified (`quick_answer_provided`).

        If conditions are met, it sets flags (`tts_final_started`), defines an inner
        generator (`get_generator`) that yields the `quick_answer_overhang` followed
        by the remaining chunks from the `llm_generator`. It then calls
        `audio.synthesize_generator` with this generator, feeding audio chunks into the
        *same* `audio_chunks` queue used by the quick worker. Handles stop requests
        (`stop_tts_final_request_event`) and signals completion/abortion via
        `stop_tts_final_finished_event` and internal flags. Runs until `shutdown_event` is set.
        """
        logger.info("🗣️👄🚀 Final TTS Worker: Starting...")
        while not self.shutdown_event.is_set():
            current_gen = self.running_generation
            time.sleep(0.01) # Prevent tight spinning when idle

            # --- Wait for prerequisites ---
            if not current_gen: continue # No active generation
            if current_gen.tts_final_started: continue # Final TTS already running for this gen
            if not current_gen.tts_quick_started: continue # Quick TTS hasn't even started
            if not current_gen.audio_quick_finished: continue # Quick TTS hasn't finished (successfully or aborted)

            gen_id = current_gen.id # Get ID once prerequisites seem met

            # --- Check conditions to *start* final TTS ---
            if current_gen.audio_quick_aborted:
                #logger.debug(f"🗣️👄🙅 [Gen {gen_id}] Final TTS Worker: Quick TTS was aborted, skipping final TTS.")
                continue
            if not current_gen.quick_answer_provided:
                 logger.debug(f"🗣️👄🙅 [Gen {gen_id}] Final TTS Worker: Quick answer boundary was not found, skipping final TTS (quick TTS handled everything).")
                 continue
            if current_gen.abortion_started:
                 logger.debug(f"🗣️👄🙅 [Gen {gen_id}] Final TTS Worker: Generation is aborting, skipping final TTS.")
                 continue

            # --- Conditions met, start final TTS ---
            logger.info(f"🗣️👄🔄 [Gen {gen_id}] Final TTS Worker: Processing final TTS...")

            def get_generator():
                """Yields remaining text chunks for final TTS synthesis."""
                # Yield overhang first
                if current_gen.quick_answer_overhang:
                    preprocessed_overhang = self.preprocess_chunk(current_gen.quick_answer_overhang)
                    logger.debug(f"🗣️👄< [Gen {gen_id}] Final TTS Gen: Yielding overhang: '{preprocessed_overhang[:50]}...'")
                    current_gen.final_answer += preprocessed_overhang # Add preprocessed version
                    if self.on_partial_assistant_text:
                         logger.debug(f"🗣️👄< [Gen {gen_id}] Final TTS Worker on_partial_assistant_text: Sending overhang.")
                         try:
                            self.on_partial_assistant_text(current_gen.quick_answer + current_gen.final_answer)
                         except Exception as cb_e:
                             logger.warning(f"🗣️💥 Callback error in on_partial_assistant_text (overhang): {cb_e}")
                    yield preprocessed_overhang

                # Yield remaining chunks from LLM generator
                logger.debug(f"🗣️👄< [Gen {gen_id}] Final TTS Gen: Yielding remaining LLM chunks...")
                try:
                    for chunk in current_gen.llm_generator:
                         # Check for stop *before* processing chunk
                         if self.stop_tts_final_request_event.is_set():
                             logger.info(f"🗣️👄❌ [Gen {gen_id}] Final TTS Gen: Stop request detected during LLM iteration.")
                             current_gen.audio_final_aborted = True
                             break # Stop yielding

                         preprocessed_chunk = self.preprocess_chunk(chunk)
                         current_gen.final_answer += preprocessed_chunk
                         if self.on_partial_assistant_text:
                             # logger.debug(f"🗣️👄< [Gen {gen_id}] Final TTS Worker on_partial_assistant_text: Sending final chunk: {preprocessed_chunk[:30]}")
                            try:
                                 self.on_partial_assistant_text(current_gen.quick_answer + current_gen.final_answer)
                            except Exception as cb_e:
                                 logger.warning(f"🗣️💥 Callback error in on_partial_assistant_text (final chunk): {cb_e}")

                         yield preprocessed_chunk
                    logger.debug(f"🗣️👄< [Gen {gen_id}] Final TTS Gen: Finished iterating LLM chunks.")
                except Exception as gen_e:
                     logger.exception(f"🗣️👄💥 [Gen {gen_id}] Final TTS Gen: Error iterating LLM generator: {gen_e}")
                     current_gen.audio_final_aborted = True # Mark as aborted on error

            # Set state for active generation
            self.tts_final_generation_active = True
            self.stop_tts_final_finished_event.clear()
            current_gen.tts_final_started = True
            current_gen.tts_final_finished_event.clear() # Reset TTS finish marker

            try:
                logger.info(f"🗣️👄🎶 [Gen {gen_id}] Final TTS Worker: Synthesizing remaining text...")
                completed = self.audio.synthesize_generator(
                    get_generator(),
                    current_gen.audio_chunks,
                    self.stop_tts_final_request_event # Pass the event for the synthesizer to check
                )

                if not completed:
                     logger.info(f"🗣️👄❌ [Gen {gen_id}] Final TTS Worker: Synthesis stopped via event.")
                     current_gen.audio_final_aborted = True
                else:
                    logger.info(f"🗣️👄✅ [Gen {gen_id}] Final TTS Worker: Synthesis completed successfully.")


            except Exception as e:
                logger.exception(f"🗣️👄💥 [Gen {gen_id}] Final TTS Worker: Error during synthesis: {e}")
                current_gen.audio_final_aborted = True # Mark as aborted on error
            finally:
                # Clean up state regardless of how the try block exited
                self.tts_final_generation_active = False
                self.stop_tts_final_finished_event.set() # Signal that this worker's processing attempt is done
                # logger.info(f"🗣️👄🏁 [Gen {gen_id}] Final TTS Worker: Finished processing cycle. Final answer accumulated: '{current_gen.final_answer[:50]}...'")
                logger.info(f"🗣️👄🏁 [Gen {gen_id}] Final TTS Worker: Finished processing cycle.")


                # Check if synthesis completed naturally or was stopped
                if current_gen.audio_final_aborted or self.stop_tts_final_request_event.is_set():
                    logger.info(f"🗣️👄❌ [Gen {gen_id}] Final TTS Marked as Aborted/Incomplete.")
                    self.stop_tts_final_request_event.clear() # Clear the request if it was set
                    current_gen.audio_final_aborted = True # Ensure flag is set
                else:
                    logger.info(f"🗣️👄✅ [Gen {gen_id}] Final TTS Finished Successfully.")
                    current_gen.tts_final_finished_event.set() # Signal natural completion

                current_gen.audio_final_finished = True # Mark final audio phase as done (even if aborted)


    # --- Processing Methods ---

    def process_prepare_generation(self, txt: str):
        """
        Handles the 'prepare' action: initiates a new text-to-speech generation.

        1. Calls `check_abort` to potentially stop and clean up any existing generation
           if the new input `txt` is significantly different. Waits for the abort to finish.
        2. Increments the `generation_counter`.
        3. Resets state flags and events relevant to starting a new generation.
        4. Creates a new `RunningGeneration` instance with the new ID and input text.
        5. Calls `llm.generate` to get the LLM response generator.
        6. Stores the generator in `running_generation.llm_generator`.
        7. Sets `generator_ready_event` to signal the LLM worker thread to start processing.
        8. Cleans up `running_generation` if LLM generator creation fails.

        Args:
            txt: The user input text for the new generation.
        """
        # --- Abort existing generation if necessary ---
        id_in_spec = self.generation_counter + 1 # Prospective ID for logging
        aborted = self.check_abort(txt, wait_for_finish=True, abort_reason=f"process_prepare_generation for new id {id_in_spec}")

        # --- State is now guaranteed to be clean (running_generation is None) ---
        self.generation_counter += 1
        new_gen_id = self.generation_counter
        logger.info(f"🗣️✨🔄 [Gen {new_gen_id}] Preparing new generation for: '{txt[:50]}...'")

        # Reset flags and events (mostly redundant after sync abort, but safe)
        self.llm_generation_active = False
        self.tts_quick_generation_active = False
        self.tts_final_generation_active = False
        self.llm_answer_ready_event.clear()
        self.generator_ready_event.clear()
        self.stop_llm_request_event.clear()
        self.stop_llm_finished_event.clear()
        self.stop_tts_quick_request_event.clear()
        self.stop_tts_quick_finished_event.clear()
        self.stop_tts_final_request_event.clear()
        self.stop_tts_final_finished_event.clear()
        self.abort_completed_event.clear()
        self.abort_block_event.set() # Ensure block is released if check_abort didn't run/clear it

        # --- Create new generation object ---
        self.running_generation = RunningGeneration(id=new_gen_id)
        self.running_generation.text = txt

        try:
            logger.info(f"🗣️🧠🚀 [Gen {new_gen_id}] Calling LLM generate...")
            # TODO: Update history management if needed
            # self.history.append({"role": "user", "content": txt}) # Example history update
            self.running_generation.llm_generator = self.llm.generate(
                text=txt,
                history=self.history, # Pass current history
                use_system_prompt=True,
            )
            logger.info(f"🗣️🧠✔️ [Gen {new_gen_id}] LLM generator created. Setting generator ready event.")
            self.generator_ready_event.set() # Signal LLM worker
        except Exception as e:
            logger.exception(f"🗣️🧠💥 [Gen {new_gen_id}] Failed to create LLM generator: {e}")
            self.running_generation = None # Clean up if generator creation failed


    def process_abort_generation(self):
        """
        Handles the core logic of aborting the current generation.

        Synchronized using `abort_lock`. If a `running_generation` exists:
        1. Sets the `abortion_started` flag on the generation.
        2. Blocks new requests by clearing `abort_block_event`.
        3. Sets stop request events (`stop_llm_request_event`, `stop_tts_quick_request_event`,
           `stop_tts_final_request_event`) for active worker threads.
        4. Wakes up workers that might be waiting on start events (`generator_ready_event`,
           `llm_answer_ready_event`) so they can see the stop request.
        5. Waits (with timeouts) for each worker to acknowledge the stop by setting their
           respective `stop_..._finished_event`.
        6. Calls external cancellation methods if available (e.g., `llm.cancel_generation`).
        7. Attempts to close the LLM generator stream.
        8. Clears the `running_generation` reference.
        9. Clears stale start events (`generator_ready_event`, `llm_answer_ready_event`).
        10. Signals completion by setting `abort_completed_event`.
        11. Releases the block on new requests by setting `abort_block_event`.
        """
        # This method assumes it's called within the public abort_generation or internally
        with self.abort_lock:
            current_gen_obj = self.running_generation # Store ref before potential clear
            current_gen_id_str = f"Gen {current_gen_obj.id}" if current_gen_obj else "Gen None"

            if current_gen_obj is None or current_gen_obj.abortion_started:
                if current_gen_obj is None:
                    logger.info(f"🗣️🛑🤷 {current_gen_id_str} No active generation found to abort.")
                else:
                    logger.info(f"🗣️🛑⏳ {current_gen_id_str} Abortion already in progress.")
                # Ensure events are managed correctly even if called redundantly
                self.abort_completed_event.set() # Signal completion if nothing to do/already done
                self.abort_block_event.set() # Ensure block is released
                return

            # --- Start Abort Process ---
            logger.info(f"🗣️🛑🚀 {current_gen_id_str} Abortion process starting...")
            current_gen_obj.abortion_started = True # Mark immediately
            self.abort_block_event.clear() # Block new requests *before* waiting
            self.abort_completed_event.clear() # Clear completion flag at start
            self.stop_everything_event.set() # General signal (might be unused by workers)
            aborted_something = False


            # --- Abort LLM ---
            # Check if LLM is potentially active (running OR waiting to start)
            # Need to check generator_ready_event too, as it might be waiting there.
            is_llm_potentially_active = self.llm_generation_active or self.generator_ready_event.is_set()
            if is_llm_potentially_active:
                logger.info(f"🗣️🛑🧠❌ {current_gen_id_str} - Stopping LLM...")
                self.stop_llm_request_event.set()
                self.generator_ready_event.set() # Wake up LLM worker if it's waiting
                stopped = self.stop_llm_finished_event.wait(timeout=5.0) # Wait for LLM worker
                if stopped:
                    logger.info(f"🗣️🛑🧠👍 {current_gen_id_str} LLM stopped confirmation received.")
                    self.stop_llm_finished_event.clear() # Reset for next time
                else:
                    logger.warning(f"🗣️🛑🧠⏱️ {current_gen_id_str} Timeout waiting for LLM stop confirmation.")
                # Attempt external cancellation if available
                if hasattr(self.llm, 'cancel_generation'):
                    logger.info(f"🗣️🛑🧠🔌 {current_gen_id_str} Calling external LLM cancel_generation.")
                    try:
                        self.llm.cancel_generation()
                    except Exception as cancel_e:
                         logger.warning(f"🗣️🛑🧠💥 {current_gen_id_str} Error during external LLM cancel: {cancel_e}")
                self.llm_generation_active = False # Ensure flag is off
                aborted_something = True
            else:
                logger.info(f"🗣️🛑🧠📴 {current_gen_id_str} LLM appears inactive, no stop needed.")
            self.stop_llm_request_event.clear() # Ensure stop request is clear

            # --- Abort Quick TTS ---
            # Check if TTS Quick is potentially active (running OR waiting to start)
            is_tts_quick_potentially_active = self.tts_quick_generation_active or self.llm_answer_ready_event.is_set()
            if is_tts_quick_potentially_active:
                logger.info(f"🗣️🛑👄❌ {current_gen_id_str} Stopping Quick TTS...")
                self.stop_tts_quick_request_event.set()
                self.llm_answer_ready_event.set() # Wake up TTS worker if it's waiting
                stopped = self.stop_tts_quick_finished_event.wait(timeout=5.0) # Wait for TTS worker
                if stopped:
                    logger.info(f"🗣️🛑👄👍 {current_gen_id_str} Quick TTS stopped confirmation received.")
                    self.stop_tts_quick_finished_event.clear() # Reset
                else:
                    logger.warning(f"🗣️🛑👄⏱️ {current_gen_id_str} Timeout waiting for Quick TTS stop confirmation.")
                self.tts_quick_generation_active = False # Ensure flag is off
                aborted_something = True
            else:
                logger.info(f"🗣️🛑👄📴 {current_gen_id_str} Quick TTS appears inactive, no stop needed.")
            self.stop_tts_quick_request_event.clear() # Ensure stop request is clear

            # --- Abort Final TTS ---
            # Check if TTS Final is potentially active (just running, doesn't wait on an event like others)
            is_tts_final_potentially_active = self.tts_final_generation_active
            if is_tts_final_potentially_active:
                logger.info(f"🗣️🛑👄❌ {current_gen_id_str} Stopping Final TTS...")
                self.stop_tts_final_request_event.set()
                # No event to .set() here to wake it up, it polls state
                stopped = self.stop_tts_final_finished_event.wait(timeout=5.0) # Wait for TTS worker
                if stopped:
                    logger.info(f"🗣️🛑👄👍 {current_gen_id_str} Final TTS stopped confirmation received.")
                    self.stop_tts_final_finished_event.clear() # Reset
                else:
                    logger.warning(f"🗣️🛑👄⏱️ {current_gen_id_str} Timeout waiting for Final TTS stop confirmation.")
                self.tts_final_generation_active = False # Ensure flag is off
                aborted_something = True
            else:
                logger.info(f"🗣️🛑👄📴 {current_gen_id_str} Final TTS appears inactive, no stop needed.")
            self.stop_tts_final_request_event.clear() # Ensure stop request is clear

            # --- Stop Audio Playback (if AudioProcessor handles it) ---
            # Assuming AudioProcessor might have playback control that needs stopping
            if hasattr(self.audio, 'stop_playback'):
                logger.info(f"🗣️🛑🔊 {current_gen_id_str} Requesting audio playback stop.")
                try:
                    self.audio.stop_playback() # Or similar method
                except Exception as audio_e:
                    logger.warning(f"🗣️🛑🔊💥 {current_gen_id_str} Error stopping audio playback: {audio_e}")


            # --- Clear the running generation object and close generator ---
            # Re-check self.running_generation in case it changed *during* the waits above
            # Use the initially captured current_gen_obj for closing the generator if needed
            if self.running_generation is not None and self.running_generation.id == current_gen_obj.id:
                logger.info(f"🗣️🛑🧹 {current_gen_id_str} Clearing running generation object.")
                if current_gen_obj.llm_generator and hasattr(current_gen_obj.llm_generator, 'close'):
                    try:
                        logger.info(f"🗣️🛑🧠🔌 {current_gen_id_str} Closing LLM generator stream.")
                        current_gen_obj.llm_generator.close()
                    except Exception as e:
                        logger.warning(f"🗣️🛑🧠💥 {current_gen_id_str} Error closing LLM generator: {e}")
                self.running_generation = None # Clear the reference
            elif self.running_generation is not None and self.running_generation.id != current_gen_obj.id:
                 logger.warning(f"🗣️🛑❓ {current_gen_id_str} Mismatch: self.running_generation changed during abort (now Gen {self.running_generation.id}). Clearing current ref.")
                 self.running_generation = None # Clear the unexpected new one too? Or just log? Clearing seems safer.
            elif aborted_something:
                logger.info(f"🗣️🛑🤷 {current_gen_id_str} Worker(s) aborted but running_generation was already None.")
            else:
                logger.info(f"🗣️🛑🤷 {current_gen_id_str} Nothing seemed active to abort, running_generation is None.")


            # --- Final Cleanup of Trigger Events ---
            # Ensure workers don't accidentally pick up stale signals if they restart quickly
            self.generator_ready_event.clear()
            self.llm_answer_ready_event.clear()

            # --- Signal Completion ---
            logger.info(f"🗣️🛑✅ {current_gen_id_str} Abort processing complete. Setting completion event and releasing block.")
            self.abort_completed_event.set() # Signal that the abort process is fully done
            self.abort_block_event.set() # Release the block for the request processor

    # --- Public Methods ---

    def prepare_generation(self, txt: str):
        """
        Public method to request the preparation of a new speech generation.

        Queues a 'prepare' action with the provided text onto the `requests_queue`
        for the request processing worker thread.

        Args:
            txt: The user input text to be synthesized.
        """
        logger.info(f"🗣️📥 Queueing 'prepare' request for: '{txt[:50]}...'")
        self.requests_queue.put(PipelineRequest("prepare", txt))

    def finish_generation(self):
        """
        Public method to signal the end of user input or interaction.

        Queues a 'finish' action onto the `requests_queue`.
        Note: Currently, the request worker acknowledges this action but doesn't
        trigger specific pipeline behavior based on it. It might be used for
        future features like finalizing history or state.
        """
        logger.info(f"🗣️📥 Queueing 'finish' request")
        self.requests_queue.put(PipelineRequest("finish"))

    def abort_generation(self, wait_for_completion: bool = False, timeout: float = 7.0, reason: str = ""):
        """
        Public method to initiate the abortion of the current speech generation.

        Calls the internal `process_abort_generation` method to handle the actual
        stopping of workers and cleanup. Optionally waits for the abortion to fully
        complete.

        Args:
            wait_for_completion: If True, blocks until the abort process finishes
                                 (signaled by `abort_completed_event`).
            timeout: Maximum time in seconds to wait if `wait_for_completion` is True.
            reason: A string describing why the abort was requested (for logging).
        """
        if self.shutdown_event.is_set():
            logger.warning("🗣️🔌 Shutdown in progress, ignoring abort request.")
            return

        gen_id_str = f"Gen {self.running_generation.id}" if self.running_generation else "Gen None"
        logger.info(f"🗣️🛑🚀 Requesting 'abort' (wait={wait_for_completion}, reason='{reason}') for {gen_id_str}")

        # Call the internal synchronous processor
        self.process_abort_generation()

        # Optionally wait for completion
        if wait_for_completion:
            logger.info(f"🗣️🛑⏳ Waiting for abort completion (timeout={timeout}s)...")
            completed = self.abort_completed_event.wait(timeout=timeout)
            if completed:
                logger.info(f"🗣️🛑✅ Abort completion confirmed.")
            else:
                logger.warning(f"🗣️🛑⏱️ Timeout waiting for abort completion event.")
            # Ensure block is released after waiting, even on timeout
            self.abort_block_event.set()


    def reset(self):
        """
        Resets the pipeline state completely.

        Aborts any currently running generation (waiting for completion) and
        clears the conversation history.
        """
        logger.info("🗣️🔄 Resetting pipeline state...")
        self.abort_generation(wait_for_completion=True, timeout=7.0, reason="reset") # Ensure clean slate
        self.history = []
        logger.info("🗣️🧹 History cleared. Reset complete.")

    def shutdown(self):
        """
        Initiates a graceful shutdown of the pipeline manager and worker threads.

        1. Sets the `shutdown_event`.
        2. Attempts a final abort of any running generation.
        3. Signals all relevant events to unblock any waiting worker threads.
        4. Joins each worker thread with a timeout, logging warnings if they fail to exit.
        """
        logger.info("🗣️🔌 Initiating shutdown...")
        self.shutdown_event.set()

        # Try a final synchronous abort to ensure clean state before join
        logger.info("🗣️🔌🛑 Attempting final abort before joining threads...")
        self.abort_generation(wait_for_completion=True, timeout=3.0, reason="shutdown")

        # Wake up threads that might be waiting on events so they can check shutdown_event
        logger.info("🗣️🔌🔔 Signaling events to wake up any waiting threads...")
        self.generator_ready_event.set()
        self.llm_answer_ready_event.set()
        # Also signal 'finished' and 'completion' events
        self.stop_llm_finished_event.set()
        self.stop_tts_quick_finished_event.set()
        self.stop_tts_final_finished_event.set()
        self.abort_completed_event.set()
        self.abort_block_event.set() # Ensure request processor isn't blocked

        # Join threads
        threads_to_join = [
            (self.request_processing_thread, "Request Processor"),
            (self.llm_inference_thread, "LLM Worker"),
            (self.tts_quick_inference_thread, "Quick TTS Worker"),
            (self.tts_final_inference_thread, "Final TTS Worker"),
        ]

        for thread, name in threads_to_join:
             if thread.is_alive():
                 logger.info(f"🗣️🔌⏳ Joining {name}...")
                 thread.join(timeout=5.0)
                 if thread.is_alive():
                     logger.warning(f"🗣️🔌⏱️ {name} thread did not join cleanly.")
             else:
                  logger.info(f"🗣️🔌👍 {name} thread already finished.")


        logger.info("🗣️🔌✅ Shutdown complete.")

    # --- Prompt Mode Management Methods ---

    def get_available_modes(self) -> list[str]:
        """Returns a list of available prompt mode names."""
        return list(self.prompt_modes.keys())

    async def set_active_mode(self, mode_name: str) -> bool:
        """
        Sets the active prompt mode and updates the system prompt.

        Args:
            mode_name: The name of the mode to activate.

        Returns:
            True if the mode was set successfully, False otherwise.
        """
        async with self.prompt_lock:
            if mode_name not in self.prompt_modes:
                logger.warning(f"🗣️⚠️ Attempt to set invalid mode: {mode_name}")
                return False

            new_prompt_text = self.prompt_modes[mode_name]
            self.current_mode = mode_name
            self.system_prompt = new_prompt_text

            # Append Orpheus addon if needed
            if self.tts_engine == "orpheus":
                self.system_prompt += f"\n{orpheus_prompt_addon}"

            logger.info(f"🗣️📝 System prompt mode changed to '{self.current_mode}' ({len(self.system_prompt)} chars)")

            # Attempt to update the LLM instance with the new prompt
            try:
                self._update_llm_system_prompt(self.system_prompt)
                logger.info("🗣️🧠 Updated system prompt in LLM instance.")
            except Exception as e:
                logger.error(f"🗣️💥 Error updating system prompt in LLM instance: {e}")
                # Decide if this should cause the mode set to fail? For now, log and continue.

            return True

    def _update_llm_system_prompt(self, new_prompt: str):
        """Internal method to update the system prompt in the LLM instance."""
        if hasattr(self.llm, 'set_system_prompt'):
            # If the LLM class has a dedicated method
            self.llm.set_system_prompt(new_prompt)
        elif hasattr(self.llm, 'system_prompt'):
            # Otherwise, try setting the attribute directly
            self.llm.system_prompt = new_prompt
        else:
            logger.warning("🗣️⚠️ LLM instance does not have 'set_system_prompt' method or 'system_prompt' attribute.")