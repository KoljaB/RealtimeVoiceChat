import asyncio
import logging
import os
import struct
import threading
import time
from collections import namedtuple
from queue import Queue
from typing import Callable, Generator, Optional

import numpy as np
import torch
from huggingface_hub import hf_hub_download
# Assuming RealtimeTTS is installed and available
from RealtimeTTS import TextToAudioStream
# Import Chatterbox
from chatterbox.tts import ChatterboxTTS

logger = logging.getLogger(__name__)

# Default configuration constants
START_ENGINE = "chatterbox"
Silence = namedtuple("Silence", ("comma", "sentence", "default"))
ENGINE_SILENCES = {
    "coqui":   Silence(comma=0.3, sentence=0.6, default=0.3),
    "kokoro":  Silence(comma=0.3, sentence=0.6, default=0.3),
    "orpheus": Silence(comma=0.3, sentence=0.6, default=0.3),
    "chatterbox": Silence(comma=0.3, sentence=0.6, default=0.3),
}
# Stream chunk sizes influence latency vs. throughput trade-offs
QUICK_ANSWER_STREAM_CHUNK_SIZE = 8
FINAL_ANSWER_STREAM_CHUNK_SIZE = 30

# Chatterbox adapter class to make it compatible with RealtimeTTS interface
class ChatterboxAdapter:
    """
    适配器类，使Chatterbox的API与RealtimeTTS的TextToAudioStream兼容。
    
    这个类模拟了RealtimeTTS引擎的接口，但内部使用Chatterbox进行实际的语音合成。
    它提供了feed、play、play_async和stop等方法，使其可以作为TextToAudioStream的引擎使用。
    """
    def __init__(self, model, chunk_size=30, temperature=0.8):
        """
        初始化ChatterboxAdapter。
        
        Args:
            model: ChatterboxTTS模型实例
            chunk_size: 音频块大小，影响延迟和吞吐量
            temperature: 生成温度，控制随机性
        """
        self.model = model
        self.sr = model.sr
        self.chunk_size = chunk_size
        self.temperature = temperature
        self.text_buffer = ""
        self.generator = None
        self.is_playing_flag = False
        self.on_audio_stream_stop = None
        self.on_audio_chunk_callback = None
        self.queue = Queue()  # 添加队列属性，用于与RealtimeTTS兼容
        
    def feed(self, text_or_generator):
        """
        提供文本或生成器给适配器。
        
        Args:
            text_or_generator: 要合成的文本字符串或文本生成器
        """
        if callable(getattr(text_or_generator, "__next__", None)):
            # 如果是生成器，保存引用
            self.generator = text_or_generator
        else:
            # 如果是文本，保存到缓冲区
            self.text_buffer = text_or_generator
            
    def play(self, **kwargs):
        """
        同步播放当前缓冲区中的文本。
        
        Args:
            **kwargs: 播放参数，包括on_audio_chunk回调等
        """
        self.on_audio_chunk_callback = kwargs.get("on_audio_chunk")
        self.is_playing_flag = True
        
        try:
            if self.generator:
                # 处理生成器输入
                text = "".join(list(self.generator))
                self._process_text(text, **kwargs)
            else:
                # 处理文本输入
                self._process_text(self.text_buffer, **kwargs)
        finally:
            self.is_playing_flag = False
            if self.on_audio_stream_stop:
                self.on_audio_stream_stop()
                
    def play_async(self, **kwargs):
        """
        异步播放当前缓冲区中的文本。
        
        Args:
            **kwargs: 播放参数，包括on_audio_chunk回调等
        """
        self.on_audio_chunk_callback = kwargs.get("on_audio_chunk")
        self.is_playing_flag = True
        
        # 在新线程中运行同步播放方法
        threading.Thread(
            target=self._play_thread,
            args=(kwargs,),
            daemon=True
        ).start()
    
    def _play_thread(self, kwargs):
        """线程函数，用于异步播放"""
        try:
            if self.generator:
                # 处理生成器输入
                accumulated_text = ""
                for text_chunk in self.generator:
                    if not self.is_playing_flag:
                        break
                    accumulated_text += text_chunk
                    self._process_text(text_chunk, **kwargs)
            else:
                # 处理文本输入
                self._process_text(self.text_buffer, **kwargs)
        finally:
            self.is_playing_flag = False
            if self.on_audio_stream_stop:
                self.on_audio_stream_stop()
    
    def _process_text(self, text, **kwargs):
        """
        处理文本并生成音频块。
        
        Args:
            text: 要处理的文本
            **kwargs: 处理参数
        """
        if not text.strip():
            return
            
        stream_params = {
            'chunk_size': self.chunk_size,
            'temperature': self.temperature,
            'print_metrics': False,
        }
        
        # 从kwargs中提取Chatterbox特有的参数
        if 'audio_prompt_path' in kwargs:
            stream_params['audio_prompt_path'] = kwargs['audio_prompt_path']
        if 'exaggeration' in kwargs:
            stream_params['exaggeration'] = kwargs['exaggeration']
        if 'cfg_weight' in kwargs:
            stream_params['cfg_weight'] = kwargs['cfg_weight']
        
        try:
            # 使用Chatterbox生成音频流
            logging.info(f"开始Chatterbox音频流生成，文本长度: {len(text)}")
            chunk_count = 0
            for audio_chunk, _ in self.model.generate_stream(text, **stream_params):
                chunk_count += 1
                if not self.is_playing_flag:
                    logging.info("Chatterbox生成中断，is_playing_flag为False")
                    break
                    
                # 转换为numpy数组并传递给回调
                audio_data = audio_chunk.cpu().numpy().squeeze()
                
                # 确保音频数据在[-1, 1]范围内
                if audio_data.max() > 1.0 or audio_data.min() < -1.0:
                    audio_data = np.clip(audio_data, -1.0, 1.0)
                
                # 转换为字节格式以与RealtimeTTS兼容
                audio_bytes = audio_data.tobytes()
                
                if self.on_audio_chunk_callback:
                    logging.debug(f"Chatterbox生成第{chunk_count}个音频块，大小: {len(audio_bytes)} 字节")
                    self.on_audio_chunk_callback(audio_bytes)
                else:
                    logging.warning("Chatterbox生成音频块，但on_audio_chunk_callback未设置")
            
            logging.info(f"Chatterbox音频流生成完成，共生成{chunk_count}个音频块")
        except Exception as e:
            logging.error(f"Chatterbox生成过程中出错: {e}")
            import traceback
            logging.error(traceback.format_exc())
    
    def stop(self):
        """停止当前播放"""
        self.is_playing_flag = False
        
    def is_playing(self):
        """返回当前是否正在播放"""
        return self.is_playing_flag
        
    def get_stream_info(self):
        """
        返回音频流的格式、通道数和采样率信息。
        
        Returns:
            tuple: (format, channels, rate) 音频格式、通道数和采样率
        """
        return "pcm", 1, self.sr  # 返回PCM格式，单声道，使用模型的采样率

# Coqui model download helper functions
def create_directory(path: str) -> None:
    """
    Creates a directory at the specified path if it doesn't already exist.

    Args:
        path: The directory path to create.
    """
    if not os.path.exists(path):
        os.makedirs(path)

def ensure_lasinya_models(models_root: str = "models", model_name: str = "Lasinya") -> None:
    """
    Ensures the Coqui XTTS Lasinya model files are present locally.

    Checks for required model files (config.json, vocab.json, etc.) within
    the specified directory structure. If any file is missing, it downloads
    it from the 'KoljaB/XTTS_Lasinya' Hugging Face Hub repository.

    Args:
        models_root: The root directory where models are stored.
        model_name: The specific name of the model subdirectory.
    """
    base = os.path.join(models_root, model_name)
    create_directory(base)
    files = ["config.json", "vocab.json", "speakers_xtts.pth", "model.pth"]
    for fn in files:
        local_file = os.path.join(base, fn)
        if not os.path.exists(local_file):
            # Not using logger here as it might not be configured yet during module import/init
            print(f"👄⏬ Downloading {fn} to {base}")
            hf_hub_download(
                repo_id="KoljaB/XTTS_Lasinya",
                filename=fn,
                local_dir=base
            )

class AudioProcessor:
    """
    Manages Text-to-Speech (TTS) synthesis using various engines via RealtimeTTS.

    This class initializes a chosen TTS engine (Coqui, Kokoro, or Orpheus),
    configures it for streaming output, measures initial latency (TTFT),
    and provides methods to synthesize audio from text strings or generators,
    placing the resulting audio chunks into a queue. It handles dynamic
    stream parameter adjustments and manages the synthesis lifecycle, including
    optional callbacks upon receiving the first audio chunk.
    """
    def __init__(
            self,
            engine: str = START_ENGINE,
            orpheus_model: str = "orpheus-3b-0.1-ft-Q8_0-GGUF/orpheus-3b-0.1-ft-q8_0.gguf",
        ) -> None:
        """
        Initializes the AudioProcessor with a specific TTS engine.

        Sets up the chosen engine (Coqui, Kokoro, Orpheus), downloads Coqui models
        if necessary, configures the RealtimeTTS stream, and performs an initial
        synthesis to measure Time To First Audio chunk (TTFA).

        Args:
            engine: The name of the TTS engine to use ("coqui", "kokoro", "orpheus").
            orpheus_model: The path or identifier for the Orpheus model file (used only if engine is "orpheus").
        """
        self.engine_name = engine
        self.stop_event = threading.Event()
        self.finished_event = threading.Event()
        self.audio_chunks = asyncio.Queue() # Queue for synthesized audio output
        self.orpheus_model = orpheus_model

        self.silence = ENGINE_SILENCES.get(engine, ENGINE_SILENCES[self.engine_name])
        self.current_stream_chunk_size = QUICK_ANSWER_STREAM_CHUNK_SIZE # Initial chunk size

        # Dynamically load and configure the selected TTS engine
        # if engine == "coqui":
        #     ensure_lasinya_models(models_root="models", model_name="Lasinya")
        #     self.engine = CoquiEngine(
        #         specific_model="Lasinya",
        #         local_models_path="./models",
        #         voice="reference_audio.wav",
        #         speed=1.1,
        #         use_deepspeed=True,
        #         thread_count=6,
        #         stream_chunk_size=self.current_stream_chunk_size,
        #         overlap_wav_len=1024,
        #         load_balancing=True,
        #         load_balancing_buffer_length=0.5,
        #         load_balancing_cut_off=0.1,
        #         add_sentence_filter=True,
        #     )
        # elif engine == "kokoro":
        #     self.engine = KokoroEngine(
        #         voice="af_heart",
        #         default_speed=1.26,
        #         trim_silence=True,
        #         silence_threshold=0.01,
        #         extra_start_ms=25,
        #         extra_end_ms=15,
        #         fade_in_ms=15,
        #         fade_out_ms=10,
        #     )
        # elif engine == "orpheus":
        #     self.engine = OrpheusEngine(
        #         model=self.orpheus_model,
        #         temperature=0.8,
        #         top_p=0.95,
        #         repetition_penalty=1.1,
        #         max_tokens=1200,
        #     )
        #     voice = OrpheusVoice("tara")
        #     self.engine.set_voice(voice)
        if engine == "chatterbox":
            # 创建Chatterbox模型
            device = "cuda" if torch.cuda.is_available() else "cpu"
            if device == "cpu":
                logger.warning("CUDA不可用，Chatterbox将在CPU上运行，性能可能受到影响")
            
            chatterbox_model = ChatterboxTTS.from_pretrained(
                device=device
            )
            # 使用适配器包装模型
            self.engine = ChatterboxAdapter(
                model=chatterbox_model,
                chunk_size=30,  # 可以根据需要调整
                temperature=0.8
            )
            self.sr = self.engine.sr
            self.tts_engine = engine  # 保存引擎类型，用于cleanup_resources
        else:
            raise ValueError(f"Unsupported engine: {engine}")


        # 为Chatterbox引擎创建自定义初始化逻辑
        if self.engine_name == "chatterbox":
            # 直接使用ChatterboxAdapter，不通过TextToAudioStream
            # 因为TextToAudioStream期望引擎有特定的接口
            self.stream = self.engine
            self.stream.on_audio_stream_stop = self.on_audio_stream_stop
        else:
            # 对于其他引擎，使用标准的TextToAudioStream
            self.stream = TextToAudioStream(
                self.engine,
                muted=True, # Do not play audio directly
                playout_chunk_size=4096, # Internal chunk size for processing
                on_audio_stream_stop=self.on_audio_stream_stop,
            )

        # Ensure Coqui engine starts with the quick chunk size
        if self.engine_name == "coqui" and hasattr(self.engine, 'set_stream_chunk_size') and self.current_stream_chunk_size != QUICK_ANSWER_STREAM_CHUNK_SIZE:
            logger.info(f"👄⚙️ Setting Coqui stream chunk size to {QUICK_ANSWER_STREAM_CHUNK_SIZE} for initial setup.")
            self.engine.set_stream_chunk_size(QUICK_ANSWER_STREAM_CHUNK_SIZE)
            self.current_stream_chunk_size = QUICK_ANSWER_STREAM_CHUNK_SIZE

        # Prewarm the engine
        if self.engine_name == "chatterbox":
            # Chatterbox引擎的预热逻辑
            logger.info("👄🔥 预热Chatterbox引擎")
            # 简化的预热过程，直接设置一个合理的推断时间
            self.tts_inference_time = 500  # 假设500ms的推断时间
        else:
            # 原有引擎的预热逻辑
            self.stream.feed("prewarm")
            play_kwargs = dict(
                log_synthesized_text=False, # Don't log prewarm text
                muted=True,
                fast_sentence_fragment=False,
                comma_silence_duration=self.silence.comma,
                sentence_silence_duration=self.silence.sentence,
                default_silence_duration=self.silence.default,
                force_first_fragment_after_words=999999, # Effectively disable this
            )
            self.stream.play(**play_kwargs) # Synchronous play for prewarm
            # Wait for prewarm to finish (indicated by on_audio_stream_stop)
            while self.stream.is_playing():
                time.sleep(0.01)
            self.finished_event.wait() # Wait for stop callback
            self.finished_event.clear()

            # Measure Time To First Audio (TTFA)
            start_time = time.time()
            ttfa = None
            def on_audio_chunk_ttfa(chunk: bytes):
                nonlocal ttfa
                if ttfa is None:
                    ttfa = time.time() - start_time
                    logger.debug(f"👄⏱️ TTFA measurement first chunk arrived, TTFA: {ttfa:.2f}s.")

            self.stream.feed("This is a test sentence to measure the time to first audio chunk.")
            play_kwargs_ttfa = dict(
                on_audio_chunk=on_audio_chunk_ttfa,
                log_synthesized_text=False, # Don't log test sentence
                muted=True,
                fast_sentence_fragment=False,
                comma_silence_duration=self.silence.comma,
                sentence_silence_duration=self.silence.sentence,
                default_silence_duration=self.silence.default,
                force_first_fragment_after_words=999999,
            )
            self.stream.play_async(**play_kwargs_ttfa)

            # Wait until the first chunk arrives or stream finishes
            while ttfa is None and (self.stream.is_playing() or not self.finished_event.is_set()):
                time.sleep(0.01)
            self.stream.stop() # Ensure stream stops cleanly

            # Wait for stop callback if it hasn't fired yet
            if not self.finished_event.is_set():
                self.finished_event.wait(timeout=2.0) # Add timeout for safety
            self.finished_event.clear()

            if ttfa is not None:
                logger.debug(f"👄⏱️ TTFA measurement complete. TTFA: {ttfa:.2f}s.")
                self.tts_inference_time = ttfa * 1000  # Store as ms
            else:
                logger.warning("👄⚠️ TTFA measurement failed (no audio chunk received).")
                self.tts_inference_time = 0

        # Callbacks to be set externally if needed
        self.on_first_audio_chunk_synthesize: Optional[Callable[[], None]] = None

    def on_audio_stream_stop(self) -> None:
        """
        Callback executed when the RealtimeTTS audio stream stops processing.

        Logs the event and sets the `finished_event` to signal completion or stop.
        """
        logger.info("👄🛑 Audio stream stopped.")
        self.finished_event.set()

    def _synthesize_chatterbox(
        self,
        text: str,
        audio_chunks: Queue,
        stop_event: threading.Event,
        generation_string: str = "",
    ) -> bool:
        """
        为Chatterbox引擎专门实现的合成方法。
        
        由于Chatterbox引擎与RealtimeTTS的接口不完全兼容，这个方法提供了一个直接使用
        ChatterboxAdapter的替代实现。
        
        Args:
            text: 要合成的文本字符串。
            audio_chunks: 用于存放生成的音频块的队列。
            stop_event: 用于中断合成的事件。
            generation_string: 用于日志记录的可选标识符字符串。
            
        Returns:
            如果合成完全完成则返回True，如果被stop_event中断则返回False。
        """
        logger.info(f"👄🔊 {generation_string} Chatterbox开始合成: {text[:50]}...")
        self.finished_event.clear()  # 在开始前重置完成事件
        
        # 定义音频块回调函数
        def on_audio_chunk(chunk: bytes):
            # 检查中断信号
            if stop_event.is_set():
                logger.info(f"👄🛑 {generation_string} Chatterbox音频流被stop_event中断。")
                return
                
            # 第一个音频块的处理
            if not hasattr(on_audio_chunk, "first_chunk_received"):
                on_audio_chunk.first_chunk_received = True
                logger.info(f"👄🎵 {generation_string} Chatterbox第一个音频块已接收")
                
                # 触发第一个块回调（如果设置了）
                if self.on_first_audio_chunk_synthesize:
                    logger.info(f"👄🎵 {generation_string} 触发第一个音频块回调")
                    self.on_first_audio_chunk_synthesize()
            
            # 将音频块放入队列
            try:
                audio_chunks.put_nowait(chunk)
                logger.debug(f"👄➡️ {generation_string} 音频块已放入队列，大小: {len(chunk)} 字节")
            except asyncio.QueueFull:
                logger.warning(f"👄⚠️ {generation_string} Chatterbox音频队列已满，丢弃块。")
        
        # 设置回调
        self.engine.on_audio_chunk_callback = on_audio_chunk
        
        # 提供文本给引擎
        logger.info(f"👄📝 {generation_string} 提供文本给Chatterbox引擎: {text[:50]}...")
        self.engine.feed(text)
        
        # 异步播放（实际上是合成，因为我们设置了muted=True）
        logger.info(f"👄▶️ {generation_string} 开始Chatterbox异步合成")
        self.engine.play_async(
            on_audio_chunk=on_audio_chunk,
            muted=True
        )
        
        # 等待合成完成或被中断
        logger.info(f"👄⏳ {generation_string} 等待Chatterbox合成完成...")
        wait_start = time.time()
        while self.engine.is_playing() and not stop_event.is_set():
            time.sleep(0.01)
            # 每隔5秒记录一次等待状态
            if time.time() - wait_start > 5:
                logger.info(f"👄⏳ {generation_string} 仍在等待Chatterbox合成完成...")
                wait_start = time.time()
            
        # 如果被中断，停止引擎
        if stop_event.is_set():
            logger.info(f"👄🛑 {generation_string} Chatterbox合成被中断")
            self.engine.stop()
            return False
            
        # 等待完成回调触发
        if not self.finished_event.is_set():
            logger.info(f"👄⏳ {generation_string} 等待完成回调触发...")
            self.finished_event.wait(timeout=2.0)  # 添加超时以确保安全
            if not self.finished_event.is_set():
                logger.warning(f"👄⚠️ {generation_string} 完成回调超时")
        self.finished_event.clear()
        
        logger.info(f"👄✅ {generation_string} Chatterbox合成完成")
        return True
    def synthesize(
        self,
        text: str,
        audio_chunks: Queue,
        stop_event: threading.Event,
        generation_string: str = "",
    ) -> bool:
        """
        Synthesizes audio from a complete text string and puts chunks into a queue.

        Feeds the entire text string to the TTS engine. As audio chunks are generated,
        they are potentially buffered initially for smoother streaming and then put
        into the provided queue. Synthesis can be interrupted via the stop_event.
        Skips initial silent chunks if using the Orpheus engine. Triggers the
        `on_first_audio_chunk_synthesize` callback when the first valid audio chunk is queued.

        Args:
            text: The text string to synthesize.
            audio_chunks: The queue to put the resulting audio chunks (bytes) into.
                          This should typically be the instance's `self.audio_chunks`.
            stop_event: A threading.Event to signal interruption of the synthesis.
                        This should typically be the instance's `self.stop_event`.
            generation_string: An optional identifier string for logging purposes.

        Returns:
            True if synthesis completed fully, False if interrupted by stop_event.
        """
        # 针对Chatterbox引擎的特殊处理
        if self.engine_name == "chatterbox":
            return self._synthesize_chatterbox(text, audio_chunks, stop_event, generation_string)
            
        # 原有引擎的处理逻辑
        if self.engine_name == "coqui" and hasattr(self.engine, 'set_stream_chunk_size') and self.current_stream_chunk_size != QUICK_ANSWER_STREAM_CHUNK_SIZE:
            logger.info(f"👄⚙️ {generation_string} Setting Coqui stream chunk size to {QUICK_ANSWER_STREAM_CHUNK_SIZE} for quick synthesis.")
            self.engine.set_stream_chunk_size(QUICK_ANSWER_STREAM_CHUNK_SIZE)
            self.current_stream_chunk_size = QUICK_ANSWER_STREAM_CHUNK_SIZE

        self.stream.feed(text)
        self.finished_event.clear() # Reset finished event before starting

        # Buffering state variables
        buffer: list[bytes] = []
        good_streak: int = 0
        buffering: bool = True
        buf_dur: float = 0.0
        SR, BPS = 24000, 2 # Assumed Sample Rate and Bytes Per Sample (16-bit)
        start = time.time()
        self._quick_prev_chunk_time: float = 0.0 # Track time of previous chunk

        def on_audio_chunk(chunk: bytes):
            nonlocal buffer, good_streak, buffering, buf_dur, start
            # Check for interruption signal
            if stop_event.is_set():
                logger.info(f"👄🛑 {generation_string} Quick audio stream interrupted by stop_event. Text: {text[:50]}...")
                # We should not put more chunks, let the main loop handle stream stop
                return

            now = time.time()
            samples = len(chunk) // BPS
            play_duration = samples / SR # Duration of the current chunk

            # --- Orpheus specific: Skip initial silence ---
            if on_audio_chunk.first_call and self.engine_name == "orpheus":
                if not hasattr(on_audio_chunk, "silent_chunks_count"):
                    # Initialize silence detection state
                    on_audio_chunk.silent_chunks_count = 0
                    on_audio_chunk.silent_chunks_time = 0.0
                    on_audio_chunk.silence_threshold = 200 # Amplitude threshold for silence

                try:
                    # Analyze chunk for silence
                    fmt = f"{samples}h" # Format for 16-bit signed integers
                    pcm_data = struct.unpack(fmt, chunk)
                    avg_amplitude = np.abs(np.array(pcm_data)).mean()

                    if avg_amplitude < on_audio_chunk.silence_threshold:
                        on_audio_chunk.silent_chunks_count += 1
                        on_audio_chunk.silent_chunks_time += play_duration
                        logger.debug(f"👄⏭️ {generation_string} Quick Skipping silent chunk {on_audio_chunk.silent_chunks_count} (avg_amp: {avg_amplitude:.2f})")
                        return # Skip this chunk
                    elif on_audio_chunk.silent_chunks_count > 0:
                        # First non-silent chunk after silence
                        logger.info(f"👄⏭️ {generation_string} Quick Skipped {on_audio_chunk.silent_chunks_count} silent chunks, saved {on_audio_chunk.silent_chunks_time*1000:.2f}ms")
                        # Proceed to process this non-silent chunk
                except Exception as e:
                    logger.warning(f"👄⚠️ {generation_string} Quick Error analyzing audio chunk for silence: {e}")
                    # Proceed assuming not silent on error

            # --- Timing and Logging ---
            if on_audio_chunk.first_call:
                on_audio_chunk.first_call = False
                self._quick_prev_chunk_time = now
                ttfa_actual = now - start
                logger.info(f"👄🚀 {generation_string} Quick audio start. TTFA: {ttfa_actual:.2f}s. Text: {text[:50]}...")
            else:
                gap = now - self._quick_prev_chunk_time
                self._quick_prev_chunk_time = now
                if gap <= play_duration * 1.1: # Allow small tolerance
                    # logger.debug(f"👄✅ {generation_string} Quick chunk ok (gap={gap:.3f}s ≤ {play_duration:.3f}s). Text: {text[:50]}...")
                    good_streak += 1
                else:
                    logger.warning(f"👄❌ {generation_string} Quick chunk slow (gap={gap:.3f}s > {play_duration:.3f}s). Text: {text[:50]}...")
                    good_streak = 0 # Reset streak on slow chunk

            put_occurred_this_call = False # Track if put happened in this specific call

            # --- Buffering Logic ---
            buffer.append(chunk) # Always append the received chunk first
            buf_dur += play_duration # Update buffer duration

            if buffering:
                # Check conditions to flush buffer and stop buffering
                if good_streak >= 2 or buf_dur >= 0.5: # Flush if stable or buffer > 0.5s
                    logger.info(f"👄➡️ {generation_string} Quick Flushing buffer (streak={good_streak}, dur={buf_dur:.2f}s).")
                    for c in buffer:
                        try:
                            audio_chunks.put_nowait(c)
                            put_occurred_this_call = True
                        except asyncio.QueueFull:
                            logger.warning(f"👄⚠️ {generation_string} Quick audio queue full, dropping chunk.")
                    buffer.clear()
                    buf_dur = 0.0 # Reset buffer duration
                    buffering = False # Stop buffering mode
            else: # Not buffering, put chunk directly
                try:
                    audio_chunks.put_nowait(chunk)
                    put_occurred_this_call = True
                except asyncio.QueueFull:
                    logger.warning(f"👄⚠️ {generation_string} Quick audio queue full, dropping chunk.")


            # --- First Chunk Callback ---
            if put_occurred_this_call and not on_audio_chunk.callback_fired:
                if self.on_first_audio_chunk_synthesize:
                    try:
                        logger.info(f"👄🚀 {generation_string} Quick Firing on_first_audio_chunk_synthesize.")
                        self.on_first_audio_chunk_synthesize()
                    except Exception as e:
                        logger.error(f"👄💥 {generation_string} Quick Error in on_first_audio_chunk_synthesize callback: {e}", exc_info=True)
                # Ensure callback fires only once per synthesize call
                on_audio_chunk.callback_fired = True

        # Initialize callback state for this run
        on_audio_chunk.first_call = True
        on_audio_chunk.callback_fired = False

        play_kwargs = dict(
            log_synthesized_text=True, # Log the text being synthesized
            on_audio_chunk=on_audio_chunk,
            muted=True, # We handle audio via the queue
            fast_sentence_fragment=False, # Standard processing
            comma_silence_duration=self.silence.comma,
            sentence_silence_duration=self.silence.sentence,
            default_silence_duration=self.silence.default,
            force_first_fragment_after_words=999999, # Don't force early fragments
        )

        logger.info(f"👄▶️ {generation_string} Quick Starting synthesis. Text: {text[:50]}...")
        self.stream.play_async(**play_kwargs)

        # Wait loop for completion or interruption
        while self.stream.is_playing() or not self.finished_event.is_set():
            if stop_event.is_set():
                self.stream.stop()
                logger.info(f"👄🛑 {generation_string} Quick answer synthesis aborted by stop_event. Text: {text[:50]}...")
                # Drain remaining buffer if any? Decided against it to stop faster.
                buffer.clear()
                # Wait briefly for stop confirmation? The finished_event handles this.
                self.finished_event.wait(timeout=1.0) # Wait for stream stop confirmation
                return False # Indicate interruption
            time.sleep(0.01)

        # # If loop exited normally, check if buffer still has content (stream finished before flush)
        if buffering and buffer and not stop_event.is_set():
            logger.info(f"👄➡️ {generation_string} Quick Flushing remaining buffer after stream finished.")
            for c in buffer:
                 try:
                    audio_chunks.put_nowait(c)
                 except asyncio.QueueFull:
                    logger.warning(f"👄⚠️ {generation_string} Quick audio queue full on final flush, dropping chunk.")
            buffer.clear()

        logger.info(f"👄✅ {generation_string} Quick answer synthesis complete. Text: {text[:50]}...")
        return True # Indicate successful completion

    def synthesize_generator(
        self,
        generator: Generator[str, None, None],
        audio_chunks: Queue, # Should match self.audio_chunks type
        stop_event: threading.Event,
        generation_string: str = "",
    ) -> bool:
        """
        Synthesizes audio from a generator yielding text chunks and puts audio into a queue.

        Feeds text chunks yielded by the generator to the TTS engine. As audio chunks
        are generated, they are potentially buffered initially and then put into the
        provided queue. Synthesis can be interrupted via the stop_event.
        Skips initial silent chunks if using the Orpheus engine. Sets specific playback
        parameters when using the Orpheus engine. Triggers the
       `on_first_audio_chunk_synthesize` callback when the first valid audio chunk is queued.


        Args:
            generator: A generator yielding text chunks (strings) to synthesize.
            audio_chunks: The queue to put the resulting audio chunks (bytes) into.
                          This should typically be the instance's `self.audio_chunks`.
            stop_event: A threading.Event to signal interruption of the synthesis.
                        This should typically be the instance's `self.stop_event`.
            generation_string: An optional identifier string for logging purposes.

        Returns:
            True if synthesis completed fully, False if interrupted by stop_event.
        """
        if self.engine_name == "coqui" and hasattr(self.engine, 'set_stream_chunk_size') and self.current_stream_chunk_size != FINAL_ANSWER_STREAM_CHUNK_SIZE:
            logger.info(f"👄⚙️ {generation_string} Setting Coqui stream chunk size to {FINAL_ANSWER_STREAM_CHUNK_SIZE} for generator synthesis.")
            self.engine.set_stream_chunk_size(FINAL_ANSWER_STREAM_CHUNK_SIZE)
            self.current_stream_chunk_size = FINAL_ANSWER_STREAM_CHUNK_SIZE

        # Feed the generator to the stream
        self.stream.feed(generator)
        self.finished_event.clear() # Reset finished event

        # Buffering state variables
        buffer: list[bytes] = []
        good_streak: int = 0
        buffering: bool = True
        buf_dur: float = 0.0
        SR, BPS = 24000, 2 # Assumed Sample Rate and Bytes Per Sample
        start = time.time()
        self._final_prev_chunk_time: float = 0.0 # Separate timer for generator synthesis

        def on_audio_chunk(chunk: bytes):
            nonlocal buffer, good_streak, buffering, buf_dur, start
            if stop_event.is_set():
                logger.info(f"👄🛑 {generation_string} Final audio stream interrupted by stop_event.")
                return

            now = time.time()
            samples = len(chunk) // BPS
            play_duration = samples / SR

            # --- Orpheus specific: Skip initial silence ---
            if on_audio_chunk.first_call and self.engine_name == "orpheus":
                if not hasattr(on_audio_chunk, "silent_chunks_count"):
                    on_audio_chunk.silent_chunks_count = 0
                    on_audio_chunk.silent_chunks_time = 0.0
                    # Lower threshold potentially for final answers? Or keep consistent? Using 100 as in original code.
                    on_audio_chunk.silence_threshold = 100

                try:
                    fmt = f"{samples}h"
                    pcm_data = struct.unpack(fmt, chunk)
                    avg_amplitude = np.abs(np.array(pcm_data)).mean()

                    if avg_amplitude < on_audio_chunk.silence_threshold:
                        on_audio_chunk.silent_chunks_count += 1
                        on_audio_chunk.silent_chunks_time += play_duration
                        logger.debug(f"👄⏭️ {generation_string} Final Skipping silent chunk {on_audio_chunk.silent_chunks_count} (avg_amp: {avg_amplitude:.2f})")
                        return # Skip
                    elif on_audio_chunk.silent_chunks_count > 0:
                        logger.info(f"👄⏭️ {generation_string} Final Skipped {on_audio_chunk.silent_chunks_count} silent chunks, saved {on_audio_chunk.silent_chunks_time*1000:.2f}ms")
                except Exception as e:
                    logger.warning(f"👄⚠️ {generation_string} Final Error analyzing audio chunk for silence: {e}")

            # --- Timing and Logging ---
            if on_audio_chunk.first_call:
                on_audio_chunk.first_call = False
                self._final_prev_chunk_time = now
                ttfa_actual = now-start
                logger.info(f"👄🚀 {generation_string} Final audio start. TTFA: {ttfa_actual:.2f}s.")
            else:
                gap = now - self._final_prev_chunk_time
                self._final_prev_chunk_time = now
                if gap <= play_duration * 1.1:
                    # logger.debug(f"👄✅ {generation_string} Final chunk ok (gap={gap:.3f}s ≤ {play_duration:.3f}s).")
                    good_streak += 1
                else:
                    logger.warning(f"👄❌ {generation_string} Final chunk slow (gap={gap:.3f}s > {play_duration:.3f}s).")
                    good_streak = 0

            put_occurred_this_call = False

            # --- Buffering Logic ---
            buffer.append(chunk)
            buf_dur += play_duration
            if buffering:
                if good_streak >= 2 or buf_dur >= 0.5: # Same flush logic as synthesize
                    logger.info(f"👄➡️ {generation_string} Final Flushing buffer (streak={good_streak}, dur={buf_dur:.2f}s).")
                    for c in buffer:
                        try:
                           audio_chunks.put_nowait(c)
                           put_occurred_this_call = True
                        except asyncio.QueueFull:
                            logger.warning(f"👄⚠️ {generation_string} Final audio queue full, dropping chunk.")
                    buffer.clear()
                    buf_dur = 0.0
                    buffering = False
            else: # Not buffering
                try:
                    audio_chunks.put_nowait(chunk)
                    put_occurred_this_call = True
                except asyncio.QueueFull:
                    logger.warning(f"👄⚠️ {generation_string} Final audio queue full, dropping chunk.")


            # --- First Chunk Callback --- (Using the same callback as synthesize)
            if put_occurred_this_call and not on_audio_chunk.callback_fired:
                if self.on_first_audio_chunk_synthesize:
                    try:
                        logger.info(f"👄🚀 {generation_string} Final Firing on_first_audio_chunk_synthesize.")
                        self.on_first_audio_chunk_synthesize()
                    except Exception as e:
                        logger.error(f"👄💥 {generation_string} Final Error in on_first_audio_chunk_synthesize callback: {e}", exc_info=True)
                on_audio_chunk.callback_fired = True

        # Initialize callback state
        on_audio_chunk.first_call = True
        on_audio_chunk.callback_fired = False

        play_kwargs = dict(
            log_synthesized_text=True, # Log text from generator
            on_audio_chunk=on_audio_chunk,
            muted=True,
            fast_sentence_fragment=False,
            comma_silence_duration=self.silence.comma,
            sentence_silence_duration=self.silence.sentence,
            default_silence_duration=self.silence.default,
            force_first_fragment_after_words=999999,
        )

        # Add Orpheus specific parameters for generator streaming
        if self.engine_name == "orpheus":
            # These encourage waiting for more text before synthesizing, potentially better for generators
            play_kwargs["minimum_sentence_length"] = 200
            play_kwargs["minimum_first_fragment_length"] = 200

        logger.info(f"👄▶️ {generation_string} Final Starting synthesis from generator.")
        self.stream.play_async(**play_kwargs)

        # Wait loop for completion or interruption
        while self.stream.is_playing() or not self.finished_event.is_set():
            if stop_event.is_set():
                self.stream.stop()
                logger.info(f"👄🛑 {generation_string} Final answer synthesis aborted by stop_event.")
                buffer.clear()
                self.finished_event.wait(timeout=1.0) # Wait for stream stop confirmation
                return False # Indicate interruption
            time.sleep(0.01)

        # Flush remaining buffer if stream finished before flush condition met
        if buffering and buffer and not stop_event.is_set():
            logger.info(f"👄➡️ {generation_string} Final Flushing remaining buffer after stream finished.")
            for c in buffer:
                try:
                   audio_chunks.put_nowait(c)
                except asyncio.QueueFull:
                   logger.warning(f"👄⚠️ {generation_string} Final audio queue full on final flush, dropping chunk.")
            buffer.clear()

        logger.info(f"👄✅ {generation_string} Final answer synthesis complete.")
        return True # Indicate successful completion
        
    def cleanup_resources(self):
        """清理资源，特别是GPU内存"""
        if hasattr(self, 'tts_engine') and self.tts_engine == "chatterbox" and torch.cuda.is_available():
            torch.cuda.empty_cache()
            logger.info("已清理CUDA缓存")